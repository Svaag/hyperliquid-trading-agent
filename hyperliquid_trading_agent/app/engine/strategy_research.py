from __future__ import annotations

import hashlib
import itertools
import json
import random
import statistics
import time
from typing import Any

from hyperliquid_trading_agent.app.engine.signal_quality import load_signal_quality_rows
from hyperliquid_trading_agent.app.engine.time_block_stats import non_overlapping_block_statistics

RESEARCH_GRIDS: dict[str, dict[str, Any]] = {
    "microstructure_ofi_v2": {
        "candidate_strategy_id": "microstructure_ofi_v3",
        "candidate_version": "3.0.0",
        "focus": "positive_gross_slices",
        "grid": {
            "min_abs_top_imbalance": [0.3, 0.45, 0.6],
            "min_depth_usd": [50_000, 100_000],
            "max_spread_bps": [4.0, 8.0],
        },
    },
    "liquidity_vacuum_breakout_v1": {
        "candidate_strategy_id": "liquidity_vacuum_breakout_v2",
        "candidate_version": "2.0.0",
        "focus": "positive_gross_slices",
        "grid": {
            "min_depth_thinning_pct": [30.0, 45.0, 60.0],
            "min_abs_return_bps": [14.0, 22.0],
            "max_spread_bps": [5.0, 10.0],
        },
    },
    "microstructure_absorption_v1": {
        "candidate_strategy_id": "microstructure_absorption_v2",
        "candidate_version": "2.0.0",
        "focus": "redesign_before_retest",
        "grid": {
            "min_abs_top_imbalance": [0.45, 0.6],
            "min_depth_replenishment_rate": [0.1, 0.25],
            "min_visible_depth_usd": [50_000, 100_000],
            "max_abs_mid_return_5m_bps": [2.0, 5.0],
        },
        "required_new_features": [
            "bid_depth_usd",
            "ask_depth_usd",
            "depth_replenishment_rate",
            "top_imbalance",
            "mid_return_5m_bps",
        ],
        "future_feature_backlog": ["aggressive_trade_to_visible_depth_ratio"],
    },
}


