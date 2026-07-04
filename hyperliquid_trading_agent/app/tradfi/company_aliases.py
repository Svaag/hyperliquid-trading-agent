from __future__ import annotations

import re

COMPANY_NAME_TO_TICKER: dict[str, str] = {
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
    "circle": "CRCL",
    "circle internet": "CRCL",
    "circle internet group": "CRCL",
    "circle internet financial": "CRCL",
}

_ALIAS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcircle(?:\s+internet(?:\s+(?:group|financial))?)?\b", re.IGNORECASE), "CRCL"),
    (re.compile(r"\bspace\s*x\b", re.IGNORECASE), "SPCX"),
]


def company_aliases_in_text(text: str) -> list[str]:
    """Return known ticker aliases mentioned by company/name in insertion order."""
    lowered = f" {str(text or '').lower()} "
    out: list[str] = []
    for name, ticker in COMPANY_NAME_TO_TICKER.items():
        if _contains_alias(lowered, name) and ticker not in out:
            out.append(ticker)
    for pattern, ticker in _ALIAS_PATTERNS:
        if pattern.search(text or "") and ticker not in out:
            out.append(ticker)
    return out


def resolve_company_alias(text: str) -> str | None:
    aliases = company_aliases_in_text(text)
    return aliases[0] if aliases else None


def _contains_alias(lowered_padded: str, alias: str) -> bool:
    escaped = re.escape(alias.lower()).replace(r"\ ", r"\s+")
    return re.search(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", lowered_padded) is not None
