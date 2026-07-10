from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.news.rss import RssFetchResult, fetch_rss_feed
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

    def __init__(
        self,
        feed_urls: list[str],
        *,
        poll_seconds: int = 60,
        limit_per_feed: int = 10,
        user_agent: str | None = None,
    ):
        self.feed_urls = feed_urls
        self.poll_seconds = max(15, poll_seconds)
        self.limit_per_feed = limit_per_feed
        self.user_agent = user_agent.strip() if user_agent else None
        self._stop = asyncio.Event()
        self.poll_count = 0
        self.items_emitted = 0
        self.feed_health: dict[str, dict[str, Any]] = {
            _feed_key(url): {
                "ok": None,
                "successes": 0,
                "errors": 0,
                "last_poll_at_ms": None,
                "last_success_at_ms": None,
                "last_error": None,
                "last_item_count": 0,
            }
            for url in feed_urls
        }

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
        self.poll_count += 1
        polled_at_ms = int(time.time() * 1000)
        fetched = await asyncio.gather(
            *(
                fetch_rss_feed(
                    url,
                    limit=self.limit_per_feed,
                    user_agent=self.user_agent,
                )
                for url in self.feed_urls
            ),
            return_exceptions=True,
        )
        successful_feeds = 0
        for url, outcome in zip(self.feed_urls, fetched, strict=True):
            key = _feed_key(url)
            health = self.feed_health.setdefault(key, {})
            health["last_poll_at_ms"] = polled_at_ms
            if isinstance(outcome, BaseException):
                outcome = RssFetchResult(
                    feed_url=url,
                    items=[],
                    ok=False,
                    error=f"fetch_exception:{type(outcome).__name__}",
                )
            health["last_item_count"] = len(outcome.items)
            if not outcome.ok:
                health["ok"] = False
                health["errors"] = int(health.get("errors") or 0) + 1
                health["last_error"] = {
                    "error": outcome.error or "rss_feed_failed",
                    "http_status": outcome.http_status,
                    "at_ms": polled_at_ms,
                }
                log.warning(
                    "newswire_rss_feed_failed",
                    feed=key,
                    error=outcome.error or "rss_feed_failed",
                    http_status=outcome.http_status,
                )
                continue
            successful_feeds += 1
            health["ok"] = True
            health["successes"] = int(health.get("successes") or 0) + 1
            health["last_success_at_ms"] = polled_at_ms
            health["last_error"] = None
            health["warning"] = outcome.warning
            for item in outcome.items:
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
                        raw={"feed": _safe_feed_url(item.source), "published": item.published},
                    )
                )
                self.items_emitted += 1
        if self.feed_urls and successful_feeds == 0:
            raise RuntimeError(f"all_{len(self.feed_urls)}_rss_feeds_failed")

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "feeds": len(self.feed_urls),
            "poll_seconds": self.poll_seconds,
            "poll_count": self.poll_count,
            "items_emitted": self.items_emitted,
            "user_agent_configured": bool(self.user_agent),
            "feed_health": self.feed_health,
        }


def _safe_feed_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _feed_key(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    host = (parsed.hostname or "unknown").lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{host}{port}{path}"
