from __future__ import annotations

import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.autonomy.schemas import AlphaEventEvaluation, SignalEvaluation, TuningProposal
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.metrics import TUNING_PROPOSALS_CREATED


class TuningProposalService:
    """Observe-and-recommend tuning proposal generator.

    This service never applies changes. It persists exact recommended diffs with
    evidence, risk, blast radius, rollback, expiry, and evaluation window.
    """

    def __init__(self, *, settings: Settings, repository: Any = None, memory_service: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.memory_service = memory_service
        self.proposals: dict[str, TuningProposal] = {}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.autonomy_tuning_proposals_enabled,
            "effective_enabled": self.settings.autonomy_tuning_proposals_effective_enabled,
            "mode": "observe_and_recommend_only",
            "active_count": len([item for item in self.proposals.values() if item.status == "proposed"]),
            "auto_apply_enabled": False,
        }

    async def generate_from_evaluations(self, evaluations: list[SignalEvaluation]) -> list[TuningProposal]:
        if not self.settings.autonomy_tuning_proposals_enabled:
            return []
        grouped: dict[tuple[str, str], list[SignalEvaluation]] = defaultdict(list)
        for evaluation in evaluations:
            if evaluation.status == "complete":
                grouped[(evaluation.symbol, evaluation.signal_type)].append(evaluation)
        proposals: list[TuningProposal] = []
        for (symbol, signal_type), items in grouped.items():
            if len(items) < 3:
                continue
            avg_r = _avg([item.realized_or_marked_r for item in items]) or 0.0
            stop_rate = len([item for item in items if item.terminal_outcome == "stop_hit"]) / len(items)
            missed = _avg([item.opportunity_cost_r for item in items if item.opportunity_cost_r is not None]) or 0.0
            if avg_r <= -0.25 or stop_rate >= 0.5:
                proposals.append(await self._upsert_proposal(_threshold_proposal(settings=self.settings, symbol=symbol, signal_type=signal_type, items=items, avg_r=avg_r, stop_rate=stop_rate)))
            if missed >= 1.0 and len([item for item in items if item.rejected]) >= 2:
                proposals.append(await self._upsert_proposal(_review_rejection_proposal(settings=self.settings, symbol=symbol, signal_type=signal_type, items=items, missed=missed)))
        return proposals

    async def generate_from_event_evaluations(self, evaluations: list[AlphaEventEvaluation]) -> list[TuningProposal]:
        if not self.settings.autonomy_tuning_proposals_enabled:
            return []
        grouped: dict[tuple[str, str, str, str], list[AlphaEventEvaluation]] = defaultdict(list)
        for evaluation in evaluations:
            if evaluation.status == "complete":
                grouped[(evaluation.asset_class, evaluation.event_source, evaluation.event_type, evaluation.sentiment)].append(evaluation)
        proposals: list[TuningProposal] = []
        for scope, items in grouped.items():
            if len(items) < 8:
                continue
            worked_rate = len([item for item in items if item.terminal_outcome == "worked"]) / len(items)
            failed_rate = len([item for item in items if item.terminal_outcome == "failed"]) / len(items)
            if failed_rate >= 0.45:
                proposals.append(await self._upsert_proposal(_event_confirmation_gate_proposal(self.settings, scope, items, failed_rate)))
            if worked_rate >= 0.60:
                proposals.append(await self._upsert_proposal(_event_weight_review_proposal(self.settings, scope, items, worked_rate)))
        return proposals

    async def generate_from_lessons(self) -> list[TuningProposal]:
        if self.memory_service is None or not self.settings.autonomy_tuning_proposals_enabled:
            return []
        candidates = await self.memory_service.list_candidates(status="promoted", limit=200)
        proposals: list[TuningProposal] = []
        for candidate in candidates:
            if not (candidate.get("strategy_affecting") or candidate.get("risk_affecting") or candidate.get("execution_affecting") or candidate.get("capital_allocation_affecting")):
                continue
            proposal = _lesson_review_proposal(self.settings, candidate)
            proposals.append(await self._upsert_proposal(proposal))
        return proposals

    async def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return await self.repository.list_tuning_proposals(status=status, limit=limit)
        items = list(self.proposals.values())
        if status:
            items = [item for item in items if item.status == status]
        return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def get(self, proposal_id: str) -> dict[str, Any] | None:
        if proposal_id in self.proposals:
            return self.proposals[proposal_id].model_dump(mode="json")
        if self._repo_enabled():
            return await self.repository.get_tuning_proposal(proposal_id)
        return None

    async def mark_reviewed(self, proposal_id: str) -> None:
        await self.set_status(proposal_id, "accepted_manually")

    async def reject(self, proposal_id: str) -> None:
        await self.set_status(proposal_id, "rejected")

    async def expire(self, proposal_id: str) -> None:
        await self.set_status(proposal_id, "expired")

    async def set_status(self, proposal_id: str, status: str) -> None:
        proposal = self.proposals.get(proposal_id)
        if proposal is not None:
            self.proposals[proposal_id] = proposal.model_copy(update={"status": status})
        if self._repo_enabled():
            await self.repository.set_tuning_proposal_status(proposal_id, status)
            await self.repository.record_autonomy_event("tuning_proposal_status_changed", actor="autonomy_tuning", payload={"proposal_id": proposal_id, "status": status, "exchange_actions": []})

    async def _upsert_proposal(self, proposal: TuningProposal) -> TuningProposal:
        existing = self._find_existing(proposal)
        if existing is not None:
            return existing
        self.proposals[proposal.id] = proposal
        TUNING_PROPOSALS_CREATED.labels(proposal_type=proposal.proposal_type).inc()
        if self._repo_enabled():
            await self.repository.upsert_tuning_proposal(proposal.model_dump(mode="json"))
            await self.repository.record_autonomy_event("tuning_proposal_created", actor="autonomy_tuning", symbol=proposal.affected_scope.get("symbol"), payload={"proposal_id": proposal.id, "proposal_type": proposal.proposal_type, "auto_apply_enabled": False, "exchange_actions": []})
        return proposal

    def _find_existing(self, proposal: TuningProposal) -> TuningProposal | None:
        for existing in self.proposals.values():
            if existing.status not in {"draft", "proposed"}:
                continue
            if existing.proposal_type == proposal.proposal_type and existing.affected_scope == proposal.affected_scope and existing.proposed_diff == proposal.proposed_diff:
                return existing
        return None

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)


