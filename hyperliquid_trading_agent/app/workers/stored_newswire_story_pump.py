from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireStoryRevision

log = get_logger(__name__)

StoryHandler = Callable[[NewswireEvent], Awaitable[None] | None]
SOURCE_TABLE = "newswire_story_revisions"


class StoredNewswireStoryPump:
    """Deliver append-only canonical story revisions to one durable consumer."""

    def __init__(
        self,
        *,
        consumer_name: str,
        repository: Repository,
        callbacks: list[StoryHandler],
        poll_seconds: float = 1.0,
        batch_size: int = 100,
        bootstrap_from_latest: bool = False,
        bootstrap_metadata: dict[str, Any] | None = None,
    ):
        self.consumer_name = consumer_name
        self.repository = repository
        self.callbacks = callbacks
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.batch_size = max(1, int(batch_size))
        self.bootstrap_from_latest = bool(bootstrap_from_latest)
        self.bootstrap_metadata = bootstrap_metadata or {}
        self.running = False
        self.processed = 0
        self.error_count = 0
        self.invalid_rows_skipped = 0
        self.bootstrapped_from_latest = False
        self.last_revision_id: str | None = None
        self.last_story_id: str | None = None
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
        offset = await self.repository.get_consumer_offset(self.consumer_name, source_table=SOURCE_TABLE)
        if str(offset.get("source_table") or SOURCE_TABLE) != SOURCE_TABLE:
            offset = {
                "consumer_name": self.consumer_name,
                "source_table": SOURCE_TABLE,
                "last_event_id": None,
                "last_event_ts_ms": 0,
                "updated_at_ms": 0,
                "metadata": {},
            }
        if await self._maybe_bootstrap_from_latest(offset):
            return 0
        rows = await self.repository.list_newswire_story_revisions_after(
            last_event_ts_ms=int(offset.get("last_event_ts_ms") or 0),
            last_event_id=offset.get("last_event_id"),
            limit=self.batch_size,
        )
        count = 0
        for row in rows:
            try:
                revision = NewswireStoryRevision.model_validate(row)
            except Exception as exc:
                await self._skip_invalid_row(row, exc)
                count += 1
                continue
            event = revision.story.to_event(update_type=revision.update_type)
            try:
                for callback in self.callbacks:
                    result = callback(event)
                    if result is not None:
                        await result
                await self.repository.update_consumer_offset(
                    self.consumer_name,
                    source_table=SOURCE_TABLE,
                    last_event_id=revision.revision_id,
                    last_event_ts_ms=revision.emitted_at_ms,
                    metadata={
                        "last_story_id": revision.story_id,
                        "last_story_revision": revision.revision,
                        "last_update_type": revision.update_type,
                    },
                )
                self.processed += 1
                count += 1
                self.last_revision_id = revision.revision_id
                self.last_story_id = revision.story_id
            except Exception as exc:  # pragma: no cover - worker safety net
                self.error_count += 1
                self.last_error = type(exc).__name__
                log.warning(
                    "stored_newswire_story_pump_event_failed",
                    consumer=self.consumer_name,
                    story_id=revision.story_id,
                    revision=revision.revision,
                    error=type(exc).__name__,
                )
                break
        return count

    async def _maybe_bootstrap_from_latest(self, offset: dict[str, Any]) -> bool:
        if not self.bootstrap_from_latest or self.bootstrapped_from_latest:
            return False
        has_offset = bool(offset.get("last_event_id")) or int(offset.get("last_event_ts_ms") or 0) > 0
        if has_offset:
            self.bootstrapped_from_latest = True
            return False
        rows = await self.repository.list_newswire_story_revisions(limit=1)
        if not rows:
            return False
        row = rows[0]
        revision_id = str(row.get("revision_id") or "")
        emitted_at_ms = int(row.get("emitted_at_ms") or 0)
        if not revision_id or emitted_at_ms <= 0:
            return False
        await self.repository.update_consumer_offset(
            self.consumer_name,
            source_table=SOURCE_TABLE,
            last_event_id=revision_id,
            last_event_ts_ms=emitted_at_ms,
            metadata={
                **self.bootstrap_metadata,
                "bootstrap_from_latest": True,
                "reason": "avoid_historical_story_replay",
                "last_story_id": row.get("story_id"),
            },
        )
        self.bootstrapped_from_latest = True
        self.last_revision_id = revision_id
        self.last_story_id = str(row.get("story_id") or "") or None
        return True

    async def _skip_invalid_row(self, row: dict[str, Any], exc: Exception) -> None:
        revision_id = str(row.get("revision_id") or "")
        emitted_at_ms = int(row.get("emitted_at_ms") or 0)
        self.error_count += 1
        self.invalid_rows_skipped += 1
        self.last_error = type(exc).__name__
        if revision_id and emitted_at_ms > 0:
            await self.repository.update_consumer_offset(
                self.consumer_name,
                source_table=SOURCE_TABLE,
                last_event_id=revision_id,
                last_event_ts_ms=emitted_at_ms,
                metadata={"invalid_row_skipped": True, "error": type(exc).__name__, "story_id": row.get("story_id")},
            )
            self.last_revision_id = revision_id
        log.warning(
            "stored_newswire_story_pump_invalid_row",
            consumer=self.consumer_name,
            revision_id=revision_id or None,
            error=type(exc).__name__,
        )

    async def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "consumer_name": self.consumer_name,
            "source_table": SOURCE_TABLE,
            "running": self.running,
            "processed": self.processed,
            "last_revision_id": self.last_revision_id,
            "last_story_id": self.last_story_id,
            "error_count": self.error_count,
            "invalid_rows_skipped": self.invalid_rows_skipped,
            "bootstrap_from_latest": self.bootstrap_from_latest,
            "bootstrapped_from_latest": self.bootstrapped_from_latest,
            "last_error": self.last_error,
        }
