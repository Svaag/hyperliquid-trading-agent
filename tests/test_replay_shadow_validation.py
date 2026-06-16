from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.governance.review import ReviewWorkflowService
from hyperliquid_trading_agent.app.governance.shadow import ShadowComparisonService


class FakeReplayRepository:
    enabled = True

    def __init__(self):
        self.diff = {
            "proposal_id": "tp_replay",
            "strategy_id": "autonomy_v1",
            "scope": {"symbol": "BTC", "signal_type": "trend_continuation"},
            "change_type": "threshold_adjustment",
            "current_value": {"autonomy_min_signal_score": 75},
            "proposed_value": {"asset_overrides.BTC.trend_continuation.min_signal_score": 80},
            "rationale": "Raise low-quality threshold.",
            "evidence": ["sig_low", "sig_high", "sig_stop", "sig_good"],
            "expected_effect": "Reduce weak paper signals.",
            "known_risks": ["May reduce trade count."],
            "validation_required": ["replay", "shadow_run", "human_review"],
            "risk_direction": "tightens_risk",
            "requires_human_approval": True,
            "status": "proposed",
            "metadata": {},
        }
        self.signal_evals = {
            "sig_low": _evaluation("eval_low", "sig_low", score=60, r=-0.5, stop=True),
            "sig_high": _evaluation("eval_high", "sig_high", score=85, r=0.5),
            "sig_stop": _evaluation("eval_stop", "sig_stop", score=90, r=-1.0, stop=True),
            "sig_good": _evaluation("eval_good", "sig_good", score=95, r=1.5, tp=True),
        }
        self.replays: list[dict] = []
        self.shadows: list[dict] = []
        self.review_packets: list[dict] = []
        self.rollback_plans: list[dict] = []
        self.status_updates: list[tuple[str, str]] = []

    async def get_candidate_config_diff(self, proposal_id: str):
        return self.diff if proposal_id == self.diff["proposal_id"] else None

    async def get_signal_evaluation(self, evaluation_id: str):
        return next((item for item in self.signal_evals.values() if item["id"] == evaluation_id), None)

    async def get_signal_evaluation_by_signal_id(self, signal_id: str):
        return self.signal_evals.get(signal_id)

    async def get_alpha_event_evaluation(self, evaluation_id: str):
        return None

    async def get_alpha_event_evaluation_by_event_id(self, event_id: str):
        return []

    async def list_signal_evaluations(self, status=None, symbol=None, limit=100):
        return list(self.signal_evals.values())[:limit]

    async def list_alpha_event_evaluations(self, status=None, symbol=None, limit=100):
        return []

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


def _evaluation(eval_id: str, signal_id: str, *, score: float, r: float, stop: bool = False, tp: bool = False):
    return {
        "id": eval_id,
        "signal_id": signal_id,
        "symbol": "BTC",
        "signal_type": "trend_continuation",
        "status": "complete",
        "terminal_outcome": "stop_hit" if stop else "tp_hit" if tp else "expired_positive",
        "signal_score": score,
        "realized_or_marked_r": r,
        "stop_hit": stop,
        "take_profit_hit": tp,
        "approved": True,
        "paper_ordered": True,
        "rejected": False,
        "opportunity_cost_r": None,
        "max_favorable_r": max(r, 0),
        "max_adverse_r": min(r, 0),
    }


@pytest.mark.asyncio
async def test_replay_candidate_diff_computes_and_persists_metrics():
    repo = FakeReplayRepository()
    service = ShadowComparisonService(repository=repo)

    replay = await service.replay_candidate_diff("tp_replay")

    assert replay.status == "passed"
    assert replay.baseline_metrics["sample_size"] == 4
    assert replay.candidate_metrics["sample_size"] == 3
    assert replay.diffs["transform"]["type"] == "min_signal_score_filter"
    assert replay.diffs["avg_r"] > 0
    assert repo.replays[0]["replay_id"] == replay.replay_id
    assert repo.diff["current_value"] == {"autonomy_min_signal_score": 75}


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
