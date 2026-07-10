from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.discord_publish import DiscordMessageSink
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.normalize import now_ms

log = get_logger(__name__)


class OperationalNotificationPump:
    """Deliver durable operational notifications through one Discord owner."""

    def __init__(
        self,
        *,
        repository: Repository,
        sink: DiscordMessageSink,
        poll_seconds: float = 1.0,
        batch_size: int = 25,
        max_attempts: int = 8,
    ):
        self.repository = repository
        self.sink = sink
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.batch_size = max(1, min(100, int(batch_size)))
        self.max_attempts = max(1, int(max_attempts))
        self.running = False
        self.processed = 0
        self.failed = 0
        self.last_notification_id: str | None = None
        self.last_sent_at_ms: int | None = None
        self.last_error: str | None = None
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        self.running = True
        try:
            while not self._stop.is_set():
                processed = await self.run_once()
                if processed == 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                    except TimeoutError:
                        continue
        finally:
            self.running = False

    async def run_once(self) -> int:
        claimed = await self.repository.claim_due_operational_notifications(
            destination="discord",
            now_ms=now_ms(),
            limit=self.batch_size,
        )
        for item in claimed:
            notification_id = str(item["notification_id"])
            payload = dict(item.get("payload") or {})
            try:
                content = str(payload.get("content") or "")
                embeds = _optional_dict_list(payload.get("embeds"))
                components = _optional_dict_list(payload.get("components"))
                if not content and not embeds:
                    raise ValueError("notification_payload_empty")
                if len(content) > 2_000:
                    raise ValueError("notification_content_exceeds_discord_limit")
                message_id = await self.sink.send(
                    str(item["channel_id"]),
                    content,
                    embeds=embeds,
                    components=components,
                )
                if not message_id:
                    raise RuntimeError("discord_message_not_sent")
                sent_at_ms = now_ms()
                await self.repository.mark_operational_notification_sent(
                    notification_id,
                    message_id=str(message_id),
                    now_ms=sent_at_ms,
                )
                self.processed += 1
                self.last_notification_id = notification_id
                self.last_sent_at_ms = sent_at_ms
                self.last_error = None
            except Exception as exc:  # pragma: no cover - runtime transport failures vary
                self.failed += 1
                self.last_notification_id = notification_id
                self.last_error = f"{type(exc).__name__}: {exc}"
                await self.repository.mark_operational_notification_failed(
                    notification_id,
                    error=self.last_error,
                    now_ms=now_ms(),
                    max_attempts=self.max_attempts,
                )
                log.warning(
                    "operational_notification_delivery_failed",
                    notification_id=notification_id,
                    category=item.get("category"),
                    error=type(exc).__name__,
                )
        return len(claimed)

    async def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "processed": self.processed,
            "failed": self.failed,
            "last_notification_id": self.last_notification_id,
            "last_sent_at_ms": self.last_sent_at_ms,
            "last_error": self.last_error,
        }


def _optional_dict_list(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    items = [dict(item) for item in value if isinstance(item, dict)]
    return items or None
