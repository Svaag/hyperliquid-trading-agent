from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

import lighter

from hyperliquid_trading_agent.app.markets.schemas import InstrumentRef, VenueMarketSnapshot


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {}


class LighterSequenceGap(RuntimeError):
    pass


class _GapAwareLighterWsClient(lighter.WsClient):
    """Keep the upstream SDK's state machine while auditing raw sequencing."""

    def __init__(self, *args: Any, sequence_guard: Callable[[dict[str, Any]], None], **kwargs: Any):
        self._sequence_guard = sequence_guard
        super().__init__(*args, **kwargs)

    def on_message(self, ws: Any, message: Any) -> None:
        parsed = json.loads(message) if isinstance(message, str) else message
        if isinstance(parsed, dict):
            self._sequence_guard(parsed)
        super().on_message(ws, parsed)


class LighterSDKMarketDataAdapter:
    """Read-only Lighter mainnet adapter backed by ``elliottech/lighter-python``.

    The adapter deliberately constructs only ``ApiClient``, ``OrderApi``,
    ``FundingApi``, and the public ``WsClient``. Signer and transaction clients are
    absent from this interface by design.
    """

    def __init__(self, *, base_url: str = "https://mainnet.zklighter.elliot.ai", timeout_seconds: float = 10.0):
        configuration = lighter.Configuration(host=base_url.rstrip("/"))
        self.api_client = lighter.ApiClient(configuration)
        self.order_api = lighter.OrderApi(self.api_client)
        self.funding_api = lighter.FundingApi(self.api_client)
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._last_sequence: dict[int, int] = {}
        self._market_details: dict[int, dict[str, Any]] = {}

    @property
    def read_only(self) -> bool:
        return True

    async def close(self) -> None:
        await self.api_client.close()

    async def list_instruments(self) -> list[InstrumentRef]:
        response = await self.order_api.order_book_details(_request_timeout=self.timeout_seconds)
        rows = [_dump(item) for item in response.order_book_details]
        refs: list[InstrumentRef] = []
        self._market_details = {}
        for row in rows:
            market_type = str(row.get("market_type") or "").lower()
            if market_type and market_type != "perp":
                continue
            market_id = int(row["market_id"])
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            self._market_details[market_id] = row
            active = str(row.get("status") or "").lower() in {"active", "open", "trading"}
            refs.append(
                InstrumentRef(
                    underlying_id=f"CRYPTO:{_underlying_symbol(symbol)}",
                    venue_id="lighter",
                    provider_symbol=symbol,
                    instrument_type="crypto_perp",
                    quote_currency="USDC",
                    tradability_status="active" if active else "disabled",
                    capabilities={
                        "market_id": market_id,
                        "mark": True,
                        "funding": True,
                        "open_interest": True,
                        "trades": True,
                        "l2": True,
                        "paper_simulation": True,
                        "read_only": True,
                    },
                    metadata={
                        "sdk": "elliottech/lighter-python@v1.1.0",
                        "maker_fee": row.get("maker_fee"),
                        "taker_fee": row.get("taker_fee"),
                        "size_decimals": row.get("size_decimals"),
                        "price_decimals": row.get("price_decimals"),
                    },
                )
            )
        return refs

    async def snapshot(self, instrument: InstrumentRef, *, depth_limit: int = 100) -> VenueMarketSnapshot:
        market_id_value = instrument.capabilities.get("market_id")
        if market_id_value is None:
            raise ValueError("Lighter instrument is missing a market_id capability")
        market_id = int(market_id_value)
        if market_id < 0:
            raise ValueError("Lighter instrument market_id must be non-negative")
        book_response = await self.order_api.order_book_orders(
            market_id=market_id,
            limit=max(1, min(250, int(depth_limit))),
            _request_timeout=self.timeout_seconds,
        )
        trades_response = await self.order_api.recent_trades(
            market_id=market_id,
            limit=1,
            _request_timeout=self.timeout_seconds,
        )
        details = self._market_details.get(market_id, {})
        bids = [_simple_level(item) for item in book_response.bids]
        asks = [_simple_level(item) for item in book_response.asks]
        bids = sorted((item for item in bids if item[0] > 0 and item[1] > 0), reverse=True)
        asks = sorted(item for item in asks if item[0] > 0 and item[1] > 0)
        bid = bids[0][0] if bids else None
        ask = asks[0][0] if asks else None
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else bid or ask
        trades = list(trades_response.trades or [])
        latest_trade = _dump(trades[0]) if trades else {}
        received_at_ms = _now_ms()
        exchange_ts_ms = int(latest_trade.get("timestamp") or latest_trade.get("transaction_time") or 0) or None
        return VenueMarketSnapshot(
            snapshot_id=f"vms_lighter_{market_id}_{received_at_ms}_{uuid4().hex[:6]}",
            instrument_id=instrument.instrument_id,
            underlying_id=instrument.underlying_id,
            venue_id="lighter",
            provider_symbol=instrument.provider_symbol,
            bid_px=bid,
            ask_px=ask,
            mid_px=mid,
            last_trade_px=_float(latest_trade.get("price")),
            volume_24h=_float(details.get("daily_quote_token_volume")),
            open_interest=_float(details.get("open_interest")),
            depth_bands={
                "bids": [{"px": px, "size": size} for px, size in bids],
                "asks": [{"px": px, "size": size} for px, size in asks],
            },
            exchange_ts_ms=exchange_ts_ms,
            received_ts_ms=received_at_ms,
            source_integrity="confirmed",
            staleness_ms=max(0, received_at_ms - exchange_ts_ms) if exchange_ts_ms else None,
            metadata={"sdk": "elliottech/lighter-python@v1.1.0", "market_id": market_id, "read_only": True},
        )

    async def funding_rates(self) -> dict[int, float]:
        response = await self.funding_api.funding_rates(_request_timeout=self.timeout_seconds)
        return {int(item.market_id): float(item.rate) for item in response.funding_rates}

    def websocket_client(
        self,
        market_ids: list[int],
        *,
        on_order_book_update: Callable[[int, dict[str, Any]], Awaitable[None] | None],
    ) -> Any:
        if not market_ids:
            raise ValueError("at least one Lighter market_id is required")

        def callback(market_id_value: str | int, payload: dict[str, Any]) -> None:
            market_id = int(market_id_value)
            result = on_order_book_update(market_id, payload)
            if inspect.isawaitable(result):
                # WsClient invokes callbacks synchronously; schedule async sinks on
                # the active loop without blocking SDK message processing.
                import asyncio

                asyncio.get_running_loop().create_task(result)

        return _GapAwareLighterWsClient(
            host=self.base_url.removeprefix("https://").removeprefix("http://"),
            order_book_ids=[int(item) for item in market_ids],
            account_ids=[],
            on_order_book_update=callback,
            on_account_update=None,
            sequence_guard=self._guard_raw_sequence,
        )

    def _guard_raw_sequence(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "")
        if message_type not in {"subscribed/order_book", "update/order_book"}:
            return
        channel = str(message.get("channel") or "")
        market_text = channel.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        try:
            market_id = int(market_text)
        except ValueError:
            return
        order_book = message.get("order_book") if isinstance(message.get("order_book"), dict) else {}
        sequence_value = message.get("sequence", order_book.get("sequence"))
        nonce_value = message.get("nonce", order_book.get("nonce"))
        if sequence_value is not None:
            sequence = int(sequence_value)
            previous = self._last_sequence.get(market_id)
            if message_type == "update/order_book" and previous is not None and sequence != previous + 1:
                self._last_sequence.pop(market_id, None)
                raise LighterSequenceGap(
                    f"Lighter market {market_id} sequence gap: previous={previous} current={sequence}"
                )
            self._last_sequence[market_id] = sequence
        elif nonce_value is not None:
            nonce = int(nonce_value)
            previous = self._last_sequence.get(market_id)
            if message_type == "update/order_book" and previous is not None and nonce <= previous:
                self._last_sequence.pop(market_id, None)
                raise LighterSequenceGap(
                    f"Lighter market {market_id} nonce regression: previous={previous} current={nonce}"
                )
            self._last_sequence[market_id] = nonce


