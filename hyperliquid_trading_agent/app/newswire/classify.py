from __future__ import annotations

from hyperliquid_trading_agent.app.newswire.schemas import AssetClass, EventType, Urgency

# Source reliability weights (0-1). Used for dedupe preference and importance boosting.
# Regulators/exchanges/central banks score highest; social scores lowest.
SOURCE_SCORES: dict[str, float] = {
    "sec_edgar": 1.0,
    "nasdaq_halts": 1.0,
    "federal_reserve": 1.0,
    "ecb": 1.0,
    "trading_economics": 0.9,
    "alpaca": 0.9,
    "benzinga": 0.9,
    "globe_newswire": 0.85,
    "business_wire": 0.85,
    "coindesk": 0.7,
    "cointelegraph": 0.65,
    "x_allowlist": 0.6,
    "x_cashtag": 0.4,
    "x": 0.4,
    "tavily": 0.5,
    "serpapi": 0.5,
    "newsapi": 0.5,
    "perplexity": 0.45,
    "rss": 0.6,
}

_MACRO_WORDS = {"cpi", "fomc", "federal reserve", "rate decision", "payrolls", "nonfarm", "gdp", "inflation", "unemployment", "ecb", "boe", "boj", "pce", "jobless", "interest rate"}
_FILING_WORDS = {"8-k", "10-q", "10-k", "13d", "13g", "s-1", "form 4", "prospectus", "edgar", "files with sec", "sec filing"}
_HALT_WORDS = {"trading halt", "halted", "trade halt", "trading pause", "lulD", "circuit breaker"}
_MNA_WORDS = {"merger", "acquisition", "to acquire", "acquires", "takeover", "buyout", "to buy", "all-cash deal"}
_REG_WORDS = {"lawsuit", "sues", "charges", "investigation", "subpoena", "sanction", "fine", "settlement", "ban", "regulator", "indicted", "fraud"}
_RATING_WORDS = {"upgrade", "downgrade", "price target", "initiated", "reiterates", "raises target", "cuts target", "overweight", "underweight", "outperform"}
_EARN_WORDS = {"earnings", "eps", "revenue", "quarterly results", "guidance", "beats estimates", "misses estimates", "profit", "q1", "q2", "q3", "q4 results"}
_CRYPTO_PROTO_WORDS = {"hack", "exploit", "mainnet", "hard fork", "staking", "airdrop", "protocol", "validator", "token unlock", "bridge", "depeg", "smart contract"}
_EXCHANGE_WORDS = {"outage", "maintenance", "delist", "delisting", "listing", "lists", "suspend", "withdrawal"}
_PR_WORDS = {"announces", "announced", "launches", "partnership", "unveils", "press release", "to host", "appoints"}
_CRYPTO_WORDS = {"crypto", "bitcoin", "ethereum", "blockchain", "defi", "stablecoin", "altcoin", "token", "onchain", "web3", "nft"}
_BREAKING_WORDS = {"breaking", "urgent", "alert", "just in", "developing"}


def source_score(source: str) -> float:
    return SOURCE_SCORES.get(source.lower(), 0.5)


def classify_event_type(source: str, text: str, hint: EventType | None = None) -> EventType:
    if hint is not None:
        return hint
    lowered = text.lower()
    src = source.lower()
    if src == "sec_edgar" or _any(lowered, _FILING_WORDS):
        return "sec_filing"
    if src == "nasdaq_halts" or _any(lowered, _HALT_WORDS):
        return "halt"
    if src in {"federal_reserve", "ecb", "trading_economics"} or _any(lowered, _MACRO_WORDS):
        return "macro"
    if _any(lowered, _MNA_WORDS):
        return "mna"
    if _any(lowered, _REG_WORDS):
        return "regulatory"
    if _any(lowered, _RATING_WORDS):
        return "analyst_rating"
    if _any(lowered, _EARN_WORDS):
        return "earnings"
    if _any(lowered, _CRYPTO_PROTO_WORDS):
        return "crypto_protocol"
    if _any(lowered, _EXCHANGE_WORDS):
        return "exchange_status"
    if src in {"globe_newswire", "business_wire"} or _any(lowered, _PR_WORDS):
        return "press_release"
    if src in {"x", "x_allowlist", "x_cashtag"}:
        return "social"
    return "headline"


def classify_asset_class(source: str, text: str, symbols: list[str], hint: AssetClass | None = None) -> AssetClass:
    if hint is not None and hint != "unknown":
        return hint
    lowered = text.lower()
    src = source.lower()
    if src in {"federal_reserve", "ecb", "trading_economics"} or _any(lowered, _MACRO_WORDS):
        return "macro"
    if _any(lowered, _CRYPTO_WORDS) or any(s.upper() in {"BTC", "ETH", "HYPE", "SOL"} for s in symbols):
        return "crypto"
    if src in {"sec_edgar", "nasdaq_halts", "globe_newswire", "business_wire", "alpaca", "benzinga"}:
        return "equity"
    if src in {"coindesk", "cointelegraph"}:
        return "crypto"
    return "unknown"


def classify_urgency(source: str, transport: str, event_type: EventType, importance: float, text: str) -> Urgency:
    lowered = text.lower()
    if _any(lowered, _BREAKING_WORDS):
        return "breaking"
    if event_type in {"halt", "sec_filing", "mna", "regulatory"} and transport == "websocket":
        return "breaking"
    if event_type in {"halt", "mna"} or importance >= 80:
        return "breaking"
    if event_type == "social" and importance < 40:
        return "background"
    if importance < 25:
        return "background"
    return "normal"


def _any(text: str, words: set[str]) -> bool:
    return any(word in text for word in words)
