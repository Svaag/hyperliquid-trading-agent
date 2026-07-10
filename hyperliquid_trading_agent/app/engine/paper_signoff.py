from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report
from hyperliquid_trading_agent.app.newswire.observability import build_newswire_soak_readiness


async def build_paper_signoff_preflight(
    repository: Any,
    settings: Settings,
    engine_service: Any | None,
    *,
    symbols: list[str] | None = None,
    window_hours: int | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Aggregate paper-signoff evidence without enabling live execution.

    This preflight is intentionally read-only.  It combines the stricter paper
    readiness scorecard with the engine evidence/validation report and annotates
    whether the requested canary symbols have shadow evidence.
    """

    symbols = [symbol.upper() for symbol in symbols or []]
    readiness = await build_paper_readiness_scorecard(repository, settings, engine_service, window_hours=window_hours, limit=limit)
    validation = await build_engine_validation_report(repository, limit=limit)
    newsfeed_evidence = await build_newswire_soak_readiness(repository, settings, limit=max(5000, limit))
    symbol_evidence = _symbol_evidence(validation, symbols=symbols)
    live_blocks = _live_exchange_blocks(settings, validation)
    evidence_quality = _evidence_quality(readiness, validation)
    ready = bool(readiness.get("ready_for_paper")) and not live_blocks and evidence_quality["passes_minimums"] and all(item["has_shadow_evidence"] for item in symbol_evidence)
    next_actions = list(readiness.get("next_actions", []))
    if settings.engine_newsfeed_enabled and not newsfeed_evidence.get("ready"):
        next_actions.append(
            "Continue the continuous Newswire engine-feed soak until /newswire/readiness passes; evidence remains advisory."
        )
    return {
        "mode": "paper_signoff_preflight",
        "ready_for_paper_signoff": ready,
        "requested_symbols": symbols,
        "live_exchange_blocks": live_blocks,
        "evidence_quality": evidence_quality,
        "symbol_evidence": symbol_evidence,
        "readiness": readiness,
        "validation_summary": validation.get("summary", {}),
        "validation_evidence": {
            "shadow_candidates": validation.get("shadow_candidates", {}),
            "ev_calibration": validation.get("ev_calibration", {}),
            "risk_rejects": validation.get("risk_rejects", {}),
            "execution_simulations": validation.get("execution_simulations", {}),
            "allocation_status_counts": validation.get("allocation_status_counts", {}),
        },
        "newsfeed_evidence": newsfeed_evidence,
        "next_actions": next_actions,
        "paper_only": True,
        "live_execution_allowed": False,
    }


def _live_exchange_blocks(settings: Settings, validation: dict[str, Any]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    if settings.engine_live_enabled:
        blocks.append({"code": "engine_live_enabled", "detail": "ENGINE_LIVE_ENABLED must remain false for paper signoff."})
    execution_modes = [mode.lower() for mode in settings.engine_execution_mode_list]
    if "live" in execution_modes:
        blocks.append({"code": "live_execution_mode_configured", "detail": f"ENGINE_EXECUTION_MODES={execution_modes}"})
    summary = validation.get("summary") or {}
    if int(summary.get("live_intent_count") or 0) > 0:
        blocks.append({"code": "live_intents_present", "detail": f"live_intent_count={summary.get('live_intent_count')}"})
    return blocks


def _evidence_quality(readiness: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    metrics = readiness.get("metrics") or {}
    summary = validation.get("summary") or {}
    coverage = {
        "ev_coverage_pct": float(metrics.get("ev_coverage_pct") or 0.0),
        "candidate_evidence_link_coverage_pct": float(metrics.get("candidate_evidence_link_coverage_pct") or 0.0),
        "candidate_risk_gateway_coverage_pct": float(metrics.get("candidate_risk_gateway_coverage_pct") or 0.0),
        "matured_outcome_attribution_coverage_pct": float(metrics.get("matured_outcome_attribution_coverage_pct") or 0.0),
        "strategy_regime_evidence_coverage_pct": float(metrics.get("strategy_regime_evidence_coverage_pct") or 0.0),
        "candidate_count": int(summary.get("candidate_count") or metrics.get("candidate_count") or 0),
        "shadow_intent_count": int(summary.get("shadow_intent_count") or metrics.get("shadow_intent_count") or 0),
        "execution_report_count": int(summary.get("execution_report_count") or 0),
    }
    minimum_failures = [
        key for key in (
            "ev_coverage_pct",
            "candidate_evidence_link_coverage_pct",
            "candidate_risk_gateway_coverage_pct",
            "matured_outcome_attribution_coverage_pct",
            "strategy_regime_evidence_coverage_pct",
        )
        if coverage[key] < 100.0
    ]
    if coverage["candidate_count"] <= 0:
        minimum_failures.append("candidate_count")
    if coverage["shadow_intent_count"] <= 0:
        minimum_failures.append("shadow_intent_count")
    return {**coverage, "minimum_failures": minimum_failures, "passes_minimums": not minimum_failures}


def _symbol_evidence(validation: dict[str, Any], *, symbols: list[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    asset_counts = (validation.get("shadow_candidates") or {}).get("asset_counts") or {}
    latest = (validation.get("shadow_candidates") or {}).get("latest") or []
    latest_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    for item in latest:
        symbol = str(item.get("asset") or "").upper()
        if symbol in latest_by_symbol:
            latest_by_symbol[symbol].append(item)
    return [
        {
            "symbol": symbol,
            "shadow_candidate_count": int(asset_counts.get(symbol) or 0),
            "has_shadow_evidence": int(asset_counts.get(symbol) or 0) > 0,
            "latest_candidates": latest_by_symbol.get(symbol, [])[:5],
        }
        for symbol in symbols
    ]
