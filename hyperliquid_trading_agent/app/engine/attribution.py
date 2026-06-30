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
        slippage_bps = _float(ev.get("expected_slippage_bps")) + _float(ev.get("expected_spread_cost_bps")) + _float(ev.get("expected_market_impact_bps"))
        funding_bps = _float(ev.get("expected_funding_cost_bps"))
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
                        "allocated_notional_usd": allocation.get("allocated_notional_usd"),
                        "allocated_size": allocation.get("allocated_size"),
                        "regime_label": (getattr(candidate, "metadata", {}) or {}).get("regime_label") if isinstance(getattr(candidate, "metadata", {}), dict) else None,
                        "candidate_status": getattr(candidate, "status", None),
                        "replay_context_id": replay_context_id,
                    },
                )
            )
        return out

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
        pending = await list_rows(terminal_state="pending", limit=limit)
        matured: list[CandidateOutcomeAttribution] = []
        for row in pending:
            if int(row.get("window_end_ms") or 0) > ts:
                continue
            asset = str(row.get("asset") or "").upper()
            mark_px = _float(marks.get(asset))
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
            outcome = self._mark_outcome(row, mark_px=mark_px, timestamp_ms=ts)
            await self._persist_outcome(outcome)
            matured.append(outcome)
        return matured

    def _mark_outcome(self, row: dict[str, Any], *, mark_px: float, timestamp_ms: int) -> CandidateOutcomeAttribution:
        entry = _float(row.get("entry_px"))
        side = str(row.get("side") or "flat")
        direction = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
        gross = ((mark_px / entry) - 1.0) * 10_000.0 * direction if entry > 0 and direction else 0.0
        fees = _float(row.get("fees_bps"))
        slippage = _float(row.get("slippage_bps"))
        funding = _float(row.get("funding_bps"))
        net = gross - fees - slippage - funding
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        stop = _float(metadata.get("stop"))
        stop_bps = abs(entry - stop) / entry * 10_000.0 if entry > 0 and stop > 0 else 0.0
        realized_r = net / stop_bps if stop_bps > 0 else 0.0
        return CandidateOutcomeAttribution(
            **{
                **row,
                "mark_px": mark_px,
                "gross_return_bps": round(gross, 4),
                "net_return_bps": round(net, 4),
                "realized_r": round(realized_r, 4),
                "mfe_bps": round(max(gross, 0.0), 4),
                "mae_bps": round(min(gross, 0.0), 4),
                "terminal_state": "matured",
                "updated_at_ms": timestamp_ms,
                "metadata": {**metadata, "mark_source": "all_mids", "marked_at_ms": timestamp_ms},
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
        digest = hashlib.sha1(f"{strategy_id}:{asset}:{window_start_ms}:{window_end_ms}:{position_id}:{candidate_id}".encode()).hexdigest()[:24]
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


def _dump(value: Any | None) -> dict[str, Any]:
    if value is None:
        return {}
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return {}


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
