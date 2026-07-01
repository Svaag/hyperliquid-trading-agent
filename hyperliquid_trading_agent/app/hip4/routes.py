from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.service import Hip4Service

RequireAuth = Callable[[Settings, str | None], None]


def _accepted_command(command: dict[str, Any]) -> dict[str, Any]:
    command_id = str(command.get("command_id") or "")
    return {
        "accepted": True,
        "command_id": command_id,
        "status_url": f"/commands/{command_id}",
        "target_role": command.get("target_role"),
        "command_type": command.get("command_type"),
        "status": command.get("status"),
    }


def register_hip4_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    def _service() -> Hip4Service:
        service = getattr(app.state, "hip4_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="HIP-4 service unavailable")
        return service

    def _enabled() -> Hip4Service:
        if not settings.hip4_enabled:
            raise HTTPException(status_code=409, detail="HIP-4 subsystem is disabled")
        return _service()

    def _auth(authorization: str | None) -> None:
        require_auth(settings, authorization)

    def _require_scan_mode() -> None:
        if not settings.hip4_mode_allows_scan:
            raise HTTPException(status_code=409, detail="HIP-4 mode does not allow scanning")
        if not settings.hip4_scan_enabled:
            raise HTTPException(status_code=409, detail="HIP-4 scanning is disabled")

    def _require_paper_mode() -> None:
        if not settings.hip4_mode_allows_paper:
            raise HTTPException(status_code=409, detail="HIP-4 mode does not allow paper execution")
        if not settings.hip4_paper_execution_enabled:
            raise HTTPException(status_code=409, detail="HIP-4 paper execution is disabled")

    def _require_manual_ticket_mode() -> None:
        if not settings.hip4_mode_allows_manual_ticket:
            raise HTTPException(status_code=409, detail="HIP-4 mode does not allow manual tickets")

    def _require_paper_capability(service: Hip4Service) -> None:
        capabilities = getattr(service, "capabilities", None)
        if capabilities is None:
            raise HTTPException(status_code=403, detail="HIP-4 capability probe has not run")
        if not bool(getattr(capabilities, "supports_abstract_native_mechanics", getattr(capabilities, "supports_native_action_modeling", False))):
            raise HTTPException(status_code=403, detail="HIP-4 capabilities do not allow paper action modeling")

    def _require_manual_ticket_capability(service: Hip4Service) -> None:
        capabilities = getattr(service, "capabilities", None)
        if capabilities is None:
            raise HTTPException(status_code=403, detail="HIP-4 capability probe has not run")
        if not bool(getattr(capabilities, "supports_manual_ticket_export", False)):
            raise HTTPException(status_code=403, detail="HIP-4 capabilities do not allow manual ticket export")

    @app.get("/hip4/status")
    async def hip4_status() -> dict[str, Any]:
        service = getattr(app.state, "hip4_service", None)
        if service is None:
            return Hip4Service(settings=settings).status()
        return service.status()

    @app.get("/hip4/capabilities")
    async def hip4_capabilities() -> dict[str, Any]:
        service = _enabled()
        if service.capabilities is None:
            return {}
        return service.capabilities.model_dump(mode="json")

    @app.get("/hip4/outcomes")
    async def hip4_outcomes() -> dict[str, Any]:
        service = _enabled()
        items = service.list_outcomes()
        return {"items": items, "count": len(items), "registry": service.registry.status()}

    @app.get("/hip4/questions")
    async def hip4_questions() -> dict[str, Any]:
        service = _enabled()
        items = service.list_questions()
        return {"items": items, "count": len(items), "registry": service.registry.status()}

    @app.get("/hip4/questions/{question_id}")
    async def hip4_question(question_id: int) -> dict[str, Any]:
        service = _enabled()
        item = service.registry.questions.get(question_id)
        if item is None:
            raise HTTPException(status_code=404, detail="HIP-4 question not found")
        return item.model_dump(mode="json")

    @app.get("/hip4/books")
    async def hip4_books() -> dict[str, Any]:
        service = _enabled()
        items = service.list_books()
        return {"items": items, "count": len(items), "market_data": service.ws_manager.status()}

    @app.get("/hip4/edges")
    async def hip4_edges() -> dict[str, Any]:
        service = _enabled()
        items = service.list_edges()
        return {"items": items, "count": len(items), "rejects": service.scanner.last_rejects}

    @app.get("/hip4/paper/portfolio")
    async def hip4_paper_portfolio() -> dict[str, Any]:
        _require_paper_mode()
        return _enabled().paper.snapshot()

    @app.get("/hip4/paper/actions")
    async def hip4_paper_actions() -> dict[str, Any]:
        _require_paper_mode()
        items = _enabled().paper.list_actions()
        return {"items": items, "count": len(items)}

    @app.get("/hip4/paper/fills")
    async def hip4_paper_fills() -> dict[str, Any]:
        _require_paper_mode()
        items = _enabled().paper.list_fills()
        return {"items": items, "count": len(items)}

    @app.get("/hip4/loop/status")
    async def hip4_loop_status() -> dict[str, Any]:
        return _enabled().proactive_loop_status()

    @app.get("/hip4/learning")
    async def hip4_learning() -> dict[str, Any]:
        return _enabled().learning_status()

    async def _enqueue_command(command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        repo = getattr(app.state, "repository", None)
        if repo is None or not callable(getattr(repo, "enqueue_worker_command", None)):
            return {"command_id": f"unpersisted_{command_type}", "target_role": "trader", "command_type": command_type, "status": "accepted_unpersisted"}
        return await repo.enqueue_worker_command(target_role="trader", command_type=command_type, payload=payload, requested_by="api")

    @app.post("/hip4/loop/run-once")
    async def hip4_loop_run_once(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command("hip4_loop_run_once", {"manual": True})
        return _accepted_command(command)

    @app.post("/hip4/scan/run")
    async def hip4_scan_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        _require_scan_mode()
        command = await _enqueue_command("hip4_scan_run", {})
        return _accepted_command(command)

    @app.post("/hip4/paper/execute/{candidate_id}")
    async def hip4_paper_execute(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        _require_paper_mode()
        _require_paper_capability(_enabled())
        command = await _enqueue_command("hip4_paper_execute", {"candidate_id": candidate_id})
        return _accepted_command(command)

    @app.post("/hip4/reconcile/run")
    async def hip4_reconcile_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        command = await _enqueue_command("hip4_reconcile_run", {})
        return _accepted_command(command)

    if settings.hip4_manual_ticket_export_enabled:
        @app.post("/hip4/manual-ticket/{candidate_id}")
        async def hip4_manual_ticket(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
            _auth(authorization)
            _require_manual_ticket_mode()
            _require_manual_ticket_capability(_enabled())
            command = await _enqueue_command("hip4_manual_ticket", {"candidate_id": candidate_id})
            return _accepted_command(command)
