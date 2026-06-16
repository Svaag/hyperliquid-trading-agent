from __future__ import annotations

import html
import time
from collections import Counter, defaultdict
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 4) if denominator else 0.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _strategy_of(item: dict[str, Any]) -> str:
    return str(item.get("strategy_id") or "unknown")


async def build_engine_validation_report(repository: Any, *, limit: int = 500) -> dict[str, Any]:
    """Build an operator-facing validation summary from read-only engine ledgers."""

    candidates = await repository.list_alpha_candidates(limit=limit)
    ev_estimates = await repository.list_ev_estimates(limit=limit)
    allocations = await repository.list_allocation_decisions(limit=limit)
    intents = await repository.list_order_intents(limit=limit)
    reports = await repository.list_execution_reports(limit=limit)
    positions = await repository.list_position_theses(limit=limit)
    risk_rejects = await repository.list_risk_gateway_decisions(limit=limit, decision="reject")
    pnl_records = await repository.list_pnl_attribution(limit=limit)

    candidates_by_id = {str(item.get("candidate_id")): item for item in candidates if item.get("candidate_id")}
    intents_by_id = {str(item.get("intent_id")): item for item in intents if item.get("intent_id")}
    reports_by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        reports_by_intent[str(report.get("intent_id") or "")].append(report)

    by_strategy: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "candidate_count": 0,
            "allocated_count": 0,
            "shadow_intent_count": 0,
            "paper_intent_count": 0,
            "sim_report_count": 0,
            "filled_report_count": 0,
            "avg_net_ev_bps": 0.0,
            "avg_risk_adjusted_utility": 0.0,
            "avg_slippage_bps": 0.0,
            "fees_usd": 0.0,
            "total_pnl_usd": 0.0,
            "alpha_pnl_usd": 0.0,
            "execution_pnl_usd": 0.0,
            "positions_open": 0,
        }
    )

    ev_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ev_values_by_strategy: dict[str, list[float]] = defaultdict(list)
    utility_values_by_strategy: dict[str, list[float]] = defaultdict(list)
    calibration_buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "avg_net_ev_bps": 0.0, "avg_uncertainty": 0.0, "avg_realized_pnl_usd": 0.0})
    bucket_ev: dict[str, list[float]] = defaultdict(list)
    bucket_uncertainty: dict[str, list[float]] = defaultdict(list)
    bucket_realized: dict[str, list[float]] = defaultdict(list)

    realized_by_candidate: dict[str, list[float]] = defaultdict(list)
    for item in pnl_records:
        if item.get("candidate_id"):
            realized_by_candidate[str(item["candidate_id"])].append(_f(item.get("total_pnl_usd")))

    for candidate in candidates:
        strategy = _strategy_of(candidate)
        by_strategy[strategy]["candidate_count"] += 1

    for estimate in ev_estimates:
        candidate_id = str(estimate.get("candidate_id") or "")
        candidate = candidates_by_id.get(candidate_id, {})
        strategy = _strategy_of(candidate) if candidate else "unknown"
        ev_by_candidate[candidate_id].append(estimate)
        ev_values_by_strategy[strategy].append(_f(estimate.get("net_ev_bps")))
        utility_values_by_strategy[strategy].append(_f(estimate.get("risk_adjusted_utility")))
        bucket = str(estimate.get("calibration_bucket") or "unknown")
        bucket_ev[bucket].append(_f(estimate.get("net_ev_bps")))
        bucket_uncertainty[bucket].append(_f(estimate.get("uncertainty")))
        if realized_by_candidate.get(candidate_id):
            bucket_realized[bucket].extend(realized_by_candidate[candidate_id])

    for strategy, values in ev_values_by_strategy.items():
        by_strategy[strategy]["avg_net_ev_bps"] = round(_avg(values), 4)
        by_strategy[strategy]["avg_risk_adjusted_utility"] = round(_avg(utility_values_by_strategy[strategy]), 4)

    for bucket, values in bucket_ev.items():
        calibration_buckets[bucket] = {
            "count": len(values),
            "avg_net_ev_bps": round(_avg(values), 4),
            "avg_uncertainty": round(_avg(bucket_uncertainty[bucket]), 4),
            "avg_realized_pnl_usd": round(_avg(bucket_realized[bucket]), 4),
            "realized_sample_count": len(bucket_realized[bucket]),
        }

    for allocation in allocations:
        candidate = candidates_by_id.get(str(allocation.get("candidate_id") or ""), {})
        strategy = _strategy_of(candidate) if candidate else "unknown"
        if allocation.get("status") in {"allocate", "reduce", "require_debate"}:
            by_strategy[strategy]["allocated_count"] += 1

    for intent in intents:
        strategy = _strategy_of(intent)
        if intent.get("execution_mode") == "shadow":
            by_strategy[strategy]["shadow_intent_count"] += 1
        elif intent.get("execution_mode") == "paper":
            by_strategy[strategy]["paper_intent_count"] += 1

    slippage_by_strategy: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        intent = intents_by_id.get(str(report.get("intent_id") or ""), {})
        strategy = _strategy_of(intent) if intent else "unknown"
        by_strategy[strategy]["sim_report_count"] += 1
        if report.get("status") == "filled":
            by_strategy[strategy]["filled_report_count"] += 1
        by_strategy[strategy]["fees_usd"] = round(by_strategy[strategy]["fees_usd"] + _f(report.get("fees_usd")), 4)
        slippage_by_strategy[strategy].append(_f(report.get("slippage_bps")))
    for strategy, values in slippage_by_strategy.items():
        by_strategy[strategy]["avg_slippage_bps"] = round(_avg(values), 4)

    for position in positions:
        strategy = _strategy_of(position)
        if position.get("position_state") == "open":
            by_strategy[strategy]["positions_open"] += 1

    for pnl in pnl_records:
        strategy = _strategy_of(pnl)
        by_strategy[strategy]["total_pnl_usd"] = round(by_strategy[strategy]["total_pnl_usd"] + _f(pnl.get("total_pnl_usd")), 4)
        by_strategy[strategy]["alpha_pnl_usd"] = round(by_strategy[strategy]["alpha_pnl_usd"] + _f(pnl.get("alpha_pnl_usd")), 4)
        by_strategy[strategy]["execution_pnl_usd"] = round(by_strategy[strategy]["execution_pnl_usd"] + _f(pnl.get("execution_pnl_usd")), 4)

    candidate_status = Counter(str(item.get("status") or "unknown") for item in candidates)
    allocation_status = Counter(str(item.get("status") or "unknown") for item in allocations)
    risk_violation_counts: Counter[str] = Counter()
    for reject in risk_rejects:
        for violation in reject.get("violations") or []:
            risk_violation_counts[str(violation)] += 1

    total_candidates = len(candidates)
    total_allocated = sum(1 for item in allocations if item.get("status") in {"allocate", "reduce", "require_debate"})
    total_shadow_intents = sum(1 for item in intents if item.get("execution_mode") == "shadow")
    total_paper_intents = sum(1 for item in intents if item.get("execution_mode") == "paper")

    return {
        "generated_at_ms": _now_ms(),
        "sample_limit": limit,
        "summary": {
            "candidate_count": total_candidates,
            "ev_estimate_count": len(ev_estimates),
            "allocation_count": len(allocations),
            "allocated_count": total_allocated,
            "allocation_rate_pct": _pct(total_allocated, len(allocations)),
            "shadow_intent_count": total_shadow_intents,
            "paper_intent_count": total_paper_intents,
            "execution_report_count": len(reports),
            "open_position_count": sum(1 for item in positions if item.get("position_state") == "open"),
            "risk_reject_count": len(risk_rejects),
            "pnl_attribution_count": len(pnl_records),
        },
        "shadow_candidates": {
            "status_counts": dict(candidate_status),
            "asset_counts": dict(Counter(str(item.get("asset") or "unknown") for item in candidates)),
            "side_counts": dict(Counter(str(item.get("side") or "unknown") for item in candidates)),
            "latest": candidates[:10],
        },
        "ev_calibration": {
            "bucket_summary": dict(calibration_buckets),
            "candidate_estimate_coverage_pct": _pct(len(ev_by_candidate), total_candidates),
        },
        "risk_rejects": {
            "count": len(risk_rejects),
            "violation_counts": dict(risk_violation_counts),
            "latest": risk_rejects[:10],
        },
        "execution_simulations": {
            "intent_count": len(intents),
            "report_count": len(reports),
            "shadow_intent_count": total_shadow_intents,
            "paper_intent_count": total_paper_intents,
            "status_counts": dict(Counter(str(item.get("status") or "unknown") for item in reports)),
            "avg_slippage_bps": round(_avg([_f(item.get("slippage_bps")) for item in reports]), 4),
            "fees_usd": round(sum(_f(item.get("fees_usd")) for item in reports), 4),
        },
        "pnl_attribution_by_strategy": {
            strategy: {
                "total_pnl_usd": values["total_pnl_usd"],
                "alpha_pnl_usd": values["alpha_pnl_usd"],
                "execution_pnl_usd": values["execution_pnl_usd"],
                "fees_usd": values["fees_usd"],
                "positions_open": values["positions_open"],
            }
            for strategy, values in by_strategy.items()
        },
        "by_strategy": dict(sorted(by_strategy.items())),
        "allocation_status_counts": dict(allocation_status),
    }