def _threshold_proposal(*, settings: Settings, symbol: str, signal_type: str, items: list[SignalEvaluation], avg_r: float, stop_rate: float) -> TuningProposal:
    now_ms = _now_ms()
    current_min = settings.autonomy_min_signal_score
    proposed = min(95.0, current_min + 7)
    evidence = [_evaluation_evidence(item) for item in items[-10:]]
    return TuningProposal(
        id=f"tp_{uuid4().hex}",
        proposal_type="threshold_change",
        status="proposed",
        title=f"Raise {symbol} {signal_type} minimum score to {proposed:.0f} for 7 days",
        summary=f"{symbol} {signal_type} completed weakly: avg {avg_r:.2f}R, stop rate {stop_rate:.0%}. Recommendation only; do not auto-apply.",
        affected_scope={"symbol": symbol, "signal_type": signal_type},
        current_behavior={"autonomy_min_signal_score": current_min},
        proposed_diff={f"asset_overrides.{symbol}.{signal_type}.min_signal_score": proposed},
        evidence=evidence,
        source_signal_ids=[item.signal_id for item in items],
        expected_impact="Reduce low-quality alerts for this scoped setup while evidence is weak.",
        risk_assessment="May miss valid rebound signals if the sample is regime-specific or too small.",
        blast_radius="low",
        rollback_plan=f"Remove asset override or reset {symbol} {signal_type} min signal score to {current_min}.",
        confidence=min(0.85, 0.55 + len(items) * 0.04 + max(0.0, -avg_r) * 0.1),
        sample_size=len(items),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + settings.autonomy_tuning_proposal_ttl_days * 86_400_000,
        evaluation_window="7d",
        metadata={"observe_and_recommend_only": True, "auto_apply_enabled": False, "exchange_actions": []},
    )


