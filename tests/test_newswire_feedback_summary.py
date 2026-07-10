from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.newswire.feedback import build_newswire_feedback_summary


class _FeedbackRepository:
    async def list_newswire_deliveries(self, **kwargs):
        return [
            {"delivery_id": "d1", "story_id": "s1", "status": "posted", "posted_at_ms": 1_100},
            {"delivery_id": "d2", "story_id": "s2", "status": "posted", "posted_at_ms": 1_200},
            {"delivery_id": "d3", "story_id": "s3", "status": "posted", "posted_at_ms": 1_300},
            {"delivery_id": "skipped", "story_id": "s4", "status": "skipped", "posted_at_ms": 1_400},
        ]

    async def list_newswire_stories(self, **kwargs):
        return [
            {"story_id": "s1", "source": "alpaca", "assessment": {"priority_score": 69}},
            {"story_id": "s2", "source": "alpaca", "assessment": {"priority_score": 81}},
            {"story_id": "s3", "source": "cointelegraph", "assessment": {"priority_score": 53}},
        ]

    async def list_newswire_evals(self, **kwargs):
        return [
            {"eval_id": "old", "event_id": "s1", "evaluator_id": "u1", "label_type": "quality", "label_value": True, "created_at_ms": 1_150},
            {"eval_id": "new", "event_id": "s1", "evaluator_id": "u1", "label_type": "quality", "label_value": False, "created_at_ms": 1_250},
            {"eval_id": "useful", "event_id": "s2", "evaluator_id": "u2", "label_type": "quality", "label_value": True, "created_at_ms": 1_260},
            {"eval_id": "symbol", "event_id": "s2", "evaluator_id": "u2", "label_type": "symbol_correct", "label_value": False, "created_at_ms": 1_270},
            {"eval_id": "direction", "event_id": "s3", "evaluator_id": "u3", "label_type": "direction_correct", "label_value": False, "created_at_ms": 1_350},
        ]


def test_feedback_summary_uses_posted_story_denominator_and_latest_vote() -> None:
    report = anyio.run(
        lambda: build_newswire_feedback_summary(
            _FeedbackRepository(),
            cohort_start_ms=1_000,
            as_of_ms=2_000,
        )
    )

    overall = report["overall"]
    assert overall["posted_story_count"] == 3
    assert overall["quality_vote_count"] == 2
    assert overall["useful_rate_pct"] == 50.0
    assert overall["noise_rate_pct"] == 50.0
    assert overall["wrong_symbol_story_count"] == 1
    assert overall["wrong_direction_story_count"] == 1
    assert report["data_quality"]["raw_eval_count"] == 5
    assert report["data_quality"]["latest_deduplicated_vote_count"] == 4
    assert report["semantics"]["absence_of_flag_is_not_correctness"] is True
    assert {(row["source"], row["score_bucket"]) for row in report["groups"]} == {
        ("alpaca", "50-69"),
        ("alpaca", "80+"),
        ("cointelegraph", "50-69"),
    }
