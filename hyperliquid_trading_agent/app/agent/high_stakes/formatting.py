from __future__ import annotations

import re

from hyperliquid_trading_agent.app.agent.high_stakes.schemas import JudgeDecision, TradeProposal
from hyperliquid_trading_agent.app.tracking.levels import summarize_tracking_plan
from hyperliquid_trading_agent.app.tracking.schemas import PositionTrackingPlan


def format_trade_proposal(proposal: TradeProposal, judge: JudgeDecision | None = None) -> str:
    if _should_use_compact_position_review(proposal, judge):
        return _format_compact_position_review(proposal, judge)

    coverage = judge.data_coverage if judge and judge.data_coverage else None
    accepted = [item for item in (judge.accepted_critiques if judge else []) if _meaningful_text(item)]
    deferred = [item for item in (judge.deferred_critiques if judge else []) if _meaningful_text(item)]
    adversary = proposal.role_summaries.get("adversary", "")
    treasury = proposal.role_summaries.get("treasury", "")
    confidence = judge.confidence if judge else None
    lines = [
        f"**Decision:** {proposal.judge_summary or (judge.summary if judge else 'No judge summary available.')}",
        f"**Status:** `{proposal.status}`" + (f" | confidence={confidence:.0%}" if confidence is not None else ""),
    ]

    if coverage:
        coverage_lines = [f"{coverage.coverage_score:.0%}: {len(coverage.used_endpoints)}/{len(coverage.required_endpoints)} required endpoints used"]
        if coverage.missing_endpoints:
            coverage_lines.append(f"Missing/failed: {', '.join((coverage.missing_endpoints + coverage.stale_or_failed_endpoints)[:8])}")
        _add_section(lines, "Endpoint coverage", coverage_lines)

    participation = _format_debate_participation(proposal.debate_participation)
    _add_section(lines, "Team participation", participation)
    _add_section(lines, "Accepted critiques", accepted[:6])
    _add_section(lines, "Deferred critiques", deferred[:6])

    setup_lines = []
    if proposal.coin or proposal.side:
        setup_lines.append(f"Coin/side: {proposal.coin or 'unknown'} {proposal.side or 'unknown'}")
    if proposal.entry is not None or proposal.stop is not None or proposal.take_profit is not None:
        setup_lines.append(f"Entry / stop / take-profit: {proposal.entry} / {proposal.stop} / {proposal.take_profit}")
    if proposal.timeframe:
        setup_lines.append(f"Timeframe: {proposal.timeframe}")
    if proposal.thesis:
        setup_lines.append(f"Thesis: {proposal.thesis}")
    if proposal.invalidation and "missing" not in proposal.invalidation.lower():
        setup_lines.append(f"Invalidation: {proposal.invalidation}")
    _add_section(lines, "Setup", setup_lines)

    _add_section(lines, "Rationale", proposal.rationale[:5])

    risk_lines = []
    if proposal.risk_usd is not None:
        risk_lines.append(f"Max planned loss to stop: ${proposal.risk_usd:g}" + (f" ({proposal.risk_pct:g}% configured risk)" if proposal.risk_pct is not None else ""))
    if proposal.size_units is not None or proposal.notional_usd is not None:
        risk_lines.append(f"Size/notional: {proposal.size_units} units, ${proposal.notional_usd}")
    risk_lines.extend(_dedupe_text([item for item in proposal.risks if _meaningful_text(item)])[:6])
    _add_section(lines, "Risk", risk_lines)

    execution_lines = [_clean_checklist_item(item) for item in proposal.checklist]
    _add_section(lines, "Liquidity / execution", [item for item in execution_lines if item][:3])

    if _meaningful_text(treasury):
        _add_section(lines, "Treasury", [treasury])
    if _meaningful_text(adversary):
        _add_section(lines, "Adversary", [adversary])
    if judge and judge.data_requests:
        _add_section(lines, "Needs more data", [f"{item.endpoint_family}: {item.reason}" for item in judge.data_requests[:5]])

    note_lines = _public_warnings(proposal.warnings)[:5]
    note_lines.append("No trade was placed; live execution remains disabled.")
    _add_section(lines, "Notes", note_lines)
    return "\n".join(str(line) for line in lines if line is not None)[:2400]


def _add_section(lines: list[str], title: str, items: list[str]) -> None:
    cleaned = [str(item).strip() for item in items if _meaningful_text(str(item))]
    if not cleaned:
        return
    lines.extend(["", f"**{title}:**"])
    lines.extend(f"- {item}" for item in cleaned)


def _dedupe_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = " ".join(str(item).split()).lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(str(item))
    return out


def _meaningful_text(text: str) -> bool:
    lowered = " ".join(str(text).strip().lower().split())
    if not lowered:
        return False
    empty_markers = (
        "none recorded",
        "role not activated",
        "no adversary summary available",
        "no account-specific treasury review",
        "no execution-readiness checklist",
        "fresh price, funding, liquidity",
        "deterministic fallback adversary review",
        "model fallback",
        "model_fallback",
        "deterministic_debate_fallback",
        "deterministic_context_unavailable",
    )
    return not any(marker in lowered for marker in empty_markers)


