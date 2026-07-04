from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.paper.schemas import PaperTradeDraftRequest

_NUMBER = r"(\d+(?:\.\d+)?)"
_SYMBOL = r"([A-Za-z][A-Za-z0-9:_-]{1,31})"
_RESERVED_SYMBOLS = {
    "ACCOUNT",
    "ENTRY",
    "MARKET",
    "ORDER",
    "PAPER",
    "PORTFOLIO",
    "PRESSURE",
    "QTY",
    "RISK",
    "SIZE",
    "STOP",
    "TRADE",
}


class PaperDiscordCommand(BaseModel):
    action: Literal["draft", "confirm", "cancel", "close", "portfolio", "positions", "orders", "council_send"]
    order_id: str | None = None
    position_ref: str | None = None
    proposal_id: str | None = None
    draft: PaperTradeDraftRequest | None = None
    symbol: str | None = None
    side: Literal["long", "short"] | None = None
    actor: str = "discord"
    price: float | None = Field(default=None, gt=0)
    risk_pct: float | None = Field(default=None, gt=0)
    reason: str = ""
    close_opposite: bool = False
    error: str = ""
    raw_prompt: str = ""


def parse_paper_discord_command(prompt: str) -> PaperDiscordCommand | None:
    normalized = " ".join(prompt.strip().split())
    lowered = normalized.lower()
    if not lowered:
        return None
    if lowered in {"paper portfolio", "portfolio paper"}:
        return PaperDiscordCommand(action="portfolio")
    if lowered in {"paper positions", "positions paper"}:
        return PaperDiscordCommand(action="positions")
    if lowered in {"paper orders", "orders paper"}:
        return PaperDiscordCommand(action="orders")

    match = re.match(r"^confirm\s+paper\s+([a-zA-Z0-9_:-]+)(?:\s+close\s+opposite)?$", lowered)
    if match:
        return PaperDiscordCommand(action="confirm", order_id=match.group(1), close_opposite="close opposite" in lowered)
    match = re.match(r"^cancel\s+paper\s+([a-zA-Z0-9_:-]+)(?:\s+(.+))?$", normalized, flags=re.IGNORECASE)
    if match:
        return PaperDiscordCommand(action="cancel", order_id=match.group(1), reason=(match.group(2) or "discord_cancel").strip())
    match = re.match(rf"^paper\s+close\s+([a-zA-Z0-9_:-]+)(?:\s+price\s+{_NUMBER})?(?:\s+(.+))?$", normalized, flags=re.IGNORECASE)
    if match:
        return PaperDiscordCommand(action="close", position_ref=match.group(1), price=_float(match.group(2)), reason=(match.group(3) or "discord_manual_close").strip())
    match = re.match(r"^paper\s+from\s+proposal\s+([a-zA-Z0-9_:-]+)$", normalized, flags=re.IGNORECASE)
    if match:
        return PaperDiscordCommand(action="draft", proposal_id=match.group(1))

    if "paper" not in lowered:
        return None
    side_match = re.search(r"\b(long|short|buy|sell)\b", lowered)
    if side_match is None:
        return None
    side_word = side_match.group(1)
    side = "long" if side_word in {"long", "buy"} else "short"
    symbol = _extract_symbol(normalized, side_word)
    if symbol is None:
        return PaperDiscordCommand(action="draft", error="Missing symbol. Example: `paper long BTC entry 65000 stop 64000 risk 0.25`.")
    entry = _labeled_number(normalized, ("entry", "entry px", "entry price"))
    stop = _labeled_number(normalized, ("stop", "sl", "stop loss", "stop-loss"))
    take_profit = _labeled_number(normalized, ("tp", "take profit", "target", "take-profit"))
    risk_pct = _labeled_number(normalized, ("risk", "risk pct", "risk percent"))
    quantity = _labeled_number(normalized, ("size", "qty", "quantity"))
    market = bool(re.search(r"\bmarket\b", lowered)) or (side_word in {"buy", "sell"} and entry is None)
    close_opposite = "close opposite" in lowered or "flip" in lowered
    if entry is None and stop is None and take_profit is None:
        return PaperDiscordCommand(
            action="council_send",
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            risk_pct=risk_pct,
            close_opposite=close_opposite,
            raw_prompt=normalized,
        )
    if stop is None:
        return PaperDiscordCommand(action="draft", error=f"Missing stop for paper {side} {symbol}. Include `stop <price>`.")
    if entry is None and not market:
        return PaperDiscordCommand(action="draft", error="Missing entry. Include `entry <price>` or `market`.")
    try:
        draft = PaperTradeDraftRequest(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            entry=entry,
            market=market,
            stop=stop,
            take_profit=take_profit,
            risk_pct=risk_pct,
            quantity=quantity,
            thesis=normalized[:1000],
            actor="discord",
            source="manual_discord",
            close_opposite=close_opposite,
        )
    except ValueError as exc:
        return PaperDiscordCommand(action="draft", error=str(exc))
    return PaperDiscordCommand(action="draft", draft=draft, close_opposite=close_opposite)


def format_paper_result(command: PaperDiscordCommand, result: dict) -> str:
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    if command.action == "draft":
        order = payload.get("order") or payload.get("draft") or {}
        if not order:
            return "Paper draft command completed, but no order payload was returned. No live trade was placed."
        return (
            f"Drafted paper order `{str(order.get('id', ''))[:8]}`: {order.get('symbol')} {order.get('side')} "
            f"qty `{float(order.get('quantity') or 0):.6g}` ref `{_fmt(order.get('requested_px'))}` stop `{_fmt(order.get('stop_px'))}`.\n"
            f"Confirm with `confirm paper {order.get('id')}`. No live trade was placed."
        )
    if command.action == "confirm":
        order = payload.get("order") or {}
        position = payload.get("position") or {}
        return (
            f"Confirmed paper order `{str(order.get('id', ''))[:8]}`; opened position `{str(position.get('id', ''))[:8]}` "
            f"{position.get('symbol')} {position.get('side')} qty `{float(position.get('quantity') or 0):.6g}` at `{_fmt(position.get('avg_entry_px'))}`. "
            "No live trade was placed."
        )
    if command.action == "cancel":
        order = payload.get("order") or {}
        return f"Cancelled paper draft `{str(order.get('id') or command.order_id)[:8]}`. No live trade was placed."
    if command.action == "close":
        position = payload.get("position") or {}
        return (
            f"Closed paper position `{str(position.get('id') or command.position_ref)[:8]}` with realized PnL "
            f"`${float(position.get('realized_pnl_usd') or 0):,.2f}`. No live trade was placed."
        )
    return "Paper command completed. No live trade was placed."


def _extract_symbol(prompt: str, side: str) -> str | None:
    patterns = [
        rf"\b{side}\s+(?:on\s+)?{_SYMBOL}\b",
        rf"\bpaper\s+{side}\s+(?:on\s+)?{_SYMBOL}\b",
        rf"\b{side}\s+{_SYMBOL}\s+(?:for|in|into)\s+(?:your\s+|my\s+|the\s+)?paper\s+(?:portfolio|account)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            symbol = match.group(1).upper()
            if symbol not in _RESERVED_SYMBOLS:
                return symbol
    return None


def _labeled_number(prompt: str, labels: tuple[str, ...]) -> float | None:
    alternates = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    match = re.search(rf"\b(?:{alternates})\s*[:=]?\s*\$?{_NUMBER}", prompt, flags=re.IGNORECASE)
    return _float(match.group(1)) if match else None


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt(value: object) -> str:
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return "n/a"
