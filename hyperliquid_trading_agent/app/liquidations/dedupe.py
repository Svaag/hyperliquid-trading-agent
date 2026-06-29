"""Venue-aware, deterministic event-id construction and address redaction.

This is the only venue-aware layer besides the adapters themselves. Each source
quality gets an id rule chosen to be stable for the *same* underlying event and
distinct across different events, so re-delivered frames (reconnects, snapshot
re-pushes) collapse to one stored row.

Kept import-free of ``models`` to avoid a cycle: it operates on duck-typed
attributes and compares ``StrEnum`` values as plain strings.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent


def _d(value: Decimal | None) -> str:
    """Stable string for a Decimal/None used inside composite ids."""
    if value is None:
        return ""
    # Normalize so Decimal("1.0") and Decimal("1") collapse to the same token.
    return format(value.normalize(), "f") if isinstance(value, Decimal) else str(value)


def _short_hash(payload: Any) -> str:
    return hashlib.blake2b(repr(payload).encode("utf-8"), digest_size=6).hexdigest()


def redact_address(address: str | None) -> str | None:
    """Public-surface redaction: keep a recognizable prefix, hash the identity.

    ``0xabcd...1234`` -> ``0xabcd…<6-hex>`` so the same wallet is still
    correlatable across rows without publishing the full address.
    """
    if not address:
        return None
    prefix = address[:6]
    return f"{prefix}…{_short_hash(address)}"


def event_id_for(event: LiquidationEvent) -> str:
    """Deterministic dedupe key dispatched on (venue, source_integrity)."""
    venue = str(event.venue)
    integrity = str(event.source_integrity)
    market = event.venue_market_id or event.symbol

    if venue == "lighter":
        ref = event.trade_id or event.liquidation_id or _short_hash(event.raw)
        return f"lighter:{market}:{ref}:{event.event_type}:{_d(event.price)}:{_d(event.size_base)}"

    if venue == "aster":
        # snapshot_throttled: no stable trade id guaranteed — pin to symbol+times+fill.
        ref = event.trade_id or ""
        ap = event.avg_price if event.avg_price is not None else event.price
        return f"aster:{event.symbol}:{event.timestamp_ms}:{ref}:{event.liquidated_side}:{_d(ap)}:{_d(event.size_base)}"

    if venue == "hyperliquid":
        if integrity == "confirmed" or integrity == "vendor":
            ref = event.liquidation_id or event.trade_id or _short_hash(event.raw)
            return f"hyperliquid:confirmed:{event.block_height or ''}:{event.symbol}:{ref}"
        if integrity == "account_private":
            user = (event.liquidated_user or "").lower()
            ref = event.liquidation_id or event.trade_id or _short_hash(event.raw)
            return f"hyperliquid:user:{user}:{ref}"
        # derived / pressure: bucket to the second so dup trades within a frame collapse.
        ts_bucket = event.timestamp_ms // 1000
        return (
            f"hyperliquid:derived:{event.symbol}:{ts_bucket}:{event.liquidated_side}"
            f":{_d(event.price)}:{_d(event.size_base)}:{_short_hash(event.raw)}"
        )

    # Chain-native venues (dydx/drift/gmx/...) carry a natural unique anchor.
    if event.tx_hash is not None:
        return f"{venue}:{event.tx_hash}:{event.log_index if event.log_index is not None else ''}"

    # Generic, defensive fallback.
    return (
        f"{venue}:{integrity}:{event.symbol}:{event.timestamp_ms}:{event.liquidated_side}"
        f":{_d(event.price)}:{_d(event.size_base)}:{_short_hash(event.raw)}"
    )
