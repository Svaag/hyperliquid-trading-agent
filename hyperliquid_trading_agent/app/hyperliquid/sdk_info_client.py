from __future__ import annotations

import asyncio
from typing import Any, Callable

from hyperliquid.info import Info

from hyperliquid_trading_agent.app.config import Settings


class SDKInfoClient:
    """Async wrapper around the official Hyperliquid Python SDK Info client.

    This adapter intentionally exposes read-only Info methods only. It never
    imports or instantiates hyperliquid.exchange.Exchange and cannot sign or send
    /exchange actions.
    """

    def __init__(self, settings: Settings, info_factory: Callable[..., Any] | None = None):
        self.settings = settings
        self.info_factory = info_factory or Info
        self._info: Any | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        info = self._info
        if info is not None and getattr(info, "ws_manager", None) is not None:
            await asyncio.to_thread(info.disconnect_websocket)

    async def _ensure_info(self) -> Any:
        if self._info is not None:
            return self._info
        async with self._lock:
            if self._info is None:
                self._info = await asyncio.to_thread(self.info_factory, self.settings.hyperliquid_base_url, True)
        return self._info

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        info = await self._ensure_info()
        return await asyncio.to_thread(lambda: getattr(info, method)(*args, **kwargs))

    async def _post_info(self, payload: dict[str, Any]) -> Any:
        info = await self._ensure_info()
        return await asyncio.to_thread(lambda: info.post("/info", payload))

    async def all_mids(self, dex: str = "") -> Any:
        return await self._call("all_mids", dex)

    async def meta(self, dex: str = "") -> Any:
        return await self._call("meta", dex)

    async def meta_and_asset_ctxs(self, dex: str = "") -> Any:
        if dex:
            return await self._post_info({"type": "metaAndAssetCtxs", "dex": dex})
        return await self._call("meta_and_asset_ctxs")

    async def perp_dexs(self) -> Any:
        return await self._call("perp_dexs")

    async def spot_meta(self) -> Any:
        return await self._call("spot_meta")

    async def spot_meta_and_asset_ctxs(self) -> Any:
        return await self._call("spot_meta_and_asset_ctxs")

    async def user_state(self, address: str, dex: str = "") -> Any:
        return await self._call("user_state", address.lower(), dex)

    async def spot_user_state(self, address: str) -> Any:
        return await self._call("spot_user_state", address.lower())

    async def open_orders(self, address: str, dex: str = "") -> Any:
        return await self._call("open_orders", address.lower(), dex)

    async def frontend_open_orders(self, address: str, dex: str = "") -> Any:
        return await self._call("frontend_open_orders", address.lower(), dex)

    async def user_fills(self, address: str) -> Any:
        return await self._call("user_fills", address.lower())

    async def user_fills_by_time(self, address: str, start_time_ms: int, end_time_ms: int | None = None, aggregate_by_time: bool = False) -> Any:
        return await self._call("user_fills_by_time", address.lower(), start_time_ms, end_time_ms, aggregate_by_time)

    async def historical_orders(self, address: str) -> Any:
        return await self._call("historical_orders", address.lower())

    async def user_funding_history(self, address: str, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        return await self._call("user_funding_history", address.lower(), start_time_ms, end_time_ms)

    async def funding_history(self, coin: str, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        name = _normalize_coin_name(coin)
        try:
            return await self._call("funding_history", name, start_time_ms, end_time_ms)
        except KeyError:
            payload: dict[str, Any] = {"type": "fundingHistory", "coin": name, "startTime": start_time_ms}
            if end_time_ms is not None:
                payload["endTime"] = end_time_ms
            return await self._post_info(payload)

    async def l2_snapshot(self, coin: str) -> Any:
        name = _normalize_coin_name(coin)
        try:
            return await self._call("l2_snapshot", name)
        except KeyError:
            return await self._post_info({"type": "l2Book", "coin": name})

    async def candles_snapshot(self, coin: str, interval: str, start_time_ms: int, end_time_ms: int) -> Any:
        name = _normalize_coin_name(coin)
        try:
            return await self._call("candles_snapshot", name, interval, start_time_ms, end_time_ms)
        except KeyError:
            return await self._post_info(
                {
                    "type": "candleSnapshot",
                    "req": {"coin": name, "interval": interval, "startTime": start_time_ms, "endTime": end_time_ms},
                }
            )

    async def user_fees(self, address: str) -> Any:
        return await self._call("user_fees", address.lower())

    async def portfolio(self, address: str) -> Any:
        return await self._call("portfolio", address.lower())

    async def user_non_funding_ledger_updates(self, address: str, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        return await self._call("user_non_funding_ledger_updates", address.lower(), start_time_ms, end_time_ms)

    async def user_twap_slice_fills(self, address: str) -> Any:
        return await self._call("user_twap_slice_fills", address.lower())

    async def user_vault_equities(self, address: str) -> Any:
        return await self._call("user_vault_equities", address.lower())

    async def user_role(self, address: str) -> Any:
        return await self._call("user_role", address.lower())

    async def user_rate_limit(self, address: str) -> Any:
        return await self._call("user_rate_limit", address.lower())

    async def extra_agents(self, address: str) -> Any:
        return await self._call("extra_agents", address.lower())

    async def query_sub_accounts(self, address: str) -> Any:
        return await self._call("query_sub_accounts", address.lower())

    async def query_referral_state(self, address: str) -> Any:
        return await self._call("query_referral_state", address.lower())


def _normalize_coin_name(coin: str) -> str:
    text = coin.strip()
    if not text:
        return coin.upper()
    if ":" not in text:
        return text.upper()
    dex, base = text.split(":", 1)
    return f"{dex.strip().lower()}:{base.strip().upper()}"
