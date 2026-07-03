from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.autonomy.discord import format_signal_alert, parse_autonomy_command
from hyperliquid_trading_agent.app.autonomy.levels import infer_liquidation_clusters
from hyperliquid_trading_agent.app.autonomy.market_map import MarketMapReducer
from hyperliquid_trading_agent.app.autonomy.orderflow import compute_orderflow_state
from hyperliquid_trading_agent.app.autonomy.portfolio import PaperPortfolioService
from hyperliquid_trading_agent.app.autonomy.schemas import (
    MarketAsset,
    MarketLevel,
    NewsEvent,
    SignalEvidence,
    TradeSignal,
)
from hyperliquid_trading_agent.app.autonomy.signals import SignalEngine, risk_vetoes
from hyperliquid_trading_agent.app.autonomy.universe import MarketUniverseResolver
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.paper.discord import parse_paper_discord_command
from hyperliquid_trading_agent.app.paper.schemas import PaperTradeDraftRequest


class FakeHyperliquid:
    async def meta_and_asset_ctxs(self, dex: str = ""):
        if dex == "dex1":
            return [
                {"universe": [{"name": "SPX", "szDecimals": 2, "maxLeverage": 10}]},
                [{"coin": "SPX", "dayNtlVlm": "5000000", "markPx": "5000"}],
            ]
        return [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 50}, {"name": "DOGE", "szDecimals": 0, "maxLeverage": 20}]},
            [{"coin": "BTC", "dayNtlVlm": "100000000"}, {"coin": "ETH", "dayNtlVlm": "50000000"}, {"coin": "DOGE", "dayNtlVlm": "75000000"}],
        ]


def test_universe_resolver_core_top_volume_and_hip3_alias():
    settings = Settings(autonomy_core_universe="BTC,ETH,HYPE", autonomy_universe_top_n_perps=1, autonomy_hip3_dexs="dex1")
    resolver = MarketUniverseResolver(settings, FakeHyperliquid())

    import anyio

    assets = anyio.run(resolver.resolve)
    by_symbol = {asset.symbol: asset for asset in assets}

    assert by_symbol["BTC"].source == "core"
    assert by_symbol["ETH"].source == "core"
    assert by_symbol["DOGE"].source == "top_volume"
    assert by_symbol["SPX"].source == "hip3_alias"
    assert any("core asset unresolved: HYPE" in warning for warning in resolver.warnings)


def test_orderflow_depth_imbalance_and_walls():
    state = compute_orderflow_state(
        "BTC",
        {"levels": [[{"px": "99", "sz": "1"}, {"px": "98.9", "sz": "1"}, {"px": "98.8", "sz": "100"}], [{"px": "101", "sz": "1"}, {"px": "101.1", "sz": "2"}]]},
        100,
        1,
    )

    assert state.spread_bps == 200
    assert state.imbalance_top is not None and state.imbalance_top < 0
    assert state.depth_50bps_bid_usd is None or state.depth_50bps_bid_usd >= 0
    assert state.large_bid_walls


def test_market_map_reducer_levels_news_and_inferred_liquidations():
    reducer = MarketMapReducer()
    reducer.set_universe([MarketAsset(symbol="BTC", display_name="BTC", source="core")], timestamp_ms=1)
    reducer.apply_all_mids({"BTC": 100}, timestamp_ms=1)
    reducer.apply_all_mids({"BTC": 102}, timestamp_ms=2)
    reducer.apply_all_mids({"BTC": 104}, timestamp_ms=3)
    candles = [{"h": 105, "l": 99, "c": 104, "v": 10}, {"h": 106, "l": 101, "c": 105, "v": 20}, {"h": 107, "l": 103, "c": 106, "v": 30}]
    reducer.apply_candles("BTC", candles, "1h", timestamp_ms=4)
    reducer.apply_l2_book("BTC", {"levels": [[[103, 5]], [[105, 5]]]}, timestamp_ms=4)
    reducer.apply_news([NewsEvent(id="n1", source="x", provider="x", title="BTC ETF inflow surge", observed_at_ms=4, assets=["BTC"], importance_score=80, sentiment="bullish")], timestamp_ms=4)

    state = reducer.snapshot().assets["BTC"]

    assert state.trend == "up"
    assert state.support_levels
    assert state.news_state is not None
    assert state.news_state.sentiment == "bullish"
    assert all(cluster.confidence != "direct" for cluster in state.liquidation_clusters)


