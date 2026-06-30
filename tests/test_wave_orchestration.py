from __future__ import annotations

import json
import time
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.orchestration.agent_core_trace import AgentCoreTraceEmitter
from hyperliquid_trading_agent.app.orchestration.wave_supervisor import (
    WaveSupervisor,
    WaveSupervisorRunOptions,
    classify_wave_state,
)
from tests.test_engine_readiness import FakeReadinessRepository, FakeReadinessService, readiness_settings


def test_wave_supervisor_classifies_clean_wave1a_as_wave1c_candidate() -> None:
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    service = FakeReadinessService(now_ms=now_ms)
    settings = readiness_settings(_env_file=None)

    async def run() -> dict:
        supervisor = WaveSupervisor(settings=settings, repository=repo, engine_service=service)
        return await supervisor.run_once(WaveSupervisorRunOptions(perform_maintenance=False, escalate=False))

    result = anyio.run(run)

    assert result["status"] == "completed"
    assert result["classification"]["state"] == "wave1c_promotion_candidate"
    assert result["classification"]["promotion_candidate"] is True
    assert result["handoff"]["handoff"]["objective_key"] == "enable-wave1c-controlled-canary-v1"
    assert result["safety"]["direct_config_mutation"] is False
    assert result["safety"]["wave2_enabled"] is False
    assert result["escalation"]["status"] == "skipped"


def test_wave_supervisor_classifies_spine_blocker_for_escalation() -> None:
    now_ms = int(time.time() * 1000)
    repo = FakeReadinessRepository(now_ms=now_ms)
    repo.replay_results = []
    repo.council_reviews = []
    settings = readiness_settings(_env_file=None)

    async def run() -> dict:
        readiness = await __import__("hyperliquid_trading_agent.app.engine.readiness", fromlist=["build_paper_readiness_scorecard"]).build_paper_readiness_scorecard(
            repo,
            settings,
            FakeReadinessService(now_ms=now_ms),
            window_hours=1,
            limit=100,
        )
        return classify_wave_state(settings, readiness, None, service_status={"run_count": 1})

    classification = anyio.run(run)

    assert classification["state"] == "blocked"
    assert classification["handoff_recommended"] is True
    assert classification["blocker_counts"]["spine"] >= 1
    assert any(item["code"] == "replay_comparison_missing" for item in classification["blockers"])


def test_wave_orchestration_routes_are_registered() -> None:
    now_ms = int(time.time() * 1000)
    settings = readiness_settings(_env_file=None)
    app = create_app(settings)
    app.state.repository = FakeReadinessRepository(now_ms=now_ms)
    app.state.engine_service = FakeReadinessService(now_ms=now_ms)
    client = TestClient(app)

    status_response = client.get("/orchestration/wave/status")
    assert status_response.status_code == 200
    assert status_response.json()["current"]["classification"]["state"] == "wave1c_promotion_candidate"

    run_response = client.post("/orchestration/wave/run-once", json={"perform_maintenance": False, "escalate": False})
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["status"] == "completed"
    assert payload["handoff"]["schema_version"] == "hyperliquid.wave_lhp.v1"
    assert payload["handoff"]["lhp_compatibility"]["bounded_payload"] is True


def test_agent_core_trace_emitter_is_optional_and_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    settings = Settings(environment="test", _env_file=None, agent_core_trace_enabled=True, agent_core_trace_path=str(path))
    emitter = AgentCoreTraceEmitter(settings=settings)

    assert emitter.emit("unit_event", "hello", payload={"secret_token": "do-not-write", "ok": True}, run_id="run-1") is True

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "unit_event"
    assert event["payload"]["secret_token"] == "[redacted]"
