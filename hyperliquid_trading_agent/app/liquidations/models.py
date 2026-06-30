"""The generalized liquidation contract.

Every venue exposes liquidations through a *different* visibility model, so the
contract carries two honesty axes that the product never collapses:

- ``source_integrity`` — how trustworthy/complete the *source* is
  (confirmed execution vs. throttled snapshot vs. account-private vs. derived
  inference vs. vendor index).
- ``event_type`` — what *kind* of event it is, including a distinct
  ``liquidation_pressure`` for inferred flow that must never be labeled as a
  confirmed liquidation.

`LiquidationEvent` is the single normalized row the whole subsystem consumes;
adapters are the only venue-aware producers, and `dedupe` is the only other
venue-aware layer. The public API serves a redacted view (`public_view`) that
hashes counterparties and drops the raw provenance payload.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hyperliquid_trading_agent.app.liquidations import dedupe

Venue = Literal[
    "hyperliquid",
    "lighter",
    "aster",
    "dydx",
    "drift",
    "gmx",
    "orderly",
    "other",
]

Side = Literal["long", "short", "unknown"]


class SourceIntegrity(StrEnum):
    """How complete/trustworthy the producing source is. Drives the UI badges."""

    CONFIRMED = "confirmed"  # venue/indexer explicitly marks a liquidation/deleverage
    SNAPSHOT_THROTTLED = "snapshot_throttled"  # public stream coalesces/drops (e.g. Aster)
    ACCOUNT_PRIVATE = "account_private"  # exact, but only for a subscribed account
    DERIVED = "derived"  # inferred from trades/flow/vault behavior
    VENDOR = "vendor"  # provider-indexed / all-fills source


class EventType(StrEnum):
    """What kind of event this is. ``LIQUIDATION_PRESSURE`` is inferred-only."""

    LIQUIDATION = "liquidation"
    BACKSTOP = "backstop"
    ADL = "adl"
    DELEVERAGE = "deleverage"
    MARKET_SETTLEMENT = "market_settlement"
    LIQUIDATION_PRESSURE = "liquidation_pressure"  # inferred — never "confirmed"


# Event types that represent a *confirmed execution* rather than inferred pressure.
EXECUTION_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.LIQUIDATION,
        EventType.BACKSTOP,
        EventType.ADL,
        EventType.DELEVERAGE,
        EventType.MARKET_SETTLEMENT,
    }
)


class LiquidationEvent(BaseModel):
    """One normalized liquidation (or liquidation-pressure) event.

    ``event_id`` and ``notional_usd`` are derived after construction when not
    supplied, so adapters can build an event from a raw payload and let the
    contract assign a deterministic, venue-aware dedupe key.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = ""  # deterministic, venue-aware; auto-filled by `dedupe`

    venue: Venue
    source: str  # producing adapter id, e.g. "lighter_ws"
    source_integrity: SourceIntegrity
    event_type: EventType

    symbol: str  # normalized market symbol, e.g. "BTC"
    venue_market_id: str | None = None  # venue-native market id / index

    liquidated_side: Side = "unknown"
    raw_side: str | None = None  # venue-native side string, for provenance

    price: Decimal | None = None
    avg_price: Decimal | None = None
    mark_price: Decimal | None = None
    bankruptcy_price: Decimal | None = None

    size_base: Decimal | None = None
    notional_usd: Decimal | None = None

    timestamp_ms: int  # exchange event time
    received_at_ms: int  # local ingest time

    # On-chain / venue provenance (used by chain-native venues + dedupe).
    block_height: int | None = None
    tx_hash: str | None = None
    log_index: int | None = None
    trade_id: str | None = None
    liquidation_id: str | None = None

    liquidated_user: str | None = None  # redacted on the public surface
    liquidator: str | None = None  # redacted on the public surface
    method: str | None = None  # venue-native method, e.g. HL "market"/"backstop"

    confidence: Decimal = Decimal("1.0")
    raw: dict[str, Any] = Field(default_factory=dict)  # full provenance, kept private

    @model_validator(mode="after")
    def _derive(self) -> LiquidationEvent:
        # Inferred pressure can never masquerade as a confirmed source.
        if self.event_type == EventType.LIQUIDATION_PRESSURE and self.source_integrity == SourceIntegrity.CONFIRMED:
            raise ValueError("liquidation_pressure events must not use source_integrity=confirmed")
        if self.notional_usd is None:
            ref_price = self.avg_price if self.avg_price is not None else self.price
            if self.size_base is not None and ref_price is not None:
                self.notional_usd = self.size_base * ref_price
        if not self.event_id:
            self.event_id = dedupe.event_id_for(self)
        return self

    @property
    def is_execution(self) -> bool:
        """True for a confirmed-kind execution, False for inferred pressure."""
        return self.event_type in EXECUTION_EVENT_TYPES

    def public_view(self) -> dict[str, Any]:
        """JSON-safe dict for the public API: counterparties hashed, raw dropped."""
        data = self.model_dump(mode="json", exclude={"raw", "liquidated_user", "liquidator"})
        # Emit numeric fields as JSON numbers (not Decimal strings) so the public
        # API is consistent with the DB-backed projection and chart-friendly.
        for field in ("price", "avg_price", "mark_price", "bankruptcy_price", "size_base", "notional_usd", "confidence"):
            value = getattr(self, field)
            data[field] = float(value) if value is not None else None
        data["liquidated_user"] = dedupe.redact_address(self.liquidated_user)
        data["liquidator"] = dedupe.redact_address(self.liquidator)
        return data


class LiquidationSignal(BaseModel):
    """Read-only, observe-only signal handed to the trading agent.

    Carries ``source_mix`` so the agent always sees the data-quality behind a
    number and can choose to weight ``confirmed`` over ``derived``. This signal
    must never be used to loosen risk, raise leverage, or change sizing — only to
    annotate/alert, and to feed defensive actions through the existing
    ``RiskGateway``.
    """

    model_config = ConfigDict(extra="forbid")

    venue: str  # specific venue, or "all" for cross-venue aggregate
    symbol: str
    window_ms: int

    long_liq_notional_usd: Decimal = Decimal("0")
    short_liq_notional_usd: Decimal = Decimal("0")
    net_liq_imbalance_usd: Decimal = Decimal("0")  # long - short
    max_single_liq_usd: Decimal = Decimal("0")
    event_count: int = 0

    source_mix: dict[str, int] = Field(default_factory=dict)  # source_integrity -> count
    confidence: Decimal = Decimal("1.0")
    as_of_ms: int = 0