def render_engine_validation_dashboard(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    by_strategy = report.get("by_strategy") or {}
    risk = report.get("risk_rejects") or {}
    ev = report.get("ev_calibration") or {}
    execution = report.get("execution_simulations") or {}

    def cell(value: Any) -> str:
        return f"<td>{html.escape(str(value))}</td>"

    strategy_rows = "".join(
        "<tr>"
        + cell(strategy)
        + cell(values.get("candidate_count", 0))
        + cell(values.get("allocated_count", 0))
        + cell(values.get("shadow_intent_count", 0))
        + cell(values.get("avg_net_ev_bps", 0))
        + cell(values.get("avg_slippage_bps", 0))
        + cell(values.get("total_pnl_usd", 0))
        + "</tr>"
        for strategy, values in by_strategy.items()
    )
    bucket_rows = "".join(
        "<tr>" + cell(bucket) + cell(values.get("count", 0)) + cell(values.get("avg_net_ev_bps", 0)) + cell(values.get("avg_uncertainty", 0)) + cell(values.get("avg_realized_pnl_usd", 0)) + "</tr>"
        for bucket, values in (ev.get("bucket_summary") or {}).items()
    )
    risk_rows = "".join("<tr>" + cell(name) + cell(count) + "</tr>" for name, count in (risk.get("violation_counts") or {}).items())

    return f"""
<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Engine Validation Dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #17202a; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
.card {{ border: 1px solid #d8dee4; border-radius: 10px; padding: 12px; background: #f6f8fa; }}
.card b {{ display: block; font-size: 24px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; }}
th, td {{ border: 1px solid #d8dee4; padding: 8px; text-align: left; }}
th {{ background: #f6f8fa; }}
small {{ color: #57606a; }}
</style></head><body>
<h1>Engine Validation Dashboard</h1>
<small>Generated at ms: {html.escape(str(report.get('generated_at_ms')))}</small>
<div class=\"cards\">
  <div class=\"card\">Candidates<b>{summary.get('candidate_count', 0)}</b></div>
  <div class=\"card\">EV estimates<b>{summary.get('ev_estimate_count', 0)}</b></div>
  <div class=\"card\">Allocated<b>{summary.get('allocated_count', 0)}</b></div>
  <div class=\"card\">Shadow intents<b>{summary.get('shadow_intent_count', 0)}</b></div>
  <div class=\"card\">Risk rejects<b>{summary.get('risk_reject_count', 0)}</b></div>
  <div class=\"card\">Open positions<b>{summary.get('open_position_count', 0)}</b></div>
</div>
<h2>By strategy</h2>
<table><thead><tr><th>Strategy</th><th>Candidates</th><th>Allocated</th><th>Shadow intents</th><th>Avg EV bps</th><th>Avg slippage bps</th><th>Total PnL USD</th></tr></thead><tbody>{strategy_rows}</tbody></table>
<h2>EV calibration buckets</h2>
<table><thead><tr><th>Bucket</th><th>Count</th><th>Avg EV bps</th><th>Avg uncertainty</th><th>Avg realized PnL USD</th></tr></thead><tbody>{bucket_rows}</tbody></table>
<h2>Risk rejects</h2>
<table><thead><tr><th>Violation</th><th>Count</th></tr></thead><tbody>{risk_rows}</tbody></table>
<h2>Execution simulations</h2>
<pre>{html.escape(str(execution))}</pre>
</body></html>
"""
