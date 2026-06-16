from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import (
    AllocationDecision,
    AlphaCandidate,
    DebateDecision,
    EVEstimate,
    EvidencePack,
    RegimeVector,
)


def debate_priority(candidate: AlphaCandidate, ev: EVEstimate, allocation: AllocationDecision, regime: RegimeVector, *, portfolio_equity: float) -> float:
    edge_score = min(abs(ev.net_ev_bps) / 50.0, 1.0)
    uncertainty_score = ev.uncertainty
    capital_score = min(allocation.risk_usd / max(portfolio_equity * 0.01, 1.0), 1.0)
    novelty_score = float(candidate.metadata.get("novelty_score", 0.5))
    conflict_score = float(candidate.metadata.get("conflict_score", 0.0))
    regime_instability = 1.0 - regime.regime_stability_score
    return (
        edge_score
        * max(0.25, uncertainty_score)
        * max(0.25, capital_score)
        * max(0.25, novelty_score)
        * max(0.25, conflict_score)
        * max(0.25, regime_instability)
    )


class EvidencePackBuilder:
    def build(
        self,
        candidate: AlphaCandidate,
        ev: EVEstimate,
        allocation: AllocationDecision,
        regime: RegimeVector,
        *,
        feature_snapshot: dict[str, Any] | None = None,
    ) -> EvidencePack:
        digest = hashlib.sha1(f"{candidate.candidate_id}:{ev.estimate_id}:{allocation.allocation_id}".encode()).hexdigest()[:24]
        return EvidencePack(
            evidence_pack_id="ep_" + digest,
            candidate_id=candidate.candidate_id,
            strategy_id=candidate.strategy_id,
            asset=candidate.asset,
            side=candidate.side,
            horizon=candidate.horizon,
            feature_snapshot_id=candidate.feature_snapshot_id,
            market_regime_snapshot=regime.model_dump(mode="json"),
            orderflow_summary={key: value for key, value in (feature_snapshot or {}).items() if "imbalance" in key or "spread" in key or "depth" in key},
            news_summary={key: value for key, value in (feature_snapshot or {}).items() if "news" in key or "catalyst" in key},
            risk_summary={"ev": ev.model_dump(mode="json"), "allocation": allocation.model_dump(mode="json")},
            historical_analogs=[],
            model_outputs={"ev_estimate_id": ev.estimate_id, "model_version_id": ev.model_version_id},
            known_missing_data=regime.quality_flags,
            data_quality_flags=regime.quality_flags,
            proposed_trade_plan={
                "entry": candidate.proposed_entry,
                "stop": candidate.stop,
                "targets": candidate.targets,
                "allocated_notional_usd": allocation.allocated_notional_usd,
                "exchange_actions": [],
            },
            invalidation_conditions=candidate.invalidation_conditions,
            created_at_ms=now_ms(),
        )


class DebateAdjudicator:
    """EvidencePack-based adjudicator facade.

    Full multi-agent graph integration lands in a later step. This deterministic
    fallback preserves the exact authority boundary: approve/downgrade/block only,
    never relax risk or create execution actions.
    """

    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def adjudicate_fallback(self, pack: EvidencePack) -> DebateDecision:
        risk = pack.risk_summary.get("ev", {}) if isinstance(pack.risk_summary, dict) else {}
        net_ev = float(risk.get("net_ev_bps") or 0.0)
        missing = pack.known_missing_data or pack.data_quality_flags
        if net_ev <= 0:
            decision = "block"
            multiplier = 0.0
            adjustment = -0.35
            reason_codes = ["non_positive_ev"]
        elif len(missing) >= 3:
            decision = "downgrade"
            multiplier = 0.5
            adjustment = -0.18
            reason_codes = ["data_quality_flags"]
        else:
            decision = "approve"
            multiplier = 1.0
            adjustment = 0.0
            reason_codes = ["deterministic_fallback_review"]
        digest = hashlib.sha1(f"{pack.evidence_pack_id}:{decision}:{multiplier}".encode()).hexdigest()[:24]
        result = DebateDecision(
            debate_decision_id="dd_" + digest,
            evidence_pack_id=pack.evidence_pack_id,
            candidate_id=pack.candidate_id,
            decision=decision,  # type: ignore[arg-type]
            confidence_adjustment=adjustment,
            max_size_multiplier=multiplier,
            reason_codes=reason_codes,
            required_invalidation_checks=pack.invalidation_conditions,
            audit_summary=f"Deterministic fallback debate decision: {decision}. No live execution authority granted.",
            role_outputs=[],
            judge_model=None,
            created_at_ms=now_ms(),
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_debate_decision", None)
            if callable(record):
                await record(result.model_dump(mode="json"))
        return result
