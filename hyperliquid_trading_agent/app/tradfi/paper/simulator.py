"""Equity paper trading simulator — separate from crypto paper simulation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.paper.schemas import (
    EquityPaperFill,
    EquityPaperOrder,
    EquityPaperPortfolio,
    EquityPaperPosition,
    EquityPortfolioSnapshot,
    EquityRiskControlError,
    EquityTradeRequest,
)

log = get_logger(__name__)


class EquityPaperSimulator:
    """Local paper simulation for equities (stocks only; options deferred).

    Order lifecycle: pending → filled (at market) → creates position.
    Risk controls enforce single-name exposure and gross leverage limits.
    """

    def __init__(
        self,
        *,
        initial_equity_usd: float = 100_000.0,
        risk_pct_per_trade: float = 0.25,
        max_gross_leverage: float = 2.0,
        max_single_name_exposure_pct: float = 15.0,
        taker_fee_bps: float = 2.0,
        maker_fee_bps: float = 0.5,
        default_slippage_bps: float = 1.0,
        tradfi_client: TradFiClient | None = None,
        repository: Any | None = None,
    ):
        self.portfolio = EquityPaperPortfolio(
            initial_equity_usd=initial_equity_usd,
            cash_usd=initial_equity_usd,
        )
        self.risk_pct_per_trade = risk_pct_per_trade / 100.0
        self.max_gross_leverage = max_gross_leverage
        self.max_single_name_exposure_pct = max_single_name_exposure_pct / 100.0 if max_single_name_exposure_pct > 1 else max_single_name_exposure_pct
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.default_slippage_bps = default_slippage_bps
        self.tradfi = tradfi_client
        self.repository = repository

        self.positions: dict[str, EquityPaperPosition] = {}
        self.orders: dict[str, EquityPaperOrder] = {}
        self.fills: dict[str, EquityPaperFill] = {}
        self.snapshots: list[EquityPortfolioSnapshot] = []

    # --- Order lifecycle --------------------------------------------------------

    async def place_order(self, request: EquityTradeRequest) -> EquityPaperOrder:
        """Create and fill a paper equity order.

        Raises ``EquityRiskControlError`` if risk limits are exceeded.
        """
        symbol = request.symbol.upper()
        side = request.side.lower()
        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")

        # Determine price
        if request.entry is not None:
            price = request.entry
        elif self.tradfi is not None:
            snap = await self.tradfi.get_snapshots([symbol])
            if sym_snap := snap.get(symbol):
                price = sym_snap.daily_bar.close if sym_snap.daily_bar else 0.0
            else:
                raise ValueError(f"No price data for {symbol}")
        else:
            raise ValueError("No entry price and no TradFi client available")

        if price <= 0:
            raise ValueError(f"Invalid price for {symbol}: {price}")

        # Size the order
        equity = request.account_equity_usd or self.portfolio.equity_usd
        risk_amount = equity * request.risk_pct / 100.0
        stop_distance = abs(price - (request.stop or price * 0.95))

        if request.quantity is not None:
            quantity = request.quantity
        elif stop_distance > 0:
            quantity = risk_amount / stop_distance
        else:
            quantity = equity * self.risk_pct_per_trade / price

        if quantity <= 0:
            raise EquityRiskControlError("risk controls produced zero quantity")

        fill_price = _fill_price(side, price, self.default_slippage_bps)
        notional = quantity * fill_price

        # Risk checks
        total_exposure = sum(
            (p.quantity * (p.mark_px or p.avg_entry_px))
            for p in self.positions.values()
            if p.status == "open"
        )
        new_total = total_exposure + notional

        if new_total > equity * self.max_gross_leverage:
            raise EquityRiskControlError(
                f"Max gross leverage {self.max_gross_leverage:.1f}x exceeded: "
                f"new total ${new_total:,.0f} > ${equity * self.max_gross_leverage:,.0f}"
            )

        single_name = sum(
            (p.quantity * (p.mark_px or p.avg_entry_px))
            for p in self.positions.values()
            if p.status == "open" and p.symbol == symbol
        )
        if single_name + notional > equity * self.max_single_name_exposure_pct:
            raise EquityRiskControlError(
                f"Max single-name exposure {self.max_single_name_exposure_pct*100:.0f}% exceeded for {symbol}"
            )

        # Create order
        order = EquityPaperOrder(
            portfolio_id=self.portfolio.id,
            signal_id=request.signal_id,
            symbol=symbol,
            side=side,
            order_type="market",
            status="pending",
            quantity=quantity,
            requested_px=price,
            stop_px=request.stop,
            take_profit_px=request.take_profit,
            fee_bps=self.taker_fee_bps,
            slippage_bps=self.default_slippage_bps,
        )
        self.orders[order.id] = order

        # Fill immediately (paper market orders fill at the snapshot price plus slippage)
        fee_usd = notional * self.taker_fee_bps / 10000
        slippage_usd = abs(fill_price - price) * quantity

        order.status = "filled"
        order.filled_px = fill_price
        order.filled_at = datetime.now(timezone.utc)

        fill = EquityPaperFill(
            order_id=order.id,
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=fill_price,
            fee_usd=fee_usd,
            slippage_usd=slippage_usd,
        )
        self.fills[fill.id] = fill

        # Create position (or add to existing)
        existing = next((p for p in self.positions.values() if p.symbol == symbol and p.side == side and p.status == "open"), None)
        if existing:
            new_qty = existing.quantity + quantity
            existing.avg_entry_px = ((existing.avg_entry_px * existing.quantity) + (fill_price * quantity)) / new_qty
            existing.quantity = new_qty
            existing.mark_px = fill_price
            position = existing
        else:
            position = EquityPaperPosition(
                portfolio_id=self.portfolio.id,
                signal_id=request.signal_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                avg_entry_px=fill_price,
                mark_px=fill_price,
                stop_px=request.stop,
                take_profit_px=request.take_profit,
            )
            self.positions[position.id] = position

        # Margin-style paper accounting mirrors the crypto paper simulator:
        # opening a position changes cash only by fees. Notional exposure is
        # tracked separately in positions/snapshots; realized PnL is booked at close.
        self.portfolio.cash_usd -= fee_usd
        await self._persist_order_fill_position(order, fill, position)

        return order

    async def close_position(self, position_id: str) -> EquityPaperPosition | None:
        """Close an open paper position at current market price."""
        pos = self.positions.get(position_id)
        if pos is None or pos.status != "open":
            return None

        price = pos.mark_px or pos.avg_entry_px
        if self.tradfi is not None:
            snap = await self.tradfi.get_snapshots([pos.symbol])
            if sym_snap := snap.get(pos.symbol):
                price = sym_snap.daily_bar.close if sym_snap.daily_bar else price

        close_side = "short" if pos.side == "long" else "long"
        close_px = _fill_price(close_side, price, self.default_slippage_bps)
        notional = pos.quantity * close_px
        fee_usd = notional * self.taker_fee_bps / 10000

        pnl = (close_px - pos.avg_entry_px) * pos.quantity
        if pos.side == "short":
            pnl = (pos.avg_entry_px - close_px) * pos.quantity

        pos.mark_px = close_px
        pos.unrealized_pnl_usd = 0.0
        pos.realized_pnl_usd = pnl - fee_usd
        pos.status = "closed"
        pos.closed_at = datetime.now(timezone.utc)

        realized = pnl - fee_usd
        self.portfolio.cash_usd += realized
        self.portfolio.realized_pnl_usd += realized
        await self._persist_position(pos)
        await self._persist_portfolio()

        return pos

    async def update_marks(self) -> None:
        """Refresh mark prices for all open positions."""
        if self.tradfi is None:
            return
        open_symbols = list({p.symbol for p in self.positions.values() if p.status == "open"})
        if not open_symbols:
            return
        snaps = await self.tradfi.get_snapshots(open_symbols)
        for pos in self.positions.values():
            if pos.status != "open":
                continue
            sym_snap = snaps.get(pos.symbol.upper())
            if not sym_snap or not sym_snap.daily_bar:
                continue
            price = sym_snap.daily_bar.close
            pos.mark_px = price
            if pos.side == "long":
                pos.unrealized_pnl_usd = (price - pos.avg_entry_px) * pos.quantity
            else:
                pos.unrealized_pnl_usd = (pos.avg_entry_px - price) * pos.quantity

    def snapshot(self) -> EquityPortfolioSnapshot:
        """Create a portfolio snapshot at the current state."""
        gross = sum(
            (p.quantity * (p.mark_px or p.avg_entry_px))
            for p in self.positions.values()
            if p.status == "open"
        )
        net = sum(
            ((p.quantity * (p.mark_px or p.avg_entry_px)) if p.side == "long" else -(p.quantity * (p.mark_px or p.avg_entry_px)))
            for p in self.positions.values()
            if p.status == "open"
        )
        unreal = sum(p.unrealized_pnl_usd for p in self.positions.values() if p.status == "open")
        equity = self.portfolio.cash_usd + unreal
        total_pnl = equity - self.portfolio.initial_equity_usd

        snap = EquityPortfolioSnapshot(
            portfolio_id=self.portfolio.id,
            timestamp_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            cash_usd=self.portfolio.cash_usd,
            equity_usd=equity,
            gross_exposure_usd=gross,
            net_exposure_usd=net,
            realized_pnl_usd=self.portfolio.realized_pnl_usd,
            unrealized_pnl_usd=unreal,
            total_pnl_usd=total_pnl,
        )
        self.snapshots.append(snap)
        if len(self.snapshots) > 500:
            self.snapshots = self.snapshots[-500:]
        return snap

    async def _persist_portfolio(self) -> None:
        repository = self.repository
        if repository is None or not getattr(repository, "enabled", False):
            return
        try:
            await repository.upsert_equity_paper_portfolio(self.portfolio.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - persistence is best effort
            log.warning("equity_paper_portfolio_persist_failed", error=type(exc).__name__)

    async def _persist_position(self, position: EquityPaperPosition) -> None:
        repository = self.repository
        if repository is None or not getattr(repository, "enabled", False):
            return
        try:
            await repository.upsert_equity_paper_position(position.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover
            log.warning("equity_paper_position_persist_failed", symbol=position.symbol, error=type(exc).__name__)

    async def _persist_order_fill_position(
        self,
        order: EquityPaperOrder,
        fill: EquityPaperFill,
        position: EquityPaperPosition,
    ) -> None:
        repository = self.repository
        if repository is None or not getattr(repository, "enabled", False):
            return
        try:
            await repository.upsert_equity_paper_portfolio(self.portfolio.model_dump(mode="json"))
            await repository.create_equity_paper_order(order.model_dump(mode="json"))
            await repository.record_equity_paper_fill(fill.model_dump(mode="json"))
            await repository.upsert_equity_paper_position(position.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover
            log.warning("equity_paper_order_persist_failed", symbol=order.symbol, error=type(exc).__name__)

    def status(self) -> dict[str, Any]:
        return {
            "portfolio": self.portfolio.model_dump(mode="json"),
            "open_positions": len([p for p in self.positions.values() if p.status == "open"]),
            "total_positions": len(self.positions),
            "total_orders": len(self.orders),
            "snapshot_count": len(self.snapshots),
        }


def _fill_price(side: str, reference_px: float, slippage_bps: float) -> float:
    slip = reference_px * slippage_bps / 10_000
    return reference_px + slip if side == "long" else max(0.0, reference_px - slip)
