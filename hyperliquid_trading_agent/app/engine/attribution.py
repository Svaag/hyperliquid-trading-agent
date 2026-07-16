from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import (
    CandidateEvidenceLink,
    CandidateOutcomeAttribution,
    PnLAttributionRecord,
)

OUTCOME_WINDOWS_MS: dict[str, int] = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "24h": 24 * 60 * 60_000,
}


class CandidateOutcomeAttributionService:
    """Candidate-level evidence spine for delayed strategy-regime outcomes.

    The service pre-creates fixed horizon rows at decision time, then later marks
    matured rows from observed market prices. Rows are created even for rejected or
    skipped candidates so the engine can learn from trades and no-trades.
    """

    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def record_candidate_evidence(
        self,
        *,
        candidate: Any,
        allocation: Any | None = None,
        ev: Any | None = None,
        risk_decision: Any | None = None,
        council_review: Any | None = None,
        packet: Any | None = None,
        replay_context: dict[str, Any] | None = None,
        created_at_ms: int | None = None,
    ) -> tuple[CandidateEvidenceLink, list[CandidateOutcomeAttribution]]:
        ts = created_at_ms or now_ms()
        risk_payload = _dump(risk_decision)
        council_payload = _dump(council_review)
        allocation_payload = _dump(allocation)
        ev_payload = _dump(ev)
        packet_payload = _dump(packet)
        replay_context = replay_context or {}
        replay_context_id = _replay_context_id(replay_context)
        allocation_id = _text(allocation_payload.get("allocation_id"))
        packet_id = _text(packet_payload.get("packet_id"))
        risk_decision_id = _text(risk_payload.get("decision_id"))
        council_review_id = _text(council_payload.get("review_id"))
        outcomes = self._build_outcome_windows(
            candidate=candidate,
            allocation=allocation_payload,
            ev=ev_payload,
            risk_decision=risk_payload,
            council_review=council_payload,
            replay_context_id=replay_context_id,
            created_at_ms=ts,
        )
        for outcome in outcomes:
            await self._persist_outcome(outcome)
        link = CandidateEvidenceLink(
            link_id="cel_" + hashlib.sha1(f"{candidate.candidate_id}:{ts}".encode()).hexdigest()[:24],
            candidate_id=candidate.candidate_id,
            strategy_id=candidate.strategy_id,
            strategy_version=getattr(candidate, "strategy_version", "unknown"),
            strategy_family=getattr(candidate, "strategy_family", "unknown"),
            asset=candidate.asset,
            venue=getattr(candidate, "venue", "hyperliquid"),
            instrument_id=getattr(candidate, "instrument_id", ""),
            underlying_id=getattr(candidate, "underlying_id", ""),
            venue_id=getattr(candidate, "venue_id", ""),
            horizon=getattr(candidate, "horizon", "unknown"),
            regime_snapshot_id=candidate.regime_snapshot_id,
            feature_snapshot_id=candidate.feature_snapshot_id,
            risk_decision_id=risk_decision_id,
            council_review_id=council_review_id,
            replay_context_id=replay_context_id,
            allocation_id=allocation_id,
            packet_id=packet_id,
            outcome_window_ids=[item.attribution_id for item in outcomes],
            created_at_ms=ts,
            metadata={
                "schema_version": 1,
                "artifact_type": "candidate_evidence_link",
                "risk_decision": risk_payload.get("decision"),
                "council_decision": council_payload.get("decision"),
                "allocation_status": allocation_payload.get("status"),
                "replay_status": replay_context.get("status"),
                "outcome_windows": list(OUTCOME_WINDOWS_MS),
            },
        )
        await self._persist_link(link)
        return link, outcomes

    def _build_outcome_windows(
        self,
        *,
        candidate: Any,
        allocation: dict[str, Any],
        ev: dict[str, Any],
        risk_decision: dict[str, Any],
        council_review: dict[str, Any],
        replay_context_id: str | None,
        created_at_ms: int,
    ) -> list[CandidateOutcomeAttribution]:
        out: list[CandidateOutcomeAttribution] = []
        risk_text = str(risk_decision.get("decision") or "not_applicable")
        council_text = str(council_review.get("decision") or "not_reviewed")
        allocation_status = str(allocation.get("status") or "unknown")
        fees_bps = _float(ev.get("expected_fee_bps"))
        slippage_bps = (
            _float(ev.get("expected_slippage_bps"))
            + _float(ev.get("expected_spread_cost_bps"))
            + _float(ev.get("expected_market_impact_bps"))
        )
        funding_bps = _float(ev.get("expected_funding_cost_bps"))
        execution_cost_quote_id = _text(ev.get("execution_cost_quote_id"))
        ev_metadata = _dict(ev.get("metadata"))
        execution_cost_quality = str(ev_metadata.get("cost_quality") or "unavailable")
        start_ms = int(getattr(candidate, "created_at_ms", created_at_ms) or created_at_ms)
        for window, delta_ms in OUTCOME_WINDOWS_MS.items():
            attribution_id = "coa_" + hashlib.sha1(f"{candidate.candidate_id}:{window}".encode()).hexdigest()[:24]
            out.append(
                CandidateOutcomeAttribution(
                    attribution_id=attribution_id,
                    candidate_id=candidate.candidate_id,
                    strategy_id=candidate.strategy_id,
                    strategy_version=getattr(candidate, "strategy_version", "unknown"),
                    strategy_family=getattr(candidate, "strategy_family", "unknown"),
                    asset=candidate.asset,
                    venue=getattr(candidate, "venue", "hyperliquid"),
                    instrument_id=getattr(candidate, "instrument_id", ""),
                    underlying_id=getattr(candidate, "underlying_id", ""),
                    venue_id=getattr(candidate, "venue_id", ""),
                    side=getattr(candidate, "side", "flat"),
                    candidate_horizon=getattr(candidate, "horizon", "unknown"),
                    regime_snapshot_id=candidate.regime_snapshot_id,
                    feature_snapshot_id=candidate.feature_snapshot_id,
                    risk_decision_id=_text(risk_decision.get("decision_id")),
                    council_review_id=_text(council_review.get("review_id")),
                    replay_context_id=replay_context_id,
                    allocation_id=_text(allocation.get("allocation_id")),
                    outcome_window=window,  # type: ignore[arg-type]
                    window_start_ms=start_ms,
                    window_end_ms=start_ms + delta_ms,
                    entry_px=float(candidate.proposed_entry),
                    fees_bps=fees_bps,
                    slippage_bps=slippage_bps,
                    funding_bps=funding_bps,
                    execution_cost_quote_id=execution_cost_quote_id,
                    execution_cost_quality=execution_cost_quality,  # type: ignore[arg-type]
                    risk_decision=risk_text,
                    council_decision=council_text,
                    allocation_status=allocation_status,
                    terminal_state="pending",
                    created_at_ms=created_at_ms,
                    updated_at_ms=created_at_ms,
                    metadata={
                        "stop": getattr(candidate, "stop", None),
                        "targets": list(getattr(candidate, "targets", []) or []),
                        "expected_net_ev_bps": ev.get("net_ev_bps"),
                        "risk_adjusted_utility": ev.get("risk_adjusted_utility"),
                        "expected_fee_bps": ev.get("expected_fee_bps"),
                        "expected_spread_cost_bps": ev.get("expected_spread_cost_bps"),
                        "expected_slippage_bps": ev.get("expected_slippage_bps"),
                        "expected_market_impact_bps": ev.get("expected_market_impact_bps"),
                        "expected_funding_cost_bps": ev.get("expected_funding_cost_bps"),
                        "execution_cost_quote_id": execution_cost_quote_id,
                        "execution_cost_quality": execution_cost_quality,
                        "research_features": _dict(getattr(candidate, "metadata", {})).get(
                            "research_features", {}
                        ),
                        "allocated_notional_usd": allocation.get("allocated_notional_usd"),
                        "allocated_size": allocation.get("allocated_size"),
                        "regime_label": (getattr(candidate, "metadata", {}) or {}).get("regime_label")
                        if isinstance(getattr(candidate, "metadata", {}), dict)
                        else None,
                        "candidate_status": getattr(candidate, "status", None),
                        "replay_context_id": replay_context_id,
                    },
                )
            )
        return out

    async def attach_execution_report(self, *, candidate_id: str, report: Any) -> int:
        """Bind the simulated fill evidence to every horizon row for a candidate."""

        if self.repository is None or not getattr(self.repository, "enabled", False):
            return 0
        list_rows = getattr(self.repository, "list_candidate_outcome_attributions", None)
        if not callable(list_rows):
            return 0
        report_payload = _dump(report)
        rows = await list_rows(candidate_id=candidate_id, limit=100)
        updated = 0
        total_cost = _float(report_payload.get("total_execution_cost_bps"))
        fee_bps = _float(report_payload.get("fee_bps"))
        non_fee_cost = max(0.0, total_cost - fee_bps)
        quality = str(report_payload.get("cost_quality") or "unavailable")
        for row in rows:
            gross = _float(row.get("gross_return_bps"))
            funding = _float(row.get("funding_bps"))
            execution_adjusted = (
                gross - total_cost - funding
                if quality == "measured" and str(row.get("terminal_state")) == "matured"
                else None
            )
            flags = list(row.get("quality_flags") or [])
            if quality != "measured":
                flags.append("execution_cost_not_measured")
            outcome = CandidateOutcomeAttribution(
                **{
                    **row,
                    "fees_bps": fee_bps,
                    "slippage_bps": non_fee_cost,
                    "execution_adjusted_return_bps": execution_adjusted,
                    "execution_cost_quote_id": report_payload.get("execution_cost_quote_id"),
                    "execution_report_id": report_payload.get("report_id"),
                    "execution_cost_quality": quality,
                    "quality_flags": sorted(set(flags)),
                    "updated_at_ms": now_ms(),
                    "metadata": {
                        **_dict(row.get("metadata")),
                        "execution_simulation_model_version": report_payload.get("simulation_model_version"),
                        "execution_fee_schedule_id": report_payload.get("fee_schedule_id"),
                        "execution_book_snapshot_id": report_payload.get("book_snapshot_id"),
                        "total_execution_cost_bps": total_cost,
                    },
                }
            )
            await self._persist_outcome(outcome)
            updated += 1
        return updated

    async def refresh_matured_outcomes(
        self,
        *,
        marks: dict[str, float],
        timestamp_ms: int | None = None,
        limit: int = 1000,
    ) -> list[CandidateOutcomeAttribution]:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return []
        list_rows = getattr(self.repository, "list_candidate_outcome_attributions", None)
        if not callable(list_rows):
            return []
        ts = timestamp_ms or now_ms()
        list_due_rows = getattr(self.repository, "list_due_candidate_outcome_attributions", None)
        if callable(list_due_rows):
            pending = await list_due_rows(timestamp_ms=ts, limit=limit)
        else:
            pending = await list_rows(terminal_state="pending", limit=limit)
            pending = sorted(
                [row for row in pending if int(row.get("window_end_ms") or 0) <= ts],
                key=lambda row: int(row.get("window_end_ms") or 0),
            )
        matured: list[CandidateOutcomeAttribution] = []
        mid_row_cache: dict[str, list[dict[str, Any]]] = {}
        for row in pending:
            if int(row.get("window_end_ms") or 0) > ts:
                continue
            mark = await self._mark_for_outcome(row, marks=marks, timestamp_ms=ts, mid_row_cache=mid_row_cache)
            mark_px = _float(mark.get("mark_px"))
            if mark_px <= 0:
                row = {
                    **row,
                    "terminal_state": "missing_mark",
                    "quality_flags": [*(row.get("quality_flags") or []), "missing_mark_px"],
                    "updated_at_ms": ts,
                }
                outcome = CandidateOutcomeAttribution(**row)
                await self._persist_outcome(outcome)
                matured.append(outcome)
                continue
            outcome = self._mark_outcome(
                row,
                mark_px=mark_px,
                timestamp_ms=ts,
                mark_source=str(mark.get("mark_source") or "unknown"),
                mark_lag_ms=mark.get("mark_lag_ms"),
                mark_ts_ms=mark.get("mark_ts_ms"),
                mark_age_ms=mark.get("mark_age_ms"),
                path_mids=list(mark.get("path_mids") or []),
            )
            await self._persist_outcome(outcome)
            matured.append(outcome)
        return matured

    async def _mark_for_outcome(
        self,
        row: dict[str, Any],
        *,
        marks: dict[str, float],
        timestamp_ms: int,
        mid_row_cache: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        asset = str(row.get("asset") or "").upper()
        target_ms = int(row.get("window_end_ms") or timestamp_ms)
        start_ms = int(row.get("window_start_ms") or target_ms)
        if mid_row_cache is not None and asset in mid_row_cache:
            mid_rows = mid_row_cache[asset]
        else:
            mid_rows = await self._historical_mid_rows(asset)
            if mid_row_cache is not None:
                mid_row_cache[asset] = mid_rows
        path_mids = _path_mids(mid_rows, start_ms=start_ms, end_ms=target_ms)
        nearest = _nearest_mid(mid_rows, target_ms=target_ms)
        if nearest is not None:
            mark_ts, mark_px = nearest
            return {
                "mark_px": mark_px,
                "mark_source": "feature_store_mid",
                "mark_lag_ms": abs(target_ms - mark_ts),
                "mark_ts_ms": mark_ts,
                "mark_age_ms": target_ms - mark_ts,
                "path_mids": path_mids,
            }
        latest_mark = _float(marks.get(asset))
        return {
            "mark_px": latest_mark,
            "mark_source": "latest_mark_fallback",
            "mark_lag_ms": max(0, timestamp_ms - target_ms),
            "mark_ts_ms": timestamp_ms,
            "mark_age_ms": target_ms - timestamp_ms,
            "path_mids": path_mids,
        }

    async def _historical_mid_rows(self, asset: str) -> list[dict[str, Any]]:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return []
        list_values = getattr(self.repository, "list_feature_values", None)
        if not callable(list_values):
            return []
        try:
            return await list_values(asset=asset, feature_name="mid", limit=5000)
        except TypeError:
            try:
                return await list_values(asset=asset, limit=5000)
            except TypeError:
                return await list_values()

    def _mark_outcome(
        self,
        row: dict[str, Any],
        *,
        mark_px: float,
        timestamp_ms: int,
        mark_source: str,
        mark_lag_ms: Any | None,
        mark_ts_ms: Any | None,
        mark_age_ms: Any | None,
        path_mids: list[tuple[int, float]],
    ) -> CandidateOutcomeAttribution:
        entry = _float(row.get("entry_px"))
        side = str(row.get("side") or "flat")
        direction = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
        gross = ((mark_px / entry) - 1.0) * 10_000.0 * direction if entry > 0 and direction else 0.0
        fees = _float(row.get("fees_bps"))
        slippage = _float(row.get("slippage_bps"))
        funding = _float(row.get("funding_bps"))
        net = gross - fees - slippage - funding
        cost_quality = str(row.get("execution_cost_quality") or "unavailable")
        execution_adjusted = (
            gross - fees - slippage - funding if cost_quality == "measured" and row.get("execution_report_id") else None
        )
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        stop = _float(metadata.get("stop"))
        stop_bps = abs(entry - stop) / entry * 10_000.0 if entry > 0 and stop > 0 else 0.0
        realized_r = net / stop_bps if stop_bps > 0 else 0.0
        path_returns = [
            ((px / entry) - 1.0) * 10_000.0 * direction for _, px in path_mids if entry > 0 and px > 0 and direction
        ]
        if direction:
            path_returns.append(gross)
        mfe = max(path_returns) if path_returns else max(gross, 0.0)
        mae = min(path_returns) if path_returns else min(gross, 0.0)
        quality_flags = list(row.get("quality_flags") or [])
        lag_ms = int(mark_lag_ms or 0)
        age_ms = int(mark_age_ms or 0)
        if mark_source == "latest_mark_fallback":
            quality_flags.append("latest_mark_fallback")
        if lag_ms > 60_000:
            quality_flags.append("late_mark")
        if age_ms < 0:
            quality_flags.append("future_mark")
        if cost_quality != "measured":
            quality_flags.append("execution_cost_not_measured")
        return CandidateOutcomeAttribution(
            **{
                **row,
                "mark_px": mark_px,
                "gross_return_bps": round(gross, 4),
                "net_return_bps": round(net, 4),
                "execution_adjusted_return_bps": round(execution_adjusted, 4)
                if execution_adjusted is not None
                else None,
                "realized_r": round(realized_r, 4),
                "mfe_bps": round(mfe, 4),
                "mae_bps": round(mae, 4),
                "terminal_state": "matured",
                "quality_flags": sorted(set(quality_flags)),
                "updated_at_ms": timestamp_ms,
                "metadata": {
                    **metadata,
                    "mark_source": mark_source,
                    "marked_at_ms": timestamp_ms,
                    "mark_ts_ms": int(mark_ts_ms or 0),
                    "mark_age_ms": age_ms,
                    "mark_lag_ms": lag_ms,
                    "path_mark_count": len(path_mids),
                },
            }
        )

    async def _persist_link(self, link: CandidateEvidenceLink) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "upsert_candidate_evidence_link", None)
            if callable(record):
                await record(link.model_dump(mode="json"))

    async def _persist_outcome(self, outcome: CandidateOutcomeAttribution) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "upsert_candidate_outcome_attribution", None)
            if callable(record):
                await record(outcome.model_dump(mode="json"))


