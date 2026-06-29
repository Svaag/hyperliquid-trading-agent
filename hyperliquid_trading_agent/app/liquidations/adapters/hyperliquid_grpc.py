"""Hyperliquid confirmed/global liquidations via a managed gRPC ``StreamFills``
provider — decode + transport.

This is the only path to *global* Hyperliquid liquidations: stream all fills from
a provider/node and keep those whose payload carries a ``liquidation`` key.

Layering (so CI needs no ``grpcio`` and the decode stays golden-tested):
- ``parse_grpc_fill`` — one node ``node_fills`` object -> ``LiquidationEvent``.
- ``decode_block_fills`` — one ``BlockFills`` JSON envelope -> (events, resume_ms,
  total_fills); injects the envelope ``block_number`` onto each fill. Pure.
- ``HyperliquidGrpcAdapter._open_stream`` — the only piece that touches the
  network and the generated proto stubs; lazily imports ``grpc`` so the module
  imports cleanly without the optional ``grpc`` extra. Overridable seam for tests.

Source integrity is ``vendor`` (provider-indexed all-fills), not ``confirmed``:
we label the *transport* truthfully — a third-party indexer, however reliable, is
a vendor source, not first-party venue confirmation. Until
``liquidations_hl_grpc_enabled`` + an endpoint are set the adapter raises
``NotConfigured`` and the HL badge stays ``derived`` (honest about coverage).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters._ws import dec, to_ms
from hyperliquid_trading_agent.app.liquidations.adapters.base import LiquidationAdapter, NotConfigured
from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    LIQUIDATION_GRPC_FILLS_TOTAL,
    LIQUIDATION_GRPC_LIQUIDATIONS_TOTAL,
)

log = get_logger(__name__)

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


def _split_event(entry: Any) -> tuple[str | None, Any]:
    """Unpack a ``BlockFills.events`` entry into ``(address, fill)``.

    The node envelope carries ``[address, fill]`` pairs; we also tolerate a
    ``{"address"/"user", "fill"}`` mapping or a bare fill object defensively.
    """
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        address, fill = entry
        return (str(address) if address is not None else None), fill
    if isinstance(entry, Mapping):
        address = entry.get("address") or entry.get("user")
        fill = entry.get("fill", entry)
        return (str(address) if address else None), fill
    return None, entry


def decode_block_fills(
    payload: bytes | bytearray | str | Mapping[str, Any],
    *,
    provider: str | None = None,
) -> tuple[list[LiquidationEvent], int | None, int]:
    """Decode one ``StreamFills`` ``BlockFills`` payload into liquidation events.

    ``payload`` is the node JSON envelope
    ``{ block_number, events: [[address, fill], ...] }`` (the gRPC message's JSON
    ``data`` field as bytes/str, or an already-parsed mapping). Each ``fill`` is a
    node ``node_fills`` object; only those carrying a ``liquidation`` key are kept.
    The block height lives on the envelope (never on the fill), so it is injected
    as ``fill["block"]`` before the golden-tested ``parse_grpc_fill`` runs.

    Returns ``(liquidation_events, resume_ms, total_fills)`` where ``resume_ms`` is
    the max fill timestamp seen (the inclusive reconnect cursor) and ``total_fills``
    is every fill in the batch (for stream-coverage metrics), not just the liqs.
    """
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            doc: Any = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return [], None, 0
    else:
        doc = payload
    if not isinstance(doc, Mapping):
        return [], None, 0

    block_number = doc.get("block_number")
    events: list[LiquidationEvent] = []
    resume_ms: int | None = None
    total_fills = 0
    for entry in doc.get("events") or []:
        address, fill = _split_event(entry)
        if not isinstance(fill, dict):
            continue
        total_fills += 1
        if block_number is not None and "block" not in fill:
            fill = {**fill, "block": block_number}
        if address is not None:
            fill.setdefault("_address", address)
        event = parse_grpc_fill(fill, provider=provider)
        if event is None:
            continue
        events.append(event)
        if resume_ms is None or event.timestamp_ms > resume_ms:
            resume_ms = event.timestamp_ms
    return events, resume_ms, total_fills


@dataclass
class StreamPosition:
    """Inclusive start cursor for ``StreamFills`` (kept proto-free so the resume
    logic is testable without ``protobuf``/``grpcio``)."""

    timestamp_ms: int | None = None
    block_height: int | None = None


class HyperliquidGrpcAdapter(LiquidationAdapter):
    venue = "hyperliquid"
    source = "hyperliquid_grpc"
    source_integrity = SourceIntegrity.VENDOR

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._provider = settings.hl_grpc_provider or None
        self._resume_ms: int | None = None  # carried across reconnects (base re-enters)

    def _start_position(self) -> StreamPosition:
        if self._resume_ms is not None:
            # Resume inclusively at the last fill seen; dedupe collapses the one
            # re-delivered fill, so there is no gap and no double-count.
            return StreamPosition(timestamp_ms=self._resume_ms)
        lookback = max(0, self._settings.hl_grpc_resume_lookback_ms)
        return StreamPosition(timestamp_ms=int(time.time() * 1000) - lookback)

    async def _connect_and_stream(self) -> AsyncIterator[LiquidationEvent]:
        s = self._settings
        if not (s.liquidations_hl_grpc_enabled and s.hl_grpc_endpoint):
            raise NotConfigured(
                "Hyperliquid gRPC StreamFills provider is not configured. "
                "Set liquidations_hl_grpc_enabled + hl_grpc_endpoint (Phase 2-live)."
            )
        async for payload in self._open_stream(self._start_position()):
            batch, resume_ms, total_fills = decode_block_fills(payload, provider=self._provider)
            if total_fills:
                LIQUIDATION_GRPC_FILLS_TOTAL.inc(total_fills)
            if resume_ms is not None:
                self._resume_ms = resume_ms
            for event in batch:
                LIQUIDATION_GRPC_LIQUIDATIONS_TOTAL.inc()
                yield event

    async def _open_stream(self, position: StreamPosition) -> AsyncIterator[Any]:
        """Yield ``BlockFills.data`` JSON payloads from the live gRPC stream.

        Overridable seam: tests replace this with a fake async iterator so the
        decode/resume/metrics path is exercised without ``grpcio``. The real path
        lazily imports ``grpc`` + the generated stubs and raises ``NotConfigured``
        when the optional ``grpc`` extra is absent.
        """
        try:
            import grpc

            from hyperliquid_trading_agent.app.liquidations.adapters.proto import (
                hyperliquid_l1_gateway_pb2 as pb,
            )
            from hyperliquid_trading_agent.app.liquidations.adapters.proto import (
                hyperliquid_l1_gateway_pb2_grpc as pb_grpc,
            )
        except ImportError as exc:
            raise NotConfigured(
                "gRPC transport requires the optional 'grpc' extra — install with "
                "`pip install '.[grpc]'` (grpcio + protobuf)."
            ) from exc

        s = self._settings
        metadata = [(s.hl_grpc_auth_header, s.hl_grpc_api_key)] if s.hl_grpc_api_key else None
        if s.hl_grpc_use_tls:
            channel = grpc.aio.secure_channel(s.hl_grpc_endpoint, grpc.ssl_channel_credentials())
        else:
            channel = grpc.aio.insecure_channel(s.hl_grpc_endpoint)
        if position.block_height is not None:
            req = pb.Position(block_height=position.block_height)
        else:
            req = pb.Position(timestamp_ms=position.timestamp_ms or 0)
        log.info("liquidation_grpc_connect", endpoint=s.hl_grpc_endpoint, provider=self._provider)
        try:
            stub = pb_grpc.HyperliquidL1GatewayStub(channel)
            async for block_fills in stub.StreamFills(req, metadata=metadata):
                yield block_fills.data
        finally:
            await channel.close()
