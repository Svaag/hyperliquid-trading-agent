from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.markets.non_market import (
    NON_MARKET_SYMBOLS,
    is_non_market_symbol,
    normalize_market_symbol_token,
)

HYPERLIQUID_NATIVE_TOKEN = "HYPE"

NON_MARKET_TICKERS = NON_MARKET_SYMBOLS

_SYMBOL_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,12}\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

_IDENTITY_PATTERNS = (
    re.compile(r"\bnative\s+token\s+of\s+(?:the\s+)?hyperliquid\b", re.IGNORECASE),
    re.compile(r"\bhyperliquid(?:'s)?\s+(?:native|gas|utility)\s+token\b", re.IGNORECASE),
    re.compile(r"\bgas\s*/\s*utility\s+token\b", re.IGNORECASE),
    re.compile(r"\beth\s+of\s+hyperliquid\b", re.IGNORECASE),
    re.compile(r"\bhyperliquid\s+ecosystem\b", re.IGNORECASE),
)

_CATALYST_TERMS = {
    "mainnet",
    "staking",
    "stake",
    "validator",
    "delegation",
    "airdrop",
    "gas",
}

_EVIDENCE_TOOLS = {"search_market_news", "search_hyperliquid_docs"}


@dataclass(frozen=True)
class IdentityGuardVerdict:
    blocked: bool
    symbols: list[str]
    reason: str = ""
    correction: str = ""
    warning: str = ""


