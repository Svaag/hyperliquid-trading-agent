from __future__ import annotations

import bisect
import time
from collections import Counter, defaultdict
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison


def _now_ms() -> int:
    return int(time.time() * 1000)


def _f(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100.0, 4) if denominator else 0.0


async def _maybe_list(repository: Any, method_name: str, *, limit: int) -> list[dict[str, Any]]:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return []
    try:
        return list(await method(limit=limit))
    except TypeError:
        return list(await method())


def _timestamp(item: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(item.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _in_window(items: list[dict[str, Any]], start_ms: int, *keys: str) -> list[dict[str, Any]]:
    return [item for item in items if _timestamp(item, *keys) >= start_ms]


def _legacy_summary(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = [
        item
        for item in evaluations
        if str(item.get("terminal_outcome") or "open") != "open"
        or _f(item.get("realized_or_marked_r")) is not None
    ]
    r_values = [value for item in terminal if (value := _f(item.get("realized_or_marked_r"))) is not None]
    positive = len([value for value in r_values if value > 0])
    latest_mark_returns: list[float] = []
    for item in evaluations:
        completed_marks = [
            mark
            for mark in item.get("marks") or []
            if str(mark.get("status") or "") == "completed"
            and _f(mark.get("direction_adjusted_return_bps")) is not None
        ]
        if completed_marks:
            latest = max(completed_marks, key=lambda mark: _timestamp(mark, "marked_at_ms", "due_at_ms"))
            value = _f(latest.get("direction_adjusted_return_bps"))
            if value is not None:
                latest_mark_returns.append(value)
    return {
        "source": "legacy_signal_engine",
        "evaluation_count": len(evaluations),
        "terminal_evaluation_count": len(terminal),
        "positive_r_count": positive,
        "hit_rate_pct": _pct(positive, len(r_values)),
        "avg_realized_or_marked_r": _avg(r_values),
        "latest_mark_sample_count": len(latest_mark_returns),
        "avg_latest_direction_adjusted_return_bps": _avg(latest_mark_returns),
        "terminal_outcome_counts": dict(
            Counter(str(item.get("terminal_outcome") or "open") for item in evaluations)
        ),
        "signal_type_counts": dict(Counter(str(item.get("signal_type") or "unknown") for item in evaluations)),
    }


def _latest_outcomes_by_candidate(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in outcomes:
        if str(item.get("terminal_state") or "pending") == "pending":
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id:
            continue
        existing = latest.get(candidate_id)
        if existing is None or _timestamp(item, "window_end_ms", "updated_at_ms") > _timestamp(
            existing, "window_end_ms", "updated_at_ms"
        ):
            latest[candidate_id] = item
    return list(latest.values())


def _engine_summary(
    candidates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    pnl_records: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    latest_replay: dict[str, Any] | None,
) -> dict[str, Any]:
    latest_outcomes = _latest_outcomes_by_candidate(outcomes)
    net_returns = [value for item in latest_outcomes if (value := _f(item.get("net_return_bps"))) is not None]
    realized_r = [value for item in latest_outcomes if (value := _f(item.get("realized_r"))) is not None]
    positive = len([value for value in net_returns if value > 0])
    return {
        "source": "institutional_engine",
        "candidate_count": len(candidates),
        "matured_candidate_outcome_count": len(latest_outcomes),
        "positive_net_return_count": positive,
        "hit_rate_pct": _pct(positive, len(net_returns)),
        "avg_net_return_bps": _avg(net_returns),
        "avg_realized_r": _avg(realized_r),
        "shadow_pnl_attribution_count": len(pnl_records),
        "shadow_total_pnl_usd": round(
            sum(_f(item.get("total_pnl_usd")) or 0.0 for item in pnl_records),
            4,
        ),
        "operator_proposal_count": len(proposals),
        "operator_proposal_status_counts": dict(
            Counter(str(item.get("status") or "unknown") for item in proposals)
        ),
        "strategy_counts": dict(Counter(str(item.get("strategy_id") or "unknown") for item in candidates)),
        "latest_replay": {
            "replay_id": latest_replay.get("replay_id"),
            "status": latest_replay.get("status"),
            "verdict": (latest_replay.get("metadata") or {}).get("verdict"),
            "promotion_decision": (latest_replay.get("metadata") or {}).get("promotion_decision"),
            "created_at_ms": latest_replay.get("created_at_ms"),
        }
        if latest_replay
        else None,
    }


def _overlap_report(
    evaluations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    tolerance_minutes: int,
) -> dict[str, Any]:
    tolerance_ms = max(1, tolerance_minutes) * 60_000
    by_key: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for candidate in candidates:
        key = (str(candidate.get("asset") or "").upper(), str(candidate.get("side") or "").lower())
        created_at_ms = _timestamp(candidate, "created_at_ms")
        if key[0] and key[1] in {"long", "short"} and created_at_ms:
            by_key[key].append((created_at_ms, candidate))
    for rows in by_key.values():
        rows.sort(key=lambda row: row[0])

    matches: list[dict[str, Any]] = []
    for evaluation in evaluations:
        key = (str(evaluation.get("symbol") or "").upper(), str(evaluation.get("side") or "").lower())
        rows = by_key.get(key) or []
        signal_ts = _timestamp(evaluation, "created_at_ms")
        if not rows or not signal_ts:
            continue
        timestamps = [row[0] for row in rows]
        index = bisect.bisect_left(timestamps, signal_ts)
        nearby = rows[max(0, index - 2) : min(len(rows), index + 2)]
        if not nearby:
            continue
        candidate_ts, candidate = min(nearby, key=lambda row: abs(row[0] - signal_ts))
        delta_ms = abs(candidate_ts - signal_ts)
        if delta_ms > tolerance_ms:
            continue
        matches.append(
            {
                "legacy_signal_id": evaluation.get("signal_id"),
                "candidate_id": candidate.get("candidate_id"),
                "symbol": key[0],
                "side": key[1],
                "strategy_id": candidate.get("strategy_id"),
                "time_delta_seconds": round(delta_ms / 1000.0, 3),
                "legacy_created_at_ms": signal_ts,
                "candidate_created_at_ms": candidate_ts,
            }
        )
    return {
        "matching_rule": f"same symbol and side within {tolerance_minutes} minutes",
        "legacy_signal_count": len(evaluations),
        "matched_legacy_signal_count": len(matches),
        "legacy_overlap_pct": _pct(len(matches), len(evaluations)),
        "matches": matches[:100],
    }


async def build_signal_path_comparison(
    repository: Any,
    *,
    settings: Settings | None = None,
    window_hours: int = 24,
    limit: int = 5000,
    overlap_tolerance_minutes: int = 30,
) -> dict[str, Any]:
    """Compare legacy signal outcomes with canonical engine candidate outcomes.

    The report is read-only. Engine operator proposals remain acknowledgment-only and
    are never written into legacy ``trade_signals`` or promoted to execution here.
    """

    generated_at_ms = _now_ms()
    start_ms = generated_at_ms - max(1, window_hours) * 60 * 60 * 1000
    bounded_limit = max(1, min(20_000, limit))
    evaluations = _in_window(
        await _maybe_list(repository, "list_signal_evaluations", limit=bounded_limit),
        start_ms,
        "created_at_ms",
    )
    candidates = _in_window(
        await _maybe_list(repository, "list_alpha_candidates", limit=bounded_limit),
        start_ms,
        "created_at_ms",
    )
    outcomes = _in_window(
        await _maybe_list(repository, "list_candidate_outcome_attributions", limit=bounded_limit),
        start_ms,
        "window_end_ms",
        "updated_at_ms",
        "created_at_ms",
    )
    pnl_records = _in_window(
        await _maybe_list(repository, "list_pnl_attribution", limit=bounded_limit),
        start_ms,
        "window_end_ms",
    )
    proposals = _in_window(
        await _maybe_list(repository, "list_engine_operator_proposals", limit=bounded_limit),
        start_ms,
        "created_at_ms",
    )
    latest_replay = await latest_engine_replay_comparison(repository)
    return {
        "generated_at_ms": generated_at_ms,
        "window": {"hours": max(1, window_hours), "start_ms": start_ms, "end_ms": generated_at_ms},
        "safety": {
            "execution_authority": "none",
            "engine_operator_proposals_acknowledgment_only": True,
            "paper_order_created_by_report": False,
            "live_order_created_by_report": False,
        },
        "path_mapping": {
            "legacy": {
                "model": "TradeSignal",
                "outcome_ledger": "signal_evaluations",
                "configured_with_engine": bool(settings.autonomy_signals_run_with_engine_enabled)
                if settings
                else None,
            },
            "engine": {
                "model": "AlphaCandidate -> EVEstimate -> AllocationDecision -> OrderIntent",
                "operator_projection": "engine_operator_proposals",
                "operator_proposals_enabled": bool(settings.engine_operator_proposals_enabled)
                if settings
                else None,
            },
            "canonical_operator_signal_source": "institutional_engine_operator_proposals",
        },
        "legacy": _legacy_summary(evaluations),
        "engine": _engine_summary(candidates, outcomes, pnl_records, proposals, latest_replay),
        "overlap": _overlap_report(
            evaluations,
            candidates,
            tolerance_minutes=overlap_tolerance_minutes,
        ),
    }
