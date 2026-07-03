from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    reason: str = ""
    category: str = "allowed"


_DISALLOWED_SECRET_TERMS = {
    "private key",
    "seed phrase",
    "mnemonic",
    "password",
    "api secret",
    "api key",
    "sign this transaction",
}

_DISALLOWED_ABUSE_TERMS = {
    "wash trade",
    "spoof",
    "manipulate",
    "pump and dump",
    "front-run",
    "insider",
}

_ALLOWED_COIN_TERMS = {
    "btc",
    "bitcoin",
    "eth",
    "ethereum",
    "sol",
    "hype",
    "doge",
    "xrp",
    "bnb",
    "avax",
    "sui",
    "link",
    "arb",
    "op",
    "ltc",
    "ena",
    "pump",
    "fartcoin",
    "purr",
}

UPPERCASE_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,12}\b")

_ALLOWED_TOPIC_TERMS = {
    "trade",
    "trading",
    "market",
    *_ALLOWED_COIN_TERMS,
    "crypto",
    "hyperliquid",
    "funding",
    "margin",
    "liquidation",
    "order",
    "book",
    "pnl",
    "macro",
    "fed",
    "fomc",
    "inflation",
    "cpi",
    "ppi",
    "rates",
    "treasury",
    "dollar",
    "economy",
    "economic",
    "edgar",
    "filing",
    "filings",
    "news",
    "stock",
    "equity",
    "forex",
    "oil",
    "gold",
    "risk",
    "portfolio",
    "support",
    "api",
}


def classify_request(text: str) -> GuardrailResult:
    lowered = text.lower()
    if any(term in lowered for term in _DISALLOWED_SECRET_TERMS):
        return GuardrailResult(False, "I cannot handle private keys, seed phrases, passwords, API keys, or signing secrets.", "secret")
    if any(term in lowered for term in _DISALLOWED_ABUSE_TERMS):
        return GuardrailResult(False, "I cannot help with market manipulation, abusive trading, or insider-trading behavior.", "abuse")
    if any(term in lowered for term in _ALLOWED_TOPIC_TERMS):
        return GuardrailResult(True)
    if UPPERCASE_TICKER_RE.search(text):
        return GuardrailResult(True)
    return GuardrailResult(False, "I can only help with trading, Hyperliquid, markets, macro/economics, or adjacent news.", "off_topic")
