from __future__ import annotations

import re

from hyperliquid_trading_agent.app.agent.high_stakes.schemas import JudgeDecision, TradeProposal
from hyperliquid_trading_agent.app.tracking.levels import summarize_tracking_plan
from hyperliquid_trading_agent.app.tracking.schemas import PositionTrackingPlan


def format_trade_proposal(proposal: TradeProposal, judge: JudgeDecision | None = None) -> str:
    if _should_use_compact_position_review(proposal, judge):
        return _format_compact_position_review(proposal, judge)

    coverage = judge.data_coverage if judge and judge.data_coverage else None
    accepted = list(judge.accepted_critiques if judge else [])
    deferred = list(judge.deferred_critiques if judge else [])
    adversary = proposal.role_summaries.get("adversary", "No adversary summary available.")
    treasury = proposal.role_summaries.get("treasury", "No account-specific treasury review was activated or available.")
    confidence = judge.confidence if judge else None
    lines = [
        "Decision:",
        proposal.judge_summary or (judge.summary if judge else "No judge summary available."),
        "",
        "Status:",
        f"{proposal.status}" + (f" | confidence={confidence:.0%}" if confidence is not None else ""),
        "",
        "Endpoint coverage:",
    ]
    if coverage:
        lines.append(f"- {coverage.coverage_score:.0%} coverage: {len(coverage.used_endpoints)}/{len(coverage.required_endpoints)} required endpoints used.")
        if coverage.used_endpoints:
            lines.append(f"- Used: {', '.join(coverage.used_endpoints[:12])}")
        if coverage.missing_endpoints:
            lines.append(f"- Missing/failed: {', '.join((coverage.missing_endpoints + coverage.stale_or_failed_endpoints)[:12])}")
    elif proposal.tool_summary:
        lines.extend(f"- {item}" for item in proposal.tool_summary[:10])
    else:
        lines.append("- No endpoint coverage summary available.")

    lines.extend(["", "Accepted critiques:"])
    lines.extend(f"- {item}" for item in accepted[:8]) if accepted else lines.append("- None recorded as accepted by Judge.")
    lines.extend(["", "Deferred critiques:"])
    lines.extend(f"- {item}" for item in deferred[:8]) if deferred else lines.append("- None recorded as deferred by Judge.")

    lines.extend(["", "Setup:"])
    lines.append(f"- Coin/side: {proposal.coin or 'unknown'} {proposal.side or 'unknown'}")
    lines.append(f"- Entry / stop / take-profit: {proposal.entry} / {proposal.stop} / {proposal.take_profit}")
    if proposal.timeframe:
        lines.append(f"- Timeframe: {proposal.timeframe}")
    if proposal.thesis:
        lines.append(f"- Thesis: {proposal.thesis}")
    lines.append(f"- Invalidation: {proposal.invalidation or 'missing'}")

    lines.extend(["", "Rationale / tape read:"])
    if proposal.rationale:
        lines.extend(f"- {item}" for item in proposal.rationale[:8])
    else:
        lines.append("- No rationale was produced.")

    lines.extend(["", "Risk:"])
    if proposal.risk_usd is not None:
        lines.append(f"- Max planned loss to stop: ${proposal.risk_usd} ({proposal.risk_pct}% configured risk).")
    if proposal.size_units is not None or proposal.notional_usd is not None:
        lines.append(f"- Size/notional: {proposal.size_units} units, ${proposal.notional_usd}")
    lines.extend(f"- {item}" for item in proposal.risks[:8])

    lines.extend(["", "Execution readiness:"])
    if proposal.checklist:
        lines.extend(f"- {item}" for item in proposal.checklist[:10])
    else:
        lines.append("- No execution-readiness checklist was produced.")

    lines.extend(["", "Treasury/account:", f"- {treasury}"])
    lines.extend(["", "Adversary objections:", f"- {adversary}"])

    lines.extend(["", "What would change the decision:"])
    if judge and judge.data_requests:
        lines.extend(f"- More data requested: {item.endpoint_family} ({item.reason})" for item in judge.data_requests[:5])
    elif deferred:
        lines.extend(f"- Resolve: {item}" for item in deferred[:5])
    else:
        lines.append("- Fresh price, funding, liquidity, account exposure, or news data that contradicts the thesis.")

    lines.extend(["", "No-execution caveat:"])
    lines.append("- No trade was placed. This is a non-executing proposal/review.")
    lines.append("- Autonomous/live exchange execution is disabled; exchange_actions is intentionally empty.")
    lines.extend(f"- {item}" for item in _public_warnings(proposal.warnings)[:8])
    return "\n".join(str(line) for line in lines if line is not None)[:7000]


def _should_use_compact_position_review(proposal: TradeProposal, judge: JudgeDecision | None) -> bool:
    model_fallback = any("model_fallback" in item or "Model fallback" in item for item in proposal.warnings + proposal.risks)
    judge_fallback = bool(judge and judge.model is None)
    has_position = bool(proposal.coin and proposal.entry is not None and proposal.stop is not None)
    return has_position and (model_fallback or judge_fallback)


