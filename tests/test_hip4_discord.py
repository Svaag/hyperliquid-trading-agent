from __future__ import annotations

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import build_capability_probe
from hyperliquid_trading_agent.app.hip4.discord import format_hip4_digest


def test_hip4_digest_includes_degraded_status_and_rejects() -> None:
    probe = build_capability_probe({"outcomes": []}, settings=Settings(environment="test"), probed_at_ms=1)

    digest = format_hip4_digest(
        status={"enabled": True, "status": "degraded", "registry": {"stale": True}},
        capabilities=probe,
        candidates=[],
        rejects=[{"code": "stale_book", "message": "book stale"}],
        paper={"balances": {"USDC": "100"}},
    )

    assert "degraded" in digest
    assert "stale" in digest.lower()
    assert "stale_book" in digest
    assert "No accepted HIP-4 candidates" in digest


def test_hip4_digest_handles_cycle_summary_loop_without_none_values() -> None:
    digest = format_hip4_digest(
        status={"enabled": True, "status": "ok", "registry": {"stale": False}},
        capabilities=None,
        candidates=[],
        rejects=[],
        paper={"balances": {"USDC": "100"}},
        reason="proactive_cycle",
        loop={"cycle_id": "cycle_1", "status": "ok", "candidate_count": 0},
    )

    assert "running=`n/a`" in digest
    assert "cycles=`n/a`" in digest
    assert "last_status=`ok`" in digest
    assert "None" not in digest
