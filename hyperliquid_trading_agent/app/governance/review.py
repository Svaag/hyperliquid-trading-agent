from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.governance.schemas import PromotionDecision, ReviewPacket, RollbackPlan


class ReviewWorkflowService:
    """Human-review workflow for candidate diffs.

    Approval records governance status only; it does not mutate runtime config.
    """

    def __init__(self, *, repository: Any | None = None, shadow_service: Any | None = None):
        self.repository = repository
        self.shadow_service = shadow_service
        self.review_packets: dict[str, ReviewPacket] = {}
        self.decisions: dict[str, PromotionDecision] = {}
        self.rollback_plans: dict[str, RollbackPlan] = {}

    async def create_review_packet(self, proposal_id: str, *, owner: str = "api") -> ReviewPacket:
        diff = await self._load_candidate_diff(proposal_id)
        if diff is None:
            raise KeyError("candidate config diff not found")
        if not diff.get("evidence"):
            raise PermissionError("review packet requires linked evidence")
        replay_result = await self._latest_replay_result(proposal_id)
        shadow_result = await self._latest_shadow_result(proposal_id)
        if replay_result is None or shadow_result is None:
            raise PermissionError("review packet requires replay and shadow evidence")
        if replay_result.get("status") == "insufficient_data" or shadow_result.get("status") == "insufficient_data":
            raise PermissionError("review packet requires sufficient replay and shadow evidence")
        rollback = await self.create_rollback_plan(
            target_type="config",
            target_id=proposal_id,
            previous_version_id=str((diff.get("metadata") or {}).get("previous_version_id") or "manual_current_runtime"),
            owner=owner,
        )
        packet = ReviewPacket(
            review_packet_id=f"review_{uuid4().hex}",
            proposal_id=proposal_id,
            evidence_links=list(diff.get("evidence") or []),
            affected_strategies=[str(diff.get("strategy_id") or "autonomy_v1")],
            affected_symbols=[str(item) for item in (diff.get("scope") or {}).get("symbols", [])] or ([str((diff.get("scope") or {}).get("symbol"))] if (diff.get("scope") or {}).get("symbol") else []),
            affected_venues=[str(item) for item in (diff.get("scope") or {}).get("venues", [])],
            risk_direction=diff.get("risk_direction") or "unknown",
            expected_effect=str(diff.get("expected_effect") or ""),
            known_risks=list(diff.get("known_risks") or []),
            replay_results=replay_result,
            shadow_results=shadow_result,
            approval_requirements=["human_approval", "rollback_plan", "replay_result", "shadow_result", "same_actor_cannot_approve_own_change"],
            rollback_plan_id=rollback.rollback_plan_id,
            created_at_ms=_now_ms(),
        )
        self.review_packets[packet.review_packet_id] = packet
        if self._repo_enabled():
            record = getattr(self.repository, "upsert_review_packet", None)
            if callable(record):
                await record(packet.model_dump(mode="json"))
            set_status = getattr(self.repository, "set_candidate_config_diff_status", None)
            if callable(set_status):
                await set_status(proposal_id, "review_ready")
        return packet

    async def create_rollback_plan(
        self,
        *,
        target_type: str,
        target_id: str,
        previous_version_id: str,
        owner: str,
    ) -> RollbackPlan:
        plan = RollbackPlan(
            rollback_plan_id=f"rollback_{uuid4().hex}",
            target_type=target_type,  # type: ignore[arg-type]
            target_id=target_id,
            previous_version_id=previous_version_id,
            rollback_steps=["Do not auto-apply; restore previous approved version manually if a canary change was deployed."],
            verification_steps=["Verify active config/prompt/risk version matches previous_version_id.", "Confirm risk gateway remains enforce/audit as configured."],
            owner=owner,
            created_at_ms=_now_ms(),
        )
        self.rollback_plans[plan.rollback_plan_id] = plan
        if self._repo_enabled():
            record = getattr(self.repository, "upsert_rollback_plan", None)
            if callable(record):
                await record(plan.model_dump(mode="json"))
        return plan

    async def record_promotion_decision(
        self,
        *,
        proposal_id: str,
        reviewer: str,
        decision: str,
        rationale: str,
        proposer_actor: str,
        approver_actor: str,
        change_control_id: str,
        evidence_reviewed: list[str] | None = None,
        tests_reviewed: list[str] | None = None,
        approved_contexts: list[str] | None = None,
        rollback_plan_id: str | None = None,
    ) -> PromotionDecision:
        if rollback_plan_id is None:
            if decision == "approved":
                packet = await self.create_review_packet(proposal_id, owner=reviewer)
                rollback_plan_id = packet.rollback_plan_id
            else:
                rollback = await self.create_rollback_plan(target_type="config", target_id=proposal_id, previous_version_id="no_change", owner=reviewer)
                rollback_plan_id = rollback.rollback_plan_id
        promotion = PromotionDecision(
            decision_id=f"promote_{uuid4().hex}",
            proposal_id=proposal_id,
            reviewer=reviewer,
            decision=decision,  # type: ignore[arg-type]
            rationale=rationale,
            evidence_reviewed=evidence_reviewed or [],
            tests_reviewed=tests_reviewed or [],
            proposer_actor=proposer_actor,
            approver_actor=approver_actor,
            change_control_id=change_control_id,
            approved_contexts=approved_contexts or [],
            rollback_plan_id=rollback_plan_id,
            created_at_ms=_now_ms(),
        )
        self.decisions[promotion.decision_id] = promotion
        if self._repo_enabled():
            record = getattr(self.repository, "upsert_promotion_decision", None)
            if callable(record):
                await record(promotion.model_dump(mode="json"))
            set_status = getattr(self.repository, "set_candidate_config_diff_status", None)
            if callable(set_status):
                await set_status(proposal_id, "approved" if decision == "approved" else decision)
        return promotion

    async def _latest_replay_result(self, proposal_id: str) -> dict[str, Any] | None:
        if not self._repo_enabled():
            return None
        list_replays = getattr(self.repository, "list_replay_results", None)
        if callable(list_replays):
            items = await list_replays(proposal_id=proposal_id, limit=1)
            return items[0] if items else None
        return None

    async def _latest_shadow_result(self, proposal_id: str) -> dict[str, Any] | None:
        if not self._repo_enabled():
            return None
        list_shadow = getattr(self.repository, "list_shadow_comparisons", None)
        if callable(list_shadow):
            items = await list_shadow(proposal_id=proposal_id, limit=1)
            return items[0] if items else None
        return None

    async def _load_candidate_diff(self, proposal_id: str) -> dict[str, Any] | None:
        if not self._repo_enabled():
            return None
        get_diff = getattr(self.repository, "get_candidate_config_diff", None)
        if callable(get_diff):
            return await get_diff(proposal_id)
        return None

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)


def _now_ms() -> int:
    return int(time.time() * 1000)
