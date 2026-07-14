from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.governance.review import ReviewWorkflowService
from hyperliquid_trading_agent.app.governance.shadow import ShadowComparisonService


class FakeReplayRepository:
    enabled = True

    def __init__(self):
        self.diff = {
            "proposal_id": "tp_replay",
            "strategy_id": "news_event_alpha_v2",
            "scope": {"symbol": "BTC", "event_type": "headline"},
            "change_type": "threshold_adjustment",
            "current_value": {"autonomy_event_eval_min_importance": 50},
            "proposed_value": {"autonomy_event_eval_min_importance": 80},
            "rationale": "Raise the low-importance catalyst threshold.",
            "evidence": ["event_failed", "event_worked", "event_flat", "event_volatile"],
            "expected_effect": "Reduce weak catalyst evaluations.",
            "known_risks": ["May reduce event coverage."],
            "validation_required": ["replay", "shadow_run", "human_review"],
            "risk_direction": "tightens_risk",
            "requires_human_approval": True,
            "status": "proposed",
            "metadata": {},
        }
        self.event_evals = {
            "event_failed": _event_evaluation("eval_failed", "event_failed", outcome="failed", bps=-50),
            "event_worked": _event_evaluation("eval_worked", "event_worked", outcome="worked", bps=80),
            "event_flat": _event_evaluation("eval_flat", "event_flat", outcome="expired_neutral", bps=5),
            "event_volatile": _event_evaluation("eval_volatile", "event_volatile", outcome="volatility_only", bps=-10),
        }
        self.replays: list[dict] = []
        self.shadows: list[dict] = []
        self.review_packets: list[dict] = []
        self.rollback_plans: list[dict] = []
        self.status_updates: list[tuple[str, str]] = []

    async def get_candidate_config_diff(self, proposal_id: str):
        return self.diff if proposal_id == self.diff["proposal_id"] else None

    async def get_alpha_event_evaluation(self, evaluation_id: str):
        return next((item for item in self.event_evals.values() if item["id"] == evaluation_id), None)

    async def get_alpha_event_evaluation_by_event_id(self, event_id: str):
        item = self.event_evals.get(event_id)
        return [item] if item is not None else []

    async def list_alpha_event_evaluations(self, status=None, symbol=None, limit=100):
        return list(self.event_evals.values())[:limit]

    async def record_replay_result(self, result: dict):
        self.replays.insert(0, result)
        return result["replay_id"]

    async def list_replay_results(self, proposal_id=None, limit=100):
        items = [item for item in self.replays if proposal_id is None or item.get("proposal_id") == proposal_id]
        return items[:limit]

    async def record_shadow_comparison(self, result: dict):
        self.shadows.insert(0, result)
        return result["comparison_id"]

    async def list_shadow_comparisons(self, proposal_id=None, limit=100):
        items = [item for item in self.shadows if proposal_id is None or item.get("proposal_id") == proposal_id]
        return items[:limit]

    async def upsert_rollback_plan(self, plan: dict):
        self.rollback_plans.append(plan)
        return plan["rollback_plan_id"]

    async def upsert_review_packet(self, packet: dict):
        self.review_packets.append(packet)
        return packet["review_packet_id"]

    async def set_candidate_config_diff_status(self, proposal_id: str, status: str):
        self.status_updates.append((proposal_id, status))
        self.diff["status"] = status


def _event_evaluation(eval_id: str, event_id: str, *, outcome: str, bps: float):
    return {
        "id": eval_id,
        "event_id": event_id,
        "symbol": "BTC",
        "event_type": "headline",
        "status": "complete",
        "terminal_outcome": outcome,
        "realized_or_marked_bps": bps,
    }


@pytest.mark.asyncio
async def test_replay_candidate_diff_audits_event_evidence_and_persists_metrics():
    repo = FakeReplayRepository()
    service = ShadowComparisonService(repository=repo)

    replay = await service.replay_candidate_diff("tp_replay")

    assert replay.status == "audit_only"
    assert replay.baseline_metrics["event_sample_size"] == 4
    assert replay.candidate_metrics == replay.baseline_metrics
    assert replay.diffs["transform"]["reason"] == "no_deterministic_event_transform"
    assert repo.replays[0]["replay_id"] == replay.replay_id
    assert repo.diff["current_value"] == {"autonomy_event_eval_min_importance": 50}


@pytest.mark.asyncio
async def test_shadow_uses_replay_metrics_and_persists_comparison():
    repo = FakeReplayRepository()
    service = ShadowComparisonService(repository=repo)

    shadow = await service.compare_candidate_diff("tp_replay")

    assert shadow.status == "shadow_passed"
    assert shadow.recommendation == "promote_to_review"
    assert shadow.metadata["replay_id"] == repo.replays[0]["replay_id"]
    assert repo.shadows[0]["comparison_id"] == shadow.comparison_id


@pytest.mark.asyncio
async def test_review_packet_requires_replay_and_shadow_then_marks_review_ready():
    repo = FakeReplayRepository()
    review = ReviewWorkflowService(repository=repo)

    with pytest.raises(PermissionError, match="replay and shadow"):
        await review.create_review_packet("tp_replay")

    shadow_service = ShadowComparisonService(repository=repo)
    await shadow_service.compare_candidate_diff("tp_replay")
    packet = await review.create_review_packet("tp_replay")

    assert packet.replay_results is not None
    assert packet.shadow_results is not None
    assert packet.rollback_plan_id.startswith("rollback_")
    assert ("tp_replay", "review_ready") in repo.status_updates
