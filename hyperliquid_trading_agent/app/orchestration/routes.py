from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.orchestration.agent_core_trace import AgentCoreTraceEmitter
from hyperliquid_trading_agent.app.orchestration.wave_supervisor import WaveSupervisor, WaveSupervisorRunOptions

RequireAuth = Callable[[Settings, str | None], None]


class WaveSupervisorRunRequest(BaseModel):
    perform_maintenance: bool | None = None
    escalate: bool | None = None


def register_orchestration_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _auth(authorization: str | None) -> None:
        require_auth(settings, authorization)

    def _supervisor() -> WaveSupervisor:
        existing = getattr(app.state, "wave_supervisor", None)
        if isinstance(existing, WaveSupervisor):
            return existing
        repository = getattr(app.state, "repository", None)
        if repository is None:
            raise HTTPException(status_code=503, detail="repository unavailable")
        supervisor = WaveSupervisor(
            settings=settings,
            repository=repository,
            engine_service=getattr(app.state, "engine_service", None),
            trace_emitter=AgentCoreTraceEmitter(settings=settings),
        )
        app.state.wave_supervisor = supervisor
        return supervisor

    async def _scheduler_runtime() -> dict:
        repository = getattr(app.state, "repository", None)
        if repository is None or not callable(getattr(repository, "list_service_heartbeats", None)):
            return {}
        try:
            heartbeats = await repository.list_service_heartbeats(service_role="scheduler", limit=5)
        except TypeError:
            heartbeats = await repository.list_service_heartbeats()
        for heartbeat in heartbeats:
            metadata = heartbeat.get("metadata") if isinstance(heartbeat, dict) else None
            supervisor = metadata.get("wave_supervisor") if isinstance(metadata, dict) else None
            if isinstance(supervisor, dict):
                return {
                    **supervisor,
                    "runtime_source": "scheduler_heartbeat",
                    "runtime_instance_id": heartbeat.get("instance_id"),
                    "runtime_updated_at_ms": heartbeat.get("updated_at_ms"),
                }
        return {}

    async def _latest_run() -> dict | None:
        repository = getattr(app.state, "repository", None)
        latest = getattr(repository, "latest_wave_supervisor_run", None)
        if not callable(latest):
            return None
        return await latest()

    @app.get("/orchestration/wave/status")
    async def wave_orchestration_status(authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        computed = await _supervisor().current_status()
        runtime = await _scheduler_runtime()
        latest_run = await _latest_run()
        return {
            **computed,
            "enabled": bool(runtime.get("enabled", computed.get("enabled"))),
            "running": bool(runtime.get("running", False)),
            "owner_role": "scheduler",
            "runtime": runtime,
            "latest_persisted_run": latest_run,
        }

    @app.post("/orchestration/wave/run-once")
    async def wave_orchestration_run_once(request: WaveSupervisorRunRequest, authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        repo = getattr(app.state, "repository", None)
        if repo is None or not callable(getattr(repo, "enqueue_worker_command", None)):
            return await _supervisor().run_once(
                WaveSupervisorRunOptions(
                    perform_maintenance=bool(request.perform_maintenance),
                    escalate=bool(request.escalate),
                )
            )
        command = await repo.enqueue_worker_command(
            target_role="scheduler",
            command_type="orchestration_wave_run_once",
            payload=request.model_dump(mode="json"),
            requested_by="api",
        )
        command_id = str(command.get("command_id") or "")
        return {"accepted": True, "command_id": command_id, "status_url": f"/commands/{command_id}", "target_role": "scheduler", "command_type": "orchestration_wave_run_once", "status": command.get("status")}

    @app.get("/orchestration/wave/runs")
    async def wave_orchestration_runs(
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _auth(authorization)
        repository = getattr(app.state, "repository", None)
        list_runs = getattr(repository, "list_wave_supervisor_runs", None)
        items = await list_runs(limit=max(1, min(1000, limit))) if callable(list_runs) else []
        return {"items": items, "count": len(items), "owner_role": "scheduler"}
