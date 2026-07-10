from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import CandidateTradePacket, CouncilReview, CouncilVote, RegimeVector


class DeterministicCouncil:
    """Role-based deterministic council for candidate trade packets."""

    def review(self, packet: CandidateTradePacket, regime: RegimeVector) -> CouncilReview:
        ts = now_ms()
        review_id = "council_" + hashlib.sha1(f"{packet.packet_id}:{ts}".encode()).hexdigest()[:24]
        votes = [
            self._risk_vote(review_id, packet, ts),
            self._regime_vote(review_id, packet, regime, ts),
            self._replay_vote(review_id, packet, ts),
            self._portfolio_vote(review_id, packet, ts),
            self._microstructure_vote(review_id, packet, regime, ts),
            self._news_event_vote(review_id, packet, regime, ts),
            self._execution_vote(review_id, packet, ts),
        ]
        vetoes = [item for vote in votes for item in vote.vetoes]
        warnings = [item for vote in votes for item in vote.warnings]
        required = [item for vote in votes for item in vote.required_evidence]
        execution_mode = str((packet.order_intent or {}).get("execution_mode") or "shadow")
        if vetoes:
            decision = "reject"
        elif required and execution_mode == "paper":
            decision = "needs_more_evidence"
        else:
            decision = "allow_paper" if execution_mode == "paper" else "allow_shadow"
        regime_fit = _avg_score(votes, "regime_fit")
        strategy_regime = _avg_score(votes, "strategy_regime")
        portfolio_impact = _avg_score(votes, "portfolio_impact")
        return CouncilReview(
            review_id=review_id,
            packet_id=packet.packet_id,
            candidate_id=packet.candidate_id,
            strategy_id=packet.strategy_id,
            decision=decision,  # type: ignore[arg-type]
            vetoes=sorted(set(vetoes)),
            warnings=sorted(set(warnings)),
            required_evidence=sorted(set(required)),
            regime_fit_score=regime_fit,
            strategy_regime_score=strategy_regime,
            portfolio_impact_score=portfolio_impact,
            votes=votes,
            created_at_ms=ts,
            metadata={"deterministic": True, "roles": [vote.role for vote in votes]},
        )

    def _vote(
        self,
        review_id: str,
        role: str,
        decision: str,
        rationale: str,
        ts: int,
        *,
        vetoes: list[str] | None = None,
        warnings: list[str] | None = None,
        required_evidence: list[str] | None = None,
        scores: dict[str, float] | None = None,
    ) -> CouncilVote:
        vote_id = "vote_" + hashlib.sha1(f"{review_id}:{role}".encode()).hexdigest()[:24]
        return CouncilVote(
            vote_id=vote_id,
            review_id=review_id,
            role=role,
            decision=decision,  # type: ignore[arg-type]
            rationale=rationale,
            vetoes=vetoes or [],
            warnings=warnings or [],
            required_evidence=required_evidence or [],
            scores=scores or {},
            created_at_ms=ts,
            metadata={"deterministic": True},
        )

    def _risk_vote(self, review_id: str, packet: CandidateTradePacket, ts: int) -> CouncilVote:
        allowed = packet.risk_decision.get("allowed")
        if allowed is None:
            allowed = packet.risk_decision.get("decision", "allow") == "allow"
        allowed = bool(allowed)
        violations = packet.risk_decision.get("violations") or []
        vetoes = ["risk_gateway_reject"] if not allowed else []
        warnings = [str(item.get("code") or item) for item in violations] if violations else []
        return self._vote(review_id, "Risk Council", "veto" if vetoes else "allow", "RiskGateway result is authoritative.", ts, vetoes=vetoes, warnings=warnings, scores={"portfolio_impact": 0.0 if vetoes else 0.8})

    def _regime_vote(self, review_id: str, packet: CandidateTradePacket, regime: RegimeVector, ts: int) -> CouncilVote:
        candidate = packet.candidate or {}
        valid_regimes = set(candidate.get("valid_regimes") or [])
        labels = _regime_labels(regime)
        match = not valid_regimes or bool(valid_regimes & labels) or _special_regime_match(valid_regimes, labels)
        vetoes = [] if match else ["strategy_invalid_for_current_regime"]
        score = 1.0 if match else 0.0
        return self._vote(review_id, "Regime Council", "allow" if match else "veto", "Checks strategy valid_regimes against deterministic regime labels.", ts, vetoes=vetoes, scores={"regime_fit": score, "strategy_regime": score})

    def _replay_vote(self, review_id: str, packet: CandidateTradePacket, ts: int) -> CouncilVote:
        replay = packet.replay_context or {}
        status = str(replay.get("status") or "missing")
        execution_mode = str((packet.order_intent or {}).get("execution_mode") or "shadow")
        if status in {"passed", "advisory_pass"}:
            return self._vote(review_id, "Replay Council", "allow", "Latest replay context is acceptable.", ts, scores={"strategy_regime": 0.8})
        required = ["latest_replay_pass_or_advisory_pass"]
        vetoes = ["latest_replay_missing_or_failed"] if execution_mode == "paper" else []
        decision = "veto" if vetoes else "needs_more_evidence"
        return self._vote(review_id, "Replay Council", decision, "Replay is required before paper promotion; shadow can continue collecting evidence.", ts, vetoes=vetoes, required_evidence=required, scores={"strategy_regime": 0.4})

    def _portfolio_vote(self, review_id: str, packet: CandidateTradePacket, ts: int) -> CouncilVote:
        allocation = packet.allocation or {}
        metadata = allocation.get("metadata") if isinstance(allocation.get("metadata"), dict) else {}
        diversity = metadata.get("diversity") if isinstance(metadata.get("diversity"), dict) else {}
        reasons = list(allocation.get("reason_codes") or []) + list(diversity.get("reason_codes") or [])
        concentration_reasons = [reason for reason in reasons if "share" in str(reason) or "concentration" in str(reason)]
        projected = diversity.get("projected") if isinstance(diversity.get("projected"), dict) else {}
        report_only = bool(
            projected.get("shadow_observation_report_only")
            or diversity.get("shadow_observation_report_only")
            or "shadow_observation_report_only" in reasons
        )
        enforced_concentration = bool(
            concentration_reasons
            and str(diversity.get("decision") or "allow") == "throttle"
            and not report_only
        )
        vetoes = ["concentration_cap_breach"] if enforced_concentration else []
        status = str(allocation.get("status") or "skip")
        if status not in {"allocate", "reduce", "require_debate"} and not vetoes:
            vetoes.append("allocation_not_approved")
        warnings = list(concentration_reasons)
        if report_only and concentration_reasons:
            warnings.append("shadow_diversity_observation_only")
        return self._vote(review_id, "Portfolio Council", "veto" if vetoes else "allow", "Checks allocation status and enforced diversity controller output.", ts, vetoes=vetoes, warnings=warnings, scores={"portfolio_impact": 0.0 if vetoes else 0.85})

    def _microstructure_vote(self, review_id: str, packet: CandidateTradePacket, regime: RegimeVector, ts: int) -> CouncilVote:
        candidate = packet.candidate or {}
        warnings: list[str] = []
        vetoes: list[str] = []
        if regime.spread_state == "wide" or regime.liquidity_state == "impaired":
            vetoes.append("critical_microstructure_quality")
        coverage = float(candidate.get("feature_coverage_pct") or 0.0)
        if coverage < 50:
            vetoes.append("critical_feature_coverage_missing")
        elif coverage < 95:
            warnings.append("feature_coverage_below_95pct")
        score = max(0.0, min(1.0, coverage / 100.0))
        return self._vote(review_id, "Microstructure Council", "veto" if vetoes else "allow", "Checks spread/liquidity and feature coverage.", ts, vetoes=vetoes, warnings=warnings, scores={"regime_fit": score})

    def _news_event_vote(self, review_id: str, packet: CandidateTradePacket, regime: RegimeVector, ts: int) -> CouncilVote:
        risk_tags = set(packet.candidate.get("risk_tags") or []) if packet.candidate else set()
        warnings: list[str] = []
        if {"news", "event_driven", "catalyst"} & risk_tags and regime.news_state == "no_event":
            warnings.append("news_strategy_without_active_news_state")
        return self._vote(review_id, "News/Event Council", "warn" if warnings else "allow", "Checks event-driven candidates against news state.", ts, warnings=warnings, scores={"strategy_regime": 0.7 if warnings else 0.9})

    def _execution_vote(self, review_id: str, packet: CandidateTradePacket, ts: int) -> CouncilVote:
        intent = packet.order_intent or {}
        vetoes: list[str] = []
        if packet.side == "flat":
            vetoes.append("flat_candidate_must_not_execute")
        if not intent:
            vetoes.append("missing_order_intent")
        if intent and intent.get("execution_mode") not in {"paper", "shadow"}:
            vetoes.append("unsupported_execution_mode")
        return self._vote(review_id, "Execution Council", "veto" if vetoes else "allow", "Checks order intent execution contract.", ts, vetoes=vetoes, scores={"portfolio_impact": 0.0 if vetoes else 0.8})


