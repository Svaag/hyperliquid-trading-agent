from __future__ import annotations

import hashlib
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

InstrumentType = Literal[
    "crypto_perp",
    "hip3_perp",
    "equity",
    "etf",
    "index_benchmark",
    "commodity_perp",
    "fx_perp",
    "synthetic_perp",
    "unknown",
]
TradabilityStatus = Literal["active", "delisted", "absent", "data_only", "disabled"]
WatchlistTier = Literal["pinned", "broad"]


def stable_instrument_id(venue_id: str, provider_symbol: str) -> str:
    """Return a deterministic provider-specific identity.

    A symbol is not globally unique: ``COIN`` at Alpaca and ``xyz:COIN`` on
    Hyperliquid are different instruments even though they share an underlying.
    """

    key = f"{venue_id.strip().lower()}|{provider_symbol.strip()}"
    return "ins_" + hashlib.sha256(key.encode()).hexdigest()[:32]


class InstrumentRef(BaseModel):
    instrument_id: str = ""
    underlying_id: str
    venue_id: str
    provider_symbol: str
    instrument_type: InstrumentType = "unknown"
    quote_currency: str = "USD"
    session_timezone: str = "UTC"
    tradability_status: TradabilityStatus = "active"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    mapping_version: int = Field(default=1, ge=1)
    display_symbol: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("underlying_id", "venue_id", "provider_symbol")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("underlying_id, venue_id, and provider_symbol are required")
        return value

    @field_validator("quote_currency")
    @classmethod
    def _uppercase_quote(cls, value: str) -> str:
        return value.strip().upper() or "USD"

    @model_validator(mode="after")
    def _derive_identity(self) -> Self:
        if not self.instrument_id:
            self.instrument_id = stable_instrument_id(self.venue_id, self.provider_symbol)
        if not self.display_symbol:
            self.display_symbol = self.provider_symbol.split(":", 1)[-1]
        return self


class WatchlistMembership(BaseModel):
    membership_id: str
    instrument_id: str
    tier: WatchlistTier = "pinned"
    desired: bool = True
    enabled: bool = True
    source: str = "admin"
    created_by: str = "system"
    created_at_ms: int
    updated_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class WatchlistChangeRequest(BaseModel):
    action: Literal["add", "move", "remove", "import_us_large_cap"]
    symbol: str | None = None
    venue_id: str | None = None
    instrument_id: str | None = None
    tier: WatchlistTier = "pinned"
    reason: str = ""
    actor: str = "api"
    metadata: dict[str, Any] = Field(default_factory=dict)


class VenueMarketSnapshot(BaseModel):
    snapshot_id: str
    instrument_id: str
    underlying_id: str
    venue_id: str
    provider_symbol: str
    bid_px: float | None = None
    ask_px: float | None = None
    mid_px: float | None = None
    mark_px: float | None = None
    index_px: float | None = None
    last_trade_px: float | None = None
    volume_24h: float | None = None
    open_interest: float | None = None
    funding_rate: float | None = None
    depth_bands: dict[str, Any] = Field(default_factory=dict)
    exchange_ts_ms: int | None = None
    received_ts_ms: int
    source_integrity: str = "confirmed"
    staleness_ms: int | None = Field(default=None, ge=0)
    sequence: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrossVenueFeatureSnapshot(BaseModel):
    snapshot_id: str
    underlying_id: str
    reference_instrument_id: str
    comparison_instrument_id: str
    reference_venue_id: str
    comparison_venue_id: str
    as_of_ms: int
    price_delta_bps: float | None = None
    volume_imbalance: float | None = None
    depth_divergence: float | None = None
    liquidation_divergence: float | None = None
    lead_lag_windows: dict[str, float] = Field(default_factory=dict)
    max_clock_skew_ms: int | None = None
    quality_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
