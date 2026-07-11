from __future__ import annotations

import io
import zipfile
from collections import Counter
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass

from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.schemas import NormalizedEvent, RegimeVector, StrategySpec
from hyperliquid_trading_agent.app.engine.strategy_selector import ConservativeStrategySelector
from hyperliquid_trading_agent.app.markets.discord import parse_watchlist_command
from hyperliquid_trading_agent.app.markets.holdings_importer import HoldingsImport, parse_holdings_xlsx
from hyperliquid_trading_agent.app.markets.lighter_adapter import (
    LighterLocalPaperSimulator,
    LighterSDKMarketDataAdapter,
    LighterSequenceGap,
)
from hyperliquid_trading_agent.app.markets.schemas import InstrumentRef, VenueMarketSnapshot, WatchlistChangeRequest
from hyperliquid_trading_agent.app.markets.sync import MarketUniverseSyncService
from hyperliquid_trading_agent.app.markets.universe import (
    WatchlistService,
    default_instrument_seeds,
    resolve_requested_instrument,
)
from hyperliquid_trading_agent.app.tradfi.alpaca_paper_execution import AlpacaPaperExecutionAdapter
from hyperliquid_trading_agent.app.tradfi.paper.schemas import EquityTradeRequest


class FakeMarketRepository:
    enabled = True

    def __init__(self):
        self.instruments: dict[str, dict[str, Any]] = {}
        self.memberships: dict[str, dict[str, Any]] = {}
        self.changes: dict[str, dict[str, Any]] = {}
        self.universe_snapshots: list[dict[str, Any]] = []
        self.venue_snapshots: list[dict[str, Any]] = []
        self.cross_snapshots: list[dict[str, Any]] = []

    async def upsert_instrument(self, item, *, observed_at_ms):
        current = self.instruments.get(item["instrument_id"], {})
        self.instruments[item["instrument_id"]] = {
            **current,
            **item,
            "first_observed_at_ms": current.get("first_observed_at_ms", observed_at_ms),
            "last_observed_at_ms": observed_at_ms,
        }
        return item["instrument_id"]

    async def get_instrument(self, instrument_id):
        return self.instruments.get(instrument_id)

    async def list_instruments(self, *, venue_id=None, underlying_id=None, tradability_status=None, limit=1000):
        rows = list(self.instruments.values())
        if venue_id:
            rows = [item for item in rows if item.get("venue_id") == venue_id]
        if underlying_id:
            rows = [item for item in rows if item.get("underlying_id") == underlying_id]
        if tradability_status:
            rows = [item for item in rows if item.get("tradability_status") == tradability_status]
        return rows[:limit]

    async def upsert_watchlist_membership(self, item):
        current = self.memberships.get(item["instrument_id"], {})
        self.memberships[item["instrument_id"]] = {**current, **item}
        return item["membership_id"]

    async def get_watchlist_membership_by_instrument(self, instrument_id):
        return self.memberships.get(instrument_id)

    async def list_watchlist_memberships(self, *, tier=None, limit=1000):
        rows = list(self.memberships.values())
        if tier:
            rows = [item for item in rows if item.get("tier") == tier]
        return rows[:limit]

    async def record_watchlist_change_event(self, item):
        self.changes[item["change_id"]] = dict(item)
        return item["change_id"]

    async def get_watchlist_change_event(self, change_id):
        return self.changes.get(change_id)

    async def update_watchlist_change_event(self, change_id, **updates):
        self.changes[change_id].update(updates)

    async def list_watchlist_change_events(self, *, limit=100):
        return list(self.changes.values())[:limit]

    async def record_universe_snapshot(self, item):
        self.universe_snapshots.append(dict(item))
        return item["snapshot_id"]

    async def latest_universe_snapshot(self):
        return self.universe_snapshots[-1] if self.universe_snapshots else None

    async def list_universe_snapshots(self, *, limit=100):
        return self.universe_snapshots[-limit:]

    async def record_venue_market_snapshot(self, item):
        self.venue_snapshots.append(dict(item))

    async def record_cross_venue_feature_snapshot(self, item):
        self.cross_snapshots.append(dict(item))


class FakeHoldingsImporter:
    async def fetch(self):
        return HoldingsImport(
            symbols=["AAPL", "NVDA"],
            source_url="https://issuer.example/spy.xlsx",
            fetched_at_ms=123,
            content_sha256="abc123",
        )


