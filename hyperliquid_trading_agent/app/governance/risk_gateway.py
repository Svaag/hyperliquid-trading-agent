from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.schemas import RiskGatewayDecision


class RiskGateway:
    """Final deterministic gate for paper/live-like trade intents.

    LLMs may recommend; this gateway can only allow, reject, halt, or tighten.
    It never relaxes limits and never creates broker/exchange actions.
    """

    def __init__(self, *, settings: Settings, repository: Any | None = None, decision_context_recorder: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.decision_context_recorder = decision_context_recorder

    async def check_signal(
        self,
        signal: Any,
        *,
        mode: str = "paper",
        ref_px: float | None = None,
        asset_class: str = "crypto",
        portfolio_snapshot: dict[str, Any] | None = None,
        market_snapshot: dict[str, Any] | None = None,
    ) -> RiskGatewayDecision:
        violations: list[dict[str, Any]] = []
        risk_plan = dict(getattr(signal, "risk_plan", {}) or {})
        metadata = dict(getattr(signal, "metadata", {}) or {})
        exchange_actions = list(risk_plan.get("exchange_actions") or metadata.get("exchange_actions") or [])
        if exchange_actions:
            violations.append(_violation("exchange_actions_present", "LLM/system output attempted to carry exchange actions."))
        if mode == "live":
            if self.settings.hyperliquid_exchange_enabled is False and asset_class == "crypto":
                violations.append(_violation("live_crypto_disabled", "Hyperliquid exchange execution is disabled."))
            if self.settings.alpaca_trading_enabled is False and asset_class == "equity":
                violations.append(_violation("live_equity_disabled", "Alpaca trading execution is disabled."))
        entry = _float(getattr(signal, "entry", None))
        stop = _float(getattr(signal, "stop", None))
        side = str(getattr(signal, "side", "") or "")
        if entry is None or entry <= 0:
            violations.append(_violation("invalid_entry", "Entry price must be positive."))
        if stop is None or stop <= 0:
            violations.append(_violation("invalid_stop", "Stop price must be positive."))
        if entry is not None and stop is not None:
            if side == "long" and stop >= entry:
                violations.append(_violation("invalid_long_stop", "Long stop must be below entry."))
            if side == "short" and stop <= entry:
                violations.append(_violation("invalid_short_stop", "Short stop must be above entry."))
        now_ms = _now_ms()
        expires_at = int(getattr(signal, "expires_at_ms", now_ms + 1) or 0)
        if expires_at <= now_ms:
            violations.append(_violation("expired_signal", "Signal is expired."))
        market_snapshot = market_snapshot or {}
        last_market_data_at_ms = _int(market_snapshot.get("last_market_data_at_ms"))
        if last_market_data_at_ms is not None and now_ms - last_market_data_at_ms > 300_000:
            violations.append(_violation("stale_market_data", "Market data is older than 5 minutes."))
        notional = _float(risk_plan.get("notional_usd") or metadata.get("notional_usd"))
        max_single_pct = self.settings.autonomy_paper_max_single_name_exposure_pct if asset_class == "crypto" else self.settings.autonomy_equity_paper_max_single_name_exposure_pct
        equity = _float((portfolio_snapshot or {}).get("equity_usd")) or _float((portfolio_snapshot or {}).get("initial_equity_usd"))
        if notional is not None and equity is not None and equity > 0 and notional > equity * max_single_pct / 100:
            violations.append(_violation("single_name_exposure_limit", "Notional exceeds single-name exposure limit."))
        decision = RiskGatewayDecision(
            decision_id=f"rgd_{uuid4().hex}",
            intent_id=str(getattr(signal, "id", uuid4().hex)),
            mode=mode,  # type: ignore[arg-type]
            decision="reject" if violations else "allow",
            violations=violations,
            limits_snapshot=self._limits_snapshot(asset_class),
            market_snapshot=market_snapshot,
            portfolio_snapshot=portfolio_snapshot or {},
            config_version_id=self._config_version_id(),
            created_at_ms=now_ms,
            metadata={"asset_class": asset_class, "symbol": getattr(signal, "symbol", None), "side": side, "exchange_actions": []},
        )
        await self.record(decision)
        return decision

    async def record(self, decision: RiskGatewayDecision) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_risk_gateway_decision", None)
        if callable(record):
            await record(decision.model_dump(mode="json"))

    def _limits_snapshot(self, asset_class: str) -> dict[str, Any]:
        if asset_class == "equity":
            return {
                "trading_enabled": self.settings.alpaca_trading_enabled,
                "risk_pct_per_trade": self.settings.autonomy_equity_paper_risk_pct_per_trade,
                "max_gross_leverage": self.settings.autonomy_equity_paper_max_gross_leverage,
                "max_single_name_exposure_pct": self.settings.autonomy_equity_paper_max_single_name_exposure_pct,
            }
        return {
            "exchange_enabled": self.settings.hyperliquid_exchange_enabled,
            "risk_pct_per_trade": self.settings.autonomy_paper_risk_pct_per_trade,
            "max_gross_leverage": self.settings.autonomy_paper_max_gross_leverage,
            "max_single_name_exposure_pct": self.settings.autonomy_paper_max_single_name_exposure_pct,
        }

    def _config_version_id(self) -> str | None:
        if self.decision_context_recorder is None:
            return None
        refs = self.decision_context_recorder.active_refs()
        return refs.get("risk_config_version_id")


def _violation(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
