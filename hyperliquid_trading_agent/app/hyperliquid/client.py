from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import HYPERLIQUID_LATENCY, HYPERLIQUID_REQUESTS

log = get_logger(__name__)

INFO_ENDPOINT_TYPES = {
    "allMids",
    "meta",
    "metaAndAssetCtxs",
    "spotMeta",
    "spotMetaAndAssetCtxs",
    "clearinghouseState",
    "spotClearinghouseState",
    "frontendOpenOrders",
    "openOrders",
    "userFills",
    "userFillsByTime",
    "historicalOrders",
    "userFunding",
    "fundingHistory",
    "predictedFundings",
    "l2Book",
    "candleSnapshot",
    "userRateLimit",
    "orderStatus",
    "perpDexs",
    "outcomeMeta",
    "settledOutcome",
    "userFees",
    "portfolio",
    "userNonFundingLedgerUpdates",
    "userTwapSliceFills",
    "userVaultEquities",
    "userRole",
    "extraAgents",
    "subAccounts",
    "referral",
    "delegatorSummary",
    "delegations",
    "delegatorRewards",
    "delegatorHistory",
}

LOW_WEIGHT_INFO_TYPES = {
    "allMids",
    "l2Book",
    "clearinghouseState",
    "spotClearinghouseState",
    "orderStatus",
}


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    expires_at: float


class MinuteWeightLimiter:
    """Simple in-process Hyperliquid REST weight limiter.

    Official docs: REST requests share 1200 aggregate weight/minute/IP. This is a
    conservative process-local guard; deployments with multiple replicas still
    need external coordination.
    """

    def __init__(self, max_weight_per_minute: int = 1100):
        self.max_weight_per_minute = max_weight_per_minute
        self._lock = asyncio.Lock()
        self._window_start = time.monotonic()
        self._used = 0

    async def acquire(self, weight: int) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_start
            if elapsed >= 60:
                self._window_start = now
                self._used = 0
            if self._used + weight > self.max_weight_per_minute:
                sleep_for = max(0.0, 60 - elapsed)
                log.warning("hyperliquid_rate_limit_sleep", sleep_seconds=round(sleep_for, 3), weight=weight)
                await asyncio.sleep(sleep_for)
                self._window_start = time.monotonic()
                self._used = 0
            self._used += weight


