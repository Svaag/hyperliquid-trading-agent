from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

ALLOCATING_STATUSES = {"allocate", "reduce", "require_debate"}
COUNCIL_ALLOW_DECISIONS = {"allow_shadow", "allow_paper"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


async def _list_rows(repository: Any, method_name: str, *, limit: int = 20_000, **kwargs: Any) -> list[dict[str, Any]]:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return []
    try:
        return list(await method(limit=limit, **kwargs))
    except TypeError:
        try:
            return list(await method(limit=limit))
        except TypeError:
            return list(await method())


def _in_window(rows: list[dict[str, Any]], start_ms: int, end_ms: int, key: str = "created_at_ms") -> list[dict[str, Any]]:
    return [row for row in rows if start_ms <= int(row.get(key) or 0) <= end_ms]


def _latest_by(rows: list[dict[str, Any]], key: str, *, timestamp_key: str = "created_at_ms") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value:
            continue
        current = out.get(value)
        if current is None or int(row.get(timestamp_key) or 0) >= int(current.get(timestamp_key) or 0):
            out[value] = row
    return out


def _allowed_risk(payload: dict[str, Any]) -> bool:
    if "allowed" in payload:
        return bool(payload.get("allowed"))
    return str(payload.get("decision") or "allow") in {"allow", "not_applicable"}


def _candidate_stage(
    candidate: dict[str, Any],
    *,
    ev: dict[str, Any] | None,
    packet: dict[str, Any] | None,
    final_allocation: dict[str, Any] | None,
    council: dict[str, Any] | None,
    intent: dict[str, Any] | None,
    report: dict[str, Any] | None,
    matured_count: int,
) -> tuple[str, list[str], dict[str, bool]]:
    reached = {
        "candidate": True,
        "ev_scored": ev is not None,
        "allocator_approved": False,
        "risk_allowed": False,
        "diversity_allowed": False,
        "council_allowed": False,
        "terminal_allocation_approved": False,
        "shadow_intent": intent is not None and str(intent.get("execution_mode") or "") == "shadow",
        "execution_report": report is not None,
        "matured_outcome": matured_count > 0,
    }
    reasons: list[str] = []
    if ev is None:
        return "ev_missing", ["ev_estimate_missing"], reached
    if packet is None:
        return "packet_missing", ["candidate_trade_packet_missing"], reached
    allocation = _dict(packet.get("allocation"))
    allocation_status = str(allocation.get("status") or "skip")
    reasons.extend(str(item) for item in allocation.get("reason_codes") or [])
    reached["allocator_approved"] = allocation_status in ALLOCATING_STATUSES
    if not reached["allocator_approved"]:
        return "allocator_rejected", reasons or ["allocation_not_approved"], reached
    risk = _dict(packet.get("risk_decision"))
    reached["risk_allowed"] = _allowed_risk(risk)
    if not reached["risk_allowed"]:
        violations = _list(risk.get("violations"))
        reasons.extend(str(item.get("code") or item) if isinstance(item, dict) else str(item) for item in violations)
        return "risk_rejected", reasons or ["risk_gateway_reject"], reached
    metadata = _dict(allocation.get("metadata"))
    diversity = _dict(metadata.get("diversity"))
    diversity_reasons = [str(item) for item in diversity.get("reason_codes") or []]
    reasons.extend(diversity_reasons)
    report_only = bool(
        (diversity.get("projected") or {}).get("shadow_observation_report_only")
        if isinstance(diversity.get("projected"), dict)
        else False
    ) or "shadow_observation_report_only" in diversity_reasons
    reached["diversity_allowed"] = str(diversity.get("decision") or "allow") != "throttle" or report_only
    if not reached["diversity_allowed"]:
        return "diversity_blocked", reasons or ["diversity_throttle"], reached
    if council is None:
        return "council_missing", [*reasons, "council_review_missing"], reached
    council_vetoes = [str(item) for item in council.get("vetoes") or []]
    reasons.extend(council_vetoes)
    reached["council_allowed"] = str(council.get("decision") or "") in COUNCIL_ALLOW_DECISIONS
    if not reached["council_allowed"]:
        return "council_rejected", reasons or ["council_not_allowed"], reached
    final_status = str((final_allocation or {}).get("status") or "unknown")
    reached["terminal_allocation_approved"] = final_status in ALLOCATING_STATUSES
    if not reached["terminal_allocation_approved"]:
        reasons.extend(str(item) for item in (final_allocation or {}).get("reason_codes") or [])
        return "terminal_allocation_rejected", reasons or ["terminal_allocation_not_approved"], reached
    if intent is None:
        return "shadow_intent_missing", [*reasons, "shadow_intent_missing"], reached
    if report is None:
        return "execution_report_missing", [*reasons, "execution_report_missing"], reached
    return ("matured_outcome" if matured_count else "awaiting_outcome"), reasons, reached


async def build_candidate_funnel(
    repository: Any,
    *,
    window_hours: int = 24,
    as_of_ms: int | None = None,
    strategy_id: str | None = None,
    asset: str | None = None,
    limit: int = 20_000,
) -> dict[str, Any]:
    end_ms = int(as_of_ms or _now_ms())
    start_ms = end_ms - max(1, int(window_hours)) * 3_600_000
    candidates = await _list_rows(
        repository,
        "list_alpha_candidates",
        limit=limit,
        since_ms=start_ms,
        until_ms=end_ms,
    )
    candidates = _in_window(candidates, start_ms, end_ms)
    if strategy_id:
        candidates = [row for row in candidates if str(row.get("strategy_id") or "") == strategy_id]
    if asset:
        candidates = [row for row in candidates if str(row.get("asset") or "").upper() == asset.upper()]
    candidate_ids = {str(row.get("candidate_id") or "") for row in candidates}
    evs, allocations, packets, councils, intents, reports, outcomes = await _candidate_related_rows(
        repository,
        limit=limit,
        start_ms=start_ms,
        end_ms=end_ms,
        strategy_id=strategy_id,
    )
    ev_by_candidate = _latest_by([row for row in evs if str(row.get("candidate_id") or "") in candidate_ids], "candidate_id")
    allocation_by_candidate = _latest_by(
        [row for row in allocations if str(row.get("candidate_id") or "") in candidate_ids],
        "candidate_id",
    )
    packet_by_candidate = _latest_by(
        [row for row in packets if str(row.get("candidate_id") or "") in candidate_ids],
        "candidate_id",
    )
    council_by_candidate = _latest_by(
        [row for row in councils if str(row.get("candidate_id") or "") in candidate_ids],
        "candidate_id",
    )
    intent_by_candidate = _latest_by(
        [row for row in intents if str(row.get("parent_candidate_id") or "") in candidate_ids],
        "parent_candidate_id",
    )
    report_by_intent = _latest_by(reports, "intent_id")
    matured_by_candidate = Counter(
        str(row.get("candidate_id") or "")
        for row in outcomes
        if str(row.get("candidate_id") or "") in candidate_ids
        and str(row.get("terminal_state") or "") == "matured"
    )
    stage_counts: Counter[str] = Counter()
    first_failure_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    group_data: dict[tuple[str, str, str], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id_value = str(candidate.get("candidate_id") or "")
        intent = intent_by_candidate.get(candidate_id_value)
        report = report_by_intent.get(str((intent or {}).get("intent_id") or ""))
        primary_stage, reasons, reached = _candidate_stage(
            candidate,
            ev=ev_by_candidate.get(candidate_id_value),
            packet=packet_by_candidate.get(candidate_id_value),
            final_allocation=allocation_by_candidate.get(candidate_id_value),
            council=council_by_candidate.get(candidate_id_value),
            intent=intent,
            report=report,
            matured_count=matured_by_candidate.get(candidate_id_value, 0),
        )
        for stage, did_reach in reached.items():
            if did_reach:
                stage_counts[stage] += 1
        first_failure_counts[primary_stage] += 1
        clean_reasons = sorted({reason for reason in reasons if reason})
        reason_counts.update(clean_reasons)
        metadata = _dict(candidate.get("metadata"))
        strategy = str(candidate.get("strategy_id") or "unknown")
        family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        candidate_asset = str(candidate.get("asset") or "UNKNOWN").upper()
        key = (strategy, family, candidate_asset)
        group = group_data.setdefault(
            key,
            {"strategy_id": strategy, "strategy_family": family, "asset": candidate_asset, "candidate_count": 0, "stage_counts": Counter(), "first_failure_counts": Counter(), "reason_counts": Counter()},
        )
        group["candidate_count"] += 1
        group["first_failure_counts"][primary_stage] += 1
        group["reason_counts"].update(clean_reasons)
        for stage, did_reach in reached.items():
            if did_reach:
                group["stage_counts"][stage] += 1
        rows.append(
            {
                "candidate_id": candidate_id_value,
                "strategy_id": strategy,
                "strategy_family": family,
                "asset": candidate_asset,
                "side": candidate.get("side"),
                "horizon": candidate.get("horizon"),
                "primary_stage": primary_stage,
                "reason_codes": clean_reasons,
                "pre_council_allocation_status": ((packet_by_candidate.get(candidate_id_value) or {}).get("allocation") or {}).get("status"),
                "final_allocation_status": (allocation_by_candidate.get(candidate_id_value) or {}).get("status"),
                "council_decision": (council_by_candidate.get(candidate_id_value) or {}).get("decision"),
                "shadow_intent": bool(reached["shadow_intent"]),
                "matured_outcome_count": matured_by_candidate.get(candidate_id_value, 0),
            }
        )
    groups = []
    for group in group_data.values():
        groups.append(
            {
                **{key: value for key, value in group.items() if not isinstance(value, Counter)},
                "stage_counts": dict(group["stage_counts"]),
                "first_failure_counts": dict(group["first_failure_counts"]),
                "reason_counts": dict(group["reason_counts"]),
            }
        )
    return {
        "generated_at_ms": _now_ms(),
        "window": {"hours": max(1, int(window_hours)), "start_ms": start_ms, "end_ms": end_ms},
        "sample_limit": limit,
        "sample_limit_reached": len(candidates) >= limit,
        "grain": "candidate_id",
        "candidate_count": len(candidates),
        "stage_counts": dict(stage_counts),
        "first_failure_counts": dict(first_failure_counts),
        "reason_counts": dict(reason_counts),
        "groups": sorted(groups, key=lambda item: (-int(item["candidate_count"]), item["strategy_id"], item["asset"])),
        "items": rows,
        "methodology": {
            "primary_failure": "first_terminal_stage",
            "pre_council_source": "candidate_trade_packets.allocation",
            "terminal_allocation_source": "allocation_decisions",
            "downstream_reason_deduplication": True,
        },
    }


async def _candidate_related_rows(
    repository: Any,
    *,
    limit: int,
    start_ms: int,
    end_ms: int,
    strategy_id: str | None,
) -> tuple[list[dict[str, Any]], ...]:
    packet_method = (
        "list_candidate_trade_packet_summaries"
        if callable(getattr(repository, "list_candidate_trade_packet_summaries", None))
        else "list_candidate_trade_packets"
    )
    rows = await asyncio.gather(
        _list_rows(repository, "list_ev_estimates", limit=limit, since_ms=start_ms, until_ms=end_ms),
        _list_rows(repository, "list_allocation_decisions", limit=limit, since_ms=start_ms, until_ms=end_ms),
        _list_rows(
            repository,
            packet_method,
            limit=limit,
            since_ms=start_ms,
            until_ms=end_ms,
            strategy_id=strategy_id,
        ),
        _list_rows(
            repository,
            "list_council_reviews",
            limit=limit,
            since_ms=start_ms,
            until_ms=end_ms,
            strategy_id=strategy_id,
        ),
        _list_rows(repository, "list_order_intents", limit=limit, since_ms=start_ms, until_ms=end_ms),
        _list_rows(repository, "list_execution_reports", limit=limit, since_ms=start_ms, until_ms=end_ms),
        _list_rows(
            repository,
            "list_candidate_outcome_attributions",
            limit=limit,
            since_ms=start_ms,
            until_ms=end_ms,
            strategy_id=strategy_id,
        ),
    )
    return tuple(rows)


async def build_strategy_funnel(
    repository: Any,
    *,
    window_hours: int = 24,
    as_of_ms: int | None = None,
    strategy_id: str | None = None,
    asset: str | None = None,
    limit: int = 50_000,
) -> dict[str, Any]:
    end_ms = int(as_of_ms or _now_ms())
    start_ms = end_ms - max(1, int(window_hours)) * 3_600_000
    evaluations = await _list_rows(
        repository,
        "list_engine_strategy_evaluations",
        limit=limit,
        strategy_id=strategy_id,
        asset=asset,
        since_ms=start_ms,
        until_ms=end_ms,
    )
    evaluations = _in_window(evaluations, start_ms, end_ms, "evaluated_at_ms")
    candidate_funnel = await build_candidate_funnel(
        repository,
        window_hours=window_hours,
        as_of_ms=end_ms,
        strategy_id=strategy_id,
        asset=asset,
        limit=min(limit, 20_000),
    )
    specs = await _list_rows(repository, "list_strategy_specs", limit=1000)
    specs_by_id = {str(row.get("strategy_id") or ""): row for row in specs}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for evaluation in evaluations:
        sid = str(evaluation.get("strategy_id") or "unknown")
        candidate_asset = str(evaluation.get("asset") or "UNKNOWN").upper()
        key = (sid, candidate_asset)
        group = groups.setdefault(
            key,
            {
                "strategy_id": sid,
                "strategy_family": str(evaluation.get("strategy_family") or "unknown"),
                "asset": candidate_asset,
                "evaluation_count": 0,
                "selected_count": 0,
                "feature_ready_count": 0,
                "triggered_evaluation_count": 0,
                "generated_candidate_count": 0,
                "selection_reason_counts": Counter(),
                "generation_outcome_counts": Counter(),
                "no_candidate_reason_counts": Counter(),
                "stale_feature_counts": Counter(),
                "missing_feature_counts": Counter(),
            },
        )
        group["evaluation_count"] += 1
        selected = str(evaluation.get("selection_status") or "") == "selected"
        if selected:
            group["selected_count"] += 1
        feature_ready = not (evaluation.get("missing_features") or []) and not (evaluation.get("stale_features") or [])
        if feature_ready:
            group["feature_ready_count"] += 1
        if bool(evaluation.get("trigger_fired")):
            group["triggered_evaluation_count"] += 1
        group["generated_candidate_count"] += int(evaluation.get("candidate_count") or 0)
        group["selection_reason_counts"][str(evaluation.get("selection_reason") or "unknown")] += 1
        outcome = str(evaluation.get("generation_outcome") or "unknown")
        group["generation_outcome_counts"][outcome] += 1
        if not bool(evaluation.get("trigger_fired")):
            group["no_candidate_reason_counts"].update(str(item) for item in evaluation.get("reason_codes") or [])
        group["stale_feature_counts"].update(str(item) for item in evaluation.get("stale_features") or [])
        group["missing_feature_counts"].update(str(item) for item in evaluation.get("missing_features") or [])
    candidate_groups = {
        (str(row.get("strategy_id") or "unknown"), str(row.get("asset") or "UNKNOWN").upper()): row
        for row in candidate_funnel.get("groups") or []
    }
    output_groups: list[dict[str, Any]] = []
    for key, group in groups.items():
        candidate_group = candidate_groups.get(key) or {}
        spec = specs_by_id.get(key[0]) or {}
        metadata = _dict(spec.get("metadata"))
        output_groups.append(
            {
                **{name: value for name, value in group.items() if not isinstance(value, Counter)},
                "paper_eligible": bool(metadata.get("paper_eligible", True)) and str(metadata.get("activation_scope") or "paper_shadow") != "shadow_only",
                "counts_for_breadth": bool(spec.get("counts_for_breadth", True)),
                "selection_reason_counts": dict(group["selection_reason_counts"]),
                "generation_outcome_counts": dict(group["generation_outcome_counts"]),
                "no_candidate_reason_counts": dict(group["no_candidate_reason_counts"]),
                "stale_feature_counts": dict(group["stale_feature_counts"]),
                "missing_feature_counts": dict(group["missing_feature_counts"]),
                "candidate_stage_counts": candidate_group.get("stage_counts") or {},
                "candidate_first_failure_counts": candidate_group.get("first_failure_counts") or {},
                "candidate_reason_counts": candidate_group.get("reason_counts") or {},
            }
        )
    active_candidates = []
    for row in candidate_funnel.get("items") or []:
        spec = specs_by_id.get(str(row.get("strategy_id") or "")) or {}
        metadata = _dict(spec.get("metadata"))
        paper_eligible = bool(metadata.get("paper_eligible", True)) and str(metadata.get("activation_scope") or "paper_shadow") != "shadow_only"
        if bool(spec.get("counts_for_breadth", True)) and paper_eligible and str(row.get("side") or "") != "flat":
            active_candidates.append(row)
    active_strategy_ids = {str(row.get("strategy_id") or "unknown") for row in active_candidates}
    active_families = {str(row.get("strategy_family") or "unknown") for row in active_candidates}
    return {
        "generated_at_ms": _now_ms(),
        "window": {"hours": max(1, int(window_hours)), "start_ms": start_ms, "end_ms": end_ms},
        "sample_limit": limit,
        "sample_limit_reached": len(evaluations) >= limit,
        "grain": "engine_run_id_x_asset_x_strategy_id",
        "activation_telemetry_available": bool(evaluations),
        "evaluation_count": len(evaluations),
        "active_strategy_count": len(active_strategy_ids),
        "active_strategy_family_count": len(active_families),
        "active_strategy_ids": sorted(active_strategy_ids),
        "active_strategy_families": sorted(active_families),
        "requirements": {"strategy_count": 5, "strategy_family_count": 3},
        "groups": sorted(output_groups, key=lambda item: (item["strategy_id"], item["asset"])),
        "candidate_funnel": {key: value for key, value in candidate_funnel.items() if key != "items"},
        "methodology": {
            "breadth_authority": "strategy_specs",
            "feature_freshness": "report_only",
            "historical_gap_behavior": "unavailable_not_zero",
        },
    }
