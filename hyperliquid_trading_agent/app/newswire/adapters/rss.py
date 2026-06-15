from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.news.rss import fetch_rss_items
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.normalize import created_at_ms_from
from hyperliquid_trading_agent.app.newswire.schemas import EventType, RawNewsItem

log = get_logger(__name__)

# (url substring, source, default event_type). Order matters: first match wins.
_FEED_MAP: list[tuple[str, str, EventType]] = [
    ("sec.gov", "sec_edgar", "sec_filing"),
    ("nasdaqtrader.com", "nasdaq_halts", "halt"),
    ("globenewswire.com", "globe_newswire", "press_release"),
    ("businesswire.com", "business_wire", "press_release"),
    ("federalreserve.gov", "federal_reserve", "macro"),
    ("ecb.europa.eu", "ecb", "macro"),
    ("coindesk.com", "coindesk", "headline"),
    ("cointelegraph.com", "cointelegraph", "headline"),
]


def feed_source(url: str) -> tuple[str, EventType | None]:
    lowered = url.lower()
    for needle, source, event_type in _FEED_MAP:
        if needle in lowered:
            return source, event_type
    return "rss", None


class RssAdapter(NewswireAdapter):
    """Polls the RSS reliability layer (filings, halts, press releases, macro, crypto).

    Keyless — works out of the box. Re-emits items each poll; the service's dedupe drops
    repeats, so this stays simple and stateless.
    """

    name = "rss"

    def __init__(self, feed_urls: list[str], *, poll_seconds: int = 60, limit_per_feed: int = 10):
        self.feed_urls = feed_urls
        self.poll_seconds = max(15, poll_seconds)
        self.limit_per_feed = limit_per_feed
        self._stop = asyncio.Event()

    async def run(self, emit: RawEmit) -> None:
        while not self._stop.is_set():
            await self._poll_once(emit)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()

    async def _poll_once(self, emit: RawEmit) -> None:
        items = await fetch_rss_items(self.feed_urls, limit_per_feed=self.limit_per_feed)
        for item in items:
            source, event_type = feed_source(item.source)
            await emit(
                RawNewsItem(
                    source=source,
                    provider=source,
                    transport="rss",
                    external_id=item.link or None,
                    headline=item.title,
                    body=item.summary,
                    url=item.link or None,
                    published_at_ms=created_at_ms_from(item.published),
                    event_type=event_type,
                    raw={"feed": item.source, "published": item.published},
                )
            )

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "feeds": len(self.feed_urls), "poll_seconds": self.poll_seconds}
