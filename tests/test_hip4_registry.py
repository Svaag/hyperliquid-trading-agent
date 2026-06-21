from __future__ import annotations

import json
from pathlib import Path

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.registry import Hip4Registry

FIXTURES = Path("tests/fixtures/hip4")


def test_registry_parses_outcomes_questions_and_preserves_raw() -> None:
    payload = json.loads((FIXTURES / "outcome_meta_with_questions.json").read_text())
    registry = Hip4Registry(settings=Settings(environment="test"))

    registry.load_raw(payload, observed_at_ms=123)

    assert registry.last_refresh_at_ms == 123
    assert registry.raw_payload == payload
    assert registry.raw_schema_hash
    assert registry.outcomes[172].quote_token == "USDC"
    assert registry.outcomes[172].raw["name"] == "Canada"
    assert registry.questions[32].fallback_outcome_id == 174
    assert registry.questions[32].outcome_ids == [174, 172, 173]
    assert registry.status()["outcome_count"] == 3
