from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import Hip4CapabilityProbeService, build_capability_probe

FIXTURES = Path("tests/fixtures/hip4")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_probe_detects_questions_and_quote_token() -> None:
    probe = build_capability_probe(_load("outcome_meta_with_questions.json"), settings=Settings(environment="test"), probed_at_ms=1)

    assert probe.outcome_meta_available is True
    assert probe.supports_outcomes is True
    assert probe.supports_questions is True
    assert probe.supports_question_fields is True
    assert probe.supports_quote_token is True
    assert probe.quote_tokens_seen == ["USDC"]
    assert probe.supports_abstract_native_mechanics is True
    assert probe.supports_user_outcome_action_json is False
    assert probe.supports_question_mechanics is True
    assert probe.supports_manual_ticket_export is True
    assert probe.supports_outcome_meta_ws is False
    assert "outcome_meta_ws_unconfirmed_rest_polling" in probe.degraded_reasons


def test_probe_degrades_to_binary_only_when_questions_missing() -> None:
    probe = build_capability_probe(_load("outcome_meta_outcomes_only.json"), settings=Settings(environment="test"), probed_at_ms=1)

    assert probe.supports_outcomes is True
    assert probe.supports_questions is False
    assert probe.supports_question_mechanics is False
    assert "questions_missing_binary_only" in probe.degraded_reasons


def test_probe_disables_action_modeling_when_quote_token_missing() -> None:
    probe = build_capability_probe(_load("outcome_meta_missing_quote_token.json"), settings=Settings(environment="test"), probed_at_ms=1)

    assert probe.supports_quote_token is False
    assert probe.supports_abstract_native_mechanics is False
    assert probe.supports_native_action_modeling is False
    assert probe.supports_manual_ticket_export is False
    assert "quote_token_missing" in probe.degraded_reasons


def test_docs_testnet_only_disables_mainnet_mechanics() -> None:
    settings = Settings(environment="test", hyperliquid_network="mainnet", hip4_docs_scope_status="testnet_only")

    probe = build_capability_probe(_load("outcome_meta_with_questions.json"), settings=settings, probed_at_ms=1)

    assert probe.docs_scope_status == "testnet_only"
    assert probe.supports_abstract_native_mechanics is False
    assert probe.supports_manual_ticket_export is False
    assert "hip4_docs_mark_testnet_only" in probe.degraded_reasons


@pytest.mark.asyncio
async def test_outcome_meta_ws_probe_timeout_degrades_to_rest_polling() -> None:
    class Client:
        async def outcome_meta(self):
            return _load("outcome_meta_with_questions.json")

    class Worker:
        async def subscribe(self, spec, callback):
            return "sub"

        async def unsubscribe(self, sub_id):
            return None

    settings = Settings(environment="test", hip4_probe_outcome_meta_ws=True, hip4_outcome_meta_ws_probe_timeout_seconds=0.001)

    probe = await Hip4CapabilityProbeService(settings=settings, hip4_client=Client(), ws_worker=Worker()).probe()

    assert probe.supports_outcome_meta_ws is False
    assert probe.outcome_meta_ws_status == "unconfirmed"


@pytest.mark.asyncio
async def test_probe_unavailable_outcome_meta_is_degraded() -> None:
    class FailingClient:
        async def outcome_meta(self):
            raise RuntimeError("boom")

    probe = await Hip4CapabilityProbeService(settings=Settings(environment="test"), hip4_client=FailingClient()).probe()

    assert probe.outcome_meta_available is False
    assert "outcome_meta_unavailable" in probe.degraded_reasons
