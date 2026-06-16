from __future__ import annotations

import hashlib
from typing import Protocol

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import ExecutionReport, OrderIntent


class ExecutionAdapter(Protocol):
    async def submit(self, intent: OrderIntent) -> ExecutionReport: ...


class PaperAdapter:
    def __init__(self, *, taker_fee_bps: float = 4.5, default_slippage_bps: float = 2.0):
        self.taker_fee_bps = taker_fee_bps
        self.default_slippage_bps = default_slippage_bps

    async def submit(self, intent: OrderIntent) -> ExecutionReport:
        slippage = min(intent.max_slippage_bps, self.default_slippage_bps)
        fill_px = intent.price_limit or (intent.target_notional_usd / intent.target_size)
        if intent.side == "buy":
            fill_px *= 1 + slippage / 10_000
        else:
            fill_px *= 1 - slippage / 10_000
        fees = intent.target_notional_usd * self.taker_fee_bps / 10_000
        digest = hashlib.sha1(f"paper:{intent.intent_id}".encode()).hexdigest()[:24]
        return ExecutionReport(
            report_id="er_" + digest,
            intent_id=intent.intent_id,
            execution_mode="paper",
            status="filled",
            requested_size=intent.target_size,
            filled_size=intent.target_size,
            avg_fill_px=fill_px,
            fees_usd=fees,
            slippage_bps=slippage,
            market_impact_bps=0.0,
            adapter="paper",
            assumptions={"fill_model": "instant_marketable_limit", "live_exchange_actions": False},
            created_at_ms=now_ms(),
        )


class ShadowAdapter:
    async def submit(self, intent: OrderIntent) -> ExecutionReport:
        digest = hashlib.sha1(f"shadow:{intent.intent_id}".encode()).hexdigest()[:24]
        return ExecutionReport(
            report_id="er_" + digest,
            intent_id=intent.intent_id,
            execution_mode="shadow",
            status="accepted",
            requested_size=intent.target_size,
            filled_size=0.0,
            avg_fill_px=None,
            fees_usd=0.0,
            slippage_bps=0.0,
            market_impact_bps=None,
            adapter="shadow",
            assumptions={"shadow_only": True, "would_submit": intent.model_dump(mode="json"), "live_exchange_actions": False},
            created_at_ms=now_ms(),
        )


class ExecutionGateway:
    def __init__(self, *, repository=None, paper_adapter: PaperAdapter | None = None, shadow_adapter: ShadowAdapter | None = None):
        self.repository = repository
        self.paper_adapter = paper_adapter or PaperAdapter()
        self.shadow_adapter = shadow_adapter or ShadowAdapter()

    async def submit(self, intent: OrderIntent) -> ExecutionReport:
        adapter: ExecutionAdapter = self.paper_adapter if intent.execution_mode == "paper" else self.shadow_adapter
        report = await adapter.submit(intent)
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record_intent = getattr(self.repository, "record_order_intent", None)
            record_report = getattr(self.repository, "record_execution_report", None)
            if callable(record_intent):
                await record_intent(intent.model_dump(mode="json"))
            if callable(record_report):
                await record_report(report.model_dump(mode="json"))
        return report
