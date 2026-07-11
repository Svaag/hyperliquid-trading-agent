from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass as AlpacaAssetClass
from alpaca.trading.enums import AssetStatus, OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from hyperliquid_trading_agent.app.engine.schemas import OrderIntent
from hyperliquid_trading_agent.app.markets.schemas import InstrumentRef, VenueMarketSnapshot
from hyperliquid_trading_agent.app.tradfi.paper.schemas import EquityTradeRequest

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {}


class AlpacaPaperExecutionAdapter:
    """Hosted Alpaca Paper adapter with broker-authoritative reconciliation."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        repository: Any | None = None,
        base_url: str = ALPACA_PAPER_BASE_URL,
        client: Any | None = None,
        data_client: Any | None = None,
        data_feed: str = "iex",
    ):
        if base_url.rstrip("/") != ALPACA_PAPER_BASE_URL:
            raise ValueError("Alpaca execution adapter accepts only the hosted paper endpoint")
        if client is None and (not api_key or not api_secret):
            raise ValueError("separate Alpaca Paper credentials are required")
        self.base_url = ALPACA_PAPER_BASE_URL
        self.repository = repository
        self.client = client or TradingClient(
            api_key=api_key,
            secret_key=api_secret,
            paper=True,
            url_override=ALPACA_PAPER_BASE_URL,
        )
        self.data_client = data_client or StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
        try:
            self.data_feed = DataFeed(data_feed.lower())
        except ValueError as exc:
            raise ValueError(f"unsupported Alpaca data feed: {data_feed}") from exc
        self._portfolio_id: str | None = None
        self._asset_cache: dict[str, dict[str, Any]] = {}
        self._asset_cache_at_ms = 0
        self.last_reconciliation: dict[str, Any] | None = None

    @property
    def live_capable(self) -> bool:
        return False

    async def submit_intent(self, intent: OrderIntent) -> dict[str, Any]:
        if intent.execution_mode != "paper" or intent.asset_class != "equity" or intent.venue_id != "alpaca:paper":
            raise ValueError("Alpaca Paper accepts only equity paper intents addressed to alpaca:paper")
        client_order_id = _client_order_id(intent.intent_id)
        existing = await self._get_by_client_order_id(client_order_id)
        if existing is not None:
            return existing
        request = MarketOrderRequest(
            symbol=intent.provider_symbol or intent.asset,
            qty=float(intent.target_size),
            side=OrderSide.BUY if intent.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        order = _dump(await _client_call(self.client.submit_order, request))
        await self._mirror_order(order, signal_id=intent.parent_candidate_id)
        return order

    async def submit_equity_trade(self, request: EquityTradeRequest) -> dict[str, Any]:
        if request.quantity is None or request.quantity <= 0:
            raise ValueError("hosted Alpaca Paper orders require an explicit positive share quantity")
        if request.stop is None or request.stop <= 0 or request.take_profit is None or request.take_profit <= 0:
            raise ValueError("hosted Alpaca Paper entries require positive broker-hosted stop and take-profit prices")
        client_order_id = _client_order_id(request.signal_id or f"equity:{request.symbol}:{request.side}:{request.quantity}")
        existing = await self._get_by_client_order_id(client_order_id)
        if existing is not None:
            return existing
        order_request = MarketOrderRequest(
            symbol=request.symbol.upper(),
            qty=float(request.quantity),
            side=OrderSide.BUY if request.side == "long" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=float(request.take_profit)),
            stop_loss=StopLossRequest(stop_price=float(request.stop)),
        )
        order = _dump(await _client_call(self.client.submit_order, order_request))
        await self._mirror_order(order, signal_id=request.signal_id)
        return order

    async def cancel_order(self, order_id: str) -> None:
        await _client_call(self.client.cancel_order_by_id, order_id)

    async def cancel_all(self) -> list[dict[str, Any]]:
        rows = await _client_call(self.client.cancel_orders)
        return [_dump(item) for item in rows or []]

    async def reconcile(self) -> dict[str, Any]:
        account, orders, positions = await asyncio.gather(
            _client_call(self.client.get_account),
            _client_call(self.client.get_orders, GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)),
            _client_call(self.client.get_all_positions),
        )
        account_row = _dump(account)
        order_rows = [_dump(item) for item in orders or []]
        position_rows = [_dump(item) for item in positions or []]
        await self._mirror_account(account_row)
        for order in order_rows:
            await self._mirror_order(order, signal_id=None)
        for position in position_rows:
            await self._mirror_position(position)
        result = {
            "source_of_truth": "alpaca_paper",
            "base_url": self.base_url,
            "account": account_row,
            "orders": order_rows,
            "positions": position_rows,
            "open_order_count": sum(1 for item in order_rows if str(item.get("status")) not in {"filled", "canceled", "expired", "rejected"}),
            "position_count": len(position_rows),
            "reconciled_at": datetime.now(UTC).isoformat(),
        }
        self.last_reconciliation = result
        return result

    async def readiness(self) -> dict[str, Any]:
        try:
            result = await self.reconcile()
        except Exception as exc:
            return {"ready": False, "paper_endpoint": True, "error": type(exc).__name__, "source_of_truth": "alpaca_paper"}
        account = result.get("account") or {}
        blocked = bool(account.get("trading_blocked") or account.get("account_blocked"))
        return {
            "ready": not blocked,
            "paper_endpoint": self.base_url == ALPACA_PAPER_BASE_URL,
            "trading_blocked": blocked,
            "source_of_truth": "alpaca_paper",
            "live_capable": False,
            "position_count": result.get("position_count", 0),
            "open_order_count": result.get("open_order_count", 0),
        }

    async def refresh_instruments(
        self,
        instruments: list[InstrumentRef],
        *,
        cache_seconds: int = 900,
    ) -> list[InstrumentRef]:
        """Resolve exact Alpaca tradability without treating a ticker as identity."""

        now_ms = int(time.time() * 1000)
        if not self._asset_cache or now_ms - self._asset_cache_at_ms >= max(1, cache_seconds) * 1000:
            request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AlpacaAssetClass.US_EQUITY)
            assets = await _client_call(self.client.get_all_assets, request)
            self._asset_cache = {
                str(row.get("symbol") or "").upper(): row
                for row in (_dump(item) for item in assets or [])
                if row.get("symbol")
            }
            self._asset_cache_at_ms = now_ms
        resolved: list[InstrumentRef] = []
        for ref in instruments:
            asset = self._asset_cache.get(ref.provider_symbol.upper())
            active = bool(asset and asset.get("tradable") is not False)
            resolved.append(
                ref.model_copy(
                    update={
                        "tradability_status": "active" if active else "absent",
                        "capabilities": {
                            **ref.capabilities,
                            "quote": active,
                            "bars": active,
                            "paper_execution": active,
                            "fractionable": bool((asset or {}).get("fractionable")),
                            "shortable": bool((asset or {}).get("shortable")),
                            "easy_to_borrow": bool((asset or {}).get("easy_to_borrow")),
                        },
                        "metadata": {
                            **ref.metadata,
                            "alpaca_asset_id": (asset or {}).get("id"),
                            "exchange": (asset or {}).get("exchange"),
                            "asset_class": (asset or {}).get("class"),
                            "provider_verified_at_ms": now_ms,
                        },
                    }
                )
            )
        return resolved

    async def market_snapshots(self, instruments: list[InstrumentRef]) -> list[VenueMarketSnapshot]:
        active = [item for item in instruments if item.tradability_status == "active"]
        if not active:
            return []
        request = StockLatestQuoteRequest(
            symbol_or_symbols=[item.provider_symbol for item in active],
            feed=self.data_feed,
        )
        raw_quotes = await _client_call(self.data_client.get_stock_latest_quote, request)
        quotes = {str(symbol).upper(): _dump(value) for symbol, value in (raw_quotes or {}).items()}
        received_at_ms = int(time.time() * 1000)
        snapshots: list[VenueMarketSnapshot] = []
        for ref in active:
            quote = quotes.get(ref.provider_symbol.upper())
            if not quote:
                continue
            bid = _float(quote.get("bid_price"))
            ask = _float(quote.get("ask_price"))
            mid = (bid + ask) / 2.0 if bid and ask else bid or ask
            exchange_ts_ms = _timestamp_ms(quote.get("timestamp"))
            snapshots.append(
                VenueMarketSnapshot(
                    snapshot_id=f"vms_alpaca_{ref.instrument_id}_{received_at_ms}_{uuid4().hex[:6]}",
                    instrument_id=ref.instrument_id,
                    underlying_id=ref.underlying_id,
                    venue_id=ref.venue_id,
                    provider_symbol=ref.provider_symbol,
                    bid_px=bid,
                    ask_px=ask,
                    mid_px=mid,
                    exchange_ts_ms=exchange_ts_ms,
                    received_ts_ms=received_at_ms,
                    source_integrity="confirmed",
                    staleness_ms=max(0, received_at_ms - exchange_ts_ms) if exchange_ts_ms else None,
                    metadata={
                        "provider": "alpaca",
                        "feed": self.data_feed.value,
                        "paper_execution_source_of_truth": True,
                        "read_only_market_data": True,
                    },
                )
            )
        return snapshots

    async def _get_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            return _dump(await _client_call(self.client.get_order_by_client_id, client_order_id))
        except APIError as exc:
            if getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower():
                return None
            raise

    async def _portfolio(self, account: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return None
        if self._portfolio_id is None:
            equity = _float((account or {}).get("equity")) or 0.0
            portfolio = await self.repository.create_or_get_equity_paper_portfolio(
                "alpaca_hosted_paper",
                equity,
                mode="alpaca_hosted_paper_source_of_truth",
            )
            self._portfolio_id = str(portfolio["id"])
            return portfolio
        return {"id": self._portfolio_id}

    async def _mirror_account(self, account: dict[str, Any]) -> None:
        portfolio = await self._portfolio(account)
        if portfolio is None:
            return
        initial = _float(account.get("last_equity")) or _float(account.get("equity")) or 0.0
        cash = _float(account.get("cash")) or 0.0
        await self.repository.upsert_equity_paper_portfolio(
            {
                "id": portfolio["id"],
                "name": "alpaca_hosted_paper",
                "status": "blocked" if account.get("trading_blocked") else "active",
                "initial_equity_usd": initial,
                "cash_usd": cash,
                "realized_pnl_usd": 0.0,
                "metadata": {"source_of_truth": "alpaca_paper", "broker_account_id": account.get("id")},
            }
        )

    async def _mirror_order(self, order: dict[str, Any], *, signal_id: str | None) -> None:
        portfolio = await self._portfolio()
        if portfolio is None or not order:
            return
        broker_id = str(order.get("id") or order.get("client_order_id") or "")
        if not broker_id:
            return
        side = "long" if str(order.get("side")).lower() == "buy" else "short"
        filled_at = order.get("filled_at")
        await self.repository.create_equity_paper_order(
            {
                "id": broker_id,
                "portfolio_id": portfolio["id"],
                "signal_id": signal_id,
                "symbol": order.get("symbol"),
                "side": side,
                "order_type": order.get("type") or "market",
                "status": order.get("status") or "accepted",
                "quantity": _float(order.get("qty")) or 0.0,
                "filled_px": _float(order.get("filled_avg_price")),
                "filled_at": filled_at,
                "metadata": {
                    "source_of_truth": "alpaca_paper",
                    "client_order_id": order.get("client_order_id"),
                    "broker_order": order,
                },
            }
        )
        filled_qty = _float(order.get("filled_qty")) or 0.0
        filled_px = _float(order.get("filled_avg_price"))
        if filled_qty > 0 and filled_px:
            await self.repository.record_equity_paper_fill(
                {
                    "id": f"alpaca_fill_{broker_id}",
                    "order_id": broker_id,
                    "portfolio_id": portfolio["id"],
                    "symbol": order.get("symbol"),
                    "side": side,
                    "quantity": filled_qty,
                    "price": filled_px,
                    "fee_usd": 0.0,
                    "slippage_usd": 0.0,
                    "metadata": {"source_of_truth": "alpaca_paper"},
                }
            )

    async def _mirror_position(self, position: dict[str, Any]) -> None:
        portfolio = await self._portfolio()
        if portfolio is None or not position:
            return
        qty = _float(position.get("qty")) or 0.0
        symbol = str(position.get("symbol") or "").upper()
        asset_id = str(position.get("asset_id") or symbol)
        await self.repository.upsert_equity_paper_position(
            {
                "id": f"alpaca_pos_{asset_id}",
                "portfolio_id": portfolio["id"],
                "symbol": symbol,
                "side": "long" if qty >= 0 else "short",
                "status": "open",
                "quantity": abs(qty),
                "avg_entry_px": _float(position.get("avg_entry_price")) or 0.0,
                "mark_px": _float(position.get("current_price")),
                "unrealized_pnl_usd": _float(position.get("unrealized_pl")) or 0.0,
                "opened_at": datetime.now(UTC).isoformat(),
                "metadata": {"source_of_truth": "alpaca_paper", "broker_position": position},
            }
        )


class AlpacaIndexDataAdapter:
    """Read-only adapter for Alpaca's index value endpoints."""

    def __init__(self, *, api_key: str, api_secret: str, base_url: str = "https://data.alpaca.markets/v1beta1/indices", client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=10.0,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def latest_values(self, symbols: list[str]) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.base_url}/latest/values",
            params={"index_symbols": ",".join(symbols)},
        )
        response.raise_for_status()
        return response.json()

    async def historical_values(self, symbols: list[str], *, start: str, end: str, limit: int = 1000) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.base_url}/values",
            params={"index_symbols": ",".join(symbols), "start": start, "end": end, "limit": limit},
        )
        response.raise_for_status()
        return response.json()


def _client_order_id(value: str) -> str:
    return "hta_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


async def _client_call(method: Any, *args: Any) -> Any:
    """Call injectable async fakes directly and real blocking SDK methods off-loop."""

    if inspect.iscoroutinefunction(method):
        return await method(*args)
    return await asyncio.to_thread(method, *args)