async def build_strategy_research_report(
    repository: Any,
    *,
    window_hours: int = 24 * 30,
    as_of_ms: int | None = None,
    bootstrap_iterations: int = 10_000,
    min_effective_blocks: int = 30,
) -> dict[str, Any]:
    context, rows = await load_signal_quality_rows(
        repository,
        window_hours=window_hours,
        as_of_ms=as_of_ms,
        max_rows=100_000,
    )
    results: list[dict[str, Any]] = []
    all_slices: list[dict[str, Any]] = []
    for strategy_id, design in RESEARCH_GRIDS.items():
        strategy_rows = [
            row
            for row in rows
            if str(row.get("strategy_id") or "") == strategy_id
            and str(row.get("candidate_horizon") or "") == str(row.get("outcome_window") or "")
        ]
        horizon_groups: dict[str, list[dict[str, Any]]] = {}
        for row in strategy_rows:
            horizon_groups.setdefault(str(row.get("outcome_window") or "unknown"), []).append(row)
        if not horizon_groups:
            horizon_groups = {"unknown": []}
        for horizon, values in sorted(horizon_groups.items()):
            observed_versions = sorted({str(row.get("strategy_version") or "unknown") for row in values})
            overall_stats = _statistics(
                values,
                horizon=horizon,
                bootstrap_iterations=bootstrap_iterations,
                min_effective_blocks=min_effective_blocks,
                seed_key=f"strategy_research:{strategy_id}:{horizon}:overall",
            )
            slices: list[dict[str, Any]] = []
            for parameters in _parameter_grid(design["grid"]):
                slice_rows = [row for row in values if _matches_slice(strategy_id, row, parameters)]
                slice_stats = _statistics(
                    slice_rows,
                    horizon=horizon,
                    bootstrap_iterations=bootstrap_iterations,
                    min_effective_blocks=min_effective_blocks,
                    seed_key=(
                        f"strategy_research:{strategy_id}:{horizon}:"
                        f"{json.dumps(parameters, sort_keys=True, separators=(',', ':'))}"
                    ),
                )
                block_values = slice_stats.pop("_block_values")
                item = {
                    "slice_id": "slice_"
                    + hashlib.sha256(
                        f"{strategy_id}:{horizon}:{json.dumps(parameters, sort_keys=True)}".encode()
                    ).hexdigest()[:20],
                    "parameters": parameters,
                    "raw_matching_outcome_count": len(slice_rows),
                    "statistics": slice_stats,
                    "one_sided_p_value": _one_sided_sign_flip_p_value(
                        block_values,
                        iterations=bootstrap_iterations,
                        seed_key=f"research_p:{strategy_id}:{horizon}:{parameters}",
                    ),
                    "walk_forward": _walk_forward_positive(block_values),
                }
                slices.append(item)
                all_slices.append(item)
            results.append(
                {
                    "strategy_id": strategy_id,
                    "current_versions": observed_versions,
                    "candidate_strategy_id": design["candidate_strategy_id"],
                    "candidate_version": design["candidate_version"],
                    "horizon": horizon,
                    "research_focus": design["focus"],
                    "fixed_parameter_grid": design["grid"],
                    "required_new_features": design.get("required_new_features", []),
                    "future_feature_backlog": design.get("future_feature_backlog", []),
                    "overall_statistics": {key: value for key, value in overall_stats.items() if key != "_block_values"},
                    "slices": slices,
                }
            )

    _apply_benjamini_hochberg(all_slices)
    eligible_by_version: dict[str, dict[str, Any]] = {}
    for result in results:
        redesign_complete = result["research_focus"] != "redesign_before_retest" or _has_redesigned_feature_evidence(
            rows, result["strategy_id"]
        )
        eligible_slices: list[dict[str, Any]] = []
        for item in result["slices"]:
            reasons = _slice_gate_reasons(
                item,
                min_effective_blocks=min_effective_blocks,
                redesign_complete=redesign_complete,
            )
            item["gate_reason_codes"] = reasons
            item["eligible_for_new_version"] = not reasons
            if not reasons:
                eligible_slices.append(item)
        eligible_slices.sort(
            key=lambda item: (
                float(item["statistics"].get("ci_95_lower") or float("-inf")),
                int(item["statistics"].get("effective_block_count") or 0),
            ),
            reverse=True,
        )
        selected = eligible_slices[0] if eligible_slices else None
        result["redesign_feature_evidence_complete"] = redesign_complete
        result["eligible_slice_count"] = len(eligible_slices)
        result["eligible_for_new_version"] = selected is not None
        result["selected_slice"] = selected
        result["gate_reason_codes"] = (
            []
            if selected is not None
            else sorted({reason for item in result["slices"] for reason in item["gate_reason_codes"]})
            or ["no_strict_native_horizon_rows"]
        )
        if selected is None:
            continue
        version_key = f"{result['candidate_strategy_id']}@{result['candidate_version']}"
        proposal = {
            "strategy_version_key": version_key,
            "strategy_id": result["candidate_strategy_id"],
            "strategy_version": result["candidate_version"],
            "predecessor_strategy_id": result["strategy_id"],
            "predecessor_versions": result["current_versions"],
            "horizon": result["horizon"],
            "selected_parameters": selected["parameters"],
            "evidence": selected["statistics"],
            "state": "research_only",
            "auto_register": False,
            "reason_codes": ["research_slice_gate_passed", "manual_code_review_required"],
        }
        current = eligible_by_version.get(version_key)
        if current is None or float(proposal["evidence"].get("ci_95_lower") or float("-inf")) > float(
            current["evidence"].get("ci_95_lower") or float("-inf")
        ):
            eligible_by_version[version_key] = proposal

    generated_at_ms = int(time.time() * 1000)
    return {
        "report_id": "strategy_research_"
        + hashlib.sha256(f"{context.get('dataset_id')}:{generated_at_ms // 3_600_000}".encode()).hexdigest()[:24],
        "generated_at_ms": generated_at_ms,
        "dataset": context,
        "multiple_testing_control": "Benjamini-Hochberg FDR 5% across every predeclared strategy/horizon/parameter slice",
        "promotion_gate": {
            "minimum_effective_blocks": min_effective_blocks,
            "gross_return_ci_95_lower_must_exceed_bps": 0.0,
            "walk_forward_test_blocks_must_be_positive": True,
            "new_versions_default_state": "research_only",
            "automatic_runtime_registration": False,
        },
        "results": results,
        "eligible_version_specs": sorted(eligible_by_version.values(), key=lambda item: item["strategy_version_key"]),
    }


def _statistics(
    rows: list[dict[str, Any]],
    *,
    horizon: str,
    bootstrap_iterations: int,
    min_effective_blocks: int,
    seed_key: str,
) -> dict[str, Any]:
    stats = non_overlapping_block_statistics(
        rows,
        value_field="gross_return_bps",
        horizon=None if horizon == "unknown" else horizon,
        bootstrap_iterations=bootstrap_iterations,
        min_promotion_blocks=min_effective_blocks,
        seed_key=seed_key,
    )
    block_values = [float(item["value"]) for item in stats.pop("blocks")]
    return {**stats, "_block_values": block_values}


def _parameter_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*(grid[key] for key in keys))]


