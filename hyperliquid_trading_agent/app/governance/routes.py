from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.review import ReviewWorkflowService
from hyperliquid_trading_agent.app.governance.shadow import ShadowComparisonService


class PromotionDecisionRequest(BaseModel):
    reviewer: str = "api"
    decision: str = "approved"
    rationale: str = ""
    proposer_actor: str = "autonomy_tuning"
    approver_actor: str = "api"
    change_control_id: str = ""
    evidence_reviewed: list[str] = []
    tests_reviewed: list[str] = []
    approved_contexts: list[str] = []


def register_governance_routes(app: FastAPI, settings: Settings, require_auth: Callable[[Settings, str | None], None]) -> None:
    @app.get("/governance/config/active")
    async def governance_active_config(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        recorder = getattr(app.state, "decision_context_recorder", None)
        return recorder.active_refs() if recorder is not None else {}

    @app.get("/governance/decisions/{decision_id}")
    async def governance_decision(decision_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        item = await repository.get_decision_context(decision_id)
        if item is None:
            raise HTTPException(status_code=404, detail="decision context not found")
        return item

    @app.get("/governance/proposals")
    async def governance_proposals(status: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        if getattr(repository, "enabled", False):
            items = await repository.list_candidate_config_diffs(status=status, limit=limit)
        else:
            tuning_service = getattr(app.state, "tuning_service", None)
            raw = await tuning_service.list(status=status, limit=limit) if tuning_service is not None else []
            items = [(item.get("metadata") or {}).get("candidate_config_diff", item) for item in raw]
        return {"items": items, "count": len(items)}

    @app.get("/governance/proposals/{proposal_id}")
    async def governance_proposal(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        item = await repository.get_candidate_config_diff(proposal_id) if getattr(repository, "enabled", False) else None
        if item is None:
            tuning_service = getattr(app.state, "tuning_service", None)
            raw = await tuning_service.get(proposal_id) if tuning_service is not None else None
            item = (raw.get("metadata") or {}).get("candidate_config_diff") if raw else None
        if item is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return item

    @app.post("/governance/proposals/{proposal_id}/request-replay")
    async def governance_request_replay(proposal_id: str, decision_id: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ShadowComparisonService = app.state.shadow_service
        result = await service.replay_candidate_diff(proposal_id, decision_id=decision_id)
        return result.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/request-shadow")
    async def governance_request_shadow(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ShadowComparisonService = app.state.shadow_service
        result = await service.compare_candidate_diff(proposal_id)
        return result.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/review-packet")
    async def governance_review_packet(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ReviewWorkflowService = app.state.review_service
        packet = await service.create_review_packet(proposal_id)
        return packet.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/approve")
    async def governance_approve(proposal_id: str, request: PromotionDecisionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ReviewWorkflowService = app.state.review_service
        decision = await service.record_promotion_decision(proposal_id=proposal_id, reviewer=request.reviewer, decision="approved", rationale=request.rationale, proposer_actor=request.proposer_actor, approver_actor=request.approver_actor, change_control_id=request.change_control_id, evidence_reviewed=request.evidence_reviewed, tests_reviewed=request.tests_reviewed, approved_contexts=request.approved_contexts)
        return decision.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/reject")
    async def governance_reject(proposal_id: str, request: PromotionDecisionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ReviewWorkflowService = app.state.review_service
        decision = await service.record_promotion_decision(proposal_id=proposal_id, reviewer=request.reviewer, decision="rejected", rationale=request.rationale, proposer_actor=request.proposer_actor, approver_actor=request.approver_actor, change_control_id=request.change_control_id or "reject-no-change", evidence_reviewed=request.evidence_reviewed, tests_reviewed=request.tests_reviewed)
        return decision.model_dump(mode="json")

    @app.get("/governance/memories")
    async def governance_memories(role: str | None = None, status: str | None = "active", authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service = app.state.memory_service
        items = await service.list_lessons(role=role, status=status, include_shadow=status == "shadow", limit=100)
        return {"items": items, "count": len(items)}

    @app.post("/governance/memories/{memory_id}/deprecate")
    async def governance_deprecate_memory(memory_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        await app.state.memory_service.archive_lesson(memory_id)
        return {"memory_id": memory_id, "status": "deprecated"}

    @app.post("/governance/freeze-live")
    async def governance_freeze_live(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        await app.state.repository.record_audit_event("live_trading_frozen", actor="api", payload={"paper_learning_continues": True, "exchange_actions": []})
        return {"live_trading_frozen": True, "paper_learning_continues": True}

    @app.post("/governance/paper-only")
    async def governance_paper_only(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        await app.state.repository.record_audit_event("paper_only_mode_confirmed", actor="api", payload={"exchange_actions": []})
        return {"paper_only": True, "exchange_actions": []}
