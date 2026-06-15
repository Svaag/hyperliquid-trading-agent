from __future__ import annotations

import math
import statistics
from itertools import pairwise
from typing import Any

from hyperliquid_trading_agent.app.autonomy.schemas import PaperPortfolio, PaperPosition, PortfolioSnapshot

HOURS_PER_YEAR_CRYPTO = 24 * 365


def unrealized_pnl(position: PaperPosition, mark: float | None = None) -> float:
    px = mark or position.mark_px or position.avg_entry_px
    if position.side == "long":
        return (px - position.avg_entry_px) * position.quantity
    return (position.avg_entry_px - px) * position.quantity


def exposure_usd(position: PaperPosition, mark: float | None = None) -> float:
    px = mark or position.mark_px or position.avg_entry_px
    return abs(position.quantity * px)


def net_exposure_usd(position: PaperPosition, mark: float | None = None) -> float:
    px = mark or position.mark_px or position.avg_entry_px
    sign = 1 if position.side == "long" else -1
    return sign * position.quantity * px


def aggregate_metrics(portfolio: PaperPortfolio, positions: list[PaperPosition], snapshots: list[PortfolioSnapshot]) -> dict[str, Any]:
    open_positions = [item for item in positions if item.status == "open"]
    closed_positions = [item for item in positions if item.status == "closed"]
    unrealized = sum(unrealized_pnl(item) for item in open_positions)
    equity = portfolio.cash_usd + unrealized
    gross = sum(exposure_usd(item) for item in open_positions)
    net = sum(net_exposure_usd(item) for item in open_positions)
    wins = [item.realized_pnl_usd for item in closed_positions if item.realized_pnl_usd > 0]
    losses = [item.realized_pnl_usd for item in closed_positions if item.realized_pnl_usd < 0]
    return {
        "cash_usd": portfolio.cash_usd,
        "equity_usd": equity,
        "gross_exposure_usd": gross,
        "net_exposure_usd": net,
        "realized_pnl_usd": portfolio.realized_pnl_usd,
        "unrealized_pnl_usd": unrealized,
        "total_pnl_usd": equity - portfolio.initial_equity_usd,
        "return_pct": (equity / portfolio.initial_equity_usd - 1) * 100 if portfolio.initial_equity_usd else 0.0,
        "max_drawdown_pct": max_drawdown_pct(snapshots),
        "sharpe": sharpe_ratio(snapshots),
        "win_rate": len(wins) / len(closed_positions) if closed_positions else None,
        "average_win_usd": statistics.mean(wins) if wins else None,
        "average_loss_usd": statistics.mean(losses) if losses else None,
        "open_position_count": len(open_positions),
        "closed_position_count": len(closed_positions),
        "open_risk_to_stops_usd": open_risk_to_stops(open_positions),
    }


def sharpe_ratio(snapshots: list[PortfolioSnapshot]) -> float | None:
    hourly = _hourly_snapshots(snapshots)
    if len(hourly) < 31:
        return None
    returns: list[float] = []
    for previous, current in pairwise(hourly):
        if previous.equity_usd > 0:
            returns.append((current.equity_usd - previous.equity_usd) / previous.equity_usd)
    if len(returns) < 30:
        return None
    mean = statistics.mean(returns)
    stdev = statistics.pstdev(returns)
    if stdev <= 0:
        return None
    return mean / stdev * math.sqrt(HOURS_PER_YEAR_CRYPTO)


def max_drawdown_pct(snapshots: list[PortfolioSnapshot]) -> float:
    peak = None
    max_dd = 0.0
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp_ms):
        equity = snapshot.equity_usd
        peak = equity if peak is None else max(peak, equity)
        if peak and peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
    return max_dd


def open_risk_to_stops(positions: list[PaperPosition]) -> float:
    risk = 0.0
    for position in positions:
        if position.side == "long":
            risk += max(0.0, (position.avg_entry_px - position.stop_px) * position.quantity)
        else:
            risk += max(0.0, (position.stop_px - position.avg_entry_px) * position.quantity)
    return risk


def _hourly_snapshots(snapshots: list[PortfolioSnapshot]) -> list[PortfolioSnapshot]:
    by_hour: dict[int, PortfolioSnapshot] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp_ms):
        by_hour[snapshot.timestamp_ms // 3_600_000] = snapshot
    return [by_hour[key] for key in sorted(by_hour)]
