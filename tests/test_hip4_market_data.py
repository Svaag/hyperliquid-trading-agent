from __future__ import annotations

from decimal import Decimal

import pytest

from hyperliquid_trading_agent.app.hip4.ids import OutcomeAssetId
from hyperliquid_trading_agent.app.hip4.market_data import Hip4MarketData


class FakeHip4Client:
    async def outcome_meta(self):
        return {
            "outcomes": [
                {
                    "outcome": 2,
                    "name": "BTC daily binary",
                    "description": "class:priceBinary|underlying:BTC",
                    "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                    "quoteToken": "USDC",
                }
            ],
            "questions": [
                {
                    "question": 7,
                    "name": "BTC daily",
                    "description": "question",
                    "fallbackOutcome": 2,
                    "namedOutcomes": [2],
                    "settledNamedOutcomes": [],
                }
            ],
        }

    async def l2_book(self, coin):
        if coin != "#20":
            raise KeyError(coin)
        return {
            "coin": coin,
            "time": 123456,
            "levels": [
                [{"px": "0.60", "sz": "10", "n": 1}],
                [{"px": "0.62", "sz": "15", "n": 1}],
            ],
        }


@pytest.mark.asyncio
async def test_hip4_market_data_resolves_identifier_forms_and_quotes_side():
    market_data = Hip4MarketData(hip4_client=FakeHip4Client())

    hash_ref = await market_data.resolve_identifier("#20")
    plus_ref = await market_data.resolve_identifier("+20")
    bare_ref = await market_data.resolve_identifier("20")
    asset_ref = await market_data.resolve_identifier("100000020")
    outcome_ref = await market_data.resolve_identifier("hip4:2")

    assert hash_ref is not None and hash_ref.asset == OutcomeAssetId(outcome_id=2, side=0)
    assert plus_ref is not None and plus_ref.asset == OutcomeAssetId(outcome_id=2, side=0)
    assert bare_ref is not None and bare_ref.asset == OutcomeAssetId(outcome_id=2, side=0)
    assert asset_ref is not None and asset_ref.asset == OutcomeAssetId(outcome_id=2, side=0)
    assert outcome_ref is not None and outcome_ref.side is None and outcome_ref.outcome.outcome_id == 2

    quote = await market_data.quote_outcome_side(hash_ref.outcome, 0)

    assert quote is not None
    assert quote.coin == "#20"
    assert quote.outcome_name == "Yes"
    assert quote.best_bid == Decimal("0.60")
    assert quote.best_ask == Decimal("0.62")
    assert quote.mid_price == Decimal("0.61")
    assert quote.buy_price == Decimal("0.62")
    assert quote.liquidity_usd == Decimal("15.30")
