"""Shared raw-websocket helpers for venue adapters.

A thin wrapper over ``websockets`` that yields parsed JSON messages with a recv
timeout (so a silently-dead socket triggers the base adapter's reconnect). Kept
separate so Aster/Lighter share one connection loop. Server pings are auto-ponged
by the library; we don't send client pings (Binance-style servers don't expect
them).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets

OnOpen = Callable[[Any], Awaitable[None]]


async def ws_json_messages(
    url: str,
    *,
    on_open: OnOpen | None = None,
    recv_timeout: float = 60.0,
    max_size: int = 2**22,
    ping_payload: dict[str, Any] | None = None,
    ping_interval_s: float = 20.0,
) -> AsyncIterator[dict[str, Any]]:
    async with websockets.connect(url, ping_interval=None, max_size=max_size) as ws:
        if on_open is not None:
            await on_open(ws)
        ping_task: asyncio.Task[None] | None = None
        if ping_payload is not None:
            ping_task = asyncio.create_task(_pinger(ws, ping_payload, ping_interval_s))
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                except TimeoutError as exc:
                    raise ConnectionError("websocket recv timeout") from exc
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(message, dict):
                    yield message
        finally:
            if ping_task is not None:
                ping_task.cancel()


async def _pinger(ws: Any, payload: dict[str, Any], interval_s: float) -> None:
    try:
        while True:
            await asyncio.sleep(interval_s)
            await ws.send(json.dumps(payload))
    except (asyncio.CancelledError, Exception):  # best-effort keepalive; never crash the stream
        return


def dec(value: Any) -> Decimal | None:
    """Best-effort Decimal from a string/number; None on empty/invalid."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def to_ms(timestamp: Any) -> int:
    """Normalize a venue timestamp (s / ms / µs / ns) to milliseconds."""
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    # Bucket by magnitude: seconds < 1e12 <= ms < 1e15 <= µs < 1e18 <= ns
    if value < 1_000_000_000_000:
        return value * 1000
    if value < 1_000_000_000_000_000:
        return value
    if value < 1_000_000_000_000_000_000:
        return value // 1000
    return value // 1_000_000
