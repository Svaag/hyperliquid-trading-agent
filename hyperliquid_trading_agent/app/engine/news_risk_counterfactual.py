from __future__ import annotations

import hashlib
import math
import random
import time
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent.app.engine.signal_quality import OUTCOME_WINDOW_MS, load_signal_quality_rows

ARTIFACT_TYPE = "engine_news_risk_counterfactual"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    denominator = sum(weights)
    return sum(value * weight for value, weight in zip(values, weights, strict=True)) / denominator if denominator else 0.0


def _weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    pairs = sorted((value, weight) for value, weight in zip(values, weights, strict=True) if weight > 0)
    total = sum(weight for _, weight in pairs)
    if not pairs or total <= 0:
        return 0.0
    target = max(0.0, min(1.0, q)) * total
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= target:
            return value
    return pairs[-1][0]


def _overlay_weight(row: dict[str, Any]) -> tuple[float, str]:
    allocation = _dict(row.get("allocation"))
    metadata = _dict(allocation.get("metadata"))
    overlay = _dict(metadata.get("news_risk_overlay"))
    if overlay:
        if bool(overlay.get("would_block")):
            return 0.0, "persisted_would_block"
        if overlay.get("would_size_multiplier") is not None:
            return max(0.0, min(1.0, _f(overlay.get("would_size_multiplier")))), "persisted_size_multiplier"
    mode = str(row.get("observed_news_risk_mode") or "neutral")
    side = str(row.get("side") or "flat")
    if mode == "shock":
        return 0.0, "derived_fallback"
    if mode == "risk_off" and side == "long":
        return 0.5, "derived_fallback"
    return 1.0, "derived_fallback"


def _max_drawdown(returns: list[float], weights: list[float], *, equal_risk: bool) -> float:
    exposure = sum(weights)
    scale = len(weights) / exposure if equal_risk and exposure > 0 else 1.0
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value, weight in zip(returns, weights, strict=True):
        equity += value * weight * scale
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _scenario_metrics(rows: list[dict[str, Any]], weights: list[float]) -> dict[str, Any]:
    returns = [_f(row.get("net_return_bps")) for row in rows]
    realized_r = [_f(row.get("realized_r")) for row in rows]
    mae = [_f(row.get("mae_bps")) for row in rows]
    p05 = _weighted_quantile(returns, weights, 0.05)
    tail_pairs = [(value, weight) for value, weight in zip(returns, weights, strict=True) if value <= p05]
    tail_values = [value for value, _ in tail_pairs]
    tail_weights = [weight for _, weight in tail_pairs]
    chronological = sorted(zip(rows, weights, strict=True), key=lambda item: int(item[0].get("window_end_ms") or 0))
    chrono_returns = [_f(row.get("net_return_bps")) for row, _ in chronological]
    chrono_weights = [weight for _, weight in chronological]
    return {
        "sample_count": len(rows),
        "exposure_units": round(sum(weights), 4),
        "blocked_count": sum(weight == 0 for weight in weights),
        "halved_count": sum(weight == 0.5 for weight in weights),
        "weighted_hit_rate_pct": round(_weighted_mean([100.0 if value > 0 else 0.0 for value in returns], weights), 4),
        "mean_modeled_net_return_bps": round(_weighted_mean(returns, weights), 4),
        "mean_realized_r": round(_weighted_mean(realized_r, weights), 4),
        "p05_return_bps": round(p05, 4),
        "expected_shortfall_05_bps": round(_weighted_mean(tail_values, tail_weights), 4),
        "mean_mae_bps": round(_weighted_mean(mae, weights), 4),
        "equal_risk_max_drawdown_bps": round(_max_drawdown(chrono_returns, chrono_weights, equal_risk=True), 4),
    }


def _paired_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_weights = [1.0 for _ in rows]
    overlay = [_overlay_weight(row) for row in rows]
    active_weights = [item[0] for item in overlay]
    baseline = _scenario_metrics(rows, baseline_weights)
    active = _scenario_metrics(rows, active_weights)
    diffs = {
        key: round(_f(active.get(key)) - _f(baseline.get(key)), 4)
        for key in (
            "weighted_hit_rate_pct",
            "mean_modeled_net_return_bps",
            "mean_realized_r",
            "p05_return_bps",
            "expected_shortfall_05_bps",
            "mean_mae_bps",
            "equal_risk_max_drawdown_bps",
            "exposure_units",
        )
    }
    return {
        "baseline": baseline,
        "overlay_active": active,
        "diffs": diffs,
        "weight_provenance_counts": {
            provenance: sum(item[1] == provenance for item in overlay)
            for provenance in sorted({item[1] for item in overlay})
        },
    }


def _time_blocks(rows: list[dict[str, Any]], duration_ms: int) -> list[list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row.get("window_end_ms") or 0) // max(1, duration_ms)].append(row)
    return [grouped[key] for key in sorted(grouped)]


