from __future__ import annotations

import time

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.schemas import OrderIntent
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway


def _intent(**updates):
    now_ms = int(time.time() * 1000)
    data = {
        "intent_id": "intent_risk_test",
        "parent_candidate_id": "cand_1",
        "portfolio_decision_id": "alloc_1",
        "asset": "BTC",
        "venue": "hyperliquid",
        "side": "buy",
        "order_type": "marketable_limit",
        "time_in_force": "ioc",
        "target_size": 1,
        "target_notional_usd": 10_000,
        "max_slippage_bps": 5,
        "price_limit": 100,
        "reduce_only": False,
        "post_only": False,
        "deadline_ts_ms": now_ms + 60_000,
        "strategy_id": "directional_momentum_v2",
        "model_version_id": "deterministic_fallback_v1",
        "config_version_id": "cfg_1",
        "risk_budget_id": "risk_1",
        "execution_mode": "paper",
        "created_at_ms": now_ms,
    }
    data.update(updates)
    return OrderIntent(**data)


def test_engine_order_intent_risk_gateway_allows_clean_paper_intent():
    gateway = RiskGateway(settings=Settings(autonomy_paper_max_single_name_exposure_pct=20))
    intent = _intent()
    now_ms = int(time.time() * 1000)

    async def run():
        return await gateway.check_order_intent(
            intent,
            market_snapshot={"last_price_at_ms": now_ms, "last_orderbook_at_ms": now_ms, "spread_bps": 3},
            portfolio_snapshot={"equity_usd": 100_000},
            strategy_snapshot={"net_ev_bps": 15, "regime_permission": True},
            operator_context={"kill_switch_active": False, "config_approved": True, "model_approved": True},
        )

    decision = anyio.run(run)

    assert decision.allowed
    assert decision.metadata["exchange_actions"] == []


def test_engine_order_intent_risk_gateway_rejects_stale_wide_and_kill_switch():
    gateway = RiskGateway(settings=Settings())
    intent = _intent(target_notional_usd=50_000)
    now_ms = int(time.time() * 1000)

    async def run():
        return await gateway.check_order_intent(
            intent,
            market_snapshot={"last_price_at_ms": now_ms - 300_000, "last_orderbook_at_ms": now_ms - 120_000, "spread_bps": 50},
            portfolio_snapshot={"equity_usd": 100_000},
            strategy_snapshot={"net_ev_bps": 1, "regime_permission": False},
            operator_context={"kill_switch_active": True, "config_approved": False, "model_approved": False},
        )

    decision = anyio.run(run)

    codes = {item["code"] for item in decision.violations}
    assert not decision.allowed
    assert "stale_price" in codes
    assert "stale_orderbook" in codes
    assert "spread_too_wide" in codes
    assert "single_name_exposure_limit" in codes
    assert "edge_below_minimum" in codes
    assert "kill_switch_active" in codes
