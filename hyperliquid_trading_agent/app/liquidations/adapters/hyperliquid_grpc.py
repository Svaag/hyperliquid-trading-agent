"""Hyperliquid confirmed/global liquidations via a managed gRPC ``StreamFills``
provider — decode ready, transport gated.

This is the only path to *global* Hyperliquid liquidations: stream all fills from
a provider/node and keep those whose payload carries a ``liquidation`` key. The
decode (`parse_grpc_fill`) is implemented and unit-tested now; the only remaining
infra-gated piece is wiring the provider transport in ``_connect_and_stream``.
Until ``liquidations_hl_grpc_enabled`` + an endpoint are set, the adapter raises
``NotConfigured`` and the venue badge stays ``derived`` (honest about coverage).

Source integrity is ``vendor`` (provider-indexed all-fills), not ``confirmed``:
we label the *transport* truthfully — a third-party indexer, however reliable, is
a vendor source, not first-party venue confirmation.

TODO(phase2-live): add ``grpcio`` + generated StreamFills stubs as an optional
extra and implement ``_connect_and_stream`` against ``hl_grpc_endpoint`` using
``hl_grpc_api_key``; feed each fill through ``parse_grpc_fill``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._ws import dec, to_ms
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter, NotConfigured
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity

_DIR_TO_SIDE = {"close long": "long", "close short": "short"}


def parse_grpc_fill(fill: dict[str, Any], *, provider: str | None = None) -> LiquidationEvent | None:
    """Decode one StreamFills fill into a vendor-confirmed liquidation event.

    Only fills carrying a ``liquidation`` object are liquidations; everything else
    is an ordinary fill and is skipped. ``method == "backstop"`` marks the HLP
    backstop path. The full fill is preserved in ``raw`` for audit/replay.
    """
    liquidation = fill.get("liquidation")
    if not isinstance(liquidation, dict):
        return None
    method = str(liquidation.get("method", "")).lower()
    event_type = EventType.BACKSTOP if method == "backstop" else EventType.LIQUIDATION
    direction = str(fill.get("dir", "")).lower()
    side = _DIR_TO_SIDE.get(direction, "unknown")
    return LiquidationEvent(
        venue="hyperliquid",
        source="hyperliquid_grpc",
        source_integrity=SourceIntegrity.VENDOR,
        event_type=event_type,
        symbol=str(fill.get("coin", "")).upper(),
        liquidated_side=side,  # type: ignore[arg-type]
        raw_side=direction or None,
        price=dec(fill.get("px")),
        size_base=dec(fill.get("sz")),
        mark_price=dec(liquidation.get("markPx")),
        timestamp_ms=to_ms(fill.get("time")) or int(time.time() * 1000),
        received_at_ms=int(time.time() * 1000),
        block_height=int(fill["block"]) if str(fill.get("block", "")).isdigit() else None,
        trade_id=str(fill.get("tid") or fill.get("hash") or "") or None,
        liquidation_id=str(liquidation.get("liquidationId") or "") or None,
        liquidated_user=str(liquidation.get("liquidatedUser") or "") or None,
        method=method or None,
        confidence=Decimal("0.95"),  # vendor-indexed: high but not first-party
        raw={**fill, "_provider": provider} if provider else fill,
    )


class HyperliquidGrpcAdapter(LiquidationAdapter):
    venue = "hyperliquid"
    source = "hyperliquid_grpc"
    source_integrity = SourceIntegrity.VENDOR

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._provider = settings.hl_grpc_provider or None

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        if not (self._settings.liquidations_hl_grpc_enabled and self._settings.hl_grpc_endpoint):
            raise NotConfigured(
                "Hyperliquid gRPC StreamFills provider is not configured (Phase 2-live). "
                "Set liquidations_hl_grpc_enabled + hl_grpc_endpoint and wire the grpcio transport."
            )
        raise NotConfigured(  # pragma: no cover - transport intentionally unimplemented
            "gRPC transport not implemented yet; decode (parse_grpc_fill) is ready."
        )
        yield  # pragma: no cover - marks this as an async generator for the type checker
