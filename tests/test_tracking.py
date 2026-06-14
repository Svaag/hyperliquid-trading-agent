from __future__ import annotations

from hyperliquid_trading_agent.app.agent.high_stakes.formatting import format_trade_proposal
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import JudgeDecision, TradeProposal
from hyperliquid_trading_agent.app.tracking.alerts import format_level_hit_alert
from hyperliquid_trading_agent.app.tracking.commands import parse_tracking_command
from hyperliquid_trading_agent.app.tracking.levels import derive_position_tracking_plan, summarize_tracking_plan
from hyperliquid_trading_agent.app.tracking.schemas import LevelHitEvent
from hyperliquid_trading_agent.app.tracking.service import evaluate_level


def test_tracking_command_parser_understands_thread_controls():
    assert parse_tracking_command("tracking status").action == "status"  # type: ignore[union-attr]
    stop = parse_tracking_command("stop tracking VVV")
    assert stop is not None
    assert stop.action == "stop"
    assert stop.coin == "VVV"
    ttl = parse_tracking_command("track until 7d")
    assert ttl is not None
    assert ttl.action == "set_ttl"
    assert ttl.ttl_hours == 168


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


def test_crossing_engine_hits_and_rearms_with_hysteresis():
    features = {"market": {"VVV": {"mid": 16.25}}, "candles": {"VVV": {"recent_support": 15.63}}}
    plan = derive_position_tracking_plan(coin="VVV", side="long", entry=16.4, stop=15.5, features=features, now_ms=1)
    assert plan is not None
    technical = next(level for level in plan.levels if level.kind == "technical_exit")
    reclaim = next(level for level in plan.levels if level.kind == "entry_reclaim")

    assert evaluate_level(technical, 15.7, 15.62).hit is True
    disarmed = reclaim.model_copy(update={"armed": False, "hit_count": 1})
    assert evaluate_level(disarmed, 16.5, 16.39).rearmed is False
    assert evaluate_level(disarmed, 16.5, 16.38).rearmed is True


def test_crossing_engine_detects_initial_terminal_breach_only():
    features = {"market": {"VVV": {"mid": 15.4}}, "candles": {"VVV": {}}}
    plan = derive_position_tracking_plan(coin="VVV", side="long", entry=16.4, stop=15.5, features=features, now_ms=1)
    assert plan is not None
    hard_stop = next(level for level in plan.levels if level.kind == "hard_stop")

    assert evaluate_level(hard_stop, None, 15.4, first_update=True).already_breached is True


def test_alert_format_includes_level_meaning_and_no_execution():
    features = {"market": {"VVV": {"mid": 16.25}}, "candles": {"VVV": {"recent_support": 15.63}}}
    plan = derive_position_tracking_plan(coin="VVV", side="long", entry=16.4, stop=15.5, features=features, now_ms=1)
    assert plan is not None
    level = next(item for item in plan.levels if item.kind == "technical_exit")
    event = LevelHitEvent(
        tracker_id=plan.id,
        coin="VVV",
        side="long",
        level_id=level.id,
        level_kind=level.kind,
        level_price=level.price,
        current_price=15.62,
        direction=level.direction,
        terminal=level.terminal,
        recommended_action="exit",
    )

    content = format_level_hit_alert(plan, level, event)

    assert "technical reduce/exit trigger" in content
    assert "No trade was placed" in content


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
        checklist=[
            "Re-check Hyperliquid mark/oracle, funding, spread, and depth immediately before acting.",
            "Execution readiness: asset_id=178 spread_bps=4.2 top_depth=$2000 est_slippage_bps=9.5",
        ],
        tracking_plan=plan.model_dump(mode="json"),
    )
    decision = JudgeDecision(status="manual_review_required", confidence=0.35, summary="fallback")

    content = format_trade_proposal(proposal, decision)

    assert "Levels to watch:" in content
    assert "Technical reduce/exit trigger: cross down through 15.63" in content
    assert "Resistance confirmation: cross up through 16.904" in content
    assert "Re-check Hyperliquid" not in content
    assert "Liquidity: spread ~4.20 bps" in content
    assert summarize_tracking_plan(plan)
