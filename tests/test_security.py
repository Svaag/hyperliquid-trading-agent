from __future__ import annotations

from hyperliquid_trading_agent.app.newswire.schemas import NewswireAssessment
from hyperliquid_trading_agent.app.security import is_secret_field_name, redact_secrets


def test_secret_redaction_preserves_ticker_keys_in_symbol_reason_maps() -> None:
    assessment = {
        "decision_id": "decision_1",
        "story_id": "story_1",
        "assessed_at_ms": 1,
        "symbol_match_reasons": {
            "KEYS": ["trusted_source_symbol"],
            "WKEY": ["headline_alias"],
        },
    }

    redacted = redact_secrets(assessment)

    assert redacted["symbol_match_reasons"] == assessment["symbol_match_reasons"]
    assert NewswireAssessment.model_validate(redacted).matched_symbols == []


def test_secret_redaction_still_covers_credential_shaped_fields() -> None:
    payload = {
        "api_key": "one",
        "apiKey": "two",
        "private-key": "three",
        "secretToken": "four",
        "AWS_SECRET_ACCESS_KEY": "five",
        "password": "six",
        "KEYS": "ticker",
        "WKEY": "wrapped ticker",
        "monkey": "animal",
        "keynote": "presentation",
    }

    redacted = redact_secrets(payload)

    for name in (
        "api_key",
        "apiKey",
        "private-key",
        "secretToken",
        "AWS_SECRET_ACCESS_KEY",
        "password",
    ):
        assert redacted[name] == "[REDACTED]"
        assert is_secret_field_name(name) is True
    assert redacted["KEYS"] == "ticker"
    assert redacted["WKEY"] == "wrapped ticker"
    assert redacted["monkey"] == "animal"
    assert redacted["keynote"] == "presentation"
