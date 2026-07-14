from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.db.models import (
    AuditEvent,
    ConsumerOffsetRecord,
    NewswireEventRow,
    ServiceHeartbeatRecord,
    WorkerCommandRecord,
    WorldEventRecord,
)
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.runtime import main as runtime_main

SIDE_EFFECT_FLAGS = {
    "NEWSWIRE_ENABLED": "false",
    "WORLD_MODEL_STREAMS_ENABLED": "false",
    "WORLD_MODEL_ADAPTERS_ENABLED": "false",
    "ENGINE_ENABLED": "false",
    "ENGINE_PNL_ATTRIBUTION_ENABLED": "false",
    "POSITION_TRACKING_ENABLED": "false",
    "AUTONOMY_ENABLED": "false",
    "HIP4_ENABLED": "false",
    "ORCHESTRATION_WAVE_SUPERVISOR_ENABLED": "false",
    "LIQUIDATIONS_ENABLED": "false",
    "TRADFI_ENABLED": "false",
    "HYPERLIQUID_WS_ENABLED": "false",
    "DISCORD_BOT_ENABLED": "false",
    "DISCORD_PUBLISHER_ENABLED": "false",
}


def _neutral_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in SIDE_EFFECT_FLAGS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNTIME_PROFILE", "dev")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("VAULT_ENABLED", "false")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    monkeypatch.setenv("NEWSWIRE_NEWS_CHANNEL_ID", "")


def test_api_role_rejects_side_effect_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _neutral_env(monkeypatch)
    with pytest.raises(ValueError, match="SERVICE_ROLE=api must be passive"):
        Settings(service_role=ServiceRole.API, newswire_enabled=True)


def test_worker_role_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    _neutral_env(monkeypatch)
    assert Settings(service_role=ServiceRole.NEWSWIRE, newswire_enabled=True).service_role == ServiceRole.NEWSWIRE
    with pytest.raises(ValueError, match="SERVICE_ROLE=world_model"):
        Settings(service_role=ServiceRole.WORLD_MODEL, newswire_enabled=True)
    with pytest.raises(ValueError, match="SERVICE_ROLE=trader"):
        Settings(service_role=ServiceRole.TRADER, newswire_enabled=True)
    with pytest.raises(ValueError, match="discord_publisher missing"):
        Settings(service_role=ServiceRole.DISCORD_PUBLISHER, discord_publisher_enabled=True)


def test_legacy_runtime_profile_rejected_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _neutral_env(monkeypatch)
    with pytest.raises(ValueError, match="RUNTIME_PROFILE='world_model_live' is deprecated"):
        Settings(service_role=ServiceRole.API, environment="prod", runtime_profile="world_model_live")


def test_create_app_requires_api_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _neutral_env(monkeypatch)
    create_app(Settings(service_role=ServiceRole.API))
    with pytest.raises(RuntimeError, match="SERVICE_ROLE=api"):
        create_app(Settings(service_role=ServiceRole.NEWSWIRE, newswire_enabled=True))


def test_runtime_cli_role_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _neutral_env(monkeypatch)
    monkeypatch.setenv("SERVICE_ROLE", "api")
    with pytest.raises(SystemExit, match="does not match"):
        runtime_main(["newswire"])