def build_candidate_trade_packet(
    *,
    candidate: Any,
    ev: Any,
    allocation: Any,
    order_intent: Any | None,
    risk_decision: Any,
    replay_context: dict[str, Any] | None = None,
    created_at_ms: int | None = None,
) -> CandidateTradePacket:
    ts = created_at_ms or now_ms()
    packet_id = "packet_" + hashlib.sha1(f"{candidate.candidate_id}:{allocation.allocation_id}:{ts}".encode()).hexdigest()[:24]
    risk_payload = risk_decision.model_dump(mode="json") if callable(getattr(risk_decision, "model_dump", None)) else dict(risk_decision or {})
    return CandidateTradePacket(
        packet_id=packet_id,
        candidate_id=candidate.candidate_id,
        strategy_id=candidate.strategy_id,
        strategy_version=candidate.strategy_version,
        strategy_family=candidate.strategy_family,
        asset=candidate.asset,
        side=candidate.side,
        horizon=candidate.horizon,
        feature_snapshot_id=candidate.feature_snapshot_id,
        regime_snapshot_id=candidate.regime_snapshot_id,
        candidate=candidate.model_dump(mode="json"),
        ev_estimate=ev.model_dump(mode="json"),
        allocation=allocation.model_dump(mode="json"),
        order_intent=order_intent.model_dump(mode="json") if order_intent is not None else None,
        risk_decision=risk_payload,
        replay_context=replay_context or {},
        created_at_ms=ts,
        metadata={"deterministic": True},
    )


def council_allows_execution(review: CouncilReview, *, execution_mode: str) -> bool:
    if execution_mode == "paper":
        return review.decision == "allow_paper"
    return review.decision in {"allow_shadow", "allow_paper"}


def _regime_labels(regime: RegimeVector) -> set[str]:
    return {
        regime.trend_state,
        regime.volatility_state,
        regime.funding_state,
        regime.oi_state,
        regime.liquidation_state,
        regime.orderflow_state,
        regime.news_state,
        regime.correlation_state,
        regime.session_state,
        regime.regime_label,
    }


def _special_regime_match(valid_regimes: set[str], labels: set[str]) -> bool:
    if "news_catalyst" in valid_regimes and "catalyst" in labels:
        return True
    if "event_risk" in valid_regimes and "catalyst" in labels:
        return True
    if "risk_off" in valid_regimes and ({"extreme", "impaired", "wide", "breakdown"} & labels):
        return True
    return False


def _avg_score(votes: list[CouncilVote], key: str) -> float:
    values = [vote.scores[key] for vote in votes if key in vote.scores]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