def test_canonical_seed_covers_requested_universe_and_provider_identities():
    seeds = default_instrument_seeds()
    assert len(seeds) == 85
    assert len({item.underlying_id for item in seeds}) == 62
    assert Counter(item.venue_id for item in seeds) == {
        "hyperliquid:main": 9,
        "hyperliquid:xyz": 53,
        "alpaca:paper": 23,
    }
    assert len({(item.venue_id, item.provider_symbol) for item in seeds}) == len(seeds)

    xyz = {item.provider_symbol: item for item in seeds if item.venue_id == "hyperliquid:xyz"}
    assert xyz["xyz:SP500"].status == "active"
    assert xyz["xyz:KRW"].status == "delisted"
    assert xyz["xyz:DOW_JONES"].status == "absent"
    assert len([item for item in xyz.values() if item.provider_symbol == "xyz:IBM"]) == 1
    assert resolve_requested_instrument("WTIOIL").provider_symbol == "xyz:CL"
    assert resolve_requested_instrument("NASDAQ").provider_symbol == "xyz:XYZ100"

    hip3_msft = xyz["xyz:MSFT"].ref()
    alpaca_msft = next(item.ref() for item in seeds if item.venue_id == "alpaca:paper" and item.provider_symbol == "MSFT")
    assert hip3_msft.instrument_id != alpaca_msft.instrument_id
    assert hip3_msft.underlying_id == alpaca_msft.underlying_id == "EQUITY:MSFT"


def test_watchlist_seed_admin_confirmation_and_official_holdings_import():
    repository = FakeMarketRepository()
    service = WatchlistService(repository, holdings_importer=FakeHoldingsImporter())

    async def run():
        summary = await service.seed_if_empty()
        assert summary["desired_count"] == 85
        assert summary["underlying_count"] == 62
        assert summary["active_count"] == 77
        assert summary["unavailable_count"] == 8

        nvda = await service.request_change(
            WatchlistChangeRequest(action="add", symbol="NVDA", venue_id="alpaca:paper", tier="broad", actor="admin")
        )
        assert nvda["instrument"]["tradability_status"] == "absent"
        assert nvda["membership"]["enabled"] is False

        removal = await service.request_change(
            WatchlistChangeRequest(action="remove", instrument_id=nvda["instrument"]["instrument_id"], actor="admin")
        )
        assert removal["status"] == "pending_confirmation"
        await service.confirm(removal["change_id"], actor="admin")
        assert repository.memberships[nvda["instrument"]["instrument_id"]]["desired"] is False

        staged_import = await service.request_change(WatchlistChangeRequest(action="import_us_large_cap", actor="admin"))
        imported = await service.confirm(staged_import["change_id"], actor="admin")
        assert imported["result"]["imported_count"] == 2
        assert imported["result"]["paper_tradability_requires_provider_verification"] is True
        aapl = resolve_requested_instrument("AAPL", venue_id="alpaca:paper")
        assert repository.memberships[aapl.instrument_id]["tier"] == "broad"
        assert repository.memberships[aapl.instrument_id]["enabled"] is False

    anyio.run(run)


def test_discord_watchlist_parser_supports_bulk_provider_specific_admin_edits():
    command = parse_watchlist_command("watchlist add NVDA,AAPL MSFT venue=alpaca:paper tier=broad")
    assert command is not None
    assert command.action == "add"
    assert command.symbols == ("NVDA", "AAPL", "MSFT")
    assert command.venue_id == "alpaca:paper"
    assert command.tier == "broad"
    assert command.mutating is True


def test_holdings_xlsx_parser_finds_ticker_column_without_excel_dependency():
    shared = ["Name", "Ticker", "Apple Inc", "AAPL", "NVIDIA Corp", "NVDA"]
    shared_xml = "<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>" + "".join(
        f"<si><t>{value}</t></si>" for value in shared
    ) + "</sst>"
    sheet_xml = """<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>
      <row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1' t='s'><v>1</v></c></row>
      <row r='2'><c r='A2' t='s'><v>2</v></c><c r='B2' t='s'><v>3</v></c></row>
      <row r='3'><c r='A3' t='s'><v>4</v></c><c r='B3' t='s'><v>5</v></c></row>
    </sheetData></worksheet>"""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as workbook:
        workbook.writestr("xl/sharedStrings.xml", shared_xml)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    assert parse_holdings_xlsx(data.getvalue()) == ["AAPL", "NVDA"]