def _should_use_compact_position_review(proposal: TradeProposal, judge: JudgeDecision | None) -> bool:
    model_fallback = any("model_fallback" in item or "Model fallback" in item for item in proposal.warnings + proposal.risks)
    judge_fallback = bool(judge and judge.model is None)
    has_position = bool(proposal.coin and proposal.entry is not None and proposal.stop is not None)
    return has_position and (bool(proposal.tracking_plan) or model_fallback or judge_fallback)


def _format_compact_position_review(proposal: TradeProposal, judge: JudgeDecision | None = None) -> str:
    confidence = judge.confidence if judge else 0.35
    lines = [
        f"**{proposal.coin} position review** — {proposal.side or 'position'} from `{proposal.entry}`, stop `{proposal.stop}`",
        "",
        "**Read:**",
    ]
    rationale = [item for item in proposal.rationale if "model" not in item.lower()]
    if rationale:
        lines.extend(f"- {item}" for item in rationale[:5])
    else:
        lines.append(f"- Parsed setup: {proposal.coin} {proposal.side or 'position'} from {proposal.entry:g} with hard stop {proposal.stop:g}; no model-backed edge survived this pass.")

    tracking_levels = summarize_tracking_plan(proposal.tracking_plan)
    if tracking_levels:
        lines.extend(["", "**Levels to watch:**"])
        lines.extend(f"- {item}" for item in tracking_levels[:6])
        tracking_status = _tracking_status_line(proposal.tracking_plan)
        if tracking_status:
            lines.extend(["", "**Live tracking:**", f"- {tracking_status}"])
            lines.append('- In the thread, say "tracking status" or "stop tracking" to control it.')

    participation = _format_debate_participation(proposal.debate_participation)
    if participation:
        lines.extend(["", "**Team participation:**"])
        lines.extend(participation)

    lines.extend(["", "**Risk:**"])
    if proposal.risk_usd is not None:
        lines.append(f"- Planned loss to stop: ${proposal.risk_usd:g}" + (f" ({proposal.risk_pct:g}% configured risk)." if proposal.risk_pct is not None else "."))
    else:
        lines.append("- No account size or risk % was supplied, so I am not estimating dollar loss or position notional.")
    lines.append(f"- Confidence: {'low' if confidence < 0.45 else 'moderate'}; this is based on live tape/structure, not a full discretionary model pass.")

    useful_checklist = [_clean_checklist_item(item) for item in proposal.checklist if _clean_checklist_item(item)]
    if useful_checklist:
        lines.extend(["", "**Liquidity / execution:**"])
        lines.extend(f"- {item}" for item in useful_checklist[:2])

    public_warnings = _public_warnings(proposal.warnings)
    if public_warnings:
        lines.extend(["", "**Notes:**"])
        lines.extend(f"- {item}" for item in public_warnings[:2])
    return "\n".join(str(line) for line in lines if line is not None)[:1900]


def _format_debate_participation(participation: list[dict]) -> list[str]:
    if not participation:
        return []
    model_backed = [item for item in participation if item.get("status") == "ok" and item.get("model")]
    fallback = [item for item in participation if item.get("status") == "fallback"]
    skipped = [item for item in participation if item.get("status") in {"abstain", "not_run"}]
    errored = [item for item in participation if item.get("status") == "error"]
    lines: list[str] = []
    lines.append("Models: " + (", ".join(_role_detail(item, include_model=True) for item in model_backed) if model_backed else "none") + ".")
    if fallback:
        lines.append("Fallback: " + ", ".join(_role_detail(item, include_reason=True) for item in fallback) + ".")
    if skipped:
        lines.append("Skipped: " + ", ".join(_role_name(item) for item in skipped) + ".")
    if errored:
        lines.append("Errors: " + ", ".join(_role_detail(item, include_reason=True) for item in errored) + ".")
    return lines


def _role_detail(item: dict, *, include_model: bool = False, include_reason: bool = False) -> str:
    bits = []
    if include_model and item.get("model"):
        bits.append(_short_model(str(item.get("model"))))
    if include_reason and item.get("fallback_reason"):
        bits.append(_short_reason(str(item.get("fallback_reason"))))
    latency = item.get("latency_ms")
    if isinstance(latency, (int, float)) and latency > 0:
        bits.append(f"{latency / 1000:.1f}s")
    return _role_name(item) + (f" ({', '.join(bits)})" if bits else "")


def _short_model(model: str) -> str:
    cleaned = model.replace("openrouter:", "")
    return cleaned if len(cleaned) <= 28 else cleaned[:27].rstrip("-/") + "…"


def _short_reason(reason: str) -> str:
    cleaned = " ".join(reason.replace("openrouter:", "").split())
    cleaned = cleaned.replace("All configured model attempts failed or lacked credentials:", "").strip()
    if cleaned == "invalid_structured_json":
        cleaned = "malformed structured output"
    return cleaned if len(cleaned) <= 70 else cleaned[:69].rstrip(" ;,") + "…"


def _role_name(item: dict) -> str:
    return str(item.get("role", "unknown")).replace("_", " ").title()


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
    hidden_terms = ("model_fallback", "judge_model_fallback", "timeouterror", "deterministic")
    return [warning for warning in warnings if not any(term in warning.lower() for term in hidden_terms)]
