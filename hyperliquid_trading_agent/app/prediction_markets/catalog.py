from __future__ import annotations

import hashlib
import re
import time
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.ids import coin, parse_coin
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.registry import parse_outcomes
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.prediction_markets.schemas import PredictionMarketQuote

log = get_logger(__name__)

_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "against",
    "beating",
    "beat",
    "beats",
    "bet",
    "buy",
    "defeat",
    "defeating",
    "defeats",
    "for",
    "in",
    "market",
    "no",
    "of",
    "on",
    "or",
    "paper",
    "pm",
    "predict",
    "prediction",
    "the",
    "to",
    "versus",
    "vs",
    "will",
    "win",
    "winning",
    "wins",
    "yes",
}


class PredictionMarketCatalog:
    def __init__(self, *, settings: Settings, repository: Any, hyperliquid: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid

    async def search(self, query: str = "", *, venue: str | None = None, limit: int = 10) -> list[PredictionMarketQuote]:
        items = await self._signals(limit=max(100, limit * 8), venue=venue)
        quotes = [_quote_from_signal(item, settings=self.settings) for item in items]
        quotes = [quote for quote in quotes if quote is not None and _fresh_enough(quote, self.settings)]
        if venue is None or venue == "hip4":
            quotes.extend(await self._live_hip4_quotes(query=query, limit=max(limit * 2, 8)))
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
        live_ref = await self._resolve_live_hip4_ref(needle)
        if live_ref is not None:
            return live_ref
        for quote in await self.search(limit=250):
            if _quote_matches_ref(quote, needle):
                return quote
        return None

    async def _signals(self, *, limit: int, venue: str | None = None) -> list[dict[str, Any]]:
        list_signals = getattr(self.repository, "list_prediction_market_signals", None)
        if not callable(list_signals):
            return []
        return await list_signals(limit=limit, venue=venue)

    async def _live_hip4_quotes(self, *, query: str, limit: int) -> list[PredictionMarketQuote]:
        if self.hyperliquid is None:
            return []
        tokens = _query_terms(query)
        if not tokens:
            return []
        try:
            payload = await self.hyperliquid.outcome_meta()
        except Exception as exc:  # pragma: no cover - external API behavior
            log.warning("prediction_market_hip4_outcome_meta_failed", error=type(exc).__name__)
            return []
        outcomes = parse_outcomes(payload if isinstance(payload, dict) else {})
        matches = [item for item in outcomes if not item.settled and _outcome_matches_terms(item, tokens)]
        quotes: list[PredictionMarketQuote] = []
        for outcome in matches[: max(1, limit)]:
            quotes.extend(await self._quotes_for_hip4_outcome(outcome))
        return quotes

    async def _resolve_live_hip4_ref(self, ref: str) -> PredictionMarketQuote | None:
        parsed = _parse_hip4_ref(ref)
        if parsed is None or self.hyperliquid is None:
            return None
        outcome_id, side = parsed
        try:
            payload = await self.hyperliquid.outcome_meta()
        except Exception as exc:  # pragma: no cover
            log.warning("prediction_market_hip4_ref_outcome_meta_failed", error=type(exc).__name__)
            payload = {}
        outcomes = {item.outcome_id: item for item in parse_outcomes(payload if isinstance(payload, dict) else {})}
        outcome = outcomes.get(outcome_id)
        if outcome is None:
            outcome = _SyntheticOutcome(outcome_id=outcome_id)
        quotes = await self._quotes_for_hip4_outcome(outcome, sides=[side] if side is not None else [0])
        return quotes[0] if quotes else None

    async def _quotes_for_hip4_outcome(self, outcome: Any, *, sides: list[int] | None = None) -> list[PredictionMarketQuote]:
        if self.hyperliquid is None:
            return []
        quotes: list[PredictionMarketQuote] = []
        for side in sides or [0, 1]:
            try:
                book = parse_l2_book(coin(int(outcome.outcome_id), side), await self.hyperliquid.l2_book(coin(int(outcome.outcome_id), side)), source="rest")
            except Exception as exc:  # pragma: no cover - external API behavior
                log.warning("prediction_market_hip4_l2_book_failed", outcome_id=getattr(outcome, "outcome_id", None), side=side, error=type(exc).__name__)
                continue
            quote = _quote_from_hip4_book(outcome, side=side, book=book, settings=self.settings)
            if quote is not None and _fresh_enough(quote, self.settings):
                quotes.append(quote)
        return quotes


class _SyntheticOutcome:
    def __init__(self, *, outcome_id: int):
        self.outcome_id = outcome_id
        self.name = f"HIP-4 outcome {outcome_id}"
        self.description = ""
        self.side0_name = "Side 0"
        self.side1_name = "Side 1"
        self.settled = False
        self.raw = {}


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


def _quote_from_hip4_book(outcome: Any, *, side: int, book: Any, settings: Settings) -> PredictionMarketQuote | None:
    data = book.model_dump(mode="json") if hasattr(book, "model_dump") else dict(book or {})
    best_bid = _top_price(data.get("bids"))
    best_ask = _top_price(data.get("asks"))
    implied = _mid_price(best_bid, best_ask)
    price = best_ask if best_ask is not None else implied if implied is not None else best_bid
    if price is None or price <= 0 or price > 1:
        return None
    outcome_id = int(getattr(outcome, "outcome_id"))
    now_ms = int(time.time() * 1000)
    as_of_ms = int(data.get("as_of_ms") or now_ms)
    side_name = str(getattr(outcome, "side0_name", "Side 0") if side == 0 else getattr(outcome, "side1_name", "Side 1"))
    question = str(getattr(outcome, "name", "") or f"HIP-4 outcome {outcome_id}")
    return PredictionMarketQuote(
        quote_id=quote_id_for(venue="hip4", market_id=str(outcome_id), outcome_id=f"{outcome_id}:{side}"),
        signal_id=f"pm_hip4_live_{outcome_id}_{side}_{as_of_ms}",
        venue="hip4",
        market_id=str(outcome_id),
        question=question,
        outcome_id=f"{outcome_id}:{side}",
        outcome_name=side_name,
        side="yes",
        implied_probability=implied,
        best_bid=best_bid,
        best_ask=best_ask,
        price=price,
        liquidity_usd=_levels_liquidity(data),
        volume_usd=None,
        status="open",
        as_of_ms=as_of_ms,
        staleness_ms=max(0, now_ms - as_of_ms),
        symbols=[],
        topics=_hip4_topics(outcome),
        metadata={
            "source_signal": {
                "source": "hyperliquid_info",
                "adapter": "hip4_live",
                "coin": data.get("coin") or coin(outcome_id, side),
                "side": side,
                "outcome": getattr(outcome, "raw", {}) or {},
                "paper_only": True,
                "execution_authority": "none",
            },
            "search_max_staleness_seconds": settings.prediction_market_search_max_staleness_seconds,
        },
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
    haystack = _quote_haystack(quote)
    tokens = _query_terms(query) or [token for token in re.findall(r"[a-z0-9]+", query.lower()) if token]
    return all(token in haystack for token in tokens) or any(token in haystack for token in tokens[:3])


def _rank_key(quote: PredictionMarketQuote, query: str) -> tuple[float, int, int, int, float, int]:
    text = _quote_haystack(quote)
    tokens = _query_terms(query)
    relevance = sum(1.0 for token in tokens if token in text)
    outcome_text = quote.outcome_name.lower()
    first_term_outcome = 1 if tokens and tokens[0] in outcome_text else 0
    outcome_relevance = sum(1 for token in tokens if token in outcome_text)
    venue_priority = 2 if quote.venue == "hip4" else 1
    liquidity = float(quote.liquidity_usd or 0.0)
    return (relevance, first_term_outcome, outcome_relevance, venue_priority, liquidity, quote.as_of_ms)


def quote_matches_required_terms(quote: PredictionMarketQuote, query: str) -> bool:
    tokens = _query_terms(query)
    if not tokens:
        return True
    haystack = _quote_haystack(quote)
    return all(token in haystack for token in tokens)


def required_query_terms(query: str) -> list[str]:
    return _query_terms(query)


def _quote_haystack(quote: PredictionMarketQuote) -> str:
    text = " ".join([quote.question, quote.outcome_name, quote.venue, quote.market_id, " ".join(quote.symbols), " ".join(quote.topics)]).lower()
    aliases = []
    if "round of 16" in text:
        aliases.append("r16")
    return " ".join([text, *aliases])


def _query_terms(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", query.lower().replace("$", " ")) if token and token not in _QUERY_STOPWORDS]


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_matches_ref(quote: PredictionMarketQuote, ref: str) -> bool:
    source = quote.metadata.get("source_signal") if isinstance(quote.metadata, dict) else {}
    coin_ref = str(source.get("coin") or "") if isinstance(source, dict) else ""
    ref = ref.strip()
    values = {quote.quote_id, quote.signal_id, quote.market_id, quote.outcome_id or "", coin_ref}
    return ref in values or quote.quote_id.startswith(ref) or quote.signal_id.startswith(ref)


def _parse_hip4_ref(ref: str) -> tuple[int, int | None] | None:
    raw = ref.strip().lower()
    raw = raw.removeprefix("hip4:")
    raw = raw.removeprefix("hl:")
    if raw.startswith("#"):
        try:
            asset = parse_coin(raw)
            return asset.outcome_id, asset.side
        except ValueError:
            return None
    match = re.fullmatch(r"(\d+)(?::([01]))?", raw)
    if match:
        return int(match.group(1)), int(match.group(2)) if match.group(2) is not None else None
    match = re.fullmatch(r"(\d+)([01])", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _outcome_matches_terms(outcome: Any, tokens: list[str]) -> bool:
    text = _outcome_haystack(outcome)
    return all(token in text for token in tokens) or any(token in text for token in tokens[:3])


def _outcome_haystack(outcome: Any) -> str:
    text = " ".join(
        [
            str(getattr(outcome, "name", "") or ""),
            str(getattr(outcome, "description", "") or ""),
            str(getattr(outcome, "side0_name", "") or ""),
            str(getattr(outcome, "side1_name", "") or ""),
            str(getattr(outcome, "outcome_id", "") or ""),
        ]
    ).lower()
    if "round of 16" in text:
        text = f"{text} r16"
    return text


def _top_price(levels: Any) -> float | None:
    if not isinstance(levels, list) or not levels:
        return None
    level = levels[0]
    raw = level.get("px") if isinstance(level, dict) else getattr(level, "px", None)
    value = _optional_float(raw)
    return None if value is None else max(0.0, min(1.0, value))


def _mid_price(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is not None and best_ask is not None:
        return max(0.0, min(1.0, (best_bid + best_ask) / 2.0))
    return best_bid if best_bid is not None else best_ask


def _levels_liquidity(data: dict[str, Any]) -> float | None:
    total = Decimal("0")
    for side in ("bids", "asks"):
        for level in data.get(side) or []:
            if isinstance(level, dict):
                px = _decimal(level.get("px"))
                sz = _decimal(level.get("sz"))
            else:
                px = _decimal(getattr(level, "px", None))
                sz = _decimal(getattr(level, "sz", None))
            total += px * sz
    return float(total) if total > 0 else None


def _decimal(value: Any) -> Decimal:
    try:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _hip4_topics(outcome: Any) -> list[str]:
    text = _outcome_haystack(outcome)
    topics = ["hip4", "prediction_market", "outcome_market"]
    for token in ("sports", "football", "world", "cup", "economics", "crypto"):
        if token in text:
            topics.append(token)
    return sorted(set(topics))