def test_lighter_sdk_adapter_is_read_only_gap_aware_and_simulates_depth():
    adapter = object.__new__(LighterSDKMarketDataAdapter)
    adapter._last_sequence = {}
    adapter._guard_raw_sequence(
        {"type": "subscribed/order_book", "channel": "order_book:7", "sequence": 10, "order_book": {}}
    )
    adapter._guard_raw_sequence(
        {"type": "update/order_book", "channel": "order_book:7", "sequence": 11, "order_book": {}}
    )
    with pytest.raises(LighterSequenceGap):
        adapter._guard_raw_sequence(
            {"type": "update/order_book", "channel": "order_book:7", "sequence": 13, "order_book": {}}
        )

    snapshot = VenueMarketSnapshot(
        snapshot_id="vms_test",
        instrument_id="ins_lighter_btc",
        underlying_id="CRYPTO:BTC",
        venue_id="lighter",
        provider_symbol="BTC-USDC",
        bid_px=99,
        ask_px=101,
        mid_px=100,
        depth_bands={
            "bids": [{"px": 99, "size": 2}],
            "asks": [{"px": 101, "size": 1}, {"px": 102, "size": 2}],
        },
        received_ts_ms=1,
    )
    fill = LighterLocalPaperSimulator().simulate(
        side="buy",
        size=2,
        snapshot=snapshot,
        taker_fee_rate=0.001,
    )
    assert fill.status == "filled"
    assert fill.avg_fill_px == 101.5
    assert fill.fees_usd == pytest.approx(0.203)
    assert fill.slippage_bps == pytest.approx(150.0)


def test_lighter_market_zero_is_a_valid_instrument():
    class FakeOrderApi:
        async def order_book_details(self, **kwargs):
            return SimpleNamespace(
                order_book_details=[
                    SimpleNamespace(
                        model_dump=lambda mode="json": {
                            "symbol": "BTC-USDC",
                            "market_id": 0,
                            "market_type": "perp",
                            "status": "active",
                            "maker_fee": "0",
                            "taker_fee": "0.0002",
                            "size_decimals": 5,
                            "price_decimals": 1,
                        }
                    )
                ]
            )

    async def run():
        adapter = object.__new__(LighterSDKMarketDataAdapter)
        adapter.order_api = FakeOrderApi()
        adapter.timeout_seconds = 10.0
        adapter._market_details = {}

        instruments = await adapter.list_instruments()

        assert len(instruments) == 1
        assert instruments[0].capabilities["market_id"] == 0
        assert instruments[0].underlying_id == "CRYPTO:BTC"

    anyio.run(run)


def test_canonical_hip3_snapshot_preserves_identity_and_asset_class_in_features():
    async def run():
        store = FeatureStore()
        event = NormalizedEvent(
            event_id="evt_hip3_msft",
            event_type="venue_market_snapshot",
            asset_class="equity",
            symbols=["MSFT"],
            source="canonical_market_universe",
            provider="hyperliquid:xyz",
            received_ts_ms=1_000,
            computed_ts_ms=1_000,
            payload={
                "display_symbol": "MSFT",
                "provider_symbol": "xyz:MSFT",
                "bid_px": 499.0,
                "ask_px": 501.0,
                "mid_px": 500.0,
                "mark_px": 500.0,
                "index_px": 499.5,
                "funding_rate": 0.0001,
                "open_interest": 1_000_000,
                "volume_24h": 25_000_000,
                "depth_bands": {
                    "bids": [{"px": 499.0, "size": 400.0}],
                    "asks": [{"px": 501.0, "size": 400.0}],
                },
            },
            metadata={
                "asset_class": "equity",
                "instrument_identity": {
                    "instrument_id": "ins_hip3_msft",
                    "underlying_id": "EQUITY:MSFT",
                    "venue_id": "hyperliquid:xyz",
                    "provider_symbol": "xyz:MSFT",
                },
            },
        )

        features = await store.features_for_event(event)
        snapshot = store.snapshot(asset="MSFT")

        assert {item.feature_name for item in features} >= {
            "mid",
            "spread_bps",
            "top_depth_usd",
            "funding_hourly",
            "open_interest",
            "perp_basis_bps",
        }
        assert snapshot.instrument_id == "ins_hip3_msft"
        assert snapshot.underlying_id == "EQUITY:MSFT"
        assert snapshot.venue_id == "hyperliquid:xyz"
        assert snapshot.provider_symbol == "xyz:MSFT"
        assert snapshot.metadata["asset_class"] == "equity"
        assert snapshot.features["top_depth_usd"] == pytest.approx(400_000.0)

    anyio.run(run)


