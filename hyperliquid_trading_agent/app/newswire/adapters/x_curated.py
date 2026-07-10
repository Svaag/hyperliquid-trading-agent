from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.news.x_api import XApiClient
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.normalize import created_at_ms_from
from hyperliquid_trading_agent.app.newswire.schemas import Action, RawNewsItem

log = get_logger(__name__)


def public_metric_score(metrics: Any) -> float:
    if not isinstance(metrics, dict):
        return 0.0
    return (
        float(metrics.get("like_count", 0))
        + float(metrics.get("retweet_count", 0)) * 2
        + float(metrics.get("reply_count", 0)) * 1.5
        + float(metrics.get("quote_count", 0)) * 2
    )


class XCuratedAdapter(NewswireAdapter):
    """Curated X feed: author allowlist OR tracked cashtags, gated by an engagement floor.

    Polls the recent-search endpoint; high signal-to-noise by construction.
    """

    name = "x_curated"

    def __init__(self, *, settings: Settings, x_client: XApiClient | None = None):
        self.settings = settings
        self.x_client = x_client or XApiClient(settings)
        self._stop = asyncio.Event()
        self._poll_seconds = max(10, settings.x_poll_seconds)
        self._seen_revisions: dict[str, str] = {}
        self.poll_count = 0
        self.items_emitted = 0
        self.duplicates_dropped = 0
        self.updates_emitted = 0
        self.last_event_at_ms: int | None = None

    @property
    def _source(self) -> str:
        if self.settings.x_watchlist_users:
            return "x_allowlist"
        if self.settings.newswire_cashtag_list:
            return "x_cashtag"
        return "x"

    def build_query(self) -> str:
        atoms: list[str] = []
        for value in self.settings.x_watchlist_users:
            username = value.strip().lstrip("@")
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
                atoms.append(f"from:{username}")
        for value in self.settings.newswire_cashtag_list:
            cashtag = value.strip().lstrip("$").upper()
            if re.fullmatch(r"[A-Z0-9._-]{1,20}", cashtag):
                atoms.append(f"${cashtag}")
        if not atoms:
            atoms = [value.strip() for value in self.settings.newswire_query_terms[:5] if value.strip()]
        if not atoms:
            atoms = ["crypto"]
        while len(atoms) > 1 and len(f"({' OR '.join(atoms)}) -is:retweet") > 512:
            atoms.pop()
        query = f"({' OR '.join(atoms)}) -is:retweet"
        if len(query) > 512:
            raise ValueError("x_curated_query_exceeds_512_characters")
        return query

    async def run(self, emit: RawEmit) -> None:
        if not self.x_client.enabled:
            return
        while not self._stop.is_set():
            await self._poll(emit)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()

    async def _poll(self, emit: RawEmit) -> None:
        self.poll_count += 1
        items = await self.x_client.search_recent(self.build_query(), max_results=25)
        floor = self.settings.x_min_public_metric_score
        for item in items:
            metrics = item.get("public_metrics", {})
            if public_metric_score(metrics) < floor:
                continue
            revision_id = str(item.get("id") or "")
            external_id = str(item.get("canonical_id") or revision_id)
            if not revision_id or not external_id:
                continue
            previous_revision = self._seen_revisions.get(external_id)
            if previous_revision == revision_id:
                self.duplicates_dropped += 1
                continue
            action: Action = "updated" if previous_revision is not None else "created"
            self._seen_revisions[external_id] = revision_id
            if action == "updated":
                self.updates_emitted += 1
            await emit(
                RawNewsItem(
                    source=self._source,
                    provider="x",
                    transport="poll",
                    external_id=external_id,
                    action=action,
                    headline=str(item.get("text") or ""),
                    author=item.get("author_username") or item.get("author_id"),
                    published_at_ms=created_at_ms_from(item.get("created_at")),
                    event_type="social",
                    public_metrics=metrics if isinstance(metrics, dict) else {},
                    raw=item,
                )
            )
            self.items_emitted += 1
            self.last_event_at_ms = int(time.time() * 1000)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.x_client.enabled,
            "source": self._source,
            "poll_count": self.poll_count,
            "items_emitted": self.items_emitted,
            "duplicates_dropped": self.duplicates_dropped,
            "updates_emitted": self.updates_emitted,
            "last_event_at_ms": self.last_event_at_ms,
            "query_length": len(self.build_query()),
            "update_semantics": "canonical_initial_post_id_with_revision_updates",
            "delete_semantics": "not_available_from_recent_search; requires X compliance stream",
        }
