from __future__ import annotations

import argparse
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.models import Base
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.governance.cli import _dispatch
from hyperliquid_trading_agent.app.governance.export import ReviewExportService
from hyperliquid_trading_agent.app.governance.routes import register_governance_routes


class FakeReviewExportRepository:
    enabled = True

    def __init__(self) -> None:
        self.proposals = {
            "tp_ready": {
                "proposal_id": "tp_ready",
                "status": "review_ready",
                "strategy_id": "news_event_alpha_v2",
                "risk_direction": "tightens_risk",
                "requires_human_approval": True,
                "auto_apply_allowed": False,
                "current_value": {"threshold": 80, "api_key": "must-not-export"},
                "proposed_value": {"threshold": 85},
                "evidence": ["event_eval_1", "news_1", "missing_1"],
                "metadata": {"decision_id": "dcx_1"},
            },
            "tp_proposed": {
                "proposal_id": "tp_proposed",
                "status": "proposed",
                "risk_direction": "neutral",
                "evidence": ["event_eval_1"],
            },
        }

    async def get_candidate_config_diff(self, proposal_id: str) -> dict[str, Any] | None:
        return self.proposals.get(proposal_id)

    async def list_candidate_config_diffs(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = list(self.proposals.values())
        if status:
            items = [item for item in items if item.get("status") == status]
        return items[:limit]

    async def list_review_packets(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if proposal_id == "tp_proposed":
            return []
        return [
            {
                "review_packet_id": "review_1",
                "proposal_id": "tp_ready",
                "approval_requirements": ["human_approval", "rollback_plan"],
                "rollback_plan_id": "rollback_1",
                "created_at_ms": 10,
            }
        ][:limit]

    async def list_replay_results(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "replay_id": "replay_1",
                "proposal_id": "tp_ready",
                "decision_id": "dcx_1",
                "status": "passed",
                "caveats": ["paper only"],
                "created_at_ms": 8,
                "metadata": {},
            }
        ][:limit]

    async def list_shadow_comparisons(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "comparison_id": "shadow_1",
                "proposal_id": "tp_ready",
                "status": "shadow_passed",
                "recommendation": "promote_to_review",
                "created_at_ms": 9,
            }
        ][:limit]

    async def get_rollback_plan(self, rollback_plan_id: str) -> dict[str, Any] | None:
        return {
            "rollback_plan_id": rollback_plan_id,
            "target_type": "config",
            "target_id": "tp_ready",
            "previous_version_id": "cfg_previous",
            "rollback_steps": ["Restore cfg_previous manually."],
            "verification_steps": ["Verify active config reference."],
        }

    async def list_promotion_decisions(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return []

    async def get_alpha_event_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        if evaluation_id != "event_eval_1":
            return None
        return {
            "id": evaluation_id,
            "event_id": "event_1",
            "event_source": "coindesk",
            "event_type": "headline",
            "symbol": "BTC",
            "direction": "long",
            "status": "complete",
            "terminal_outcome": "worked",
            "realized_or_marked_bps": 120.0,
            "metadata": {"private_key": "must-not-export"},
        }

    async def get_newswire_event(self, event_id: str) -> dict[str, Any] | None:
        if event_id != "news_1":
            return None
        return {
            "event_id": event_id,
            "headline": "Broad crypto market shock",
            "symbols": ["BTC", "ETH"],
            "importance_score": 91,
            "body": "excluded from compact summary",
        }

    async def get_decision_context(self, decision_id: str) -> dict[str, Any] | None:
        return {
            "id": decision_id,
            "config_version_id": "cfg_1",
            "risk_config_version_id": "risk_1",
            "model_route_version_id": "model_1",
            "prompt_version_ids": ["prompt_1"],
        }


class FakeRecorder:
    def active_refs(self) -> dict[str, Any]:
        return {
            "config_version_id": "cfg_1",
            "risk_config_version_id": "risk_1",
            "model_route_version_id": "model_1",
            "prompt_version_ids": ["prompt_1"],
        }


def _require_token(settings: Settings, authorization: str | None) -> None:
    if authorization != "Bearer operator-token":
        raise HTTPException(status_code=401, detail="authentication required")


def _app() -> FastAPI:
    app = FastAPI()
    app.state.repository = FakeReviewExportRepository()
    app.state.decision_context_recorder = FakeRecorder()
    register_governance_routes(app, Settings(environment="test"), _require_token)
    return app


@pytest.mark.asyncio
async def test_review_export_is_complete_redacted_and_non_mutating() -> None:
    bundle = await ReviewExportService(repository=FakeReviewExportRepository()).build(
        "tp_ready", active_refs=FakeRecorder().active_refs()
    )

    assert bundle["candidate_diff"]["current_value"]["api_key"] == "[REDACTED]"
    assert bundle["validation"]["replay_count"] == 1
    assert bundle["validation"]["shadow_count"] == 1
    assert bundle["review"]["approval_requirements"] == ["human_approval", "rollback_plan"]
    assert bundle["rollback_plan"]["previous_version_id"] == "cfg_previous"
    assert bundle["runtime_references"]["active"]["model_route_version_id"] == "model_1"
    assert {item["evidence_type"] for item in bundle["evidence"]["items"]} == {
        "alpha_event_evaluation",
        "newswire_event",
    }
    assert bundle["evidence"]["unresolved_ids"] == ["missing_1"]
    assert bundle["authority"] == {
        "mode": "review_export_only",
        "execution_authority": False,
        "config_mutation_authority": False,
        "auto_apply_allowed": False,
        "apply_performed": False,
        "exchange_actions": [],
    }


@pytest.mark.asyncio
async def test_review_export_rejects_missing_and_not_ready_proposals() -> None:
    service = ReviewExportService(repository=FakeReviewExportRepository())

    with pytest.raises(KeyError):
        await service.build("missing")
    with pytest.raises(PermissionError, match="not review-ready"):
        await service.build("tp_proposed")


def test_review_export_endpoint_is_protected_and_maps_errors() -> None:
    client = TestClient(_app())

    unauthenticated = client.get("/governance/proposals/tp_ready/review-export")
    ready = client.get(
        "/governance/proposals/tp_ready/review-export",
        headers={"Authorization": "Bearer operator-token"},
    )
    not_ready = client.get(
        "/governance/proposals/tp_proposed/review-export",
        headers={"Authorization": "Bearer operator-token"},
    )
    missing = client.get(
        "/governance/proposals/missing/review-export",
        headers={"Authorization": "Bearer operator-token"},
    )

    assert unauthenticated.status_code == 401
    assert ready.status_code == 200
    assert ready.json()["authority"]["apply_performed"] is False
    assert not_ready.status_code == 409
    assert missing.status_code == 404


def test_governance_cli_dispatches_review_export() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/governance/proposals/tp_ready/review-export"
        return httpx.Response(200, json={"proposal_id": "tp_ready", "export_type": "governance_review_bundle"})

    args = argparse.Namespace(command="export-review", proposal_id="tp_ready")
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
        result = _dispatch(client, args)

    assert result["proposal_id"] == "tp_ready"


@pytest.mark.asyncio
async def test_repository_reads_rollback_plans_and_promotion_decisions() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = Repository(async_sessionmaker(engine, expire_on_commit=False))

    await repository.upsert_rollback_plan(
        {
            "rollback_plan_id": "rollback_db_1",
            "target_type": "config",
            "target_id": "tp_db_1",
            "previous_version_id": "cfg_previous",
            "rollback_steps": ["Restore cfg_previous manually."],
            "verification_steps": ["Verify the active reference."],
            "owner": "operator",
            "created_at_ms": 1,
        }
    )
    await repository.upsert_promotion_decision(
        {
            "decision_id": "decision_db_1",
            "proposal_id": "tp_db_1",
            "reviewer": "operator",
            "decision": "rejected",
            "rationale": "paper drill",
            "evidence_reviewed": ["event_1"],
            "tests_reviewed": ["replay_1"],
            "proposer_actor": "autonomy_tuning",
            "approver_actor": "operator",
            "change_control_id": "reject-no-change",
            "approved_contexts": [],
            "rollback_plan_id": "rollback_db_1",
            "created_at_ms": 2,
        }
    )

    rollback = await repository.get_rollback_plan("rollback_db_1")
    decisions = await repository.list_promotion_decisions(proposal_id="tp_db_1")

    assert rollback is not None
    assert rollback["rollback_steps"] == ["Restore cfg_previous manually."]
    assert decisions[0]["decision"] == "rejected"
    assert decisions[0]["evidence_reviewed"] == ["event_1"]
    await engine.dispose()
