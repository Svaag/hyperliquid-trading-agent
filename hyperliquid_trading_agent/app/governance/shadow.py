from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.governance.schemas import ReplayResult, ShadowComparisonResult

MIN_REPLAY_SAMPLE_SIZE = 3


class ShadowComparisonService:
    """Evidence-producing replay/shadow comparison for candidate config diffs.

    Replay/shadow results are still advisory evidence only. They never mutate live
    config, prompts, risk limits, model routes, broker permissions, or code.
    """

    def __init__(self, *, repository: Any | None = None):
        self.repository = repository
        self.results: dict[str, ShadowComparisonResult] = {}
        self.replays: dict[str, ReplayResult] = {}

    async def build_replay_bundle(self, decision_id: str) -> dict[str, Any]:
        context = None
        if self._repo_enabled():
            get_context = getattr(self.repository, "get_decision_context", None)
            if callable(get_context):
                context = await get_context(decision_id)
        return {
            "decision_id": decision_id,
            "decision_context": context,
            "status": "available" if context else "insufficient_data",
            "caveats": _paper_caveats(),
        }

    async def replay_candidate_diff(self, proposal_id: str, *, decision_id: str | None = None) -> ReplayResult:
        diff = await self._load_candidate_diff(proposal_id)
        bundle = await self.build_replay_bundle(decision_id) if decision_id else {"status": "not_requested"}
        if diff is None:
            result = ReplayResult(
                replay_id=f"replay_{uuid4().hex}",
                proposal_id=proposal_id,
                decision_id=decision_id,
                status="insufficient_data",
                baseline_metrics={},
                candidate_metrics={},
                diffs={"reason": "candidate_config_diff_not_found", "bundle_status": bundle.get("status")},
                caveats=_paper_caveats(),
                created_at_ms=_now_ms(),
                metadata={"exchange_actions": []},
            )
            await self._record_replay(result)
            return result

        signal_evaluations, event_evaluations, evidence_summary = await self._load_replay_evidence(diff)
        candidate_signals, transform = _apply_candidate_signal_transform(diff, signal_evaluations)
        baseline_metrics = _combined_metrics(signal_evaluations, event_evaluations)
        candidate_metrics = _combined_metrics(candidate_signals, event_evaluations)
        diffs = _metric_deltas(baseline_metrics, candidate_metrics)
        status = _classify_replay(baseline_metrics, candidate_metrics, diffs)
        if not transform["applied"]:
            status = "audit_only" if signal_evaluations or event_evaluations else "insufficient_data"
        result = ReplayResult(
            replay_id=f"replay_{uuid4().hex}",
            proposal_id=proposal_id,
            decision_id=decision_id,
            status=status,  # type: ignore[arg-type]
            baseline_metrics=baseline_metrics,
            candidate_metrics=candidate_metrics,
            diffs={**diffs, "bundle_status": bundle.get("status"), "transform": transform},
            caveats=_paper_caveats(),
            created_at_ms=_now_ms(),
            metadata={
                "candidate_diff_status": diff.get("status"),
                "risk_direction": diff.get("risk_direction"),
                "requires_human_approval": diff.get("requires_human_approval", True),
                "evidence_summary": evidence_summary,
                "exchange_actions": [],
            },
        )
        self.replays[result.replay_id] = result
        await self._record_replay(result)
        return result

    async def compare_candidate_diff(
        self,
        proposal_id: str,
        *,
        baseline_metrics: dict[str, Any] | None = None,
        candidate_metrics: dict[str, Any] | None = None,
    ) -> ShadowComparisonResult:
        diff = await self._load_candidate_diff(proposal_id)
        replay: ReplayResult | None = None
        if baseline_metrics is None and candidate_metrics is None and diff is not None:
            replay = await self.replay_candidate_diff(proposal_id)
            baseline = replay.baseline_metrics
            candidate = replay.candidate_metrics
        else:
            baseline = baseline_metrics or {}
            candidate = candidate_metrics or {}
        deltas = _metric_deltas(baseline, candidate)
        status = "audit_only"
        recommendation = "audit_only"
        if baseline or candidate:
            status, recommendation = _classify_shadow_result(deltas)
        if diff is None:
            status = "insufficient_data"
            recommendation = "needs_more_evidence"
        if replay is not None and replay.status == "insufficient_data":
            status = "insufficient_data"
            recommendation = "needs_more_evidence"
        result = ShadowComparisonResult(
            comparison_id=f"shadow_{uuid4().hex}",
            proposal_id=proposal_id,
            status=status,  # type: ignore[arg-type]
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            metric_deltas=deltas,
            recommendation=recommendation,  # type: ignore[arg-type]
            created_at_ms=_now_ms(),
            metadata={
                "candidate_diff_found": diff is not None,
                "replay_id": replay.replay_id if replay is not None else None,
                "exchange_actions": [],
            },
        )
        self.results[result.comparison_id] = result
        if self._repo_enabled():
            record = getattr(self.repository, "record_shadow_comparison", None)
            if callable(record):
                await record(result.model_dump(mode="json"))
        return result

    async def _load_candidate_diff(self, proposal_id: str) -> dict[str, Any] | None:
        if not self._repo_enabled():
            return None
        get_diff = getattr(self.repository, "get_candidate_config_diff", None)
        if callable(get_diff):
            return await get_diff(proposal_id)
        return None

    async def _load_replay_evidence(self, diff: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        signal_evaluations: dict[str, dict[str, Any]] = {}
        event_evaluations: dict[str, dict[str, Any]] = {}
        evidence_ids = [str(item) for item in diff.get("evidence") or [] if str(item)]
        for evidence_id in evidence_ids:
            await self._load_signal_evidence_id(evidence_id, signal_evaluations)
            await self._load_event_evidence_id(evidence_id, event_evaluations)
        scope = dict(diff.get("scope") or {})
        symbol = scope.get("symbol") or (scope.get("symbols") or [None])[0]
        signal_type = scope.get("signal_type")
        if not signal_evaluations and symbol:
            list_signals = getattr(self.repository, "list_signal_evaluations", None)
            if callable(list_signals):
                for item in await list_signals(status="complete", symbol=str(symbol), limit=500):
                    if signal_type and item.get("signal_type") != signal_type:
                        continue
                    signal_evaluations[str(item.get("id") or item.get("signal_id"))] = item
        if not event_evaluations and symbol:
            list_events = getattr(self.repository, "list_alpha_event_evaluations", None)
            if callable(list_events):
                for item in await list_events(status="complete", symbol=str(symbol), limit=500):
                    event_evaluations[str(item.get("id") or item.get("event_id"))] = item
        return (
            list(signal_evaluations.values()),
            list(event_evaluations.values()),
            {
                "evidence_ids": evidence_ids,
                "signal_evaluation_count": len(signal_evaluations),
                "event_evaluation_count": len(event_evaluations),
                "scope_fallback_used": bool((signal_evaluations or event_evaluations) and not evidence_ids),
            },
        )

    async def _load_signal_evidence_id(self, evidence_id: str, out: dict[str, dict[str, Any]]) -> None:
        if not self._repo_enabled():
            return
        get_eval = getattr(self.repository, "get_signal_evaluation", None)
        if callable(get_eval):
            item = await get_eval(evidence_id)
            if item is not None:
                out[str(item.get("id") or evidence_id)] = item
        get_by_signal = getattr(self.repository, "get_signal_evaluation_by_signal_id", None)
        if callable(get_by_signal):
            item = await get_by_signal(evidence_id)
            if item is not None:
                out[str(item.get("id") or item.get("signal_id") or evidence_id)] = item

    async def _load_event_evidence_id(self, evidence_id: str, out: dict[str, dict[str, Any]]) -> None:
        if not self._repo_enabled():
            return
        get_eval = getattr(self.repository, "get_alpha_event_evaluation", None)
        if callable(get_eval):
            item = await get_eval(evidence_id)
            if item is not None:
                out[str(item.get("id") or evidence_id)] = item
        get_by_event = getattr(self.repository, "get_alpha_event_evaluation_by_event_id", None)
        if callable(get_by_event):
            for item in await get_by_event(evidence_id):
                out[str(item.get("id") or item.get("event_id") or evidence_id)] = item

    async def _record_replay(self, result: ReplayResult) -> None:
        self.replays[result.replay_id] = result
        if self._repo_enabled():
            record = getattr(self.repository, "record_replay_result", None)
            if callable(record):
                await record(result.model_dump(mode="json"))

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)


