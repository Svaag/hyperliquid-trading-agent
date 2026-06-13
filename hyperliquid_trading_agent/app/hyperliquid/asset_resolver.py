from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedAsset:
    coin: str
    asset_id: int | None
    kind: str
    sz_decimals: int | None = None
    max_leverage: int | None = None
    context: dict[str, Any] | None = None


class AssetResolver:
    """Resolve user-facing coin strings to Hyperliquid perp/spot metadata."""

    def __init__(
        self,
        perp_meta: dict[str, Any] | None = None,
        spot_meta: dict[str, Any] | None = None,
        perp_ctxs: list[dict[str, Any]] | None = None,
        spot_ctxs: list[dict[str, Any]] | None = None,
    ):
        self.perp_meta = perp_meta or {"universe": []}
        self.spot_meta = spot_meta or {"universe": []}
        self.perp_ctxs = perp_ctxs or []
        self.spot_ctxs = spot_ctxs or []

    @classmethod
    def from_meta_and_contexts(cls, perp_meta_and_ctxs: list[Any] | None = None, spot_meta_and_ctxs: list[Any] | None = None) -> AssetResolver:
        perp_meta: dict[str, Any] = {"universe": []}
        perp_ctxs: list[dict[str, Any]] = []
        spot_meta: dict[str, Any] = {"universe": []}
        spot_ctxs: list[dict[str, Any]] = []
        if perp_meta_and_ctxs and len(perp_meta_and_ctxs) >= 2:
            perp_meta = perp_meta_and_ctxs[0]
            perp_ctxs = perp_meta_and_ctxs[1]
        if spot_meta_and_ctxs and len(spot_meta_and_ctxs) >= 2:
            spot_meta = spot_meta_and_ctxs[0]
            spot_ctxs = spot_meta_and_ctxs[1]
        return cls(perp_meta=perp_meta, spot_meta=spot_meta, perp_ctxs=perp_ctxs, spot_ctxs=spot_ctxs)

    def resolve(self, symbol: str) -> ResolvedAsset | None:
        return self.resolve_perp(symbol) or self.resolve_spot(symbol)

    def resolve_perp(self, symbol: str) -> ResolvedAsset | None:
        target = _normalize_symbol(symbol)
        for idx, item in enumerate(self.perp_meta.get("universe", [])):
            name = str(item.get("name", ""))
            if _normalize_symbol(name) == target:
                return ResolvedAsset(
                    coin=name,
                    asset_id=idx,
                    kind="perp",
                    sz_decimals=item.get("szDecimals"),
                    max_leverage=item.get("maxLeverage"),
                    context=self.perp_ctxs[idx] if idx < len(self.perp_ctxs) else None,
                )
        return None

    def resolve_spot(self, symbol: str) -> ResolvedAsset | None:
        raw = symbol.strip()
        target = _normalize_symbol(symbol)
        for idx, item in enumerate(self.spot_meta.get("universe", [])):
            name = str(item.get("name", ""))
            spot_index = int(item.get("index", idx))
            aliases = {_normalize_symbol(name), f"@{spot_index}".upper()}
            if target in aliases or raw == f"@{spot_index}":
                return ResolvedAsset(
                    coin=name,
                    asset_id=10000 + spot_index,
                    kind="spot",
                    context=self.spot_ctxs[idx] if idx < len(self.spot_ctxs) else None,
                )
        return None

    def available_perps(self) -> list[str]:
        return [str(item.get("name")) for item in self.perp_meta.get("universe", []) if item.get("name")]


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("/USDC", "")