class AttributionService:
    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def record_basic(
        self,
        *,
        strategy_id: str,
        asset: str,
        window_start_ms: int,
        window_end_ms: int,
        total_pnl_usd: float,
        fees_usd: float = 0.0,
        funding_usd: float = 0.0,
        position_id: str | None = None,
        candidate_id: str | None = None,
    ) -> PnLAttributionRecord:
        digest = hashlib.sha1(
            f"{strategy_id}:{asset}:{window_start_ms}:{window_end_ms}:{position_id}:{candidate_id}".encode()
        ).hexdigest()[:24]
        residual = total_pnl_usd - fees_usd - funding_usd
        item = PnLAttributionRecord(
            attribution_id="attr_" + digest,
            position_id=position_id,
            candidate_id=candidate_id,
            strategy_id=strategy_id,
            asset=asset,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            alpha_pnl_usd=residual,
            fees_usd=fees_usd,
            funding_usd=funding_usd,
            residual_pnl_usd=0.0,
            total_pnl_usd=total_pnl_usd,
            metrics={"created_at_ms": now_ms()},
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_pnl_attribution", None)
            if callable(record):
                await record(item.model_dump(mode="json"))
        return item


def _path_mids(rows: list[dict[str, Any]], *, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    points = [_feature_mid_point(row) for row in rows]
    return sorted(
        [point for point in points if point is not None and start_ms <= point[0] <= end_ms], key=lambda item: item[0]
    )


def _nearest_mid(rows: list[dict[str, Any]], *, target_ms: int) -> tuple[int, float] | None:
    points = [point for point in (_feature_mid_point(row) for row in rows) if point is not None]
    if not points:
        return None
    return min(points, key=lambda item: (abs(item[0] - target_ms), 0 if item[0] <= target_ms else 1, item[0]))


def _feature_mid_point(row: dict[str, Any]) -> tuple[int, float] | None:
    if row.get("feature_name") not in {None, "mid"}:
        return None
    ts = int(row.get("computed_ts_ms") or row.get("received_ts_ms") or row.get("event_ts_ms") or 0)
    raw = row.get("scalar_value")
    value_payload = (
        row.get("value")
        if isinstance(row.get("value"), dict)
        else row.get("value_json")
        if isinstance(row.get("value_json"), dict)
        else {}
    )
    if raw is None:
        raw = value_payload.get("mid")
    px = _float(raw)
    if ts <= 0 or px <= 0:
        return None
    return ts, px


def _dump(value: Any | None) -> dict[str, Any]:
    if value is None:
        return {}
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _replay_context_id(replay_context: dict[str, Any]) -> str | None:
    for key in ("replay_id", "comparison_id", "proposal_id"):
        if replay_context.get(key):
            return str(replay_context[key])
    metadata = replay_context.get("metadata") if isinstance(replay_context.get("metadata"), dict) else {}
    if metadata.get("replay_dataset_id"):
        return str(metadata["replay_dataset_id"])
    return None
