from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SearchProviderStatus:
    name: str
    enabled: bool


class MarketSearch:
    """Optional live search facade for Tavily, SerpAPI, NewsAPI, and Perplexity."""

    def __init__(self, settings: Settings, timeout: float = 10.0):
        self.settings = settings
        self.timeout = timeout

    def providers(self) -> list[SearchProviderStatus]:
        return [
            SearchProviderStatus("tavily", bool(self.settings.tavily_api_key)),
            SearchProviderStatus("serpapi", bool(self.settings.serpapi_api_key)),
            SearchProviderStatus("newsapi", bool(self.settings.newsapi_api_key)),
            SearchProviderStatus("perplexity", bool(self.settings.perplexity_api_key)),
        ]

    async def search(self, query: str, lookback_hours: int = 24, limit: int = 6) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for provider in self.providers():
                if not provider.enabled:
                    continue
                try:
                    if provider.name == "tavily":
                        results.extend(await self._tavily(client, query, limit))
                    elif provider.name == "serpapi":
                        results.extend(await self._serpapi(client, query, limit))
                    elif provider.name == "newsapi":
                        results.extend(await self._newsapi(client, query, limit))
                    elif provider.name == "perplexity":
                        results.extend(await self._perplexity(client, query, limit))
                except Exception as exc:  # pragma: no cover - provider behavior depends on external service
                    log.warning("market_search_provider_failed", provider=provider.name, error=type(exc).__name__)
        return _dedupe(results)[:limit]

    async def _tavily(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
        response = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": self.settings.tavily_api_key, "query": query, "max_results": limit, "topic": "news"},
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"provider": "tavily", "title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")}
            for item in data.get("results", [])
        ]

    async def _serpapi(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
        response = await client.get(
            "https://serpapi.com/search.json",
            params={"engine": "google_news", "q": query, "api_key": self.settings.serpapi_api_key, "num": limit},
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"provider": "serpapi", "title": item.get("title", ""), "url": item.get("link", ""), "snippet": item.get("snippet", "")}
            for item in data.get("news_results", [])
        ]

    async def _newsapi(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
        response = await client.get(
            "https://newsapi.org/v2/everything",
            params={"q": query, "pageSize": limit, "sortBy": "publishedAt", "apiKey": self.settings.newsapi_api_key},
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"provider": "newsapi", "title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("description", "")}
            for item in data.get("articles", [])
        ]

    async def _perplexity(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
        response = await client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.perplexity_api_key}"},
            json={
                "model": "sonar-pro",
                "messages": [
                    {"role": "system", "content": "Return concise current market/news context with URLs where available."},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 500,
            },
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return [{"provider": "perplexity", "title": "Perplexity market context", "url": "", "snippet": content[:1500]}]


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("url") or item.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
