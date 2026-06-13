from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSizingResult:
    risk_usd: float
    size_units: float
    notional_usd: float
    invalid: bool = False
    reason: str = ""


def fixed_risk_position_size(account_equity_usd: float, risk_pct: float, entry: float, stop: float) -> PositionSizingResult:
    if account_equity_usd <= 0:
        return PositionSizingResult(0, 0, 0, True, "account_equity_usd must be positive")
    if entry <= 0 or stop <= 0:
        return PositionSizingResult(0, 0, 0, True, "entry and stop must be positive")
    per_unit_risk = abs(entry - stop)
    if per_unit_risk == 0:
        return PositionSizingResult(0, 0, 0, True, "entry and stop cannot be equal")
    risk_usd = account_equity_usd * (risk_pct / 100.0)
    size_units = risk_usd / per_unit_risk
    return PositionSizingResult(risk_usd=risk_usd, size_units=size_units, notional_usd=size_units * entry)