def _matches_slice(strategy_id: str, row: dict[str, Any], parameters: dict[str, Any]) -> bool:
    features = _research_features(row)
    if strategy_id == "microstructure_ofi_v2":
        return (
            abs(_number(features.get("top_imbalance"))) >= float(parameters["min_abs_top_imbalance"])
            and _number(features.get("top_depth_usd")) >= float(parameters["min_depth_usd"])
            and _number(features.get("spread_bps"), default=float("inf")) <= float(parameters["max_spread_bps"])
        )
    if strategy_id == "liquidity_vacuum_breakout_v1":
        return (
            _number(features.get("depth_thinning_5m_pct")) >= float(parameters["min_depth_thinning_pct"])
            and abs(_number(features.get("mid_return_5m_bps"))) >= float(parameters["min_abs_return_bps"])
            and _number(features.get("spread_bps"), default=float("inf")) <= float(parameters["max_spread_bps"])
        )
    if strategy_id == "microstructure_absorption_v1":
        visible_depth = _number(features.get("bid_depth_usd")) + _number(features.get("ask_depth_usd"))
        return (
            abs(_number(features.get("top_imbalance"))) >= float(parameters["min_abs_top_imbalance"])
            and _number(features.get("depth_replenishment_rate"))
            >= float(parameters["min_depth_replenishment_rate"])
            and visible_depth >= float(parameters["min_visible_depth_usd"])
            and abs(_number(features.get("mid_return_5m_bps"), default=float("inf")))
            <= float(parameters["max_abs_mid_return_5m_bps"])
        )
    return False


def _research_features(row: dict[str, Any]) -> dict[str, Any]:
    metadata_value = row.get("metadata")
    metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    nested_value = metadata.get("research_features")
    nested: dict[str, Any] = nested_value if isinstance(nested_value, dict) else {}
    return {**metadata, **nested}


def _number(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _one_sided_sign_flip_p_value(values: list[float], *, iterations: int, seed_key: str) -> float:
    if not values:
        return 1.0
    observed = statistics.fmean(values)
    if observed <= 0:
        return 1.0
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    exceed = 1
    for _ in range(max(1, iterations)):
        null_mean = statistics.fmean(value if rng.random() < 0.5 else -value for value in values)
        if null_mean >= observed:
            exceed += 1
    return exceed / (max(1, iterations) + 1)


def _apply_benjamini_hochberg(rows: list[dict[str, Any]]) -> None:
    ordered = sorted(enumerate(rows), key=lambda item: float(item[1]["one_sided_p_value"]))
    total = len(ordered)
    running = 1.0
    adjusted: dict[int, float] = {}
    for rank_from_end in range(total - 1, -1, -1):
        original_index, row = ordered[rank_from_end]
        rank = rank_from_end + 1
        raw = float(row["one_sided_p_value"]) * total / rank
        running = min(running, raw)
        adjusted[original_index] = min(1.0, running)
    for index, row in enumerate(rows):
        row["bh_q_value"] = adjusted.get(index, 1.0)


def _walk_forward_positive(values: list[float]) -> dict[str, Any]:
    if len(values) < 4:
        return {
            "fold_count": 0,
            "test_means_bps": [],
            "all_test_blocks_positive": False,
            "reason": "insufficient_effective_blocks",
        }
    boundaries = sorted({max(1, int(len(values) * fraction)) for fraction in (0.5, 0.67, 0.8)})
    means: list[float] = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(values)
        if start < end:
            means.append(statistics.fmean(values[start:end]))
    return {
        "fold_count": len(means),
        "test_means_bps": means,
        "all_test_blocks_positive": bool(means) and all(value > 0 for value in means),
    }


def _slice_gate_reasons(
    item: dict[str, Any],
    *,
    min_effective_blocks: int,
    redesign_complete: bool,
) -> list[str]:
    stats = item["statistics"]
    reasons: list[str] = []
    if stats["effective_block_count"] < min_effective_blocks:
        reasons.append("insufficient_effective_time_blocks")
    if stats["ci_95_lower"] is None or stats["ci_95_lower"] <= 0:
        reasons.append("gross_return_ci_lower_not_positive")
    if item["bh_q_value"] > 0.05:
        reasons.append("multiple_testing_adjusted_significance_failed")
    if not item["walk_forward"]["all_test_blocks_positive"]:
        reasons.append("walk_forward_stability_failed")
    if not redesign_complete:
        reasons.append("absorption_redesign_features_not_observed")
    return reasons


def _has_redesigned_feature_evidence(rows: list[dict[str, Any]], strategy_id: str) -> bool:
    required = set(RESEARCH_GRIDS[strategy_id].get("required_new_features") or [])
    if not required:
        return True
    return any(
        str(row.get("strategy_id") or "") == strategy_id and required <= set(_research_features(row))
        for row in rows
    )