@dataclass(frozen=True, slots=True)
class LocalPaperFill:
    status: str
    requested_size: float
    filled_size: float
    avg_fill_px: float | None
    fees_usd: float
    slippage_bps: float
    venue_id: str = "lighter"


class LighterLocalPaperSimulator:
    def simulate(
        self,
        *,
        side: str,
        size: float,
        snapshot: VenueMarketSnapshot,
        taker_fee_rate: float,
        price_limit: float | None = None,
    ) -> LocalPaperFill:
        if side not in {"buy", "sell"} or size <= 0:
            raise ValueError("side must be buy/sell and size must be positive")
        depth = snapshot.depth_bands.get("asks" if side == "buy" else "bids") or []
        remaining = float(size)
        notional = 0.0
        filled = 0.0
        for level in depth:
            px = _float(level.get("px")) or 0.0
            available = _float(level.get("size")) or 0.0
            if px <= 0 or available <= 0:
                continue
            if price_limit is not None and ((side == "buy" and px > price_limit) or (side == "sell" and px < price_limit)):
                break
            take = min(remaining, available)
            notional += take * px
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        avg_fill = notional / filled if filled > 0 else None
        reference = (snapshot.mid_px or snapshot.ask_px) if side == "buy" else (snapshot.mid_px or snapshot.bid_px)
        direction = 1.0 if side == "buy" else -1.0
        slippage = ((avg_fill / reference) - 1.0) * 10_000.0 * direction if avg_fill and reference else 0.0
        status = "filled" if filled >= size - 1e-12 else "partial" if filled > 0 else "rejected"
        return LocalPaperFill(
            status=status,
            requested_size=float(size),
            filled_size=filled,
            avg_fill_px=avg_fill,
            fees_usd=notional * max(0.0, taker_fee_rate),
            slippage_bps=max(0.0, slippage),
        )


def _simple_level(value: Any) -> tuple[float, float]:
    row = _dump(value)
    return _float(row.get("price")) or 0.0, _float(row.get("remaining_base_amount")) or 0.0


def _underlying_symbol(symbol: str) -> str:
    for suffix in ("-USDC", "/USDC", "_USDC"):
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
