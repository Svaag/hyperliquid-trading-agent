from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Iterable

import feedparser


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    source: str
    published: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RssFetchResult:
    feed_url: str
    items: list[NewsItem]
    ok: bool
    http_status: int | None = None
    error: str | None = None
    warning: str | None = None


def fetch_rss_feed_sync(feed_url: str, limit: int = 5) -> RssFetchResult:
    """Fetch one feed without allowing it to poison the remaining RSS layer."""

    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:  # pragma: no cover - feedparser/network boundary
        return RssFetchResult(
            feed_url=feed_url,
            items=[],
            ok=False,
            error=f"fetch_exception:{type(exc).__name__}",
        )

    status_value = getattr(parsed, "status", None)
    try:
        status = int(status_value) if status_value is not None else None
    except (TypeError, ValueError):
        status = None
    entries = list(getattr(parsed, "entries", []) or [])
    items = [
        NewsItem(
            title=str(getattr(entry, "title", "")),
            link=str(getattr(entry, "link", "")),
            source=feed_url,
            published=str(getattr(entry, "published", getattr(entry, "updated", ""))),
            summary=_clean_summary(str(getattr(entry, "summary", getattr(entry, "description", "")))),
        )
        for entry in entries[:limit]
    ]
    bozo = bool(getattr(parsed, "bozo", False))
    bozo_exception = getattr(parsed, "bozo_exception", None)
    warning = f"parse_warning:{type(bozo_exception).__name__}" if bozo and bozo_exception else None
    if status is not None and status >= 400:
        return RssFetchResult(
            feed_url=feed_url,
            items=items,
            ok=False,
            http_status=status,
            error=f"http_status:{status}",
            warning=warning,
        )
    if bozo and not entries:
        return RssFetchResult(
            feed_url=feed_url,
            items=[],
            ok=False,
            http_status=status,
            error=warning or "parse_error",
        )
    return RssFetchResult(
        feed_url=feed_url,
        items=items,
        ok=True,
        http_status=status,
        warning=warning,
    )


def fetch_rss_items_sync(feed_urls: Iterable[str], limit_per_feed: int = 5) -> list[NewsItem]:
    items: list[NewsItem] = []
    for url in feed_urls:
        items.extend(fetch_rss_feed_sync(url, limit=limit_per_feed).items)
    return items


async def fetch_rss_items(feed_urls: Iterable[str], limit_per_feed: int = 5) -> list[NewsItem]:
    return await asyncio.to_thread(fetch_rss_items_sync, list(feed_urls), limit_per_feed)


async def fetch_rss_feed(feed_url: str, limit: int = 5) -> RssFetchResult:
    return await asyncio.to_thread(fetch_rss_feed_sync, feed_url, limit)


def _clean_summary(summary: str) -> str:
    return " ".join(summary.replace("\n", " ").split())[:800]