def _combined_metrics(signal_evaluations: list[dict[str, Any]], event_evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = _signal_metrics(signal_evaluations)
    metrics.update(_event_metrics(event_evaluations))
    return metrics


def _signal_metrics(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in evaluations if item.get("status") in {"complete", "partial"} or item.get("terminal_outcome") not in {None, "open"}]
    r_values = [_float(item.get("realized_or_marked_r")) for item in completed]
    r_values = [item for item in r_values if item is not None]
    opportunity = [_float(item.get("opportunity_cost_r")) for item in completed]
    opportunity = [item for item in opportunity if item is not None]
    stop_count = len([item for item in completed if item.get("stop_hit") or item.get("terminal_outcome") == "stop_hit"])
    tp_count = len([item for item in completed if item.get("take_profit_hit") or item.get("terminal_outcome") == "tp_hit"])
    rejected = [item for item in completed if bool(item.get("rejected"))]
    rejected_missed = [item for item in rejected if (_float(item.get("opportunity_cost_r")) or 0) >= 1.0]
    mfe = [_float(item.get("max_favorable_r")) for item in completed]
    mfe = [item for item in mfe if item is not None]
    mae = [_float(item.get("max_adverse_r")) for item in completed]
    mae = [item for item in mae if item is not None]
    return {
        "sample_size": len(completed),
        "trade_count": len([item for item in completed if item.get("paper_ordered") or item.get("approved")]),
        "avg_r": _avg(r_values),
        "stop_hit_rate": stop_count / len(completed) if completed else None,
        "tp_hit_rate": tp_count / len(completed) if completed else None,
        "rejected_count": len(rejected),
        "rejected_missed_count": len(rejected_missed),
        "missed_opportunity_rate": len(rejected_missed) / len(rejected) if rejected else None,
        "avg_opportunity_cost_r": _avg(opportunity),
        "avg_mfe_r": _avg(mfe),
        "avg_mae_r": _avg(mae),
        "max_drawdown_pct": _drawdown_proxy(r_values),
        "evidence_quality": "paper_simulation",
    }


def _event_metrics(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in evaluations if item.get("status") in {"complete", "partial"} or item.get("terminal_outcome") not in {None, "open"}]
    worked = len([item for item in completed if item.get("terminal_outcome") == "worked"])
    failed = len([item for item in completed if item.get("terminal_outcome") == "failed"])
    bps_values = [_float(item.get("realized_or_marked_bps")) for item in completed]
    bps_values = [item for item in bps_values if item is not None]
    return {
        "event_sample_size": len(completed),
        "event_worked_rate": worked / len(completed) if completed else None,
        "event_failed_rate": failed / len(completed) if completed else None,
        "avg_event_bps": _avg(bps_values),
    }


def _apply_candidate_signal_transform(diff: dict[str, Any], evaluations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    threshold = _extract_min_signal_score(diff.get("proposed_value") or {})
    if threshold is None:
        return list(evaluations), {"applied": False, "reason": "no_deterministic_signal_transform"}
    filtered = [item for item in evaluations if (_float(item.get("signal_score")) or 0) >= threshold]
    return filtered, {"applied": True, "type": "min_signal_score_filter", "threshold": threshold, "dropped": len(evaluations) - len(filtered)}


def _extract_min_signal_score(values: dict[str, Any]) -> float | None:
    for key, value in values.items():
        if "min_signal_score" in str(key):
            return _float(value)
    return None


def _metric_deltas(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    deltas: dict[str, Any] = {}
    for key in sorted(set(baseline) | set(candidate)):
        left = baseline.get(key)
        right = candidate.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            deltas[key] = right - left
        elif left != right:
            deltas[key] = {"baseline": left, "candidate": right}
    return deltas


def _classify_replay(baseline: dict[str, Any], candidate: dict[str, Any], deltas: dict[str, Any]) -> str:
    baseline_sample = int(baseline.get("sample_size") or 0) + int(baseline.get("event_sample_size") or 0)
    candidate_sample = int(candidate.get("sample_size") or 0) + int(candidate.get("event_sample_size") or 0)
    if baseline_sample < MIN_REPLAY_SAMPLE_SIZE or candidate_sample == 0:
        return "insufficient_data"
    avg_r_delta = _float(deltas.get("avg_r")) or 0.0
    stop_rate_delta = _float(deltas.get("stop_hit_rate")) or 0.0
    drawdown_delta = _float(deltas.get("max_drawdown_pct")) or 0.0
    if avg_r_delta >= 0 and stop_rate_delta <= 0 and drawdown_delta <= 0:
        return "passed"
    return "failed"


def _classify_shadow_result(deltas: dict[str, Any]) -> tuple[str, str]:
    pnl_delta = float(deltas.get("avg_r", 0) or 0)
    drawdown_delta = float(deltas.get("max_drawdown_pct", 0) or 0)
    trade_count_delta = float(deltas.get("trade_count", 0) or 0)
    if drawdown_delta > 0.5 or pnl_delta < -0.1:
        return "shadow_failed", "reject"
    if pnl_delta >= 0 and drawdown_delta <= 0 and trade_count_delta <= 0:
        return "shadow_passed", "promote_to_review"
    return "audit_only", "needs_more_evidence"


def _paper_caveats() -> list[str]:
    return [
        "paper fills are simulated",
        "slippage/fees/queue position may be inaccurate",
        "recent samples may be regime-specific",
        "shadow comparison is evidence, not authority to apply config",
        "candidate replay only applies deterministic transforms currently supported by the replay engine",
    ]


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _drawdown_proxy(r_values: list[float]) -> float | None:
    if not r_values:
        return None
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in r_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return max_drawdown


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
