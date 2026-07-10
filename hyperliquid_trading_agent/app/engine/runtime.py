from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.config import ServiceRole, Settings


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def resolve_engine_runtime(
    repository: Any,
    settings: Settings,
    local_service: Any | None = None,
    *,
    generated_at_ms: int | None = None,
) -> dict[str, Any]:
    """Resolve the canonical engine runtime across split service roles.

    A trader-local service is authoritative. Passive API/scheduler processes use the
    freshest trader heartbeat and retain stale runtime evidence so callers can report
    a real run count while still blocking readiness on staleness.
    """

    now_ms = int(generated_at_ms or time.time() * 1000)
    local = (
        dict(local_service.status())
        if local_service is not None and callable(getattr(local_service, "status", None))
        else {}
    )
    local_is_authoritative = bool(
        _int(local.get("last_run_at_ms"), 0) > 0
        or _int(local.get("run_count"), 0) > 0
        or (settings.service_role == ServiceRole.TRADER and local.get("enabled"))
    )
    if local_is_authoritative:
        return {
            **local,
            "runtime_source": "local_trader_service",
            "runtime_running": True,
            "runtime_stale": False,
            "runtime_age_ms": max(0, now_ms - _int(local.get("last_run_at_ms"), now_ms)),
        }

    method = getattr(repository, "list_service_heartbeats", None)
    if callable(method):
        try:
            heartbeats = await method(service_role="trader", limit=20)
        except TypeError:
            try:
                heartbeats = await method()
            except Exception:
                heartbeats = []
        except Exception:
            heartbeats = []
        stale_after_ms = max(1, int(settings.service_heartbeat_stale_seconds)) * 1000
        best_stale: dict[str, Any] | None = None
        for heartbeat in heartbeats:
            if not isinstance(heartbeat, dict) or str(heartbeat.get("status") or "") != "running":
                continue
            updated_at_ms = _int(heartbeat.get("updated_at_ms"), 0)
            metadata_value = heartbeat.get("metadata")
            metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}
            loop_value = metadata.get("engine_loop")
            engine_loop = dict(loop_value) if isinstance(loop_value, dict) else {}
            if not engine_loop.get("enabled"):
                continue
            service_value = engine_loop.get("service")
            service = dict(service_value) if isinstance(service_value, dict) else {}
            if not service:
                continue
            age_ms = max(0, now_ms - updated_at_ms) if updated_at_ms else stale_after_ms + 1
            entry = {
                **service,
                "enabled": bool(service.get("enabled", engine_loop.get("enabled"))),
                "execution_modes": engine_loop.get("execution_modes"),
                "shadow_enabled": engine_loop.get("shadow_enabled"),
                "paper_enabled": engine_loop.get("paper_enabled"),
                "live_enabled": engine_loop.get("live_enabled"),
                "wave1c_enabled": engine_loop.get("wave1c_enabled"),
                "wave2_enabled": engine_loop.get("wave2_enabled"),
                "runtime_source": "trader_heartbeat",
                "runtime_instance_id": heartbeat.get("instance_id"),
                "runtime_updated_at_ms": updated_at_ms,
                "runtime_running": bool(engine_loop.get("running")),
                "runtime_stale": age_ms > stale_after_ms,
                "runtime_age_ms": age_ms,
            }
            if not entry["runtime_stale"]:
                return entry
            if best_stale is None or updated_at_ms > _int(best_stale.get("runtime_updated_at_ms"), 0):
                best_stale = entry
        if best_stale is not None:
            return best_stale

    return {
        **local,
        "enabled": bool(local.get("enabled", False)),
        "runtime_source": "local_passive_service" if local else "unavailable",
        "runtime_running": False,
        "runtime_stale": False,
        "runtime_age_ms": None,
    }
