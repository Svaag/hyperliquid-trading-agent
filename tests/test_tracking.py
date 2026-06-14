from __future__ import annotations

from hyperliquid_trading_agent.app.agent.high_stakes.formatting import format_trade_proposal
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import JudgeDecision, TradeProposal
from hyperliquid_trading_agent.app.tracking.levels import derive_position_tracking_plan, summarize_tracking_plan


def test_derive_long_position_tracking_plan_from_canonical_features():
    features = {
        "market": {"VVV": {"mid": 16.25}},
        "candles": {"VVV": {"recent_support": 15.63, "recent_resistance": 16.904}},
    }

    plan = derive_position_tracking_plan(coin="VVV", side="long", entry=16.4, stop=15.5, features=features, now_ms=1)

    assert plan is not None
    by_kind = {level.kind: level for level in plan.levels}
    assert by_kind["hard_stop"].direction == "cross_down"
    assert by_kind["hard_stop"].terminal is True
    assert by_kind["technical_exit"].price == 15.63
    assert by_kind["technical_exit"].direction == "cross_down"
    assert by_kind["entry_reclaim"].price == 16.4
    assert by_kind["entry_reclaim"].direction == "cross_up"
    assert by_kind["resistance_confirm"].price == 16.904
    assert plan.expires_at_ms == 1 + 168 * 60 * 60 * 1000


def test_derive_short_position_tracking_plan_mirrors_directions():
    features = {
        "market": {"BTC": {"mid": 99.0}},
        "candles": {"BTC": {"recent_support": 95.0, "recent_resistance": 101.0}},
    }

    plan = derive_position_tracking_plan(coin="BTC", side="short", entry=100, stop=102, take_profit=90, features=features, now_ms=1)

    assert plan is not None
    by_kind = {level.kind: level for level in plan.levels}
    assert by_kind["hard_stop"].direction == "cross_up"
    assert by_kind["technical_exit"].price == 101.0
    assert by_kind["entry_trim"].direction == "cross_up"
    assert by_kind["support_confirm"].direction == "cross_down"
    assert by_kind["take_profit"].direction == "cross_down"


def test_tracking_level_dedup_keeps_hard_stop_over_nearby_derived_level():
    features = {
        "market": {"ABC": {"mid": 101.0}},
        "candles": {"ABC": {"recent_support": 100.001, "recent_resistance": 104.0}},
    }

    plan = derive_position_tracking_plan(coin="ABC", side="long", entry=101.5, stop=100.0, features=features, now_ms=1)

    assert plan is not None
    near_stop = [level for level in plan.levels if abs(level.price - 100.0) < 0.01]
    assert [level.kind for level in near_stop] == ["hard_stop"]


def test_formatter_uses_tracking_plan_levels():
    features = {
        "market": {"VVV": {"mid": 16.25}},
        "candles": {"VVV": {"recent_support": 15.63, "recent_resistance": 16.904}},
    }
    plan = derive_position_tracking_plan(coin="VVV", side="long", entry=16.4, stop=15.5, features=features, now_ms=1)
    assert plan is not None
    proposal = TradeProposal(
        status="manual_review_required",
        coin="VVV",
        side="long",
        entry=16.4,
        stop=15.5,
        rationale=["Position: VVV long is below entry."],
        risks=["Model fallback for quant: TimeoutError"],
        tracking_plan=plan.model_dump(mode="json"),
    )
    decision = JudgeDecision(status="manual_review_required", confidence=0.35, summary="fallback")

    content = format_trade_proposal(proposal, decision)

    assert "Levels to watch:" in content
    assert "Technical reduce/exit trigger: cross down through 15.63" in content
    assert "Resistance confirmation: cross up through 16.904" in content
    assert summarize_tracking_plan(plan)
