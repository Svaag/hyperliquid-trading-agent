from __future__ import annotations

import time
from typing import Any
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
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeDraftRequest


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
        self._hydrated = False

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
            await self._hydrate_persisted_state()
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

    async def draft_trade(self, request: PaperTradeDraftRequest, mid: float | None = None, timestamp_ms: int | None = None) -> PaperOrder:
        portfolio = await self.initialize()
        if portfolio.status != "active":
            raise RiskControlError("paper portfolio is not active")
        ts = timestamp_ms or _now_ms()
        reference_px = _positive_float(mid) or _positive_float(request.entry)
        if reference_px is None:
            raise RiskControlError("market paper draft requires a positive reference price")
        _validate_stop(request.side, reference_px, request.stop)
        if request.quantity is not None:
            quantity = request.quantity
            self._validate_explicit_quantity(
                symbol=request.symbol,
                side=request.side,
                quantity=quantity,
                entry_px=reference_px,
                close_opposite=request.close_opposite,
            )
        else:
            quantity = self._sized_quantity_from_values(
                symbol=request.symbol,
                side=request.side,
                entry_px=reference_px,
                stop_px=request.stop,
                risk_pct=request.risk_pct,
                close_opposite=request.close_opposite,
            )
        if quantity <= 0:
            raise RiskControlError("risk controls produced zero quantity")
        order = PaperOrder(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            signal_id=None,
            symbol=request.symbol.upper(),
            side=request.side,
            status="new",
            quantity=quantity,
            requested_px=reference_px,
            filled_px=None,
            stop_px=request.stop,
            take_profit_px=request.take_profit,
            fee_bps=self.settings.autonomy_paper_taker_fee_bps,
            slippage_bps=self.settings.autonomy_paper_default_slippage_bps,
            created_at_ms=ts,
            filled_at_ms=None,
            metadata={
                **request.metadata,
                "actor": request.actor,
                "drafted_by": request.actor,
                "source": request.source,
                "proposal_id": request.proposal_id,
                "thesis": request.thesis,
                "market": request.market,
                "risk_pct": request.risk_pct,
                "sizing_mode": "explicit_quantity" if request.quantity is not None else "risk",
                "close_opposite_requested": request.close_opposite,
                "exchange_actions": [],
            },
        )
        self.orders[order.id] = order
        await self._persist_order(order)
        return order

    async def confirm_draft(self, order_id: str, *, actor: str = "api", mid: float | None = None, close_opposite: bool = False, timestamp_ms: int | None = None) -> tuple[PaperOrder, PaperFill, PaperPosition]:
        portfolio = await self.initialize()
        if portfolio.status != "active":
            raise RiskControlError("paper portfolio is not active")
        ts = timestamp_ms or _now_ms()
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError("paper order not found")
        if order.status == "filled":
            fill = next((item for item in self.fills.values() if item.order_id == order.id), None)
            position = next((item for item in self.positions.values() if item.metadata.get("order_id") == order.id), None)
            if fill is not None and position is not None:
                return order, fill, position
            raise RiskControlError("paper order is filled but fill/position state is incomplete")
        if order.status != "new":
            raise RiskControlError(f"paper order status {order.status} cannot be confirmed")
        if order.stop_px is None:
            raise RiskControlError("paper order missing stop")
        reference_px = _positive_float(mid) or _positive_float(order.requested_px)
        if reference_px is None:
            raise RiskControlError("confirmed paper order requires a positive reference price")
        _validate_stop(order.side, reference_px, order.stop_px)
        should_close_opposite = close_opposite or bool(order.metadata.get("close_opposite_requested"))
        opposing = self.find_opposing_position(order.symbol, order.side)
        if opposing is not None and not should_close_opposite:
            raise RiskControlError("opposing paper position is open; confirm with close_opposite=true to flip")
        if opposing is not None:
            await self.close_position(opposing.id, reference_px, reason=f"flip_for_order:{order.id}", timestamp_ms=ts)
        if order.metadata.get("sizing_mode") != "explicit_quantity":
            risk_pct = _positive_float(order.metadata.get("risk_pct"))
            order.quantity = self._sized_quantity_from_values(
                symbol=order.symbol,
                side=order.side,
                entry_px=reference_px,
                stop_px=order.stop_px,
                risk_pct=risk_pct,
                close_opposite=should_close_opposite,
            )
        else:
            self._validate_explicit_quantity(
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                entry_px=reference_px,
                close_opposite=should_close_opposite,
            )
        if order.quantity <= 0:
            raise RiskControlError("risk controls produced zero quantity")
        fill_px = _fill_price(order.side, reference_px, self.settings.autonomy_paper_default_slippage_bps)
        notional = order.quantity * fill_px
        fee = notional * self.settings.autonomy_paper_taker_fee_bps / 10_000
        slippage_usd = abs(fill_px - reference_px) * order.quantity
        order.status = "filled"
        order.requested_px = reference_px
        order.filled_px = fill_px
        order.filled_at_ms = ts
        order.metadata = {**order.metadata, "confirmed_by": actor, "confirmed_at_ms": ts}
        fill = PaperFill(
            id=uuid4().hex,
            order_id=order.id,
            portfolio_id=portfolio.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_px,
            fee_usd=fee,
            slippage_usd=slippage_usd,
            created_at_ms=ts,
            metadata={"order_id": order.id, "source": order.metadata.get("source", "manual"), "confirmed_by": actor, "exchange_actions": []},
        )
        position = PaperPosition(
            id=uuid4().hex,
            portfolio_id=portfolio.id,
            signal_id=None,
            symbol=order.symbol,
            side=order.side,
            status="open",
            quantity=order.quantity,
            avg_entry_px=fill_px,
            mark_px=fill_px,
            stop_px=order.stop_px,
            take_profit_px=order.take_profit_px,
            opened_at_ms=ts,
            metadata={
                "order_id": order.id,
                "source": order.metadata.get("source", "manual"),
                "proposal_id": order.metadata.get("proposal_id"),
                "thesis": order.metadata.get("thesis", ""),
                "confirmed_by": actor,
                "exchange_actions": [],
            },
        )
        portfolio.cash_usd -= fee
        portfolio.updated_at_ms = ts
        self.orders[order.id] = order
        self.fills[fill.id] = fill
        self.positions[position.id] = position
        await self._persist_order_fill_position(order, fill, position)
        await self.snapshot(ts)
        return order, fill, position

    async def cancel_draft(self, order_id: str, *, actor: str = "api", reason: str = "cancelled", timestamp_ms: int | None = None) -> PaperOrder:
        await self.initialize()
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError("paper order not found")
        if order.status == "filled":
            raise RiskControlError("filled paper orders cannot be cancelled")
        if order.status == "cancelled":
            return order
        ts = timestamp_ms or _now_ms()
        order.status = "cancelled"
        order.cancelled_at_ms = ts
        order.metadata = {**order.metadata, "cancelled_by": actor, "cancel_reason": reason, "exchange_actions": []}
        self.orders[order.id] = order
        await self._persist_order(order)
        return order

    async def close_position_by_symbol(self, symbol: str, price: float, reason: str = "manual", timestamp_ms: int | None = None) -> PaperPosition:
        await self.initialize()
        matches = [item for item in self.open_positions() if item.symbol == symbol.upper()]
        if not matches:
            raise KeyError("open paper position not found")
        if len(matches) > 1:
            raise RiskControlError("multiple open paper positions match symbol; use position id")
        return await self.close_position(matches[0].id, price, reason=reason, timestamp_ms=timestamp_ms)

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

    def find_opposing_position(self, symbol: str, side: str) -> PaperPosition | None:
        opposite = "short" if side == "long" else "long"
        for item in self.open_positions():
            if item.symbol == symbol and item.side == opposite:
                return item
        return None

    def sizing_diagnostics(self, signal: TradeSignal, fill_px: float) -> dict[str, Any]:
        portfolio = self.portfolio
        latest = self.latest_snapshot()
        equity = latest.equity_usd if latest is not None else (portfolio.cash_usd if portfolio else 0.0)
        risk_pct = self.settings.autonomy_paper_risk_pct_per_trade
        max_single_pct = self.settings.autonomy_paper_max_single_name_exposure_pct
        max_gross = self.settings.autonomy_paper_max_gross_leverage
        risk_usd = equity * risk_pct / 100
        stop_distance = abs(fill_px - signal.stop)
        current_symbol_exposure = sum(
            exposure_usd(item) for item in self.open_positions() if item.symbol == signal.symbol
        )
        current_gross = sum(exposure_usd(item) for item in self.open_positions())
        opposing = self.find_opposing_position(signal.symbol, signal.side)
        return {
            "equity_usd": round(equity, 2),
            "risk_pct": risk_pct,
            "risk_usd": round(risk_usd, 2),
            "stop_distance": round(stop_distance, 8),
            "max_single_name_exposure_pct": max_single_pct,
            "max_single_name_exposure_usd": round(equity * max_single_pct / 100, 2),
            "current_symbol_exposure_usd": round(current_symbol_exposure, 2),
            "max_gross_leverage": max_gross,
            "max_gross_exposure_usd": round(equity * max_gross, 2),
            "current_gross_exposure_usd": round(current_gross, 2),
            "opposing_position_id": opposing.id if opposing else None,
            "opposing_position_side": opposing.side if opposing else None,
            "opposing_position_quantity": opposing.quantity if opposing else None,
        }

    def _sized_quantity(self, signal: TradeSignal, fill_px: float) -> float:
        return self._sized_quantity_from_values(
            symbol=signal.symbol,
            side=signal.side,
            entry_px=fill_px,
            stop_px=signal.stop,
            risk_pct=self.settings.autonomy_paper_risk_pct_per_trade,
        )

    def _sized_quantity_from_values(
        self,
        *,
        symbol: str,
        side: str,
        entry_px: float,
        stop_px: float,
        risk_pct: float | None = None,
        close_opposite: bool = False,
    ) -> float:
        portfolio = self.portfolio
        if portfolio is None:
            raise RiskControlError("portfolio not initialized")
        latest = self.latest_snapshot()
        equity = latest.equity_usd if latest is not None else portfolio.cash_usd
        risk_usd = equity * (risk_pct if risk_pct is not None else self.settings.autonomy_paper_risk_pct_per_trade) / 100
        stop_distance = abs(entry_px - stop_px)
        if stop_distance <= 0:
            raise RiskControlError("invalid stop distance")
        risk_quantity = risk_usd / stop_distance
        max_single_notional = equity * self.settings.autonomy_paper_max_single_name_exposure_pct / 100
        current_symbol_exposure = sum(
            exposure_usd(item)
            for item in self.open_positions()
            if item.symbol == symbol.upper() and not (close_opposite and item.side != side)
        )
        single_cap_quantity = max(0.0, max_single_notional - current_symbol_exposure) / entry_px
        gross_cap_notional = equity * self.settings.autonomy_paper_max_gross_leverage
        current_gross = sum(exposure_usd(item) for item in self.open_positions() if not (close_opposite and item.symbol == symbol.upper() and item.side != side))
        gross_cap_quantity = max(0.0, gross_cap_notional - current_gross) / entry_px
        return max(0.0, min(risk_quantity, single_cap_quantity, gross_cap_quantity))

    def _validate_explicit_quantity(self, *, symbol: str, side: str, quantity: float, entry_px: float, close_opposite: bool = False) -> None:
        portfolio = self.portfolio
        if portfolio is None:
            raise RiskControlError("portfolio not initialized")
        latest = self.latest_snapshot()
        equity = latest.equity_usd if latest is not None else portfolio.cash_usd
        max_single_notional = equity * self.settings.autonomy_paper_max_single_name_exposure_pct / 100
        current_symbol_exposure = sum(
            exposure_usd(item)
            for item in self.open_positions()
            if item.symbol == symbol.upper() and not (close_opposite and item.side != side)
        )
        gross_cap_notional = equity * self.settings.autonomy_paper_max_gross_leverage
        current_gross = sum(exposure_usd(item) for item in self.open_positions() if not (close_opposite and item.symbol == symbol.upper() and item.side != side))
        added_notional = quantity * entry_px
        if current_symbol_exposure + added_notional > max_single_notional + 1e-9:
            raise RiskControlError("explicit quantity exceeds single-name exposure limit")
        if current_gross + added_notional > gross_cap_notional + 1e-9:
            raise RiskControlError("explicit quantity exceeds gross exposure limit")

    async def _persist_order(self, order: PaperOrder) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        await self.repository.create_paper_order(order.model_dump(mode="json"))

    async def _persist_order_fill_position(self, order: PaperOrder, fill: PaperFill, position: PaperPosition) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        await self.repository.create_paper_order(order.model_dump(mode="json"))
        await self.repository.record_paper_fill(fill.model_dump(mode="json"))
        await self.repository.upsert_paper_position(position.model_dump(mode="json"))

    async def _hydrate_persisted_state(self) -> None:
        if self._hydrated or self.portfolio is None or self.repository is None or not getattr(self.repository, "enabled", False):
            return
        portfolio_id = self.portfolio.id
        try:
            for item in await self.repository.list_paper_orders(limit=5000):
                if str(item.get("portfolio_id")) == portfolio_id:
                    self.orders[str(item["id"])] = _order_from_dict(item)
            for item in await self.repository.list_paper_fills(limit=5000):
                if str(item.get("portfolio_id")) == portfolio_id:
                    self.fills[str(item["id"])] = _fill_from_dict(item)
            for item in await self.repository.list_paper_positions(limit=5000):
                if str(item.get("portfolio_id")) == portfolio_id:
                    self.positions[str(item["id"])] = _position_from_dict(item)
            for item in reversed(await self.repository.list_portfolio_snapshots(portfolio_id=portfolio_id, limit=5000)):
                self.snapshots.append(_snapshot_from_dict(item))
            self.snapshots = self.snapshots[-5000:]
            self._hydrated = True
        except Exception:
            self._hydrated = True
            raise


