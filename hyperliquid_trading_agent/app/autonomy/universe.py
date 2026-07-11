from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.autonomy.schemas import MarketAsset
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.markets.universe import WatchlistService

log = get_logger(__name__)


class MarketUniverseResolver:
    """Resolve the autonomy market universe from Hyperliquid public metadata.

    The resolver is deterministic and read-only. It never touches signed exchange
    endpoints and reports unresolved configured aliases as warnings.
    """

    def __init__(self, settings: Settings, hyperliquid: Any, repository: Any | None = None):
        self.settings = settings
        self.hyperliquid = hyperliquid
        self.repository = repository
        self.warnings: list[str] = []
        self.last_assets: list[MarketAsset] = []

    async def resolve(self) -> list[MarketAsset]:
        self.warnings = []
        assets: dict[str, MarketAsset] = {}
        try:
            main_meta_and_ctxs = await self.hyperliquid.meta_and_asset_ctxs()
        except Exception as exc:
            self.warnings.append(f"main metaAndAssetCtxs unavailable: {type(exc).__name__}")
            main_meta_and_ctxs = []
        for asset in self._resolve_main_perps(main_meta_and_ctxs):
            assets[asset.symbol] = asset

        requested_by_dex = await self._canonical_hip3_requests()
        if self.settings.autonomy_hip3_dex_names:
            await self._resolve_hip3_assets(assets, requested_by_dex=requested_by_dex)

        ordered = sorted(
            assets.values(),
            key=lambda item: (
                0 if item.symbol in self.settings.autonomy_core_symbols else 1 if item.source == "top_volume" else 2,
                -(item.day_volume_usd or 0.0),
                item.symbol,
            ),
        )[: self.settings.autonomy_max_tracked_assets]
        self.last_assets = ordered
        return ordered

    def status(self) -> dict[str, Any]:
        return {
            "universe_count": len(self.last_assets),
            "symbols": [asset.symbol for asset in self.last_assets],
            "warnings": list(self.warnings),
        }

    def _resolve_main_perps(self, meta_and_ctxs: Any) -> list[MarketAsset]:
        universe, contexts = _split_meta_and_contexts(meta_and_ctxs)
        ctx_by_symbol = {_symbol_from_ctx(symbol, ctx): ctx for symbol, ctx in zip([_asset_symbol(item) for item in universe], contexts, strict=False)}
        assets: dict[str, MarketAsset] = {}
        universe_by_symbol = {_asset_symbol(item): item for item in universe if _asset_symbol(item)}

        for symbol in self.settings.autonomy_core_symbols:
            raw = universe_by_symbol.get(symbol)
            ctx = ctx_by_symbol.get(symbol, {})
            if raw is None and not ctx:
                self.warnings.append(f"core asset unresolved: {symbol}")
                continue
            assets[symbol] = _asset_from_raw(symbol, raw or {}, ctx, source="core")

        ranked = sorted(
            [(_asset_symbol(item), item) for item in universe if _asset_symbol(item)],
            key=lambda pair: _float(ctx_by_symbol.get(pair[0], {}).get("dayNtlVlm")) or 0.0,
            reverse=True,
        )
        added_top_volume = 0
        for symbol, raw in ranked:
            if symbol in assets:
                continue
            assets[symbol] = _asset_from_raw(symbol, raw, ctx_by_symbol.get(symbol, {}), source="top_volume")
            added_top_volume += 1
            if added_top_volume >= self.settings.autonomy_universe_top_n_perps:
                break
        return list(assets.values())

    async def _canonical_hip3_requests(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self.repository is None:
            return {}
        try:
            service = WatchlistService(self.repository)
            await service.seed_if_empty(actor="autonomy_universe")
            rows = await service.list(status="active", limit=20_000)
        except Exception as exc:
            self.warnings.append(f"canonical watchlist unavailable: {type(exc).__name__}")
            return {}
        requested: dict[str, dict[str, dict[str, Any]]] = {}
        for item in rows:
            venue_id = str(item.get("venue_id") or "")
            if not venue_id.startswith("hyperliquid:") or venue_id == "hyperliquid:main":
                continue
            dex = venue_id.split(":", 1)[1]
            requested.setdefault(dex, {})[str(item.get("provider_symbol") or "").upper()] = item
        return requested

    async def _resolve_hip3_assets(
        self,
        assets: dict[str, MarketAsset],
        *,
        requested_by_dex: dict[str, dict[str, dict[str, Any]]],
    ) -> None:
        alias_map = self.settings.autonomy_index_aliases
        for dex in self.settings.autonomy_hip3_dex_names:
            try:
                meta_and_ctxs = await self.hyperliquid.meta_and_asset_ctxs(dex=dex)
            except Exception as exc:
                self.warnings.append(f"HIP-3 dex {dex} meta unavailable: {type(exc).__name__}")
                continue
            universe, contexts = _split_meta_and_contexts(meta_and_ctxs)
            ctx_by_symbol = {_symbol_from_ctx(symbol, ctx): ctx for symbol, ctx in zip([_asset_symbol(item) for item in universe], contexts, strict=False)}
            dex_symbols = {_asset_symbol(item): item for item in universe if _asset_symbol(item)}
            searchable = {symbol.upper(): symbol for symbol in dex_symbols}
            for canonical, aliases in alias_map.items():
                matched_symbol = next((searchable[alias] for alias in aliases if alias in searchable), None)
                if matched_symbol is None:
                    self.warnings.append(f"HIP-3 alias unresolved on {dex}: {canonical} ({'|'.join(aliases)})")
                    continue
                raw = dex_symbols[matched_symbol]
                ctx = ctx_by_symbol.get(matched_symbol, {})
                asset = _asset_from_raw(matched_symbol, raw, ctx, source="hip3_alias", dex=dex, kind="hip3_index")
                asset = asset.model_copy(update={"display_name": canonical, "metadata": {**asset.metadata, "aliases": aliases}})
                assets[asset.symbol] = asset
            for requested_symbol, registry_row in requested_by_dex.get(dex, {}).items():
                matched_symbol = searchable.get(requested_symbol)
                if matched_symbol is None:
                    self.warnings.append(f"watchlist instrument unresolved on {dex}: {requested_symbol}")
                    continue
                raw = dex_symbols[matched_symbol]
                ctx = ctx_by_symbol.get(matched_symbol, {})
                asset = _asset_from_raw(
                    matched_symbol,
                    raw,
                    ctx,
                    source="watchlist",
                    dex=dex,
                    kind="hip3_perp",
                )
                asset = asset.model_copy(
                    update={
                        "display_name": str(registry_row.get("display_symbol") or matched_symbol).upper(),
                        "metadata": {
                            **asset.metadata,
                            "instrument_id": registry_row.get("instrument_id"),
                            "underlying_id": registry_row.get("underlying_id"),
                            "instrument_type": registry_row.get("instrument_type"),
                        },
                    }
                )
                assets[asset.symbol] = asset


def _split_meta_and_contexts(meta_and_ctxs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(meta_and_ctxs, list) and len(meta_and_ctxs) >= 2:
        meta = meta_and_ctxs[0] if isinstance(meta_and_ctxs[0], dict) else {}
        contexts = meta_and_ctxs[1] if isinstance(meta_and_ctxs[1], list) else []
        universe = meta.get("universe", []) if isinstance(meta.get("universe"), list) else []
        return [item for item in universe if isinstance(item, dict)], [item for item in contexts if isinstance(item, dict)]
    if isinstance(meta_and_ctxs, dict):
        universe = meta_and_ctxs.get("universe", []) if isinstance(meta_and_ctxs.get("universe"), list) else []
        contexts = meta_and_ctxs.get("contexts", []) if isinstance(meta_and_ctxs.get("contexts"), list) else []
        return [item for item in universe if isinstance(item, dict)], [item for item in contexts if isinstance(item, dict)]
    return [], []


def _asset_symbol(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("coin") or item.get("symbol") or "").upper()


def _symbol_from_ctx(default_symbol: str, ctx: dict[str, Any]) -> str:
    return str(ctx.get("coin") or ctx.get("name") or default_symbol or "").upper()


def _asset_from_raw(
    symbol: str,
    raw: dict[str, Any],
    ctx: dict[str, Any],
    *,
    source: str,
    dex: str | None = None,
    kind: str = "perp",
) -> MarketAsset:
    leverage = raw.get("maxLeverage") or raw.get("max_leverage") or raw.get("marginTableId")
    return MarketAsset(
        symbol=symbol.upper(),
        display_name=str(raw.get("displayName") or raw.get("name") or symbol).upper(),
        source=source,  # type: ignore[arg-type]
        dex=dex,
        kind=kind,  # type: ignore[arg-type]
        sz_decimals=_optional_int(raw.get("szDecimals")),
        max_leverage=_optional_int(leverage),
        day_volume_usd=_float(ctx.get("dayNtlVlm")),
        metadata={"raw": raw, "context": ctx},
    )


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
