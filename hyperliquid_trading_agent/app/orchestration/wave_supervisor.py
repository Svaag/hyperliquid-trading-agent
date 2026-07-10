from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.bandit import OfflineContextualBanditReporter
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import (
    EngineReplayComparisonService,
    latest_engine_replay_comparison,
)
from hyperliquid_trading_agent.app.engine.runtime import resolve_engine_runtime
from hyperliquid_trading_agent.app.engine.strategy_performance import refresh_strategy_regime_performance
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.orchestration.agent_core_trace import AgentCoreTraceEmitter, trace_run_id
from hyperliquid_trading_agent.app.orchestration.gate_snapshots import GateEvidenceSnapshotService
from hyperliquid_trading_agent.app.orchestration.lhp import (
    render_engineering_issue_body,
    render_wave_handoff_payload,
)

log = get_logger(__name__)

_WAIT_CODES = {
    "insufficient_shadow_observation",
    "insufficient_engine_runs",
    "insufficient_candidates",
    "insufficient_shadow_intents",
}
_BREADTH_CODE_FRAGMENTS = (
    "active_strategy_count",
    "active_strategy_family_count",
    "strategy_allocation_dominance",
    "strategy_family_allocation_dominance",
    "symbol_strategy_allocation_dominance",
    "strategy_dominance",
)
_SPINE_CODE_FRAGMENTS = (
    "paper_intents",
    "live_intents",
    "runtime_error",
    "stale_loop",
    "missing_core_data",
    "feature_coverage",
    "regime_coverage",
    "ev_coverage",
    "candidate_strategy_metadata",
    "candidate_evidence_link",
    "council",
    "risk_gateway",
    "outcome_attribution",
    "strategy_regime_evidence",
    "replay",
    "pnl",
    "fill_failure",
    "slippage",
    "risk_reject",
)


@dataclass(frozen=True)
class WaveSupervisorRunOptions:
    perform_maintenance: bool = True
    escalate: bool = False