def _fill_price(side: str, mid: float, slippage_bps: float) -> float:
    if side == "long":
        return mid * (1 + slippage_bps / 10_000)
    return mid * (1 - slippage_bps / 10_000)


def _stop_or_take_profit_hit(position: PaperPosition, mark: float) -> bool:
    if position.side == "long":
        return mark <= position.stop_px or (position.take_profit_px is not None and mark >= position.take_profit_px)
    return mark >= position.stop_px or (position.take_profit_px is not None and mark <= position.take_profit_px)


def _validate_stop(side: str, entry: float, stop: float) -> None:
    if side == "long" and stop >= entry:
        raise RiskControlError("long stop must be below entry")
    if side == "short" and stop <= entry:
        raise RiskControlError("short stop must be above entry")


def _positive_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


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


def _order_from_dict(data: dict) -> PaperOrder:
    return PaperOrder(
        id=str(data["id"]),
        portfolio_id=str(data["portfolio_id"]),
        signal_id=data.get("signal_id"),
        symbol=str(data.get("symbol") or "").upper(),
        side=str(data.get("side") or "long"),  # type: ignore[arg-type]
        order_type=str(data.get("order_type") or "market"),  # type: ignore[arg-type]
        status=str(data.get("status") or "new"),  # type: ignore[arg-type]
        quantity=float(data.get("quantity") or 0),
        requested_px=_positive_float(data.get("requested_px")),
        filled_px=_positive_float(data.get("filled_px")),
        stop_px=_positive_float(data.get("stop_px")),
        take_profit_px=_positive_float(data.get("take_profit_px")),
        fee_bps=float(data.get("fee_bps") or 0),
        slippage_bps=float(data.get("slippage_bps") or 0),
        created_at_ms=int(data.get("created_at_ms") or _now_ms()),
        filled_at_ms=int(data["filled_at_ms"]) if data.get("filled_at_ms") is not None else None,
        cancelled_at_ms=int(data["cancelled_at_ms"]) if data.get("cancelled_at_ms") is not None else None,
        metadata=dict(data.get("metadata") or {}),
    )


