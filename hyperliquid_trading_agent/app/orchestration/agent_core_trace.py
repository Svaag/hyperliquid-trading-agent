from __future__ import annotations

import json
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


class AgentCoreTraceEmitter:
    """Best-effort optional agent-core trace emission.

    The trading service must not require ``agent-core`` at import/runtime. When the
    package is installed we emit real ``TraceEvent`` objects through its sinks; when
    it is absent we still write/POST a schema-compatible bounded JSON payload so
    local canaries can validate integration before dependency rollout.
    """

    def __init__(self, *, settings: Settings, graph_id: str = "hyperliquid-wave-supervisor"):
        self.settings = settings
        self.graph_id = graph_id
        self.enabled = bool(settings.agent_core_trace_enabled)
        self.path = settings.agent_core_trace_path.strip()
        self.collector_url = settings.agent_core_trace_collector_url.strip()
        self.collector_token = settings.agent_core_trace_collector_token.strip()
        self._agent_core_available = False
        self._sink: Any | None = None
        if self.enabled:
            self._sink = self._build_agent_core_sink()

    @property
    def configured(self) -> bool:
        return bool(self.path or self.collector_url)

    @property
    def agent_core_available(self) -> bool:
        return self._agent_core_available

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "agent_core_available": self._agent_core_available,
            "path_configured": bool(self.path),
            "collector_configured": bool(self.collector_url),
            "graph_id": self.graph_id,
        }

    def emit(
        self,
        event_type: str,
        summary: str = "",
        *,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
        case_id: str | None = None,
        handoff_id: str | None = None,
        change_id: str | None = None,
        repository: str | None = None,
    ) -> bool:
        if not self.enabled or not self.configured:
            return False
        payload = _bounded_payload(payload or {})
        if self._sink is not None:
            try:
                from agent_core.contracts.tracing import TraceEvent  # type: ignore

                event = TraceEvent(
                    environment=self.settings.environment,
                    graph_id=self.graph_id,
                    graph_version=__version__,
                    node_id=node_id,
                    run_id=run_id,
                    trace_id=run_id,
                    event_type=event_type,
                    summary=summary[:1000],
                    payload=payload,
                    case_id=case_id,
                    handoff_id=handoff_id,
                    change_id=change_id,
                    repository=repository,
                )
                return bool(self._sink.emit(event))
            except Exception as exc:  # pragma: no cover - optional dependency/runtime path
                log.debug("agent_core_trace_emit_failed", error=type(exc).__name__)
        return self._emit_fallback(
            event_type,
            summary,
            payload=payload,
            run_id=run_id,
            node_id=node_id,
            case_id=case_id,
            handoff_id=handoff_id,
            change_id=change_id,
            repository=repository,
        )

    def _build_agent_core_sink(self) -> Any | None:
        try:
            from agent_core.tracing.sink import HttpSink, JsonlFileSink, MultiSink  # type: ignore
        except Exception:
            return None
        sinks: list[Any] = []
        if self.path:
            sinks.append(JsonlFileSink(self.path))
        if self.collector_url:
            sinks.append(HttpSink(self.collector_url, token=self.collector_token or None))
        self._agent_core_available = True
        if not sinks:
            return None
        if len(sinks) == 1:
            return sinks[0]
        return MultiSink(sinks)

    def _emit_fallback(
        self,
        event_type: str,
        summary: str,
        *,
        payload: dict[str, Any],
        run_id: str | None,
        node_id: str | None,
        case_id: str | None,
        handoff_id: str | None,
        change_id: str | None,
        repository: str | None,
    ) -> bool:
        event = {
            "schema_version": "agent-core-compatible.fallback.v1",
            "event_id": f"evt_{uuid4().hex}",
            "environment": self.settings.environment,
            "graph_id": self.graph_id,
            "graph_version": __version__,
            "node_id": node_id,
            "run_id": run_id,
            "trace_id": run_id,
            "event_type": event_type,
            "summary": summary[:1000],
            "payload": payload,
            "case_id": case_id,
            "handoff_id": handoff_id,
            "change_id": change_id,
            "repository": repository,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        delivered = False
        if self.path:
            try:
                path = Path(self.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                delivered = True
            except Exception as exc:  # pragma: no cover - filesystem runtime behavior
                log.debug("agent_core_trace_file_fallback_failed", error=type(exc).__name__)
        if self.collector_url:
            try:
                data = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
                headers = {"content-type": "application/json"}
                if self.collector_token:
                    headers["authorization"] = f"Bearer {self.collector_token}"
                request = urllib.request.Request(self.collector_url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    delivered = delivered or bool(200 <= int(response.status) < 300)
            except Exception as exc:  # pragma: no cover - network runtime behavior
                log.debug("agent_core_trace_http_fallback_failed", error=type(exc).__name__)
        return delivered


def trace_run_id(prefix: str = "hwave") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid4().hex[:8]}"


def _bounded_payload(value: Any, *, depth: int = 5, string_limit: int = 2000) -> Any:
    if depth <= 0:
        return str(value)[:string_limit]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:string_limit]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, child) in enumerate(value.items()):
            if idx >= 100:
                break
            safe_key = "".join(ch for ch in str(key) if ch.isalnum() or ch in {"_", "-", ":", ".", "/"})[:80] or "key"
            lowered = safe_key.lower()
            if any(marker in lowered for marker in {"secret", "token", "password", "credential", "authorization"}):
                out[safe_key] = "[redacted]"
            else:
                out[safe_key] = _bounded_payload(child, depth=depth - 1, string_limit=string_limit)
        return out
    if isinstance(value, list | tuple | set):
        return [_bounded_payload(item, depth=depth - 1, string_limit=string_limit) for item in list(value)[:100]]
    if hasattr(value, "model_dump"):
        try:
            return _bounded_payload(value.model_dump(mode="json"), depth=depth - 1, string_limit=string_limit)
        except Exception:
            return str(value)[:string_limit]
    return str(value)[:string_limit]
