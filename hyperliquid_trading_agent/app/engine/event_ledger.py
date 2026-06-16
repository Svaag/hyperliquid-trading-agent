from __future__ import annotations

import hashlib
import time
from typing import Any

from hyperliquid_trading_agent.app.engine.schemas import NormalizedEvent


def now_ms() -> int:
    return int(time.time() * 1000)


def stable_event_id(*, source: str, provider: str, event_type: str, payload: dict[str, Any], received_ts_ms: int) -> str:
    key = f"{source}:{provider}:{event_type}:{received_ts_ms}:{repr(sorted(payload.items()))[:500]}"
    return "evt_" + hashlib.sha1(key.encode()).hexdigest()[:24]


class EventLedger:
    """Append-only normalized event facade.

    The ledger is deliberately thin: adapters own source parsing, this class enforces
    canonical timestamps/quality metadata and delegates persistence to Repository.
    """

    def __init__(self, repository: Any | None = None):
        self.repository = repository
        self._events: dict[str, NormalizedEvent] = {}

    async def record(self, event: NormalizedEvent) -> NormalizedEvent:
        self._events[event.event_id] = event
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_normalized_event", None)
            if callable(record):
                await record(event.model_dump(mode="json"))
        return event

    async def normalize_and_record(
        self,
        *,
        event_type: str,
        source: str,
        provider: str,
        payload: dict[str, Any],
        asset_class: str = "unknown",
        symbols: list[str] | None = None,
        event_ts_ms: int | None = None,
        received_ts_ms: int | None = None,
        quality_score: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> NormalizedEvent:
        received = received_ts_ms or now_ms()
        computed = now_ms()
        staleness = None if event_ts_ms is None else max(0, received - event_ts_ms)
        event = NormalizedEvent(
            event_id=stable_event_id(source=source, provider=provider, event_type=event_type, payload=payload, received_ts_ms=received),
            event_type=event_type,
            asset_class=asset_class,  # type: ignore[arg-type]
            symbols=symbols or [],
            source=source,
            provider=provider,
            event_ts_ms=event_ts_ms,
            received_ts_ms=received,
            computed_ts_ms=computed,
            payload=payload,
            quality_score=quality_score,
            staleness_ms=staleness,
            metadata=metadata or {},
        )
        return await self.record(event)

    async def get(self, event_id: str) -> NormalizedEvent | None:
        if event_id in self._events:
            return self._events[event_id]
        if self.repository is not None and getattr(self.repository, "enabled", False):
            get = getattr(self.repository, "get_normalized_event", None)
            if callable(get):
                data = await get(event_id)
                return NormalizedEvent(**data) if data else None
        return None

    async def list(self, *, limit: int = 100, event_type: str | None = None, asset_class: str | None = None) -> list[NormalizedEvent]:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            list_events = getattr(self.repository, "list_normalized_events", None)
            if callable(list_events):
                return [NormalizedEvent(**item) for item in await list_events(limit=limit, event_type=event_type, asset_class=asset_class)]
        events = list(self._events.values())
        if event_type:
            events = [item for item in events if item.event_type == event_type]
        if asset_class:
            events = [item for item in events if item.asset_class == asset_class]
        return sorted(events, key=lambda item: item.received_ts_ms, reverse=True)[:limit]
