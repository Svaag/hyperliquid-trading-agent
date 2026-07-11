from __future__ import annotations

from collections import Counter, deque
from typing import Any

from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate


class ShadowEvidenceAdmissionController:
    """Balance simulated evidence without changing raw candidate generation.

    Every raw candidate and its RiskGateway/Council evidence is persisted first.
    This controller only decides whether an otherwise approved shadow allocation is
    admitted to the downstream simulated-intent sample.
    """

    def __init__(self, settings: Any):
        self.settings = settings
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.reason_counts: Counter[str] = Counter()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.settings, "engine_shadow_evidence_admission_enabled", True)),
            "reason_counts": dict(self.reason_counts),
            "recent_events": list(self.events)[-20:],
            "settings": {
                "lookback_intents": int(getattr(self.settings, "engine_shadow_evidence_lookback_intents", 100)),
                "strategy_target_share_pct": float(getattr(self.settings, "engine_shadow_evidence_strategy_share_pct", 45.0)),
                "family_hard_share_pct": float(getattr(self.settings, "engine_shadow_evidence_family_share_pct", 60.0)),
                "symbol_strategy_hard_share_pct": float(getattr(self.settings, "engine_shadow_evidence_symbol_strategy_share_pct", 35.0)),
            },
        }

    async def admit(
        self,
        candidate: AlphaCandidate,
        *,
        repository: Any,
        timestamp_ms: int,
        alternative_strategy_available: bool,
        alternative_family_available: bool,
    ) -> tuple[bool, dict[str, Any]]:
        if not bool(getattr(self.settings, "engine_shadow_evidence_admission_enabled", True)):
            return True, {"decision": "admit", "reason": "controller_disabled"}
        if not bool(getattr(self.settings, "engine_shadow_enabled", True)) or bool(getattr(self.settings, "engine_paper_enabled", False)):
            return True, {"decision": "admit", "reason": "not_shadow_only"}
        if candidate.side == "flat":
            return False, {"decision": "observe_only", "reason": "defensive_no_trade_control"}
        if not alternative_strategy_available and not alternative_family_available:
            return True, {"decision": "admit", "reason": "quota_override_no_alternative"}

        lookback = max(10, int(getattr(self.settings, "engine_shadow_evidence_lookback_intents", 100)))
        method = getattr(repository, "list_order_intents", None)
        rows: list[dict[str, Any]] = []
        if callable(method):
            try:
                rows = await method(execution_mode="shadow", limit=lookback)
            except TypeError:
                rows = await method(limit=lookback)
                rows = [item for item in rows if item.get("execution_mode") == "shadow"]
        rows = rows[:lookback]
        total = len(rows)
        if total < 10:
            return True, {"decision": "admit", "reason": "warmup", "observed_intents": total}

        strategy_counts = Counter(str(item.get("strategy_id") or "unknown") for item in rows)
        family_counts = Counter(str((item.get("metadata") or {}).get("strategy_family") or "unknown") for item in rows)
        symbol_strategy_counts = Counter(
            f"{str(item.get('asset') or 'UNKNOWN').upper()}:{item.get('strategy_id') or 'unknown'!s}"
            for item in rows
        )
        projected_total = total + 1
        strategy_share = (strategy_counts[candidate.strategy_id] + 1) / projected_total * 100.0
        family_share = (family_counts[candidate.strategy_family] + 1) / projected_total * 100.0
        symbol_strategy_key = f"{candidate.asset}:{candidate.strategy_id}"
        symbol_strategy_share = (symbol_strategy_counts[symbol_strategy_key] + 1) / projected_total * 100.0
        metadata = {
            "observed_intents": total,
            "projected_strategy_share_pct": round(strategy_share, 4),
            "projected_family_share_pct": round(family_share, 4),
            "projected_symbol_strategy_share_pct": round(symbol_strategy_share, 4),
        }
        reasons: list[str] = []
        if alternative_strategy_available and strategy_share > float(getattr(self.settings, "engine_shadow_evidence_strategy_share_pct", 45.0)):
            reasons.append("strategy_evidence_quota")
        if alternative_family_available and family_share > float(getattr(self.settings, "engine_shadow_evidence_family_share_pct", 60.0)):
            reasons.append("family_evidence_quota")
        if alternative_strategy_available and symbol_strategy_share > float(getattr(self.settings, "engine_shadow_evidence_symbol_strategy_share_pct", 35.0)):
            reasons.append("symbol_strategy_evidence_quota")
        if not reasons:
            return True, {**metadata, "decision": "admit", "reason": "within_quota"}
        for reason in reasons:
            self.reason_counts[reason] += 1
        event = {
            **metadata,
            "decision": "defer",
            "reasons": reasons,
            "candidate_id": candidate.candidate_id,
            "strategy_id": candidate.strategy_id,
            "strategy_family": candidate.strategy_family,
            "asset": candidate.asset,
            "timestamp_ms": timestamp_ms,
        }
        self.events.append(event)
        return False, event
