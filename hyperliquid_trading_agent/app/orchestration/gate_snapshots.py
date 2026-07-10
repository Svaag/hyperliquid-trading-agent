from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Awaitable, Callable

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.engine.diagnostics import build_candidate_funnel, build_strategy_funnel
from hyperliquid_trading_agent.app.engine.news_risk_counterfactual import run_news_risk_counterfactual
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import EngineReplayComparisonService, engine_config_hash
from hyperliquid_trading_agent.app.engine.signal_quality import build_signal_quality_report
from hyperliquid_trading_agent.app.newswire.feedback import build_newswire_feedback_summary
from hyperliquid_trading_agent.app.newswire.observability import build_newswire_soak_readiness


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class GateEvidenceSnapshotService:
    """Persist and publish immutable evidence at clean-window milestones."""

    def __init__(
        self,
        *,
        settings: Any,
        repository: Any,
        engine_service: Any | None,
        github_client: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.engine_service = engine_service
        self.github_client = github_client

    async def capture_due(self, *, now_ms: int | None = None) -> list[dict[str, Any]]:
        if not bool(getattr(self.settings, "orchestration_gate_snapshots_enabled", True)):
            return []
        current_ms = int(now_ms or _now_ms())
        clocks = await self._runtime_clocks()
        anchor_ms = int(clocks.get("combined_anchor_ms") or 0)
        if anchor_ms <= 0:
            return [{"status": "skipped", "reason": "clean_window_anchor_unavailable"}]
        existing = await self._existing_runs()
        captured: list[dict[str, Any]] = []
        for milestone_hours in self._milestones():
            due_at_ms = anchor_ms + milestone_hours * 3_600_000
            run_id = f"gate_{anchor_ms}_{milestone_hours}h_v1"
            if current_ms < due_at_ms:
                continue
            previous = existing.get(run_id)
            if previous is not None and str(previous.get("status") or "") == "completed":
                continue
            captured.append(
                await self.capture(
                    run_id=run_id,
                    anchor_ms=anchor_ms,
                    milestone_hours=milestone_hours,
                    due_at_ms=due_at_ms,
                    clocks=clocks,
                    captured_at_ms=current_ms,
                )
            )
        return captured

    async def capture(
        self,
        *,
        run_id: str,
        anchor_ms: int,
        milestone_hours: int,
        due_at_ms: int,
        clocks: dict[str, Any],
        captured_at_ms: int | None = None,
    ) -> dict[str, Any]:
        started_ms = int(captured_at_ms or _now_ms())
        await self._persist(
            run_id,
            status="running",
            created_at_ms=started_ms,
            result={"artifact_type": "gate_evidence_snapshot", "scheduled_for_ms": due_at_ms},
        )
        components: dict[str, dict[str, Any]] = {}
        components["readiness"] = await self._component(
            "readiness",
            lambda: build_paper_readiness_scorecard(
                self.repository,
                self.settings,
                self.engine_service,
                window_hours=milestone_hours,
                limit=20_000,
            ),
        )
        components["replay"] = await self._component(
            "replay",
            lambda: EngineReplayComparisonService(
                repository=self.repository,
                settings=self.settings,
            ).compare_variant(
                baseline_config={"current": True},
                candidate_config={"current": True},
                window_start_ms=anchor_ms,
                window_end_ms=due_at_ms,
                universe=list(self.settings.autonomy_core_symbols or ["BTC", "ETH", "HYPE"]),
                variant_id=f"gate_current_config_{milestone_hours}h_{anchor_ms}",
            ),
        )
        components["candidate_funnel"] = await self._component(
            "candidate_funnel",
            lambda: build_candidate_funnel(
                self.repository,
                window_hours=milestone_hours,
                as_of_ms=due_at_ms,
            ),
        )
        components["strategy_funnel"] = await self._component(
            "strategy_funnel",
            lambda: build_strategy_funnel(
                self.repository,
                window_hours=milestone_hours,
                as_of_ms=due_at_ms,
            ),
        )
        components["signal_quality"] = await self._component(
            "signal_quality",
            lambda: build_signal_quality_report(
                self.repository,
                window_hours=milestone_hours,
                as_of_ms=due_at_ms,
            ),
        )
        components["news_risk_counterfactual"] = await self._component(
            "news_risk_counterfactual",
            lambda: run_news_risk_counterfactual(
                self.repository,
                window_hours=milestone_hours,
                as_of_ms=due_at_ms,
                persist=True,
            ),
        )
        components["newswire_soak"] = await self._component(
            "newswire_soak",
            lambda: build_newswire_soak_readiness(self.repository, self.settings),
        )
        publisher_start_ms = int(clocks.get("publisher_start_ms") or anchor_ms)
        components["newswire_feedback"] = await self._component(
            "newswire_feedback",
            lambda: build_newswire_feedback_summary(
                self.repository,
                cohort_start_ms=publisher_start_ms,
                as_of_ms=due_at_ms,
            ),
        )
        components["side_effect_checks"] = await self._component(
            "side_effect_checks",
            lambda: self._side_effect_checks(anchor_ms=anchor_ms, end_ms=due_at_ms),
        )
        snapshot = {
            "schema_version": 1,
            "artifact_type": "gate_evidence_snapshot",
            "snapshot_id": run_id,
            "milestone_hours": milestone_hours,
            "anchor_ms": anchor_ms,
            "scheduled_for_ms": due_at_ms,
            "captured_at_ms": started_ms,
            "capture_delay_ms": max(0, started_ms - due_at_ms),
            "runtime_clocks": clocks,
            "code_version": __version__,
            "engine_config_hash": engine_config_hash(self.settings),
            "components": components,
        }
        snapshot_sha256 = _digest(snapshot)
        delivery = await self._publish(snapshot, snapshot_sha256=snapshot_sha256)
        completed_ms = _now_ms()
        result = {
            "artifact_type": "gate_evidence_snapshot",
            "snapshot": snapshot,
            "snapshot_sha256": snapshot_sha256,
            "delivery": delivery,
        }
        await self._persist(
            run_id,
            status="completed",
            classification_state=_classification(components.get("readiness")),
            created_at_ms=started_ms,
            completed_at_ms=completed_ms,
            duration_ms=completed_ms - started_ms,
            result=result,
        )
        return {"run_id": run_id, "status": "completed", "snapshot_sha256": snapshot_sha256, "delivery": delivery}

    async def _component(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        try:
            return {"status": "available", "data": await factory()}
        except Exception as exc:
            return {"status": "error", "error": type(exc).__name__, "component": name}

    async def _runtime_clocks(self) -> dict[str, Any]:
        method = getattr(self.repository, "list_service_heartbeats", None)
        heartbeats = list(await method(limit=100)) if callable(method) else []
        current: dict[str, dict[str, Any]] = {}
        for row in heartbeats:
            role = str(row.get("service_role") or "")
            if role and role not in current and str(row.get("status") or "") in {"starting", "running"}:
                current[role] = row
        trader = current.get("trader") or {}
        newswire = current.get("newswire") or {}
        publisher = current.get("discord_publisher") or {}
        configured_engine_anchor = int(getattr(self.settings, "engine_readiness_clean_window_start_ms", 0) or 0)
        trader_start = int(trader.get("started_at_ms") or 0)
        newswire_start = int(newswire.get("started_at_ms") or 0)
        engine_anchor = max(configured_engine_anchor, trader_start)
        valid_anchors = [value for value in (engine_anchor, newswire_start) if value > 0]
        return {
            "engine_anchor_ms": engine_anchor,
            "newswire_anchor_ms": newswire_start,
            "combined_anchor_ms": max(valid_anchors) if len(valid_anchors) == 2 else 0,
            "publisher_start_ms": int(publisher.get("started_at_ms") or 0),
            "trader": _runtime_identity(trader),
            "newswire": _runtime_identity(newswire),
            "discord_publisher": _runtime_identity(publisher),
        }

    async def _side_effect_checks(self, *, anchor_ms: int, end_ms: int) -> dict[str, Any]:
        intents_method = getattr(self.repository, "list_order_intents", None)
        reports_method = getattr(self.repository, "list_execution_reports", None)
        intents = list(await intents_method(limit=100_000)) if callable(intents_method) else []
        reports = list(await reports_method(since_ms=anchor_ms, until_ms=end_ms, limit=100_000)) if callable(reports_method) else []
        intents = [row for row in intents if anchor_ms <= int(row.get("created_at_ms") or 0) <= end_ms]
        modes = {mode: sum(str(row.get("execution_mode") or "") == mode for row in intents) for mode in ("shadow", "paper", "live")}
        report_modes = {mode: sum(str(row.get("execution_mode") or "") == mode for row in reports) for mode in ("shadow", "paper", "live")}
        violation = bool(modes["paper"] or modes["live"] or report_modes["paper"] or report_modes["live"])
        return {
            "window": {"start_ms": anchor_ms, "end_ms": end_ms},
            "configured": {
                "engine_paper_enabled": bool(self.settings.engine_paper_enabled),
                "engine_live_enabled": bool(self.settings.engine_live_enabled),
                "hyperliquid_exchange_enabled": bool(self.settings.hyperliquid_exchange_enabled),
                "alpaca_trading_enabled": bool(self.settings.alpaca_trading_enabled),
            },
            "order_intent_counts": modes,
            "execution_report_counts": report_modes,
            "paper_or_live_side_effect_violation": violation,
            "exchange_actions": [],
        }

    async def _existing_runs(self) -> dict[str, dict[str, Any]]:
        method = getattr(self.repository, "list_wave_supervisor_runs", None)
        if not callable(method):
            return {}
        rows = list(await method(limit=5000))
        return {
            str(row.get("run_id") or ""): row
            for row in rows
            if (row.get("result") or {}).get("artifact_type") == "gate_evidence_snapshot"
            or str(row.get("run_id") or "").startswith("gate_")
        }

    def _milestones(self) -> list[int]:
        raw = str(getattr(self.settings, "orchestration_gate_snapshot_milestone_hours", "24,72"))
        values = []
        for item in raw.split(","):
            try:
                value = int(item.strip())
            except ValueError:
                continue
            if value > 0:
                values.append(value)
        return sorted(set(values or [24, 72]))

    async def _publish(self, snapshot: dict[str, Any], *, snapshot_sha256: str) -> dict[str, Any]:
        if not bool(getattr(self.settings, "orchestration_gate_snapshot_github_enabled", True)):
            return {"status": "skipped", "reason": "github_delivery_disabled"}
        if self.github_client is None or not callable(getattr(self.github_client, "upsert_comment", None)):
            return {"status": "skipped", "reason": "github_client_unavailable"}
        issues = [
            int(getattr(self.settings, "orchestration_gate_snapshot_issue_10", 10)),
            int(getattr(self.settings, "orchestration_gate_snapshot_issue_16", 16)),
            int(getattr(self.settings, "orchestration_gate_snapshot_issue_21", 21)),
        ]
        results: dict[str, Any] = {}
        for issue_number in issues:
            marker = f"<!-- gate-snapshot:{snapshot['snapshot_id']}:issue:{issue_number} -->"
            body = render_gate_snapshot_comment(
                snapshot,
                issue_number=issue_number,
                snapshot_sha256=snapshot_sha256,
                marker=marker,
            )
            results[str(issue_number)] = await self.github_client.upsert_comment(
                issue_number=issue_number,
                body=body,
                marker=marker,
            )
        return {"status": "attempted", "issues": results}

    async def _persist(
        self,
        run_id: str,
        *,
        status: str,
        created_at_ms: int,
        result: dict[str, Any],
        classification_state: str | None = None,
        completed_at_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        method = getattr(self.repository, "record_wave_supervisor_run", None)
        if not callable(method):
            return
        await method(
            {
                "run_id": run_id,
                "owner_role": "scheduler",
                "status": status,
                "classification_state": classification_state,
                "created_at_ms": created_at_ms,
                "completed_at_ms": completed_at_ms,
                "duration_ms": duration_ms,
                "result": result,
                "updated_at_ms": completed_at_ms or _now_ms(),
            }
        )


def _runtime_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "service_role": row.get("service_role"),
        "instance_id": row.get("instance_id"),
        "started_at_ms": row.get("started_at_ms"),
        "updated_at_ms": row.get("updated_at_ms"),
        "status": row.get("status"),
        "version": row.get("version"),
    }


def _component_data(component: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(component, dict) or component.get("status") != "available":
        return {}
    data = component.get("data")
    return data if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _classification(component: dict[str, Any] | None) -> str:
    readiness = _component_data(component)
    return "ready" if readiness.get("ready_for_paper") else "readiness_blocked"


def render_gate_snapshot_comment(
    snapshot: dict[str, Any],
    *,
    issue_number: int,
    snapshot_sha256: str,
    marker: str,
) -> str:
    components = _dict(snapshot.get("components"))
    readiness = _component_data(components.get("readiness"))
    candidate = _component_data(components.get("candidate_funnel"))
    strategy = _component_data(components.get("strategy_funnel"))
    signal = _component_data(components.get("signal_quality"))
    soak = _component_data(components.get("newswire_soak"))
    feedback = _component_data(components.get("newswire_feedback"))
    side_effects = _component_data(components.get("side_effect_checks"))
    counterfactual = _component_data(components.get("news_risk_counterfactual"))
    safety = ((counterfactual.get("metadata") or {}).get("safety_decision") or {}) if counterfactual else {}
    title = {
        10: "Shadow validation gate evidence",
        16: "Wave 1C gate evidence",
        21: "Newswire soak and counterfactual evidence",
    }.get(issue_number, "Gate evidence")
    blocks = [str(item.get("code") or "unknown") for item in readiness.get("hard_blocks") or []]
    lines = [
        marker,
        f"## {title} — {snapshot.get('milestone_hours')}h",
        "",
        f"Snapshot `{snapshot.get('snapshot_id')}` · SHA-256 `{snapshot_sha256}`",
        f"Scheduled `{snapshot.get('scheduled_for_ms')}` · captured `{snapshot.get('captured_at_ms')}`",
        "",
        f"- Readiness: **{'ready' if readiness.get('ready_for_paper') else 'blocked'}**, score `{readiness.get('score', 'n/a')}`, recommendation `{readiness.get('recommendation', 'n/a')}`",
        f"- Hard blocks: `{', '.join(blocks) if blocks else 'none'}`",
        f"- Candidate funnel: `{candidate.get('candidate_count', 0)}` candidates; `{(candidate.get('stage_counts') or {}).get('allocator_approved', 0)}` allocator-approved; `{(candidate.get('stage_counts') or {}).get('council_allowed', 0)}` Council-allowed; `{(candidate.get('stage_counts') or {}).get('shadow_intent', 0)}` shadow intents",
        f"- Breadth: `{strategy.get('active_strategy_count', 0)}/5` strategies across `{strategy.get('active_strategy_family_count', 0)}/3` families",
        f"- Strict signal sample: `{(signal.get('data_quality') or {}).get('usable_rows', 0)}` rows; result remains modeled, not execution PnL",
        f"- Newswire soak: `{soak.get('status', soak.get('readiness', 'unavailable'))}`",
        f"- Newswire overlay: `{safety.get('recommendation', 'not_available')}`",
        f"- Discord feedback: `{(feedback.get('overall') or {}).get('posted_story_count', 0)}` posted stories, `{(feedback.get('overall') or {}).get('vote_coverage_pct', 0)}%` vote coverage",
        f"- Paper/live side-effect violation: `{side_effects.get('paper_or_live_side_effect_violation', 'unavailable')}`",
        "",
        "The full immutable JSON payload is stored in `wave_supervisor_runs`; this comment is a bounded, redacted projection.",
    ]
    return "\n".join(lines)[:64_000]
