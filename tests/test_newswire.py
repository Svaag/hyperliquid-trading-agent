from __future__ import annotations

import anyio
from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app
from hyperliquid_trading_agent.app.newswire.adapters.alpaca_ws import AlpacaNewsAdapter
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus, QueueSubscriber
from hyperliquid_trading_agent.app.newswire.classify import classify_event_type, classify_urgency, source_score
from hyperliquid_trading_agent.app.newswire.consumers.discord_news import DiscordNewsPublisher
from hyperliquid_trading_agent.app.newswire.normalize import normalize
from hyperliquid_trading_agent.app.newswire.riskgate import HaltStateGate
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter, RawNewsItem
from hyperliquid_trading_agent.app.newswire.service import NewswireService

UNIVERSE = ["BTC", "ETH", "HYPE", "AAPL", "NVDA"]


def _raw(**kwargs) -> RawNewsItem:
    base = {"source": "rss", "transport": "rss", "headline": "Generic headline"}
    base.update(kwargs)
    return RawNewsItem(**base)


def _event(**kwargs) -> NewswireEvent:
    event = normalize(_raw(**kwargs), symbols_universe=UNIVERSE, received_at_ms=1_000)
    assert event is not None
    return event


# --- deterministic pipeline --------------------------------------------------


def test_normalize_classifies_filing_and_tags_symbols():
    event = normalize(
        _raw(source="sec_edgar", external_id="acc-1", headline="NVDA files 8-K with SEC on guidance"),
        symbols_universe=UNIVERSE,
        received_at_ms=2_000,
    )
    assert event is not None
    assert event.event_type == "sec_filing"
    assert event.asset_class == "equity"
    assert "NVDA" in event.symbols
    assert event.source_score == 1.0
    assert event.event_id.startswith("nw_")
    assert event.tradability.allow_auto_trade is False


def test_normalize_returns_none_for_empty_item():
    assert normalize(_raw(headline="", body=""), symbols_universe=UNIVERSE) is None


def test_event_id_is_stable_for_updates():
    created = _raw(source="alpaca", external_id="42", headline="Headline v1")
    updated = _raw(source="alpaca", external_id="42", headline="Headline v2 corrected", action="updated")
    assert normalize(created, symbols_universe=UNIVERSE).event_id == normalize(updated, symbols_universe=UNIVERSE).event_id


def test_classify_event_type_and_urgency_and_source_score():
    assert classify_event_type("federal_reserve", "FOMC raises interest rate") == "macro"
    assert classify_event_type("rss", "Acme to acquire Beta in all-cash deal") == "mna"
    assert classify_urgency("nasdaq_halts", "rss", "halt", 50.0, "trading halt on AAPL") == "breaking"
    assert classify_urgency("rss", "rss", "headline", 10.0, "minor update") == "background"
    assert source_score("sec_edgar") == 1.0
    assert source_score("x_cashtag") == 0.4


def test_to_news_event_bridge_preserves_core_fields():
    event = _event(source="coindesk", headline="ETH rallies on inflows", symbols=["ETH"])
    legacy = event.to_news_event()
    assert legacy.id == event.event_id
    assert legacy.title == event.headline
    assert legacy.assets == event.symbols
    assert legacy.metadata["event_type"] == event.event_type


# --- bus ---------------------------------------------------------------------


def test_bus_fanout_respects_filters():
    async def run():
        bus = InProcessNewswireBus()
        crypto: list[NewswireEvent] = []
        equity: list[NewswireEvent] = []
        await bus.subscribe(lambda e: crypto.append(e), filter=NewswireFilter(asset_classes=["crypto"]))
        await bus.subscribe(lambda e: equity.append(e), filter=NewswireFilter(asset_classes=["equity"]))
        await bus.publish(_event(source="coindesk", headline="BTC pumps", symbols=["BTC"]))
        await bus.publish(_event(source="sec_edgar", external_id="x", headline="AAPL files 10-Q"))
        return crypto, equity

    crypto, equity = anyio.run(run)
    assert [e.asset_class for e in crypto] == ["crypto"]
    assert [e.asset_class for e in equity] == ["equity"]


def test_queue_subscriber_receives_published_events():
    async def run():
        bus = InProcessNewswireBus()
        async with QueueSubscriber(bus, filter=NewswireFilter(min_importance=0)) as sub:
            await bus.publish(_event(headline="Something happened"))
            return await sub.get()

    event = anyio.run(run)
    assert event.headline == "Something happened"


# --- halt gate ---------------------------------------------------------------


def test_halt_gate_marks_and_clears_symbols():
    gate = HaltStateGate()
    halt = _event(source="nasdaq_halts", external_id="h1", headline="Trading halt on AAPL", symbols=["AAPL"])
    gate.apply(halt)
    follow_up = _event(source="rss", external_id="f1", headline="AAPL rumor mill", symbols=["AAPL"])
    gated = gate.apply(follow_up)
    assert gated.tradability.halted_symbols == ["AAPL"]
    assert gated.tradability.halt_state_checked is True
    assert gated.tradability.allow_auto_trade is False

    resume = _event(source="nasdaq_halts", external_id="h2", headline="Trading resumes for AAPL", symbols=["AAPL"])
    gate.apply(resume)
    assert "AAPL" not in gate.halted_symbols()


