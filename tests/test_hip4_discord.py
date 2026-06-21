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