class FakeRuntimeRepository:
    def __init__(self) -> None:
        self.commands: dict[str, dict[str, Any]] = {}
        self.heartbeats = [{"service_role": "newswire", "instance_id": "nw-1", "status": "running"}]

    async def list_service_heartbeats(self, service_role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = [item for item in self.heartbeats if service_role is None or item["service_role"] == service_role]
        return items[:limit]

    async def list_worker_commands(self, target_role: str | None = None, status: str | None = None, command_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = list(self.commands.values())
        if target_role is not None:
            items = [item for item in items if item["target_role"] == target_role]
        if status is not None:
            items = [item for item in items if item["status"] == status]
        if command_type is not None:
            items = [item for item in items if item["command_type"] == command_type]
        return items[:limit]

    async def list_consumer_offsets(self, limit: int = 100) -> list[dict[str, Any]]:
        return [{"consumer_name": "world_model:newswire", "source_table": "newswire_events", "last_event_id": "nw_1", "last_event_ts_ms": 1, "updated_at_ms": 2, "metadata": {}}]

    async def get_consumer_offset(self, consumer_name: str) -> dict[str, Any]:
        return {"consumer_name": consumer_name, "source_table": "newswire_events", "last_event_id": None, "last_event_ts_ms": 0, "updated_at_ms": 0, "metadata": {}}

    async def get_worker_command(self, command_id: str) -> dict[str, Any] | None:
        return self.commands.get(command_id)

    async def retry_worker_command(self, command_id: str, requested_by: str = "api") -> dict[str, Any] | None:
        original = self.commands.get(command_id)
        if original is None:
            return None
        return await self.enqueue_worker_command(target_role=original["target_role"], command_type=original["command_type"], payload=original["payload"], requested_by=requested_by, idempotency_key=f"retry:{command_id}:1")

    async def cancel_worker_command(self, command_id: str, cancelled_by: str = "api") -> dict[str, Any] | None:
        command = self.commands.get(command_id)
        if command is not None and command["status"] == "pending":
            command["status"] = "cancelled"
        return command

    async def enqueue_worker_command(
        self,
        *,
        target_role: str,
        command_type: str,
        payload: dict[str, Any],
        requested_by: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        command_id = f"cmd_{len(self.commands) + 1}"
        command = {
            "command_id": command_id,
            "target_role": target_role,
            "command_type": command_type,
            "payload": payload,
            "requested_by": requested_by,
            "idempotency_key": idempotency_key,
            "status": "pending",
        }
        self.commands[command_id] = command
        return command


class FakeProposalRepository(FakeRuntimeRepository):
    async def get_trade_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return None


def test_api_runtime_status_and_command_intent_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    _neutral_env(monkeypatch)
    app = create_app(Settings(service_role=ServiceRole.API))
    repo = FakeProposalRepository()
    app.state.repository = repo
    client = TestClient(app)

    status = client.get("/runtime/status")
    assert status.status_code == 200
    assert status.json()["worker_count"] == 1

    dashboard = client.get("/runtime/dashboard/data")
    assert dashboard.status_code == 200
    assert dashboard.json()["command_health"]["registered_command_count"] >= 1
    assert "health" in dashboard.json()["newsfeed"]
    assert client.get("/runtime/dashboard").status_code == 200
    assert client.get("/runtime/command-registry").json()["count"] >= 1
    assert client.get("/runtime/offsets").json()["count"] == 1

    ask = client.post("/ask", json={"prompt": "what is BTC doing?"})
    assert ask.status_code == 202
    body = ask.json()
    assert body["accepted"] is True
    assert body["target_role"] == "agent"
    assert body["command_type"] == "ask"
    assert client.get(body["status_url"]).json()["payload"] == {"prompt": "what is BTC doing?"}
    assert client.post(f"{body['status_url']}/cancel").json()["status"] == "cancelled"

    pause_tracker = client.post("/tracking/positions/tracker-1/pause")
    assert pause_tracker.status_code == 202
    assert pause_tracker.json()["command_type"] == "tracking_pause"
    assert pause_tracker.json()["target_role"] == "trader"


def test_repository_runtime_helpers() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(ServiceHeartbeatRecord.__table__.create)
            await conn.run_sync(ConsumerOffsetRecord.__table__.create)
            await conn.run_sync(WorkerCommandRecord.__table__.create)
            await conn.run_sync(NewswireEventRow.__table__.create)
            await conn.run_sync(AuditEvent.__table__.create)
        repo = Repository(async_sessionmaker(engine, expire_on_commit=False))

        await repo.upsert_service_heartbeat(service_role="newswire", instance_id="nw-1", status="running", started_at_ms=1, updated_at_ms=2)
        heartbeats = await repo.list_service_heartbeats(service_role="newswire")
        assert heartbeats[0]["status"] == "running"

        await repo.upsert_service_heartbeat(
            service_role="newswire",
            instance_id="nw-2",
            status="running",
            started_at_ms=3,
            updated_at_ms=4,
        )
        await repo.upsert_service_heartbeat(
            service_role="newswire",
            instance_id="nw-1",
            status="running",
            started_at_ms=1,
            updated_at_ms=5,
        )
        current = await repo.list_service_heartbeats(service_role="newswire")
        history = await repo.list_service_heartbeats(service_role="newswire", include_history=True)
        assert [item["instance_id"] for item in current] == ["nw-2"]
        assert {item["instance_id"]: item["status"] for item in history} == {
            "nw-1": "superseded",
            "nw-2": "running",
        }
        await repo.mark_service_stopped("newswire", "nw-2")
        assert await repo.list_service_heartbeats(service_role="newswire") == []

        await repo.update_consumer_offset("world_model:newswire", last_event_id="nw_1", last_event_ts_ms=10)
        offset = await repo.get_consumer_offset("world_model:newswire")
        assert offset["last_event_id"] == "nw_1"

        await repo.record_newswire_event({"event_id": "nw_1", "source": "alpaca", "provider": "benzinga", "transport": "websocket", "headline": "BTC pops", "symbols": ["BTC"], "received_at_ms": 1})
        await repo.record_newswire_event({"event_id": "nw_1", "source": "alpaca", "provider": "benzinga", "transport": "websocket", "headline": "BTC updated", "symbols": ["BTC"], "received_at_ms": 2, "action": "updated"})
        stored_event = await repo.get_newswire_event("nw_1")
        assert stored_event is not None and stored_event["headline"] == "BTC updated"

        command = await repo.enqueue_worker_command(target_role="agent", command_type="ask", payload={"prompt": "hi"}, idempotency_key="ask-hi")
        duplicate = await repo.enqueue_worker_command(target_role="agent", command_type="ask", payload={"prompt": "hi"}, idempotency_key="ask-hi")
        assert duplicate["command_id"] == command["command_id"]
        auto_keyed = await repo.enqueue_worker_command(target_role="trader", command_type="engine_operator_proposal_ack", payload={"proposal_id": "eng_prop_1"})
        duplicate_auto_keyed = await repo.enqueue_worker_command(target_role="trader", command_type="engine_operator_proposal_ack", payload={"proposal_id": "eng_prop_1"})
        assert duplicate_auto_keyed["command_id"] == auto_keyed["command_id"]
        claimed = await repo.claim_next_worker_command(target_role="agent", instance_id="agent-1", stale_after_ms=300_000)
        assert claimed is not None
        await repo.complete_worker_command(claimed["command_id"], result={"ok": True})
        completed = await repo.get_worker_command(claimed["command_id"])
        assert completed is not None and completed["status"] == "completed"
        assert [item["event"] for item in completed["metadata"]["history"]] == ["enqueued", "claimed", "completed"]
        claimed_auto = await repo.claim_next_worker_command(target_role="trader", instance_id="trader-1", stale_after_ms=300_000)
        assert claimed_auto is not None
        cancelling = await repo.cancel_worker_command(claimed_auto["command_id"], cancelled_by="operator")
        assert cancelling is not None and cancelling["status"] == "cancelling"
        await repo.complete_worker_command(claimed_auto["command_id"], result={"ok": True})
        cancelled = await repo.get_worker_command(claimed_auto["command_id"])
        assert cancelled is not None and cancelled["status"] == "cancelled"
        retried = await repo.retry_worker_command(cancelled["command_id"], requested_by="operator")
        assert retried is not None and retried["metadata"]["retry_of"] == cancelled["command_id"]
        await engine.dispose()

    anyio.run(run)


def test_world_model_upserts_are_retry_safe_under_duplicate_writes(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'world-model.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(WorldEventRecord.__table__.create)
        repo = Repository(async_sessionmaker(engine, expire_on_commit=False))

        async def write(index: int) -> None:
            await repo.upsert_world_event(
                {
                    "event_id": "wm_event_1",
                    "source_type": "newswire",
                    "source": "test",
                    "provider": "test",
                    "event_type": "headline",
                    "symbols": ["BTC"],
                    "title": f"headline {index}",
                    "received_ts_ms": 100 + index,
                    "computed_ts_ms": 100 + index,
                }
            )

        await asyncio.gather(*(write(index) for index in range(20)))
        events = await repo.list_world_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_id"] == "wm_event_1"
        await engine.dispose()

    anyio.run(run)


def test_compose_single_public_api_port() -> None:
    text = Path("docker-compose.yml").read_text()
    config = yaml.safe_load(text)
    services = config["services"]
    app_services = {name: svc for name, svc in services.items() if name not in {"postgres", "vault", "migrate"}}
    services_with_ports = [name for name, svc in app_services.items() if svc.get("ports")]
    assert services_with_ports == ["api"]
    assert "WORLD_MODEL_LIVE_HOST_PORT" not in text
    assert "8091" not in text
    assert all("SERVICE_ROLE" in svc.get("environment", {}) for svc in app_services.values())
    newswire_enabled = [name for name, svc in app_services.items() if svc.get("environment", {}).get("NEWSWIRE_ENABLED") == "true"]
    assert newswire_enabled == ["newswire"]