def _review_rejection_proposal(*, settings: Settings, symbol: str, signal_type: str, items: list[SignalEvaluation], missed: float) -> TuningProposal:
    now_ms = _now_ms()
    evidence = [_evaluation_evidence(item) for item in items[-10:]]
    return TuningProposal(
        id=f"tp_{uuid4().hex}",
        proposal_type="data_quality_gate",
        status="proposed",
        title=f"Review rejection criteria for {symbol} {signal_type}",
        summary=f"Rejected/expired {symbol} {signal_type} signals averaged {missed:.2f}R opportunity cost before stop.",
        affected_scope={"symbol": symbol, "signal_type": signal_type, "decision": "rejection_filter"},
        current_behavior={"human_reject_or_expiry": "evaluated but not traded"},
        proposed_diff={"review_required": "Audit rejection reasons before tightening this scoped filter"},
        evidence=evidence,
        source_signal_ids=[item.signal_id for item in items],
        expected_impact="Improve operator calibration by separating good rejections from missed asymmetric setups.",
        risk_assessment="Could overfit to missed opportunities; do not loosen filters without larger sample and human review.",
        blast_radius="low",
        rollback_plan="Keep current rejection process unchanged.",
        confidence=min(0.80, 0.50 + len(items) * 0.04 + missed * 0.05),
        sample_size=len(items),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + settings.autonomy_tuning_proposal_ttl_days * 86_400_000,
        evaluation_window="7d",
        metadata={"observe_and_recommend_only": True, "auto_apply_enabled": False, "exchange_actions": []},
    )


def _lesson_review_proposal(settings: Settings, candidate: dict[str, Any]) -> TuningProposal:
    now_ms = _now_ms()
    return TuningProposal(
        id=f"tp_{uuid4().hex}",
        proposal_type="role_prompt_change" if candidate.get("lesson_type") == "role_behavior" else "risk_rule_change",
        status="proposed",
        title=f"Human-review lesson: {str(candidate.get('claim') or '')[:80]}",
        summary=str(candidate.get("expected_future_behavior_change") or candidate.get("claim") or "Review promoted lesson."),
        affected_scope=dict(candidate.get("scope") or {}),
        current_behavior={"memory_status": candidate.get("status")},
        proposed_diff={"manual_review": candidate.get("expected_future_behavior_change") or candidate.get("claim")},
        evidence=list(candidate.get("evidence") or []),
        source_lesson_ids=[str(candidate.get("id"))],
        source_signal_ids=list(candidate.get("source_signal_ids") or []),
        expected_impact="Convert validated lesson into a manually reviewed prompt/rule/config update if approved.",
        risk_assessment="Strategy/risk/execution/capital-affecting; must not be applied automatically.",
        blast_radius="medium" if candidate.get("risk_affecting") else "low",
        rollback_plan="Do not apply, or revert the manual prompt/rule/config change using the prior version.",
        confidence=float(candidate.get("confidence") or 0),
        sample_size=int(candidate.get("sample_size") or 0),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + settings.autonomy_tuning_proposal_ttl_days * 86_400_000,
        evaluation_window="14d",
        metadata={"observe_and_recommend_only": True, "auto_apply_enabled": False, "exchange_actions": []},
    )


