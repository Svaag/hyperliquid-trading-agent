from __future__ import annotations

import re
from typing import Any, Protocol

from hyperliquid_trading_agent.app.autonomy.schemas import (
    AutonomyCommand,
    GlobalMarketMap,
    PaperOrder,
    PaperPosition,
    PortfolioSnapshot,
)


class AutonomyAlertSink(Protocol):
    async def send(self, channel_id: str, content: str, embeds: list[dict[str, Any]] | None = None) -> str | None: ...


class DiscordAutonomyAlertSink:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, channel_id: str, content: str, embeds: list[dict[str, Any]] | None = None) -> str | None:
        return await self.bot.send_channel_message(channel_id, content, embeds=embeds)


def parse_autonomy_command(prompt: str, referenced_message: Any = None) -> AutonomyCommand | None:
    """Parse non-trading observation, reporting, and paper-portfolio commands."""

    del referenced_message
    normalized = " ".join(prompt.strip().split())
    lowered = normalized.lower()
    match = re.match(r"^(event outcome|event eval|catalyst outcome|catalyst eval)\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="event_outcome", target_id=match.group(2))
    match = re.match(r"^feedback\s+bot\s+(.+)$", normalized, flags=re.IGNORECASE)
    if match:
        return AutonomyCommand(action="feedback_bot", rating="unclear", note=match.group(1))
    match = re.match(r"^memories(?:\s+(analyst|quant|research|risk|treasury|execution|adversary|judge))?$", lowered)
    if match:
        return AutonomyCommand(action="memories", role=match.group(1))
    match = re.match(r"^memory\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="memory", lesson_id=match.group(1))
    match = re.match(r"^tuning\s+proposal\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="tuning_proposal", proposal_id=match.group(1))
    match = re.match(r"^apply\s+tuning\s+proposal\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="apply_tuning_proposal", proposal_id=match.group(1))
    if lowered in {"daily report", "report daily", "autonomy daily report"}:
        return AutonomyCommand(action="daily_report")
    if lowered in {"weekly report", "report weekly", "autonomy weekly report"}:
        return AutonomyCommand(action="weekly_report")
    if lowered in {"token capital", "token-capital"}:
        return AutonomyCommand(action="token_capital")
    if lowered in {"tuning proposals", "proposals", "tuning"}:
        return AutonomyCommand(action="tuning_proposals")
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


def format_event_evaluation(evaluations: list[dict] | None) -> str:
    if not evaluations:
        return "Event evaluation not found. No trade was placed. No strategy setting was changed."
    first = evaluations[0]
    lines = [
        f"**Catalyst outcome — `{first.get('event_id')}`**",
        f"{first.get('event_source')} {first.get('event_type')} | urgency `{first.get('urgency')}` | sentiment `{first.get('sentiment')}`",
    ]
    for item in evaluations[:8]:
        lines.append(
            f"- {item.get('symbol')} {item.get('direction')} status `{item.get('status')}` outcome `{item.get('terminal_outcome')}` | "
            f"MFE `{_fmt_num(item.get('max_favorable_bps'))}` bps | MAE `{_fmt_num(item.get('max_adverse_bps'))}` bps | "
            f"max abs `{_fmt_num(item.get('max_abs_move_bps'))}` bps"
        )
        marks = item.get("marks") or []
        if marks:
            mark_text = ", ".join(
                f"{mark.get('horizon')}:{mark.get('status')}:{_fmt_num(mark.get('direction_adjusted_return_bps'))}bps"
                for mark in marks[:5]
            )
            lines.append(f"  marks: {mark_text}")
    lines.append("No trade was placed. No strategy setting was changed.")
    return "\n".join(lines)


def format_memories(items: list[dict], *, title: str = "Memories") -> str:
    if not items:
        return f"No {title.lower()} found."
    lines = [f"**{title}**"]
    for item in items[:12]:
        scope = item.get("scope") or {}
        scope_text = ", ".join(f"{key}={value}" for key, value in scope.items() if value) or "general"
        lines.append(
            f"- `{item.get('id')}` {item.get('role', 'operator')} "
            f"`{item.get('validation_status', item.get('status', '-'))}` {scope_text}: "
            f"{str(item.get('claim') or item.get('issue_or_pattern') or '')[:160]}"
        )
    return "\n".join(lines)


def format_tuning_proposals(items: list[dict]) -> str:
    if not items:
        return "No tuning proposals."
    lines = ["**Tuning proposals — observe/recommend only**"]
    for item in items[:12]:
        lines.append(
            f"- `{item.get('id')}` `{item.get('status')}` {item.get('title')} | "
            f"confidence `{_fmt_num(item.get('confidence'))}` | auto-apply `false`"
        )
    return "\n".join(lines)


def format_tuning_proposal(item: dict | None) -> str:
    if not item:
        return "Tuning proposal not found."
    return (
        f"**Tuning proposal `{item.get('id')}` — observe/recommend only**\n"
        f"{item.get('title')}\n"
        f"Status: `{item.get('status')}` | Type: `{item.get('proposal_type')}` | "
        f"Confidence: `{_fmt_num(item.get('confidence'))}` | Auto-apply: `false`\n"
        f"Summary: {item.get('summary')}\n"
        f"Proposed diff: `{item.get('proposed_diff')}`\n"
        f"Expected impact: {item.get('expected_impact')}\n"
        f"Risk/blast radius: `{item.get('blast_radius')}` — {item.get('risk_assessment')}\n"
        f"Rollback: {item.get('rollback_plan')}\n"
        "Humans must apply/reject manually. No runtime strategy settings were changed."
    )


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
            f"- `{item.id[:8]}` {item.symbol} {item.side} {item.status}: qty `{item.quantity:.6g}` "
            f"entry `{item.avg_entry_px:.6g}` mark `{(item.mark_px or item.avg_entry_px):.6g}` "
            f"uPnL `${item.unrealized_pnl_usd:,.2f}` rPnL `${item.realized_pnl_usd:,.2f}`"
        )
    return "\n".join(lines)


def format_orders(orders: list[PaperOrder]) -> str:
    if not orders:
        return "No paper orders."
    lines = ["**Paper orders**"]
    for item in orders[:20]:
        lines.append(
            f"- `{item.id[:8]}` {item.symbol} {item.side} {item.status}: "
            f"qty `{item.quantity:.6g}` fill `{item.filled_px or 0:.6g}`"
        )
    return "\n".join(lines)


def format_market_map(market_map: GlobalMarketMap) -> str:
    lines = [f"**Market mental map** — regime `{market_map.risk_regime}`"]
    if market_map.leaders:
        lines.append("Leaders: " + ", ".join(market_map.leaders[:5]))
    if market_map.laggards:
        lines.append("Laggards: " + ", ".join(market_map.laggards[:5]))
    for symbol, state in list(market_map.assets.items())[:12]:
        mid = "n/a" if state.mid is None else f"{state.mid:.6g}"
        lines.append(
            f"- {symbol}: mid `{mid}` trend `{state.trend}` vol `{state.volatility_regime}` score `{state.regime_score:.0f}`"
        )
    return "\n".join(lines)


def _fmt_num(value: Any) -> str:
    try:
        return "n/a" if value is None else f"{float(value):.4g}"
    except (TypeError, ValueError):
        return "n/a"
