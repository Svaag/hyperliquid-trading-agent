from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from hyperliquid_trading_agent.app.autonomy.discord import AutonomyAlertSink
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import NEWSWIRE_DISCORD_POSTS, NEWSWIRE_DISCORD_SKIPS
from hyperliquid_trading_agent.app.newswire.bus import NewswireBus
from hyperliquid_trading_agent.app.newswire.enrich import Enricher
from hyperliquid_trading_agent.app.newswire.format import format_news_digest_message, format_news_event_message
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter

log = get_logger(__name__)


class DiscordNewsPublisher:
    """Routes the curated Newswire feed to a dedicated #news channel.

    Breaking / high-importance events post immediately; the rest roll up into a periodic
    batched digest. Posting is fresh-only to avoid startup backlog blasts and uses the
    repository publish ledger when available to prevent restart/multi-process dupes.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        bus: NewswireBus,
        alert_sink: AutonomyAlertSink | None = None,
        enricher: Enricher | None = None,
        repository: Any | None = None,
    ):
        self.settings = settings
        self.bus = bus
        self.alert_sink = alert_sink
        self.enricher = enricher
        self.repository = repository
        self._buffer: list[NewswireEvent] = []
        self._buffer_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._last_send_ms = 0
        self._subscription_id: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._running = False
        self._started_at_ms: int | None = None
        self._last_post_at_ms: int | None = None
        self._last_error: str | None = None
        self._posted_count = 0
        self._skip_counts: Counter[str] = Counter()
        self._process_claimed: set[str] = set()

    @property
    def _channel_id(self) -> str:
        return self.settings.newswire_news_channel_id

    @property
    def enabled(self) -> bool:
        owns_news_channel = self.settings.newswire_enabled or self.settings.discord_publisher_enabled
        return bool(owns_news_channel and self.settings.newswire_discord_enabled and self.settings.newswire_news_channel_configured)

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        if self.alert_sink is None or not self.settings.discord_bot_token:
            self._last_error = "discord_sink_or_token_missing"
            log.info("newswire_discord_publisher_disabled", reason=self._last_error)
            return
        self._started_at_ms = _now_ms()
        flt = NewswireFilter(min_importance=self.settings.newswire_news_min_importance)
        self._subscription_id = await self.bus.subscribe(self._on_event, filter=flt)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="newswire-discord-digest")
        self._running = True
        log.info("newswire_discord_publisher_started", channel=self._channel_id)

    async def stop(self) -> None:
        self._running = False
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
        reason = self._skip_reason(event)
        if reason is not None:
            self._skip(reason)
            return
        breaking = event.urgency == "breaking" or event.importance_score >= self.settings.newswire_breaking_min_importance
        if breaking:
            claimed = await self._claim(event, mode="breaking")
            if not claimed:
                self._skip("duplicate")
                return
            await self._enrich(event)
            payload = format_news_event_message(event)
            message_id = await self._send_payload(payload, mode="breaking")
            if message_id:
                await self._mark_posted([event.event_id], message_id)
            else:
                await self._mark_failed([event.event_id], self._last_error or "send_skipped")
        else:
            async with self._buffer_lock:
                self._buffer.append(event)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(max(30, self.settings.newswire_digest_interval_seconds))
            try:
                await self._flush()
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self._last_error = type(exc).__name__
                log.warning("newswire_digest_flush_failed", error=type(exc).__name__)

    async def _flush(self) -> None:
        async with self._buffer_lock:
            events, self._buffer = self._buffer, []
        if not events:
            return
        publishable: list[NewswireEvent] = []
        for event in sorted(events, key=lambda item: item.importance_score, reverse=True):
            reason = self._skip_reason(event)
            if reason is not None:
                self._skip(reason)
                continue
            if await self._claim(event, mode="digest"):
                publishable.append(event)
            else:
                self._skip("duplicate")
        if not publishable:
            return
        max_items = max(1, self.settings.newswire_discord_digest_max_items)
        for chunk in _event_chunks(publishable, max_items):
            for event in chunk[:3]:
                await self._enrich(event)
            payload = format_news_digest_message(chunk, max_items=max_items)
            ids = [event.event_id for event in chunk]
            message_id = await self._send_payload(payload, mode="digest")
            if message_id:
                await self._mark_posted(ids, message_id)
            else:
                await self._mark_failed(ids, self._last_error or "send_skipped")

    async def _enrich(self, event: NewswireEvent) -> None:
        if self.enricher is None or event.enrichment is not None:
            return
        enrichment = await self.enricher.maybe_enrich(event)
        if enrichment:
            event.enrichment = enrichment

    async def _claim(self, event: NewswireEvent, *, mode: str) -> bool:
        repo = self.repository
        now_ms = _now_ms()
        if repo is not None and callable(getattr(repo, "claim_newswire_publish", None)):
            return bool(
                await repo.claim_newswire_publish(
                    event.event_id,
                    self._channel_id,
                    mode,
                    now_ms,
                    metadata={"headline": event.headline, "source": event.source},
                )
            )
        key = f"{self._channel_id}:{event.event_id}"
        if key in self._process_claimed:
            return False
        self._process_claimed.add(key)
        return True

    async def _mark_posted(self, event_ids: list[str], message_id: str | None) -> None:
        self._last_post_at_ms = _now_ms()
        self._posted_count += len(event_ids)
        repo = self.repository
        if repo is not None and callable(getattr(repo, "mark_newswire_publish_posted", None)):
            await repo.mark_newswire_publish_posted(event_ids, self._channel_id, message_id, self._last_post_at_ms)

    async def _mark_failed(self, event_ids: list[str], error: str) -> None:
        repo = self.repository
        if repo is not None and callable(getattr(repo, "mark_newswire_publish_failed", None)):
            await repo.mark_newswire_publish_failed(event_ids, self._channel_id, error, _now_ms())

    async def _send_payload(self, payload: dict[str, Any], *, mode: str) -> str | None:
        if self.alert_sink is None or not self._channel_id:
            self._last_error = "discord_sink_missing"
            NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="skipped").inc()
            return None
        content = str(payload.get("content") or "")[: self.settings.discord_max_response_chars]
        fallback_content = str(payload.get("fallback_content") or content)[: self.settings.discord_max_response_chars]
        embeds = payload.get("embeds") if isinstance(payload.get("embeds"), list) else None
        async with self._send_lock:
            await self._throttle()
            try:
                try:
                    result = await self.alert_sink.send(self._channel_id, content, embeds=embeds)
                except TypeError:
                    # Compatibility with older/fake sinks that accept only content.
                    result = await self.alert_sink.send(self._channel_id, fallback_content)
                NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="ok" if result else "skipped").inc()
                if not result:
                    self._last_error = "discord_send_skipped"
                return result
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self._last_error = type(exc).__name__
                NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="error").inc()
                log.warning("newswire_discord_send_failed", error=type(exc).__name__)
                return None

    async def _throttle(self) -> None:
        min_interval = max(0, self.settings.newswire_send_min_interval_ms)
        now = _now_ms()
        wait_ms = self._last_send_ms + min_interval - now
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        self._last_send_ms = _now_ms()

    def _skip_reason(self, event: NewswireEvent) -> str | None:
        if event.importance_score < self.settings.newswire_news_min_importance:
            return "low_importance"
        if event.freshness == "stale":
            return "stale"
        if self._started_at_ms is not None and event.published_at_ms is not None:
            grace_ms = max(0, self.settings.newswire_discord_startup_grace_seconds) * 1000
            if int(event.published_at_ms) < self._started_at_ms - grace_ms:
                return "startup_backlog"
        return None

    def _skip(self, reason: str) -> None:
        self._skip_counts[reason] += 1
        NEWSWIRE_DISCORD_SKIPS.labels(reason=reason).inc()

    async def send_test_message(self, *, channel_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        target = str(channel_id or self._channel_id)
        payload = {
            "content": "",
            "fallback_content": "🧪 Newswire Discord test — no market event.",
            "embeds": [
                {
                    "title": "🧪 Newswire Discord test",
                    "description": "This verifies the send-only Newswire publisher can post to #news.",
                    "color": 0x3498DB,
                }
            ],
        }
        if dry_run:
            return {"sent": False, "dry_run": True, "channel_id": target, "payload": payload, "publisher": self.status()}
        original_channel = self.settings.newswire_news_channel_id
        try:
            if channel_id:
                self.settings.newswire_news_channel_id = target
            message_id = await self._send_payload(payload, mode="test")
            if message_id:
                self._last_post_at_ms = _now_ms()
            return {"sent": bool(message_id), "channel_id": target, "message_id": message_id, "publisher": self.status()}
        finally:
            self.settings.newswire_news_channel_id = original_channel

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "channel_configured": self.settings.newswire_news_channel_configured,
            "channel_id": self._channel_id if self.settings.newswire_news_channel_configured else None,
            "buffered": len(self._buffer),
            "started_at_ms": self._started_at_ms,
            "last_post_at_ms": self._last_post_at_ms,
            "last_error": self._last_error,
            "posted_count": self._posted_count,
            "skip_counts": dict(self._skip_counts),
            "thresholds": {
                "news_min_importance": self.settings.newswire_news_min_importance,
                "breaking_min_importance": self.settings.newswire_breaking_min_importance,
                "digest_interval_seconds": self.settings.newswire_digest_interval_seconds,
                "digest_max_items": self.settings.newswire_discord_digest_max_items,
                "startup_grace_seconds": self.settings.newswire_discord_startup_grace_seconds,
            },
            "discord": _sink_status(self.alert_sink),
        }

    async def status_async(self) -> dict[str, Any]:
        status = self.status()
        repo = self.repository
        if self._running and repo is not None and callable(getattr(repo, "newswire_publish_status", None)) and self._channel_id:
            status["ledger"] = await repo.newswire_publish_status(self._channel_id)
        return status


def _sink_status(sink: Any | None) -> dict[str, Any]:
    if sink is None:
        return {"configured": False, "ready": False}
    client = getattr(sink, "client", None)
    if client is not None and callable(getattr(client, "status", None)):
        return {"configured": True, **client.status()}
    bot = getattr(sink, "bot", None)
    discord_client = getattr(bot, "client", None)
    ready = bool(discord_client is not None and callable(getattr(discord_client, "is_ready", None)) and discord_client.is_ready())
    return {"configured": True, "ready": ready}


def _event_chunks(events: list[NewswireEvent], size: int) -> list[list[NewswireEvent]]:
    return [events[index : index + size] for index in range(0, len(events), max(1, size))]


def _now_ms() -> int:
    return int(time.time() * 1000)
