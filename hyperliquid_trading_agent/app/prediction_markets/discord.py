from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.prediction_markets.schemas import (
    PredictionMarketQuote,
    PredictionMarketSettlementRequest,
)

_NUMBER = r"(\d+(?:\.\d+)?)"
_PM_REF_RE = re.compile(r"\bpm:?(pm_[a-f0-9]{4,}|[a-z0-9_:-]{6,})\b", re.IGNORECASE)
_HIP4_REF_RE = re.compile(r"(?:\bhip4:(?:#)?\d+(?::[01])?\b|#\d+[01]\b)", re.IGNORECASE)
_YES_WORD_RE = re.compile(r"\byes\b", re.IGNORECASE)
_NO_WORD_RE = re.compile(r"\bno\b", re.IGNORECASE)
_NATURAL_YES_RE = re.compile(r"\b(?:win|wins|winning|beat|beats|beating|defeat|defeats|defeating)\b", re.IGNORECASE)
_BETTING_WORD_RE = re.compile(r"\b(?:bet|buy|paper|pm|prediction|predict|market)\b", re.IGNORECASE)


class PredictionMarketDiscordCommand(BaseModel):
    action: Literal["search", "draft", "confirm", "cancel", "close", "portfolio", "positions", "leaderboard", "settle", "settlement_sweep"]
    draft_id: str | None = None
    position_ref: str | None = None
    query: str = ""
    market_ref: str | None = None
    side: Literal["yes", "no"] = "yes"
    stake_usd: float | None = Field(default=None, gt=0)
    settlement: PredictionMarketSettlementRequest | None = None
    error: str = ""


