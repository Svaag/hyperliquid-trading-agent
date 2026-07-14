from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.alpha.wave2 import WAVE_2_IDS
from hyperliquid_trading_agent.app.engine.attribution import OUTCOME_WINDOWS_MS
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
from hyperliquid_trading_agent.app.engine.runtime import resolve_engine_runtime
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pct(numerator: float, denominator: float) -> float:
    return round(numerator / denominator * 100, 4) if denominator else 0.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ts(item: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = _i(item.get(key), 0)
        if value > 0:
            return value
    return 0


def _in_window(items: list[dict[str, Any]], start_ms: int, *timestamp_keys: str) -> list[dict[str, Any]]:
    return [item for item in items if _ts(item, *timestamp_keys) >= start_ms]


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _source_integrity(item: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(item)
    value = item.get("source_integrity") or metadata.get("source_integrity")
    return value if isinstance(value, dict) else {}


def _activation_scope(item: dict[str, Any]) -> str:
    metadata = _metadata(item)
    source = _source_integrity(item)
    return str(metadata.get("activation_scope") or source.get("activation_scope") or "paper_shadow")


def _paper_eligible(item: dict[str, Any]) -> bool:
    metadata = _metadata(item)
    source = _source_integrity(item)
    value = metadata.get("paper_eligible", source.get("paper_eligible", True))
    return bool(value) and _activation_scope(item) != "shadow_only"


def _shadow_research(item: dict[str, Any]) -> bool:
    metadata = _metadata(item)
    source = _source_integrity(item)
    return _activation_scope(item) == "shadow_only" or bool(metadata.get("operator_promotion_required") or source.get("operator_promotion_required"))


def _candidate_metadata_complete(item: dict[str, Any]) -> bool:
    metadata = _metadata(item)
    strategy_version = metadata.get("strategy_version") or item.get("strategy_version")
    strategy_family = metadata.get("strategy_family") or item.get("strategy_family")
    feature_coverage = metadata.get("feature_coverage_pct", item.get("feature_coverage_pct"))
    return bool(
        item.get("strategy_id")
        and strategy_version
        and strategy_version != "unknown"
        and strategy_family
        and strategy_family != "unknown"
        and item.get("regime_snapshot_id")
        and feature_coverage is not None
    )


def _issue(code: str, detail: str, *, severity: str = "critical") -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


async def _maybe_list(repository: Any, method_name: str, **kwargs) -> list[dict[str, Any]]:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return []
    try:
        return await method(**kwargs)
    except TypeError:
        return await method()


async def _latest_trader_engine_loop_status(repository: Any, settings: Settings, *, generated_at_ms: int) -> dict[str, Any]:
    runtime = await resolve_engine_runtime(repository, settings, generated_at_ms=generated_at_ms)
    return runtime if runtime.get("runtime_source") == "trader_heartbeat" else {}


async def _engine_service_status(repository: Any, settings: Settings, engine_service: Any | None, *, generated_at_ms: int) -> dict[str, Any]:
    return await resolve_engine_runtime(
        repository,
        settings,
        local_service=engine_service,
        generated_at_ms=generated_at_ms,
    )


async def build_paper_readiness_scorecard(
    repository: Any,
    settings: Settings,
    engine_service: Any | None,
    *,
    window_hours: int | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Return a deterministic paper-mode promotion scorecard.

    The scorecard is intentionally conservative: critical safety/data failures are
    hard blocks regardless of the numeric score. It is read-only and does not
    alter engine, exchange, or configuration state.
    """

    generated_at_ms = _now_ms()
    hours = int(window_hours or settings.engine_readiness_window_hours)
    window_ms = max(1, hours) * 60 * 60 * 1000
    start_ms = max(generated_at_ms - window_ms, int(getattr(settings, "engine_readiness_clean_window_start_ms", 0) or 0))
    service_status = await _engine_service_status(repository, settings, engine_service, generated_at_ms=generated_at_ms)
    engine_enabled = bool(service_status.get("enabled", settings.engine_enabled))
    live_enabled = bool(service_status.get("live_enabled", settings.engine_live_enabled))
    paper_enabled = bool(service_status.get("paper_enabled", settings.engine_paper_enabled))
    shadow_enabled = bool(service_status.get("shadow_enabled", settings.engine_shadow_enabled))
    execution_modes = service_status.get("execution_modes") or settings.engine_execution_mode_list

    expanded_limit = max(limit, int(settings.engine_readiness_min_candidates), int(settings.engine_readiness_min_shadow_intents))
    evidence_limit = max(expanded_limit * 2, 5000)
    risk_limit = max(expanded_limit * 5, 5000)
    outcome_limit = max(expanded_limit * (len(OUTCOME_WINDOWS_MS) + 2), 10_000)

    report = await build_engine_validation_report(
        repository,
        limit=expanded_limit,
        settings=settings,
        window_hours=hours,
    )
    candidates_all = await repository.list_alpha_candidates(since_ms=start_ms, until_ms=generated_at_ms, limit=expanded_limit)
    ev_all = await repository.list_ev_estimates(since_ms=start_ms, until_ms=generated_at_ms, limit=expanded_limit)
    allocations_all = await repository.list_allocation_decisions(since_ms=start_ms, until_ms=generated_at_ms, limit=expanded_limit)
    intents_all = await repository.list_order_intents(since_ms=start_ms, until_ms=generated_at_ms, limit=expanded_limit)
    reports_all = await repository.list_execution_reports(since_ms=start_ms, until_ms=generated_at_ms, limit=expanded_limit)
    risk_all = await repository.list_risk_gateway_decisions(since_ms=start_ms, until_ms=generated_at_ms, limit=risk_limit)
    risk_rejects_all = [item for item in risk_all if item.get("decision") == "reject"]
    positions_all = await repository.list_position_theses(limit=expanded_limit)
    pnl_all = await repository.list_pnl_attribution(since_ms=start_ms, until_ms=generated_at_ms, limit=expanded_limit)
    council_all = await _maybe_list(repository, "list_council_reviews", since_ms=start_ms, until_ms=generated_at_ms, limit=evidence_limit)
    candidate_evidence_links_all = await _maybe_list(repository, "list_candidate_evidence_links", since_ms=start_ms, until_ms=generated_at_ms, limit=evidence_limit)
    candidate_outcomes_all = await _maybe_list(repository, "list_candidate_outcome_attributions", since_ms=start_ms, until_ms=generated_at_ms, limit=outcome_limit)
    portfolio_concentration_events_all = await _maybe_list(repository, "list_portfolio_concentration_events", since_ms=start_ms, until_ms=generated_at_ms, limit=evidence_limit)
    strategy_regime_performance_all = await _maybe_list(repository, "list_strategy_regime_performance", since_ms=start_ms, until_ms=generated_at_ms, limit=evidence_limit)
    latest_replay_comparison = await latest_engine_replay_comparison(repository)
    exact_method = getattr(repository, "get_engine_readiness_aggregates", None)
    exact: dict[str, Any] = {}
    if callable(exact_method):
        try:
            exact = await exact_method(start_ms=start_ms, end_ms=generated_at_ms)
        except Exception:
            exact = {}
    exact_window = exact.get("window") if isinstance(exact.get("window"), dict) else {}
    exact_counts = exact.get("counts") if isinstance(exact.get("counts"), dict) else {}
    exact_coverage = exact.get("coverage") if isinstance(exact.get("coverage"), dict) else {}
    exact_breadth = exact.get("breadth") if isinstance(exact.get("breadth"), dict) else {}
    exact_concentration = exact.get("concentration") if isinstance(exact.get("concentration"), dict) else {}
    exact_execution = exact.get("execution") if isinstance(exact.get("execution"), dict) else {}
    strict_performance = exact.get("strict_performance") if isinstance(exact.get("strict_performance"), dict) else {}

    candidates = _in_window(candidates_all, start_ms, "created_at_ms")
    ev_estimates = _in_window(ev_all, start_ms, "created_at_ms")
    allocations = _in_window(allocations_all, start_ms, "created_at_ms")
    intents = _in_window(intents_all, start_ms, "created_at_ms")
    reports = _in_window(reports_all, start_ms, "created_at_ms")
    risk_decisions = _in_window(risk_all, start_ms, "created_at_ms")
    risk_rejects = _in_window(risk_rejects_all, start_ms, "created_at_ms")
    positions = _in_window(positions_all, start_ms, "updated_at_ms", "opened_at_ms")
    pnl_records = _in_window(pnl_all, start_ms, "window_end_ms")
    council_reviews = _in_window(council_all, start_ms, "created_at_ms")
    candidate_evidence_links = _in_window(candidate_evidence_links_all, start_ms, "created_at_ms")
    candidate_outcomes = _in_window(candidate_outcomes_all, start_ms, "created_at_ms", "window_end_ms")
    portfolio_concentration_events = _in_window(portfolio_concentration_events_all, start_ms, "created_at_ms")
    strategy_regime_performance = _in_window(strategy_regime_performance_all, start_ms, "created_at_ms", "window_end_ms")

    hard_blocks: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: dict[str, Any] = {}

    # Shadow/live safety integrity.
    paper_intents = [item for item in intents if item.get("execution_mode") == "paper"]
    paper_reports = [item for item in reports if item.get("execution_mode") == "paper"]
    live_intents = [item for item in intents if item.get("execution_mode") == "live"]
    live_reports = [item for item in reports if item.get("execution_mode") == "live"]
    paper_intent_count = _i(exact_counts.get("paper_intent_count"), len(paper_intents)) if exact_counts else len(paper_intents)
    paper_report_count = _i(exact_counts.get("paper_report_count"), len(paper_reports)) if exact_counts else len(paper_reports)
    live_intent_count = _i(exact_counts.get("live_intent_count"), len(live_intents)) if exact_counts else len(live_intents)
    live_report_count = _i(exact_counts.get("live_report_count"), len(live_reports)) if exact_counts else len(live_reports)
    shadow_only = shadow_enabled and not paper_enabled and execution_modes == ["shadow"]
    if live_enabled:
        hard_blocks.append(_issue("live_enabled", "ENGINE_LIVE_ENABLED must remain false."))
    if shadow_only and (paper_intent_count or paper_report_count):
        hard_blocks.append(_issue("paper_intents_in_shadow_only", f"paper_intents={paper_intent_count} paper_reports={paper_report_count}"))
    if live_intent_count or live_report_count:
        hard_blocks.append(_issue("live_intents_present", f"live_intents={live_intent_count} live_reports={live_report_count}"))
    checks["shadow_integrity"] = {
        "shadow_only": shadow_only,
        "paper_intent_count": paper_intent_count,
        "paper_report_count": paper_report_count,
        "live_intent_count": live_intent_count,
        "live_report_count": live_report_count,
        "live_enabled": live_enabled,
    }

    # Reliability and observation sample.
    last_run_at = service_status.get("last_run_at_ms")
    last_run_completed_at_ms = service_status.get("last_run_completed_at_ms") or service_status.get("last_successful_run_completed_at_ms") or last_run_at
    run_in_progress = bool(service_status.get("run_in_progress"))
    current_run_started_at_ms = service_status.get("current_run_started_at_ms")
    stale_after_ms = max(1, settings.engine_validation_alert_stale_loop_seconds) * 1000
    if engine_enabled and run_in_progress and current_run_started_at_ms and generated_at_ms - _i(current_run_started_at_ms) > stale_after_ms:
        hard_blocks.append(_issue("engine_loop_stale", f"current_run_started_at_ms={current_run_started_at_ms}; stuck_after_ms={stale_after_ms}"))
    elif engine_enabled and not run_in_progress and (not last_run_completed_at_ms or generated_at_ms - _i(last_run_completed_at_ms) > stale_after_ms):
        hard_blocks.append(_issue("engine_loop_stale", f"last_run_completed_at_ms={last_run_completed_at_ms}; stale_after_ms={stale_after_ms}"))
    if service_status.get("last_error"):
        hard_blocks.append(_issue("runtime_errors_present", f"last_error={service_status.get('last_error')}"))
    run_count = _i(service_status.get("run_count"), 0)
    if run_count < settings.engine_readiness_min_runs:
        warnings.append(_issue("insufficient_engine_runs", f"Need >={settings.engine_readiness_min_runs} runs; observed {run_count}.", severity="warning"))

    shadow_timestamps = [_ts(item, "created_at_ms") for item in intents_all if item.get("execution_mode") == "shadow"] + [_ts(item, "created_at_ms") for item in candidates_all]
    shadow_timestamps = [item for item in shadow_timestamps if item > 0]
    first_shadow_ms = min(shadow_timestamps) if shadow_timestamps else None
    if exact_window:
        rolling_start_ms = generated_at_ms - window_ms
        history_covers_window = (
            start_ms == rolling_start_ms
            and bool(exact_window.get("candidate_before_window"))
            and bool(exact_window.get("shadow_intent_before_window"))
        )
        exact_first_values = [
            _i(exact_window.get("first_candidate_ms"), 0),
            _i(exact_window.get("first_shadow_intent_ms"), 0),
        ]
        exact_first_values = [value for value in exact_first_values if value > 0]
        first_shadow_ms = start_ms if history_covers_window else min(exact_first_values) if exact_first_values else None
    observed_hours = 0.0
    if first_shadow_ms is not None:
        observed_hours = round((generated_at_ms - max(start_ms, first_shadow_ms)) / 3_600_000, 4)
    if observed_hours < hours:
        hard_blocks.append(_issue("insufficient_shadow_observation", f"Need >={hours}h shadow observation; observed {observed_hours:.2f}h."))
    candidate_count_for_gate = _i(exact_counts.get("candidate_count"), len(candidates)) if exact_counts else len(candidates)
    shadow_intent_count_for_gate = _i(exact_counts.get("shadow_intent_count"), len([item for item in intents if item.get("execution_mode") == "shadow"])) if exact_counts else len([item for item in intents if item.get("execution_mode") == "shadow"])
    if candidate_count_for_gate < settings.engine_readiness_min_candidates or shadow_intent_count_for_gate < settings.engine_readiness_min_shadow_intents:
        hard_blocks.append(
            _issue(
                "insufficient_sample_size",
                f"Need >={settings.engine_readiness_min_candidates} candidates and >={settings.engine_readiness_min_shadow_intents} shadow intents; observed candidates={candidate_count_for_gate} shadow_intents={shadow_intent_count_for_gate}.",
            )
        )
    checks["engine_reliability"] = {
        "last_run_at_ms": last_run_at,
        "last_run_completed_at_ms": last_run_completed_at_ms,
        "run_in_progress": run_in_progress,
        "current_run_started_at_ms": current_run_started_at_ms,
        "last_error": service_status.get("last_error"),
        "run_count": run_count,
        "stale_after_ms": stale_after_ms,
        "observed_shadow_hours": observed_hours,
        "first_shadow_ms": first_shadow_ms,
        "runtime_source": service_status.get("runtime_source"),
        "runtime_instance_id": service_status.get("runtime_instance_id"),
        "runtime_updated_at_ms": service_status.get("runtime_updated_at_ms"),
        "runtime_running": service_status.get("runtime_running"),
        "runtime_stale": service_status.get("runtime_stale"),
        "runtime_age_ms": service_status.get("runtime_age_ms"),
        "last_run_duration_ms": service_status.get("last_run_duration_ms"),
        "last_stage_ms": service_status.get("last_stage_ms"),
    }

    # Data completeness for core symbols.
    feature_ok = 0
    regime_ok = 0
    data_details: dict[str, Any] = {}
    max_data_age_ms = max(1, settings.engine_validation_missing_data_seconds) * 1000
    core_symbols = settings.autonomy_core_symbols or ["BTC", "ETH", "HYPE"]
    for asset in core_symbols:
        features = await repository.list_feature_values(asset=asset, limit=1)
        regime = await repository.latest_regime_snapshot(primary_asset=asset)
        feature_age_ms = None
        regime_age_ms = None
        feature_present = bool(features)
        regime_present = regime is not None
        if features:
            feature_age_ms = generated_at_ms - _ts(features[0], "computed_ts_ms", "received_ts_ms")
            if feature_age_ms <= max_data_age_ms:
                feature_ok += 1
        if regime is not None:
            regime_age_ms = generated_at_ms - _ts(regime, "as_of_ms", "created_at_ms")
            if regime_age_ms <= max_data_age_ms:
                regime_ok += 1
        data_details[asset] = {
            "feature_present": feature_present,
            "feature_age_ms": feature_age_ms,
            "regime_present": regime_present,
            "regime_age_ms": regime_age_ms,
        }
        if not feature_present or not regime_present:
            hard_blocks.append(_issue("missing_core_data", f"{asset}: feature_present={feature_present} regime_present={regime_present}"))
        elif (feature_age_ms is not None and feature_age_ms > max_data_age_ms) or (regime_age_ms is not None and regime_age_ms > max_data_age_ms):
            hard_blocks.append(_issue("missing_core_data", f"{asset}: feature_age_ms={feature_age_ms} regime_age_ms={regime_age_ms}"))
    feature_coverage_pct = _pct(feature_ok, len(core_symbols))
    regime_coverage_pct = _pct(regime_ok, len(core_symbols))
    checks["data_completeness"] = {
        "core_symbols": core_symbols,
        "feature_coverage_pct": feature_coverage_pct,
        "regime_coverage_pct": regime_coverage_pct,
        "details": data_details,
    }

    # Decision quality and risk gateway.
    candidate_ids = {str(item.get("candidate_id")) for item in candidates if item.get("candidate_id")}
    ev_candidate_ids = {str(item.get("candidate_id")) for item in ev_estimates if item.get("candidate_id")}
    ev_coverage_pct = _pct(
        _i(exact_coverage.get("ev_covered_candidate_count"), len(candidate_ids & ev_candidate_ids)),
        candidate_count_for_gate,
    ) if exact_coverage else _pct(len(candidate_ids & ev_candidate_ids), len(candidate_ids))
    allocated_count = _i(exact_counts.get("allocated_count"), len([item for item in allocations if item.get("status") in {"allocate", "reduce", "require_debate"}])) if exact_counts else len([item for item in allocations if item.get("status") in {"allocate", "reduce", "require_debate"}])
    allocation_count_for_gate = _i(exact_counts.get("allocation_count"), len(allocations)) if exact_counts else len(allocations)
    allocation_rate_pct = _pct(allocated_count, allocation_count_for_gate)
    if ev_coverage_pct < settings.engine_readiness_min_ev_coverage_pct:
        warnings.append(_issue("low_ev_coverage", f"EV coverage {ev_coverage_pct}% below {settings.engine_readiness_min_ev_coverage_pct}%.", severity="warning"))
    if allocation_rate_pct < settings.engine_readiness_min_allocation_rate_pct or allocation_rate_pct > settings.engine_readiness_max_allocation_rate_pct:
        warnings.append(_issue("allocation_rate_out_of_bounds", f"Allocation rate {allocation_rate_pct}% outside {settings.engine_readiness_min_allocation_rate_pct}-{settings.engine_readiness_max_allocation_rate_pct}%.", severity="warning"))
    metadata_complete = [_candidate_metadata_complete(item) for item in candidates]
    candidate_strategy_metadata_coverage_pct = (
        _pct(_i(exact_coverage.get("candidate_strategy_metadata_covered_count")), candidate_count_for_gate)
        if exact_coverage
        else _pct(sum(1 for item in metadata_complete if item), len(metadata_complete))
    )
    if candidate_strategy_metadata_coverage_pct < settings.engine_readiness_min_candidate_strategy_metadata_coverage_pct:
        hard_blocks.append(_issue("candidate_strategy_metadata_missing", f"Candidate strategy metadata coverage {candidate_strategy_metadata_coverage_pct}% below {settings.engine_readiness_min_candidate_strategy_metadata_coverage_pct}%."))
    allocated_candidate_ids = {str(item.get("candidate_id")) for item in allocations if item.get("status") in {"allocate", "reduce", "require_debate"} and item.get("candidate_id")}
    council_candidate_ids = {str(item.get("candidate_id")) for item in council_reviews if item.get("candidate_id")}
    allocated_candidate_count = _i(exact_counts.get("allocated_candidate_count"), len(allocated_candidate_ids)) if exact_counts else len(allocated_candidate_ids)
    council_review_coverage_pct = (
        100.0 if not allocated_candidate_count else _pct(_i(exact_coverage.get("council_covered_allocated_candidate_count")), allocated_candidate_count)
    ) if exact_coverage else (100.0 if not allocated_candidate_ids else _pct(len(allocated_candidate_ids & council_candidate_ids), len(allocated_candidate_ids)))
    if council_review_coverage_pct < settings.engine_readiness_min_council_review_coverage_pct:
        hard_blocks.append(_issue("council_review_coverage_low", f"Council review coverage {council_review_coverage_pct}% below {settings.engine_readiness_min_council_review_coverage_pct}%."))
    checks["decision_quality"] = {
        "candidate_count": candidate_count_for_gate,
        "ev_estimate_count": len(ev_estimates),
        "ev_coverage_pct": ev_coverage_pct,
        "allocation_count": allocation_count_for_gate,
        "allocated_count": allocated_count,
        "allocation_rate_pct": allocation_rate_pct,
        "candidate_strategy_metadata_coverage_pct": candidate_strategy_metadata_coverage_pct,
        "council_review_coverage_pct": council_review_coverage_pct,
    }

    # Wave 1B evidence spine coverage.
    evidence_candidate_ids = {str(item.get("candidate_id")) for item in candidate_evidence_links if item.get("candidate_id")}
    candidate_evidence_link_coverage_pct = (
        100.0 if not candidate_count_for_gate else _pct(_i(exact_coverage.get("evidence_covered_candidate_count")), candidate_count_for_gate)
    ) if exact_coverage else (100.0 if not candidate_ids else _pct(len(candidate_ids & evidence_candidate_ids), len(candidate_ids)))
    if candidate_evidence_link_coverage_pct < settings.engine_readiness_min_candidate_evidence_link_coverage_pct:
        hard_blocks.append(_issue("candidate_evidence_link_coverage_low", f"Candidate evidence link coverage {candidate_evidence_link_coverage_pct}% below {settings.engine_readiness_min_candidate_evidence_link_coverage_pct}%."))
    council_packet_candidate_ids = {str(item.get("candidate_id")) for item in candidate_evidence_links if item.get("candidate_id") and (item.get("council_review_id") or (_metadata(item).get("council_decision") in {"reject", "allow_shadow", "allow_paper", "needs_more_evidence"}))}
    council_packet_coverage_pct = (
        100.0 if not candidate_count_for_gate else _pct(_i(exact_coverage.get("council_covered_candidate_count")), candidate_count_for_gate)
    ) if exact_coverage else (100.0 if not candidate_ids else _pct(len(candidate_ids & council_packet_candidate_ids), len(candidate_ids)))
    if council_packet_coverage_pct < settings.engine_readiness_min_council_packet_coverage_pct:
        hard_blocks.append(_issue("council_packet_coverage_low", f"Council packet coverage {council_packet_coverage_pct}% below {settings.engine_readiness_min_council_packet_coverage_pct}%."))
    non_flat_candidate_ids = {str(item.get("candidate_id")) for item in candidates if item.get("candidate_id") and item.get("side") != "flat"}
    flat_candidate_ids = {str(item.get("candidate_id")) for item in candidates if item.get("candidate_id") and item.get("side") == "flat"}
    candidate_risk_ids = {str(item.get("candidate_id")) for item in candidate_evidence_links if item.get("candidate_id") and item.get("risk_decision_id")}
    non_flat_candidate_count = _i(exact_counts.get("non_flat_candidate_count"), len(non_flat_candidate_ids)) if exact_counts else len(non_flat_candidate_ids)
    flat_candidate_count = _i(exact_counts.get("flat_candidate_count"), len(flat_candidate_ids)) if exact_counts else len(flat_candidate_ids)
    candidate_risk_gateway_coverage_pct = (
        100.0 if not non_flat_candidate_count else _pct(_i(exact_coverage.get("risk_covered_candidate_count")), non_flat_candidate_count)
    ) if exact_coverage else (100.0 if not non_flat_candidate_ids else _pct(len(non_flat_candidate_ids & candidate_risk_ids), len(non_flat_candidate_ids)))
    if candidate_risk_gateway_coverage_pct < settings.engine_readiness_min_candidate_risk_gateway_coverage_pct:
        hard_blocks.append(_issue("candidate_risk_gateway_coverage_low", f"Candidate-level RiskGateway coverage {candidate_risk_gateway_coverage_pct}% below {settings.engine_readiness_min_candidate_risk_gateway_coverage_pct}%."))
    flat_no_trade_risk_coverage_pct = (
        _pct(_i(exact_coverage.get("flat_risk_covered_candidate_count")), flat_candidate_count)
    ) if exact_coverage else _pct(len(flat_candidate_ids & candidate_risk_ids), len(flat_candidate_ids))
    if flat_candidate_count and flat_no_trade_risk_coverage_pct < 100.0:
        hard_blocks.append(_issue("flat_no_trade_risk_evidence_coverage_low", f"Flat/no-trade RiskGateway evidence coverage {flat_no_trade_risk_coverage_pct}% below 100%."))
    outcome_candidate_ids = {str(item.get("candidate_id")) for item in candidate_outcomes if item.get("candidate_id") and str(item.get("terminal_state") or "") in {"matured", "missing_mark"}}
    matured_candidate_ids = {str(item.get("candidate_id")) for item in candidates if item.get("candidate_id") and generated_at_ms - _ts(item, "created_at_ms") >= 5 * 60 * 1000}
    matured_candidate_count = _i(exact_counts.get("matured_candidate_count"), len(matured_candidate_ids)) if exact_counts else len(matured_candidate_ids)
    matured_outcome_attribution_coverage_pct = (
        100.0 if not matured_candidate_count else _pct(_i(exact_coverage.get("matured_outcome_covered_candidate_count")), matured_candidate_count)
    ) if exact_coverage else (100.0 if not matured_candidate_ids else _pct(len(matured_candidate_ids & outcome_candidate_ids), len(matured_candidate_ids)))
    if matured_outcome_attribution_coverage_pct < settings.engine_readiness_min_matured_outcome_attribution_coverage_pct:
        hard_blocks.append(_issue("matured_outcome_attribution_coverage_low", f"Matured outcome attribution coverage {matured_outcome_attribution_coverage_pct}% below {settings.engine_readiness_min_matured_outcome_attribution_coverage_pct}%."))
    checks["wave1b_evidence_spine"] = {
        "candidate_evidence_link_count": _i(exact_coverage.get("evidence_covered_candidate_count"), len(candidate_evidence_links)) if exact_coverage else len(candidate_evidence_links),
        "candidate_evidence_link_coverage_pct": candidate_evidence_link_coverage_pct,
        "council_packet_coverage_pct": council_packet_coverage_pct,
        "candidate_risk_gateway_coverage_pct": candidate_risk_gateway_coverage_pct,
        "flat_no_trade_risk_coverage_pct": flat_no_trade_risk_coverage_pct,
        "candidate_outcome_attribution_count": len(candidate_outcomes),
        "matured_candidate_count": matured_candidate_count,
        "matured_outcome_attribution_coverage_pct": matured_outcome_attribution_coverage_pct,
    }

    stale_or_invalid_rejects = 0
    for reject in risk_rejects:
        violations = " ".join(str(item) for item in reject.get("violations") or []).lower()
        if any(token in violations for token in ["stale", "invalid", "schema", "missing", "freshness"]):
            stale_or_invalid_rejects += 1
    risk_reject_count_for_gate = _i(exact_counts.get("risk_reject_count"), len(risk_rejects)) if exact_counts else len(risk_rejects)
    risk_denominator = risk_reject_count_for_gate + shadow_intent_count_for_gate
    risk_reject_rate_pct = _pct(risk_reject_count_for_gate, risk_denominator)
    if risk_reject_rate_pct > settings.engine_readiness_max_risk_reject_rate_pct:
        warnings.append(_issue("risk_reject_rate_high", f"Risk reject rate {risk_reject_rate_pct}% exceeds {settings.engine_readiness_max_risk_reject_rate_pct}%.", severity="warning"))
    if risk_reject_count_for_gate >= settings.engine_validation_risk_reject_spike_count and stale_or_invalid_rejects >= max(1, risk_reject_count_for_gate // 2):
        hard_blocks.append(_issue("risk_reject_spike_critical", f"{risk_reject_count_for_gate} rejects in window; stale_or_invalid={stale_or_invalid_rejects}."))
    intent_ids = {str(item.get("intent_id")) for item in intents if item.get("intent_id")}
    risk_intent_ids = {str(item.get("intent_id")) for item in risk_decisions if item.get("intent_id")}
    intent_count_for_gate = _i(exact_counts.get("intent_count"), len(intent_ids)) if exact_counts else len(intent_ids)
    risk_gateway_coverage_pct = (
        100.0 if not intent_count_for_gate else _pct(_i(exact_coverage.get("risk_covered_intent_count")), intent_count_for_gate)
    ) if exact_coverage else (100.0 if not intent_ids else _pct(len(intent_ids & risk_intent_ids), len(intent_ids)))
    if risk_gateway_coverage_pct < settings.engine_readiness_min_risk_gateway_coverage_pct:
        hard_blocks.append(_issue("risk_gateway_coverage_low", f"RiskGateway coverage {risk_gateway_coverage_pct}% below {settings.engine_readiness_min_risk_gateway_coverage_pct}%."))
    checks["risk_gateway"] = {
        "risk_reject_count": risk_reject_count_for_gate,
        "risk_reject_rate_pct": risk_reject_rate_pct,
        "stale_or_invalid_reject_count": stale_or_invalid_rejects,
        "risk_gateway_coverage_pct": risk_gateway_coverage_pct,
    }

    # Execution simulation health.
    report_status_counts = Counter(str(item.get("status") or "unknown") for item in reports)
    if exact_execution:
        report_status_counts = Counter(
            {str(status): _i(count) for status, count in (exact_execution.get("status_counts") or {}).items()}
        )
    failure_count = (
        _i(exact_counts.get("execution_failure_count"))
        if exact_counts
        else sum(count for status, count in report_status_counts.items() if status in {"failed", "rejected", "expired", "cancelled"})
    )
    execution_report_count = _i(exact_counts.get("execution_report_count"), len(reports)) if exact_counts else len(reports)
    fill_failure_rate_pct = _pct(failure_count, execution_report_count)
    measured_reports = [
        item
        for item in reports
        if item.get("status") == "filled" and item.get("avg_fill_px") is not None and _f(item.get("filled_size")) > 0
    ]
    measured_report_count = _i(exact_counts.get("measured_execution_report_count"), len(measured_reports)) if exact_counts else len(measured_reports)
    avg_slippage_bps = (
        round(_f(exact_counts.get("measured_slippage_total_bps")) / measured_report_count, 4)
        if exact_counts and measured_report_count
        else round(sum(_f(item.get("slippage_bps")) for item in measured_reports) / len(measured_reports), 4)
        if measured_reports
        else 0.0
    )
    if avg_slippage_bps > settings.engine_readiness_max_avg_slippage_bps:
        warnings.append(_issue("slippage_high", f"Average simulated slippage {avg_slippage_bps} bps exceeds {settings.engine_readiness_max_avg_slippage_bps} bps.", severity="warning"))
    if fill_failure_rate_pct > settings.engine_readiness_max_fill_failure_rate_pct:
        warnings.append(_issue("fill_failure_rate_high", f"Fill failure rate {fill_failure_rate_pct}% exceeds {settings.engine_readiness_max_fill_failure_rate_pct}%.", severity="warning"))
    checks["execution_simulation"] = {
        "report_count": execution_report_count,
        "status_counts": dict(report_status_counts),
        "measurement_state": "measured" if measured_report_count else "not_measured",
        "measured_report_count": measured_report_count,
        "avg_slippage_bps": avg_slippage_bps if measured_report_count else None,
        "fill_failure_rate_pct": fill_failure_rate_pct,
    }

    # Strategy diversity and dominance.
    candidate_strategy_counts = Counter(str(item.get("strategy_id") or "unknown") for item in candidates)
    active_alpha_strategies: set[str] = set()
    active_alpha_families: set[str] = set()
    paper_eligible_active_strategies: set[str] = set()
    paper_eligible_active_families: set[str] = set()
    shadow_research_strategies: set[str] = set()
    shadow_research_families: set[str] = set()
    candidate_strategy_families: dict[str, str] = {}
    matured_outcome_candidate_ids_by_strategy: dict[str, set[str]] = defaultdict(set)
    for outcome in candidate_outcomes:
        if str(outcome.get("terminal_state") or "") != "matured":
            continue
        strategy_id = str(outcome.get("strategy_id") or "unknown")
        candidate_id = str(outcome.get("candidate_id") or "")
        if candidate_id:
            matured_outcome_candidate_ids_by_strategy[strategy_id].add(candidate_id)
    matured_outcome_counts_by_strategy = {
        key: len(value) for key, value in matured_outcome_candidate_ids_by_strategy.items()
    }
    min_matured_per_strategy = max(
        1,
        int(getattr(settings, "engine_readiness_min_matured_outcomes_per_active_strategy", 20)),
    )
    raw_paper_eligible_strategies: set[str] = set()
    raw_paper_eligible_families: set[str] = set()
    for candidate in candidates:
        metadata = _metadata(candidate)
        family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        counts_for_breadth = bool(metadata.get("counts_for_breadth", candidate.get("counts_for_breadth", True)))
        if counts_for_breadth and family not in {"legacy_bridge", "risk_off_defensive"} and candidate.get("side") != "flat":
            strategy_id = str(candidate.get("strategy_id") or "unknown")
            candidate_strategy_families[strategy_id] = family
            active_alpha_strategies.add(strategy_id)
            active_alpha_families.add(family)
            if _paper_eligible(candidate):
                raw_paper_eligible_strategies.add(strategy_id)
                raw_paper_eligible_families.add(family)
                if len(matured_outcome_candidate_ids_by_strategy[strategy_id]) >= min_matured_per_strategy:
                    paper_eligible_active_strategies.add(strategy_id)
                    paper_eligible_active_families.add(family)
            if _shadow_research(candidate):
                shadow_research_strategies.add(strategy_id)
                shadow_research_families.add(family)
    if exact_breadth:
        active_alpha_strategies = {str(item) for item in exact_breadth.get("active_shadow_strategies") or []}
        active_alpha_families = {str(item) for item in exact_breadth.get("active_shadow_families") or []}
        raw_paper_eligible_strategies = {str(item) for item in exact_breadth.get("raw_paper_eligible_strategies") or []}
        raw_paper_eligible_families = {str(item) for item in exact_breadth.get("raw_paper_eligible_families") or []}
        matured_outcome_counts_by_strategy = {
            str(strategy_id): _i(count)
            for strategy_id, count in (exact_breadth.get("matured_outcome_candidate_count_by_strategy") or {}).items()
        }
        strategy_families = {
            str(strategy_id): str(family)
            for strategy_id, family in (exact_breadth.get("paper_eligible_strategy_families") or {}).items()
        }
        paper_eligible_active_strategies = {
            strategy_id
            for strategy_id in raw_paper_eligible_strategies
            if matured_outcome_counts_by_strategy.get(strategy_id, 0) >= min_matured_per_strategy
        }
        paper_eligible_active_families = {
            strategy_families.get(strategy_id, "unknown")
            for strategy_id in paper_eligible_active_strategies
            if strategy_families.get(strategy_id, "unknown") != "unknown"
        }
    # A strategy can have immutable historical shadow-only candidates and new
    # first-class candidates in the same rolling window after integration. Report
    # strategy-level categories as mutually exclusive so it is not simultaneously
    # presented as paper eligible and research only.
    current_first_class_strategies = WAVE_2_IDS if settings.engine_alpha_catalog_mode == "integrated" else set()
    shadow_research_strategies.difference_update(raw_paper_eligible_strategies | current_first_class_strategies)
    shadow_research_families = {
        candidate_strategy_families.get(strategy_id, "unknown")
        for strategy_id in shadow_research_strategies
        if candidate_strategy_families.get(strategy_id, "unknown") != "unknown"
    }
    if len(paper_eligible_active_strategies) < settings.engine_readiness_min_active_strategy_count_24h:
        hard_blocks.append(_issue("insufficient_active_strategy_count", f"Need >={settings.engine_readiness_min_active_strategy_count_24h} paper-eligible active alpha strategies; observed {len(paper_eligible_active_strategies)}. Shadow-active strategies observed={len(active_alpha_strategies)}."))
    if len(paper_eligible_active_families) < settings.engine_readiness_min_active_strategy_family_count_24h:
        hard_blocks.append(_issue("insufficient_active_strategy_family_count", f"Need >={settings.engine_readiness_min_active_strategy_family_count_24h} paper-eligible active alpha families; observed {len(paper_eligible_active_families)}. Shadow-active families observed={len(active_alpha_families)}."))

    allocation_strategy_counts: Counter[str] = Counter()
    allocation_notional_by_strategy: dict[str, float] = defaultdict(float)
    allocation_notional_by_family: dict[str, float] = defaultdict(float)
    allocation_notional_by_symbol_strategy: dict[str, float] = defaultdict(float)
    total_allocated_notional = 0.0
    for allocation in allocations:
        if allocation.get("status") not in {"allocate", "reduce", "require_debate"}:
            continue
        metadata = _metadata(allocation)
        strategy = str(metadata.get("strategy_id") or allocation.get("strategy_id") or "unknown")
        family = str(metadata.get("strategy_family") or allocation.get("strategy_family") or "unknown")
        asset = str(metadata.get("asset") or allocation.get("asset") or "UNKNOWN").upper()
        notional = _f(allocation.get("allocated_notional_usd"))
        allocation_strategy_counts[strategy] += 1
        allocation_notional_by_strategy[strategy] += notional
        allocation_notional_by_family[family] += notional
        allocation_notional_by_symbol_strategy[f"{asset}:{strategy}"] += notional
        total_allocated_notional += notional
    if exact_concentration:
        allocation_notional_by_strategy = defaultdict(float, {str(key): _f(value) for key, value in (exact_concentration.get("allocation_notional_by_strategy") or {}).items()})
        allocation_notional_by_family = defaultdict(float, {str(key): _f(value) for key, value in (exact_concentration.get("allocation_notional_by_family") or {}).items()})
        allocation_notional_by_symbol_strategy = defaultdict(float, {str(key): _f(value) for key, value in (exact_concentration.get("allocation_notional_by_symbol_strategy") or {}).items()})
        total_allocated_notional = _f(exact_concentration.get("total_allocated_notional_usd"))
    dominant_strategy = max(allocation_notional_by_strategy, key=lambda key: allocation_notional_by_strategy[key]) if allocation_notional_by_strategy else None
    dominant_share_pct = _pct(allocation_notional_by_strategy.get(dominant_strategy or "", 0.0), total_allocated_notional)
    dominant_family = max(allocation_notional_by_family, key=lambda key: allocation_notional_by_family[key]) if allocation_notional_by_family else None
    dominant_family_share_pct = _pct(allocation_notional_by_family.get(dominant_family or "", 0.0), total_allocated_notional)
    dominant_symbol_strategy = max(allocation_notional_by_symbol_strategy, key=lambda key: allocation_notional_by_symbol_strategy[key]) if allocation_notional_by_symbol_strategy else None
    dominant_symbol_strategy_share_pct = _pct(allocation_notional_by_symbol_strategy.get(dominant_symbol_strategy or "", 0.0), total_allocated_notional)
    directional_shadow_intent_count = shadow_intent_count_for_gate
    concentration_min_samples = max(1, int(getattr(settings, "engine_readiness_concentration_min_samples", 50)))
    concentration_gate_active = directional_shadow_intent_count >= concentration_min_samples
    if dominant_share_pct > 45 and concentration_gate_active:
        warnings.append(_issue("strategy_dominance", f"{dominant_strategy} produced {dominant_share_pct}% of allocation notional.", severity="warning"))
    elif dominant_share_pct > 45:
        warnings.append(
            _issue(
                "strategy_concentration_observation",
                f"{dominant_strategy} produced {dominant_share_pct}% of allocation notional, but concentration is report-only until {concentration_min_samples} shadow intents; observed {directional_shadow_intent_count}.",
                severity="warning",
            )
        )
    if concentration_gate_active and dominant_share_pct > settings.engine_readiness_max_strategy_allocation_share_pct:
        hard_blocks.append(_issue("strategy_allocation_dominance", f"{dominant_strategy} allocation share {dominant_share_pct}% exceeds {settings.engine_readiness_max_strategy_allocation_share_pct}%."))
    if concentration_gate_active and dominant_family_share_pct > settings.engine_readiness_max_strategy_family_allocation_share_pct:
        hard_blocks.append(_issue("strategy_family_allocation_dominance", f"{dominant_family} family allocation share {dominant_family_share_pct}% exceeds {settings.engine_readiness_max_strategy_family_allocation_share_pct}%."))
    if concentration_gate_active and dominant_symbol_strategy_share_pct > settings.engine_readiness_max_symbol_strategy_allocation_share_pct:
        hard_blocks.append(_issue("symbol_strategy_allocation_dominance", f"{dominant_symbol_strategy} share {dominant_symbol_strategy_share_pct}% exceeds {settings.engine_readiness_max_symbol_strategy_allocation_share_pct}%."))
    checks["strategy_diversity"] = {
        "candidate_counts": dict(candidate_strategy_counts),
        "active_alpha_strategy_count": len(active_alpha_strategies),
        "active_alpha_family_count": len(active_alpha_families),
        "active_alpha_strategies": sorted(active_alpha_strategies),
        "active_alpha_families": sorted(active_alpha_families),
        "active_shadow_strategy_count": len(active_alpha_strategies),
        "active_shadow_family_count": len(active_alpha_families),
        "paper_eligible_active_strategy_count": len(paper_eligible_active_strategies),
        "paper_eligible_active_family_count": len(paper_eligible_active_families),
        "paper_eligible_active_strategies": sorted(paper_eligible_active_strategies),
        "paper_eligible_active_families": sorted(paper_eligible_active_families),
        "raw_paper_eligible_strategies": sorted(raw_paper_eligible_strategies),
        "raw_paper_eligible_families": sorted(raw_paper_eligible_families),
        "matured_outcome_candidate_count_by_strategy": {
            key: value for key, value in sorted(matured_outcome_counts_by_strategy.items())
        },
        "required_matured_outcomes_per_active_strategy": min_matured_per_strategy,
        "shadow_research_strategy_count": len(shadow_research_strategies),
        "shadow_research_family_count": len(shadow_research_families),
        "shadow_research_strategies": sorted(shadow_research_strategies),
        "shadow_research_families": sorted(shadow_research_families),
        "allocation_counts": dict(allocation_strategy_counts),
        "allocation_notional_usd": {key: round(value, 4) for key, value in allocation_notional_by_strategy.items()},
        "allocation_notional_by_family": {key: round(value, 4) for key, value in allocation_notional_by_family.items()},
        "allocation_notional_by_symbol_strategy": {key: round(value, 4) for key, value in allocation_notional_by_symbol_strategy.items()},
        "dominant_strategy": dominant_strategy,
        "dominant_allocation_share_pct": dominant_share_pct,
        "dominant_family": dominant_family,
        "dominant_family_share_pct": dominant_family_share_pct,
        "dominant_symbol_strategy": dominant_symbol_strategy,
        "dominant_symbol_strategy_share_pct": dominant_symbol_strategy_share_pct,
        "concentration_gate_active": concentration_gate_active,
        "concentration_sample_count": directional_shadow_intent_count,
        "concentration_min_samples": concentration_min_samples,
    }

    performance_groups = [item for item in strict_performance.get("groups") or [] if isinstance(item, dict)]
    performance_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in performance_groups:
        performance_by_strategy[str(item.get("strategy_id") or "unknown")].append(item)
    performance_sample_failures: list[str] = []
    performance_nonpositive: list[dict[str, Any]] = []
    if strict_performance:
        for strategy_id in sorted(paper_eligible_active_strategies):
            groups = performance_by_strategy.get(strategy_id, [])
            qualifying = [item for item in groups if _i(item.get("unique_candidate_count")) >= min_matured_per_strategy]
            if not qualifying:
                performance_sample_failures.append(strategy_id)
                continue
            for item in qualifying:
                if _f(item.get("mean_modeled_net_return_bps")) <= 0 or _f(item.get("mean_realized_r")) <= 0:
                    performance_nonpositive.append(item)
        if performance_sample_failures:
            hard_blocks.append(
                _issue(
                    "strategy_performance_sample_insufficient",
                    f"Paper-eligible strategies lack >={min_matured_per_strategy} strict native-horizon shadow outcomes: {performance_sample_failures}.",
                )
            )
        if performance_nonpositive:
            compact = [
                {
                    "strategy_id": item.get("strategy_id"),
                    "horizon": item.get("candidate_horizon"),
                    "samples": item.get("unique_candidate_count"),
                    "mean_net_bps": item.get("mean_modeled_net_return_bps"),
                    "mean_r": item.get("mean_realized_r"),
                }
                for item in performance_nonpositive
            ]
            hard_blocks.append(
                _issue(
                    "strategy_performance_nonpositive",
                    f"Strict native-horizon after-cost shadow performance is non-positive: {compact}.",
                )
            )
    checks["strict_signal_performance"] = {
        "semantics": strict_performance.get("semantics"),
        "rows_seen": _i(strict_performance.get("rows_seen")),
        "exclusions": strict_performance.get("exclusions") or {},
        "required_samples_per_strategy_horizon": min_matured_per_strategy,
        "groups": performance_groups,
        "sample_failures": performance_sample_failures,
        "nonpositive_groups": performance_nonpositive,
        "horizons_pooled": False,
    }

    # Strategy-regime evidence.
    strategy_regime_rows_ok = [
        row
        for row in strategy_regime_performance
        if _i(row.get("candidate_count"), 0) >= settings.engine_readiness_min_strategy_regime_sample_count and _f(row.get("score")) >= settings.engine_readiness_min_strategy_regime_score
    ]
    evidence_strategy_ids = {str(row.get("strategy_id") or "unknown") for row in strategy_regime_rows_ok}
    shadow_strategy_regime_evidence_coverage_pct = _pct(len(active_alpha_strategies & evidence_strategy_ids), len(active_alpha_strategies))
    strategy_regime_evidence_coverage_pct = _pct(len(paper_eligible_active_strategies & evidence_strategy_ids), len(paper_eligible_active_strategies))
    if strategy_regime_evidence_coverage_pct < settings.engine_readiness_min_strategy_regime_evidence_coverage_pct:
        hard_blocks.append(_issue("strategy_regime_evidence_coverage_low", f"Paper-eligible strategy-regime evidence coverage {strategy_regime_evidence_coverage_pct}% below {settings.engine_readiness_min_strategy_regime_evidence_coverage_pct}%. Shadow strategy-regime evidence coverage={shadow_strategy_regime_evidence_coverage_pct}%."))
    low_score_rows = [
        row
        for row in strategy_regime_performance
        if str(row.get("strategy_id") or "unknown") in paper_eligible_active_strategies
        and _i(row.get("candidate_count"), 0) >= settings.engine_readiness_min_strategy_regime_sample_count
        and _f(row.get("score")) < settings.engine_readiness_min_strategy_regime_score
    ]
    if low_score_rows:
        hard_blocks.append(_issue("strategy_regime_score_low", f"{len(low_score_rows)} strategy-regime rows below minimum score {settings.engine_readiness_min_strategy_regime_score}."))
    checks["strategy_regime_evidence"] = {
        "row_count": len(strategy_regime_performance),
        "qualifying_row_count": len(strategy_regime_rows_ok),
        "coverage_pct": strategy_regime_evidence_coverage_pct,
        "paper_eligible_coverage_pct": strategy_regime_evidence_coverage_pct,
        "shadow_coverage_pct": shadow_strategy_regime_evidence_coverage_pct,
        "required_sample_count": settings.engine_readiness_min_strategy_regime_sample_count,
        "required_score": settings.engine_readiness_min_strategy_regime_score,
    }

    # PnL attribution and EV calibration.
    open_positions = [item for item in positions if item.get("position_state") == "open"]
    stale_open_positions = [item for item in open_positions if _ts(item, "opened_at_ms", "updated_at_ms") and generated_at_ms - _ts(item, "opened_at_ms", "updated_at_ms") > 15 * 60 * 1000]
    latest_pnl_ms = max([_ts(item, "window_end_ms") for item in pnl_records] or [0])
    if stale_open_positions and not pnl_records:
        hard_blocks.append(_issue("position_marking_unhealthy", f"{len(stale_open_positions)} open positions older than 15m and zero PnL attribution records."))
    elif open_positions and (not latest_pnl_ms or generated_at_ms - latest_pnl_ms > max(1, settings.engine_pnl_attribution_interval_seconds) * 2 * 1000):
        warnings.append(_issue("pnl_attribution_stale", "Open positions exist but PnL attribution is stale or missing.", severity="warning"))
    drift_buckets = []
    for bucket, values in (report.get("ev_calibration", {}).get("bucket_summary") or {}).items():
        sample_count = _i(values.get("realized_sample_count"), 0)
        avg_ev = _f(values.get("avg_net_ev_bps"))
        avg_realized = _f(values.get("avg_realized_pnl_usd"))
        if sample_count >= settings.engine_validation_ev_drift_min_samples and avg_ev > 0 and avg_realized <= settings.engine_validation_ev_drift_loss_usd:
            drift_buckets.append({"bucket": bucket, "samples": sample_count, "avg_ev_bps": avg_ev, "avg_realized_pnl_usd": avg_realized})
    if drift_buckets:
        warnings.append(_issue("ev_calibration_drift", f"Positive-EV buckets underperforming: {drift_buckets[:3]}", severity="warning"))
    checks["pnl_calibration"] = {
        "open_position_count": len(open_positions),
        "pnl_attribution_count": len(pnl_records),
        "latest_pnl_attribution_ms": latest_pnl_ms or None,
        "drift_buckets": drift_buckets,
        "bucket_summary": report.get("ev_calibration", {}).get("bucket_summary") or {},
    }

    # Replay comparison gate.
    latest_replay: dict[str, Any] | None = latest_replay_comparison
    replay_required = bool(settings.engine_readiness_require_latest_replay)
    replay_ok = False
    replay_status = None
    replay_window_hours = 0.0
    replay_sample_size = 0
    if latest_replay:
        replay_status = str(latest_replay.get("status") or "unknown")
        metadata_value = latest_replay.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        data_window_value = metadata.get("data_window")
        data_window = data_window_value if isinstance(data_window_value, dict) else {}
        window_start = _i(data_window.get("start_ms"), 0)
        window_end = _i(data_window.get("end_ms"), 0)
        if window_start and window_end and window_end > window_start:
            replay_window_hours = round((window_end - window_start) / 3_600_000, 4)
        candidate_metrics_value = latest_replay.get("candidate_metrics")
        candidate_metrics = candidate_metrics_value if isinstance(candidate_metrics_value, dict) else {}
        replay_sample_size = _i(candidate_metrics.get("candidate_count"), 0)
        if replay_status in {"passed", "advisory_pass"}:
            replay_ok = True
        if replay_status == "insufficient_data":
            hard_blocks.append(
                _issue(
                    "replay_comparison_insufficient_data",
                    f"latest engine replay {latest_replay.get('replay_id')} has not met its sample requirements.",
                )
            )
        elif replay_status not in {"passed", "advisory_pass"}:
            hard_blocks.append(_issue("replay_comparison_failed", f"latest engine replay {latest_replay.get('replay_id')} status={replay_status}."))
        if replay_window_hours and replay_window_hours < settings.engine_readiness_min_replay_window_hours:
            hard_blocks.append(_issue("replay_comparison_stale", f"Replay window {replay_window_hours}h below required {settings.engine_readiness_min_replay_window_hours}h."))
        if replay_sample_size < settings.engine_readiness_min_replay_sample_size:
            hard_blocks.append(_issue("replay_comparison_stale", f"Replay sample size {replay_sample_size} below required {settings.engine_readiness_min_replay_sample_size}."))
    elif replay_required:
        hard_blocks.append(_issue("replay_comparison_missing", "No engine shadow replay comparison artifact exists."))
    else:
        warnings.append(_issue("replay_comparison_missing", "No engine shadow replay comparison artifact exists yet.", severity="warning"))
    checks["shadow_replay"] = {"latest_replay": latest_replay, "required": replay_required, "ok": replay_ok, "status": replay_status, "window_hours": replay_window_hours, "sample_size": replay_sample_size}

    # Numeric score.
    score = 100
    warning_penalty = min(15, len(warnings) * 3)
    score -= warning_penalty
    if ev_coverage_pct < settings.engine_readiness_min_ev_coverage_pct:
        score -= 10
    if feature_coverage_pct < settings.engine_readiness_min_feature_coverage_pct or regime_coverage_pct < settings.engine_readiness_min_regime_coverage_pct:
        score -= 10
    if risk_reject_rate_pct > settings.engine_readiness_max_risk_reject_rate_pct:
        score -= 10
    if allocation_rate_pct < settings.engine_readiness_min_allocation_rate_pct or allocation_rate_pct > settings.engine_readiness_max_allocation_rate_pct:
        score -= 8
    if concentration_gate_active and dominant_share_pct > settings.engine_readiness_max_strategy_allocation_share_pct:
        score -= 10
    if concentration_gate_active and dominant_family_share_pct > settings.engine_readiness_max_strategy_family_allocation_share_pct:
        score -= 10
    if concentration_gate_active and dominant_symbol_strategy_share_pct > settings.engine_readiness_max_symbol_strategy_allocation_share_pct:
        score -= 10
    if council_review_coverage_pct < settings.engine_readiness_min_council_review_coverage_pct:
        score -= 10
    if candidate_evidence_link_coverage_pct < settings.engine_readiness_min_candidate_evidence_link_coverage_pct:
        score -= 10
    if council_packet_coverage_pct < settings.engine_readiness_min_council_packet_coverage_pct:
        score -= 8
    if candidate_risk_gateway_coverage_pct < settings.engine_readiness_min_candidate_risk_gateway_coverage_pct:
        score -= 10
    if flat_candidate_count and flat_no_trade_risk_coverage_pct < 100.0:
        score -= 10
    if matured_outcome_attribution_coverage_pct < settings.engine_readiness_min_matured_outcome_attribution_coverage_pct:
        score -= 10
    if strategy_regime_evidence_coverage_pct < settings.engine_readiness_min_strategy_regime_evidence_coverage_pct:
        score -= 10
    if avg_slippage_bps > settings.engine_readiness_max_avg_slippage_bps:
        score -= 8
    if fill_failure_rate_pct > settings.engine_readiness_max_fill_failure_rate_pct:
        score -= 8
    if any(item.get("code") == "pnl_attribution_stale" for item in warnings) or any(item.get("code") == "position_marking_unhealthy" for item in hard_blocks):
        score -= 10
    if drift_buckets:
        score -= 15
    if performance_sample_failures:
        score -= 10
    if performance_nonpositive:
        score -= 15
    score = max(0, min(100, score))

    ready_for_paper = not hard_blocks and score >= settings.engine_readiness_min_score_to_pass
    if ready_for_paper:
        grade = "pass"
    elif not hard_blocks and score >= 70:
        grade = "watch"
    else:
        grade = "blocked"

    recommendation = _recommendation(hard_blocks, warnings, ready_for_paper)
    next_actions = _next_actions(hard_blocks, warnings, recommendation)
    wave_reports = _wave1d_reports(
        candidates=candidates,
        allocations=allocations,
        council_reviews=council_reviews,
        risk_decisions=risk_decisions,
        candidate_outcomes=candidate_outcomes,
        strategy_regime_performance=strategy_regime_performance,
        portfolio_concentration_events=portfolio_concentration_events,
        latest_replay=latest_replay,
    )
    wave_reports["legacy_engine_signal_comparison"] = report.get("signal_path_comparison") or {}
    metrics = {
        "candidate_count": candidate_count_for_gate,
        "ev_estimate_count": _i(exact_counts.get("ev_estimate_count"), len(ev_estimates)) if exact_counts else _i((report.get("summary") or {}).get("ev_estimate_count"), len(ev_estimates)),
        "allocation_count": allocation_count_for_gate,
        "allocated_count": allocated_count,
        "shadow_intent_count": shadow_intent_count_for_gate,
        "paper_intent_count": paper_intent_count,
        "risk_reject_count": risk_reject_count_for_gate,
        "risk_reject_rate_pct": risk_reject_rate_pct,
        "ev_coverage_pct": ev_coverage_pct,
        "feature_coverage_pct": feature_coverage_pct,
        "regime_coverage_pct": regime_coverage_pct,
        "allocation_rate_pct": allocation_rate_pct,
        "dominant_strategy": dominant_strategy,
        "dominant_allocation_share_pct": dominant_share_pct,
        "dominant_family": dominant_family,
        "dominant_family_share_pct": dominant_family_share_pct,
        "dominant_symbol_strategy": dominant_symbol_strategy,
        "dominant_symbol_strategy_share_pct": dominant_symbol_strategy_share_pct,
        "active_alpha_strategy_count": len(active_alpha_strategies),
        "active_alpha_family_count": len(active_alpha_families),
        "candidate_strategy_metadata_coverage_pct": candidate_strategy_metadata_coverage_pct,
        "candidate_evidence_link_coverage_pct": candidate_evidence_link_coverage_pct,
        "council_packet_coverage_pct": council_packet_coverage_pct,
        "candidate_risk_gateway_coverage_pct": candidate_risk_gateway_coverage_pct,
        "flat_no_trade_risk_coverage_pct": flat_no_trade_risk_coverage_pct,
        "matured_outcome_attribution_coverage_pct": matured_outcome_attribution_coverage_pct,
        "council_review_coverage_pct": council_review_coverage_pct,
        "risk_gateway_coverage_pct": risk_gateway_coverage_pct,
        "strategy_regime_evidence_coverage_pct": strategy_regime_evidence_coverage_pct,
        "execution_measurement_state": "measured" if measured_report_count else "not_measured",
        "avg_slippage_bps": avg_slippage_bps if measured_report_count else None,
        "fill_failure_rate_pct": fill_failure_rate_pct,
        "open_position_count": len(open_positions),
        "candidate_outcome_attribution_count": _i(exact_counts.get("candidate_outcome_attribution_count"), len(candidate_outcomes)) if exact_counts else len(candidate_outcomes),
        "pnl_attribution_count": _i((report.get("summary") or {}).get("pnl_attribution_count"), len(pnl_records)),
        "observed_shadow_hours": observed_hours,
        "run_count": run_count,
    }
    return {
        "generated_at_ms": generated_at_ms,
        "ready_for_paper": ready_for_paper,
        "score": score,
        "grade": grade,
        "window": {
            "hours": hours,
            "start_ms": start_ms,
            "end_ms": generated_at_ms,
            "semantics": "[start_ms,end_ms)",
            "calculation_scope": "full_window" if exact else "sampled_fallback",
        },
        "hard_blocks": hard_blocks,
        "warnings": warnings,
        "checks": checks,
        "metrics": metrics,
        "reports": wave_reports,
        "recommendation": recommendation,
        "next_actions": next_actions,
    }


def _wave1d_reports(
    *,
    candidates: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    council_reviews: list[dict[str, Any]],
    risk_decisions: list[dict[str, Any]],
    candidate_outcomes: list[dict[str, Any]],
    strategy_regime_performance: list[dict[str, Any]],
    portfolio_concentration_events: list[dict[str, Any]],
    latest_replay: dict[str, Any] | None,
) -> dict[str, Any]:
    by_family: dict[str, dict[str, Any]] = defaultdict(lambda: {"candidate_count": 0, "outcome_count": 0, "avg_score": 0.0, "scores": []})
    by_regime: dict[str, dict[str, Any]] = defaultdict(lambda: {"candidate_count": 0, "outcome_count": 0, "avg_net_return_bps": 0.0, "returns": []})
    for candidate in candidates:
        metadata = _metadata(candidate)
        family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        by_family[family]["candidate_count"] += 1
        regime = str(metadata.get("regime_label") or candidate.get("regime_snapshot_id") or "unknown")
        by_regime[regime]["candidate_count"] += 1
    for row in strategy_regime_performance:
        family = str(row.get("strategy_family") or "unknown")
        by_family[family]["scores"].append(_f(row.get("score")))
    for outcome in candidate_outcomes:
        family = str(outcome.get("strategy_family") or "unknown")
        by_family[family]["outcome_count"] += 1
        metadata = _metadata(outcome)
        regime = str(metadata.get("regime_label") or outcome.get("regime_snapshot_id") or "unknown")
        by_regime[regime]["outcome_count"] += 1
        by_regime[regime]["returns"].append(_f(outcome.get("net_return_bps")))
    for family, data in by_family.items():
        scores = data.pop("scores")
        data["avg_score"] = round(sum(scores) / len(scores), 4) if scores else 0.0
    for regime, data in by_regime.items():
        returns = data.pop("returns")
        data["avg_net_return_bps"] = round(sum(returns) / len(returns), 4) if returns else 0.0
    council_counts = Counter(str(item.get("decision") or "unknown") for item in council_reviews)
    risk_counts = Counter(str(item.get("decision") or "unknown") for item in risk_decisions)
    outcome_counts = Counter(str(item.get("terminal_state") or "unknown") for item in candidate_outcomes)
    allocation_notional_by_strategy: dict[str, float] = defaultdict(float)
    for allocation in allocations:
        metadata = _metadata(allocation)
        strategy = str(metadata.get("strategy_id") or allocation.get("strategy_id") or "unknown")
        allocation_notional_by_strategy[strategy] += _f(allocation.get("allocated_notional_usd"))
    total_notional = sum(allocation_notional_by_strategy.values())
    strategy_concentration = {
        strategy: {"notional_usd": round(value, 4), "share_pct": _pct(value, total_notional)}
        for strategy, value in sorted(allocation_notional_by_strategy.items(), key=lambda item: item[1], reverse=True)
    }
    return {
        "readiness_by_strategy_family": dict(by_family),
        "readiness_by_market_regime": dict(by_regime),
        "latest_clean_replay_comparison": latest_replay,
        "strategy_concentration_report": strategy_concentration,
        "council_veto_reject_report": dict(council_counts),
        "risk_gateway_coverage_report": dict(risk_counts),
        "shadow_mode_outcome_report": dict(outcome_counts),
        "portfolio_concentration_event_count": len(portfolio_concentration_events),
    }


def _recommendation(hard_blocks: list[dict[str, str]], warnings: list[dict[str, str]], ready_for_paper: bool) -> str:
    if ready_for_paper:
        return "ready_for_paper"
    codes = {item.get("code") for item in [*hard_blocks, *warnings]}
    if "live_enabled" in codes or "paper_intents_in_shadow_only" in codes or "live_intents_present" in codes:
        return "rollback_to_shadow"
    if "missing_core_data" in codes or "engine_loop_stale" in codes:
        return "fix_data_quality"
    if "insufficient_active_strategy_count" in codes or "insufficient_active_strategy_family_count" in codes:
        return "expand_strategy_opportunity_coverage"
    if "insufficient_sample_size" in codes or "strategy_concentration_observation" in codes:
        return "collect_balanced_evidence"
    if "strategy_dominance" in codes or "strategy_allocation_dominance" in codes or "strategy_family_allocation_dominance" in codes or "symbol_strategy_allocation_dominance" in codes:
        return "balance_shadow_evidence"
    if "risk_reject_spike_critical" in codes or "risk_reject_rate_high" in codes:
        return "review_risk_rejects"
    if "replay_comparison_missing" in codes or "replay_comparison_failed" in codes or "replay_comparison_stale" in codes:
        return "run_replay_comparison"
    return "continue_shadow"


def _next_actions(hard_blocks: list[dict[str, str]], warnings: list[dict[str, str]], recommendation: str) -> list[str]:
    codes = {item.get("code") for item in [*hard_blocks, *warnings]}
    actions: list[str] = []
    if "insufficient_shadow_observation" in codes or "insufficient_sample_size" in codes:
        actions.append("Continue shadow-only observation until minimum time and sample thresholds are met.")
    if "missing_core_data" in codes or "engine_loop_stale" in codes:
        actions.append("Fix feature/regime freshness and verify the engine loop is running without errors.")
    if "paper_intents_in_shadow_only" in codes or "live_enabled" in codes or "live_intents_present" in codes:
        actions.append("Keep ENGINE_PAPER_ENABLED=false, ENGINE_EXECUTION_MODES=shadow, and ENGINE_LIVE_ENABLED=false before further review.")
    if "risk_reject_spike_critical" in codes or "risk_reject_rate_high" in codes:
        actions.append("Review latest risk rejects and stale/invalid data violations before promotion.")
    if "candidate_risk_gateway_coverage_low" in codes or "flat_no_trade_risk_evidence_coverage_low" in codes:
        actions.append("Verify every trade and no-trade candidate has candidate-level RiskGateway evidence before promotion.")
    if "strategy_dominance" in codes or "strategy_allocation_dominance" in codes or "strategy_family_allocation_dominance" in codes or "symbol_strategy_allocation_dominance" in codes:
        actions.append("Use the shadow evidence-admission quotas to balance strategy/family/symbol samples; retain the current paper hard caps.")
    if "strategy_concentration_observation" in codes:
        actions.append("Continue collecting directional shadow intents; concentration remains report-only until the minimum sample is met.")
    if "replay_comparison_insufficient_data" in codes:
        actions.append("Continue shadow collection until the replay candidate, approved-allocation, and shadow-intent samples mature.")
    if "replay_comparison_missing" in codes or "replay_comparison_stale" in codes or "replay_comparison_failed" in codes:
        actions.append("Run an engine shadow replay comparison artifact for the readiness window.")
    if "position_marking_unhealthy" in codes or "pnl_attribution_stale" in codes:
        actions.append("Complete the simulated PnL attribution loop before enabling paper fills.")
    if "insufficient_active_strategy_count" in codes or "insufficient_active_strategy_family_count" in codes or "strategy_regime_evidence_coverage_low" in codes:
        actions.append("Continue shadow collection until diversified strategy-regime evidence is populated.")
    if not actions and recommendation == "ready_for_paper":
        actions.append("Proceed to human review of the paper-mode promotion runbook; do not enable live execution.")
    return actions
