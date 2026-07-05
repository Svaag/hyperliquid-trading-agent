from __future__ import annotations

import hashlib
import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.prediction_markets.schemas import PredictionMarketQuote


class PredictionMarketCatalog:
    def __init__(self, *, settings: Settings, repository: Any):
        self.settings = settings
        self.repository = repository

    async def search(self, query: str = "", *, venue: str | None = None, limit: int = 10) -> list[PredictionMarketQuote]:
        items = await self._signals(limit=max(100, limit * 8), venue=venue)
        quotes = [_quote_from_signal(item, settings=self.settings) for item in items]
        quotes = [quote for quote in quotes if quote is not None and _fresh_enough(quote, self.settings)]
        quotes = _dedupe_latest(quotes)
        query = query.strip()
        if query:
            quotes = [quote for quote in quotes if _matches_query(quote, query)]
        ranked = sorted(quotes, key=lambda quote: _rank_key(quote, query), reverse=True)
        return ranked[: max(1, limit)]

    async def resolve(self, ref: str) -> PredictionMarketQuote | None:
        needle = ref.strip()
        if not needle:
            return None
        if needle.lower().startswith("pm:"):
            needle = needle[3:]
        for quote in await self.search(limit=250):
            if needle in {quote.quote_id, quote.signal_id}:
                return quote
            if quote.quote_id.startswith(needle) or quote.signal_id.startswith(needle):
                return quote
        return None

    async def _signals(self, *, limit: int, venue: str | None = None) -> list[dict[str, Any]]:
        list_signals = getattr(self.repository, "list_prediction_market_signals", None)
        if not callable(list_signals):
            return []
        return await list_signals(limit=limit, venue=venue)


def quote_id_for(*, venue: str, market_id: str, outcome_id: str | None) -> str:
    digest = hashlib.sha1(f"{venue}:{market_id}:{outcome_id or ''}".encode()).hexdigest()[:10]
    return f"pm_{digest}"


def _quote_from_signal(signal: dict[str, Any], *, settings: Settings) -> PredictionMarketQuote | None:
    venue = str(signal.get("venue") or "unknown").lower()
    status = str(signal.get("status") or "unknown").lower()
    if status not in {"open", "unknown"}:
        return None
    price = _buy_price(signal)
    if price is None or price <= 0 or price > 1:
        return None
    market_id = str(signal.get("market_id") or "")
    if not market_id:
        return None
    outcome_id = str(signal.get("outcome_id")) if signal.get("outcome_id") is not None else None
    now_ms = int(time.time() * 1000)
    as_of_ms = int(signal.get("as_of_ms") or 0)
    staleness_ms = int(signal.get("staleness_ms") or max(0, now_ms - as_of_ms)) if as_of_ms else None
    return PredictionMarketQuote(
        quote_id=quote_id_for(venue=venue, market_id=market_id, outcome_id=outcome_id),
        signal_id=str(signal.get("signal_id") or ""),
        venue=venue,
        market_id=market_id,
        question=str(signal.get("question") or ""),
        outcome_id=outcome_id,
        outcome_name=str(signal.get("outcome_name") or ""),
        side="yes",
        implied_probability=_optional_float(signal.get("implied_probability")),
        best_bid=_optional_float(signal.get("best_bid")),
        best_ask=_optional_float(signal.get("best_ask")),
        price=price,
        liquidity_usd=_optional_float(signal.get("liquidity_usd")),
        volume_usd=_optional_float(signal.get("volume_usd")),
        status=status,
        as_of_ms=as_of_ms,
        staleness_ms=staleness_ms,
        symbols=[str(item) for item in signal.get("symbols") or []],
        topics=[str(item) for item in signal.get("topics") or []],
        metadata={"source_signal": signal.get("metadata") or {}, "search_max_staleness_seconds": settings.prediction_market_search_max_staleness_seconds},
    )


def _buy_price(signal: dict[str, Any]) -> float | None:
    return _optional_float(signal.get("best_ask")) or _optional_float(signal.get("implied_probability")) or _optional_float(signal.get("best_bid"))


def _fresh_enough(quote: PredictionMarketQuote, settings: Settings) -> bool:
    if quote.staleness_ms is None:
        return True
    return quote.staleness_ms <= max(1, settings.prediction_market_search_max_staleness_seconds) * 1000


def _dedupe_latest(quotes: list[PredictionMarketQuote]) -> list[PredictionMarketQuote]:
    by_key: dict[tuple[str, str, str | None], PredictionMarketQuote] = {}
    for quote in quotes:
        key = (quote.venue, quote.market_id, quote.outcome_id)
        current = by_key.get(key)
        if current is None or quote.as_of_ms > current.as_of_ms:
            by_key[key] = quote
    return list(by_key.values())


def _matches_query(quote: PredictionMarketQuote, query: str) -> bool:
    haystack = " ".join([quote.question, quote.outcome_name, quote.venue, quote.market_id, " ".join(quote.symbols), " ".join(quote.topics)]).lower()
    tokens = [token for token in query.lower().replace("$", " ").split() if token]
    return all(token in haystack for token in tokens) or any(token in haystack for token in tokens[:3])


def _rank_key(quote: PredictionMarketQuote, query: str) -> tuple[float, int, float, int]:
    text = f"{quote.question} {quote.outcome_name} {' '.join(quote.symbols)} {' '.join(quote.topics)}".lower()
    tokens = [token for token in query.lower().split() if token]
    relevance = sum(1.0 for token in tokens if token in text)
    venue_priority = 2 if quote.venue == "hip4" else 1
    liquidity = float(quote.liquidity_usd or 0.0)
    return (relevance, venue_priority, liquidity, quote.as_of_ms)


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
