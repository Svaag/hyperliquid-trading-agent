from __future__ import annotations

import time
from uuid import uuid4

from hyperliquid_trading_agent.app.autonomy.performance import (
    aggregate_metrics,
    exposure_usd,
    net_exposure_usd,
    unrealized_pnl,
)
from hyperliquid_trading_agent.app.autonomy.schemas import (
    PaperFill,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PortfolioSnapshot,
    TradeSignal,
)
from hyperliquid_trading_agent.app.config import Settings


class RiskControlError(ValueError):
    pass


class PaperPortfolioService:
    """Paper portfolio lifecycle for autonomy V1.

    This service models order/fill/position state but never calls exchange APIs.
    """

    def __init__(self, settings: Settings, repository=None):
        self.settings = settings
        self.repository = repository
        self.portfolio: PaperPortfolio | None = None
        self.orders: dict[str, PaperOrder] = {}
        self.fills: dict[str, PaperFill] = {}
        self.positions: dict[str, PaperPosition] = {}
        self.snapshots: list[PortfolioSnapshot] = []

    async def initialize(self) -> PaperPortfolio:
        if self.portfolio is not None:
            return self.portfolio
        if self.repository is not None and getattr(self.repository, "enabled", False):
            data = await self.repository.create_or_get_paper_portfolio(
                name="default",
                initial_equity_usd=self.settings.autonomy_paper_initial_equity_usd,
                mode=self.settings.autonomy_mode,
            )
            self.portfolio = _portfolio_from_dict(data)
        else:
            ts = _now_ms()
            self.portfolio = PaperPortfolio(
                id=uuid4().hex,
                name="default",
                status="active",
                initial_equity_usd=self.settings.autonomy_paper_initial_equity_usd,
                cash_usd=self.settings.autonomy_paper_initial_equity_usd,
                realized_pnl_usd=0.0,
                metadata={"mode": self.settings.autonomy_mode},
                created_at_ms=ts,
                updated_at_ms=ts,
            )
        return self.portfolio

    async def approve_signal(self, signal: TradeSignal, approved_by: str, mid: float | None = None, timestamp_ms: int | None = None) -> tuple[PaperOrder, PaperFill, PaperPosition]:
        portfolio = await self.initialize()
        if portfolio.status != "active":
            raise RiskControlError("paper portfolio is not active")
        ts = timestamp_ms or _now_ms()
        reference_px = mid or signal.entry
        fill_px = _fill_price(signal.side, reference_px, self.settings.autonomy_paper_default_slippage_bps)
        quantity = self._sized_quantity(signal, fill_px)
        if quantity <= 0:
            raise RiskControlError("risk controls produced zero quantity")
        notional = quantity * fill_px
        fee = notional * self.settings.autonomy_paper_taker_fee_bps / 10_000
        slippage_usd = abs(fill_px - reference_px) * quantity
        order = PaperOrder(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=signal.side,
            status="filled",
            quantity=quantity,
            requested_px=reference_px,
            filled_px=fill_px,
            stop_px=signal.stop,
            take_profit_px=signal.take_profit,
            fee_bps=self.settings.autonomy_paper_taker_fee_bps,
            slippage_bps=self.settings.autonomy_paper_default_slippage_bps,
            created_at_ms=ts,
            filled_at_ms=ts,
            metadata={"approved_by": approved_by, "source": "human_signoff", "exchange_actions": []},
        )
        fill = PaperFill(
            id=uuid4().hex,
            order_id=order.id,
            portfolio_id=portfolio.id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
            price=fill_px,
            fee_usd=fee,
            slippage_usd=slippage_usd,
            created_at_ms=ts,
            metadata={"signal_id": signal.id, "exchange_actions": []},
        )
        position = PaperPosition(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            side=signal.side,
            status="open",
            quantity=quantity,
            avg_entry_px=fill_px,
            mark_px=fill_px,
            stop_px=signal.stop,
            take_profit_px=signal.take_profit,
            opened_at_ms=ts,
            metadata={"order_id": order.id, "signal_id": signal.id, "approved_by": approved_by, "exchange_actions": []},
        )
        portfolio.cash_usd -= fee
        portfolio.updated_at_ms = ts
        self.orders[order.id] = order
        self.fills[fill.id] = fill
        self.positions[position.id] = position
        await self._persist_order_fill_position(order, fill, position)
        await self.snapshot(ts)
        return order, fill, position

    async def mark_to_market(self, mids: dict[str, float], timestamp_ms: int | None = None) -> list[PaperPosition]:
        await self.initialize()
        ts = timestamp_ms or _now_ms()
        closed: list[PaperPosition] = []
        for position in list(self.positions.values()):
            if position.status != "open":
                continue
            mark = mids.get(position.symbol)
            if mark is None or mark <= 0:
                continue
            position.mark_px = mark
            position.unrealized_pnl_usd = unrealized_pnl(position, mark)
            if _stop_or_take_profit_hit(position, mark):
                closed.append(await self.close_position(position.id, mark, reason="stop_or_take_profit", timestamp_ms=ts))
            elif self.repository is not None and getattr(self.repository, "enabled", False):
                await self.repository.upsert_paper_position(position.model_dump(mode="json"))
        await self.snapshot(ts)
        return closed

    async def close_position(self, position_id: str, price: float, reason: str = "manual", timestamp_ms: int | None = None) -> PaperPosition:
        portfolio = await self.initialize()
        ts = timestamp_ms or _now_ms()
        position = self.positions[position_id]
        if position.status == "closed":
            return position
        close_px = _fill_price("short" if position.side == "long" else "long", price, self.settings.autonomy_paper_default_slippage_bps)
        gross_pnl = unrealized_pnl(position, close_px)
        notional = position.quantity * close_px
        fee = notional * self.settings.autonomy_paper_taker_fee_bps / 10_000
        realized = gross_pnl - fee
        position.status = "closed"
        position.mark_px = close_px
        position.unrealized_pnl_usd = 0.0
        position.realized_pnl_usd = realized
        position.closed_at_ms = ts
        position.metadata = {**position.metadata, "close_reason": reason, "close_fee_usd": fee, "close_px": close_px}
        portfolio.cash_usd += realized
        portfolio.realized_pnl_usd += realized
        portfolio.updated_at_ms = ts
        if self.repository is not None and getattr(self.repository, "enabled", False):
            await self.repository.close_paper_position(position.id, close_px, realized, reason, ts)
        await self.snapshot(ts)
        return position

    async def snapshot(self, timestamp_ms: int | None = None) -> PortfolioSnapshot:
        portfolio = await self.initialize()
        ts = timestamp_ms or _now_ms()
        positions = list(self.positions.values())
        open_positions = [item for item in positions if item.status == "open"]
        unrealized = sum(unrealized_pnl(item) for item in open_positions)
        equity = portfolio.cash_usd + unrealized
        gross = sum(exposure_usd(item) for item in open_positions)
        net = sum(net_exposure_usd(item) for item in open_positions)
        total_pnl = equity - portfolio.initial_equity_usd
        prior = self.snapshots[-2000:]
        metrics = aggregate_metrics(portfolio, positions, prior)
        drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
        snapshot = PortfolioSnapshot(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            timestamp_ms=ts,
            cash_usd=portfolio.cash_usd,
            equity_usd=equity,
            gross_exposure_usd=gross,
            net_exposure_usd=net,
            realized_pnl_usd=portfolio.realized_pnl_usd,
            unrealized_pnl_usd=unrealized,
            total_pnl_usd=total_pnl,
            drawdown_pct=drawdown,
            sharpe=metrics.get("sharpe"),
            metrics=metrics,
        )
        self.snapshots.append(snapshot)
        self.snapshots = self.snapshots[-5000:]
        if self.repository is not None and getattr(self.repository, "enabled", False):
            await self.repository.record_portfolio_snapshot(snapshot.model_dump(mode="json"))
        return snapshot

    def latest_snapshot(self) -> PortfolioSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def open_positions(self) -> list[PaperPosition]:
        return [item for item in self.positions.values() if item.status == "open"]

    def _sized_quantity(self, signal: TradeSignal, fill_px: float) -> float:
        portfolio = self.portfolio
        if portfolio is None:
            raise RiskControlError("portfolio not initialized")
        latest = self.latest_snapshot()
        equity = latest.equity_usd if latest is not None else portfolio.cash_usd
        risk_usd = equity * self.settings.autonomy_paper_risk_pct_per_trade / 100
        stop_distance = abs(fill_px - signal.stop)
        if stop_distance <= 0:
            raise RiskControlError("invalid stop distance")
        risk_quantity = risk_usd / stop_distance
        max_single_notional = equity * self.settings.autonomy_paper_max_single_name_exposure_pct / 100
        current_symbol_exposure = sum(exposure_usd(item) for item in self.open_positions() if item.symbol == signal.symbol)
        single_cap_quantity = max(0.0, max_single_notional - current_symbol_exposure) / fill_px
        gross_cap_notional = equity * self.settings.autonomy_paper_max_gross_leverage
        current_gross = sum(exposure_usd(item) for item in self.open_positions())
        gross_cap_quantity = max(0.0, gross_cap_notional - current_gross) / fill_px
        return max(0.0, min(risk_quantity, single_cap_quantity, gross_cap_quantity))

    async def _persist_order_fill_position(self, order: PaperOrder, fill: PaperFill, position: PaperPosition) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        await self.repository.create_paper_order(order.model_dump(mode="json"))
        await self.repository.record_paper_fill(fill.model_dump(mode="json"))
        await self.repository.upsert_paper_position(position.model_dump(mode="json"))