# --- service -----------------------------------------------------------------


def test_service_ingest_dedupes_and_publishes():
    async def run():
        service = NewswireService(settings=Settings(newswire_enabled=True))
        seen: list[NewswireEvent] = []
        await service.bus.subscribe(lambda e: seen.append(e))
        raw = _raw(source="alpaca", external_id="100", headline="BTC breaks out", symbols=["BTC"])
        first = await service._ingest(raw)
        second = await service._ingest(raw)
        return first, second, seen

    first, second, seen = anyio.run(run)
    assert first is not None
    assert second is None  # duplicate dropped
    assert len(seen) == 1
    assert seen[0].symbols == ["BTC"]


# --- alpaca adapter parsing --------------------------------------------------


def test_alpaca_frame_parses_into_raw_item():
    adapter = AlpacaNewsAdapter(ws_url="wss://x", api_key="k", api_secret="s", symbols=["*"])
    frame = {
        "T": "n",
        "id": 555,
        "headline": "Company X reports record earnings",
        "summary": "Beats estimates",
        "author": "Benzinga",
        "created_at": "2026-06-15T12:00:00Z",
        "url": "https://example.com/n/555",
        "symbols": ["nvda"],
        "source": "benzinga",
    }
    raw = adapter._to_raw(frame)
    assert raw.external_id == "555"
    assert raw.symbols == ["NVDA"]
    assert raw.transport == "websocket"
    event = normalize(raw, symbols_universe=UNIVERSE)
    assert event is not None
    assert event.event_type == "earnings"


# --- discord publisher -------------------------------------------------------


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, channel_id: str, content: str) -> str | None:
        self.sent.append((channel_id, content))
        return "msg-1"


def test_discord_publisher_posts_breaking_immediately_and_batches_rest():
    async def run():
        settings = Settings(
            newswire_news_channel_id="999",
            newswire_send_min_interval_ms=0,
            newswire_breaking_min_importance=80,
            newswire_news_min_importance=0,
        )
        sink = _FakeSink()
        publisher = DiscordNewsPublisher(settings=settings, bus=InProcessNewswireBus(), alert_sink=sink)
        breaking = _event(source="nasdaq_halts", external_id="b", headline="Trading halt on NVDA", symbols=["NVDA"])
        normal = _event(source="coindesk", external_id="n", headline="ETH slowly grinds higher", symbols=["ETH"])
        normal.urgency = "normal"
        normal.importance_score = 30.0
        await publisher._on_event(breaking)
        await publisher._on_event(normal)
        before_flush = list(sink.sent)
        await publisher._flush()
        return before_flush, sink.sent

    before_flush, after_flush = anyio.run(run)
    assert len(before_flush) == 1  # only the breaking event posted immediately
    assert "Trading halt on NVDA" in before_flush[0][1]
    assert len(after_flush) == 2  # digest flushed the buffered normal event
    assert "digest" in after_flush[1][1].lower()


# --- gateway -----------------------------------------------------------------


def test_newswire_gateway_lists_filters_and_404s():
    settings = Settings(environment="test", newswire_enabled=False, position_tracking_enabled=False, autonomy_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        service: NewswireService = app.state.newswire_service
        service._index(_event(source="coindesk", external_id="g1", headline="BTC surges", symbols=["BTC"]))
        service._index(_event(source="sec_edgar", external_id="g2", headline="AAPL files 8-K"))

        all_events = client.get("/newswire/events")
        assert all_events.status_code == 200
        assert all_events.json()["count"] == 2

        btc = client.get("/newswire/events", params={"symbol": "BTC"})
        assert btc.json()["count"] == 1
        assert btc.json()["items"][0]["symbols"] == ["BTC"]

        status = client.get("/newswire/status")
        assert status.status_code == 200
        assert status.json()["buffered_events"] == 2

        assert client.get("/newswire/events/does-not-exist").status_code == 404


# --- service supervisor lifecycle --------------------------------------------


class _OneShotAdapter(NewswireAdapter):
    name = "oneshot"

    async def run(self, emit: RawEmit) -> None:
        await emit(_raw(source="alpaca", external_id=" os1", headline="ETH breakout confirmed", symbols=["ETH"]))


def test_service_start_supervises_adapter_and_publishes(monkeypatch):
    async def run():
        service = NewswireService(settings=Settings(newswire_enabled=True))
        monkeypatch.setattr(service, "build_adapters", lambda: [_OneShotAdapter()])
        collected: list[NewswireEvent] = []
        await service.bus.subscribe(lambda e: collected.append(e))
        await service.start()
        await anyio.sleep(0.05)
        await service.stop()
        return collected

    collected = anyio.run(run)
    assert len(collected) == 1
    assert collected[0].symbols == ["ETH"]
