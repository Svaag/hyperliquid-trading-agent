from __future__ import annotations

import asyncio
import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.discord import AutonomyAlertSink
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.discord_bot import _chunk
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import NEWSWIRE_DISCORD_POSTS
from hyperliquid_trading_agent.app.newswire.bus import NewswireBus
from hyperliquid_trading_agent.app.newswire.enrich import Enricher
from hyperliquid_trading_agent.app.newswire.format import format_news_digest, format_news_event
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter

log = get_logger(__name__)


class DiscordNewsPublisher:
    """Routes the curated feed to a dedicated #news channel.

    Breaking / high-importance events post immediately; the rest roll up into a periodic
    batched digest to avoid Discord rate limits. Trade signals are unaffected — they keep
    going to the autonomy alert channel.
    """

    def __init__(self, *, settings: Settings, bus: NewswireBus, alert_sink: AutonomyAlertSink | None = None, enricher: Enricher | None = None):
        self.settings = settings
        self.bus = bus
        self.alert_sink = alert_sink
        self.enricher = enricher
        self._buffer: list[NewswireEvent] = []
        self._buffer_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._last_send_ms = 0
        self._subscription_id: str | None = None
        self._flush_task: asyncio.Task | None = None

    @property
    def _channel_id(self) -> str:
        return self.settings.newswire_news_channel_id

    async def start(self) -> None:
        if not self.settings.newswire_enabled or not self.settings.newswire_news_channel_configured:
            return
        flt = NewswireFilter(min_importance=self.settings.newswire_news_min_importance)
        self._subscription_id = await self.bus.subscribe(self._on_event, filter=flt)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="newswire-discord-digest")
        log.info("newswire_discord_publisher_started", channel=self._channel_id)

    async def stop(self) -> None:
        if self._subscription_id is not None:
            await self.bus.unsubscribe(self._subscription_id)
            self._subscription_id = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

    async def _on_event(self, event: NewswireEvent) -> None:
        if self.alert_sink is None:
            return
        breaking = event.urgency == "breaking" or event.importance_score >= self.settings.newswire_breaking_min_importance
        if breaking:
            await self._enrich(event)
            await self._send(format_news_event(event), mode="breaking")
        else:
            async with self._buffer_lock:
                self._buffer.append(event)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(max(30, self.settings.newswire_digest_interval_seconds))
            try:
                await self._flush()
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                log.warning("newswire_digest_flush_failed", error=type(exc).__name__)

    async def _flush(self) -> None:
        async with self._buffer_lock:
            events, self._buffer = self._buffer, []
        if not events:
            return
        events.sort(key=lambda item: item.importance_score, reverse=True)
        for event in events[:3]:
            await self._enrich(event)
        await self._send(format_news_digest(events), mode="digest")

    async def _enrich(self, event: NewswireEvent) -> None:
        if self.enricher is None or event.enrichment is not None:
            return
        enrichment = await self.enricher.maybe_enrich(event)
        if enrichment:
            event.enrichment = enrichment

    async def _send(self, content: str, *, mode: str) -> None:
        if self.alert_sink is None or not self._channel_id:
            return
        async with self._send_lock:
            for chunk in _chunk(content, self.settings.discord_max_response_chars):
                await self._throttle()
                try:
                    result = await self.alert_sink.send(self._channel_id, chunk)
                    NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="ok" if result else "skipped").inc()
                except Exception as exc:  # pragma: no cover - Discord runtime behavior
                    NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="error").inc()
                    log.warning("newswire_discord_send_failed", error=type(exc).__name__)

    async def _throttle(self) -> None:
        min_interval = max(0, self.settings.newswire_send_min_interval_ms)
        now = int(time.time() * 1000)
        wait_ms = self._last_send_ms + min_interval - now
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        self._last_send_ms = int(time.time() * 1000)

    def status(self) -> dict[str, Any]:
        return {"channel_configured": self.settings.newswire_news_channel_configured, "buffered": len(self._buffer)}
