from __future__ import annotations

from hyperliquid_trading_agent.app.agent.high_stakes.schemas import JudgeDecision, TradeProposal


def format_trade_proposal(proposal: TradeProposal, judge: JudgeDecision | None = None) -> str:
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
    lines.extend(f"- {item}" for item in proposal.warnings[:8])
    return "\n".join(str(line) for line in lines if line is not None)[:7000]