class WaveSupervisor:
    """Long-running agentic supervisor for evidence-gated engine wave promotion.

    It observes and diagnoses continuously, may run report-only maintenance jobs,
    and can escalate bounded handoff payloads. It never mutates strategy wave flags,
    never enables paper/live execution, and never bypasses Council/RiskGateway.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any,
        engine_service: Any | None,
        trace_emitter: AgentCoreTraceEmitter | None = None,
        github_escalator: "GitHubIssueEscalator | None" = None,
    ):
        self.settings = settings
        self.repository = repository
        self.engine_service = engine_service
        self.trace = trace_emitter or AgentCoreTraceEmitter(settings=settings)
        self.github_escalator = github_escalator or GitHubIssueEscalator(settings=settings)
        self.gate_snapshots = GateEvidenceSnapshotService(
            settings=settings,
            repository=repository,
            engine_service=engine_service,
            github_client=self.github_escalator,
        )
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._run_count = 0
        self._started_at_ms: int | None = None

    async def start(self) -> None:
        if self._task is not None or not self.settings.orchestration_wave_supervisor_enabled:
            return
        self._started_at_ms = _now_ms()
        self._task = asyncio.create_task(self._loop(), name="hyperliquid-wave-supervisor")
        log.info("wave_supervisor_started", interval_seconds=self.settings.orchestration_wave_supervisor_interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.orchestration_wave_supervisor_enabled,
            "running": self._task is not None and not self._task.done(),
            "owner_role": "scheduler",
            "interval_seconds": self.settings.orchestration_wave_supervisor_interval_seconds,
            "maintenance_enabled": self.settings.orchestration_wave_supervisor_maintenance_enabled,
            "escalation_enabled": self.settings.orchestration_wave_supervisor_escalation_enabled,
            "gate_snapshots_enabled": self.settings.orchestration_gate_snapshots_enabled,
            "handoff_repo": self.settings.orchestration_wave_supervisor_handoff_repo,
            "started_at_ms": self._started_at_ms,
            "run_count": self._run_count,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "trace": self.trace.status(),
        }

    async def current_status(self) -> dict[str, Any]:
        base = self.status()
        try:
            snapshot = await self._snapshot(run_id=trace_run_id("hwave_status"), include_validation=False)
            classification = classify_wave_state(self.settings, snapshot.get("readiness") or {}, snapshot.get("latest_replay"), service_status=snapshot.get("engine_service") or {})
            base["current"] = {"classification": classification, "snapshot": _status_snapshot(snapshot)}
        except Exception as exc:
            base["current_error"] = type(exc).__name__
        return base

    async def run_once(self, options: WaveSupervisorRunOptions | None = None) -> dict[str, Any]:
        options = options or WaveSupervisorRunOptions(
            perform_maintenance=self.settings.orchestration_wave_supervisor_maintenance_enabled,
            escalate=self.settings.orchestration_wave_supervisor_escalation_enabled,
        )
        if self._lock.locked():
            return {"status": "locked", "detail": "another wave supervisor run is active", **self.status()}
        async with self._lock:
            run_id = trace_run_id()
            started_ms = _now_ms()
            await self._persist_run(
                {
                    "run_id": run_id,
                    "owner_role": "scheduler",
                    "status": "running",
                    "created_at_ms": started_ms,
                    "updated_at_ms": started_ms,
                }
            )
            self.trace.emit("wave_supervisor_started", "Wave Supervisor run started", run_id=run_id, payload={"options": options.__dict__})
            actions: list[dict[str, Any]] = []
            try:
                before = await self._snapshot(run_id=run_id, include_validation=True)
                if options.perform_maintenance:
                    actions.extend(await self._perform_maintenance(run_id=run_id, snapshot=before))
                after = await self._snapshot(run_id=run_id, include_validation=True)
                classification = classify_wave_state(self.settings, after.get("readiness") or {}, after.get("latest_replay"), service_status=after.get("engine_service") or {})
                handoff_payload = None
                if classification.get("handoff_recommended"):
                    handoff_payload = render_wave_handoff_payload(
                        settings=self.settings,
                        run_id=run_id,
                        classification=classification,
                        readiness=after.get("readiness") or {},
                        replay=after.get("latest_replay"),
                        actions=actions,
                    )
                escalation = await self._maybe_escalate(handoff_payload, options=options) if handoff_payload else {"status": "not_applicable"}
                result = {
                    "status": "completed",
                    "run_id": run_id,
                    "created_at_ms": started_ms,
                    "completed_at_ms": _now_ms(),
                    "duration_ms": _now_ms() - started_ms,
                    "mode": "observe_diagnose_task_verify_no_direct_promotion",
                    "classification": classification,
                    "actions": actions,
                    "handoff": handoff_payload,
                    "escalation": escalation,
                    "snapshot": _status_snapshot(after),
                    "safety": {
                        "direct_config_mutation": False,
                        "paper_enabled": bool(
                            (after.get("engine_service") or {}).get(
                                "paper_enabled", self.settings.engine_paper_enabled
                            )
                        ),
                        "live_enabled": bool(
                            (after.get("engine_service") or {}).get(
                                "live_enabled", self.settings.engine_live_enabled
                            )
                        ),
                        "wave2_enabled": bool(classification.get("wave2_enabled")),
                        "promotion_requires_pr_or_signed_operator_gate": True,
                    },
                }
                try:
                    result["gate_snapshots"] = await self.gate_snapshots.capture_due()
                except Exception as exc:  # pragma: no cover - milestone evidence cannot stop supervision
                    result["gate_snapshots"] = [{"status": "error", "error": type(exc).__name__}]
                result["completed_at_ms"] = _now_ms()
                result["duration_ms"] = result["completed_at_ms"] - started_ms
                self._run_count += 1
                self._last_result = result
                self._last_error = None
                await self._persist_run(
                    {
                        "run_id": run_id,
                        "owner_role": "scheduler",
                        "status": "completed",
                        "classification_state": classification.get("state"),
                        "created_at_ms": started_ms,
                        "completed_at_ms": result["completed_at_ms"],
                        "duration_ms": result["duration_ms"],
                        "result": result,
                        "updated_at_ms": result["completed_at_ms"],
                    }
                )
                self.trace.emit(
                    "wave_supervisor_completed",
                    f"Wave Supervisor classified state={classification.get('state')}",
                    run_id=run_id,
                    handoff_id=(handoff_payload or {}).get("handoff", {}).get("handoff_id") if isinstance(handoff_payload, dict) else None,
                    repository=self.settings.orchestration_wave_supervisor_handoff_repo,
                    payload={"classification": classification, "actions": actions, "escalation": escalation},
                )
                return result
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                failed_at_ms = _now_ms()
                await self._persist_run(
                    {
                        "run_id": run_id,
                        "owner_role": "scheduler",
                        "status": "failed",
                        "created_at_ms": started_ms,
                        "completed_at_ms": failed_at_ms,
                        "duration_ms": failed_at_ms - started_ms,
                        "last_error": self._last_error,
                        "updated_at_ms": failed_at_ms,
                    }
                )
                self.trace.emit("wave_supervisor_failed", self._last_error, run_id=run_id, payload={"error": type(exc).__name__})
                raise

    async def _persist_run(self, run: dict[str, Any]) -> None:
        persist = getattr(self.repository, "record_wave_supervisor_run", None)
        if not callable(persist):
            return
        try:
            await persist(run)
        except Exception as exc:  # pragma: no cover - observability cannot stop supervision
            log.warning("wave_supervisor_run_persistence_failed", error=type(exc).__name__)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - long-running runtime behavior
                self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                log.warning("wave_supervisor_run_failed", error=type(exc).__name__)
            await asyncio.sleep(max(60, self.settings.orchestration_wave_supervisor_interval_seconds))

    async def _snapshot(self, *, run_id: str, include_validation: bool) -> dict[str, Any]:
        readiness = await build_paper_readiness_scorecard(
            self.repository,
            self.settings,
            self.engine_service,
            window_hours=self.settings.engine_readiness_window_hours,
            limit=1000,
        )
        replay = await _safe_latest_replay(self.repository)
        service_status = await resolve_engine_runtime(
            self.repository,
            self.settings,
            local_service=self.engine_service,
        )
        registry_status = (
            dict(service_status.get("strategy_registry"))
            if isinstance(service_status.get("strategy_registry"), dict)
            else {}
        )
        registry = getattr(self.engine_service, "strategy_registry", None) if self.engine_service is not None else None
        if registry is not None and callable(getattr(registry, "metadata", None)):
            registry_status = registry.metadata()
        validation: dict[str, Any] = {}
        if include_validation:
            try:
                validation = await build_engine_validation_report(self.repository, limit=500)
            except Exception as exc:
                validation = {"status": "unavailable", "error": type(exc).__name__}
        return {
            "run_id": run_id,
            "readiness": readiness,
            "latest_replay": replay,
            "engine_service": service_status,
            "strategy_registry": registry_status,
            "validation_report": validation,
        }

    async def _perform_maintenance(self, *, run_id: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        end_ms = _now_ms()
        start_ms = end_ms - self.settings.engine_readiness_window_hours * 3_600_000
        try:
            rows = await refresh_strategy_regime_performance(self.repository, window_start_ms=start_ms, window_end_ms=end_ms)
            actions.append({"action": "refresh_strategy_regime_performance", "status": "completed", "report_only": True, "row_count": len(rows)})
        except Exception as exc:
            actions.append({"action": "refresh_strategy_regime_performance", "status": "failed", "error": type(exc).__name__, "report_only": True})
        replay = snapshot.get("latest_replay")
        if _replay_refresh_needed(replay, self.settings):
            try:
                service = EngineReplayComparisonService(repository=self.repository, settings=self.settings)
                artifact = await service.compare_variant(
                    baseline_config={"current": True},
                    candidate_config={"current": True},
                    window_start_ms=start_ms,
                    window_end_ms=end_ms,
                    universe=self.settings.autonomy_core_symbols,
                    variant_id="wave_supervisor_current_config_shadow_replay",
                )
                actions.append({"action": "run_replay_comparison", "status": "completed", "report_only": True, "replay_id": artifact.get("replay_id"), "replay_status": artifact.get("status")})
            except Exception as exc:
                actions.append({"action": "run_replay_comparison", "status": "failed", "error": type(exc).__name__, "report_only": True})
        if self.settings.orchestration_wave_supervisor_bandit_enabled:
            try:
                result = await OfflineContextualBanditReporter(self.repository).run(window_start_ms=start_ms, window_end_ms=end_ms)
                actions.append({"action": "run_bandit_recommendations", "status": "completed", "report_only": True, **{k: v for k, v in result.items() if k != "recommendations"}})
            except Exception as exc:
                actions.append({"action": "run_bandit_recommendations", "status": "failed", "error": type(exc).__name__, "report_only": True})
        self.trace.emit("wave_supervisor_maintenance", "Wave Supervisor maintenance actions completed", run_id=run_id, payload={"actions": actions})
        return actions

    async def _maybe_escalate(self, handoff_payload: dict[str, Any], *, options: WaveSupervisorRunOptions) -> dict[str, Any]:
        if not options.escalate:
            return {"status": "skipped", "reason": "escalation_not_requested"}
        if not self.settings.orchestration_wave_supervisor_escalation_enabled:
            return {"status": "skipped", "reason": "escalation_disabled"}
        if self.settings.orchestration_wave_supervisor_escalation_transport != "github_issue":
            return {"status": "skipped", "reason": "unsupported_transport", "transport": self.settings.orchestration_wave_supervisor_escalation_transport}
        return await self.github_escalator.create_or_get_issue(handoff_payload)


class GitHubIssueEscalator:
    """Create idempotent `loop:candidate` issues for Engineering Loop intake."""

    def __init__(self, *, settings: Settings):
        self.settings = settings
        self.repo = settings.orchestration_wave_supervisor_handoff_repo.strip()
        self.labels = [item.strip() for item in settings.orchestration_wave_supervisor_handoff_labels.split(",") if item.strip()]
        self.token = settings.orchestration_wave_supervisor_github_token.strip() or os.getenv("GITHUB_TOKEN", "").strip()
        self.timeout = settings.orchestration_wave_supervisor_request_timeout_seconds

    async def create_or_get_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.repo:
            return {"status": "skipped", "reason": "handoff_repo_not_configured"}
        if not self.token:
            return {"status": "skipped", "reason": "github_token_not_configured", "repo": self.repo}
        handoff = payload.get("handoff") if isinstance(payload.get("handoff"), dict) else {}
        handoff_id = str(handoff.get("handoff_id") or "unknown")
        marker = f"hyperliquid-wave-handoff-id:{handoff_id}"
        title = f"[hyperliquid-wave] {handoff.get('objective') or 'Wave orchestration task'}"[:250]
        body = render_engineering_issue_body(payload)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            existing = await self._find_existing(client, marker)
            if existing:
                return {"status": "existing", "repo": self.repo, "issue_number": existing.get("number"), "issue_url": existing.get("html_url"), "handoff_id": handoff_id}
            response = await client.post(f"https://api.github.com/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": self.labels})
            if response.status_code >= 400:
                return {"status": "failed", "repo": self.repo, "status_code": response.status_code, "error": response.text[:300]}
            issue = response.json()
            return {"status": "created", "repo": self.repo, "issue_number": issue.get("number"), "issue_url": issue.get("html_url"), "handoff_id": handoff_id, "labels": self.labels}

    async def upsert_comment(self, *, issue_number: int, body: str, marker: str) -> dict[str, Any]:
        """Create one immutable evidence comment, deduplicated by hidden marker."""

        if not self.repo:
            return {"status": "skipped", "reason": "handoff_repo_not_configured"}
        if not self.token:
            return {"status": "skipped", "reason": "github_token_not_configured", "repo": self.repo}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            comments_url = f"https://api.github.com/repos/{self.repo}/issues/{int(issue_number)}/comments"
            response = await self._request_with_retry(client, "GET", comments_url, params={"per_page": 100})
            if response is None:
                return {"status": "failed", "reason": "github_comments_unavailable", "issue_number": issue_number}
            if response.status_code in {401, 403}:
                return {"status": "failed", "terminal": True, "status_code": response.status_code, "reason": "github_auth_failed", "issue_number": issue_number}
            if response.status_code >= 400:
                return {"status": "failed", "status_code": response.status_code, "error": response.text[:300], "issue_number": issue_number}
            for comment in response.json():
                if isinstance(comment, dict) and marker in str(comment.get("body") or ""):
                    return {
                        "status": "existing",
                        "repo": self.repo,
                        "issue_number": issue_number,
                        "comment_id": comment.get("id"),
                        "comment_url": comment.get("html_url"),
                    }
            response = await self._request_with_retry(client, "POST", comments_url, json={"body": body})
            if response is None:
                return {"status": "failed", "reason": "github_comment_post_unavailable", "issue_number": issue_number}
            if response.status_code in {401, 403}:
                return {"status": "failed", "terminal": True, "status_code": response.status_code, "reason": "github_auth_failed", "issue_number": issue_number}
            if response.status_code >= 400:
                return {"status": "failed", "status_code": response.status_code, "error": response.text[:300], "issue_number": issue_number}
            comment = response.json()
            return {
                "status": "created",
                "repo": self.repo,
                "issue_number": issue_number,
                "comment_id": comment.get("id"),
                "comment_url": comment.get("html_url"),
            }

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response | None:
        for attempt in range(3):
            try:
                response = await client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError):
                response = None
            if response is not None and (response.status_code < 500 and response.status_code != 429):
                return response
            if attempt == 2:
                return response
            retry_after = response.headers.get("Retry-After") if response is not None else None
            try:
                delay = max(0.1, min(30.0, float(retry_after))) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            await asyncio.sleep(delay)
        return None

    async def _find_existing(self, client: httpx.AsyncClient, marker: str) -> dict[str, Any] | None:
        params: dict[str, Any] = {"state": "open", "per_page": 50}
        if self.labels:
            params["labels"] = ",".join(self.labels[:3])
        response = await client.get(f"https://api.github.com/repos/{self.repo}/issues", params=params)
        if response.status_code >= 400:
            return None
        for issue in response.json():
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            if marker in str(issue.get("body") or ""):
                return issue
        return None


def classify_wave_state(settings: Settings, readiness: dict[str, Any], replay: dict[str, Any] | None, *, service_status: dict[str, Any] | None = None) -> dict[str, Any]:
    service_status = service_status or {}
    wave_policy = service_status.get("wave_policy") if isinstance(service_status.get("wave_policy"), dict) else {}
    engine_enabled = bool(service_status.get("enabled", settings.engine_enabled))
    paper_enabled = bool(service_status.get("paper_enabled", settings.engine_paper_enabled))
    live_enabled = bool(service_status.get("live_enabled", settings.engine_live_enabled))
    wave1c_enabled = bool(
        service_status.get("wave1c_enabled", wave_policy.get("wave1c_enabled", settings.engine_wave1c_enabled))
    )
    wave2_enabled = bool(
        service_status.get("wave2_enabled", wave_policy.get("wave2_enabled", settings.engine_wave2_enabled))
    )
    hard_blocks = [item for item in readiness.get("hard_blocks", []) if isinstance(item, dict)]
    warnings = [item for item in readiness.get("warnings", []) if isinstance(item, dict)]
    wait_blocks = [item for item in hard_blocks if str(item.get("code")) in _WAIT_CODES]
    breadth_blocks = [item for item in hard_blocks if _is_breadth_code(str(item.get("code") or ""))]
    spine_blocks = [item for item in hard_blocks if _is_spine_code(str(item.get("code") or ""))]
    other_blocks = [item for item in hard_blocks if item not in wait_blocks and item not in breadth_blocks and item not in spine_blocks]
    replay_status = str((replay or {}).get("status") or "missing")
    existing_codes = {str(item.get("code") or "") for item in hard_blocks}
    if replay is None and settings.engine_readiness_require_latest_replay and "replay_comparison_missing" not in existing_codes:
        spine_blocks.append({"code": "replay_comparison_missing", "severity": "critical", "detail": "Latest replay comparison is missing."})
    elif replay_status == "failed" and "replay_comparison_failed" not in existing_codes:
        spine_blocks.append({"code": "replay_comparison_failed", "severity": "critical", "detail": f"Latest replay status={replay_status}."})

    if not engine_enabled:
        state = "engine_disabled"
    elif live_enabled or paper_enabled:
        state = "unsafe_execution_mode"
    elif wave1c_enabled:
        state = "wave1c_canary_blocked" if hard_blocks or spine_blocks or other_blocks else "wave1c_canary"
    elif spine_blocks or other_blocks:
        state = "blocked"
    elif wait_blocks:
        state = "collecting_wave1a_evidence"
    else:
        # Full paper readiness may still be blocked by strategy breadth; that is exactly
        # the controlled problem Wave 1C is meant to solve after the evidence spine is clean.
        state = "wave1c_promotion_candidate"

    handoff_recommended = state in {"blocked", "wave1c_promotion_candidate", "wave1c_canary_blocked"}
    objective_key = {
        "wave1c_promotion_candidate": "enable-wave1c-controlled-canary-v1",
        "wave1c_canary_blocked": "stabilize-wave1c-canary-v1",
        "blocked": "resolve-wave-readiness-blockers-v1",
    }.get(state, "continue-wave1a-shadow-observation-v1")
    return {
        "state": state,
        "objective_key": objective_key,
        "phase": "wave1c" if wave1c_enabled else "wave1a",
        "ready_for_paper": bool(readiness.get("ready_for_paper")),
        "readiness_grade": readiness.get("grade"),
        "readiness_score": readiness.get("score"),
        "wave1c_enabled": wave1c_enabled,
        "wave2_enabled": wave2_enabled,
        "handoff_recommended": handoff_recommended,
        "promotion_candidate": state == "wave1c_promotion_candidate",
        "blockers": [*spine_blocks, *other_blocks, *wait_blocks, *breadth_blocks],
        "blocker_counts": {
            "spine": len(spine_blocks),
            "other": len(other_blocks),
            "wait": len(wait_blocks),
            "breadth": len(breadth_blocks),
            "warnings": len(warnings),
        },
        "replay_status": replay_status,
        "engine_run_count": service_status.get("run_count"),
        "recommendations": _recommendations_for_state(state, spine_blocks=spine_blocks, wait_blocks=wait_blocks, breadth_blocks=breadth_blocks),
    }


def _recommendations_for_state(state: str, *, spine_blocks: list[dict[str, Any]], wait_blocks: list[dict[str, Any]], breadth_blocks: list[dict[str, Any]]) -> list[str]:
    if state == "wave1c_promotion_candidate":
        return [
            "Open an Engineering Loop candidate issue/PR to enable Wave 1C as a controlled canary.",
            "Keep Wave 2 disabled and keep RiskGateway/Council mandatory.",
            "After deploy, verify candidate evidence links, outcome attribution, replay links, and concentration events.",
        ]
    if state == "collecting_wave1a_evidence":
        return ["Continue Wave 1A shadow observation until minimum time/sample gates pass."]
    if state == "wave1c_canary":
        return ["Continue Wave 1C canary verification; do not advance Wave 2 until Wave 1C evidence is stable."]
    if state == "wave1c_canary_blocked":
        return [
            "Keep Wave 1C enabled as a shadow-only canary while readiness blockers are remediated.",
            "Keep paper, live, and Wave 2 disabled; do not promote from this state.",
        ]
    if spine_blocks:
        return ["Escalate evidence/replay/risk/council/outcome spine blockers to Engineering Loop with bounded evidence."]
    if breadth_blocks:
        return ["Treat breadth blockers as Wave 1C promotion candidates only after spine gates are clean."]
    return ["No action required beyond continued observation."]


def _is_breadth_code(code: str) -> bool:
    return any(fragment in code for fragment in _BREADTH_CODE_FRAGMENTS)


def _is_spine_code(code: str) -> bool:
    return any(fragment in code for fragment in _SPINE_CODE_FRAGMENTS)


def _replay_refresh_needed(replay: dict[str, Any] | None, settings: Settings) -> bool:
    if replay is None:
        return True
    if str(replay.get("status") or "") == "failed":
        return True
    created = int(replay.get("created_at_ms") or 0)
    if created <= 0:
        return True
    return _now_ms() - created > max(1, settings.engine_readiness_min_replay_window_hours) * 3_600_000


async def _safe_latest_replay(repository: Any) -> dict[str, Any] | None:
    try:
        return await latest_engine_replay_comparison(repository)
    except Exception:
        return None


def _status_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    readiness = snapshot.get("readiness") if isinstance(snapshot.get("readiness"), dict) else {}
    replay = snapshot.get("latest_replay") if isinstance(snapshot.get("latest_replay"), dict) else None
    validation = snapshot.get("validation_report") if isinstance(snapshot.get("validation_report"), dict) else {}
    return {
        "readiness": {
            "ready_for_paper": readiness.get("ready_for_paper"),
            "score": readiness.get("score"),
            "grade": readiness.get("grade"),
            "hard_block_count": len(readiness.get("hard_blocks") or []),
            "warning_count": len(readiness.get("warnings") or []),
            "recommendation": readiness.get("recommendation"),
        },
        "latest_replay": {
            "replay_id": (replay or {}).get("replay_id"),
            "status": (replay or {}).get("status") or "missing",
            "verdict": ((replay or {}).get("metadata") or {}).get("verdict") if isinstance((replay or {}).get("metadata"), dict) else None,
        },
        "engine_service": snapshot.get("engine_service") or {},
        "strategy_registry": snapshot.get("strategy_registry") or {},
        "validation_report": {
            "status": validation.get("status") or validation.get("grade") or ("available" if validation else "unavailable"),
            "keys": sorted(validation.keys())[:20] if validation else [],
        },
    }


def _now_ms() -> int:
    return int(time.time() * 1000)