def test_direct_and_inferred_liquidation_clusters_are_labeled():
    direct = MarketLevel(id="direct", symbol="BTC", kind="liquidation_known", price=95, strength=90, timeframe="public", source="public_account", first_seen_ms=1, last_seen_ms=1, metadata={"side_at_risk": "longs", "notional_usd": 1000, "accounts": ["0xabc"]})
    inferred = MarketLevel(id="support", symbol="BTC", kind="support", price=90, strength=60, timeframe="1h", source="candles", first_seen_ms=1, last_seen_ms=1)

    clusters = infer_liquidation_clusters("BTC", 100, [direct, inferred])

    assert clusters[0].confidence == "direct"
    assert clusters[0].source == "public_account"
    assert clusters[1].confidence == "inferred_low"
    assert clusters[1].source == "market_structure"


def test_signal_engine_generates_postable_signal_and_vetoes_duplicate():
    settings = Settings(autonomy_min_signal_score=30)
    reducer = MarketMapReducer()
    reducer.set_universe([MarketAsset(symbol="BTC", display_name="BTC", source="core")], timestamp_ms=1)
    for index, px in enumerate([100, 101, 102, 103], start=1):
        reducer.apply_all_mids({"BTC": px}, timestamp_ms=index)
    reducer.apply_candles("BTC", [{"h": 104, "l": 99, "c": 103, "v": 10}, {"h": 105, "l": 101, "c": 104, "v": 20}, {"h": 106, "l": 102, "c": 105, "v": 30}], "1h", timestamp_ms=5)
    reducer.apply_l2_book("BTC", {"levels": [[[102.9, 100]], [[103.1, 50]]]}, timestamp_ms=5)

    signals = SignalEngine(settings).generate(reducer.snapshot(), timestamp_ms=10)

    assert signals
    signal = signals[0]
    assert signal.risk_plan["exchange_actions"] == []
    assert signal.risk_plan["rr"] >= 1.5
    assert risk_vetoes(signal, reducer.snapshot().assets["BTC"], [signal], []) == ["duplicate_active_signal"]


def test_paper_portfolio_approval_fill_fees_and_stop_close():
    settings = Settings(autonomy_paper_initial_equity_usd=10_000, autonomy_paper_risk_pct_per_trade=1, autonomy_paper_max_single_name_exposure_pct=100)
    service = PaperPortfolioService(settings)
    signal = TradeSignal(
        id="sig_test",
        symbol="BTC",
        side="long",
        signal_type="trend_continuation",
        score=80,
        confidence=0.7,
        created_at_ms=1,
        expires_at_ms=100000,
        entry=100,
        stop=95,
        take_profit=115,
        invalidation="below 95",
        thesis="up",
        risk_plan={"rr": 3, "exchange_actions": []},
    )

    import anyio

    order, fill, position = anyio.run(service.approve_signal, signal, "user1", 100, 1)
    assert order.status == "filled"
    assert fill.price == 100.02
    assert round(fill.fee_usd, 2) > 0
    assert position.status == "open"
    closed = anyio.run(service.mark_to_market, {"BTC": 94}, 2)
    assert closed[0].status == "closed"
    assert service.latest_snapshot() is not None
    assert service.latest_snapshot().metrics["sharpe"] is None  # type: ignore[union-attr]


def test_flip_request_closes_opposing_position_and_marks_signal():
    settings = Settings(
        autonomy_paper_initial_equity_usd=10_000,
        autonomy_paper_risk_pct_per_trade=4,
        autonomy_paper_max_single_name_exposure_pct=5,
    )
    portfolio = PaperPortfolioService(settings)

    short_signal = TradeSignal(
        id="sig_short",
        symbol="SOL",
        side="short",
        signal_type="trend_continuation",
        score=70,
        confidence=0.7,
        created_at_ms=1,
        expires_at_ms=10_000_000,
        entry=100.0,
        stop=105.0,
        take_profit=90.0,
        invalidation="above 105",
        thesis="short",
        risk_plan={"rr": 2, "exchange_actions": []},
    )
    import anyio
    anyio.run(portfolio.approve_signal, short_signal, "user1", 100.0, 1)
    assert any(p.symbol == "SOL" and p.side == "short" and p.status == "open" for p in portfolio.open_positions())

    long_signal = TradeSignal(
        id="sig_long",
        symbol="SOL",
        side="long",
        signal_type="trend_continuation",
        score=77,
        confidence=0.86,
        created_at_ms=2,
        expires_at_ms=10_000_000,
        entry=101.0,
        stop=100.0,
        take_profit=103.0,
        invalidation="below 100",
        thesis="long",
        risk_plan={"rr": 2, "exchange_actions": []},
    )
    with pytest.raises(Exception) as excinfo:
        anyio.run(portfolio.approve_signal, long_signal, "user1", 101.0, 2)
    assert "zero quantity" in str(excinfo.value).lower()
    opposing = portfolio.find_opposing_position("SOL", "long")
    assert opposing is not None
    diag = portfolio.sizing_diagnostics(long_signal, 101.0)
    assert diag["opposing_position_side"] == "short"
    assert diag["current_symbol_exposure_usd"] > diag["max_single_name_exposure_usd"]


