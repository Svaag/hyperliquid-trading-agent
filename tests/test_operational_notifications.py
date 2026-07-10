from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import anyio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from hyperliquid_trading_agent.app.db.models import EngineOperatorProposalRow, OperationalNotificationRow
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.workers.operational_notification_pump import OperationalNotificationPump


class _RecordingSink:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.messages: list[dict[str, Any]] = []

    async def send(
        self,
        channel_id: str,
        content: str,
        embeds: list[dict[str, Any]] | None = None,
        components: list[dict[str, Any]] | None = None,
    ) -> str | None:
        self.messages.append(
            {
                "channel_id": channel_id,
                "content": content,
                "embeds": embeds,
                "components": components,
            }
        )
        if self.fail:
            raise RuntimeError("transport_down")
        return f"message-{len(self.messages)}"


async def _repository() -> tuple[Repository, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(OperationalNotificationRow.__table__.create)
        await connection.run_sync(EngineOperatorProposalRow.__table__.create)
    return Repository(async_sessionmaker(engine, expire_on_commit=False)), engine


def test_operational_notification_outbox_deduplicates_leases_and_sends() -> None:
    async def run() -> dict[str, Any]:
        repo, engine = await _repository()
        first_id = await repo.enqueue_operational_notification(
            dedupe_key="engine-proposal:sig-1",
            category="engine_proposal",
            source_type="engine_signal",
            source_id="sig-1",
            channel_id="123",
            scheduled_at_ms=1_000,
            payload={"content": "Trade-ready proposal"},
        )
        duplicate_id = await repo.enqueue_operational_notification(
            dedupe_key="engine-proposal:sig-1",
            category="engine_proposal",
            channel_id="123",
            scheduled_at_ms=1_000,
            payload={"content": "Updated proposal"},
        )
        claimed = await repo.claim_due_operational_notifications(now_ms=1_000, lease_ms=100)
        duplicate_claim = await repo.claim_due_operational_notifications(now_ms=1_050, lease_ms=100)
        await repo.mark_operational_notification_sent(str(first_id), message_id="discord-1", now_ms=1_100)
        status = await repo.operational_notification_status()
        await engine.dispose()
        return {
            "first_id": first_id,
            "duplicate_id": duplicate_id,
            "claimed": claimed,
            "duplicate_claim": duplicate_claim,
            "status": status,
        }

    result = anyio.run(run)

    assert result["first_id"] == result["duplicate_id"]
    assert len(result["claimed"]) == 1
    assert result["claimed"][0]["payload"]["content"] == "Updated proposal"
    assert result["duplicate_claim"] == []
    assert result["status"]["counts"] == {"sent": 1}
    assert result["status"]["last_sent_category"] == "engine_proposal"


def test_operational_notification_outbox_recovers_stale_lease_and_dead_letters() -> None:
    async def run() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        repo, engine = await _repository()
        notification_id = await repo.enqueue_operational_notification(
            dedupe_key="monitor:alert-1",
            category="engine_monitor_alert",
            channel_id="123",
            scheduled_at_ms=1_000,
            payload={"content": "Monitor alert"},
        )
        await repo.claim_due_operational_notifications(now_ms=1_000, lease_ms=100)
        before_expiry = await repo.claim_due_operational_notifications(now_ms=1_099, lease_ms=100)
        recovered = await repo.claim_due_operational_notifications(now_ms=1_100, lease_ms=100)
        await repo.mark_operational_notification_failed(
            str(notification_id),
            error="first_failure",
            now_ms=1_100,
            max_attempts=2,
        )
        retried = await repo.claim_due_operational_notifications(now_ms=6_100, lease_ms=100)
        await repo.mark_operational_notification_failed(
            str(notification_id),
            error="second_failure",
            now_ms=6_100,
            max_attempts=2,
        )
        status = await repo.operational_notification_status()
        await engine.dispose()
        return before_expiry, recovered + retried, status

    before_expiry, claims, status = anyio.run(run)

    assert before_expiry == []
    assert len(claims) == 2
    assert status["counts"] == {"dead_letter": 1}


def test_operational_notification_pump_delivers_and_records_transport_failure() -> None:
    async def run() -> tuple[_RecordingSink, dict[str, Any], dict[str, Any]]:
        repo, engine = await _repository()
        await repo.enqueue_operational_notification(
            dedupe_key="digest:one",
            category="shadow_digest",
            channel_id="123",
            payload={"content": "Shadow digest", "embeds": [{"title": "Top candidates"}]},
        )
        sink = _RecordingSink()
        pump = OperationalNotificationPump(repository=repo, sink=sink)
        assert await pump.run_once() == 1
        delivered_status = await repo.operational_notification_status()

        await repo.enqueue_operational_notification(
            dedupe_key="digest:two",
            category="shadow_digest",
            channel_id="123",
            payload={"content": "Second digest"},
        )
        failing_sink = _RecordingSink(fail=True)
        failing_pump = OperationalNotificationPump(repository=repo, sink=failing_sink)
        assert await failing_pump.run_once() == 1
        failed_status = await repo.operational_notification_status()
        await engine.dispose()
        return sink, delivered_status, failed_status

    sink, delivered_status, failed_status = anyio.run(run)

    assert sink.messages[0]["content"] == "Shadow digest"
    assert delivered_status["counts"] == {"sent": 1}
    assert failed_status["counts"] == {"failed": 1, "sent": 1}


def test_operational_notification_migration_creates_outbox() -> None:
    engine = create_engine("sqlite://")
    spec = spec_from_file_location(
        "migration_0027_operational_notification_outbox",
        Path("alembic/versions/0027_operational_notification_outbox.py"),
    )
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("operational_notification_outbox")}

    assert "operational_notification_outbox" in inspector.get_table_names()
    assert "engine_operator_proposals" in inspector.get_table_names()
    assert {"notification_id", "dedupe_key", "status", "lease_expires_at_ms", "payload_json"} <= columns


