"""Tests for app/tradfi/ — schemas, client, provider, options flow, paper simulation."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from hyperliquid_trading_agent.app.autonomy.equity_features import detect_technical_breakout_equity
from hyperliquid_trading_agent.app.tradfi.alpaca_provider import AlpacaTradFiProvider
from hyperliquid_trading_agent.app.tradfi.alpha_vantage_provider import AlphaVantageTradFiProvider
from hyperliquid_trading_agent.app.tradfi.base import TradFiProvider
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.composite_provider import CompositeTradFiProvider
from hyperliquid_trading_agent.app.tradfi.options_flow import FlowEnricher, OptionsFlowDetector
from hyperliquid_trading_agent.app.tradfi.paper.schemas import (
    EquityPaperPortfolio,
    EquityRiskControlError,
    EquityTradeRequest,
)
from hyperliquid_trading_agent.app.tradfi.paper.simulator import EquityPaperSimulator
from hyperliquid_trading_agent.app.tradfi.schemas import (
    Bar,
    CalendarEvent,
    CorporateAction,
    OptionContract,
    OptionsChain,
    OptionsFlowEvent,
    StockQuote,
    StockSnapshot,
    StockTrade,
)
from hyperliquid_trading_agent.app.tradfi.sec_edgar import SecEdgarClient

# --- Schemas ------------------------------------------------------------------


def test_stock_quote_serialization():
    quote = StockQuote(symbol="AAPL", bid_price=195.50, ask_price=195.55, ask_size=100, bid_size=200)
    assert quote.model_dump(mode="json")["symbol"] == "AAPL"
    assert quote.model_dump(mode="json")["bid_price"] == 195.50


def test_bar_serialization():
    bar = Bar(symbol="NVDA", timestamp=datetime.now(timezone.utc), open=110.0, high=112.0, low=109.5, close=111.0, volume=50000000.0, timeframe="1Day")
    data = bar.model_dump(mode="json")
    assert data["symbol"] == "NVDA"
    assert data["close"] == 111.0


def test_corporate_action_serialization():
    ca = CorporateAction(id="ca1", symbol="MSFT", action_type="cash_dividend", ex_date=date(2026, 6, 20), dividend_rate=0.75)
    data = ca.model_dump(mode="json")
    assert data["dividend_rate"] == 0.75


def test_option_contract_serialization():
    c = OptionContract(
        symbol="AAPL260619C00200000",
        underlying="AAPL",
        strike_price=200.0,
        expiration_date=date(2026, 6, 19),
        option_type="call",
        delta=0.55,
        gamma=0.03,
        implied_volatility=0.25,
    )
    data = c.model_dump(mode="json")
    assert data["delta"] == 0.55


def test_options_chain_properties():
    chain = OptionsChain(
        underlying="AAPL",
        underlying_price=195.0,
        contracts=[
            OptionContract(symbol="AAPL260619C00200000", underlying="AAPL", strike_price=200.0, expiration_date=date(2026, 6, 19), option_type="call"),
            OptionContract(symbol="AAPL260619P00190000", underlying="AAPL", strike_price=190.0, expiration_date=date(2026, 6, 19), option_type="put"),
        ],
    )
    assert len(chain.calls) == 1
    assert len(chain.puts) == 1


def test_options_flow_event_scoring():
    e = OptionsFlowEvent(symbol="AAPL", detected_at=datetime.now(timezone.utc), volume_oi_ratio=8.5, premium_estimate=5_000_000.0, flow_type="call_buy", urgency_score=75.0)
    assert e.urgency_score == 75.0
    data = e.model_dump(mode="json")
    assert data["flow_type"] == "call_buy"


# --- Client -------------------------------------------------------------------


class _FakeProvider(TradFiProvider):
    name = "fake"

    async def get_latest_quote(self, symbol: str) -> StockQuote | None:
        return StockQuote(symbol=symbol.upper(), bid_price=100.0, ask_price=100.1)

    async def get_latest_trade(self, symbol: str) -> StockTrade | None:
        return StockTrade(symbol=symbol.upper(), price=100.05, size=100, timestamp=datetime.now(timezone.utc))

    async def get_snapshots(self, symbols: list[str]) -> dict[str, StockSnapshot]:
        return {s.upper(): StockSnapshot(symbol=s.upper(), previous_close=99.0, change_pct=1.0) for s in symbols}

    async def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int | None = None) -> list[Bar]:
        return [Bar(symbol=symbol.upper(), timestamp=start, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000000.0, timeframe=timeframe)]

    async def get_corporate_actions(self, symbols: list[str], start=None, end=None, limit=None) -> dict[str, list[CorporateAction]]:
        return {}

    async def get_options_chain(self, underlying: str, expiration=None, strike_min=None, strike_max=None) -> OptionsChain:
        return OptionsChain(underlying=underlying.upper(), underlying_price=100.0)

    async def get_option_snapshot(self, option_symbol: str) -> OptionContract | None:
        return None

    async def get_calendar(self, start, end, event_types=None) -> list[CalendarEvent]:
        return [CalendarEvent(date=start, event_type="trading_day")]


def test_tradfi_client_cache_hit():
    async def run():
        provider = _FakeProvider()
        client = TradFiClient(provider, cache_ttl_quote_seconds=60)
        quote1 = await client.get_latest_quote("AAPL")
        quote2 = await client.get_latest_quote("AAPL")
        assert quote1 is quote2  # same cached object
        assert quote1.bid_price == 100.0
    import anyio
    anyio.run(run)


def test_tradfi_client_bars():
    async def run():
        provider = _FakeProvider()
        client = TradFiClient(provider)
        bars = await client.get_bars("NVDA", timeframe="1d", lookback_hours=24)
        assert len(bars) == 1
        assert bars[0].symbol == "NVDA"
    import anyio
    anyio.run(run)


def test_tradfi_client_calendar():
    async def run():
        provider = _FakeProvider()
        client = TradFiClient(provider)
        events = await client.get_calendar(date(2026, 6, 16), date(2026, 6, 20))
        assert len(events) == 1
        assert events[0].event_type == "trading_day"
    import anyio
    anyio.run(run)


class _FakeSecResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeSecHttp:
    def __init__(self):
        self.calls = []
        self.company_payload = {
            "0": {"cik_str": 1828242, "ticker": "CRCL", "title": "Circle Internet Group, Inc."},
            "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        }
        self.submissions_payload = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0001828242-26-000010", "0001828242-26-000009"],
                    "filingDate": ["2026-05-11", "2026-04-01"],
                    "reportDate": ["2026-03-31", "2026-03-20"],
                    "form": ["10-Q", "8-K"],
                    "primaryDocument": ["crcl-20260331.htm", "crcl-8k.htm"],
                    "primaryDocDescription": ["10-Q", "8-K"],
                }
            }
        }

    async def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if "company_tickers" in url:
            return _FakeSecResponse(self.company_payload)
        return _FakeSecResponse(self.submissions_payload)

    async def aclose(self):
        return None


def test_sec_edgar_client_resolves_crcl_and_latest_10q_links():
    async def run():
        http = _FakeSecHttp()
        client = SecEdgarClient(http_client=http)  # type: ignore[arg-type]

        result = await client.latest_filings("CRCL quarterly report EDGAR", forms=["10-Q"], limit=1)

        assert result.company is not None
        assert result.company.ticker == "CRCL"
        assert result.company.cik == "0001828242"
        assert result.forms_requested == ["10-Q"]
        assert len(result.filings) == 1
        filing = result.filings[0]
        assert filing.filing_detail_url == "https://www.sec.gov/Archives/edgar/data/1828242/000182824226000010/0001828242-26-000010-index.html"
        assert filing.document_url == "https://www.sec.gov/Archives/edgar/data/1828242/000182824226000010/crcl-20260331.htm"
        assert http.calls[0][1]["User-Agent"]

    import anyio
    anyio.run(run)


def test_sec_edgar_client_resolves_circle_alias_and_does_not_fabricate_missing_form():
    async def run():
        client = SecEdgarClient(http_client=_FakeSecHttp())  # type: ignore[arg-type]

        company = await client.resolve_company("Circle quarterly report")
        missing = await client.latest_filings("Circle S-1 EDGAR", forms=["S-1"], limit=1)

        assert company is not None
        assert company.ticker == "CRCL"
        assert missing.company is not None
        assert missing.filings == []
        assert missing.note == "no_matching_filings"

    import anyio
    anyio.run(run)


class _FakeAlphaResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeAlphaHttp:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def get(self, url, params):
        self.calls.append((url, params))
        return _FakeAlphaResponse(self.payloads.pop(0))

    async def aclose(self):
        return None


def test_alpha_vantage_provider_parses_daily_bars():
    async def run():
        payload = {
            "Time Series (Daily)": {
                "2026-07-01": {"1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "1000000"},
                "2026-06-30": {"1. open": "98", "2. high": "101", "3. low": "97", "4. close": "100", "5. volume": "900000"},
            }
        }
        http = _FakeAlphaHttp([payload])
        provider = AlphaVantageTradFiProvider(api_key="key", transport="rest", http_client=http)  # type: ignore[arg-type]

        bars = await provider.get_bars("TSLA", "1Day", datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc))

        assert [bar.close for bar in bars] == [100.0, 104.0]
        assert http.calls[0][1]["function"] == "TIME_SERIES_DAILY"
        assert http.calls[0][1]["symbol"] == "TSLA"

    import anyio

    anyio.run(run)


def test_composite_provider_falls_back_when_alpha_has_no_bars():
    async def run():
        alpha_http = _FakeAlphaHttp([{"Note": "rate limit"}])
        alpha = AlphaVantageTradFiProvider(api_key="key", transport="rest", http_client=alpha_http)  # type: ignore[arg-type]
        provider = CompositeTradFiProvider([alpha, _FakeProvider()])

        bars = await provider.get_bars("TSLA", "1Day", datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc))

        assert len(bars) == 1
        assert bars[0].symbol == "TSLA"

    import anyio

    anyio.run(run)


class _FakeAlpacaBar:
    timestamp = datetime(2026, 7, 2, 14, 30, tzinfo=timezone.utc)
    open = 100.0
    high = 102.0
    low = 99.0
    close = 101.0
    volume = 123456.0
    trade_count = 42
    vwap = 100.5


class _FakeAlpacaBarSet:
    def __init__(self):
        self.data = {"TSLA": [_FakeAlpacaBar()]}


class _FakeAlpacaStockClient:
    def get_stock_bars(self, request):
        return _FakeAlpacaBarSet()


def test_alpaca_provider_parses_barset_response():
    async def run():
        provider = AlpacaTradFiProvider(api_key="key", api_secret="secret")
        provider._stock = _FakeAlpacaStockClient()  # type: ignore[assignment]

        bars = await provider.get_bars("TSLA", "1Hour", datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc))

        assert len(bars) == 1
        assert bars[0].symbol == "TSLA"
        assert bars[0].close == 101.0
        assert bars[0].trade_count == 42

    import anyio

    anyio.run(run)


# --- Options Flow Detector ----------------------------------------------------


def _make_chain(underlying_price: float) -> OptionsChain:
    contracts = []
    for opt_type in ("call", "put"):
        for strike in [95.0, 100.0, 105.0]:
            contracts.append(
                OptionContract(
                    symbol=f"FAKE{int(strike)}{'C' if opt_type == 'call' else 'P'}",
                    underlying="FAKE",
                    strike_price=strike,
                    expiration_date=date(2026, 7, 18),
                    option_type=opt_type,  # type: ignore[arg-type]
                    bid=1.0,
                    ask=1.2,
                    last_price=1.1,
                    volume=100,
                    open_interest=500,
                    implied_volatility=0.3,
                )
            )
    # Add an unusual contract with elevated volume/OI
    contracts.append(
        OptionContract(
            symbol="FAKE100C_unusual",
            underlying="FAKE",
            strike_price=100.0,
            expiration_date=date(2026, 7, 18),
            option_type="call",
            bid=2.0,
            ask=2.2,
            last_price=2.1,
            volume=5000,
            open_interest=500,
            implied_volatility=0.35,
        )
    )
    return OptionsChain(underlying="FAKE", underlying_price=underlying_price, contracts=contracts)


def test_options_flow_detector_default_thresholds():
    detector = OptionsFlowDetector(min_volume_oi_ratio=3.0, min_premium=500_000.0)
    chain = _make_chain(100.0)
    events = detector.detect(chain)
    # The unusual contract has vol/OI = 5000/500 = 10x, premium = 5000*2.1*100 = 1,050,000
    assert len(events) >= 1
    assert events[0].volume_oi_ratio > 3.0


def test_options_flow_detector_no_events_below_threshold():
    detector = OptionsFlowDetector(min_volume_oi_ratio=20.0, min_premium=50_000_000.0)
    chain = _make_chain(100.0)
    events = detector.detect(chain)
    assert len(events) == 0


def test_options_flow_detector_classifies_call_buy():
    detector = OptionsFlowDetector()
    chain = _make_chain(100.0)
    events = detector.detect(chain)
    call_events = [e for e in events if e.flow_type == "call_buy"]
    assert len(call_events) >= 1


def test_flow_enricher_disabled_without_gateway():
    enricher = FlowEnricher(model_gateway=None)
    assert not enricher.enabled


# --- Paper Simulator ----------------------------------------------------------


def test_equity_portfolio_initial_state():
    portfolio = EquityPaperPortfolio(initial_equity_usd=100_000.0)
    assert portfolio.equity_usd == 100_000.0
    assert portfolio.cash_usd == 100_000.0


async def test_equity_paper_place_order():
    simulator = EquityPaperSimulator(initial_equity_usd=100_000.0, risk_pct_per_trade=1.0, max_single_name_exposure_pct=0.5)
    request = EquityTradeRequest(
        symbol="AAPL",
        side="long",
        entry=195.0,
        stop=190.0,
        take_profit=205.0,
        account_equity_usd=100_000.0,
        risk_pct=0.5,
    )
    order = await simulator.place_order(request)
    assert order.quantity == pytest.approx(100.0)  # 0.5% of $100k divided by the $5 stop distance
    assert order.status == "filled"
    assert order.filled_px == pytest.approx(195.0195)
    assert simulator.portfolio.cash_usd == pytest.approx(100_000.0 - (100 * order.filled_px * simulator.taker_fee_bps / 10_000))
    assert len(simulator.positions) >= 1
    position = next(iter(simulator.positions.values()))
    assert position.symbol == "AAPL"
    assert position.side == "long"


async def test_equity_paper_risk_control_leverage():
    simulator = EquityPaperSimulator(initial_equity_usd=10_000.0, max_gross_leverage=1.5)
    # A $20k position on $10k equity would be 2x leverage
    request = EquityTradeRequest(symbol="NVDA", side="long", entry=500.0, stop=450.0, account_equity_usd=10_000.0, quantity=40)  # 40 * 500 = 20k
    with pytest.raises(EquityRiskControlError, match="leverage"):
        await simulator.place_order(request)


async def test_equity_paper_close_position():
    simulator = EquityPaperSimulator(initial_equity_usd=100_000.0, max_single_name_exposure_pct=0.5)
    request = EquityTradeRequest(symbol="MSFT", side="long", entry=400.0, stop=390.0, account_equity_usd=100_000.0, quantity=100)
    await simulator.place_order(request)
    pos = next(p for p in simulator.positions.values() if p.status == "open")
    closed = await simulator.close_position(pos.id)
    assert closed is not None
    assert closed.status == "closed"


async def test_equity_paper_accounting_tracks_margin_not_cash_stock_purchase():
    simulator = EquityPaperSimulator(initial_equity_usd=100_000.0, max_single_name_exposure_pct=0.5, taker_fee_bps=0.0, default_slippage_bps=0.0)
    request = EquityTradeRequest(symbol="AAPL", side="long", entry=100.0, stop=95.0, account_equity_usd=100_000.0, quantity=100)
    await simulator.place_order(request)
    assert simulator.portfolio.cash_usd == pytest.approx(100_000.0)
    open_snapshot = simulator.snapshot()
    assert open_snapshot.equity_usd == pytest.approx(100_000.0)

    pos = next(p for p in simulator.positions.values() if p.status == "open")
    pos.mark_px = 110.0
    pos.unrealized_pnl_usd = 1_000.0
    marked_snapshot = simulator.snapshot()
    assert marked_snapshot.equity_usd == pytest.approx(101_000.0)

    closed = await simulator.close_position(pos.id)
    assert closed is not None
    assert simulator.portfolio.cash_usd == pytest.approx(101_000.0)
    assert simulator.portfolio.realized_pnl_usd == pytest.approx(1_000.0)


async def test_equity_paper_update_marks():
    simulator = EquityPaperSimulator(initial_equity_usd=100_000.0, max_single_name_exposure_pct=0.5)
    request = EquityTradeRequest(symbol="TSLA", side="long", entry=200.0, stop=190.0, account_equity_usd=100_000.0, quantity=100)
    await simulator.place_order(request)
    snapshot = simulator.snapshot()
    assert snapshot.equity_usd is not None
    assert snapshot.unrealized_pnl_usd == 0.0  # marks not updated yet


# --- Config -------------------------------------------------------------------


def test_equity_signal_evidence_sources_are_schema_valid():
    snap = StockSnapshot(
        symbol="AAPL",
        previous_close=100.0,
        change_pct=4.0,
        daily_bar=Bar(
            symbol="AAPL",
            timestamp=datetime.now(timezone.utc),
            open=100.0,
            high=105.0,
            low=99.0,
            close=104.0,
            volume=25_000_000.0,
            timeframe="1Day",
        ),
    )
    signal = detect_technical_breakout_equity("AAPL", snap, timestamp_ms=1)
    assert signal is not None
    assert {item.source for item in signal.evidence} == {"equity"}


def test_tradfi_config_warnings(monkeypatch):
    from hyperliquid_trading_agent.app.config import Settings

    # No keys, but tradfi not enabled: no warnings
    settings = Settings(tradfi_enabled=False)
    assert settings.tradfi_config_warnings() == []

    # TradFi enabled without keys
    settings = Settings(
        tradfi_enabled=True,
        tradfi_provider_order="alpaca",
        alpha_vantage_enabled=False,
        alpha_vantage_api_key="",
        alpaca_api_key="",
        alpaca_api_secret="",
    )
    warnings = settings.tradfi_config_warnings()
    assert any("ALPACA_API_KEY" in w for w in warnings)

    # Equity autonomy without tradfi
    settings = Settings(tradfi_enabled=False, autonomy_equity_enabled=True)
    warnings = settings.tradfi_config_warnings()
    assert any("TRADFI_ENABLED" in w for w in warnings)

    # Equity autonomy without universe
    settings = Settings(tradfi_enabled=True, autonomy_equity_enabled=True, autonomy_equity_universe="")
    warnings = settings.tradfi_config_warnings()
    assert any("AUTONOMY_EQUITY_UNIVERSE" in w for w in warnings)

    # Options flow without tradfi
    settings = Settings(tradfi_enabled=False, options_flow_enabled=True)
    warnings = settings.tradfi_config_warnings()
    assert any("TRADFI_ENABLED" in w for w in warnings)


def test_alpaca_trading_disabled_validation():
    from hyperliquid_trading_agent.app.config import Settings

    with pytest.raises(ValidationError, match="ALPACA_TRADING_ENABLED"):
        Settings(alpaca_trading_enabled=True)


def test_tradfi_effective_enabled():
    from hyperliquid_trading_agent.app.config import Settings

    s = Settings(tradfi_enabled=True, autonomy_equity_enabled=True)
    assert s.autonomy_equity_effective_enabled is True

    s2 = Settings(tradfi_enabled=False, autonomy_equity_enabled=True)
    assert s2.autonomy_equity_effective_enabled is False

    s3 = Settings(tradfi_enabled=True, options_flow_enabled=True)
    assert s3.options_flow_effective_enabled is True
