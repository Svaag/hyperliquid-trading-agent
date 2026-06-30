#!/usr/bin/env python
"""Operator live smoke for the Hyperliquid managed-gRPC StreamFills transport.

NOT run in CI — this is the one step that needs a real provider endpoint + key.
Connects `StreamFills` for a few seconds, decodes liquidation fills through the
same `HyperliquidGrpcAdapter` the app uses, and asserts each carries
`source_integrity=vendor` + a non-null `block_height`.

    pip install '.[grpc]'
    HL_GRPC_ENDPOINT=host:443 HL_GRPC_API_KEY=... \
        python scripts/grpc_liq_smoke.py [seconds]

Optional env: HL_GRPC_AUTH_HEADER (default x-api-key), HL_GRPC_PROVIDER,
HL_GRPC_USE_TLS (default 1).
"""

from __future__ import annotations

import asyncio
import os
import sys

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.liquidations.adapters.hyperliquid_grpc import HyperliquidGrpcAdapter
from hyperliquid_trading_agent.app.liquidations.models import SourceIntegrity


async def main(duration_s: float) -> int:
    endpoint = os.environ.get("HL_GRPC_ENDPOINT", "")
    api_key = os.environ.get("HL_GRPC_API_KEY", "")
    if not endpoint or not api_key:
        print("set HL_GRPC_ENDPOINT and HL_GRPC_API_KEY", file=sys.stderr)
        return 2

    settings = Settings(
        liquidations_hl_grpc_enabled=True,
        hl_grpc_endpoint=endpoint,
        hl_grpc_api_key=api_key,
        hl_grpc_auth_header=os.environ.get("HL_GRPC_AUTH_HEADER", "x-api-key"),
        hl_grpc_provider=os.environ.get("HL_GRPC_PROVIDER", "smoke"),
        hl_grpc_use_tls=os.environ.get("HL_GRPC_USE_TLS", "1") not in ("0", "false", "False"),
    )
    adapter = HyperliquidGrpcAdapter(settings)
    print(f"connecting StreamFills @ {endpoint} (tls={settings.hl_grpc_use_tls}) for {duration_s:.0f}s…")

    seen = 0

    async def _drain() -> None:
        nonlocal seen
        async for event in adapter.run():
            seen += 1
            assert event.source_integrity == SourceIntegrity.VENDOR, event.source_integrity
            assert event.block_height is not None, "vendor liq missing block_height"
            print(
                f"  {event.event_type} {event.symbol} {event.liquidated_side} "
                f"px={event.price} sz={event.size_base} block={event.block_height} id={event.event_id}"
            )

    task = asyncio.create_task(_drain())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=duration_s)
    except TimeoutError:
        pass
    finally:
        adapter.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    print(f"\nSMOKE OK — decoded {seen} liquidation fill(s) as vendor.")
    if seen == 0:
        print("(no liquidations in the window — re-run during a market move to see fills)")
    return 0


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    raise SystemExit(asyncio.run(main(seconds)))
