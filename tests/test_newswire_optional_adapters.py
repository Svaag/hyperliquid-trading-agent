from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import anyio
import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.news.rss import NewsItem, RssFetchResult, fetch_rss_feed_sync
from hyperliquid_trading_agent.app.news.x_api import parse_recent_search_payload
from hyperliquid_trading_agent.app.newswire.adapters import rss as rss_adapter_module
from hyperliquid_trading_agent.app.newswire.adapters.rss import RssAdapter, feed_source
from hyperliquid_trading_agent.app.newswire.adapters.trading_economics_ws import TradingEconomicsAdapter
from hyperliquid_trading_agent.app.newswire.adapters.x_curated import XCuratedAdapter
from hyperliquid_trading_agent.app.newswire.schemas import RawNewsItem

FIXTURES = Path(__file__).parent / "fixtures" / "newswire"


def _json_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_trading_economics_official_calendar_shape_maps_forecast_and_stable_updates() -> None:
    adapter = TradingEconomicsAdapter(ws_url="wss://stream.tradingeconomics.com/", api_key="redacted")
    payload = _json_fixture("trading_economics_calendar.json")

    created = adapter._to_raw(payload)
    duplicate = adapter._to_raw(payload)
    corrected_payload = {**payload, "actual": "3.7%", "importance": "2"}
    updated = adapter._to_raw(corrected_payload)

    assert created is not None
    assert created.external_id == "324314"
    assert created.action == "created"
    assert "actual 3.8% vs forecast 3.9%" in created.headline
    assert "importance: 1" in created.body
    assert duplicate is None
    assert updated is not None
    assert updated.action == "updated"
    assert updated.external_id == created.external_id
    assert adapter.status()["duplicates_dropped"] == 1
    assert adapter.status()["updates_emitted"] == 1


def test_trading_economics_error_detail_redacts_client_credentials() -> None:
    adapter = TradingEconomicsAdapter(ws_url="wss://stream.tradingeconomics.com/", api_key="private:key")
    assert "private:key" not in adapter.safe_error_detail(RuntimeError("connect ?client=private:key failed"))


def test_x_recent_search_fixture_preserves_username_and_edit_identity() -> None:
    items = parse_recent_search_payload(_json_fixture("x_recent_search.json"))

    assert len(items) == 1
    assert items[0]["author_username"] == "redacted_source"
    assert items[0]["canonical_id"] == "2000000000000000001"
    assert items[0]["id"] == "2000000000000000002"
    assert len(items[0]["edit_history_tweet_ids"]) == 2


class _FakeXClient:
    enabled = True

    def __init__(self, batches: list[list[dict[str, Any]]]):
        self.batches = batches

    async def search_recent(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        assert query.startswith("(")
        assert query.endswith(") -is:retweet")
        assert max_results == 25
        return self.batches.pop(0)


def test_x_curated_query_and_update_semantics_are_deterministic() -> None:
    initial = parse_recent_search_payload(_json_fixture("x_recent_search.json"))[0]
    revised = deepcopy(initial)
    revised["id"] = "2000000000000000003"
    revised["edit_history_tweet_ids"].append(revised["id"])
    revised["text"] = "Corrected curated market update for $BTC"
    client = _FakeXClient([[initial], [initial], [revised]])
    settings = Settings(
        _env_file=None,
        x_bearer_token="redacted",
        x_watchlist_user_ids="@redacted_source,invalid-user!",
        x_cashtags="$btc,ETH",
        x_min_public_metric_score=0,
    )
    adapter = XCuratedAdapter(settings=settings, x_client=client)  # type: ignore[arg-type]
    emitted: list[RawNewsItem] = []

    async def capture(item: RawNewsItem) -> None:
        emitted.append(item)

    async def run() -> None:
        await adapter._poll(capture)
        await adapter._poll(capture)
        await adapter._poll(capture)

    anyio.run(run)

    query = adapter.build_query()
    assert "from:redacted_source" in query
    assert "invalid-user" not in query
    assert "($BTC" not in query
    assert len(query) <= 512
    assert [item.action for item in emitted] == ["created", "updated"]
    assert emitted[0].external_id == emitted[1].external_id == "2000000000000000001"
    assert adapter.status()["duplicates_dropped"] == 1
    assert "requires X compliance stream" in adapter.status()["delete_semantics"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_title"),
    [
        ("globenewswire_rss.xml", "acquisition agreement"),
        ("ecb_press_rss.xml", "monetary policy"),
        ("businesswire_rss.xml", "quarterly results"),
    ],
)
def test_redacted_rss_fixtures_parse(fixture_name: str, expected_title: str) -> None:
    result = fetch_rss_feed_sync((FIXTURES / fixture_name).as_uri(), limit=5)

    assert result.ok is True
    assert len(result.items) == 1
    assert expected_title in result.items[0].title


def test_optional_rss_source_mapping_uses_canonical_providers() -> None:
    assert feed_source("https://www.ecb.europa.eu/rss/press.html") == ("ecb", "macro")
    assert feed_source("https://www.globenewswire.com/RssFeed/subjectcode/27") == (
        "globe_newswire",
        "press_release",
    )
    assert feed_source("https://www.businesswire.com/custom/feed.rss") == (
        "business_wire",
        "press_release",
    )


def test_rss_adapter_isolates_one_failed_feed_and_exposes_per_feed_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    good_url = "https://www.ecb.europa.eu/rss/press.html"
    bad_url = "https://broken.example.invalid/private?token=secret"

    async def fake_fetch(
        url: str,
        limit: int = 5,
        *,
        user_agent: str | None = None,
    ) -> RssFetchResult:
        assert user_agent == "agent@example.com"
        if url == bad_url:
            return RssFetchResult(feed_url=url, items=[], ok=False, error="http_status:503", http_status=503)
        return RssFetchResult(
            feed_url=url,
            items=[
                NewsItem(
                    title="ECB policy update",
                    link="https://www.ecb.europa.eu/press/pr/date/2026/html/test.en.html",
                    source=url,
                    published="Fri, 10 Jul 2026 05:30:00 GMT",
                    summary="Policy update",
                )
            ],
            ok=True,
            http_status=200,
        )

    monkeypatch.setattr(rss_adapter_module, "fetch_rss_feed", fake_fetch)
    adapter = RssAdapter([bad_url, good_url], user_agent="agent@example.com")
    emitted: list[RawNewsItem] = []

    async def run() -> None:
        async def capture(item: RawNewsItem) -> None:
            emitted.append(item)

        await adapter._poll_once(capture)

    anyio.run(run)

    assert len(emitted) == 1
    assert emitted[0].source == "ecb"
    assert adapter.status()["items_emitted"] == 1
    health = adapter.status()["feed_health"]
    bad_key = next(key for key in health if key.startswith("broken.example.invalid"))
    assert health[bad_key]["errors"] == 1
    assert adapter.status()["user_agent_configured"] is True
    assert "token" not in bad_key
    assert "secret" not in json.dumps(adapter.status())