class HyperliquidClient:
    """Async REST client for official Hyperliquid /info endpoints.

    The MVP intentionally does not implement signed /exchange actions. Use the
    official Python SDK when a later gated execution phase is enabled.
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(base_url=settings.hyperliquid_base_url, timeout=10.0)
        self._cache: dict[str, CacheEntry] = {}
        self._limiter = MinuteWeightLimiter()

    async def close(self) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def post_info(self, payload: dict[str, Any], *, cache_ttl_seconds: int | None = None) -> Any:
        request_type = str(payload.get("type", "unknown"))
        if request_type not in INFO_ENDPOINT_TYPES:
            log.warning("hyperliquid_unknown_info_type", request_type=request_type)
        cache_key = _cache_key(payload)
        if cache_ttl_seconds:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at > time.monotonic():
                return cached.value

        weight = _request_weight(request_type)
        await self._limiter.acquire(weight)
        started = time.perf_counter()
        try:
            response = await self.http.post("/info", json=payload)
            response.raise_for_status()
            data = response.json()
            if cache_ttl_seconds:
                self._cache[cache_key] = CacheEntry(data, time.monotonic() + cache_ttl_seconds)
            HYPERLIQUID_REQUESTS.labels(type=request_type, result="ok").inc()
            return data
        except Exception:
            HYPERLIQUID_REQUESTS.labels(type=request_type, result="error").inc()
            log.exception("hyperliquid_info_request_failed", request_type=request_type)
            raise
        finally:
            HYPERLIQUID_LATENCY.labels(type=request_type).observe(time.perf_counter() - started)

    async def all_mids(self, dex: str = "") -> dict[str, str]:
        payload: dict[str, Any] = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def meta(self, dex: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "meta"}
        if dex:
            payload["dex"] = dex
        return await self.post_info(payload, cache_ttl_seconds=300)

    async def meta_and_asset_ctxs(self, dex: str = "") -> list[Any]:
        payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def market_universe(self, dex: str = "") -> list[dict[str, Any]]:
        data = await self.meta_and_asset_ctxs(dex=dex)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            universe = data[0].get("universe", [])
            return [item for item in universe if isinstance(item, dict)] if isinstance(universe, list) else []
        return []

    async def active_asset_contexts(self, dex: str = "") -> dict[str, dict[str, Any]]:
        data = await self.meta_and_asset_ctxs(dex=dex)
        if not (isinstance(data, list) and len(data) >= 2 and isinstance(data[0], dict) and isinstance(data[1], list)):
            return {}
        universe = data[0].get("universe", []) if isinstance(data[0].get("universe"), list) else []
        contexts = data[1]
        out: dict[str, dict[str, Any]] = {}
        for raw, ctx in zip(universe, contexts, strict=False):
            if isinstance(raw, dict) and isinstance(ctx, dict):
                symbol = str(ctx.get("coin") or raw.get("name") or "").upper()
                if symbol:
                    out[symbol] = ctx
        return out

    async def perp_dexs(self) -> Any:
        return await self.post_info({"type": "perpDexs"}, cache_ttl_seconds=300)

    async def spot_meta(self) -> dict[str, Any]:
        return await self.post_info({"type": "spotMeta"}, cache_ttl_seconds=300)

    async def spot_meta_and_asset_ctxs(self) -> list[Any]:
        return await self.post_info({"type": "spotMetaAndAssetCtxs"}, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def outcome_meta(self) -> dict[str, Any]:
        return await self.post_info({"type": "outcomeMeta"}, cache_ttl_seconds=self.settings.hip4_outcome_meta_refresh_seconds)

    async def settled_outcome(self, outcome_id: int) -> Any:
        return await self.post_info({"type": "settledOutcome", "outcome": int(outcome_id)}, cache_ttl_seconds=self.settings.hip4_settlement_refresh_seconds)

    async def l2_book(self, coin: str, n_sig_figs: int | None = None, mantissa: int | None = None) -> Any:
        payload: dict[str, Any] = {"type": "l2Book", "coin": coin}
        if n_sig_figs is not None:
            payload["nSigFigs"] = n_sig_figs
        if mantissa is not None:
            payload["mantissa"] = mantissa
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def candle_snapshot(self, coin: str, interval: str, start_time_ms: int, end_time_ms: int) -> Any:
        return await self.post_info(
            {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": start_time_ms, "endTime": end_time_ms},
            },
            cache_ttl_seconds=self.settings.cache_ttl_market_seconds,
        )

    async def user_state(self, address: str, dex: str = "") -> Any:
        payload: dict[str, Any] = {"type": "clearinghouseState", "user": address.lower()}
        if dex:
            payload["dex"] = dex
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def spot_user_state(self, address: str) -> Any:
        return await self.post_info(
            {"type": "spotClearinghouseState", "user": address.lower()},
            cache_ttl_seconds=self.settings.cache_ttl_market_seconds,
        )

    async def open_orders(self, address: str, dex: str = "") -> Any:
        payload: dict[str, Any] = {"type": "openOrders", "user": address.lower()}
        if dex:
            payload["dex"] = dex
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def frontend_open_orders(self, address: str, dex: str = "") -> Any:
        payload: dict[str, Any] = {"type": "frontendOpenOrders", "user": address.lower()}
        if dex:
            payload["dex"] = dex
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def user_fills(self, address: str, aggregate_by_time: bool = True) -> Any:
        return await self.post_info(
            {"type": "userFills", "user": address.lower(), "aggregateByTime": aggregate_by_time},
            cache_ttl_seconds=self.settings.cache_ttl_market_seconds,
        )

    async def user_fills_by_time(
        self,
        address: str,
        start_time_ms: int,
        end_time_ms: int | None = None,
        aggregate_by_time: bool = True,
    ) -> Any:
        payload: dict[str, Any] = {
            "type": "userFillsByTime",
            "user": address.lower(),
            "startTime": start_time_ms,
            "aggregateByTime": aggregate_by_time,
        }
        if end_time_ms is not None:
            payload["endTime"] = end_time_ms
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def historical_orders(self, address: str) -> Any:
        return await self.post_info(
            {"type": "historicalOrders", "user": address.lower()},
            cache_ttl_seconds=self.settings.cache_ttl_market_seconds,
        )

    async def user_funding(self, address: str, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        payload: dict[str, Any] = {"type": "userFunding", "user": address.lower(), "startTime": start_time_ms}
        if end_time_ms is not None:
            payload["endTime"] = end_time_ms
        return await self.post_info(payload, cache_ttl_seconds=self.settings.cache_ttl_market_seconds)

    async def funding_history(self, coin: str, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        payload: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": start_time_ms}
        if end_time_ms is not None:
            payload["endTime"] = end_time_ms
        return await self.post_info(payload, cache_ttl_seconds=60)

    async def predicted_fundings(self) -> Any:
        return await self.post_info({"type": "predictedFundings"}, cache_ttl_seconds=60)

    async def user_rate_limit(self, address: str) -> Any:
        return await self.post_info({"type": "userRateLimit", "user": address.lower()}, cache_ttl_seconds=30)

    async def order_status(self, address: str, oid: int | str) -> Any:
        return await self.post_info({"type": "orderStatus", "user": address.lower(), "oid": oid}, cache_ttl_seconds=5)

    async def user_fees(self, address: str) -> Any:
        return await self.post_info({"type": "userFees", "user": address.lower()}, cache_ttl_seconds=300)

    async def portfolio(self, address: str) -> Any:
        return await self.post_info({"type": "portfolio", "user": address.lower()}, cache_ttl_seconds=60)

    async def user_non_funding_ledger_updates(self, address: str, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        payload: dict[str, Any] = {"type": "userNonFundingLedgerUpdates", "user": address.lower(), "startTime": start_time_ms}
        if end_time_ms is not None:
            payload["endTime"] = end_time_ms
        return await self.post_info(payload, cache_ttl_seconds=60)

    async def user_twap_slice_fills(self, address: str) -> Any:
        return await self.post_info({"type": "userTwapSliceFills", "user": address.lower()}, cache_ttl_seconds=60)

    async def user_vault_equities(self, address: str) -> Any:
        return await self.post_info({"type": "userVaultEquities", "user": address.lower()}, cache_ttl_seconds=60)

    async def user_role(self, address: str) -> Any:
        return await self.post_info({"type": "userRole", "user": address.lower()}, cache_ttl_seconds=300)

    async def extra_agents(self, address: str) -> Any:
        return await self.post_info({"type": "extraAgents", "user": address.lower()}, cache_ttl_seconds=300)

    async def sub_accounts(self, address: str) -> Any:
        return await self.post_info({"type": "subAccounts", "user": address.lower()}, cache_ttl_seconds=300)

    async def referral_state(self, address: str) -> Any:
        return await self.post_info({"type": "referral", "user": address.lower()}, cache_ttl_seconds=300)

    async def public_liquidation_levels(self, addresses: list[str], dex: str = "") -> list[dict[str, Any]]:
        """Return directly observable liquidation prices from configured public accounts.

        This reads public clearinghouse state only. Hidden stops and broad heatmaps
        remain inferred elsewhere and must be labeled as such.
        """

        levels: list[dict[str, Any]] = []
        for address in addresses:
            state = await self.user_state(address, dex=dex)
            positions = state.get("assetPositions", []) if isinstance(state, dict) else []
            for item in positions:
                if not isinstance(item, dict) or not isinstance(item.get("position"), dict):
                    continue
                position = item["position"]
                liquidation_px = _optional_float(position.get("liquidationPx"))
                if liquidation_px is None or liquidation_px <= 0:
                    continue
                size = _optional_float(position.get("szi")) or 0.0
                levels.append(
                    {
                        "symbol": str(position.get("coin") or "").upper(),
                        "price": liquidation_px,
                        "side_at_risk": "longs" if size > 0 else "shorts" if size < 0 else "unknown",
                        "notional_usd_known": _optional_float(position.get("positionValue")),
                        "source": "public_account",
                        "confidence": "direct",
                        "account": address.lower(),
                        "metadata": {"position": position},
                    }
                )
        return levels


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _request_weight(request_type: str) -> int:
    return 2 if request_type in LOW_WEIGHT_INFO_TYPES else 20


def _cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
