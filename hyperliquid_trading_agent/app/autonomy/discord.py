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
    match = re.match(r"^(signal outcome|signal eval|signal evaluation)\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="signal_outcome", signal_id=match.group(2))
    match = re.match(r"^mark\s+signal\s+([a-zA-Z0-9_:-]+)\s+(good|bad|unclear|too_noisy|useful|wrong)$", lowered)
    if match:
        return AutonomyCommand(action="feedback_signal", signal_id=match.group(1), rating=match.group(2))
    match = re.match(r"^feedback\s+signal\s+([a-zA-Z0-9_:-]+)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if match:
        return AutonomyCommand(action="feedback_signal", signal_id=match.group(1), rating="unclear", note=match.group(2))
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
    match = re.match(r"^(approve|confirm)\s+flip\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="approve_flip", signal_id=match.group(2))
    match = re.match(r"^cancel\s+flip\s+([a-zA-Z0-9_:-]+)$", lowered)
    if match:
        return AutonomyCommand(action="reject", signal_id=match.group(1), note="flip_cancelled")
    if lowered in {"daily report", "report daily", "autonomy daily report"}:
        return AutonomyCommand(action="daily_report")
    if lowered in {"weekly report", "report weekly", "autonomy weekly report"}:
        return AutonomyCommand(action="weekly_report")
    if lowered in {"token capital", "token-capital"}:
        return AutonomyCommand(action="token_capital")
    if lowered in {"tuning proposals", "proposals", "tuning"}:
        return AutonomyCommand(action="tuning_proposals")
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
    header = f"🚨 **AI Trading Signal — {signal.symbol} {signal.side.upper()}**"
    if signal.status == "flip_requested":
        header = f"🔁 **AI Trading Signal — {signal.symbol} {signal.side.upper()} (flip requested)**"
    body = (
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
    if signal.status == "flip_requested":
        body += (
            "\n\n**Opposing position will be closed first.** Confirm with:\n"
            f"`approve flip {signal.id}`"
        )
    return f"{header}\n\n{body}"


def format_signal_detail(signal: TradeSignal) -> str:
    return format_signal_alert(signal) + f"\n\nStatus: `{signal.status}` | ID: `{signal.id}`"


def format_flip_request(signal: TradeSignal, *, opposing_position: dict | None, diagnostics: dict | None) -> str:
    opp = opposing_position or {}
    diag = diagnostics or {}
    opp_id = str(opp.get("id") or diag.get("opposing_position_id") or "-")[:8]
    opp_qty = opp.get("quantity", diag.get("opposing_position_quantity"))
    opp_px = opp.get("avg_entry_px") or opp.get("mark_px")
    rr = signal.risk_plan.get("rr")
    rr_text = f"{rr:.2f}" if isinstance(rr, (int, float)) else "n/a"
    tp = f"{signal.take_profit:.6g}" if signal.take_profit else "n/a"
    lines = [
        f"🔁 **Flip requested — {signal.symbol} {signal.side.upper()}** (opposite of open position)",
        f"Opposing paper position `{opp_id}` will be **closed at market** before opening the new side.",
        f"New entry: `{signal.entry:.6g}` | Stop: `{signal.stop:.6g}` | TP: `{tp}` | RR: `{rr_text}`",
    ]
    if opp_qty is not None and opp_px is not None:
        lines.append(f"Closing: qty `{float(opp_qty):.6g}` from avg entry `{float(opp_px):.6g}`.")
    if diag:
        lines.append(
            "Risk context: equity `"
            f"${diag.get('equity_usd', 0):,.2f}` | single-name cap "
            f"`{diag.get('max_single_name_exposure_pct', 0)}%` (${diag.get('max_single_name_exposure_usd', 0):,.2f}) | "
            f"current {signal.symbol} exposure `${diag.get('current_symbol_exposure_usd', 0):,.2f}`."
        )
    lines.append("")
    lines.append("**Human signoff required to open the new side:**")
    lines.append(f"`approve flip {signal.id}`")
    lines.append(f"`reject signal {signal.id}` (also cancels the flip)")
    lines.append("")
    lines.append("No live trade will be placed. The opposing paper position will be closed first; the new side opens only on your approval.")
    return "\n".join(lines)


def format_signal_evaluation(evaluation: dict | None) -> str:
    if not evaluation:
        return "Signal evaluation not found."
    marks = evaluation.get("marks") or []
    lines = [
        f"**Signal outcome — `{evaluation.get('signal_id')}`**",
        f"{evaluation.get('symbol')} {evaluation.get('side')} {evaluation.get('signal_type')} | status `{evaluation.get('status')}` | outcome `{evaluation.get('terminal_outcome')}`",
        f"Entry `{_fmt_num(evaluation.get('entry'))}` | Stop `{_fmt_num(evaluation.get('stop'))}` | TP `{_fmt_num(evaluation.get('take_profit'))}`",
        f"MFE `{_fmt_num(evaluation.get('max_favorable_r'))}R` | MAE `{_fmt_num(evaluation.get('max_adverse_r'))}R` | Marked `{_fmt_num(evaluation.get('realized_or_marked_r'))}R`",
    ]
    if evaluation.get("opportunity_cost_r") is not None:
        lines.append(f"Opportunity cost: `{_fmt_num(evaluation.get('opportunity_cost_r'))}R`")
    if marks:
        lines.append("**Marks:**")
        for mark in marks[:8]:
            lines.append(f"- {mark.get('horizon')}: `{mark.get('status')}` price `{_fmt_num(mark.get('price'))}` R `{_fmt_num(mark.get('r_multiple'))}`")
    lines.append("No live trade was placed.")
    return "\n".join(lines)


def format_memories(items: list[dict], *, title: str = "Memories") -> str:
    if not items:
        return f"No {title.lower()} found."
    lines = [f"**{title}**"]
    for item in items[:12]:
        scope = item.get("scope") or {}
        scope_text = ", ".join(f"{key}={value}" for key, value in scope.items() if value) or "general"
        lines.append(f"- `{item.get('id')}` {item.get('role', 'operator')} `{item.get('validation_status', item.get('status', '-'))}` {scope_text}: {str(item.get('claim') or item.get('issue_or_pattern') or '')[:160]}")
    return "\n".join(lines)


def format_tuning_proposals(items: list[dict]) -> str:
    if not items:
        return "No tuning proposals."
    lines = ["**Tuning proposals — observe/recommend only**"]
    for item in items[:12]:
        lines.append(f"- `{item.get('id')}` `{item.get('status')}` {item.get('title')} | confidence `{_fmt_num(item.get('confidence'))}` | auto-apply `false`")
    return "\n".join(lines)


def format_tuning_proposal(item: dict | None) -> str:
    if not item:
        return "Tuning proposal not found."
    return (
        f"**Tuning proposal `{item.get('id')}` — observe/recommend only**\n"
        f"{item.get('title')}\n"
        f"Status: `{item.get('status')}` | Type: `{item.get('proposal_type')}` | Confidence: `{_fmt_num(item.get('confidence'))}` | Auto-apply: `false`\n"
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


def _fmt_num(value) -> str:
    try:
        return "n/a" if value is None else f"{float(value):.4g}"
    except (TypeError, ValueError):
        return "n/a"


def _minutes_until_expiry(signal: TradeSignal) -> int:
    remaining_ms = max(0, signal.expires_at_ms - signal.created_at_ms)
    return max(1, round(remaining_ms / 60_000))
