from __future__ import annotations

from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings


def parse_recent_search_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map X API recent-search JSON while retaining edit identity and usernames."""

    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    users = includes.get("users") if isinstance(includes, dict) else []
    usernames = {
        str(item.get("id")): str(item.get("username"))
        for item in users or []
        if isinstance(item, dict) and item.get("id") and item.get("username")
    }
    parsed: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        author_id = str(item.get("author_id") or "") or None
        edit_history = [str(value) for value in item.get("edit_history_tweet_ids") or [] if value]
        post_id = str(item.get("id") or "") or None
        if post_id and not edit_history:
            edit_history = [post_id]
        parsed.append(
            {
                "provider": "x",
                "id": post_id,
                "canonical_id": edit_history[0] if edit_history else post_id,
                "edit_history_tweet_ids": edit_history,
                "author_id": author_id,
                "author_username": usernames.get(author_id or ""),
                "created_at": item.get("created_at"),
                "text": item.get("text", ""),
                "public_metrics": item.get("public_metrics", {}),
            }
        )
    return parsed


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
                    "expansions": "author_id",
                    "tweet.fields": "created_at,public_metrics,author_id,entities,edit_history_tweet_ids",
                    "user.fields": "verified,public_metrics,username",
                },
            )
            response.raise_for_status()
            data = response.json()
        return parse_recent_search_payload(data)
