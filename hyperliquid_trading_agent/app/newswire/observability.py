from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings

ENGINE_NEWSWIRE_CONSUMER = "trader:engine_newswire"


def build_engine_newsfeed_health(
    settings: Settings,
    runtime: dict[str, Any] | None,
    offset: dict[str, Any] | None,
    *,
    newswire_active: bool,
    latest_source_at_ms: int | None = None,
    generated_at_ms: int | None = None,
) -> dict[str, Any]:
    now_ms = int(generated_at_ms or _now_ms())
    runtime_data = _as_dict(runtime)
    consumer = _as_dict(runtime_data.get("consumer"))
    pump = _as_dict(runtime_data.get("pump"))
    offset_data = _as_dict(offset)
    updated_at_ms = int(offset_data.get("updated_at_ms") or 0)
    offset_event_ts_ms = int(offset_data.get("last_event_ts_ms") or 0)
    source_at_ms = int(latest_source_at_ms or 0)
    source_lag_ms = max(0, source_at_ms - offset_event_ts_ms) if source_at_ms > 0 else None
    source_ahead = bool(source_at_ms > 0 and source_at_ms > offset_event_ts_ms)
    offset_age_ms = max(0, now_ms - updated_at_ms) if updated_at_ms > 0 else None
    stale_after_ms = max(1, int(settings.newswire_engine_offset_stale_seconds)) * 1000
    reasons: list[dict[str, str]] = []

    configured_for_local_role = bool(settings.engine_enabled and settings.engine_newsfeed_enabled)
    runtime_detected = bool(consumer.get("running") or pump.get("running"))
    enabled = bool(configured_for_local_role or runtime_detected)
    if not enabled:
        return {
            "status": "disabled",
            "enabled": False,
            "configured_for_local_role": configured_for_local_role,
            "runtime_detected": runtime_detected,
            "healthy": True,
            "reasons": [],
            "offset_age_ms": offset_age_ms,
            "stale_after_ms": stale_after_ms,
            "newswire_active": newswire_active,
            "latest_source_at_ms": source_at_ms or None,
            "source_lag_ms": source_lag_ms,
        }

    if not bool(consumer.get("running")) or not bool(pump.get("running")):
        reasons.append(_reason("consumer_not_running", "degraded", "Trader Newswire consumer or durable pump is not running."))
    if newswire_active and source_at_ms > 0 and not offset_data.get("last_event_id"):
        reasons.append(_reason("live_offset_missing", "degraded", "Newswire is active but the trader consumer has no durable offset."))
    if newswire_active and source_ahead and offset_age_ms is not None and offset_age_ms > stale_after_ms:
        reasons.append(
            _reason(
                "live_offset_stale",
                "degraded",
                f"Offset age {offset_age_ms}ms exceeds {stale_after_ms}ms while Newswire is active.",
            )
        )
    if int(pump.get("error_count") or 0) > 0:
        reasons.append(_reason("pump_errors", "degraded", f"pump.error_count={pump.get('error_count')}"))
    if int(consumer.get("error_count") or 0) > 0:
        reasons.append(_reason("consumer_errors", "degraded", f"consumer.error_count={consumer.get('error_count')}"))
    if int(pump.get("invalid_rows_skipped") or 0) > 0:
        reasons.append(
            _reason(
                "invalid_rows_skipped",
                "warning",
                f"pump.invalid_rows_skipped={pump.get('invalid_rows_skipped')}",
            )
        )
    received = int(consumer.get("received_events") or 0)
    recorded = int(consumer.get("recorded_events") or 0)
    features = int(consumer.get("features_created") or 0)
    if received >= 5 and recorded == 0:
        reasons.append(_reason("received_without_records", "warning", f"received_events={received} recorded_events=0"))
    if recorded >= 5 and features == 0:
        reasons.append(_reason("records_without_features", "warning", f"recorded_events={recorded} features_created=0"))

    status = "degraded" if any(item["severity"] == "degraded" for item in reasons) else "warning" if reasons else "healthy"
    return {
        "status": status,
        "enabled": True,
        "configured_for_local_role": configured_for_local_role,
        "runtime_detected": runtime_detected,
        "healthy": status == "healthy",
        "reasons": reasons,
        "offset_age_ms": offset_age_ms,
        "stale_after_ms": stale_after_ms,
        "newswire_active": newswire_active,
        "latest_source_at_ms": source_at_ms or None,
        "source_lag_ms": source_lag_ms,
        "consumer_name": ENGINE_NEWSWIRE_CONSUMER,
        "counters": {
            "pump_processed": int(pump.get("processed") or 0),
            "invalid_rows_skipped": int(pump.get("invalid_rows_skipped") or 0),
            "pump_errors": int(pump.get("error_count") or 0),
            "received": received,
            "recorded": recorded,
            "features_created": features,
            "skipped": int(consumer.get("skipped_events") or 0),
            "consumer_errors": int(consumer.get("error_count") or 0),
        },
        "skip_reasons": dict(consumer.get("skip_reasons") or {}),
        "offset": offset_data,
    }