class FakeAlpacaTradingClient:
    def __init__(self):
        self.submitted = None

    async def get_order_by_client_id(self, client_order_id):
        raise APIError('{"code": 404, "message": "not found"}')

    async def submit_order(self, request):
        self.submitted = request
        return {
            "id": "alpaca_order_1",
            "client_order_id": request.client_order_id,
            "symbol": request.symbol,
            "qty": str(request.qty),
            "side": request.side.value,
            "status": "accepted",
        }

    async def get_all_assets(self, request):
        return [
            {
                "id": "asset_msft",
                "symbol": "MSFT",
                "tradable": True,
                "fractionable": True,
                "shortable": True,
                "easy_to_borrow": True,
                "exchange": "NASDAQ",
                "class": "us_equity",
            }
        ]


class FakeAlpacaDataClient:
    async def get_stock_latest_quote(self, request):
        return {
            "MSFT": {
                "bid_price": 499.0,
                "ask_price": 501.0,
                "timestamp": "2026-07-11T12:00:00Z",
            }
        }


def test_alpaca_hosted_paper_uses_bracket_orders_and_verifies_market_identity():
    trading = FakeAlpacaTradingClient()
    adapter = AlpacaPaperExecutionAdapter(
        api_key="",
        api_secret="",
        client=trading,
        data_client=FakeAlpacaDataClient(),
    )

    async def run():
        order = await adapter.submit_equity_trade(
            EquityTradeRequest(
                symbol="MSFT",
                side="long",
                quantity=2,
                entry=500,
                stop=490,
                take_profit=520,
                signal_id="signal_msft",
            )
        )
        assert order["id"] == "alpaca_order_1"
        assert trading.submitted.order_class == OrderClass.BRACKET
        assert trading.submitted.stop_loss.stop_price == 490
        assert trading.submitted.take_profit.limit_price == 520

        ref = InstrumentRef(
            underlying_id="EQUITY:MSFT",
            venue_id="alpaca:paper",
            provider_symbol="MSFT",
            instrument_type="equity",
            tradability_status="data_only",
        )
        resolved = await adapter.refresh_instruments([ref])
        assert resolved[0].tradability_status == "active"
        assert resolved[0].capabilities["paper_execution"] is True
        snapshots = await adapter.market_snapshots(resolved)
        assert snapshots[0].mid_px == 500
        assert snapshots[0].metadata["paper_execution_source_of_truth"] is True

    anyio.run(run)


def test_cross_venue_features_pair_hip3_with_alpaca_without_averaging():
    repository = FakeMarketRepository()
    settings = SimpleNamespace(market_universe_enabled=True)
    service = MarketUniverseSyncService(
        settings=settings,
        repository=repository,
        hyperliquid=SimpleNamespace(),
    )
    hip3 = VenueMarketSnapshot(
        snapshot_id="hip3_msft",
        instrument_id="ins_hip3_msft",
        underlying_id="EQUITY:MSFT",
        venue_id="hyperliquid:xyz",
        provider_symbol="xyz:MSFT",
        mid_px=500,
        received_ts_ms=1_000,
    )
    alpaca = VenueMarketSnapshot(
        snapshot_id="alpaca_msft",
        instrument_id="ins_alpaca_msft",
        underlying_id="EQUITY:MSFT",
        venue_id="alpaca:paper",
        provider_symbol="MSFT",
        mid_px=501,
        received_ts_ms=1_100,
    )

    count = anyio.run(service._record_cross_venue, [hip3, alpaca])
    assert count == 1
    assert repository.cross_snapshots[0]["reference_venue_id"] == "hyperliquid:xyz"
    assert repository.cross_snapshots[0]["comparison_venue_id"] == "alpaca:paper"
    assert repository.cross_snapshots[0]["price_delta_bps"] == pytest.approx(20.0)
    assert repository.cross_snapshots[0]["metadata"]["pairwise_not_averaged"] is True


def test_strategy_selector_enforces_provider_asset_and_venue_eligibility():
    strategy = SimpleNamespace(
        spec=StrategySpec(
            strategy_id="eligible_test_v1",
            version="1.0.0",
            family="test",
            supported_assets=["BTC"],
            supported_venues=["hyperliquid:main"],
        )
    )
    regime = RegimeVector(
        regime_snapshot_id="reg_test",
        primary_asset="BTC",
        created_at_ms=1,
        as_of_ms=1,
    )
    selector = ConservativeStrategySelector()
    selected = selector.select([strategy], regime, asset="BTC", venue="hyperliquid")
    assert selected.strategies == [strategy]
    unsupported_asset = selector.select([strategy], regime, asset="SOL", venue="hyperliquid:main")
    assert unsupported_asset.skipped[0]["reason"] == "unsupported_asset"
    unsupported_venue = selector.select([strategy], regime, asset="BTC", venue="lighter")
    assert unsupported_venue.skipped[0]["reason"] == "unsupported_venue"