def test_manual_paper_trade_draft_confirm_cancel_lifecycle():
    settings = Settings(autonomy_paper_initial_equity_usd=10_000, autonomy_paper_risk_pct_per_trade=1, autonomy_paper_max_single_name_exposure_pct=100)
    portfolio = PaperPortfolioService(settings)
    request = PaperTradeDraftRequest(symbol="BTC", side="long", entry=100, stop=95, take_profit=115, actor="u1", source="manual_discord")

    import anyio

    draft = anyio.run(portfolio.draft_trade, request, None, 1)
    assert draft.status == "new"
    assert portfolio.fills == {}
    assert portfolio.positions == {}

    async def confirm_first():
        return await portfolio.confirm_draft(draft.id, actor="u1", mid=101, timestamp_ms=2)

    order, fill, position = anyio.run(confirm_first)
    assert order.status == "filled"
    assert fill.order_id == order.id
    assert position.status == "open"
    assert position.metadata["source"] == "manual_discord"
    assert portfolio.latest_snapshot() is not None

    cancel_request = PaperTradeDraftRequest(symbol="ETH", side="short", entry=200, stop=210, actor="u1", source="manual_discord")
    cancel_draft = anyio.run(portfolio.draft_trade, cancel_request, None, 3)
    async def cancel_second():
        return await portfolio.cancel_draft(cancel_draft.id, actor="u1", reason="test", timestamp_ms=4)

    cancelled = anyio.run(cancel_second)
    assert cancelled.status == "cancelled"
    with pytest.raises(Exception):
        async def confirm_cancelled():
            return await portfolio.confirm_draft(cancel_draft.id, actor="u1", mid=199, timestamp_ms=5)

        anyio.run(confirm_cancelled)


def test_manual_paper_confirm_requires_explicit_close_opposite_for_flip():
    settings = Settings(autonomy_paper_initial_equity_usd=10_000, autonomy_paper_risk_pct_per_trade=1, autonomy_paper_max_single_name_exposure_pct=100)
    portfolio = PaperPortfolioService(settings)

    import anyio

    short = anyio.run(
        portfolio.draft_trade,
        PaperTradeDraftRequest(symbol="SOL", side="short", entry=100, stop=105, actor="u1", source="manual_discord"),
        None,
        1,
    )
    async def confirm_short():
        return await portfolio.confirm_draft(short.id, actor="u1", mid=100, timestamp_ms=2)

    anyio.run(confirm_short)
    long = anyio.run(
        portfolio.draft_trade,
        PaperTradeDraftRequest(symbol="SOL", side="long", entry=101, stop=99, actor="u1", source="manual_discord"),
        None,
        3,
    )
    with pytest.raises(Exception):
        async def confirm_blocked_long():
            return await portfolio.confirm_draft(long.id, actor="u1", mid=101, timestamp_ms=4)

        anyio.run(confirm_blocked_long)

    async def confirm_long_flip():
        return await portfolio.confirm_draft(long.id, actor="u1", mid=101, close_opposite=True, timestamp_ms=5)

    _order, _fill, new_position = anyio.run(confirm_long_flip)
    assert new_position.side == "long"
    assert len([item for item in portfolio.positions.values() if item.status == "open"]) == 1


def test_manual_paper_explicit_quantity_respects_exposure_caps():
    settings = Settings(
        autonomy_paper_initial_equity_usd=10_000,
        autonomy_paper_max_single_name_exposure_pct=10,
        autonomy_paper_max_gross_leverage=1,
    )
    portfolio = PaperPortfolioService(settings)

    import anyio

    with pytest.raises(Exception):
        anyio.run(
            portfolio.draft_trade,
            PaperTradeDraftRequest(symbol="BTC", side="long", entry=100, stop=95, quantity=20),
            None,
            1,
        )


