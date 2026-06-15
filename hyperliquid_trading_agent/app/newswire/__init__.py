"""Free-standing Newswire: a pub/sub news & macro ingestion gateway.

Adapters ingest raw items from many sources (RSS, Alpaca WS, Trading Economics WS,
curated X). The service normalizes them into a single canonical ``NewswireEvent``,
dedupes/scores/classifies deterministically, applies a halt/risk gate, then publishes
to a transport-agnostic ``NewswireBus``. Consumers (the trading agent, a Discord #news
channel, external WebSocket clients) subscribe with a ``NewswireFilter``.

The bus runs in-process today via asyncio fan-out, but the contract is designed so the
transport can later be swapped (Redis/NATS) or the service split into its own process
without touching consumers.
"""

from hyperliquid_trading_agent.app.newswire.schemas import (
    NewswireEvent,
    NewswireFilter,
    RawNewsItem,
    Tradability,
)

__all__ = ["NewswireEvent", "NewswireFilter", "RawNewsItem", "Tradability"]