def is_non_market_ticker(
    token: str,
    *,
    text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> bool:
    return is_non_market_symbol(token, text=text, start=start, end=end)


def asset_identity_context(tool_results: list[Any], *, prompt: str = "") -> dict[str, Any]:
    symbols = active_hyperliquid_symbols(tool_results, prompt=prompt)
    return {
        "native_hyperliquid_token": HYPERLIQUID_NATIVE_TOKEN,
        "symbols": [
            {
                "symbol": symbol,
                "hyperliquid_identity": "native_token" if symbol == HYPERLIQUID_NATIVE_TOKEN else "listed_market_only",
                "claim_policy": (
                    "Hyperliquid-native token claims are allowed for HYPE."
                    if symbol == HYPERLIQUID_NATIVE_TOKEN
                    else "Treat as a Hyperliquid-listed market only; do not claim Hyperliquid-native token, gas, staking, validator, mainnet, or ecosystem utility without explicit source evidence."
                ),
            }
            for symbol in symbols
        ],
    }


def active_hyperliquid_symbols(tool_results: list[Any], *, prompt: str = "") -> list[str]:
    symbols: list[str] = []
    for result in tool_results:
        tool = str(_get(result, "tool") or "")
        data = _get(result, "data")
        if tool == "resolve_market_intent" and isinstance(data, dict):
            for symbol in data.get("hyperliquid_symbols") or []:
                _append_symbol(symbols, str(symbol))
        elif tool == "get_market_snapshot" and isinstance(data, dict):
            assets = data.get("assets") or {}
            if isinstance(assets, dict):
                for key, asset in assets.items():
                    _append_symbol(symbols, str(key))
                    if isinstance(asset, dict):
                        _append_symbol(symbols, str(asset.get("query_symbol") or ""))
                        _append_symbol(symbols, str(asset.get("coin") or ""))
        elif tool == "get_funding_context" and isinstance(data, dict):
            _append_symbol(symbols, str(data.get("query_symbol") or ""))
            _append_symbol(symbols, str(data.get("coin") or ""))
        elif tool == "get_candles":
            if isinstance(data, list) and data and isinstance(data[0], dict):
                _append_symbol(symbols, str(data[0].get("s") or ""))
            elif isinstance(data, dict):
                _append_symbol(symbols, str(data.get("query_symbol") or data.get("coin") or ""))
    if not symbols and prompt:
        for match in _SYMBOL_RE.finditer(prompt):
            _append_symbol(symbols, match.group(0), text=prompt, start=match.start(), end=match.end())
    return symbols


def guard_unsupported_public_claims(text: str, tool_results: list[Any], *, prompt: str = "") -> IdentityGuardVerdict:
    symbols = active_hyperliquid_symbols(tool_results, prompt=prompt)
    non_native = [symbol for symbol in symbols if symbol != HYPERLIQUID_NATIVE_TOKEN]
    if not text.strip() or not non_native:
        return IdentityGuardVerdict(blocked=False, symbols=symbols)
    sentences = [sentence.strip() for sentence in _SENTENCE_SPLIT_RE.split(text) if sentence.strip()]
    for symbol in non_native:
        for sentence in sentences:
            single_asset_pronoun_claim = len(non_native) == 1 and not _mentions_symbol(sentence, HYPERLIQUID_NATIVE_TOKEN)
            if not _mentions_symbol(sentence, symbol) and not single_asset_pronoun_claim:
                continue
            if _sentence_negates_native_claim(sentence):
                continue
            if _has_unsupported_identity_claim(sentence):
                return _blocked(symbols, f"unsupported_hyperliquid_identity_claim:{symbol}")
            catalyst_terms = _unsupported_catalyst_terms(sentence)
            if catalyst_terms and not _has_supporting_evidence(tool_results, symbol, catalyst_terms):
                return _blocked(symbols, f"unsupported_project_catalyst_claim:{symbol}:{','.join(sorted(catalyst_terms))}")
    return IdentityGuardVerdict(blocked=False, symbols=symbols)


def correction_for_symbols(symbols: list[str]) -> str:
    non_native = [symbol for symbol in symbols if symbol != HYPERLIQUID_NATIVE_TOKEN]
    target = ", ".join(non_native) if non_native else "That asset"
    return (
        f"Correction: {target} is only verified here as a Hyperliquid-listed market. "
        f"{HYPERLIQUID_NATIVE_TOKEN} is Hyperliquid's native token. Do not treat {target} as Hyperliquid-native, gas, staking, validator, or mainnet utility without explicit source evidence."
    )


def _blocked(symbols: list[str], reason: str) -> IdentityGuardVerdict:
    return IdentityGuardVerdict(
        blocked=True,
        symbols=symbols,
        reason=reason,
        correction=correction_for_symbols(symbols),
        warning=f"unsupported_identity_or_catalyst_claim_removed:{reason}",
    )


def _has_unsupported_identity_claim(sentence: str) -> bool:
    lowered = sentence.lower()
    if "hyperliquid" not in lowered and "gas/utility" not in lowered:
        return False
    return any(pattern.search(sentence) for pattern in _IDENTITY_PATTERNS)


def _unsupported_catalyst_terms(sentence: str) -> set[str]:
    lowered = sentence.lower()
    if "hyperliquid" not in lowered and "l1" not in lowered and "l2" not in lowered:
        return set()
    return {term for term in _CATALYST_TERMS if term in lowered}


def _has_supporting_evidence(tool_results: list[Any], symbol: str, terms: set[str]) -> bool:
    if not terms:
        return True
    symbol_lower = symbol.lower()
    for result in tool_results:
        tool = str(_get(result, "tool") or "")
        if tool not in _EVIDENCE_TOOLS:
            continue
        evidence = str(_get(result, "data") or "").lower()
        if symbol_lower in evidence and any(term in evidence for term in terms):
            return True
    return False


def _sentence_negates_native_claim(sentence: str) -> bool:
    lowered = sentence.lower()
    return " not " in f" {lowered} " and ("native" in lowered or "gas" in lowered or "staking" in lowered)


def _mentions_symbol(sentence: str, symbol: str) -> bool:
    return re.search(rf"\b{re.escape(symbol)}\b", sentence, re.IGNORECASE) is not None


def _append_symbol(
    symbols: list[str],
    raw: str,
    *,
    text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> None:
    symbol = _normalize_symbol(raw)
    if is_non_market_symbol(raw, text=text, start=start, end=end):
        return
    if symbol not in symbols:
        symbols.append(symbol)


def _normalize_symbol(raw: str) -> str:
    return normalize_market_symbol_token(raw)


def _get(result: Any, name: str) -> Any:
    if isinstance(result, dict):
        return result.get(name)
    return getattr(result, name, None)