def _format_compact_position_review(proposal: TradeProposal, judge: JudgeDecision | None = None) -> str:
    confidence = judge.confidence if judge else 0.35
    lines = [
        f"{proposal.coin} position review — {proposal.side or 'position'} from {proposal.entry}, stop {proposal.stop}",
        "",
        "Read:",
    ]
    rationale = [item for item in proposal.rationale if "model" not in item.lower()]
    if rationale:
        lines.extend(f"- {item}" for item in rationale[:5])
    else:
        lines.append("- Live market data was gathered, but no clean edge was produced by the review.")

    tracking_levels = summarize_tracking_plan(proposal.tracking_plan)
    if tracking_levels:
        lines.extend(["", "Levels to watch:"])
        lines.extend(f"- {item}" for item in tracking_levels[:6])
        tracking_status = _tracking_status_line(proposal.tracking_plan)
        if tracking_status:
            lines.extend(["", "Live tracking:", f"- {tracking_status}"])
            lines.append('- In the thread, say "tracking status" or "stop tracking" to control it.')

    lines.extend(["", "Decision frame:"])
    lines.append("- Hold case: price respects the terminal downside/upside trigger above and confirms through the relevant reclaim/resistance/support level.")
    lines.append("- Reduce/exit case: price loses the terminal technical trigger, cannot reclaim entry before the event you care about, or liquidity thins into the open.")

    lines.extend(["", "Risk:"])
    if proposal.risk_usd is not None:
        lines.append(f"- Planned loss to stop: ${proposal.risk_usd:g}" + (f" ({proposal.risk_pct:g}% configured risk)." if proposal.risk_pct is not None else "."))
    else:
        lines.append("- No account size or risk % was supplied, so I am not estimating dollar loss or position notional.")
    lines.append(f"- Confidence: {'low' if confidence < 0.45 else 'moderate'}; this is based on live tape/structure, not a full discretionary model pass.")

    useful_checklist = [_clean_checklist_item(item) for item in proposal.checklist if _clean_checklist_item(item)]
    if useful_checklist:
        lines.extend(["", "Execution / liquidity checks:"])
        lines.extend(f"- {item}" for item in useful_checklist[:3])

    public_warnings = _public_warnings(proposal.warnings)
    if public_warnings:
        lines.extend(["", "Notes:"])
        lines.extend(f"- {item}" for item in public_warnings[:3])
    return "\n".join(str(line) for line in lines if line is not None)[:3500]


def _tracking_status_line(plan_payload: dict | None) -> str:
    if not plan_payload:
        return ""
    try:
        plan = PositionTrackingPlan.model_validate(plan_payload)
    except Exception:
        return ""
    status = str(plan.metadata.get("auto_arm_status", ""))
    if status == "armed":
        destination = "this Discord thread" if plan.discord_thread_id else "the tracking API/event log"
        return f"Armed for {len(plan.levels)} levels via allMids; alerts go to {destination}; expires in {plan.metadata.get('ttl_hours', 168)}h or on a terminal exit/stop hit."
    if status.startswith("not_armed"):
        return f"Levels were prepared but live tracking was not armed: {_human_tracking_reason(status)}."
    return "Levels are ready for live tracking."


def _human_tracking_reason(status: str) -> str:
    reason = status.split(":", 1)[1] if ":" in status else status
    return {
        "tracking_disabled": "position tracking is disabled in config",
        "auto_arm_disabled": "automatic arming is disabled in config",
        "repository_unavailable": "tracking storage is unavailable",
        "max_active_reached": "the active-tracker limit has been reached",
        "persistence_failed": "the tracker could not be saved",
        "no_tracking_service": "the tracking service is not running",
        "unknown": "unknown reason",
    }.get(reason, reason.replace("_", " "))


def _clean_checklist_item(item: str) -> str:
    lowered = item.lower()
    if lowered.startswith("manual confirmation") or "service does not sign" in lowered:
        return ""
    if lowered.startswith("confirm stop") or lowered.startswith("hyperliquid validation") or lowered.startswith("endpoint coverage"):
        return ""
    if lowered.startswith("re-check hyperliquid") or lowered.startswith("recheck hyperliquid"):
        return ""
    if item.startswith("Execution readiness:"):
        spread = _extract_float(item, "spread_bps")
        depth = _extract_float(item, "top_depth")
        slippage = _extract_float(item, "est_slippage_bps")
        parts = []
        if spread is not None:
            parts.append(f"spread ~{spread:.2f} bps")
        if depth is not None:
            parts.append(f"top depth ~${depth:,.0f}")
        if slippage is not None:
            parts.append(f"estimated slippage ~{slippage:.1f} bps")
        return "Liquidity: " + ", ".join(parts) + "." if parts else ""
    return item


def _extract_float(text: str, key: str) -> float | None:
    match = re.search(rf"{re.escape(key)}=\$?(?P<value>-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group("value"))
    except ValueError:
        return None


def _public_warnings(warnings: list[str]) -> list[str]:
    hidden_terms = ("model_fallback", "judge_model_fallback", "TimeoutError", "deterministic")
    return [warning for warning in warnings if not any(term in warning for term in hidden_terms)]
