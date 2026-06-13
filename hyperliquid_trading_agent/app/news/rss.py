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


def fetch_rss_items_sync(feed_urls: Iterable[str], limit_per_feed: int = 5) -> list[NewsItem]:
    items: list[NewsItem] = []
    for url in feed_urls:
        parsed = feedparser.parse(url)
        for entry in parsed.entries[:limit_per_feed]:
            items.append(
                NewsItem(
                    title=str(getattr(entry, "title", "")),
                    link=str(getattr(entry, "link", "")),
                    source=url,
                    published=str(getattr(entry, "published", "")),
                    summary=_clean_summary(str(getattr(entry, "summary", ""))),
                )
            )
    return items


async def fetch_rss_items(feed_urls: Iterable[str], limit_per_feed: int = 5) -> list[NewsItem]:
    return await asyncio.to_thread(fetch_rss_items_sync, list(feed_urls), limit_per_feed)


def _clean_summary(summary: str) -> str:
    return " ".join(summary.replace("\n", " ").split())[:800]
