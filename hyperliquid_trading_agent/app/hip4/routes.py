from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.service import Hip4Service

RequireAuth = Callable[[Settings, str | None], None]


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

    def _require_paper_mode() -> None:
        if not settings.hip4_mode_allows_paper:
            raise HTTPException(status_code=409, detail="HIP-4 mode does not allow paper execution")

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

    @app.post("/hip4/loop/run-once")
    async def hip4_loop_run_once(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        _require_scan_mode()
        service = _enabled()
        if not settings.hip4_scan_enabled:
            raise HTTPException(status_code=409, detail="HIP-4 scanner is disabled")
        return await service.run_proactive_cycle(manual=True)

    @app.post("/hip4/scan/run")
    async def hip4_scan_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        _require_scan_mode()
        service = _enabled()
        if not settings.hip4_scan_enabled:
            raise HTTPException(status_code=409, detail="HIP-4 scanner is disabled")
        items = await service.run_scan()
        return {"items": items, "count": len(items), "rejects": service.scanner.last_rejects}

    @app.post("/hip4/paper/execute/{candidate_id}")
    async def hip4_paper_execute(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        _require_paper_mode()
        service = _enabled()
        _require_paper_capability(service)
        if not settings.hip4_paper_execution_enabled:
            raise HTTPException(status_code=409, detail="HIP-4 paper execution is disabled")
        try:
            return await service.execute_paper_candidate(candidate_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="HIP-4 candidate not found") from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/hip4/reconcile/run")
    async def hip4_reconcile_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        _require_paper_mode()
        return await _enabled().reconcile_paper()

    if settings.hip4_manual_ticket_export_enabled:
        @app.post("/hip4/manual-ticket/{candidate_id}")
        async def hip4_manual_ticket(candidate_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
            _auth(authorization)
            _require_manual_ticket_mode()
            service = _enabled()
            _require_manual_ticket_capability(service)
            try:
                return await service.manual_ticket(candidate_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="HIP-4 candidate not found") from None
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from None
