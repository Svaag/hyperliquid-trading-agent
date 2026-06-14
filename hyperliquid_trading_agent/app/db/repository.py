from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.db.models import (
    AuditEvent,
    CacheItem,
    ConversationMessage,
    ConversationThread,
    DecisionRoleOutput,
    DecisionRun,
    DecisionStateSnapshot,
    NewsItem,
    PaperTradeIdea,
    PaperTradeSnapshot,
    PositionTracker,
    ToolCall,
    TrackedLevel,
    TrackingEvent,
    TradeProposalRecord,
)
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.security import redact_secrets
from hyperliquid_trading_agent.app.tracking.schemas import PositionTrackingPlan

log = get_logger(__name__)


class Repository:
    """Async persistence facade for audit, cache, conversations, and paper trades."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None):
        self.sessionmaker = sessionmaker

    @property
    def enabled(self) -> bool:
        return self.sessionmaker is not None

    async def record_audit_event(self, event_type: str, actor: str = "", payload: dict[str, Any] | None = None) -> None:
        if self.sessionmaker is None:
            return
        payload = redact_secrets(payload or {})
        try:
            async with self.sessionmaker() as session:
                session.add(AuditEvent(event_type=event_type, actor=actor, payload=payload))
                await session.commit()
        except Exception as exc:  # pragma: no cover - best-effort audit logging must not break bot answers
            log.warning("audit_event_record_failed", event_type=event_type, error=type(exc).__name__)

    async def record_tool_call(
        self,
        tool_name: str,
        status: str,
        input_json: dict[str, Any] | None = None,
        output_json: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(
                    ToolCall(
                        tool_name=tool_name,
                        status=status,
                        input_json=redact_secrets(input_json or {}),
                        output_json=redact_secrets(output_json or {}),
                        latency_ms=latency_ms,
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("tool_call_record_failed", tool_name=tool_name, error=type(exc).__name__)

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(CacheItem, key)
                if item is None:
                    return None
                if item.expires_at and item.expires_at <= datetime.now(UTC):
                    await session.delete(item)
                    await session.commit()
                    return None
                return item.value
        except Exception as exc:  # pragma: no cover
            log.warning("cache_get_failed", key=key, error=type(exc).__name__)
            return None

    async def cache_set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if self.sessionmaker is None:
            return
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        try:
            async with self.sessionmaker() as session:
                existing = await session.get(CacheItem, key)
                if existing is None:
                    session.add(CacheItem(key=key, value=value, expires_at=expires_at))
                else:
                    existing.value = value
                    existing.expires_at = expires_at
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("cache_set_failed", key=key, error=type(exc).__name__)

    async def upsert_discord_thread(
        self,
        discord_guild_id: str | None,
        discord_channel_id: str | None,
        discord_thread_id: str | None,
        title: str,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(ConversationThread).where(ConversationThread.discord_thread_id == discord_thread_id)
            )
            thread = result.scalar_one_or_none()
            if thread is None:
                thread = ConversationThread(
                    discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                    discord_thread_id=discord_thread_id,
                    title=title[:255],
                )
                session.add(thread)
                await session.flush()
            await session.commit()
            return thread.id

    async def add_message(
        self,
        thread_id: str | None,
        role: str,
        content: str,
        discord_user_id: str | None = None,
    ) -> None:
        if self.sessionmaker is None or thread_id is None:
            return
        async with self.sessionmaker() as session:
            session.add(
                ConversationMessage(
                    thread_id=thread_id,
                    role=role,
                    content=content,
                    discord_user_id=discord_user_id,
                )
            )
            await session.commit()

    async def record_news_item(self, source: str, title: str, url: str, summary: str = "") -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(NewsItem(source=source, title=title, url=url, summary=summary, published_at=None))
                await session.commit()
        except Exception:  # pragma: no cover - duplicate/unavailable news persistence should not break answers
            return

    async def record_paper_trade(
        self,
        discord_user_id: str | None,
        coin: str,
        side: str,
        thesis: str,
        plan: dict[str, Any],
        market_snapshot: dict[str, Any] | None = None,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            idea = PaperTradeIdea(discord_user_id=discord_user_id, coin=coin, side=side, thesis=thesis, plan=plan)
            session.add(idea)
            await session.flush()
            session.add(PaperTradeSnapshot(idea_id=idea.id, market_snapshot=market_snapshot or {}))
            await session.commit()
            return idea.id

    async def create_decision_run(
        self,
        prompt: str,
        route: dict[str, Any],
        selected_roles: list[str],
        actor: str = "",
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                run = DecisionRun(
                    actor=actor,
                    prompt=str(redact_secrets(prompt)),
                    route=redact_secrets(route),
                    selected_roles=selected_roles,
                    status="started",
                )
                session.add(run)
                await session.flush()
                await session.commit()
                return run.id
        except Exception as exc:  # pragma: no cover
            log.warning("decision_run_create_failed", error=type(exc).__name__)
            return None

    async def update_decision_run_context(self, run_id: str | None, context_snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                run = await session.get(DecisionRun, run_id)
                if run is not None:
                    run.context_snapshot = redact_secrets(context_snapshot)
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_run_context_update_failed", run_id=run_id, error=type(exc).__name__)

    async def record_decision_role_output(
        self,
        run_id: str | None,
        role: str,
        round_index: int,
        model: str | None,
        provider: str | None,
        status: str,
        output_json: dict[str, Any],
        raw_content: str = "",
        latency_ms: int | None = None,
    ) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(
                    DecisionRoleOutput(
                        run_id=run_id,
                        role=role,
                        round_index=round_index,
                        model=model,
                        provider=provider,
                        status=status,
                        output_json=redact_secrets(output_json),
                        raw_content=str(redact_secrets(raw_content)),
                        latency_ms=latency_ms,
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_role_output_record_failed", role=role, error=type(exc).__name__)

    async def record_decision_state_snapshot(
        self,
        run_id: str | None,
        round_index: int,
        node: str,
        state_json: dict[str, Any],
    ) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(
                    DecisionStateSnapshot(
                        run_id=run_id,
                        round_index=round_index,
                        node=node,
                        state_json=redact_secrets(state_json),
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_state_snapshot_record_failed", node=node, error=type(exc).__name__)

    async def complete_decision_run(
        self,
        run_id: str | None,
        status: str,
        round_count: int,
        final_summary: str = "",
        proposal_id: str | None = None,
    ) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                run = await session.get(DecisionRun, run_id)
                if run is not None:
                    run.status = status
                    run.round_count = round_count
                    run.final_summary = final_summary
                    run.proposal_id = proposal_id
                    run.completed_at = datetime.now(UTC)
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_run_complete_failed", run_id=run_id, error=type(exc).__name__)

    async def record_trade_proposal(
        self,
        run_id: str | None,
        status: str,
        coin: str | None,
        side: str | None,
        proposal: dict[str, Any],
        content: str = "",
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = TradeProposalRecord(
                    run_id=run_id,
                    status=status,
                    coin=coin,
                    side=side,
                    proposal_json=redact_secrets(proposal),
                    content=str(redact_secrets(content)),
                )
                session.add(item)
                await session.flush()
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("trade_proposal_record_failed", error=type(exc).__name__)
            return None

    async def get_trade_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(TradeProposalRecord, proposal_id)
                if item is None:
                    return None
                return {
                    "id": item.id,
                    "run_id": item.run_id,
                    "status": item.status,
                    "coin": item.coin,
                    "side": item.side,
                    "proposal": item.proposal_json,
                    "content": item.content,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
        except Exception as exc:  # pragma: no cover
            log.warning("trade_proposal_get_failed", proposal_id=proposal_id, error=type(exc).__name__)
            return None

    async def create_position_tracker(self, plan: PositionTrackingPlan, proposal_id: str | None = None, run_id: str | None = None) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            plan_json = redact_secrets(plan.model_dump(mode="json"))
            async with self.sessionmaker() as session:
                tracker = PositionTracker(
                    id=plan.id,
                    proposal_id=proposal_id or plan.proposal_id,
                    run_id=run_id or plan.run_id,
                    source=str(plan.metadata.get("source") or "auto_high_stakes"),
                    status=plan.status,
                    coin=plan.coin.upper(),
                    side=plan.side,
                    entry_px=plan.entry,
                    stop_px=plan.stop,
                    take_profit_px=plan.take_profit,
                    current_px=plan.current_price_at_arm,
                    last_px=None,
                    last_price_at_ms=None,
                    price_source=plan.price_source,
                    expires_at=_datetime_from_ms(plan.expires_at_ms),
                    discord_guild_id=plan.discord_guild_id,
                    discord_channel_id=plan.discord_channel_id,
                    discord_thread_id=plan.discord_thread_id,
                    discord_user_id=plan.discord_user_id,
                    plan_json=plan_json,
                    metadata_json=redact_secrets(plan.metadata),
                    updated_at=datetime.now(UTC),
                )
                session.add(tracker)
                for level in plan.levels:
                    session.add(
                        TrackedLevel(
                            id=level.id,
                            tracker_id=plan.id,
                            kind=level.kind,
                            label=level.label,
                            price=level.price,
                            direction=level.direction,
                            terminal=level.terminal,
                            severity=level.severity,
                            armed=level.armed,
                            hit_count=level.hit_count,
                            rearm_band_bps=level.rearm_band_bps,
                            metadata_json=redact_secrets(level.metadata),
                        )
                    )
                await session.commit()
                return tracker.id
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_create_failed", coin=plan.coin, error=type(exc).__name__)
            return None

    async def get_active_position_trackers(self) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(select(PositionTracker).where(PositionTracker.status.in_(["pending", "active"])))
                trackers = list(result.scalars().all())
                return [await self._tracker_to_dict(session, item) for item in trackers]
        except Exception as exc:  # pragma: no cover
            log.warning("active_position_trackers_get_failed", error=type(exc).__name__)
            return []

    async def get_position_tracker(self, tracker_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(PositionTracker, tracker_id)
                return await self._tracker_to_dict(session, item) if item is not None else None
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_get_failed", tracker_id=tracker_id, error=type(exc).__name__)
            return None

    async def list_position_trackers(
        self,
        status: str | None = None,
        coin: str | None = None,
        discord_thread_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                stmt = select(PositionTracker).order_by(PositionTracker.created_at.desc()).limit(limit)
                if status:
                    stmt = stmt.where(PositionTracker.status == status)
                if coin:
                    stmt = stmt.where(PositionTracker.coin == coin.upper())
                if discord_thread_id:
                    stmt = stmt.where(PositionTracker.discord_thread_id == discord_thread_id)
                result = await session.execute(stmt)
                trackers = list(result.scalars().all())
                return [await self._tracker_to_dict(session, item) for item in trackers]
        except Exception as exc:  # pragma: no cover
            log.warning("position_trackers_list_failed", error=type(exc).__name__)
            return []

    async def update_position_tracker_price(self, tracker_id: str, current_px: float, previous_px: float | None, timestamp_ms: int) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                tracker = await session.get(PositionTracker, tracker_id)
                if tracker is not None:
                    tracker.last_px = previous_px
                    tracker.current_px = current_px
                    tracker.last_price_at_ms = timestamp_ms
                    tracker.updated_at = datetime.now(UTC)
                    if tracker.status == "pending":
                        tracker.status = "active"
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_price_update_failed", tracker_id=tracker_id, error=type(exc).__name__)

    async def update_tracked_level_state(self, level_id: str, armed: bool, hit_count: int, last_triggered_at: datetime | None = None) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                level = await session.get(TrackedLevel, level_id)
                if level is not None:
                    level.armed = armed
                    level.hit_count = hit_count
                    level.last_triggered_at = last_triggered_at
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("tracked_level_update_failed", level_id=level_id, error=type(exc).__name__)

    async def set_position_tracker_status(self, tracker_id: str, status: str, reason: str = "") -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                tracker = await session.get(PositionTracker, tracker_id)
                if tracker is not None:
                    tracker.status = status
                    tracker.updated_at = datetime.now(UTC)
                    if status in {"completed", "expired", "stopped", "error"}:
                        tracker.completed_at = datetime.now(UTC)
                    await session.commit()
            if reason:
                await self.record_tracking_event(tracker_id=tracker_id, event_type=f"tracker_{status}", coin="", payload={"reason": reason})
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_status_update_failed", tracker_id=tracker_id, status=status, error=type(exc).__name__)

    async def record_tracking_event(
        self,
        tracker_id: str,
        event_type: str,
        coin: str,
        price: float | None = None,
        level_id: str | None = None,
        payload: dict[str, Any] | None = None,
        alert_destination: str | None = None,
        alert_status: str | None = None,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                event = TrackingEvent(
                    tracker_id=tracker_id,
                    level_id=level_id,
                    event_type=event_type,
                    coin=coin.upper() if coin else "",
                    price=price,
                    payload_json=redact_secrets(payload or {}),
                    alert_destination=alert_destination,
                    alert_status=alert_status,
                )
                session.add(event)
                await session.flush()
                await session.commit()
                return event.id
        except Exception as exc:  # pragma: no cover
            log.warning("tracking_event_record_failed", tracker_id=tracker_id, event_type=event_type, error=type(exc).__name__)
            return None

    async def list_tracking_events(self, tracker_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(select(TrackingEvent).where(TrackingEvent.tracker_id == tracker_id).order_by(TrackingEvent.created_at.desc()).limit(limit))
                return [_event_to_dict(item) for item in result.scalars().all()]
        except Exception as exc:  # pragma: no cover
            log.warning("tracking_events_list_failed", tracker_id=tracker_id, error=type(exc).__name__)
            return []

    async def _tracker_to_dict(self, session: AsyncSession, tracker: PositionTracker) -> dict[str, Any]:
        levels_result = await session.execute(select(TrackedLevel).where(TrackedLevel.tracker_id == tracker.id).order_by(TrackedLevel.created_at.asc()))
        events_result = await session.execute(select(TrackingEvent).where(TrackingEvent.tracker_id == tracker.id).order_by(TrackingEvent.created_at.desc()).limit(20))
        return {
            "id": tracker.id,
            "proposal_id": tracker.proposal_id,
            "run_id": tracker.run_id,
            "source": tracker.source,
            "status": tracker.status,
            "coin": tracker.coin,
            "side": tracker.side,
            "entry": tracker.entry_px,
            "stop": tracker.stop_px,
            "take_profit": tracker.take_profit_px,
            "current_price": tracker.current_px,
            "last_price": tracker.last_px,
            "last_price_at_ms": tracker.last_price_at_ms,
            "price_source": tracker.price_source,
            "expires_at": tracker.expires_at.isoformat() if tracker.expires_at else None,
            "completed_at": tracker.completed_at.isoformat() if tracker.completed_at else None,
            "discord_guild_id": tracker.discord_guild_id,
            "discord_channel_id": tracker.discord_channel_id,
            "discord_thread_id": tracker.discord_thread_id,
            "discord_user_id": tracker.discord_user_id,
            "plan": tracker.plan_json,
            "metadata": tracker.metadata_json,
            "levels": [_level_to_dict(item) for item in levels_result.scalars().all()],
            "recent_events": [_event_to_dict(item) for item in events_result.scalars().all()],
            "created_at": tracker.created_at.isoformat() if tracker.created_at else None,
            "updated_at": tracker.updated_at.isoformat() if tracker.updated_at else None,
        }


def _datetime_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _level_to_dict(level: TrackedLevel) -> dict[str, Any]:
    return {
        "id": level.id,
        "tracker_id": level.tracker_id,
        "kind": level.kind,
        "label": level.label,
        "price": level.price,
        "direction": level.direction,
        "terminal": level.terminal,
        "severity": level.severity,
        "armed": level.armed,
        "hit_count": level.hit_count,
        "rearm_band_bps": level.rearm_band_bps,
        "last_triggered_at": level.last_triggered_at.isoformat() if level.last_triggered_at else None,
        "metadata": level.metadata_json,
        "created_at": level.created_at.isoformat() if level.created_at else None,
    }


def _event_to_dict(event: TrackingEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tracker_id": event.tracker_id,
        "level_id": event.level_id,
        "event_type": event.event_type,
        "coin": event.coin,
        "price": event.price,
        "payload": event.payload_json,
        "alert_destination": event.alert_destination,
        "alert_status": event.alert_status,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
