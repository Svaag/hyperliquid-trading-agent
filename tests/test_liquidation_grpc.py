"""Phase 2-live tests: the managed-gRPC StreamFills transport.

`parse_grpc_fill` is golden-tested in `test_liquidation_reconcile.py`; here we
cover the network-glue layer **without `grpcio`** — the pure `decode_block_fills`
envelope decode, the reconnect-resume cursor via an injected `_open_stream`, and
the two `NotConfigured` gates (disabled / grpc-extra-missing).
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters.base import NotConfigured
from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_grpc import (
    HyperliquidGrpcAdapter,
    StreamPosition,
    decode_block_fills,
)
from hyperliquid_trading_agent.app.liquidations.models import EventType, SourceIntegrity

BASE_MS = 1_700_000_000_000  # realistic epoch-ms (node fills carry ms, not seconds)


def _liq_fill(*, coin: str, time: int, direction: str, method: str, tid: int) -> dict[str, Any]:
    """A node `node_fills` liquidation object — note: no `block` field (the
    envelope carries `block_number`, which the decoder injects)."""
    return {
        "coin": coin,
        "px": "60000",
        "sz": "0.5",
        "dir": direction,
        "time": time,
        "tid": tid,
        "liquidation": {"liquidatedUser": "0xVICTIM", "markPx": "59000", "method": method},
    }


def _block_fills(block_number: int) -> bytes:
    """A BlockFills JSON envelope: 2 liquidation fills + 1 ordinary fill."""
    return json.dumps(
        {
            "block_number": block_number,
            "block_time": "2026-06-29T00:00:00Z",
            "events": [
                ["0xaaa", _liq_fill(coin="BTC", time=BASE_MS + 1000, direction="Close Long", method="market", tid=1)],
                ["0xbbb", _liq_fill(coin="ETH", time=BASE_MS + 1500, direction="Close Short", method="backstop", tid=2)],
                ["0xccc", {"coin": "SOL", "px": "150", "sz": "1", "dir": "Open Long", "time": BASE_MS + 1200, "tid": 3}],
            ],
        }
    ).encode()


# ------------------------------------------------------------- decode_block_fills


def test_decode_block_fills_keeps_only_liquidations_and_injects_block() -> None:
    events, resume_ms, total_fills = decode_block_fills(_block_fills(100), provider="acme")

    assert total_fills == 3  # all fills counted for stream-coverage metrics
    assert len(events) == 2  # the ordinary SOL fill is dropped
    assert resume_ms == BASE_MS + 1500  # max liquidation timestamp -> inclusive reconnect cursor

    btc, eth = events
    assert btc.source == "hyperliquid_grpc" and btc.source_integrity == SourceIntegrity.VENDOR
    assert btc.symbol == "BTC" and btc.liquidated_side == "long"
    assert btc.event_type == EventType.LIQUIDATION
    assert eth.event_type == EventType.BACKSTOP and eth.liquidated_side == "short"

    # block_height comes from the envelope, not the fill, and drives the dedupe id.
    assert btc.block_height == 100
    assert btc.event_id.startswith("hyperliquid:confirmed:100:BTC")
    assert btc.raw["_provider"] == "acme" and btc.raw["_address"] == "0xaaa"


def test_decode_block_fills_handles_garbage_and_empty() -> None:
    assert decode_block_fills(b"not json") == ([], None, 0)
    assert decode_block_fills(json.dumps({"events": []}).encode()) == ([], None, 0)
    # already-parsed mapping is accepted too
    events, _, total = decode_block_fills({"block_number": 7, "events": []})
    assert events == [] and total == 0


# --------------------------------------------------------------- resume cursor


class _FakeStreamAdapter(HyperliquidGrpcAdapter):
    """Overrides the network seam with canned per-connection payload batches."""

    def __init__(self, settings: Settings, batches: list[list[bytes]]) -> None:
        super().__init__(settings)
        self._batches = batches
        self._conn = 0
        self.positions: list[StreamPosition] = []

    async def _open_stream(self, position: StreamPosition) -> AsyncIterator[Any]:
        self.positions.append(position)
        payloads = self._batches[self._conn]
        self._conn += 1
        for payload in payloads:
            yield payload


async def test_reconnect_resumes_at_last_seen_timestamp() -> None:
    settings = Settings(liquidations_hl_grpc_enabled=True, hl_grpc_endpoint="host:443")
    adapter = _FakeStreamAdapter(settings, batches=[[_block_fills(100)], [_block_fills(101)]])

    first = [ev async for ev in adapter._connect_and_stream()]
    assert len(first) == 2 and adapter._resume_ms == BASE_MS + 1500

    second = [ev async for ev in adapter._connect_and_stream()]
    assert len(second) == 2

    # Cold start streamed from ~now-lookback; the reconnect resumes inclusively at
    # the last fill seen (dedupe collapses the single re-delivered fill).
    assert adapter.positions[0].timestamp_ms is not None
    assert adapter.positions[0].timestamp_ms > BASE_MS + 1500  # cold-start = now-lookback
    assert adapter.positions[1].timestamp_ms == BASE_MS + 1500


# ---------------------------------------------------------------- gating gates


async def test_disabled_adapter_stays_not_configured() -> None:
    adapter = HyperliquidGrpcAdapter(Settings(liquidations_hl_grpc_enabled=False))
    with pytest.raises(NotConfigured):
        async for _ in adapter._connect_and_stream():
            pass


async def test_enabled_without_grpc_extra_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the optional 'grpc' extra being absent even though the adapter is
    # configured: `import grpc` then fails inside the real _open_stream.
    monkeypatch.setitem(sys.modules, "grpc", None)
    adapter = HyperliquidGrpcAdapter(
        Settings(liquidations_hl_grpc_enabled=True, hl_grpc_endpoint="host:443")
    )
    with pytest.raises(NotConfigured, match="grpc"):
        async for _ in adapter._connect_and_stream():
            pass
