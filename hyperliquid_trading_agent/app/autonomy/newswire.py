from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any, Literal

from hyperliquid_trading_agent.app.autonomy.schemas import NewsEvent
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.news.service import NewsService
from hyperliquid_trading_agent.app.newswire.keyword_matcher import score_importance_details

BULLISH_WORDS = {"approval", "approved", "partnership", "surge", "rally", "inflow", "record", "breakout", "buyback", "listing"}
BEARISH_WORDS = {"hack", "exploit", "lawsuit", "selloff", "outflow", "liquidation", "ban", "downgrade", "default", "rejection"}


class AutonomyNewswire:
    """Poll and score news/X events for the autonomy loop."""

    def __init__(self, settings: Settings, news_service: NewsService):
        self.settings = settings
        self.news_service = news_service
        self._seen: set[str] = set()
        self.latest_events: list[NewsEvent] = []

    async def poll(self, symbols: list[str], limit_per_query: int = 6) -> list[NewsEvent]:
        if not self.settings.newswire_enabled:
            return []
        events: list[NewsEvent] = []
        query_terms = self.settings.newswire_query_terms or symbols[:5]
        for query in query_terms[:12]:
            try:
                bundle = await self.news_service.current_context(query, limit=limit_per_query)
            except Exception:
                continue
            for item in bundle.rss:
                event = self._event_from_item(item, provider="rss", query=query, symbols=symbols, observed_at_ms=bundle.timestamp_ms)
                if event is not None:
                    events.append(event)
            for item in bundle.search:
                event = self._event_from_item(item, provider=str(item.get("provider") or "search"), query=query, symbols=symbols, observed_at_ms=bundle.timestamp_ms)
                if event is not None:
                    events.append(event)
            for item in bundle.x:
                metric_score = _public_metric_score(item.get("public_metrics", {}))
                if metric_score < self.settings.x_min_public_metric_score:
                    continue
                event = self._event_from_item(item, provider="x", query=query, symbols=symbols, observed_at_ms=bundle.timestamp_ms)
                if event is not None:
                    events.append(event)
        deduped = self._dedupe(events)
        self.latest_events = sorted([*deduped, *self.latest_events], key=lambda item: item.observed_at_ms, reverse=True)[:200]
        return deduped

    def _event_from_item(self, item: dict[str, Any], *, provider: str, query: str, symbols: list[str], observed_at_ms: int) -> NewsEvent | None:
        title = str(item.get("title") or item.get("text") or "").strip()
        text = str(item.get("summary") or item.get("snippet") or item.get("text") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip() or None
        if not title and not text:
            return None
        event_id = _event_id(provider, url, title, text)
        assets = tag_assets(f"{query} {title} {text}", symbols)
        importance = score_importance(title, text, query, item.get("public_metrics", {}))
        return NewsEvent(
            id=event_id,
            source=str(item.get("source") or provider),
            provider=provider,
            title=title[:500],
            text=text[:2000],
            url=url,
            author_id=str(item.get("author_id")) if item.get("author_id") else None,
            created_at_ms=_created_at_ms(item.get("created_at") or item.get("published_at")),
            observed_at_ms=observed_at_ms,
            assets=assets,
            importance_score=importance,
            sentiment=score_sentiment(f"{title} {text}"),
            freshness=_freshness(observed_at_ms, _created_at_ms(item.get("created_at") or item.get("published_at"))),
            metadata={"query": query, "raw": item},
        )

    def _dedupe(self, events: list[NewsEvent]) -> list[NewsEvent]:
        out: list[NewsEvent] = []
        for event in events:
            if event.id in self._seen:
                continue
            self._seen.add(event.id)
            out.append(event)
        return out


def tag_assets(text: str, symbols: list[str]) -> list[str]:
    upper = text.upper()
    tagged = []
    for symbol in symbols:
        token = symbol.upper()
        if token and (f" {token} " in f" {upper} " or f"${token}" in upper):
            tagged.append(token)
    if "HYPERLIQUID" in upper and "HYPE" in {symbol.upper() for symbol in symbols}:
        tagged.append("HYPE")
    return sorted(set(tagged))


def score_importance(title: str, text: str, query: str = "", public_metrics: Any = None) -> float:
    return score_importance_details(title, text, query, public_metrics).score


def score_sentiment(text: str) -> Literal["bullish", "bearish", "mixed", "unknown"]:
    lowered = text.lower()
    bullish = sum(1 for word in BULLISH_WORDS if word in lowered)
    bearish = sum(1 for word in BEARISH_WORDS if word in lowered)
    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    if bullish or bearish:
        return "mixed"
    return "unknown"


def _public_metric_score(metrics: Any) -> float:
    if not isinstance(metrics, dict):
        return 0.0
    return float(metrics.get("like_count", 0)) + float(metrics.get("retweet_count", 0)) * 2 + float(metrics.get("reply_count", 0)) * 1.5 + float(metrics.get("quote_count", 0)) * 2


def _event_id(provider: str, url: str | None, title: str, text: str) -> str:
    key = url or f"{provider}:{title}:{text[:120]}"
    return "news_" + hashlib.sha1(key.encode()).hexdigest()[:24]


def _created_at_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _freshness(observed_at_ms: int, created_at_ms: int | None) -> Literal["breaking", "fresh", "stale"]:
    if created_at_ms is None:
        return "fresh"
    age_ms = max(0, observed_at_ms - created_at_ms)
    if age_ms <= 30 * 60 * 1000:
        return "breaking"
    if age_ms <= 24 * 60 * 60 * 1000:
        return "fresh"
    return "stale"


def observed_now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000 if datetime.now(UTC) else time.time() * 1000)
