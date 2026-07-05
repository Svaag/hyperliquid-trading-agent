from __future__ import annotations

import pytest

from hyperliquid_trading_agent.app.hip4.ids import (
    OutcomeAssetId,
    balance_token,
    coin,
    encoding,
    exchange_asset_id,
    outcome_asset_id_from_identifier,
    parse_balance_token,
    parse_coin,
    parse_identifier,
)


def test_outcome_asset_encoding_helpers() -> None:
    asset = OutcomeAssetId(outcome_id=172, side=0)

    assert encoding(172, 0) == 1720
    assert coin(172, 0) == "#1720"
    assert balance_token(172, 0) == "+1720"
    assert exchange_asset_id(172, 0) == 100001720
    assert asset.encoding == 1720
    assert asset.coin == "#1720"
    assert asset.balance_token == "+1720"
    assert asset.exchange_asset_id == 100001720
    assert parse_coin("#1721") == OutcomeAssetId(outcome_id=172, side=1)
    assert parse_balance_token("+1720") == OutcomeAssetId(outcome_id=172, side=0)
    assert parse_identifier("#1721") == OutcomeAssetId(outcome_id=172, side=1)
    assert parse_identifier("+1720") == OutcomeAssetId(outcome_id=172, side=0)
    assert parse_identifier("1720") == OutcomeAssetId(outcome_id=172, side=0)
    assert parse_identifier("100001720") == OutcomeAssetId(outcome_id=172, side=0)
    assert parse_identifier("#0") == OutcomeAssetId(outcome_id=0, side=0)
    assert outcome_asset_id_from_identifier("#1721") == 100001721
    assert outcome_asset_id_from_identifier("not-hip4") is None


def test_invalid_side_rejected() -> None:
    with pytest.raises(ValueError):
        encoding(172, 2)
    with pytest.raises(ValueError):
        parse_identifier("#1722")