def _fill_from_dict(data: dict) -> PaperFill:
    return PaperFill(
        id=str(data["id"]),
        order_id=str(data["order_id"]),
        portfolio_id=str(data["portfolio_id"]),
        symbol=str(data.get("symbol") or "").upper(),
        side=str(data.get("side") or "long"),  # type: ignore[arg-type]
        quantity=float(data.get("quantity") or 0),
        price=float(data.get("price") or 0),
        fee_usd=float(data.get("fee_usd") or 0),
        slippage_usd=float(data.get("slippage_usd") or 0),
        created_at_ms=int(data.get("created_at_ms") or _now_ms()),
        metadata=dict(data.get("metadata") or {}),
    )


def _position_from_dict(data: dict) -> PaperPosition:
    return PaperPosition(
        id=str(data["id"]),
        portfolio_id=str(data["portfolio_id"]),
        signal_id=data.get("signal_id"),
        symbol=str(data.get("symbol") or "").upper(),
        side=str(data.get("side") or "long"),  # type: ignore[arg-type]
        status=str(data.get("status") or "open"),  # type: ignore[arg-type]
        quantity=float(data.get("quantity") or 0),
        avg_entry_px=float(data.get("avg_entry_px") or 0),
        mark_px=_positive_float(data.get("mark_px")),
        stop_px=float(data.get("stop_px") or 0),
        take_profit_px=_positive_float(data.get("take_profit_px")),
        realized_pnl_usd=float(data.get("realized_pnl_usd") or 0),
        unrealized_pnl_usd=float(data.get("unrealized_pnl_usd") or 0),
        opened_at_ms=int(data.get("opened_at_ms") or _now_ms()),
        closed_at_ms=int(data["closed_at_ms"]) if data.get("closed_at_ms") is not None else None,
        metadata=dict(data.get("metadata") or {}),
    )


def _snapshot_from_dict(data: dict) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        id=str(data["id"]),
        portfolio_id=str(data["portfolio_id"]),
        timestamp_ms=int(data.get("timestamp_ms") or _now_ms()),
        cash_usd=float(data.get("cash_usd") or 0),
        equity_usd=float(data.get("equity_usd") or 0),
        gross_exposure_usd=float(data.get("gross_exposure_usd") or 0),
        net_exposure_usd=float(data.get("net_exposure_usd") or 0),
        realized_pnl_usd=float(data.get("realized_pnl_usd") or 0),
        unrealized_pnl_usd=float(data.get("unrealized_pnl_usd") or 0),
        total_pnl_usd=float(data.get("total_pnl_usd") or 0),
        drawdown_pct=float(data.get("drawdown_pct") or 0),
        sharpe=data.get("sharpe"),
        metrics=dict(data.get("metrics") or {}),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
