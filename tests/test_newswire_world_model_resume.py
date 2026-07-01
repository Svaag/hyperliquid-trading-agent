from __future__ import annotations

from pathlib import Path

import anyio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hyperliquid_trading_agent.app.db.models import ConsumerOffsetRecord, NewswireEventRow
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent
from hyperliquid_trading_agent.app.workers.stored_newswire_pump import StoredNewswirePump


async def _repo(tmp_path: Path) -> Repository:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resume.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(ConsumerOffsetRecord.__table__.create)
        await conn.run_sync(NewswireEventRow.__table__.create)
    return Repository(async_sessionmaker(engine, expire_on_commit=False))


async def _record(repo: Repository, event_id: str, ts: int) -> None:
    await repo.record_newswire_event(
        {
            "event_id": event_id,
            "source": "alpaca",
            "provider": "benzinga",
            "transport": "websocket",
            "headline": f"headline {event_id}",
            "body": "body",
            "symbols": ["BTC"],
            "asset_class": "crypto",
            "event_type": "headline",
            "received_at_ms": ts,
            "published_at_ms": ts,
        }
    )


def test_stored_newswire_pump_resumes_after_same_timestamp_batch_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        repo = await _repo(tmp_path)
        for event_id in ["nw_a", "nw_b", "nw_c"]:
            await _record(repo, event_id, 100)
        seen: list[str] = []

        async def callback(event: NewswireEvent) -> None:
            seen.append(event.event_id)

        pump = StoredNewswirePump(consumer_name="world_model:newswire", repository=repo, callbacks=[callback], batch_size=2)
        assert await pump.run_once() == 2
        offset = await repo.get_consumer_offset("world_model:newswire")
        assert offset["last_event_id"] == "nw_b"
        assert offset["last_event_ts_ms"] == 100

        resumed = StoredNewswirePump(consumer_name="world_model:newswire", repository=repo, callbacks=[callback], batch_size=10)
        assert await resumed.run_once() == 1
        assert seen == ["nw_a", "nw_b", "nw_c"]
        assert await resumed.run_once() == 0

    anyio.run(run)


def test_stored_newswire_pump_bootstraps_from_latest_without_replaying_history(tmp_path: Path) -> None:
    async def run() -> None:
        repo = await _repo(tmp_path)
        await _record(repo, "nw_a", 100)
        await _record(repo, "nw_b", 200)
        seen: list[str] = []

        async def callback(event: NewswireEvent) -> None:
            seen.append(event.event_id)

        pump = StoredNewswirePump(consumer_name="trader:engine_newswire", repository=repo, callbacks=[callback], bootstrap_from_latest=True)
        assert await pump.run_once() == 0
        assert seen == []
        offset = await repo.get_consumer_offset("trader:engine_newswire")
        assert offset["last_event_id"] == "nw_b"
        assert offset["last_event_ts_ms"] == 200
        assert offset["metadata"]["bootstrap_from_latest"] is True
        assert offset["metadata"]["reason"] == "avoid_historical_news_regime_pollution"

        await _record(repo, "nw_c", 300)
        assert await pump.run_once() == 1
        assert seen == ["nw_c"]

    anyio.run(run)


def test_stored_newswire_pump_skips_invalid_rows_and_continues(tmp_path: Path) -> None:
    async def run() -> None:
        repo = await _repo(tmp_path)
        await _record(repo, "nw_a", 100)
        await repo.record_newswire_event(
            {
                "event_id": "nw_bad",
                "source": "alpaca",
                "provider": "benzinga",
                "transport": "websocket",
                "headline": "bad sentiment row",
                "symbols": ["BTC"],
                "asset_class": "crypto",
                "event_type": "headline",
                "received_at_ms": 200,
                "published_at_ms": 200,
                "sentiment": "positive",
            }
        )
        await _record(repo, "nw_c", 300)
        seen: list[str] = []

        async def callback(event: NewswireEvent) -> None:
            seen.append(event.event_id)

        pump = StoredNewswirePump(consumer_name="world_model:newswire", repository=repo, callbacks=[callback], batch_size=10)
        assert await pump.run_once() == 3
        assert seen == ["nw_a", "nw_c"]
        assert pump.error_count == 1
        assert pump.invalid_rows_skipped == 1
        offset = await repo.get_consumer_offset("world_model:newswire")
        assert offset["last_event_id"] == "nw_c"

    anyio.run(run)


def test_stored_newswire_pump_does_not_advance_offset_past_failed_event(tmp_path: Path) -> None:
    async def run() -> None:
        repo = await _repo(tmp_path)
        for event_id in ["nw_a", "nw_b", "nw_c"]:
            await _record(repo, event_id, 100)
        seen: list[str] = []

        async def flaky(event: NewswireEvent) -> None:
            if event.event_id == "nw_b":
                raise RuntimeError("boom")
            seen.append(event.event_id)

        pump = StoredNewswirePump(consumer_name="world_model:newswire", repository=repo, callbacks=[flaky], batch_size=10)
        assert await pump.run_once() == 1
        offset = await repo.get_consumer_offset("world_model:newswire")
        assert offset["last_event_id"] == "nw_a"

        async def ok(event: NewswireEvent) -> None:
            seen.append(event.event_id)

        resumed = StoredNewswirePump(consumer_name="world_model:newswire", repository=repo, callbacks=[ok], batch_size=10)
        assert await resumed.run_once() == 2
        assert seen == ["nw_a", "nw_b", "nw_c"]

    anyio.run(run)