def _fill_price(side: str, mid: float, slippage_bps: float) -> float:
    if side == "long":
        return mid * (1 + slippage_bps / 10_000)
    return mid * (1 - slippage_bps / 10_000)


def _stop_or_take_profit_hit(position: PaperPosition, mark: float) -> bool:
    if position.side == "long":
        return mark <= position.stop_px or (position.take_profit_px is not None and mark >= position.take_profit_px)
    return mark >= position.stop_px or (position.take_profit_px is not None and mark <= position.take_profit_px)


def _portfolio_from_dict(data: dict) -> PaperPortfolio:
    ts = _now_ms()
    if "created_at_ms" in data:
        return PaperPortfolio(**data)
    return PaperPortfolio(
        id=str(data["id"]),
        name=str(data.get("name") or "default"),
        status=str(data.get("status") or "active"),  # type: ignore[arg-type]
        initial_equity_usd=float(data.get("initial_equity_usd") or data.get("initial_equity") or 0),
        cash_usd=float(data.get("cash_usd") or data.get("cash") or 0),
        realized_pnl_usd=float(data.get("realized_pnl_usd") or 0),
        metadata=dict(data.get("metadata") or {}),
        created_at_ms=int(data.get("created_at_ms") or ts),
        updated_at_ms=int(data.get("updated_at_ms") or ts),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