def test_engine_operator_proposal_lifecycle_is_acknowledgment_only() -> None:
    async def run() -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
        repo, engine = await _repository()
        proposal_id = await repo.upsert_engine_operator_proposal(
            {
                "proposal_id": "sig_eng_test",
                "candidate_id": "cand_test",
                "packet_id": "packet_test",
                "council_review_id": "council_test",
                "strategy_id": "funding_carry_v1",
                "asset": "BTC",
                "side": "long",
                "score": 88,
                "confidence": 0.8,
                "net_ev_bps": 15,
                "risk_adjusted_utility": 0.5,
                "feature_coverage_pct": 100,
                "allocated_notional_usd": 10_000,
                "created_at_ms": 1_000,
                "expires_at_ms": 10_000,
                "payload": {
                    "signal": {
                        "id": "sig_eng_test",
                        "metadata": {"acknowledgment_only": True},
                    }
                },
                "metadata": {"paper_execution_allowed": False},
                "updated_at_ms": 1_000,
            }
        )
        before = await repo.get_engine_operator_proposal("sig_eng_test")
        acknowledged = await repo.update_engine_operator_proposal_status(
            "sig_eng_test",
            status="acknowledged",
            actor="operator-1",
            now_ms=2_000,
        )
        terminal = await repo.update_engine_operator_proposal_status(
            "sig_eng_test",
            status="rejected",
            actor="operator-2",
            reason="late rejection",
            now_ms=3_000,
        )
        status = await repo.engine_operator_proposal_status()
        await engine.dispose()
        return proposal_id, before, acknowledged, {"terminal": terminal, "status": status}

    proposal_id, before, acknowledged, result = anyio.run(run)

    assert proposal_id == "sig_eng_test"
    assert before is not None and before["status"] == "proposed"
    assert acknowledged is not None and acknowledged["status"] == "acknowledged"
    assert acknowledged["acknowledged_by"] == "operator-1"
    assert result["terminal"]["status"] == "acknowledged"
    assert result["status"]["counts"] == {"acknowledged": 1}
    assert result["terminal"]["metadata"]["paper_execution_allowed"] is False
