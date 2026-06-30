from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
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
    return item.get("metadata") if isinstance(item.get("metadata"), dict) else {}


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
    service_status = engine_service.status() if engine_service is not None and callable(getattr(engine_service, "status", None)) else {}

    report = await build_engine_validation_report(repository, limit=limit)
    candidates_all = await repository.list_alpha_candidates(limit=limit)
    ev_all = await repository.list_ev_estimates(limit=limit)
    allocations_all = await repository.list_allocation_decisions(limit=limit)
    intents_all = await repository.list_order_intents(limit=limit)
    reports_all = await repository.list_execution_reports(limit=limit)
    risk_all = await repository.list_risk_gateway_decisions(limit=limit)
    risk_rejects_all = [item for item in risk_all if item.get("decision") == "reject"]
    positions_all = await repository.list_position_theses(limit=limit)
    pnl_all = await repository.list_pnl_attribution(limit=limit)
    council_all = await _maybe_list(repository, "list_council_reviews", limit=limit)
    strategy_regime_performance_all = await _maybe_list(repository, "list_strategy_regime_performance", limit=limit)
    latest_replay_comparison = await latest_engine_replay_comparison(repository)

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
    strategy_regime_performance = _in_window(strategy_regime_performance_all, start_ms, "created_at_ms", "window_end_ms")

    hard_blocks: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: dict[str, Any] = {}

    # Shadow/live safety integrity.
    paper_intents = [item for item in intents if item.get("execution_mode") == "paper"]
    paper_reports = [item for item in reports if item.get("execution_mode") == "paper"]
    live_intents = [item for item in intents if item.get("execution_mode") == "live"]
    live_reports = [item for item in reports if item.get("execution_mode") == "live"]
    shadow_only = settings.engine_shadow_enabled and not settings.engine_paper_enabled and settings.engine_execution_mode_list == ["shadow"]
    if settings.engine_live_enabled:
        hard_blocks.append(_issue("live_enabled", "ENGINE_LIVE_ENABLED must remain false."))
    if shadow_only and (paper_intents or paper_reports):
        hard_blocks.append(_issue("paper_intents_in_shadow_only", f"paper_intents={len(paper_intents)} paper_reports={len(paper_reports)}"))
    if live_intents or live_reports:
        hard_blocks.append(_issue("live_intents_present", f"live_intents={len(live_intents)} live_reports={len(live_reports)}"))
    checks["shadow_integrity"] = {
        "shadow_only": shadow_only,
        "paper_intent_count": len(paper_intents),
        "paper_report_count": len(paper_reports),
        "live_intent_count": len(live_intents),
        "live_report_count": len(live_reports),
        "live_enabled": settings.engine_live_enabled,
    }

    # Reliability and observation sample.
    last_run_at = service_status.get("last_run_at_ms")
    stale_after_ms = max(1, settings.engine_validation_alert_stale_loop_seconds) * 1000
    if settings.engine_enabled and (not last_run_at or generated_at_ms - _i(last_run_at) > stale_after_ms):
        hard_blocks.append(_issue("engine_loop_stale", f"last_run_at_ms={last_run_at}; stale_after_ms={stale_after_ms}"))
    if service_status.get("last_error"):
        hard_blocks.append(_issue("runtime_errors_present", f"last_error={service_status.get('last_error')}"))
    run_count = _i(service_status.get("run_count"), 0)
    if run_count < settings.engine_readiness_min_runs:
        warnings.append(_issue("insufficient_engine_runs", f"Need >={settings.engine_readiness_min_runs} runs; observed {run_count}.", severity="warning"))

    shadow_timestamps = [_ts(item, "created_at_ms") for item in intents_all if item.get("execution_mode") == "shadow"] + [_ts(item, "created_at_ms") for item in candidates_all]
    shadow_timestamps = [item for item in shadow_timestamps if item > 0]
    first_shadow_ms = min(shadow_timestamps) if shadow_timestamps else None
    observed_hours = 0.0
    if first_shadow_ms is not None:
        observed_hours = round((generated_at_ms - max(start_ms, first_shadow_ms)) / 3_600_000, 4)
    if observed_hours < hours:
        hard_blocks.append(_issue("insufficient_shadow_observation", f"Need >={hours}h shadow observation; observed {observed_hours:.2f}h."))
    if len(candidates) < settings.engine_readiness_min_candidates or len([item for item in intents if item.get("execution_mode") == "shadow"]) < settings.engine_readiness_min_shadow_intents:
        hard_blocks.append(
            _issue(
                "insufficient_sample_size",
                f"Need >={settings.engine_readiness_min_candidates} candidates and >={settings.engine_readiness_min_shadow_intents} shadow intents; observed candidates={len(candidates)} shadow_intents={len([item for item in intents if item.get('execution_mode') == 'shadow'])}.",
            )
        )
    checks["engine_reliability"] = {
        "last_run_at_ms": last_run_at,
        "last_error": service_status.get("last_error"),
        "run_count": run_count,
        "stale_after_ms": stale_after_ms,
        "observed_shadow_hours": observed_hours,
        "first_shadow_ms": first_shadow_ms,
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
    ev_coverage_pct = _pct(len(candidate_ids & ev_candidate_ids), len(candidate_ids))
    allocated_count = len([item for item in allocations if item.get("status") in {"allocate", "reduce", "require_debate"}])
    allocation_rate_pct = _pct(allocated_count, len(allocations))
    if ev_coverage_pct < settings.engine_readiness_min_ev_coverage_pct:
        warnings.append(_issue("low_ev_coverage", f"EV coverage {ev_coverage_pct}% below {settings.engine_readiness_min_ev_coverage_pct}%.", severity="warning"))
    if allocation_rate_pct < settings.engine_readiness_min_allocation_rate_pct or allocation_rate_pct > settings.engine_readiness_max_allocation_rate_pct:
        warnings.append(_issue("allocation_rate_out_of_bounds", f"Allocation rate {allocation_rate_pct}% outside {settings.engine_readiness_min_allocation_rate_pct}-{settings.engine_readiness_max_allocation_rate_pct}%.", severity="warning"))
    metadata_complete = [_candidate_metadata_complete(item) for item in candidates]
    candidate_strategy_metadata_coverage_pct = _pct(sum(1 for item in metadata_complete if item), len(metadata_complete))
    if candidate_strategy_metadata_coverage_pct < settings.engine_readiness_min_candidate_strategy_metadata_coverage_pct:
        hard_blocks.append(_issue("candidate_strategy_metadata_missing", f"Candidate strategy metadata coverage {candidate_strategy_metadata_coverage_pct}% below {settings.engine_readiness_min_candidate_strategy_metadata_coverage_pct}%."))
    allocated_candidate_ids = {str(item.get("candidate_id")) for item in allocations if item.get("status") in {"allocate", "reduce", "require_debate"} and item.get("candidate_id")}
    council_candidate_ids = {str(item.get("candidate_id")) for item in council_reviews if item.get("candidate_id")}
    council_review_coverage_pct = _pct(len(allocated_candidate_ids & council_candidate_ids), len(allocated_candidate_ids))
    if council_review_coverage_pct < settings.engine_readiness_min_council_review_coverage_pct:
        hard_blocks.append(_issue("council_review_coverage_low", f"Council review coverage {council_review_coverage_pct}% below {settings.engine_readiness_min_council_review_coverage_pct}%."))
    checks["decision_quality"] = {
        "candidate_count": len(candidates),
        "ev_estimate_count": len(ev_estimates),
        "ev_coverage_pct": ev_coverage_pct,
        "allocation_count": len(allocations),
        "allocated_count": allocated_count,
        "allocation_rate_pct": allocation_rate_pct,
        "candidate_strategy_metadata_coverage_pct": candidate_strategy_metadata_coverage_pct,
        "council_review_coverage_pct": council_review_coverage_pct,
    }

    stale_or_invalid_rejects = 0
    for reject in risk_rejects:
        violations = " ".join(str(item) for item in reject.get("violations") or []).lower()
        if any(token in violations for token in ["stale", "invalid", "schema", "missing", "freshness"]):
            stale_or_invalid_rejects += 1
    risk_denominator = len(risk_rejects) + len([item for item in intents if item.get("execution_mode") == "shadow"])
    risk_reject_rate_pct = _pct(len(risk_rejects), risk_denominator)
    if risk_reject_rate_pct > settings.engine_readiness_max_risk_reject_rate_pct:
        warnings.append(_issue("risk_reject_rate_high", f"Risk reject rate {risk_reject_rate_pct}% exceeds {settings.engine_readiness_max_risk_reject_rate_pct}%.", severity="warning"))
    if len(risk_rejects) >= settings.engine_validation_risk_reject_spike_count and stale_or_invalid_rejects >= max(1, len(risk_rejects) // 2):
        hard_blocks.append(_issue("risk_reject_spike_critical", f"{len(risk_rejects)} rejects in window; stale_or_invalid={stale_or_invalid_rejects}."))
    intent_ids = {str(item.get("intent_id")) for item in intents if item.get("intent_id")}
    risk_intent_ids = {str(item.get("intent_id")) for item in risk_decisions if item.get("intent_id")}
    risk_gateway_coverage_pct = _pct(len(intent_ids & risk_intent_ids), len(intent_ids))
    if risk_gateway_coverage_pct < settings.engine_readiness_min_risk_gateway_coverage_pct:
        hard_blocks.append(_issue("risk_gateway_coverage_low", f"RiskGateway coverage {risk_gateway_coverage_pct}% below {settings.engine_readiness_min_risk_gateway_coverage_pct}%."))
    checks["risk_gateway"] = {
        "risk_reject_count": len(risk_rejects),
        "risk_reject_rate_pct": risk_reject_rate_pct,
        "stale_or_invalid_reject_count": stale_or_invalid_rejects,
        "risk_gateway_coverage_pct": risk_gateway_coverage_pct,
    }

    # Execution simulation health.
    report_status_counts = Counter(str(item.get("status") or "unknown") for item in reports)
    failure_count = sum(count for status, count in report_status_counts.items() if status in {"failed", "rejected", "expired", "cancelled"})
    fill_failure_rate_pct = _pct(failure_count, len(reports))
    avg_slippage_bps = round(sum(_f(item.get("slippage_bps")) for item in reports) / len(reports), 4) if reports else 0.0
    if avg_slippage_bps > settings.engine_readiness_max_avg_slippage_bps:
        warnings.append(_issue("slippage_high", f"Average simulated slippage {avg_slippage_bps} bps exceeds {settings.engine_readiness_max_avg_slippage_bps} bps.", severity="warning"))
    if fill_failure_rate_pct > settings.engine_readiness_max_fill_failure_rate_pct:
        warnings.append(_issue("fill_failure_rate_high", f"Fill failure rate {fill_failure_rate_pct}% exceeds {settings.engine_readiness_max_fill_failure_rate_pct}%.", severity="warning"))
    checks["execution_simulation"] = {
        "report_count": len(reports),
        "status_counts": dict(report_status_counts),
        "avg_slippage_bps": avg_slippage_bps,
        "fill_failure_rate_pct": fill_failure_rate_pct,
    }

    # Strategy diversity and dominance.
    candidate_strategy_counts = Counter(str(item.get("strategy_id") or "unknown") for item in candidates)
    active_alpha_strategies: set[str] = set()
    active_alpha_families: set[str] = set()
    for candidate in candidates:
        metadata = _metadata(candidate)
        family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        counts_for_breadth = bool(metadata.get("counts_for_breadth", candidate.get("counts_for_breadth", True)))
        if counts_for_breadth and family not in {"legacy_bridge", "risk_off_defensive"} and candidate.get("side") != "flat":
            active_alpha_strategies.add(str(candidate.get("strategy_id") or "unknown"))
            active_alpha_families.add(family)
    if len(active_alpha_strategies) < settings.engine_readiness_min_active_strategy_count_24h:
        hard_blocks.append(_issue("insufficient_active_strategy_count", f"Need >={settings.engine_readiness_min_active_strategy_count_24h} active alpha strategies; observed {len(active_alpha_strategies)}."))
    if len(active_alpha_families) < settings.engine_readiness_min_active_strategy_family_count_24h:
        hard_blocks.append(_issue("insufficient_active_strategy_family_count", f"Need >={settings.engine_readiness_min_active_strategy_family_count_24h} active alpha families; observed {len(active_alpha_families)}."))

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
    dominant_strategy = max(allocation_notional_by_strategy, key=allocation_notional_by_strategy.get) if allocation_notional_by_strategy else None
    dominant_share_pct = _pct(allocation_notional_by_strategy.get(dominant_strategy or "", 0.0), total_allocated_notional)
    dominant_family = max(allocation_notional_by_family, key=allocation_notional_by_family.get) if allocation_notional_by_family else None
    dominant_family_share_pct = _pct(allocation_notional_by_family.get(dominant_family or "", 0.0), total_allocated_notional)
    dominant_symbol_strategy = max(allocation_notional_by_symbol_strategy, key=allocation_notional_by_symbol_strategy.get) if allocation_notional_by_symbol_strategy else None
    dominant_symbol_strategy_share_pct = _pct(allocation_notional_by_symbol_strategy.get(dominant_symbol_strategy or "", 0.0), total_allocated_notional)
    if dominant_share_pct > 45:
        warnings.append(_issue("strategy_dominance", f"{dominant_strategy} produced {dominant_share_pct}% of allocation notional.", severity="warning"))
    if dominant_share_pct > settings.engine_readiness_max_strategy_allocation_share_pct:
        hard_blocks.append(_issue("strategy_allocation_dominance", f"{dominant_strategy} allocation share {dominant_share_pct}% exceeds {settings.engine_readiness_max_strategy_allocation_share_pct}%."))
    if dominant_family_share_pct > settings.engine_readiness_max_strategy_family_allocation_share_pct:
        hard_blocks.append(_issue("strategy_family_allocation_dominance", f"{dominant_family} family allocation share {dominant_family_share_pct}% exceeds {settings.engine_readiness_max_strategy_family_allocation_share_pct}%."))
    if dominant_symbol_strategy_share_pct > settings.engine_readiness_max_symbol_strategy_allocation_share_pct:
        hard_blocks.append(_issue("symbol_strategy_allocation_dominance", f"{dominant_symbol_strategy} share {dominant_symbol_strategy_share_pct}% exceeds {settings.engine_readiness_max_symbol_strategy_allocation_share_pct}%."))
    checks["strategy_diversity"] = {
        "candidate_counts": dict(candidate_strategy_counts),
        "active_alpha_strategy_count": len(active_alpha_strategies),
        "active_alpha_family_count": len(active_alpha_families),
        "active_alpha_strategies": sorted(active_alpha_strategies),
        "active_alpha_families": sorted(active_alpha_families),
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
    }

    # Strategy-regime evidence.
    strategy_regime_rows_ok = [
        row
        for row in strategy_regime_performance
        if _i(row.get("candidate_count"), 0) >= settings.engine_readiness_min_strategy_regime_sample_count and _f(row.get("score")) >= settings.engine_readiness_min_strategy_regime_score
    ]
    evidence_strategy_ids = {str(row.get("strategy_id") or "unknown") for row in strategy_regime_rows_ok}
    strategy_regime_evidence_coverage_pct = _pct(len(active_alpha_strategies & evidence_strategy_ids), len(active_alpha_strategies))
    if strategy_regime_evidence_coverage_pct < settings.engine_readiness_min_strategy_regime_evidence_coverage_pct:
        hard_blocks.append(_issue("strategy_regime_evidence_coverage_low", f"Strategy-regime evidence coverage {strategy_regime_evidence_coverage_pct}% below {settings.engine_readiness_min_strategy_regime_evidence_coverage_pct}%."))
    low_score_rows = [row for row in strategy_regime_performance if _i(row.get("candidate_count"), 0) >= settings.engine_readiness_min_strategy_regime_sample_count and _f(row.get("score")) < settings.engine_readiness_min_strategy_regime_score]
    if low_score_rows:
        hard_blocks.append(_issue("strategy_regime_score_low", f"{len(low_score_rows)} strategy-regime rows below minimum score {settings.engine_readiness_min_strategy_regime_score}."))
    checks["strategy_regime_evidence"] = {
        "row_count": len(strategy_regime_performance),
        "qualifying_row_count": len(strategy_regime_rows_ok),
        "coverage_pct": strategy_regime_evidence_coverage_pct,
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
    latest_replay = latest_replay_comparison
    replay_required = bool(settings.engine_readiness_require_latest_replay)
    replay_ok = False
    replay_status = None
    replay_window_hours = 0.0
    replay_sample_size = 0
    if latest_replay:
        replay_status = str(latest_replay.get("status") or "unknown")
        metadata = latest_replay.get("metadata") if isinstance(latest_replay.get("metadata"), dict) else {}
        data_window = metadata.get("data_window") if isinstance(metadata.get("data_window"), dict) else {}
        window_start = _i(data_window.get("start_ms"), 0)
        window_end = _i(data_window.get("end_ms"), 0)
        if window_start and window_end and window_end > window_start:
            replay_window_hours = round((window_end - window_start) / 3_600_000, 4)
        candidate_metrics = latest_replay.get("candidate_metrics") if isinstance(latest_replay.get("candidate_metrics"), dict) else {}
        replay_sample_size = _i(candidate_metrics.get("candidate_count"), 0)
        if replay_status in {"passed", "advisory_pass"}:
            replay_ok = True
        if replay_status not in {"passed", "advisory_pass"}:
            hard_blocks.append(_issue("replay_comparison_failed", f"latest engine replay {latest_replay.get('replay_id')} status={replay_status}."))
        if replay_window_hours and replay_window_hours < settings.engine_readiness_min_replay_window_hours:
            hard_blocks.append(_issue("replay_comparison_stale", f"Replay window {replay_window_hours}h below required {settings.engine_readiness_min_replay_window_hours}h."))
        if replay_sample_size and replay_sample_size < settings.engine_readiness_min_replay_sample_size:
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
    if dominant_share_pct > settings.engine_readiness_max_strategy_allocation_share_pct:
        score -= 10
    if dominant_family_share_pct > settings.engine_readiness_max_strategy_family_allocation_share_pct:
        score -= 10
    if dominant_symbol_strategy_share_pct > settings.engine_readiness_max_symbol_strategy_allocation_share_pct:
        score -= 10
    if council_review_coverage_pct < settings.engine_readiness_min_council_review_coverage_pct:
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
    metrics = {
        "candidate_count": len(candidates),
        "ev_estimate_count": len(ev_estimates),
        "allocation_count": len(allocations),
        "allocated_count": allocated_count,
        "shadow_intent_count": len([item for item in intents if item.get("execution_mode") == "shadow"]),
        "paper_intent_count": len(paper_intents),
        "risk_reject_count": len(risk_rejects),
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
        "council_review_coverage_pct": council_review_coverage_pct,
        "risk_gateway_coverage_pct": risk_gateway_coverage_pct,
        "strategy_regime_evidence_coverage_pct": strategy_regime_evidence_coverage_pct,
        "avg_slippage_bps": avg_slippage_bps,
        "fill_failure_rate_pct": fill_failure_rate_pct,
        "open_position_count": len(open_positions),
        "pnl_attribution_count": len(pnl_records),
        "observed_shadow_hours": observed_hours,
        "run_count": run_count,
    }
    return {
        "generated_at_ms": generated_at_ms,
        "ready_for_paper": ready_for_paper,
        "score": score,
        "grade": grade,
        "window": {"hours": hours, "start_ms": start_ms, "end_ms": generated_at_ms},
        "hard_blocks": hard_blocks,
        "warnings": warnings,
        "checks": checks,
        "metrics": metrics,
        "recommendation": recommendation,
        "next_actions": next_actions,
    }


def _recommendation(hard_blocks: list[dict[str, str]], warnings: list[dict[str, str]], ready_for_paper: bool) -> str:
    if ready_for_paper:
        return "ready_for_paper"
    codes = {item.get("code") for item in [*hard_blocks, *warnings]}
    if "live_enabled" in codes or "paper_intents_in_shadow_only" in codes or "live_intents_present" in codes:
        return "rollback_to_shadow"
    if "missing_core_data" in codes or "engine_loop_stale" in codes:
        return "fix_data_quality"
    if "strategy_dominance" in codes or "strategy_allocation_dominance" in codes or "strategy_family_allocation_dominance" in codes or "symbol_strategy_allocation_dominance" in codes:
        return "tighten_strategy_throttles"
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
    if "strategy_dominance" in codes or "strategy_allocation_dominance" in codes or "strategy_family_allocation_dominance" in codes or "symbol_strategy_allocation_dominance" in codes:
        actions.append("Tighten or enable strategy/family/symbol diversity controls and continue shadow observation.")
    if "replay_comparison_missing" in codes or "replay_comparison_stale" in codes or "replay_comparison_failed" in codes:
        actions.append("Run an engine shadow replay comparison artifact for the readiness window.")
    if "position_marking_unhealthy" in codes or "pnl_attribution_stale" in codes:
        actions.append("Complete the simulated PnL attribution loop before enabling paper fills.")
    if "insufficient_active_strategy_count" in codes or "insufficient_active_strategy_family_count" in codes or "strategy_regime_evidence_coverage_low" in codes:
        actions.append("Continue shadow collection until diversified strategy-regime evidence is populated.")
    if not actions and recommendation == "ready_for_paper":
        actions.append("Proceed to human review of the paper-mode promotion runbook; do not enable live execution.")
    return actions
