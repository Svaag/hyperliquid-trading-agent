from __future__ import annotations

import re
from typing import Protocol

from hyperliquid_trading_agent.app.autonomy.schemas import (
    AutonomyCommand,
    GlobalMarketMap,
    PaperOrder,
    PaperPosition,
    PortfolioSnapshot,
    TradeSignal,
)


class AutonomyAlertSink(Protocol):
    async def send(self, channel_id: str, content: str) -> str | None: ...


class DiscordAutonomyAlertSink:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, channel_id: str, content: str) -> str | None:
        return await self.bot.send_channel_message(channel_id, content)


def parse_autonomy_command(prompt: str) -> AutonomyCommand | None:
    normalized = " ".join(prompt.strip().split())
    lowered = normalized.lower()
    match = re.match(r"^(approve|reject)\s+signal\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action=match.group(1), signal_id=match.group(2))  # type: ignore[arg-type]
    match = re.match(r"^signal\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="signal", signal_id=match.group(1))
    if lowered in {"signals", "active signals", "posted signals"}:
        return AutonomyCommand(action="signals")
    if lowered in {"portfolio", "paper portfolio"}:
        return AutonomyCommand(action="portfolio")
    if lowered in {"positions", "paper positions"}:
        return AutonomyCommand(action="positions")
    if lowered in {"orders", "paper orders"}:
        return AutonomyCommand(action="orders")
    if lowered in {"market map", "market-map", "mental map"}:
        return AutonomyCommand(action="market_map")
    if lowered in {"pause autonomy", "autonomy pause"}:
        return AutonomyCommand(action="pause")
    if lowered in {"resume autonomy", "autonomy resume"}:
        return AutonomyCommand(action="resume")
    return None


def format_signal_alert(signal: TradeSignal) -> str:
    rr = signal.risk_plan.get("rr")
    rr_text = f"{rr:.2f}" if isinstance(rr, (int, float)) else "n/a"
    tp = f"{signal.take_profit:.6g}" if signal.take_profit else "n/a"
    evidence_lines = [f"- {item.category}: {item.label} ({item.value})" for item in signal.evidence[:6]]
    if signal.model_insight:
        summary = str(signal.model_insight.get("summary") or signal.model_insight.get("status") or "attached")
        evidence_lines.append(f"- model insight: {summary[:180]}")
    return (
        f"🚨 **AI Trading Signal — {signal.symbol} {signal.side.upper()}**\n\n"
        f"Score: **{signal.score:.0f}/100** | Confidence: **{signal.confidence:.2f}** | Expires: **{_minutes_until_expiry(signal)}m**\n"
        f"Entry: `{signal.entry:.6g}`\n"
        f"Stop: `{signal.stop:.6g}`\n"
        f"TP: `{tp}`\n"
        f"RR: `{rr_text}`\n\n"
        f"**Why:**\n" + "\n".join(evidence_lines or ["- deterministic evidence unavailable"]) + "\n\n"
        f"**Thesis:** {signal.thesis}\n"
        f"**Invalidation:** {signal.invalidation}\n\n"
        f"**Human signoff required:**\n"
        f"`approve signal {signal.id}`\n"
        f"`reject signal {signal.id}`\n\n"
        f"No live trade will be placed. Approval creates a paper trade only."
    )


def format_signal_detail(signal: TradeSignal) -> str:
    return format_signal_alert(signal) + f"\n\nStatus: `{signal.status}` | ID: `{signal.id}`"


def format_portfolio_snapshot(snapshot: PortfolioSnapshot | None) -> str:
    if snapshot is None:
        return "No paper portfolio snapshot yet."
    sharpe = "n/a" if snapshot.sharpe is None else f"{snapshot.sharpe:.2f}"
    return (
        "**Paper portfolio**\n"
        f"Equity: `${snapshot.equity_usd:,.2f}`\n"
        f"Cash/Treasury: `${snapshot.cash_usd:,.2f}`\n"
        f"Realized PnL: `${snapshot.realized_pnl_usd:,.2f}`\n"
        f"Unrealized PnL: `${snapshot.unrealized_pnl_usd:,.2f}`\n"
        f"Total PnL: `${snapshot.total_pnl_usd:,.2f}`\n"
        f"Gross exposure: `${snapshot.gross_exposure_usd:,.2f}` | Net: `${snapshot.net_exposure_usd:,.2f}`\n"
        f"Max drawdown: `{snapshot.drawdown_pct:.2f}%` | Sharpe: `{sharpe}`"
    )


def format_positions(positions: list[PaperPosition]) -> str:
    if not positions:
        return "No paper positions."
    lines = ["**Paper positions**"]
    for item in positions[:20]:
        lines.append(
            f"- `{item.id[:8]}` {item.symbol} {item.side} {item.status}: qty `{item.quantity:.6g}` entry `{item.avg_entry_px:.6g}` "
            f"mark `{(item.mark_px or item.avg_entry_px):.6g}` uPnL `${item.unrealized_pnl_usd:,.2f}` rPnL `${item.realized_pnl_usd:,.2f}`"
        )
    return "\n".join(lines)


def format_orders(orders: list[PaperOrder]) -> str:
    if not orders:
        return "No paper orders."
    lines = ["**Paper orders**"]
    for item in orders[:20]:
        lines.append(f"- `{item.id[:8]}` {item.symbol} {item.side} {item.status}: qty `{item.quantity:.6g}` fill `{item.filled_px or 0:.6g}` signal `{item.signal_id or '-'}`")
    return "\n".join(lines)


def format_signals(signals: list[TradeSignal]) -> str:
    if not signals:
        return "No autonomy signals."
    lines = ["**Autonomy signals**"]
    for item in signals[:20]:
        lines.append(f"- `{item.id}` {item.symbol} {item.side} {item.status} score `{item.score:.0f}` entry `{item.entry:.6g}` stop `{item.stop:.6g}`")
    return "\n".join(lines)


def format_market_map(market_map: GlobalMarketMap) -> str:
    lines = [f"**Market mental map** — regime `{market_map.risk_regime}`"]
    if market_map.leaders:
        lines.append("Leaders: " + ", ".join(market_map.leaders[:5]))
    if market_map.laggards:
        lines.append("Laggards: " + ", ".join(market_map.laggards[:5]))
    for symbol, state in list(market_map.assets.items())[:12]:
        mid = "n/a" if state.mid is None else f"{state.mid:.6g}"
        lines.append(f"- {symbol}: mid `{mid}` trend `{state.trend}` vol `{state.volatility_regime}` score `{state.regime_score:.0f}`")
    return "\n".join(lines)


def _minutes_until_expiry(signal: TradeSignal) -> int:
    remaining_ms = max(0, signal.expires_at_ms - signal.created_at_ms)
    return max(1, round(remaining_ms / 60_000))
