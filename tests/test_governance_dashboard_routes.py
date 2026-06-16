from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.routes import register_governance_routes


class FakeGovernanceRepository:
    enabled = True

    async def list_candidate_config_diffs(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = [
            {"proposal_id": "tp_ready", "status": "review_ready", "risk_direction": "tightens_risk", "change_type": "threshold", "evidence": ["sig_1"]},
            {"proposal_id": "tp_proposed", "status": "proposed", "risk_direction": "neutral", "change_type": "weight", "evidence": ["sig_2"]},
        ]
        if status:
            items = [item for item in items if item["status"] == status]
        return items[:limit]

    async def get_candidate_config_diff(self, proposal_id: str) -> dict[str, Any] | None:
        for item in await self.list_candidate_config_diffs():
            if item["proposal_id"] == proposal_id:
                return item
        return None

    async def list_replay_results(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = [{"replay_id": "replay_1", "proposal_id": "tp_ready", "status": "passed", "baseline_metrics": {"sample_size": 4}, "candidate_metrics": {"sample_size": 3}, "diffs": {"avg_r": 0.2}, "created_at_ms": 1, "metadata": {}}]
        if proposal_id:
            items = [item for item in items if item["proposal_id"] == proposal_id]
        return items[:limit]

    async def list_shadow_comparisons(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = [{"comparison_id": "shadow_1", "proposal_id": "tp_ready", "status": "shadow_passed", "recommendation": "promote_to_review", "created_at_ms": 1, "metadata": {"replay_id": "replay_1"}}]
        if proposal_id:
            items = [item for item in items if item["proposal_id"] == proposal_id]
        return items[:limit]

    async def list_review_packets(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = [{"review_packet_id": "review_1", "proposal_id": "tp_ready", "risk_direction": "tightens_risk", "rollback_plan_id": "rollback_1"}]
        if proposal_id:
            items = [item for item in items if item["proposal_id"] == proposal_id]
        return items[:limit]

    async def list_risk_gateway_decisions(self, limit: int = 100, decision: str | None = None) -> list[dict[str, Any]]:
        return [{"decision_id": "rgd_1", "intent_id": "sig_1", "decision": decision or "allow", "violations": [], "created_at_ms": 1}]

    async def list_memory_injection_events(self, limit: int = 100, role: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "mie_1", "run_id": "run_1", "role": role or "research", "context_type": "research", "memory_ids": ["mem_1"], "blocked_memory_ids": [], "created_at_ms": 1}]


class FakeRecorder:
    def active_refs(self) -> dict[str, Any]:
        return {"config_version_id": "cfg_1", "risk_config_version_id": "risk_1", "prompt_version_ids": ["prompt_1"]}


def _require_noop(settings: Settings, authorization: str | None) -> None:
    return None


def _app() -> FastAPI:
    app = FastAPI()
    app.state.repository = FakeGovernanceRepository()
    app.state.decision_context_recorder = FakeRecorder()
    register_governance_routes(app, Settings(environment="test"), _require_noop)
    return app


def test_review_ready_endpoint_and_inspection_lists():
    client = TestClient(_app())

    ready = client.get("/governance/proposals/review-ready")
    replays = client.get("/governance/replay-results", params={"proposal_id": "tp_ready"})
    shadows = client.get("/governance/shadow-comparisons", params={"proposal_id": "tp_ready"})

    assert ready.status_code == 200
    assert ready.json()["items"][0]["proposal_id"] == "tp_ready"
    assert replays.json()["items"][0]["replay_id"] == "replay_1"
    assert shadows.json()["items"][0]["comparison_id"] == "shadow_1"


def test_dashboard_html_and_data_endpoint():
    client = TestClient(_app())

    html = client.get("/governance/dashboard")
    data = client.get("/governance/dashboard/data")

    assert html.status_code == 200
    assert "Trading Agent Governance Dashboard" in html.text
    assert data.status_code == 200
    body = data.json()
    assert body["summary"]["review_ready_count"] == 1
    assert body["replay_results"][0]["status"] == "passed"
    assert body["runtime"]["paper_only"] is True