def test_parse_paper_discord_commands():
    draft = parse_paper_discord_command("paper long BTC entry 65000 stop 64000 tp 68000 risk 0.25")
    assert draft is not None
    assert draft.action == "draft"
    assert draft.draft is not None
    assert draft.draft.symbol == "BTC"
    assert draft.draft.risk_pct == 0.25

    market = parse_paper_discord_command("take paper short ETH market stop 3600")
    assert market is not None
    assert market.draft is not None
    assert market.draft.market is True

    confirm = parse_paper_discord_command("confirm paper abc123 close opposite")
    assert confirm is not None
    assert confirm.action == "confirm"
    assert confirm.close_opposite is True

    close = parse_paper_discord_command("paper close BTC price 101")
    assert close is not None
    assert close.action == "close"
    assert close.position_ref == "BTC"
    assert close.price == 101


def test_discord_autonomy_command_parser_and_alert_format():
    flip_command = parse_autonomy_command("approve flip sig_abc")
    assert flip_command is not None
    assert flip_command.action == "approve_flip"
    assert flip_command.signal_id == "sig_abc"
    cancel_command = parse_autonomy_command("cancel flip sig_abc")
    assert cancel_command is not None
    assert cancel_command.action == "reject"
    assert cancel_command.signal_id == "sig_abc"
    command = parse_autonomy_command("approve signal sig_abc")
    assert command is not None
    assert command.action == "approve"
    assert command.signal_id == "sig_abc"
    signal = TradeSignal(
        id="sig_abc",
        symbol="HYPE",
        side="long",
        signal_type="support_bounce",
        score=82,
        confidence=0.71,
        created_at_ms=1,
        expires_at_ms=30 * 60 * 1000,
        entry=34.2,
        stop=33.45,
        take_profit=36.1,
        invalidation="below 33.45",
        thesis="reclaim",
        risk_plan={"rr": 2.5, "exchange_actions": []},
    )
    content = format_signal_alert(signal)
    assert "approve signal sig_abc" in content
    assert "No live trade will be placed" in content


def test_parse_autonomy_command_bare_approve_via_reply():
    from types import SimpleNamespace
    referenced = SimpleNamespace(content="🚨 AI Trading Signal — SOL LONG ... `approve signal sig_demo_xyz`")
    cmd = parse_autonomy_command("approve", referenced_message=referenced)
    assert cmd is not None
    assert cmd.action == "approve"
    assert cmd.signal_id == "sig_demo_xyz"

    cmd2 = parse_autonomy_command("reject", referenced_message=referenced)
    assert cmd2 is not None
    assert cmd2.action == "reject"
    assert cmd2.signal_id == "sig_demo_xyz"

    # bare "approve" without a reply should NOT trigger an autonomy command
    assert parse_autonomy_command("approve") is None
    assert parse_autonomy_command("approve", referenced_message=SimpleNamespace(content="hello there")) is None


def test_format_signal_alert_renders_funding_as_percentage():
    signal = TradeSignal(
        id="sig_funding",
        symbol="ETH",
        side="long",
        signal_type="funding_oi_squeeze",
        score=78,
        confidence=0.82,
        created_at_ms=1,
        expires_at_ms=60 * 60 * 1000,
        entry=3500,
        stop=3450,
        take_profit=3650,
        invalidation="below 3450",
        thesis="funding flip",
        risk_plan={"rr": 2, "exchange_actions": []},
        evidence=[
            SignalEvidence(category="funding_oi", label="funding/OI", value=1.25e-05, source="funding", kind="funding_hourly"),
            SignalEvidence(category="funding_oi", label="funding/OI", value=-2.5e-05, source="funding", kind="funding_hourly"),
            SignalEvidence(category="orderflow", label="depth/imbalance/spread", value=12.3, source="orderflow", kind="number"),
            SignalEvidence(category="risk_reward", label="reward:risk", value=2.0, source="risk", kind="ratio"),
        ],
    )
    content = format_signal_alert(signal)
    # funding value 1.25e-05 should render as +0.0013%/hr (4 decimals)
    assert "0.0013%/hr" in content or "0.0012%/hr" in content
    # negative funding should keep a minus sign
    assert "-0.0025%/hr" in content
    # ratio kind uses "x" suffix
    assert "2.00x" in content
    # raw evidence label still present
    assert "funding/OI" in content



def test_autonomy_api_requires_auth_outside_dev_and_no_execution_guardrail():
    app = create_app(Settings(environment="prod", position_tracking_enabled=False, autonomy_enabled=False, engine_enabled=False, hip4_enabled=False, orchestration_wave_supervisor_enabled=False, tradfi_enabled=False, agent_api_bearer_token="token", _env_file=None))
    with TestClient(app) as client:
        assert client.get("/autonomy/status").status_code == 401
        response = client.get("/autonomy/status", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    try:
        Settings(hyperliquid_exchange_enabled=True)
    except ValueError as exc:
        assert "HYPERLIQUID_EXCHANGE_ENABLED" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("live exchange enable should be rejected")
