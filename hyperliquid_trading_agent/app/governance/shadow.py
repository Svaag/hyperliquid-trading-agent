from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.governance.schemas import ReplayResult, ShadowComparisonResult


class ShadowComparisonService:
    """Audit-only replay/shadow comparison for candidate config diffs."""

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
        bundle = await self.build_replay_bundle(decision_id) if decision_id else {"status": "audit_only"}
        status = "audit_only" if diff else "insufficient_data"
        result = ReplayResult(
            replay_id=f"replay_{uuid4().hex}",
            proposal_id=proposal_id,
            decision_id=decision_id,
            status=status,  # type: ignore[arg-type]
            baseline_metrics={"source": "persisted_decision_context"},
            candidate_metrics={"candidate_diff": diff or {}},
            diffs={"bundle_status": bundle.get("status")},
            caveats=_paper_caveats(),
            created_at_ms=_now_ms(),
            metadata={"exchange_actions": []},
        )
        self.replays[result.replay_id] = result
        return result

    async def compare_candidate_diff(
        self,
        proposal_id: str,
        *,
        baseline_metrics: dict[str, Any] | None = None,
        candidate_metrics: dict[str, Any] | None = None,
    ) -> ShadowComparisonResult:
        diff = await self._load_candidate_diff(proposal_id)
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
        result = ShadowComparisonResult(
            comparison_id=f"shadow_{uuid4().hex}",
            proposal_id=proposal_id,
            status=status,  # type: ignore[arg-type]
            baseline_metrics=baseline,
            candidate_metrics=candidate,
            metric_deltas=deltas,
            recommendation=recommendation,  # type: ignore[arg-type]
            created_at_ms=_now_ms(),
            metadata={"candidate_diff_found": diff is not None, "exchange_actions": []},
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

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)


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
    ]


def _now_ms() -> int:
    return int(time.time() * 1000)
