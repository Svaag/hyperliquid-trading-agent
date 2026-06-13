from __future__ import annotations

from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings


class XApiClient:
    """Optional X/Twitter current-cycle context client."""

    def __init__(self, settings: Settings, timeout: float = 10.0):
        self.settings = settings
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.settings.x_bearer_token)

    async def search_recent(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                "https://api.x.com/2/tweets/search/recent",
                headers={"Authorization": f"Bearer {self.settings.x_bearer_token}"},
                params={
                    "query": query,
                    "max_results": max(10, min(max_results, 100)),
                    "tweet.fields": "created_at,public_metrics,author_id",
                },
            )
            response.raise_for_status()
            data = response.json()
        return [
            {
                "provider": "x",
                "id": item.get("id"),
                "author_id": item.get("author_id"),
                "created_at": item.get("created_at"),
                "text": item.get("text", ""),
                "public_metrics": item.get("public_metrics", {}),
            }
            for item in data.get("data", [])
        ]
