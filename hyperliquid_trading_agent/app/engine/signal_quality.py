from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from typing import Any

from hyperliquid_trading_agent.app.engine.time_block_stats import non_overlapping_block_statistics

OUTCOME_WINDOW_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "24h": 24 * 60 * 60_000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct(numerator: int, denominator: int) -> float:
    return numerator / denominator * 100.0 if denominator else 0.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


async def _page_outcomes(
    repository: Any,
    *,
    start_ms: int,
    end_ms: int,
    page_size: int = 5000,
    max_rows: int = 100_000,
) -> list[dict[str, Any]]:
    method = getattr(repository, "list_candidate_outcome_attributions", None)
    if not callable(method):
        return []
    rows: list[dict[str, Any]] = []
    seen_pages: set[tuple[str, ...]] = set()
    offset = 0
    while len(rows) < max_rows:
        try:
            page = list(
                await method(
                    since_ms=start_ms,
                    until_ms=end_ms,
                    limit=min(page_size, max_rows - len(rows)),
                    offset=offset,
                )
            )
        except TypeError:
            page = list(await method(limit=min(max_rows, 20_000)))
            page = [row for row in page if start_ms <= int(row.get("window_end_ms") or 0) <= end_ms]
        if not page:
            break
        fingerprint = tuple(str(row.get("attribution_id") or "") for row in page[:5])
        if fingerprint in seen_pages:
            break
        seen_pages.add(fingerprint)
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return rows[:max_rows]