def parse_prediction_market_discord_command(prompt: str, referenced_message: Any = None) -> PredictionMarketDiscordCommand | None:
    normalized = " ".join(prompt.strip().split())
    lowered = normalized.lower()
    if not lowered:
        return None
    ref = _infer_market_ref(referenced_message)

    if lowered in {"pm portfolio", "prediction portfolio", "prediction-market portfolio", "my prediction portfolio", "my pm portfolio"}:
        return PredictionMarketDiscordCommand(action="portfolio")
    if lowered in {"pm positions", "prediction positions", "prediction-market positions", "my prediction positions"}:
        return PredictionMarketDiscordCommand(action="positions")
    if lowered in {"pm leaderboard", "prediction leaderboard", "prediction-market leaderboard", "leaderboard pm"}:
        return PredictionMarketDiscordCommand(action="leaderboard")
    if lowered in {"pm settlement sweep", "pm settle sweep", "prediction settlement sweep"}:
        return PredictionMarketDiscordCommand(action="settlement_sweep")

    match = re.match(r"^(?:pm\s+)?(?:search|find)\s+(?:prediction\s+markets?\s+(?:for|on)\s+)?(.+)$", normalized, flags=re.IGNORECASE)
    if match:
        return PredictionMarketDiscordCommand(action="search", query=match.group(1).strip())
    match = re.match(r"^(?:search|find)\s+prediction\s+markets?\s+(?:for|on)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if match:
        return PredictionMarketDiscordCommand(action="search", query=match.group(1).strip())

    match = re.match(r"^confirm\s+pm\s+([a-zA-Z0-9_:-]+)$", normalized, flags=re.IGNORECASE)
    if match:
        return PredictionMarketDiscordCommand(action="confirm", draft_id=match.group(1))
    match = re.match(r"^cancel\s+pm\s+([a-zA-Z0-9_:-]+)$", normalized, flags=re.IGNORECASE)
    if match:
        return PredictionMarketDiscordCommand(action="cancel", draft_id=match.group(1))
    match = re.match(r"^pm\s+close\s+([a-zA-Z0-9_:-]+)$", normalized, flags=re.IGNORECASE)
    if match:
        return PredictionMarketDiscordCommand(action="close", position_ref=match.group(1))

    match = re.match(r"^pm\s+settle\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+(?:\.\d+)?)$", normalized, flags=re.IGNORECASE)
    if match:
        outcome_id = None if match.group(3) in {"-", "none", "null"} else match.group(3)
        return PredictionMarketDiscordCommand(
            action="settle",
            settlement=PredictionMarketSettlementRequest(
                venue=match.group(1),
                market_id=match.group(2),
                outcome_id=outcome_id,
                settlement_fraction=float(match.group(4)),
                source="admin",
                actor="discord",
            ),
        )

    side = _side(lowered)
    if side is None:
        return None
    if not _looks_like_prediction_market_bet(lowered, ref):
        return None
    stake = _stake(normalized)
    inline_ref = _market_ref_from_text(normalized)
    market_ref = inline_ref or ref
    query = _draft_query(normalized, side=side, market_ref=market_ref)
    if not market_ref and not query:
        return PredictionMarketDiscordCommand(action="draft", error="Missing prediction-market query. Try `pm search <topic>` first.")
    return PredictionMarketDiscordCommand(action="draft", query=query, market_ref=market_ref, side=side, stake_usd=stake)


def format_prediction_market_search(quotes: list[PredictionMarketQuote]) -> str:
    if not quotes:
        return "No fresh prediction markets matched."
    lines = ["**Prediction markets**"]
    for idx, quote in enumerate(quotes[:10], start=1):
        lines.append(
            f"{idx}. `pm:{quote.quote_id}` `{quote.venue}` {quote.question[:140]} "
            f"| {quote.outcome_name or 'YES'} px `{quote.price:.3f}` liq `${float(quote.liquidity_usd or 0):,.0f}`"
        )
    return "\n".join(lines)


def format_prediction_market_portfolio(portfolio: dict[str, Any]) -> str:
    lines = [
        "**Prediction-market paper portfolio**",
        f"Equity: `${float(portfolio.get('equity_usd') or 0):,.2f}` | Cash: `${float(portfolio.get('cash_usd') or 0):,.2f}`",
        f"Realized: `${float(portfolio.get('realized_pnl_usd') or 0):,.2f}` | Unrealized: `${float(portfolio.get('unrealized_pnl_usd') or 0):,.2f}`",
    ]
    positions = portfolio.get("positions") if isinstance(portfolio.get("positions"), list) else []
    if positions:
        lines.append("Open bets:")
        for item in positions[:8]:
            lines.append(
                f"- `{str(item.get('position_id') or '')[:10]}` `{item.get('venue')}` {item.get('side')} "
                f"{str(item.get('question') or '')[:80]} shares `{float(item.get('shares') or 0):.4g}` "
                f"uPnL `${float(item.get('unrealized_pnl_usd') or 0):,.2f}`"
            )
    return "\n".join(lines)


def format_prediction_market_positions(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "No prediction-market paper positions."
    lines = ["**Prediction-market positions**"]
    for item in positions[:15]:
        lines.append(
            f"- `{str(item.get('position_id') or '')[:10]}` `{item.get('venue')}` {item.get('side')} {item.get('status')}: "
            f"{str(item.get('question') or '')[:95]} cost `${float(item.get('cost_usd') or 0):,.2f}` "
            f"PnL `${float((item.get('realized_pnl_usd') if item.get('status') != 'open' else item.get('unrealized_pnl_usd')) or 0):,.2f}`"
        )
    return "\n".join(lines)


def format_prediction_market_leaderboard(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No prediction-market paper leaderboard rows yet."
    lines = ["**Prediction-market leaderboard**", "`Player` | `W-L` | `Open` | `PNL` | `ROI`"]
    for row in rows[:20]:
        lines.append(
            f"<@{row.get('discord_user_id')}> | `{int(row.get('won') or 0)}-{int(row.get('lost') or 0)}` | "
            f"`{int(row.get('open_positions') or 0)}` | `${float(row.get('total_pnl_usd') or 0):,.2f}` | `{float(row.get('roi_pct') or 0):.2f}%`"
        )
    return "\n".join(lines)


def format_prediction_market_result(command: PredictionMarketDiscordCommand, result: dict[str, Any]) -> str:
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    if command.action == "draft":
        if payload.get("error") == "no_match":
            lines = [
                f"No prediction market matched `{payload.get('query') or command.query}` closely enough. No live trade was placed.",
            ]
            suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
            if suggestions:
                lines.append("Related markets:")
                for idx, quote in enumerate(suggestions[:5], start=1):
                    lines.append(
                        f"{idx}. `pm:{quote.get('quote_id')}` `{quote.get('venue')}` {str(quote.get('question') or '')[:120]} "
                        f"| {quote.get('outcome_name') or 'YES'} px `{float(quote.get('price') or 0):.3f}`"
                    )
                lines.append("Use `pm search <topic>` or include one of those `pm:` ids.")
            else:
                lines.append("Try `pm search <topic>` first, then bet with the `pm:` id.")
            return "\n".join(lines)
        draft = payload.get("draft") or {}
        if not draft:
            return "Prediction-market paper draft did not return a draft. No live trade was placed."
        return (
            f"Drafted prediction-market paper bet `{draft.get('draft_id')}`: `{draft.get('venue')}` {draft.get('side')} "
            f"`${float(draft.get('stake_usd') or 0):,.2f}` at `{float(draft.get('price') or 0):.3f}`.\n"
            f"Confirm with `confirm pm {draft.get('draft_id')}`. No live trade was placed."
        )
    if command.action == "confirm":
        position = payload.get("position") or {}
        return (
            f"Confirmed prediction-market paper bet `{str(position.get('position_id') or '')[:10]}`: "
            f"`{position.get('venue')}` {position.get('side')} shares `{float(position.get('shares') or 0):.4g}`. No live trade was placed."
        )
    if command.action == "cancel":
        draft = payload.get("draft") or {}
        return f"Cancelled prediction-market paper draft `{draft.get('draft_id') or command.draft_id}`. No live trade was placed."
    if command.action == "close":
        position = payload.get("position") or {}
        return f"Closed prediction-market paper position `{str(position.get('position_id') or command.position_ref)[:10]}` PnL `${float(position.get('realized_pnl_usd') or 0):,.2f}`."
    if command.action in {"settle", "settlement_sweep"}:
        return f"Applied prediction-market settlement to `{int(payload.get('count') or 0)}` open positions."
    return "Prediction-market paper command completed. No live trade was placed."


def _looks_like_prediction_market_bet(lowered: str, ref: str | None) -> bool:
    if ref:
        return any(term in lowered for term in ("bet", "buy", "paper", "pm", "prediction"))
    return bool(_BETTING_WORD_RE.search(lowered)) and bool(_YES_WORD_RE.search(lowered) or _NO_WORD_RE.search(lowered) or _NATURAL_YES_RE.search(lowered))


def _side(lowered: str) -> Literal["yes", "no"] | None:
    if _YES_WORD_RE.search(lowered):
        return "yes"
    if _NO_WORD_RE.search(lowered):
        return "no"
    if _NATURAL_YES_RE.search(lowered):
        return "yes"
    return None


def _stake(prompt: str) -> float | None:
    match = re.search(rf"\$\s*{_NUMBER}", prompt)
    if match:
        return float(match.group(1))
    match = re.search(rf"\b{_NUMBER}\s*(?:usd|usdc|dollars?)\b", prompt, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _market_ref_from_text(prompt: str) -> str | None:
    hip4_match = _HIP4_REF_RE.search(prompt)
    if hip4_match:
        return hip4_match.group(0)
    match = _PM_REF_RE.search(prompt)
    if not match:
        return None
    ref = match.group(1)
    return ref if ref.startswith("pm_") else f"pm_{ref}" if re.fullmatch(r"[a-f0-9]{10}", ref) else ref


def _infer_market_ref(message: Any) -> str | None:
    if message is None:
        return None
    return _market_ref_from_text(str(getattr(message, "content", "") or ""))


def _draft_query(prompt: str, *, side: str, market_ref: str | None) -> str:
    cleaned = re.sub(rf"\$\s*{_NUMBER}", " ", prompt)
    cleaned = re.sub(rf"\b{_NUMBER}\s*(?:usd|usdc|dollars?)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:bet|buy|paper|portfolio|prediction|market|pm|on|for|my|the|a|an)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(rf"\b{side}\b", " ", cleaned, flags=re.IGNORECASE)
    if market_ref:
        cleaned = re.sub(re.escape(market_ref), " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"pm:[a-zA-Z0-9_:-]+", " ", cleaned)
    return " ".join(cleaned.strip().split())
