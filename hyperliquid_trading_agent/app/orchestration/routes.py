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

    @app.get("/orchestration/wave/status")
    async def wave_orchestration_status(authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        return await _supervisor().current_status()

    @app.post("/orchestration/wave/run-once")
    async def wave_orchestration_run_once(request: WaveSupervisorRunRequest, authorization: str | None = Header(default=None)) -> dict:
        _auth(authorization)
        supervisor = _supervisor()
        perform_maintenance = supervisor.settings.orchestration_wave_supervisor_maintenance_enabled if request.perform_maintenance is None else request.perform_maintenance
        escalate = supervisor.settings.orchestration_wave_supervisor_escalation_enabled if request.escalate is None else request.escalate
        return await supervisor.run_once(WaveSupervisorRunOptions(perform_maintenance=perform_maintenance, escalate=escalate))
