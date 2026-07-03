from __future__ import annotations

import hashlib
import html
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from hyperliquid_trading_agent.app.autonomy.newswire import score_importance, score_sentiment, tag_assets
from hyperliquid_trading_agent.app.newswire.classify import (
    classify_asset_class,
    classify_event_type,
    classify_urgency,
    source_score,
)
from hyperliquid_trading_agent.app.newswire.schemas import Freshness, NewswireEvent, RawNewsItem, Tradability

_BREAKING_MS = 30 * 60 * 1000
_FRESH_MS = 24 * 60 * 60 * 1000


def normalize(raw: RawNewsItem, *, symbols_universe: list[str], received_at_ms: int | None = None) -> NewswireEvent | None:
    """Deterministically turn a raw adapter item into a canonical ``NewswireEvent``.

    Returns ``None`` for empty items. No LLM is involved here by design.
    """
    headline = _clean_text(raw.headline or raw.body or "")
    body = _clean_text(raw.body or "")
    if not headline and not body:
        return None
    received = received_at_ms or now_ms()
    text = f"{raw.query or ''} {headline} {body}"

    symbols = [s.upper() for s in raw.symbols] if raw.symbols else tag_assets(text, symbols_universe)
    asset_class = classify_asset_class(raw.source, text, symbols, hint=raw.asset_class)
    event_type = classify_event_type(raw.source, text, hint=raw.event_type)
    importance = score_importance(headline, body, raw.query or "", raw.public_metrics)
    sentiment = score_sentiment(f"{headline} {body}")
    freshness = compute_freshness(received, raw.published_at_ms)
    score = source_score(raw.source)
    urgency = classify_urgency(raw.source, raw.transport, event_type, importance, text)

    return NewswireEvent(
        event_id=event_id(raw),
        source=raw.source,
        provider=raw.provider or raw.source,
        transport=raw.transport,
        received_at_ms=received,
        published_at_ms=raw.published_at_ms,
        updated_at_ms=received if raw.action in {"updated", "removed"} else None,
        action=raw.action,
        headline=headline[:500],
        body=body[:4000],
        url=raw.url,
        author=raw.author,
        symbols=symbols,
        asset_class=asset_class,
        event_type=event_type,
        urgency=urgency,
        importance_score=importance,
        sentiment=sentiment,
        freshness=freshness,
        confidence=_confidence(raw, score),
        source_score=score,
        tradability=Tradability(),
        metadata={"query": raw.query, "raw": raw.raw} if raw.raw or raw.query else {},
    )


def event_id(raw: RawNewsItem) -> str:
    """Stable id so update/delete actions correlate with the original created event."""
    if raw.external_id:
        key = f"{raw.source}:{raw.provider or raw.source}:{raw.external_id}"
    elif raw.url:
        key = raw.url
    else:
        key = f"{raw.source}:{raw.headline}:{raw.body[:120]}"
    return "nw_" + hashlib.sha1(key.encode()).hexdigest()[:24]


def compute_freshness(received_at_ms: int, published_at_ms: int | None) -> Freshness:
    if published_at_ms is None:
        return "fresh"
    age = max(0, received_at_ms - published_at_ms)
    if age <= _BREAKING_MS:
        return "breaking"
    if age <= _FRESH_MS:
        return "fresh"
    return "stale"


def created_at_ms_from(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        pass
    try:  # RFC 822 (common in RSS, e.g. "Mon, 15 Jun 2026 12:00:00 GMT")
        return int(parsedate_to_datetime(text).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


def _clean_text(value: Any) -> str:
    return html.unescape(" ".join(str(value or "").split()))


def _confidence(raw: RawNewsItem, score: float) -> float:
    # Higher when the adapter supplied an explicit type and the source is trusted.
    base = 0.4 + 0.4 * score
    if raw.event_type is not None:
        base += 0.1
    if raw.symbols:
        base += 0.1
    return max(0.0, min(1.0, base))
