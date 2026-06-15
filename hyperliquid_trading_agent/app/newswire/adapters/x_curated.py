from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.news.x_api import XApiClient
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.normalize import created_at_ms_from
from hyperliquid_trading_agent.app.newswire.schemas import RawNewsItem

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

    @property
    def _source(self) -> str:
        if self.settings.x_watchlist_users:
            return "x_allowlist"
        if self.settings.newswire_cashtag_list:
            return "x_cashtag"
        return "x"

    def build_query(self) -> str:
        parts: list[str] = []
        users = self.settings.x_watchlist_users
        if users:
            parts.append("(" + " OR ".join(f"from:{user}" for user in users) + ")")
        cashtags = self.settings.newswire_cashtag_list
        if cashtags:
            parts.append("(" + " OR ".join(f"${tag}" for tag in cashtags) + ")")
        query = " OR ".join(parts) or " OR ".join(self.settings.newswire_query_terms[:5])
        return f"{query} -is:retweet"

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
        items = await self.x_client.search_recent(self.build_query(), max_results=25)
        floor = self.settings.x_min_public_metric_score
        for item in items:
            metrics = item.get("public_metrics", {})
            if public_metric_score(metrics) < floor:
                continue
            await emit(
                RawNewsItem(
                    source=self._source,
                    provider="x",
                    transport="poll",
                    external_id=str(item.get("id")) if item.get("id") else None,
                    headline=str(item.get("text") or ""),
                    author=item.get("author_id"),
                    published_at_ms=created_at_ms_from(item.get("created_at")),
                    event_type="social",
                    public_metrics=metrics if isinstance(metrics, dict) else {},
                    raw=item,
                )
            )

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.x_client.enabled, "source": self._source}
