from __future__ import annotations

import re

NON_MARKET_SYMBOLS: set[str] = {
    "API",
    "APP",
    "BEA",
    "BLS",
    "CFTC",
    "CIK",
    "CPI",
    "CUSIP",
    "DEX",
    "EDGAR",
    "EOF",
    "ERROR",
    "ETF",
    "FINRA",
    "FOMC",
    "FRED",
    "GAAP",
    "GDP",
    "HTTP",
    "HTTPS",
    "IFRS",
    "INFO",
    "ISIN",
    "JSON",
    "LEI",
    "LLM",
    "NASDAQ",
    "NFP",
    "NYSE",
    "PCE",
    "POST",
    "PPI",
    "REST",
    "SDK",
    "SEC",
    "STATUS",
    "TEAM",
    "TIMEOUT",
    "URI",
    "URL",
    "USD",
    "USDC",
    "USDT",
    "XBRL",
}

_SEC_FORM_RE = re.compile(
    r"""
    (?<![A-Za-z0-9])
    (?:
        10-[KQ]
        |8-K
        |6-K
        |20-F
        |40-F
        |S-[1348]
        |F-[13]
        |13[DFG]
        |DEF\s+14A
        |FORM\s+(?:3|4|5|144|8-K|10-K|10-Q|13D|13G|13F)
    )
    (?![A-Za-z0-9])
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_market_symbol_token(raw: str) -> str:
    value = str(raw or "").strip().upper()
    if ":" in value:
        value = value.split(":", 1)[-1]
    if value.endswith("/USDC"):
        value = value[:-5]
    return value


def is_non_market_symbol(
    token: str,
    *,
    text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> bool:
    symbol = normalize_market_symbol_token(token)
    if not symbol:
        return True
    if symbol in NON_MARKET_SYMBOLS:
        return True
    return _is_sec_form_fragment(text, start, end)


def _is_sec_form_fragment(text: str | None, start: int | None, end: int | None) -> bool:
    if text is None or start is None or end is None:
        return False
    for match in _SEC_FORM_RE.finditer(text):
        if start < match.end() and end > match.start():
            return True
    return False
