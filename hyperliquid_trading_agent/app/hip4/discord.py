from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.hip4.schemas import Hip4Candidate, Hip4CapabilityProbe


def format_hip4_digest(
    *,
    status: dict[str, Any],
    capabilities: Hip4CapabilityProbe | None,
    candidates: list[Hip4Candidate],
    rejects: list[dict[str, Any]],
    paper: dict[str, Any],
    reason: str = "digest",
    executions: list[dict[str, Any]] | None = None,
    loop: dict[str, Any] | None = None,
    learning: dict[str, Any] | None = None,
) -> str:
    lines = ["**HIP-4 Outcome Markets Digest**"]
    lines.append(f"Reason: `{reason}`")
    lines.append(f"Status: `{status.get('status', 'unknown')}` | enabled=`{status.get('enabled')}`")
    stale = (status.get("registry") or {}).get("stale")
    if stale:
        lines.append("⚠️ Registry metadata is stale; scanner/paper candidates are not executable.")
    if capabilities is not None and capabilities.degraded_reasons:
        lines.append("Capabilities degraded: " + ", ".join(f"`{item}`" for item in capabilities.degraded_reasons[:8]))
    if rejects:
        lines.append("Reject reasons:")
        for item in rejects[:5]:
            lines.append(f"- `{item.get('code', 'unknown')}` {item.get('message', '')}")
    if loop:
        last_summary = loop.get("last_summary") if isinstance(loop.get("last_summary"), dict) else loop
        running = loop.get("running", "n/a")
        cycles = loop.get("cycle_count", "n/a")
        lines.append(
            "Loop: "
            f"running=`{running}` cycles=`{cycles}` "
            f"last_status=`{(last_summary or {}).get('status', 'unknown')}`"
        )
    if candidates:
        lines.append("Top candidates:")
        for candidate in sorted(candidates, key=lambda item: item.expected_net_edge_usd, reverse=True)[:5]:
            lines.append(
                f"- `{candidate.strategy_type}` edge={candidate.expected_net_edge_usd} {candidate.quote_token or 'quote'} "
                f"({candidate.expected_net_edge_bps} bps), status=`{candidate.status}`"
            )
    else:
        lines.append("No accepted HIP-4 candidates currently available.")
    if executions:
        lines.append("Paper executions:")
        for item in executions[:5]:
            lines.append(f"- `{item.get('status')}` `{item.get('strategy_type')}` candidate=`{item.get('candidate_id')}`")
    balances = (paper or {}).get("balances") or {}
    if balances:
        lines.append("Paper balances: " + ", ".join(f"{key}={value}" for key, value in list(balances.items())[:8]))
    if paper:
        lines.append(
            "Paper PnL: "
            f"realized={paper.get('realized_pnl', '0')} "
            f"unrealized={paper.get('unrealized_pnl', '0')} "
            f"fees={paper.get('modeled_fees', '0')}"
        )
    recommendations = (learning or {}).get("recommendations") or []
    if recommendations:
        lines.append("Learning notes:")
        for item in recommendations[:3]:
            lines.append(f"- {item}")
    return "\n".join(lines)