def _event_confirmation_gate_proposal(settings: Settings, scope: tuple[str, str, str, str], items: list[AlphaEventEvaluation], failed_rate: float) -> TuningProposal:
    now_ms = _now_ms()
    asset_class, source, event_type, sentiment = scope
    evidence = [_event_evaluation_evidence(item) for item in items[-12:]]
    return TuningProposal(
        id=f"tp_{uuid4().hex}",
        proposal_type="data_quality_gate",
        status="proposed",
        title=f"Require confirmation for {source} {event_type} {sentiment} catalysts",
        summary=f"{asset_class} catalysts in this scope failed {failed_rate:.0%} of completed evaluations. Recommendation only; no auto-apply.",
        affected_scope={"asset_class": asset_class, "source": source, "event_type": event_type, "sentiment": sentiment},
        current_behavior={"news_event_weighting": "eligible high-signal catalysts can contribute to signal evidence"},
        proposed_diff={"confirmation_required": True, "candidate_change": "reduce standalone catalyst confidence until price/orderflow confirms"},
        evidence=evidence,
        source_signal_ids=[signal_id for item in items for signal_id in item.linked_signal_ids],
        expected_impact="Reduce false-positive catalyst influence while preserving event tracking.",
        risk_assessment="May underweight genuinely important catalysts if the sample is regime-specific; requires manual review and canary.",
        blast_radius="low",
        rollback_plan="Remove the confirmation gate and restore prior catalyst evidence handling.",
        confidence=min(0.85, 0.50 + len(items) * 0.03 + failed_rate * 0.15),
        sample_size=len(items),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + settings.autonomy_tuning_proposal_ttl_days * 86_400_000,
        evaluation_window="14d",
        metadata={"observe_and_recommend_only": True, "requires_change_control": True, "auto_apply_enabled": False, "exchange_actions": []},
    )


def _event_weight_review_proposal(settings: Settings, scope: tuple[str, str, str, str], items: list[AlphaEventEvaluation], worked_rate: float) -> TuningProposal:
    now_ms = _now_ms()
    asset_class, source, event_type, sentiment = scope
    evidence = [_event_evaluation_evidence(item) for item in items[-12:]]
    return TuningProposal(
        id=f"tp_{uuid4().hex}",
        proposal_type="weight_change",
        status="proposed",
        title=f"Review catalyst evidence weight for {source} {event_type} {sentiment}",
        summary=f"{asset_class} catalysts in this scope worked {worked_rate:.0%} of completed evaluations. Recommendation only; do not auto-apply.",
        affected_scope={"asset_class": asset_class, "source": source, "event_type": event_type, "sentiment": sentiment},
        current_behavior={"news_event_weighting": "default deterministic evidence weight"},
        proposed_diff={"review_weight_change": "Consider modest scoped catalyst evidence increase after manual review and canary"},
        evidence=evidence,
        source_signal_ids=[signal_id for item in items for signal_id in item.linked_signal_ids],
        expected_impact="Improve capture of repeatedly validated catalyst classes without changing execution behavior automatically.",
        risk_assessment="Could overfit source/type samples; any change needs tests, approval, canary, monitoring, and rollback.",
        blast_radius="low",
        rollback_plan="Revert scoped catalyst evidence weight to the current default.",
        confidence=min(0.85, 0.50 + len(items) * 0.03 + worked_rate * 0.15),
        sample_size=len(items),
        created_at_ms=now_ms,
        expires_at_ms=now_ms + settings.autonomy_tuning_proposal_ttl_days * 86_400_000,
        evaluation_window="14d",
        metadata={"observe_and_recommend_only": True, "requires_change_control": True, "auto_apply_enabled": False, "exchange_actions": []},
    )


def _event_evaluation_evidence(item: AlphaEventEvaluation) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "evaluation_id": item.id,
        "symbol": item.symbol,
        "asset_class": item.asset_class,
        "event_source": item.event_source,
        "event_type": item.event_type,
        "sentiment": item.sentiment,
        "terminal_outcome": item.terminal_outcome,
        "max_favorable_bps": item.max_favorable_bps,
        "max_adverse_bps": item.max_adverse_bps,
        "max_abs_move_bps": item.max_abs_move_bps,
        "linked_signal_ids": item.linked_signal_ids,
    }


def _evaluation_evidence(item: SignalEvaluation) -> dict[str, Any]:
    return {
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "signal_type": item.signal_type,
        "terminal_outcome": item.terminal_outcome,
        "realized_or_marked_r": item.realized_or_marked_r,
        "max_favorable_r": item.max_favorable_r,
        "max_adverse_r": item.max_adverse_r,
        "opportunity_cost_r": item.opportunity_cost_r,
    }


def _avg(values: list[float | None]) -> float | None:
    clean = [float(item) for item in values if item is not None]
    return sum(clean) / len(clean) if clean else None


def _now_ms() -> int:
    return int(time.time() * 1000)