async def _related_rows(
    repository: Any,
    *,
    regime_ids: list[str],
    allocation_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    regime_method = getattr(repository, "list_regime_snapshots_by_ids", None)
    if callable(regime_method):
        regimes = list(await regime_method(regime_ids))
    else:
        fallback = getattr(repository, "list_regime_snapshots", None)
        regimes = list(await fallback(limit=100_000)) if callable(fallback) else []
        ids = set(regime_ids)
        regimes = [row for row in regimes if str(row.get("regime_snapshot_id") or "") in ids]
    allocation_method = getattr(repository, "list_allocation_decisions_by_ids", None)
    if callable(allocation_method):
        allocations = list(await allocation_method(allocation_ids))
    else:
        fallback = getattr(repository, "list_allocation_decisions", None)
        allocations = list(await fallback(limit=100_000)) if callable(fallback) else []
        ids = set(allocation_ids)
        allocations = [row for row in allocations if str(row.get("allocation_id") or "") in ids]
    return (
        {str(row.get("regime_snapshot_id") or ""): row for row in regimes},
        {str(row.get("allocation_id") or ""): row for row in allocations},
    )


def _regime_context(regime: dict[str, Any]) -> tuple[str, str, int]:
    vector = _dict(regime.get("vector")) or regime
    metadata = _dict(vector.get("metadata"))
    mode = str(metadata.get("observed_news_risk_mode") or vector.get("news_risk_mode") or "neutral")
    return (
        str(vector.get("regime_label") or "unknown"),
        mode,
        int(vector.get("as_of_ms") or regime.get("as_of_ms") or 0),
    )


async def load_signal_quality_rows(
    repository: Any,
    *,
    window_hours: int = 24,
    as_of_ms: int | None = None,
    max_rows: int = 100_000,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    end_ms = int(as_of_ms or _now_ms())
    start_ms = end_ms - max(1, int(window_hours)) * 3_600_000
    bounded_max_rows = max(1, min(100_000, int(max_rows)))
    source_rows = await _page_outcomes(
        repository,
        start_ms=start_ms,
        end_ms=end_ms,
        max_rows=bounded_max_rows,
    )
    regime_ids = [str(row.get("regime_snapshot_id") or "") for row in source_rows]
    allocation_ids = [str(row.get("allocation_id") or "") for row in source_rows]
    regimes, allocations = await _related_rows(
        repository,
        regime_ids=regime_ids,
        allocation_ids=allocation_ids,
    )
    duplicate_keys = Counter(
        (str(row.get("candidate_id") or ""), str(row.get("outcome_window") or "")) for row in source_rows
    )
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in source_rows:
        key = (str(row.get("candidate_id") or ""), str(row.get("outcome_window") or ""))
        current = latest.get(key)
        if current is None or int(row.get("updated_at_ms") or 0) >= int(current.get("updated_at_ms") or 0):
            latest[key] = row
    terminal_counts = Counter(str(row.get("terminal_state") or "pending") for row in latest.values())
    fallback_or_late = 0
    regime_joined = 0
    allocation_joined = 0
    clock_violations = 0
    usable: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    for row in latest.values():
        terminal = str(row.get("terminal_state") or "pending")
        if terminal != "matured":
            exclusion_counts[f"terminal_state:{terminal}"] += 1
            continue
        if str(row.get("side") or "flat") not in {"long", "short"}:
            exclusion_counts["non_directional"] += 1
            continue
        metadata = _dict(row.get("metadata"))
        flags = {str(item) for item in row.get("quality_flags") or []}
        mark_source = str(metadata.get("mark_source") or "unknown")
        bad_mark = (
            bool({"latest_mark_fallback", "late_mark", "future_mark"} & flags) or mark_source != "feature_store_mid"
        )
        if bad_mark:
            fallback_or_late += 1
            exclusion_counts["non_strict_mark"] += 1
            continue
        regime_id = str(row.get("regime_snapshot_id") or "")
        regime = regimes.get(regime_id)
        if regime is None:
            exclusion_counts["regime_join_missing"] += 1
            continue
        regime_joined += 1
        allocation_id = str(row.get("allocation_id") or "")
        allocation = allocations.get(allocation_id)
        if allocation is not None:
            allocation_joined += 1
        regime_label, news_mode, regime_as_of_ms = _regime_context(regime)
        decision_clock_skew_ms = regime_as_of_ms - int(row.get("window_start_ms") or 0)
        if abs(decision_clock_skew_ms) > 60_000:
            clock_violations += 1
            exclusion_counts["decision_clock_skew"] += 1
            continue
        combined_slippage = _f(row.get("slippage_bps"))
        spread = _f(metadata.get("expected_spread_cost_bps"))
        slippage = _f(metadata.get("expected_slippage_bps"))
        impact = _f(metadata.get("expected_market_impact_bps"))
        if spread == 0.0 and slippage == 0.0 and impact == 0.0:
            slippage = combined_slippage
        normalized = {
            **row,
            "regime_label": regime_label,
            "observed_news_risk_mode": news_mode,
            "decision_clock_skew_ms": decision_clock_skew_ms,
            "allocation": allocation or {},
            "costs": {
                "fees_bps": _f(row.get("fees_bps")),
                "spread_bps": spread,
                "slippage_bps": slippage,
                "market_impact_bps": impact,
                "funding_bps": _f(row.get("funding_bps")),
                "combined_execution_cost_bps": combined_slippage,
                "total_execution_cost_bps": _f(metadata.get("total_execution_cost_bps")),
                "cost_quality": str(row.get("execution_cost_quality") or "unavailable"),
            },
        }
        usable.append(normalized)
    total_unique = len(latest)
    context = {
        "dataset_id": "signal_quality_"
        + hashlib.sha256(
            json.dumps(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "keys": sorted(f"{key[0]}:{key[1]}" for key in latest),
                },
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24],
        "window": {
            "basis": "outcome_window_end",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "hours": max(1, int(window_hours)),
        },
        "grain": "candidate_id_x_outcome_window",
        "data_quality": {
            "sample_limit": bounded_max_rows,
            "sample_limit_reached": len(source_rows) >= bounded_max_rows,
            "rows_seen": len(source_rows),
            "unique_grain_rows": total_unique,
            "usable_rows": len(usable),
            "duplicate_keys": sum(1 for count in duplicate_keys.values() if count > 1),
            "duplicate_rows": sum(max(0, count - 1) for count in duplicate_keys.values()),
            "terminal_state_counts": dict(terminal_counts),
            "missing_mark": terminal_counts.get("missing_mark", 0),
            "pending_due": sum(
                1
                for row in latest.values()
                if str(row.get("terminal_state") or "") == "pending" and int(row.get("window_end_ms") or 0) <= end_ms
            ),
            "fallback_or_late_marks": fallback_or_late,
            "regime_join_coverage_pct": round(_pct(regime_joined, terminal_counts.get("matured", 0)), 4),
            "allocation_join_coverage_pct": round(_pct(allocation_joined, terminal_counts.get("matured", 0)), 4),
            "decision_clock_violations": clock_violations,
            "exclusion_counts": dict(exclusion_counts),
        },
    }
    return context, usable


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    net = [_f(row.get("net_return_bps")) for row in rows]
    gross = [_f(row.get("gross_return_bps")) for row in rows]
    realized_r = [_f(row.get("realized_r")) for row in rows]
    mae = [_f(row.get("mae_bps")) for row in rows]
    mfe = [_f(row.get("mfe_bps")) for row in rows]
    p05 = _quantile(net, 0.05)
    tail = [value for value in net if value <= p05]
    winners = [value for value in net if value > 0]
    losers = [value for value in net if value < 0]
    # combined_execution_cost is a compatibility field and must not be added a
    # second time when component costs are available.
    costs: list[float] = []
    for row in rows:
        item = _dict(row.get("costs"))
        total_execution = _f(item.get("total_execution_cost_bps"))
        combined_execution = _f(item.get("combined_execution_cost_bps"))
        if total_execution > 0:
            component_total = total_execution + _f(item.get("funding_bps"))
        elif combined_execution > 0:
            component_total = _f(item.get("fees_bps")) + combined_execution + _f(item.get("funding_bps"))
        else:
            component_total = sum(
                [
                    _f(item.get(name))
                    for name in ("fees_bps", "spread_bps", "slippage_bps", "market_impact_bps", "funding_bps")
                ],
                0.0,
            )
        costs.append(component_total)
    horizon = str(rows[0].get("outcome_window") or "unknown") if rows else "unknown"
    block_stats = non_overlapping_block_statistics(
        rows,
        value_field="net_return_bps",
        horizon=horizon,
        bootstrap_iterations=10_000,
        min_descriptive_blocks=8,
        min_promotion_blocks=30,
        seed_key=f"signal_quality:{horizon}",
    )
    ci = (
        [round(float(block_stats["ci_95_lower"]), 4), round(float(block_stats["ci_95_upper"]), 4)]
        if block_stats["ci_95_lower"] is not None and block_stats["ci_95_upper"] is not None
        else None
    )
    measured_rows = [
        row
        for row in rows
        if row.get("execution_adjusted_return_bps") is not None
        and str(row.get("execution_cost_quality") or "unavailable") == "measured"
    ]
    execution_stats = non_overlapping_block_statistics(
        measured_rows,
        value_field="execution_adjusted_return_bps",
        horizon=horizon,
        bootstrap_iterations=10_000,
        min_descriptive_blocks=8,
        min_promotion_blocks=30,
        seed_key=f"signal_quality_execution:{horizon}",
    )
    return {
        "n": len(rows),
        "unique_candidate_count": len({str(row.get("candidate_id") or "") for row in rows}),
        "non_overlapping_block_count": block_stats["effective_block_count"],
        "gross_hit_rate_pct": round(_pct(sum(value > 0 for value in gross), len(gross)), 4),
        "modeled_net_hit_rate_pct": round(_pct(sum(value > 0 for value in net), len(net)), 4),
        "mean_gross_return_bps": round(_avg(gross), 4),
        "mean_modeled_net_return_bps": round(_avg(net), 4),
        "median_modeled_net_return_bps": round(statistics.median(net), 4) if net else 0.0,
        "p05_return_bps": round(p05, 4),
        "expected_shortfall_05_bps": round(_avg(tail), 4),
        "mean_mae_bps": round(_avg(mae), 4),
        "p95_adverse_excursion_bps": round(_quantile([abs(min(0.0, value)) for value in mae], 0.95), 4),
        "mean_mfe_bps": round(_avg(mfe), 4),
        "mean_realized_r": round(_avg(realized_r), 4),
        "mean_modeled_cost_bps": round(_avg(costs), 4),
        "gross_to_net_delta_bps": round(_avg(gross) - _avg(net), 4),
        "payoff_ratio": round(_avg(winners) / abs(_avg(losers)), 4) if winners and losers and _avg(losers) else None,
        "profit_factor": round(sum(winners) / abs(sum(losers)), 4) if losers and sum(losers) else None,
        "mean_modeled_net_return_ci95_bps": ci,
        "time_block_confidence": {key: value for key, value in block_stats.items() if key != "blocks"},
        "execution_adjusted_mean_bps": execution_stats["mean"],
        "execution_adjusted_ci95_bps": [execution_stats["ci_95_lower"], execution_stats["ci_95_upper"]],
        "execution_adjusted_effective_block_count": execution_stats["effective_block_count"],
        "execution_adjusted_performance_used": bool(measured_rows),
        "confidence": "inferential" if block_stats["descriptive_ci"] else "descriptive",
        "promotion_eligible": block_stats["promotion_eligible"],
        "promotion_reason_codes": block_stats["promotion_reason_codes"],
    }


async def build_signal_quality_report(
    repository: Any,
    *,
    window_hours: int = 24,
    as_of_ms: int | None = None,
    strategy_id: str | None = None,
    symbol: str | None = None,
    regime_label: str | None = None,
    outcome_window: str | None = None,
    max_rows: int = 100_000,
) -> dict[str, Any]:
    context, rows = await load_signal_quality_rows(
        repository,
        window_hours=window_hours,
        as_of_ms=as_of_ms,
        max_rows=max_rows,
    )
    if strategy_id:
        rows = [row for row in rows if str(row.get("strategy_id") or "") == strategy_id]
    if symbol:
        rows = [row for row in rows if str(row.get("asset") or "").upper() == symbol.upper()]
    if regime_label:
        rows = [row for row in rows if str(row.get("regime_label") or "") == regime_label]
    if outcome_window:
        rows = [row for row in rows if str(row.get("outcome_window") or "") == outcome_window]
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    by_window: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group_key = (
            str(row.get("strategy_id") or "unknown"),
            str(row.get("strategy_family") or "unknown"),
            str(row.get("asset") or "UNKNOWN").upper(),
            str(row.get("regime_label") or "unknown"),
            str(row.get("observed_news_risk_mode") or "neutral"),
            str(row.get("candidate_horizon") or "unknown"),
            str(row.get("outcome_window") or "unknown"),
        )
        groups[group_key].append(row)
        by_window[group_key[-1]].append(row)
    group_rows = []
    for key, values in groups.items():
        group_rows.append(
            {
                "strategy_id": key[0],
                "strategy_family": key[1],
                "symbol": key[2],
                "regime_label": key[3],
                "observed_news_risk_mode": key[4],
                "candidate_horizon": key[5],
                "outcome_window": key[6],
                **_group_metrics(values),
            }
        )
    return {
        **context,
        "generated_at_ms": _now_ms(),
        "filters": {
            "strategy_id": strategy_id,
            "symbol": symbol.upper() if symbol else None,
            "regime_label": regime_label,
            "outcome_window": outcome_window,
        },
        "overall_by_outcome_window": [
            {"outcome_window": window, **_group_metrics(values)}
            for window, values in sorted(by_window.items(), key=lambda item: OUTCOME_WINDOW_MS.get(item[0], 0))
        ],
        "groups": sorted(
            group_rows, key=lambda item: (-int(item["n"]), item["strategy_id"], item["symbol"], item["outcome_window"])
        ),
        "legacy_mixed_latest_endpoint": {
            "status": "deprecated",
            "reason": "mixed_horizons_and_non_homogeneous_candidate_cohorts",
            "readiness_eligible": False,
        },
        "semantics": {
            "net_return": "legacy modeled gross mark return minus scorer-estimated costs; not execution PnL",
            "execution_adjusted_return": "reported only when a depth-walk fill and venue fee schedule have measured cost quality",
            "horizons_are_never_pooled_for_promotion": True,
            "strict_mark_source": "feature_store_mid",
            "independence_unit": "purged non-overlapping time block after per-instrument collapse",
            "promotion_gate": "at least 30 effective blocks and lower 95% confidence bound above zero",
        },
    }
