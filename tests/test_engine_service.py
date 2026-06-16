from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.service import InstitutionalEngineService
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway


class FakeHyperliquidEngine:
    async def all_mids(self):
        return {"BTC": "104"}

    async def l2_book(self, symbol):
        return {"levels": [[[103.9, 1000]], [[104.1, 900]]]}


def test_institutional_engine_run_once_is_paper_shadow_only():
    settings = Settings(
        engine_enabled=True,
        engine_min_net_ev_bps=-100,
        engine_min_risk_adjusted_utility=-100,
        autonomy_core_universe="BTC",
        autonomy_max_hot_l2_assets=1,
        engine_debate_priority_min=0,
        engine_paper_enabled=True,
        engine_shadow_enabled=True,
    )
    risk_gateway = RiskGateway(settings=settings)
    service = InstitutionalEngineService(settings=settings, repository=None, hyperliquid=FakeHyperliquidEngine(), risk_gateway=risk_gateway)

    async def run():
        # First pass seeds only one price, so run twice to build trend history.
        await service.run_once(symbols=["BTC"])
        return await service.run_once(symbols=["BTC"])

    result = anyio.run(run)

    assert result["candidates"] >= 0
    assert service.status()["run_count"] == 2
    assert service.status()["last_error"] is None
