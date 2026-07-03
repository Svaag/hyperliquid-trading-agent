from __future__ import annotations

import re

from hyperliquid_trading_agent.app.agent.high_stakes.schemas import HighStakesRoute, RiskLevel
from hyperliquid_trading_agent.app.markets.non_market import NON_MARKET_SYMBOLS, is_non_market_symbol

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,12}\b")
NON_MARKET_TOKENS = NON_MARKET_SYMBOLS

COMMON_COINS = {
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

EXPLICIT_TERMS = {"debate this trade", "high stakes", "multi-agent", "adversarial", "red team"}
SETUP_TERMS = {
    "long",
    "short",
    "entry",
    "stop",
    "sl",
    "take profit",
    "tp",
    "position size",
    "risk",
    "leverage",
    "liquidation",
}
EXECUTION_TERMS = {"execute", "autonomous", "place order", "submit order", "proposal", "trade proposal"}
ACCOUNT_TERMS = {"account", "portfolio", "margin", "exposure", "open position", "open orders", "pnl", "drawdown"}
TRADING_TERMS = {"trade", "trading", "market", "setup", "perp", "perps", "funding", "order book"}
RESEARCH_TERMS = {"news", "macro", "fed", "fomc", "cpi", "ppi", "rates", "catalyst", "headline", "swing"}


def route_high_stakes(
    prompt: str,
    *,
    forced: bool = False,
    activation_policy: str = "risk_routed",
    max_coins: int = 3,
) -> HighStakesRoute:
    lowered = prompt.lower()
    explicit = any(term in lowered for term in EXPLICIT_TERMS)
    setup = any(term in lowered for term in SETUP_TERMS)
    execution = any(term in lowered for term in EXECUTION_TERMS)
    trading = any(term in lowered for term in TRADING_TERMS)
    account_intent = any(term in lowered for term in ACCOUNT_TERMS)
    research_intent = any(term in lowered for term in RESEARCH_TERMS)
    addresses = [address.lower() for address in ADDRESS_RE.findall(prompt)]
    coins = extract_route_coins(prompt)[: max(1, max_coins)]

    if forced:
        activate = True
        reason = "forced_high_stakes_endpoint"
    elif activation_policy == "explicit_only":
        activate = explicit
        reason = "explicit_high_stakes_request" if explicit else "not_explicit_high_stakes"
    elif activation_policy == "all_trading_questions":
        activate = trading or setup or execution or account_intent or bool(addresses)
        reason = "all_trading_questions_policy" if activate else "no_trading_intent"
    else:
        activate = explicit or execution or setup or (bool(addresses) and account_intent)
        reason = _risk_routed_reason(explicit, execution, setup, bool(addresses), account_intent)

    selected_roles = _selected_roles(
        activate=activate,
        research_intent=research_intent,
        account_intent=account_intent or bool(addresses),
        execution_intent=execution,
    )
    return HighStakesRoute(
        activate=activate,
        forced=forced,
        reason=reason,
        risk_level=_risk_level(execution, setup, bool(addresses) and account_intent, explicit),
        selected_roles=selected_roles,
        coins=coins,
        addresses=addresses,
        intent=_intent(execution, setup, account_intent, research_intent),
    )


def extract_route_coins(text: str) -> list[str]:
    # Any all-caps token may be a Hyperliquid ticker. Keep the explicit common
    # list for lower/mixed-case extraction, but do not block uppercase symbols
    # just because this allowlist is stale.
    uppercase_tickers = {
        match.group(0).upper()
        for match in TOKEN_RE.finditer(text)
        if not _excluded_route_token(match.group(0), text=text, start=match.start(), end=match.end())
    }
    candidates = {coin for coin in TOKEN_RE.findall(text.upper()) if not _excluded_route_token(coin)}
    coins = list(uppercase_tickers | {coin for coin in candidates if coin in COMMON_COINS or coin.startswith("@")})
    lowered = text.lower()
    if "bitcoin" in lowered:
        coins.append("BTC")
    if "ethereum" in lowered:
        coins.append("ETH")
    return sorted(set(coins))


def _excluded_route_token(
    token: str,
    *,
    text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> bool:
    return is_non_market_symbol(token, text=text, start=start, end=end)


def _selected_roles(*, activate: bool, research_intent: bool, account_intent: bool, execution_intent: bool) -> list[str]:
    if not activate:
        return []
    roles = ["analyst", "quant", "risk"]
    if research_intent:
        roles.append("research")
    if account_intent:
        roles.append("treasury")
    if execution_intent:
        roles.append("execution")
    roles.extend(["adversary", "judge"])
    return roles


def _risk_routed_reason(explicit: bool, execution: bool, setup: bool, has_address: bool, account_intent: bool) -> str:
    if explicit:
        return "explicit_high_stakes_request"
    if execution:
        return "execution_or_autonomous_language"
    if setup:
        return "trade_setup_or_position_risk_language"
    if has_address and account_intent:
        return "account_risk_review"
    return "ordinary_question_single_agent_ok"


def _risk_level(execution: bool, setup: bool, account_review: bool, explicit: bool) -> RiskLevel:
    if execution:
        return "critical"
    if setup or account_review or explicit:
        return "high"
    return "low"


def _intent(execution: bool, setup: bool, account_intent: bool, research_intent: bool) -> str:
    if execution:
        return "autonomous_or_execution_proposal"
    if setup:
        return "trade_setup_review"
    if account_intent:
        return "account_risk_review"
    if research_intent:
        return "research_review"
    return "general"
