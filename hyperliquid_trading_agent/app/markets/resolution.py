"""Cross-asset intent router and symbol resolver.

This module is deliberately pure: it does not call Hyperliquid, Alpaca, or any
other vendor. Runtime code builds candidate catalogs from providers, then this
module scores/ranks those candidates against the user's prompt intent.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

Provider = Literal["hyperliquid", "alpaca", "alpha_vantage", "tradfi", "static"]
TRADFI_PROVIDERS: set[str] = {"alpaca", "alpha_vantage", "tradfi"}
AssetClass = Literal[
    "crypto_perp",
    "spot",
    "hip3_perp",
    "equity",
    "etf",
    "commodity",
    "unknown",
]

KNOWN_CRYPTO_SYMBOLS: set[str] = {
    "BTC",
    "ETH",
    "SOL",
    "HYPE",
    "DOGE",
    "XRP",
    "BNB",
    "AVAX",
    "SUI",
    "LINK",
    "ARB",
    "OP",
    "LTC",
    "ENA",
    "PUMP",
    "FARTCOIN",
    "PURR",
}

STOP_TICKER_WORDS: set[str] = {
    "A",
    "AN",
    "AND",
    "APP",
    "ARE",
    "ASK",
    "AT",
    "BIG",
    "BOOK",
    "BUY",
    "CALL",
    "CAN",
    "CHART",
    "COMPARE",
    "DAILY",
    "ETF",
    "FOR",
    "FROM",
    "HIGH",
    "HL",
    "IN",
    "IS",
    "LONG",
    "LOW",
    "MARKET",
    "NEW",
    "NOW",
    "OF",
    "ON",
    "OR",
    "PUT",
    "READ",
    "RISK",
    "SELL",
    "SETUP",
    "SHORT",
    "STOCK",
    "THE",
    "TO",
    "TOP",
    "VS",
    "WITH",
}

EQUITY_HINTS = {
    "stock",
    "stocks",
    "equity",
    "equities",
    "share",
    "shares",
    "nasdaq",
    "nyse",
    "earnings",
    "dividend",
    "split",
    "sec filing",
    "10-k",
    "10-q",
    "8-k",
}

ETF_HINTS = {"etf", "fund", "trust", "etn"}

HYPERLIQUID_HINTS = {
    "hyperliquid",
    " hl ",
    "hl perp",
    "perp",
    "perps",
    "futures",
    "funding",
    "orderbook",
    "order book",
    "l2",
    "open interest",
    " oi ",
    "hip-3",
    "hip3",
    "dex",
    "tradexyz",
    "trade xyz",
}

OPTIONS_HINTS = {"option", "options", "call", "calls", "put", "puts", "greeks", "iv", "implied volatility", "flow"}

COMMODITY_TOPIC_SYMBOLS: dict[str, list[str]] = {
    "oil": ["WTI", "CL", "BRENTOIL", "OIL", "USO"],
    "crude": ["WTI", "CL", "BRENTOIL", "OIL", "USO"],
    "wti": ["WTI", "CL", "OIL"],
    "brent": ["BRENTOIL", "OIL"],
    "gold": ["GOLD", "GLD"],
    "silver": ["SILVER", "SLV"],
    "copper": ["COPPER"],
    "wheat": ["WHEAT"],
    "corn": ["CORN"],
    "gas": ["GAS", "UNG"],
    "natural gas": ["GAS", "UNG"],
}

COMMODITY_SYMBOLS: set[str] = {
    "BRENTOIL",
    "CL",
    "COPPER",
    "CORN",
    "GAS",
    "GLD",
    "GOLD",
    "OIL",
    "SILVER",
    "SLV",
    "UNG",
    "USO",
    "USOIL",
    "WHEAT",
    "WTI",
}

_COMPANY_NAME_TO_TICKER: dict[str, str] = {
    "apple": "AAPL",
    "nvidia": "NVDA",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "netflix": "NFLX",
    "amd": "AMD",
    "intel": "INTC",
    "spy": "SPY",
    "qqq": "QQQ",
    "iwm": "IWM",
    "dow": "DIA",
    "spacex": "SPCX",
    "space x": "SPCX",
}

_NAMESPACED_SYMBOL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]{1,16}:[A-Za-z][A-Za-z0-9._-]{0,16})\b")
_UPPER_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9:_$])\$?([A-Z][A-Z0-9.]{0,12})(?![A-Za-z0-9:_])")


class MarketIntent(BaseModel):
    raw_text: str
    symbols: list[str] = Field(default_factory=list)
    explicit_namespaced_symbols: list[str] = Field(default_factory=list)
    commodity_topics: list[str] = Field(default_factory=list)
    wants_market: bool = False
    wants_hyperliquid: bool = False
    wants_tradfi: bool = False
    wants_equity: bool = False
    wants_etf: bool = False
    wants_options: bool = False
    wants_crypto: bool = False
    wants_commodity: bool = False
    wants_funding: bool = False
    wants_orderbook: bool = False
    wants_candles: bool = False
    wants_compare: bool = False
    wants_news: bool = False
    wants_paper: bool = False

    @property
    def has_explicit_asset_class(self) -> bool:
        return self.wants_hyperliquid or self.wants_tradfi or self.wants_equity or self.wants_etf or self.wants_options or self.wants_crypto or self.wants_commodity


class AssetCandidate(BaseModel):
    query: str
    symbol: str
    canonical_symbol: str
    display_symbol: str
    asset_class: AssetClass
    provider: Provider
    venue: str
    source: str = ""
    dex: str | None = None
    active: bool | None = None
    tradable: bool | None = None
    liquidity_usd: float | None = None
    open_interest: float | None = None
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_hyperliquid(self) -> bool:
        return self.provider == "hyperliquid"

    @property
    def is_tradfi(self) -> bool:
        return self.provider in TRADFI_PROVIDERS


class RoutedSymbol(BaseModel):
    query: str
    selected: list[AssetCandidate] = Field(default_factory=list)
    candidates: list[AssetCandidate] = Field(default_factory=list)
    ambiguous: bool = False
    reason: str = ""


class ResolutionPlan(BaseModel):
    intent: MarketIntent
    routes: list[RoutedSymbol] = Field(default_factory=list)
    hyperliquid_symbols: list[str] = Field(default_factory=list)
    tradfi_symbols: list[str] = Field(default_factory=list)
    ambiguous_queries: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def parse_market_intent(text: str) -> MarketIntent:
    lowered = f" {text.lower()} "
    symbols: list[str] = []
    namespaced = [_canonical_namespaced(match.group(1)) for match in _NAMESPACED_SYMBOL_RE.finditer(text)]
    symbols.extend(namespaced)

    uppercase_tokens: set[str] = set()
    for match in _UPPER_SYMBOL_RE.finditer(text):
        token = match.group(1).upper()
        if token in STOP_TICKER_WORDS:
            continue
        uppercase_tokens.add(token)
        if token not in symbols:
            symbols.append(token)

    for name, ticker in _COMPANY_NAME_TO_TICKER.items():
        if name in lowered and ticker not in symbols:
            symbols.append(ticker)

    commodity_topics: list[str] = []
    for topic, topic_symbols in COMMODITY_TOPIC_SYMBOLS.items():
        if _has_non_uppercase_topic(text, topic) and topic not in commodity_topics:
            commodity_topics.append(topic)
            for symbol in topic_symbols:
                # Preserve a distinction between lowercase concepts ("oil market")
                # and uppercase ticker-like queries ("OIL read"). The latter is
                # resolved as the exact symbol only; it should not fan out into a
                # whole crude-oil basket unless the user used commodity language.
                if symbol not in symbols and symbol not in uppercase_tokens:
                    symbols.append(symbol)

    wants_funding = " funding " in lowered
    wants_orderbook = any(term in lowered for term in [" orderbook ", " order book ", " l2 ", " depth ", " bid ", " ask "])
    wants_candles = any(term in lowered for term in [" chart ", " candle ", " candles ", " trend ", " 1h ", " 4h ", " daily "])
    wants_compare = any(term in lowered for term in [" compare ", " versus ", " vs ", " side by side "])
    wants_news = any(term in lowered for term in [" news ", " headline ", " macro ", " fed ", " cpi ", " fomc ", " ppi "])
    wants_paper = any(term in lowered for term in [" paper ", " simulate ", " position size ", " risk 1", " risk:"])

    wants_hyperliquid = any(term in lowered for term in HYPERLIQUID_HINTS) or wants_funding or wants_orderbook
    wants_equity = any(term in lowered for term in EQUITY_HINTS)
    wants_etf = any(term in lowered for term in ETF_HINTS)
    wants_options = any(term in lowered for term in OPTIONS_HINTS)
    wants_tradfi = wants_equity or wants_etf or wants_options
    wants_commodity = bool(commodity_topics)
    wants_crypto = any(symbol in KNOWN_CRYPTO_SYMBOLS for symbol in symbols) or any(term in lowered for term in [" crypto ", " token ", " coin "])
    wants_market = any(term in lowered for term in [" market ", " price ", " read ", " setup ", " trade ", " long ", " short "])

    return MarketIntent(
        raw_text=text,
        symbols=_dedupe(symbols),
        explicit_namespaced_symbols=_dedupe(namespaced),
        commodity_topics=commodity_topics,
        wants_market=wants_market,
        wants_hyperliquid=wants_hyperliquid,
        wants_tradfi=wants_tradfi,
        wants_equity=wants_equity,
        wants_etf=wants_etf,
        wants_options=wants_options,
        wants_crypto=wants_crypto,
        wants_commodity=wants_commodity,
        wants_funding=wants_funding,
        wants_orderbook=wants_orderbook,
        wants_candles=wants_candles,
        wants_compare=wants_compare,
        wants_news=wants_news,
        wants_paper=wants_paper,
    )


def route_market_intent(
    intent: MarketIntent,
    candidates_by_query: dict[str, list[AssetCandidate]],
    *,
    ambiguity_threshold: float = 25.0,
) -> ResolutionPlan:
    routes: list[RoutedSymbol] = []
    hyperliquid_symbols: list[str] = []
    tradfi_symbols: list[str] = []
    ambiguous_queries: list[str] = []
    notes: list[str] = []

    for query in intent.symbols:
        raw_candidates = candidates_by_query.get(query, [])
        candidates = [_score_candidate(candidate, intent, query) for candidate in raw_candidates]
        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            routes.append(RoutedSymbol(query=query, reason="no_candidates"))
            notes.append(f"No market candidate found for {query}.")
            continue
        selected, ambiguous, reason = _select_candidates(query, candidates, intent, ambiguity_threshold)
        routes.append(RoutedSymbol(query=query, selected=selected, candidates=candidates, ambiguous=ambiguous, reason=reason))
        if ambiguous:
            ambiguous_queries.append(query)
        for candidate in selected:
            if candidate.provider == "hyperliquid":
                _append_unique(hyperliquid_symbols, candidate.canonical_symbol)
            elif candidate.is_tradfi:
                _append_unique(tradfi_symbols, candidate.canonical_symbol)

    if ambiguous_queries:
        notes.append(
            "Ambiguous market symbols detected: "
            + ", ".join(ambiguous_queries)
            + ". The agent should label venues/providers explicitly."
        )

    return ResolutionPlan(
        intent=intent,
        routes=routes,
        hyperliquid_symbols=hyperliquid_symbols,
        tradfi_symbols=tradfi_symbols,
        ambiguous_queries=ambiguous_queries,
        notes=notes,
    )


def _score_candidate(candidate: AssetCandidate, intent: MarketIntent, query: str) -> AssetCandidate:
    c = candidate.model_copy(deep=True)
    score = 0.0
    reasons: list[str] = []
    query_upper = query.upper()
    canonical_upper = c.canonical_symbol.upper()

    if ":" in query and canonical_upper == query_upper:
        score += 220
        reasons.append("explicit_namespace_match")
    elif ":" in query:
        score -= 120
        reasons.append("different_namespace_than_query")

    if c.provider == "hyperliquid":
        if intent.wants_hyperliquid:
            score += 95
            reasons.append("hyperliquid_intent")
        if intent.wants_funding or intent.wants_orderbook:
            score += 85
            reasons.append("perp_microstructure_intent")
        if intent.wants_tradfi and not intent.wants_hyperliquid:
            score -= 35
            reasons.append("tradfi_intent_penalty")
        if c.asset_class == "crypto_perp" and query_upper in KNOWN_CRYPTO_SYMBOLS and not intent.wants_tradfi:
            score += 125
            reasons.append("known_crypto_main_perp")
        elif c.asset_class == "crypto_perp":
            score += 55
            reasons.append("main_hyperliquid_perp")
        elif c.asset_class == "hip3_perp":
            score += 45
            reasons.append("hip3_perp")
        elif c.asset_class == "commodity":
            score += 45
            reasons.append("hip3_commodity")
        elif c.asset_class == "spot":
            score += 35
            reasons.append("hyperliquid_spot")
    elif c.is_tradfi:
        if intent.wants_tradfi or intent.wants_equity or intent.wants_options or intent.wants_etf:
            score += 105
            reasons.append("tradfi_intent")
        if query_upper in KNOWN_CRYPTO_SYMBOLS and not intent.wants_tradfi:
            score -= 95
            reasons.append("crypto_symbol_equity_penalty")
        if c.asset_class == "etf" and intent.wants_etf:
            score += 60
            reasons.append("etf_intent")
        elif c.asset_class == "equity":
            score += 50
            reasons.append("active_equity_candidate")
        if c.active is False:
            score -= 90
            reasons.append("inactive_asset_penalty")
        if c.tradable is False:
            score -= 15
            reasons.append("non_tradable_penalty")

    if c.asset_class == "commodity":
        if intent.wants_commodity:
            score += 115
            reasons.append("commodity_intent")
        if query_upper in COMMODITY_SYMBOLS:
            score += 35
            reasons.append("commodity_symbol")
    elif intent.wants_commodity and c.is_tradfi and c.asset_class in {"etf", "equity"}:
        score += 20
        reasons.append("commodity_related_tradfi_candidate")

    if not intent.has_explicit_asset_class:
        if c.is_tradfi and c.active is not False and c.tradable is not False:
            score += 25
            reasons.append("no_explicit_intent_active_tradfi")
        if c.provider == "hyperliquid" and c.asset_class in {"hip3_perp", "commodity"}:
            score += 25
            reasons.append("no_explicit_intent_hip3")

    liq_bonus = _liquidity_bonus(c.liquidity_usd)
    if liq_bonus:
        score += liq_bonus
        reasons.append(f"liquidity_bonus_{liq_bonus:.0f}")
    oi_bonus = _open_interest_bonus(c.open_interest)
    if oi_bonus:
        score += oi_bonus
        reasons.append(f"open_interest_bonus_{oi_bonus:.0f}")

    c.score = round(score, 3)
    c.reasons = [*c.reasons, *reasons]
    return c


def _select_candidates(query: str, candidates: list[AssetCandidate], intent: MarketIntent, threshold: float) -> tuple[list[AssetCandidate], bool, str]:
    top = candidates[0]
    if ":" in query:
        return [top], False, "explicit_namespace"
    if intent.wants_tradfi and top.is_tradfi and not intent.wants_hyperliquid:
        return [top], False, "tradfi_intent"
    if intent.wants_hyperliquid and top.provider == "hyperliquid":
        return [top], False, "hyperliquid_intent"
    if intent.wants_commodity and top.asset_class == "commodity":
        alternatives = [item for item in candidates[1:] if top.score - item.score <= threshold and item.asset_class == "commodity"]
        return [top, *alternatives[:2]], len(alternatives) > 0, "commodity_intent"
    if query.upper() in KNOWN_CRYPTO_SYMBOLS and top.asset_class == "crypto_perp" and not intent.wants_tradfi:
        return [top], False, "known_crypto_default"

    close = [item for item in candidates[1:] if top.score - item.score <= threshold and item.score > 0]
    cross_provider = any(item.provider != top.provider for item in close)
    duplicate_hip3 = top.provider == "hyperliquid" and any(item.provider == "hyperliquid" and item.dex != top.dex for item in close)
    if close and (cross_provider or duplicate_hip3 or not intent.has_explicit_asset_class):
        return [top, *close[:3]], True, "ambiguous_close_candidates"
    return [top], False, "highest_score"


def _liquidity_bonus(value: float | None) -> float:
    if value is None or value <= 0:
        return 0.0
    # 10k -> 2, 100k -> 4, 1m -> 6, 10m -> 8, 100m -> 10, capped at 20.
    return max(0.0, min(20.0, (math.log10(value) - 3.0) * 2.0))


def _open_interest_bonus(value: float | None) -> float:
    if value is None or value <= 0:
        return 0.0
    return max(0.0, min(10.0, (math.log10(value) - 2.0) * 1.5))


def _has_non_uppercase_topic(text: str, topic: str) -> bool:
    pattern = r"\b" + re.escape(topic).replace(r"\ ", r"\s+") + r"\b"
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        if not match.group(0).isupper():
            return True
    return False


def _canonical_namespaced(value: str) -> str:
    dex, symbol = value.split(":", 1)
    return f"{dex.lower()}:{symbol.upper()}"


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        cleaned = _canonical_namespaced(value) if ":" in value else value.strip().upper()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
