from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.news.rss import fetch_rss_items
from hyperliquid_trading_agent.app.news.search import MarketSearch
from hyperliquid_trading_agent.app.news.x_api import XApiClient


@dataclass(frozen=True)
class NewsBundle:
    query: str
    timestamp_ms: int
    rss: list[dict[str, Any]]
    search: list[dict[str, Any]]
    x: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"query": self.query, "timestamp_ms": self.timestamp_ms, "rss": self.rss, "search": self.search, "x": self.x}


class NewsService:
    def __init__(self, settings: Settings, repository: Repository | None = None):
        self.settings = settings
        self.repository = repository
        self.search_client = MarketSearch(settings)
        self.x_client = XApiClient(settings)

    async def current_context(self, query: str, lookback_hours: int = 24, limit: int = 8) -> NewsBundle:
        cache_key = f"news:{query}:{lookback_hours}:{limit}"
        if self.repository:
            cached = await self.repository.cache_get(cache_key)
            if cached:
                return NewsBundle(**cached)
        rss_items = await fetch_rss_items(self.settings.rss_feed_urls, limit_per_feed=max(1, limit // 2))
        rss = [item.to_dict() for item in rss_items[:limit]]
        search_results = await self.search_client.search(query, lookback_hours=lookback_hours, limit=limit)
        try:
            x_results = await self.x_client.search_recent(query, max_results=10) if self.x_client.enabled else []
        except Exception:
            x_results = []
        bundle = NewsBundle(query=query, timestamp_ms=int(time.time() * 1000), rss=rss, search=search_results, x=x_results)
        if self.repository:
            await self.repository.cache_set(cache_key, bundle.to_dict(), self.settings.cache_ttl_news_seconds)
            for item in rss[:limit]:
                await self.repository.record_news_item(
                    source=str(item.get("source", "rss")),
                    title=str(item.get("title", "")),
                    url=str(item.get("link", "")),
                    summary=str(item.get("summary", "")),
                )
        return bundle