def _bootstrap_diffs(rows: list[dict[str, Any]], *, horizon: str, samples: int = 500) -> dict[str, list[float]] | None:
    blocks = _time_blocks(rows, OUTCOME_WINDOW_MS.get(horizon, 60_000))
    unique_candidates = {str(row.get("candidate_id") or "") for row in rows}
    if len(unique_candidates) < 50 or len(blocks) < 8:
        return None
    seed = int(hashlib.sha256(f"{horizon}:{len(rows)}".encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    keys = (
        "weighted_hit_rate_pct",
        "mean_modeled_net_return_bps",
        "expected_shortfall_05_bps",
        "equal_risk_max_drawdown_bps",
    )
    distributions: dict[str, list[float]] = {key: [] for key in keys}
    for _ in range(samples):
        sampled = [row for _ in blocks for row in blocks[rng.randrange(len(blocks))]]
        result = _paired_metrics(sampled)["diffs"]
        for key in keys:
            distributions[key].append(_f(result.get(key)))
    return {
        key: [round(_quantile(values, 0.025), 4), round(_quantile(values, 0.975), 4)]
        for key, values in distributions.items()
    }


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _safety_decision(horizon_results: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in horizon_results if row.get("bootstrap_diff_ci95") is not None]
    checks = []
    for row in eligible:
        diffs = row["comparison"]["diffs"]
        ci = row["bootstrap_diff_ci95"]
        checks.append(
            {
                "outcome_window": row["outcome_window"],
                "tail_point_improved": _f(diffs.get("expected_shortfall_05_bps")) > 0,
                "drawdown_point_improved": _f(diffs.get("equal_risk_max_drawdown_bps")) < 0,
                "tail_no_harm_ci": _f(ci["expected_shortfall_05_bps"][0]) >= 0,
                "drawdown_no_harm_ci": _f(ci["equal_risk_max_drawdown_bps"][1]) <= 0,
                "mean_guardrail_ci": _f(ci["mean_modeled_net_return_bps"][0]) >= -1.0,
                "hit_rate_guardrail_ci": _f(ci["weighted_hit_rate_pct"][0]) >= -2.0,
            }
        )
    promotable = bool(checks) and all(all(value for key, value in check.items() if key != "outcome_window") for check in checks)
    return {
        "posture": "safety_first",
        "promotable": promotable,
        "eligible_horizon_count": len(checks),
        "checks": checks,
        "guardrails": {"mean_degradation_bps": 1.0, "hit_rate_degradation_percentage_points": 2.0, "confidence_level": 0.95},
        "recommendation": "eligible_for_operator_review" if promotable else "keep_overlay_report_only",
    }


async def run_news_risk_counterfactual(
    repository: Any,
    *,
    window_hours: int = 24,
    as_of_ms: int | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    context, all_rows = await load_signal_quality_rows(repository, window_hours=window_hours, as_of_ms=as_of_ms)
    news_native = [row for row in all_rows if str(row.get("strategy_family") or "") == "event_driven_news"]
    rows = [row for row in all_rows if row not in news_native]
    comparison = _paired_metrics(rows)
    state_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    horizon_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        horizon = str(row.get("outcome_window") or "unknown")
        state_groups[(str(row.get("observed_news_risk_mode") or "neutral"), str(row.get("side") or "flat"), horizon)].append(row)
        horizon_groups[horizon].append(row)
    state_predictiveness = [
        {
            "news_risk_mode": key[0],
            "side": key[1],
            "outcome_window": key[2],
            **_paired_metrics(values),
        }
        for key, values in state_groups.items()
    ]
    horizon_results = []
    for horizon, values in sorted(horizon_groups.items(), key=lambda item: OUTCOME_WINDOW_MS.get(item[0], 0)):
        horizon_results.append(
            {
                "outcome_window": horizon,
                "sample_count": len(values),
                "non_overlapping_block_count": len(_time_blocks(values, OUTCOME_WINDOW_MS.get(horizon, 60_000))),
                "comparison": _paired_metrics(values),
                "bootstrap_diff_ci95": _bootstrap_diffs(values, horizon=horizon),
            }
        )
    safety = _safety_decision(horizon_results)
    ts = _now_ms()
    replay_id = "nrcf_" + hashlib.sha256(f"{context['dataset_id']}:{ts}".encode()).hexdigest()[:24]
    artifact = {
        "replay_id": replay_id,
        "proposal_id": f"research:news-risk-overlay:{context['dataset_id']}",
        "decision_id": None,
        "status": "audit_only",
        "baseline_metrics": comparison["baseline"],
        "candidate_metrics": comparison["overlay_active"],
        "diffs": comparison["diffs"],
        "caveats": [
            "candidate_fixed_as_observed_counterfactual",
            "modeled_returns_not_execution_pnl",
            "news_native_strategies_reported_separately",
            "full_strategy_regeneration_not_supported_v1",
        ],
        "created_at_ms": ts,
        "metadata": {
            "schema_version": 1,
            "artifact_type": ARTIFACT_TYPE,
            "research_only": True,
            "readiness_eligible": False,
            "dataset_id": context["dataset_id"],
            "data_window": context["window"],
            "data_quality": context["data_quality"],
            "sample_count": len(rows),
            "news_native_sample_count": len(news_native),
            "state_predictiveness": sorted(state_predictiveness, key=lambda item: (item["news_risk_mode"], item["side"], item["outcome_window"])),
            "by_outcome_window": horizon_results,
            "safety_decision": safety,
            "weight_provenance_counts": comparison["weight_provenance_counts"],
            "side_effects": {
                "normalized_events": 0,
                "feature_values": 0,
                "consumer_offsets": 0,
                "order_intents": 0,
                "execution_reports": 0,
                "positions": 0,
                "exchange_actions": [],
            },
        },
    }
    if persist and getattr(repository, "enabled", False):
        record = getattr(repository, "record_replay_result", None)
        if callable(record):
            await record(artifact)
    return artifact


async def list_news_risk_counterfactuals(repository: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    method = getattr(repository, "list_replay_results", None)
    if not callable(method):
        return []
    rows = list(await method(limit=max(1, min(1000, limit * 5))))
    return [
        row
        for row in rows
        if (row.get("metadata") or {}).get("artifact_type") == ARTIFACT_TYPE
    ][:limit]


async def latest_news_risk_counterfactual(repository: Any) -> dict[str, Any] | None:
    rows = await list_news_risk_counterfactuals(repository, limit=1)
    return rows[0] if rows else None
