"""Hyperliquid confirmed global liquidations via node/gRPC ``StreamFills`` — STUB.

This is the only path to *confirmed, global* Hyperliquid liquidations: stream all
fills from a node (or a managed gRPC provider) and keep those whose payload
carries a ``liquidation`` key (``dir`` Close Long/Short, ``user == liquidatedUser``).
It is intentionally not wired for the MVP — running/operating that source is a
Phase 2 decision. The adapter exists so the boundary is complete and the
``confirmed`` upgrade is a drop-in: implement ``_connect_and_stream`` against the
gRPC client and flip the venue badge ``derived → confirmed``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter, NotConfigured
from hyperliquid_trading_agent.app.liquidations.models import LiquidationEvent, SourceIntegrity


class HyperliquidGrpcAdapter(LiquidationAdapter):
    venue = "hyperliquid"
    source = "hyperliquid_grpc"
    source_integrity = SourceIntegrity.CONFIRMED

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        raise NotConfigured(
            "Hyperliquid gRPC StreamFills source is not configured (Phase 2). "
            "Implement against a node/managed gRPC endpoint to enable confirmed global capture."
        )
        yield  # pragma: no cover - marks this as an async generator for the type checker
