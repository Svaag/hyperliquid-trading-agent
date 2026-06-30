"""Phase 2 tests: history query, durable stats, export serializers, rate limiter.

DB-backed query/stats/stream tests run against an in-memory async-SQLite engine
seeded directly (the read queries are dialect-neutral; only persist() is
Postgres-only). Pure-logic tests (serializers, cursor, limiter) need no DB.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hyperliquid_trading_agent.app.db.models import LiquidationEventRecord
from hyperliquid_trading_agent.app.liquidations import export
from hyperliquid_trading_agent.app.liquidations.ratelimit import RateLimiter, client_ip
from hyperliquid_trading_agent.app.liquidations.store import LiquidationStore, decode_cursor, encode_cursor

# --------------------------------------------------------------------- helpers


def _rec(event_id: str, *, ts: int, venue: str = "lighter", symbol: str = "BTC",
         side: str = "long", notional: float = 1000.0, integrity: str = "confirmed",
         event_type: str = "liquidation", user: str | None = "0xVICTIMADDRESS0001") -> LiquidationEventRecord:
    return LiquidationEventRecord(
        event_id=event_id, venue=venue, source=f"{venue}_ws", source_integrity=integrity,
        event_type=event_type, symbol=symbol, liquidated_side=side, notional_usd=notional,
        price=100.0, size_base=notional / 100.0, timestamp_ms=ts, received_at_ms=ts,
        liquidated_user=user, confidence=1.0, raw_json={"secret": "private-provenance"},
    )


async def _seeded_store(records: list[LiquidationEventRecord]) -> LiquidationStore:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(LiquidationEventRecord.__table__.create)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add_all(records)
        await session.commit()
    return LiquidationStore(sessionmaker)


# ------------------------------------------------------------------ serializers


def test_csv_header_and_row_are_stable_and_redacted():
    header = export.format_csv_header().strip().split(",")
    assert header == list(export.EXPORT_COLUMNS)
    assert "raw" not in header and "raw_json" not in header
    row = {"event_id": "e1", "venue": "lighter", "notional_usd": 1234.5, "liquidated_user": "0xabcd…hash",
           "block_height": None, "symbol": "BTC"}
    line = export.format_csv_row(row)
    cells = line.strip().split(",")
    assert cells[export.EXPORT_COLUMNS.index("event_id")] == "e1"
    assert cells[export.EXPORT_COLUMNS.index("notional_usd")] == "1234.5"
    assert cells[export.EXPORT_COLUMNS.index("block_height")] == ""  # None -> empty cell, no column shift


def test_ndjson_row_round_trips():
    row = {"event_id": "e1", "notional_usd": 9.5, "venue": "aster"}
    parsed = json.loads(export.format_ndjson_row(row))
    assert parsed == row and export.format_ndjson_row(row).endswith("\n")


# ---------------------------------------------------------------------- cursor


def test_cursor_round_trips_and_rejects_garbage():
    token = encode_cursor(1_700_000_000_123, "hyperliquid:derived:BTC:42:long:1:2:abc")
    assert decode_cursor(token) == (1_700_000_000_123, "hyperliquid:derived:BTC:42:long:1:2:abc")
    assert decode_cursor("!!!not-base64!!!") is None


# ------------------------------------------------------------------ rate limiter


def test_rate_limiter_burst_then_429_then_refill():
    limiter = RateLimiter(rate_per_min=60, burst=2)  # 1 token/sec, burst 2
    assert limiter.check("ip", now=0.0)[0] is True
    assert limiter.check("ip", now=0.0)[0] is True
    allowed, retry_after = limiter.check("ip", now=0.0)
    assert allowed is False and retry_after > 0
    # one token refills after ~1s
    assert limiter.check("ip", now=1.0)[0] is True
    # a different key has its own bucket
    assert limiter.check("other", now=0.0)[0] is True


def test_client_ip_ignores_xff_unless_trusted():
    class _Req:
        def __init__(self, host, xff=None):
            self.client = type("C", (), {"host": host})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    untrusted = _Req("10.0.0.1", xff="1.2.3.4")
    assert client_ip(untrusted, trust_proxy=False) == "10.0.0.1"
    assert client_ip(untrusted, trust_proxy=True) == "1.2.3.4"


# ---------------------------------------------------------------- history query


async def test_history_keyset_pagination_and_redaction():
    records = [_rec(f"e{i}", ts=1_700_000_000_000 + i * 1000) for i in range(5)]
    store = await _seeded_store(records)

    page1 = await store.query(limit=2)
    assert [item["event_id"] for item in page1["items"]] == ["e4", "e3"]  # newest-first
    assert page1["next_cursor"] is not None
    # redaction: raw dropped, counterparty hashed (not the seeded raw address)
    assert "raw" not in page1["items"][0]
    assert page1["items"][0]["liquidated_user"].startswith("0xVICT") and "…" in page1["items"][0]["liquidated_user"]

    page2 = await store.query(limit=2, cursor=page1["next_cursor"])
    assert [item["event_id"] for item in page2["items"]] == ["e2", "e1"]
    page3 = await store.query(limit=2, cursor=page2["next_cursor"])
    assert [item["event_id"] for item in page3["items"]] == ["e0"]
    assert page3["next_cursor"] is None  # short page -> no more


async def test_history_filters():
    store = await _seeded_store([
        _rec("a", ts=1_000, venue="lighter", symbol="BTC"),
        _rec("b", ts=2_000, venue="aster", symbol="ETH"),
        _rec("c", ts=3_000, venue="lighter", symbol="ETH"),
    ])
    eth = await store.query(symbol="ETH")
    assert {item["event_id"] for item in eth["items"]} == {"b", "c"}
    lighter = await store.query(venue="lighter")
    assert {item["event_id"] for item in lighter["items"]} == {"a", "c"}


async def test_disabled_store_returns_empty():
    store = LiquidationStore(None)
    assert await store.query() == {"items": [], "next_cursor": None}
    assert [row async for row in store.stream_query(max_rows=10)] == []
    stats = await store.stats(since_ms=0, until_ms=1, bucket_ms=1)
    assert stats["series"] == [] and stats["count"] == 0


# ------------------------------------------------------------------ stream query


async def test_stream_query_respects_max_rows():
    store = await _seeded_store([_rec(f"e{i}", ts=1_000 + i) for i in range(10)])
    rows = [row async for row in store.stream_query(max_rows=3)]
    assert len(rows) == 3 and all("raw" not in row for row in rows)


# ------------------------------------------------------------------------ stats


async def test_stats_buckets_executions_only():
    base = 1_700_000_000_000
    store = await _seeded_store([
        _rec("a", ts=base + 0, side="long", notional=1000.0),
        _rec("b", ts=base + 30_000, side="short", notional=500.0),       # same 1m bucket as a
        _rec("c", ts=base + 90_000, side="long", notional=2000.0),       # next bucket
        _rec("p", ts=base + 10_000, integrity="derived",
             event_type="liquidation_pressure", notional=9_999_999.0),   # excluded from stats
    ])
    stats = await store.stats(since_ms=base, until_ms=base + 180_000, bucket_ms=60_000)
    assert stats["count"] == 3  # pressure excluded
    assert stats["total_notional_usd"] == 3500.0
    series = {row["t"]: row for row in stats["series"]}
    first = series[base]
    assert first["long"] == 1000.0 and first["short"] == 500.0 and first["total"] == 1500.0
    assert series[base + 60_000]["total"] == 2000.0
    assert stats["by_symbol"] == {"BTC": 3500.0}
    assert "derived" not in stats["by_integrity"]  # pressure never counted