async def build_newswire_soak_readiness(
    repository: Any,
    settings: Settings,
    *,
    generated_at_ms: int | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    now_ms = int(generated_at_ms or _now_ms())
    bounded_limit = max(100, min(20_000, int(limit)))
    heartbeats = await _safe_list(repository, "list_service_heartbeats", limit=100)
    newswire_worker = _running_heartbeat(heartbeats, "newswire")
    trader_worker = _running_heartbeat(heartbeats, "trader")
    runtime = _runtime_metadata(trader_worker, "engine_newsfeed")
    offset = _as_dict(
        await _safe_call(
            repository,
            "get_consumer_offset",
            ENGINE_NEWSWIRE_CONSUMER,
            source_table="newswire_story_revisions",
            default={},
        )
    )
    latest_stories = await _safe_list(repository, "list_newswire_stories", limit=1)
    newswire_active = bool(newswire_worker and latest_stories)
    latest_source_at_ms = int(latest_stories[0].get("last_updated_at_ms") or 0) if latest_stories else None
    health = build_engine_newsfeed_health(
        settings,
        runtime,
        offset,
        newswire_active=newswire_active,
        latest_source_at_ms=latest_source_at_ms,
        generated_at_ms=now_ms,
    )

    starts = [
        int(item.get("started_at_ms") or 0)
        for item in (newswire_worker, trader_worker)
        if isinstance(item, dict) and int(item.get("started_at_ms") or 0) > 0
    ]
    continuous_start_ms = max(starts) if len(starts) == 2 else None
    elapsed_ms = max(0, now_ms - continuous_start_ms) if continuous_start_ms is not None else 0
    required_ms = max(24, int(settings.newswire_soak_required_hours)) * 3_600_000

    events = await _safe_list(repository, "list_newswire_events", limit=bounded_limit)
    revisions = await _safe_list(repository, "list_newswire_story_revisions", limit=bounded_limit)
    normalized = await _safe_list(repository, "list_normalized_events", event_type="newswire", limit=bounded_limit)
    features = await _safe_list(repository, "list_feature_values", limit=bounded_limit)
    intents = await _safe_list(repository, "list_order_intents", limit=bounded_limit)
    reports = await _safe_list(repository, "list_execution_reports", since_ms=continuous_start_ms, limit=bounded_limit)
    start = int(continuous_start_ms or now_ms)
    events = [item for item in events if int(item.get("received_at_ms") or 0) >= start]
    revisions = [item for item in revisions if int(item.get("emitted_at_ms") or 0) >= start]
    normalized = [
        item
        for item in normalized
        if int(item.get("received_ts_ms") or 0) >= start and not bool((item.get("metadata") or {}).get("replay"))
    ]
    features = [
        item
        for item in features
        if int(item.get("computed_ts_ms") or 0) >= start
        and str(item.get("feature_group") or "") == "news"
        and not bool((item.get("metadata") or {}).get("replay"))
    ]
    intents = [item for item in intents if int(item.get("created_at_ms") or 0) >= start]
    paper_or_live_intents = [item for item in intents if item.get("execution_mode") in {"paper", "live"}]
    paper_or_live_reports = [item for item in reports if item.get("execution_mode") in {"paper", "live"}]
    consumer = _as_dict(runtime.get("consumer"))
    pump = _as_dict(runtime.get("pump"))

    criteria = {
        "continuous_window_complete": elapsed_ms >= required_ms,
        "workers_running": newswire_worker is not None and trader_worker is not None,
        "offset_advancing": bool(offset.get("last_event_id")) and health.get("status") != "degraded",
        "newswire_rows_ingested": len(events) > 0,
        "engine_rows_consumed": int(pump.get("processed") or 0) > 0 or int(consumer.get("received_events") or 0) > 0,
        "normalized_events_present": len(normalized) > 0,
        "news_features_present": len(features) > 0,
        "no_invalid_rows_or_errors": (
            int(pump.get("invalid_rows_skipped") or 0) == 0
            and int(pump.get("error_count") or 0) == 0
            and int(consumer.get("error_count") or 0) == 0
        ),
        "no_paper_or_live_execution_side_effects": not paper_or_live_intents and not paper_or_live_reports,
    }
    blockers = [name for name, passed in criteria.items() if not passed]
    ready = not blockers
    return {
        "ready": ready,
        "status": "passed" if ready else "collecting_evidence",
        "assessment": "time_based_continuous_worker_soak",
        "required_hours": max(24, int(settings.newswire_soak_required_hours)),
        "continuous_start_ms": continuous_start_ms,
        "elapsed_ms": elapsed_ms,
        "elapsed_hours": round(elapsed_ms / 3_600_000, 4),
        "remaining_ms": max(0, required_ms - elapsed_ms),
        "criteria": criteria,
        "blockers": blockers,
        "counts": {
            "newswire_rows_ingested": len(events),
            "story_revisions": len(revisions),
            "pump_processed": int(pump.get("processed") or 0),
            "consumer_received": int(consumer.get("received_events") or 0),
            "consumer_recorded": int(consumer.get("recorded_events") or 0),
            "consumer_features_created": int(consumer.get("features_created") or 0),
            "normalized_newswire_events": len(normalized),
            "news_feature_rows": len(features),
            "invalid_rows": int(pump.get("invalid_rows_skipped") or 0),
            "pump_errors": int(pump.get("error_count") or 0),
            "consumer_errors": int(consumer.get("error_count") or 0),
            "paper_or_live_order_intents": len(paper_or_live_intents),
            "paper_or_live_execution_reports": len(paper_or_live_reports),
        },
        "skip_reasons": dict(consumer.get("skip_reasons") or {}),
        "health": health,
        "restart_resets_continuous_window": True,
        "paper_signoff_role": "evidence_gate" if ready else "advisory_until_soak_passes",
        "execution_authority": "none",
    }


def _reason(code: str, severity: str, detail: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _running_heartbeat(rows: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in rows
            if str(row.get("service_role") or "") == role and str(row.get("status") or "") == "running"
        ),
        None,
    )


def _runtime_metadata(heartbeat: dict[str, Any] | None, key: str) -> dict[str, Any]:
    metadata = heartbeat.get("metadata") if isinstance(heartbeat, dict) else {}
    value = metadata.get(key) if isinstance(metadata, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


async def _safe_list(repository: Any, method_name: str, **kwargs: Any) -> list[dict[str, Any]]:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return []
    try:
        rows = await method(**kwargs)
    except TypeError:
        try:
            rows = await method()
        except Exception:
            return []
    except Exception:
        return []
    return [dict(item) for item in rows or [] if isinstance(item, dict)]


async def _safe_call(
    repository: Any,
    method_name: str,
    *args: Any,
    default: Any,
    **kwargs: Any,
) -> Any:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return default
    try:
        return await method(*args, **kwargs)
    except Exception:
        return default


def _now_ms() -> int:
    return int(time.time() * 1000)
