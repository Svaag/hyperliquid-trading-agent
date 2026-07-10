from __future__ import annotations

import asyncio
import time
from typing import Any

from hyperliquid_trading_agent.app.autonomy.discord import AutonomyAlertSink
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.discord_bot import _chunk
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
from hyperliquid_trading_agent.app.engine.runtime import resolve_engine_runtime
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import ENGINE_VALIDATION_ALERTS, ENGINE_VALIDATION_DIGESTS
from hyperliquid_trading_agent.app.newswire.observability import build_engine_newsfeed_health

log = get_logger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class EngineValidationMonitorService:
    """Trader-owned watchdog and digest producer for the shadow engine."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any,
        engine_service: Any | None = None,
        alert_sink: AutonomyAlertSink | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.engine_service = engine_service
        self.alert_sink = alert_sink
        self._task: asyncio.Task | None = None
        self.started_at_ms = _now_ms()
        self.last_digest_at_ms: int | None = None
        self.last_watchdog_at_ms: int | None = None
        self.last_error: str | None = None
        self.digest_count = 0
        self.alert_count = 0
        self.last_alerts: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_validation_digest_enabled,
            "running": self._task is not None and not self._task.done(),
            "owner_role": "trader",
            "delivery_transport": "operational_notification_outbox",
            "channel_configured": self.settings.autonomy_alert_channel_configured,
            "last_digest_at_ms": self.last_digest_at_ms,
            "last_watchdog_at_ms": self.last_watchdog_at_ms,
            "last_error": self.last_error,
            "digest_count": self.digest_count,
            "alert_count": self.alert_count,
            "last_alerts": self.last_alerts,
        }

    async def start(self) -> None:
        if self._task is not None or not self.settings.engine_validation_digest_enabled:
            return
        if not self.settings.engine_enabled:
            return
        self._task = asyncio.create_task(self._run(), name="engine-validation-monitor")
        log.info(
            "engine_validation_monitor_started",
            digest_interval_seconds=self.settings.engine_validation_digest_interval_seconds,
            watchdog_interval_seconds=self.settings.engine_validation_watchdog_interval_seconds,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_once(self, *, post: bool = True) -> dict[str, Any]:
        report = await build_engine_validation_report(self.repository, limit=500)
        readiness = await build_paper_readiness_scorecard(self.repository, self.settings, self.engine_service, limit=1000)
        latest_replay = await latest_engine_replay_comparison(self.repository)
        alerts = await self._detect_alerts(report)
        if readiness.get("grade") == "blocked":
            alerts.append({"type": "readiness_blocked", "severity": "warning", "detail": f"score={readiness.get('score')} recommendation={readiness.get('recommendation')}"})
        if latest_replay and (latest_replay.get("metadata") or {}).get("verdict") == "candidate_worse":
            alerts.append({"type": "replay_candidate_worse", "severity": "warning", "detail": str(latest_replay.get("replay_id"))})
        self.last_alerts = alerts
        self.alert_count += len(alerts)
        for alert in alerts:
            ENGINE_VALIDATION_ALERTS.labels(alert_type=str(alert.get("type") or "unknown")).inc()
        message = format_engine_validation_digest(report, alerts, settings=self.settings, service_status=await self._engine_status(), readiness=readiness, latest_replay=latest_replay)
        if post:
            sent_at_ms = _now_ms()
            digest_bucket = sent_at_ms // (max(60, self.settings.engine_validation_digest_interval_seconds) * 1000)
            await self._send(
                message,
                category="engine_validation_digest",
                dedupe_key=f"engine-validation-digest:{digest_bucket}",
                severity="warning" if alerts else "info",
            )
            self.last_digest_at_ms = sent_at_ms
            self.digest_count += 1
        return {"report": report, "readiness": readiness, "latest_replay": latest_replay, "alerts": alerts, "message": message}

    async def _run(self) -> None:
        # Allow the engine to finish its first loop before evaluating staleness.
        await asyncio.sleep(max(15, min(60, self.settings.engine_loop_interval_seconds)))
        while True:
            try:
                watchdog_alerts = await self._detect_alerts({})
                self.last_watchdog_at_ms = _now_ms()
                self.last_alerts = watchdog_alerts
                await self._send_watchdog_alerts(watchdog_alerts)
                digest_due = self.last_digest_at_ms is None or (
                    self.last_watchdog_at_ms - self.last_digest_at_ms
                    >= max(60, self.settings.engine_validation_digest_interval_seconds) * 1000
                )
                if digest_due:
                    await self.run_once(post=True)
                self.last_error = None
            except Exception as exc:  # pragma: no cover - runtime safety net
                self.last_error = type(exc).__name__
                ENGINE_VALIDATION_DIGESTS.labels(result="error").inc()
                log.warning("engine_validation_digest_failed", error=type(exc).__name__)
            await asyncio.sleep(max(10, self.settings.engine_validation_watchdog_interval_seconds))

    async def _send(
        self,
        content: str,
        *,
        category: str,
        dedupe_key: str,
        severity: str,
    ) -> None:
        channel_id = str(self.settings.autonomy_alert_channel_id or "").strip()
        if not channel_id:
            ENGINE_VALIDATION_DIGESTS.labels(result="skipped").inc()
            return
        try:
            sent = False
            enqueue = getattr(self.repository, "enqueue_operational_notification", None)
            chunks = _chunk(content, self.settings.discord_max_response_chars)
            if callable(enqueue):
                for index, chunk in enumerate(chunks):
                    notification_id = await enqueue(
                        dedupe_key=f"{dedupe_key}:{index}",
                        category=category,
                        severity=severity,
                        source_type="engine_validation_monitor",
                        source_id=str(self.last_watchdog_at_ms or self.last_digest_at_ms or _now_ms()),
                        channel_id=channel_id,
                        payload={"content": chunk},
                    )
                    sent = sent or bool(notification_id)
            elif self.alert_sink is not None:  # compatibility fallback for isolated tests
                for chunk in chunks:
                    result = await self.alert_sink.send(channel_id, chunk)
                    sent = sent or bool(result)
            ENGINE_VALIDATION_DIGESTS.labels(result="ok" if sent else "skipped").inc()
        except Exception as exc:  # pragma: no cover
            ENGINE_VALIDATION_DIGESTS.labels(result="error").inc()
            log.warning("engine_validation_notification_enqueue_failed", error=type(exc).__name__)

    async def _send_watchdog_alerts(self, alerts: list[dict[str, Any]]) -> None:
        now = _now_ms()
        bucket_seconds = max(300, self.settings.engine_validation_alert_stale_loop_seconds)
        bucket = now // (bucket_seconds * 1000)
        for alert in alerts:
            alert_type = str(alert.get("type") or "unknown")
            severity = str(alert.get("severity") or "warning")
            ENGINE_VALIDATION_ALERTS.labels(alert_type=alert_type).inc()
            await self._send(
                f"{'🚨' if severity == 'critical' else '⚠️'} **Engine validation alert** `{alert_type}`\n{alert.get('detail')}",
                category="engine_validation_alert",
                dedupe_key=f"engine-validation-alert:{alert_type}:{bucket}",
                severity=severity,
            )

    async def _detect_alerts(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        now = _now_ms()
        service_status = await self._engine_status()
        last_run_at = service_status.get("last_run_at_ms")
        stale_after_ms = max(1, self.settings.engine_validation_alert_stale_loop_seconds) * 1000
        if self.settings.engine_enabled and (not last_run_at or now - int(last_run_at) > stale_after_ms):
            if last_run_at or now - self.started_at_ms > stale_after_ms:
                alerts.append(
                    {
                        "type": "engine_loop_stale",
                        "severity": "critical",
                        "detail": f"last_run_at_ms={last_run_at}; stale_after_ms={stale_after_ms}",
                    }
                )
        if service_status.get("last_error"):
            alerts.append({"type": "engine_loop_error", "severity": "critical", "detail": str(service_status.get("last_error"))})
        newsfeed_health = await self._newsfeed_health()
        if newsfeed_health is not None:
            for reason in newsfeed_health.get("reasons") or []:
                alerts.append(
                    {
                        "type": f"newsfeed_{reason.get('code') or 'degraded'}",
                        "severity": "critical" if reason.get("severity") == "degraded" else "warning",
                        "detail": str(reason.get("detail") or "Engine Newswire consumer is degraded."),
                    }
                )
        duration_ms = service_status.get("last_run_duration_ms")
        interval_ms = max(5, int(self.settings.engine_loop_interval_seconds)) * 1000
        if duration_ms is not None and float(duration_ms) > interval_ms:
            stage_ms = service_status.get("last_stage_ms") if isinstance(service_status.get("last_stage_ms"), dict) else {}
            slowest_stage = max(stage_ms, key=lambda name: float(stage_ms.get(name) or 0)) if stage_ms else "unknown"
            alerts.append(
                {
                    "type": "engine_loop_duration_overrun",
                    "severity": "critical" if float(duration_ms) > 3 * interval_ms else "warning",
                    "detail": f"last_run_duration_ms={duration_ms} interval_ms={interval_ms} slowest_stage={slowest_stage}",
                }
            )

        shadow_only = self.settings.engine_shadow_enabled and not self.settings.engine_paper_enabled and self.settings.engine_execution_mode_list == ["shadow"]
        if shadow_only:
            paper_intents = await self.repository.list_order_intents(execution_mode="paper", limit=10)
            paper_reports = [item for item in await self.repository.list_execution_reports(limit=50) if item.get("execution_mode") == "paper"]
            if paper_intents or paper_reports:
                alerts.append(
                    {
                        "type": "paper_intent_in_shadow_only",
                        "severity": "critical",
                        "detail": f"paper_intents={len(paper_intents)} paper_reports={len(paper_reports)}",
                    }
                )
        if self.settings.engine_live_enabled:
            alerts.append({"type": "live_engine_enabled", "severity": "critical", "detail": "ENGINE_LIVE_ENABLED=true"})

        recent_rejects = await self._recent_risk_rejects(now)
        if len(recent_rejects) >= self.settings.engine_validation_risk_reject_spike_count:
            alerts.append(
                {
                    "type": "risk_rejects_spike",
                    "severity": "warning",
                    "detail": f"{len(recent_rejects)} rejects in the last digest window",
                }
            )

        missing = await self._missing_data(now)
        for item in missing:
            alerts.append({"type": "missing_feature_or_regime_data", "severity": "warning", "detail": item})

        for bucket, values in (report.get("ev_calibration", {}).get("bucket_summary") or {}).items():
            sample_count = int(values.get("realized_sample_count") or 0)
            avg_ev = float(values.get("avg_net_ev_bps") or 0)
            avg_realized = float(values.get("avg_realized_pnl_usd") or 0)
            if sample_count >= self.settings.engine_validation_ev_drift_min_samples and avg_ev > 0 and avg_realized <= self.settings.engine_validation_ev_drift_loss_usd:
                alerts.append(
                    {
                        "type": "ev_calibration_drift",
                        "severity": "warning",
                        "detail": f"bucket={bucket} samples={sample_count} avg_ev_bps={avg_ev:.2f} avg_realized_pnl_usd={avg_realized:.2f}",
                    }
                )
        return alerts

    async def _newsfeed_health(self) -> dict[str, Any] | None:
        list_heartbeats = getattr(self.repository, "list_service_heartbeats", None)
        if not self.settings.engine_newsfeed_enabled or not callable(list_heartbeats):
            return None
        try:
            heartbeats = await list_heartbeats(service_role="trader", limit=5)
            trader = next((item for item in heartbeats if item.get("status") == "running"), None)
            raw_metadata = trader.get("metadata") if isinstance(trader, dict) else None
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            raw_runtime = metadata.get("engine_newsfeed")
            runtime = dict(raw_runtime) if isinstance(raw_runtime, dict) else {}
            offset = await self.repository.get_consumer_offset(
                "trader:engine_newswire",
                source_table="newswire_story_revisions",
            )
            stories = await self.repository.list_newswire_stories(limit=1)
            newswire_heartbeats = await list_heartbeats(service_role="newswire", limit=5)
        except Exception:
            return None
        return build_engine_newsfeed_health(
            self.settings,
            runtime,
            offset,
            newswire_active=bool(
                stories and any(item.get("status") == "running" for item in newswire_heartbeats)
            ),
            latest_source_at_ms=int(stories[0].get("last_updated_at_ms") or 0) if stories else None,
        )

    async def _recent_risk_rejects(self, now_ms: int) -> list[dict[str, Any]]:
        window_ms = max(60, self.settings.engine_validation_digest_interval_seconds) * 1000
        items = await self.repository.list_risk_gateway_decisions(limit=500, decision="reject")
        return [item for item in items if int(item.get("created_at_ms") or 0) >= now_ms - window_ms]

    async def _missing_data(self, now_ms: int) -> list[str]:
        max_age_ms = max(1, self.settings.engine_validation_missing_data_seconds) * 1000
        missing: list[str] = []
        for asset in self.settings.autonomy_core_symbols:
            features = await self.repository.list_feature_values(asset=asset, limit=1)
            if not features:
                missing.append(f"{asset}: no feature values")
            else:
                computed = int(features[0].get("computed_ts_ms") or 0)
                if computed and now_ms - computed > max_age_ms:
                    missing.append(f"{asset}: latest feature stale by {(now_ms - computed) // 1000}s")
            regime = await self.repository.latest_regime_snapshot(primary_asset=asset)
            if regime is None:
                missing.append(f"{asset}: no regime snapshot")
            else:
                as_of_ms = int(regime.get("as_of_ms") or regime.get("created_at_ms") or 0)
                if as_of_ms and now_ms - as_of_ms > max_age_ms:
                    missing.append(f"{asset}: latest regime stale by {(now_ms - as_of_ms) // 1000}s")
        return missing

    async def _engine_status(self) -> dict[str, Any]:
        return await resolve_engine_runtime(
            self.repository,
            self.settings,
            local_service=self.engine_service,
            generated_at_ms=_now_ms(),
        )


def format_engine_validation_digest(
    report: dict[str, Any],
    alerts: list[dict[str, Any]],
    *,
    settings: Settings,
    service_status: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    latest_replay: dict[str, Any] | None = None,
) -> str:
    service_status = service_status or {}
    summary = report.get("summary") or {}
    execution = report.get("execution_simulations") or {}
    by_strategy = report.get("by_strategy") or {}
    buckets = report.get("ev_calibration", {}).get("bucket_summary") or {}
    readiness = readiness or {}
    mode = "shadow-only" if settings.engine_shadow_enabled and not settings.engine_paper_enabled else "paper/shadow"
    lines = [
        f"🧪 **Engine validation digest — {mode}**",
        f"Loop: runs `{service_status.get('run_count', 0)}` | last error `{service_status.get('last_error') or 'none'}` | last run `{service_status.get('last_run_at_ms') or 'n/a'}`",
        f"Candidates `{summary.get('candidate_count', 0)}` | EVs `{summary.get('ev_estimate_count', 0)}` | allocations `{summary.get('allocated_count', 0)}/{summary.get('allocation_count', 0)}` ({summary.get('allocation_rate_pct', 0)}%)",
        f"Intents shadow/paper `{summary.get('shadow_intent_count', 0)}`/`{summary.get('paper_intent_count', 0)}` | reports `{summary.get('execution_report_count', 0)}` | risk rejects `{summary.get('risk_reject_count', 0)}`",
        f"Sim avg slippage `{execution.get('avg_slippage_bps', 0)}` bps | fees `${execution.get('fees_usd', 0)}` | open positions `{summary.get('open_position_count', 0)}`",
        f"Readiness: `{str(readiness.get('grade') or 'unknown').upper()}` `{readiness.get('score', 'n/a')}/100` | hard blocks `{len(readiness.get('hard_blocks') or [])}` | recommendation `{readiness.get('recommendation') or 'n/a'}`",
        "",
    ]
    if alerts:
        lines.append("**Alerts:**")
        for alert in alerts[:10]:
            icon = "🚨" if alert.get("severity") == "critical" else "⚠️"
            lines.append(f"{icon} `{alert.get('type')}` — {alert.get('detail')}")
    else:
        lines.append("✅ **No alert conditions detected.**")
    lines.append("")
    if latest_replay:
        metadata = latest_replay.get("metadata") or {}
        diffs = latest_replay.get("diffs") or {}
        lines.append(f"**Latest replay:** `{metadata.get('verdict', latest_replay.get('status'))}` variant `{metadata.get('variant_id', '-')}` | EV Δ `{diffs.get('avg_net_ev_delta_bps', 0)}` bps | reject Δ `{diffs.get('risk_reject_rate_delta_pct', 0)}`%")
        lines.append("")
    throttle_summary = (service_status or {}).get("last_throttle_summary") or {}
    controller = throttle_summary.get("controller") or (service_status or {}).get("throttles") or {}
    if controller:
        lines.append("**Throttles:**")
        reason_counts = controller.get("reason_counts") or {}
        recent_share = controller.get("last_recent_share_pct") or {}
        if reason_counts:
            lines.append("- Reasons: " + ", ".join(f"`{key}`={value}" for key, value in list(reason_counts.items())[:6]))
        if recent_share:
            lines.append("- Recent allocation share: " + ", ".join(f"`{key}`={value}%" for key, value in list(recent_share.items())[:6]))
        if not reason_counts and not recent_share:
            lines.append("- No throttle blocks observed in this process yet.")
        lines.append("")
    lines.append("**Top strategies:**")
    ranked = sorted(by_strategy.items(), key=lambda item: (item[1].get("allocated_count", 0), item[1].get("candidate_count", 0)), reverse=True)
    for strategy, values in ranked[:6]:
        lines.append(
            f"- `{strategy}` candidates `{values.get('candidate_count', 0)}` | allocated `{values.get('allocated_count', 0)}` | shadow `{values.get('shadow_intent_count', 0)}` | EV `{values.get('avg_net_ev_bps', 0)}` bps | PnL `${values.get('total_pnl_usd', 0)}`"
        )
    if not ranked:
        lines.append("- no strategies observed yet")
    lines.append("")
    lines.append("**EV buckets:**")
    for bucket, values in list(buckets.items())[:6]:
        lines.append(
            f"- `{bucket}` count `{values.get('count', 0)}` | avg EV `{values.get('avg_net_ev_bps', 0)}` bps | uncertainty `{values.get('avg_uncertainty', 0)}` | realized samples `{values.get('realized_sample_count', 0)}`"
        )
    if not buckets:
        lines.append("- no EV buckets observed yet")
    lines.append("")
    lines.append("Dashboard: `/engine/dashboard` | JSON: `/engine/validation-report`")
    return "\n".join(lines)
