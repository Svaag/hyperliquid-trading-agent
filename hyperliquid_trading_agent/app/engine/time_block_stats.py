from __future__ import annotations

import hashlib
import random
import statistics
from collections import defaultdict
from typing import Any

HOUR_MS = 3_600_000
BLOCK_MS_BY_HORIZON = {
    "5m": HOUR_MS,
    "15m": HOUR_MS,
    "1h": 4 * HOUR_MS,
    "4h": 24 * HOUR_MS,
    "24h": 7 * 24 * HOUR_MS,
}


def non_overlapping_block_statistics(
    rows: list[dict[str, Any]],
    *,
    value_field: str = "net_return_bps",
    horizon: str | None = None,
    bootstrap_iterations: int = 10_000,
    min_descriptive_blocks: int = 8,
    min_promotion_blocks: int = 30,
    seed_key: str = "engine_time_blocks_v1",
) -> dict[str, Any]:
    """Purged time-block CI with equal instrument weighting inside each block.

    An outcome is admitted only when its complete [start, end] window is contained
    in one block.  Outcomes crossing a boundary are purged; overlapping candidates
    within an instrument/block first collapse to one mean, preventing them from
    masquerading as independent trials.
    """

    by_horizon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        row_horizon = str(horizon or row.get("candidate_horizon") or row.get("outcome_window") or "")
        if row_horizon in BLOCK_MS_BY_HORIZON:
            by_horizon[row_horizon].append(row)
    if not by_horizon:
        return _empty_result(value_field, min_descriptive_blocks, min_promotion_blocks)

    instrument_blocks: dict[tuple[str, int, int, str], list[float]] = defaultdict(list)
    purged = 0
    invalid = 0
    for row_horizon, horizon_rows in by_horizon.items():
        block_ms = BLOCK_MS_BY_HORIZON[row_horizon]
        for row in horizon_rows:
            try:
                start_value = row.get("window_start_ms")
                end_value = row.get("window_end_ms")
                outcome_value = row.get(value_field)
                if start_value is None or end_value is None or outcome_value is None:
                    raise TypeError("required block field missing")
                start_ms = int(start_value)
                end_ms = int(end_value)
                value = float(outcome_value)
            except (TypeError, ValueError):
                invalid += 1
                continue
            block_start = (start_ms // block_ms) * block_ms
            block_end = block_start + block_ms
            if end_ms <= start_ms or start_ms < block_start or end_ms > block_end:
                purged += 1
                continue
            instrument = str(
                row.get("instrument_id")
                or row.get("underlying_id")
                or f"{row.get('venue_id') or row.get('venue') or 'unknown'}:{row.get('asset') or 'UNKNOWN'}"
            )
            instrument_blocks[(row_horizon, block_start, block_end, instrument)].append(value)

    block_instruments: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for (row_horizon, block_start, block_end, _instrument), values in instrument_blocks.items():
        block_instruments[(row_horizon, block_start, block_end)].append(statistics.fmean(values))
    block_rows: list[dict[str, Any]] = [
        {
            "horizon": key[0],
            "block_start_ms": key[1],
            "block_end_ms": key[2],
            "instrument_count": len(instrument_values),
            "value": statistics.fmean(instrument_values),
        }
        for key, instrument_values in sorted(block_instruments.items())
    ]
    values = [float(row["value"]) for row in block_rows]
    block_count = len(values)
    mean = statistics.fmean(values) if values else None
    lower, upper = _bootstrap_ci(
        values,
        iterations=max(1, int(bootstrap_iterations)),
        seed_key=f"{seed_key}:{value_field}:{','.join(sorted(by_horizon))}",
    )
    descriptive = block_count >= min_descriptive_blocks
    promotion_eligible = block_count >= min_promotion_blocks and lower is not None and lower > 0.0
    return {
        "value_field": value_field,
        "mean": mean,
        "ci_95_lower": lower,
        "ci_95_upper": upper,
        "effective_block_count": block_count,
        "raw_outcome_count": sum(len(items) for items in by_horizon.values()),
        "included_instrument_block_count": len(instrument_blocks),
        "purged_cross_boundary_count": purged,
        "invalid_row_count": invalid,
        "descriptive_ci": descriptive,
        "ci_status": "reportable" if descriptive else "descriptive_only_insufficient_blocks",
        "promotion_eligible": promotion_eligible,
        "promotion_reason_codes": _promotion_reasons(
            block_count=block_count,
            lower=lower,
            min_promotion_blocks=min_promotion_blocks,
        ),
        "minimum_descriptive_blocks": min_descriptive_blocks,
        "minimum_promotion_blocks": min_promotion_blocks,
        "bootstrap_iterations": max(1, int(bootstrap_iterations)),
        "weighting": "candidate_mean_per_instrument_then_equal_instruments_per_non_overlapping_time_block",
        "block_duration_ms_by_horizon": {item: BLOCK_MS_BY_HORIZON[item] for item in sorted(by_horizon)},
        "blocks": block_rows,
    }


def _bootstrap_ci(values: list[float], *, iterations: int, seed_key: str) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    count = len(values)
    means = [statistics.fmean(values[rng.randrange(count)] for _ in range(count)) for _ in range(iterations)]
    means.sort()
    lower_index = max(0, min(len(means) - 1, int(0.025 * (len(means) - 1))))
    upper_index = max(0, min(len(means) - 1, int(0.975 * (len(means) - 1))))
    return means[lower_index], means[upper_index]


def _promotion_reasons(*, block_count: int, lower: float | None, min_promotion_blocks: int) -> list[str]:
    reasons: list[str] = []
    if block_count < min_promotion_blocks:
        reasons.append("insufficient_effective_time_blocks")
    if lower is None:
        reasons.append("confidence_interval_unavailable")
    elif lower <= 0:
        reasons.append("confidence_interval_lower_bound_not_positive")
    return reasons


def _empty_result(value_field: str, min_descriptive_blocks: int, min_promotion_blocks: int) -> dict[str, Any]:
    return {
        "value_field": value_field,
        "mean": None,
        "ci_95_lower": None,
        "ci_95_upper": None,
        "effective_block_count": 0,
        "raw_outcome_count": 0,
        "included_instrument_block_count": 0,
        "purged_cross_boundary_count": 0,
        "invalid_row_count": 0,
        "descriptive_ci": False,
        "ci_status": "descriptive_only_insufficient_blocks",
        "promotion_eligible": False,
        "promotion_reason_codes": [
            "insufficient_effective_time_blocks",
            "confidence_interval_unavailable",
        ],
        "minimum_descriptive_blocks": min_descriptive_blocks,
        "minimum_promotion_blocks": min_promotion_blocks,
        "bootstrap_iterations": 0,
        "weighting": "candidate_mean_per_instrument_then_equal_instruments_per_non_overlapping_time_block",
        "block_duration_ms_by_horizon": {},
        "blocks": [],
    }
