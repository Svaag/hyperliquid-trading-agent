from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.db.models import (
    AuditEvent,
    AutonomyEvent,
    AutonomyNewsEvent,
    CacheItem,
    CandidateLessonRecord,
    ConversationMessage,
    ConversationThread,
    DailyReportRecord,
    DecisionRoleOutput,
    DecisionRun,
    DecisionStateSnapshot,
    MarketAssetRecord,
    MarketLevelRecord,
    MarketObservation,
    MemoryObservationRecord,
    NewsItem,
    OperatorFeedbackRecord,
    OperatorOutputLessonRecord,
    PaperFillRecord,
    PaperOrderRecord,
    PaperPortfolioRecord,
    PaperPositionRecord,
    PaperTradeIdea,
    PaperTradeSnapshot,
    PortfolioSnapshotRecord,
    PositionTracker,
    RoleLessonRecord,
    ShadowRoleLessonRecord,
    SignalEvaluationMarkRecord,
    SignalEvaluationRecord,
    TokenCapitalSnapshotRecord,
    ToolCall,
    TrackedLevel,
    TrackingEvent,
    TradeProposalRecord,
    TradeSignalRecord,
    TuningProposalRecord,
    WeeklyReportRecord,
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

    async def get_recent_messages(self, thread_id: str | None, limit: int = 8) -> list[dict[str, Any]]:
        if self.sessionmaker is None or thread_id is None:
            return []
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.thread_id == thread_id)
                    .order_by(ConversationMessage.created_at.desc())
                    .limit(limit)
                )
                messages = list(reversed(result.scalars().all()))
                return [
                    {
                        "role": item.role,
                        "content": item.content,
                        "discord_user_id": item.discord_user_id,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    }
                    for item in messages
                ]
        except Exception as exc:  # pragma: no cover
            log.warning("recent_messages_get_failed", thread_id=thread_id, error=type(exc).__name__)
            return []

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

    async def update_trade_proposal(self, proposal_id: str, proposal: dict[str, Any], content: str = "") -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(TradeProposalRecord, proposal_id)
                if item is not None:
                    item.proposal_json = redact_secrets(proposal)
                    if content:
                        item.content = str(redact_secrets(content))
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("trade_proposal_update_failed", proposal_id=proposal_id, error=type(exc).__name__)

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

    async def set_position_tracker_expiry(self, tracker_id: str, expires_at_ms: int) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                tracker = await session.get(PositionTracker, tracker_id)
                if tracker is not None:
                    tracker.expires_at = _datetime_from_ms(expires_at_ms)
                    plan = dict(tracker.plan_json or {})
                    plan["expires_at_ms"] = expires_at_ms
                    tracker.plan_json = plan
                    tracker.updated_at = datetime.now(UTC)
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_expiry_update_failed", tracker_id=tracker_id, error=type(exc).__name__)

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

    async def record_autonomy_event(self, event_type: str, actor: str = "", symbol: str | None = None, payload: dict[str, Any] | None = None) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                event = AutonomyEvent(event_type=event_type, actor=actor, symbol=symbol.upper() if symbol else None, payload_json=redact_secrets(payload or {}))
                session.add(event)
                await session.flush()
                await session.commit()
                return event.id
        except Exception as exc:  # pragma: no cover
            log.warning("autonomy_event_record_failed", event_type=event_type, error=type(exc).__name__)
            return None

    async def upsert_market_asset(self, asset: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            symbol = str(asset["symbol"]).upper()
            item = await session.get(MarketAssetRecord, symbol)
            if item is None:
                item = MarketAssetRecord(symbol=symbol, display_name=str(asset.get("display_name") or symbol), kind=str(asset.get("kind") or "perp"), source=str(asset.get("source") or "top_volume"))
                session.add(item)
            item.display_name = str(asset.get("display_name") or symbol)
            item.kind = str(asset.get("kind") or "perp")
            item.source = str(asset.get("source") or "top_volume")
            item.dex = asset.get("dex")
            item.sz_decimals = asset.get("sz_decimals")
            item.max_leverage = asset.get("max_leverage")
            item.day_volume_usd = asset.get("day_volume_usd")
            item.metadata_json = redact_secrets(dict(asset.get("metadata") or {}))
            item.updated_at = datetime.now(UTC)
            await session.commit()

    async def list_market_assets(self) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(MarketAssetRecord).order_by(MarketAssetRecord.symbol.asc()))
            return [_market_asset_to_dict(item) for item in result.scalars().all()]

    async def record_market_observation(self, state: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = MarketObservation(
                    symbol=str(state.get("symbol") or "").upper(),
                    timestamp_ms=int(state.get("timestamp_ms") or 0),
                    mid=state.get("mid"),
                    mark=state.get("mark"),
                    oracle=state.get("oracle"),
                    funding_hourly=state.get("funding_hourly"),
                    open_interest=state.get("open_interest"),
                    day_volume_usd=state.get("day_volume_usd"),
                    features_json=redact_secrets(state),
                )
                session.add(item)
                await session.flush()
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("market_observation_record_failed", symbol=state.get("symbol"), error=type(exc).__name__)
            return None

    async def upsert_market_levels(self, levels: list[dict[str, Any]]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            for level in levels:
                item = await session.get(MarketLevelRecord, str(level["id"]))
                if item is None:
                    item = MarketLevelRecord(id=str(level["id"]), symbol=str(level.get("symbol") or "").upper(), kind=str(level.get("kind") or "support"), price=float(level.get("price") or 0), strength=float(level.get("strength") or 0), timeframe=str(level.get("timeframe") or ""), source=str(level.get("source") or "inferred"), first_seen_ms=int(level.get("first_seen_ms") or 0), last_seen_ms=int(level.get("last_seen_ms") or 0))
                    session.add(item)
                item.symbol = str(level.get("symbol") or item.symbol).upper()
                item.kind = str(level.get("kind") or item.kind)
                item.price = float(level.get("price") or item.price)
                item.strength = float(level.get("strength") or item.strength)
                item.timeframe = str(level.get("timeframe") or item.timeframe)
                item.source = str(level.get("source") or item.source)
                item.first_seen_ms = int(level.get("first_seen_ms") or item.first_seen_ms)
                item.last_seen_ms = int(level.get("last_seen_ms") or item.last_seen_ms)
                item.expires_at_ms = level.get("expires_at_ms")
                item.metadata_json = redact_secrets(dict(level.get("metadata") or {}))
            await session.commit()

    async def record_news_event(self, event: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                existing = await session.get(AutonomyNewsEvent, str(event["id"]))
                if existing is not None:
                    return existing.id
                item = AutonomyNewsEvent(
                    id=str(event["id"]),
                    provider=str(event.get("provider") or "unknown"),
                    source=str(event.get("source") or "unknown"),
                    title=str(event.get("title") or ""),
                    text=str(event.get("text") or ""),
                    url=event.get("url"),
                    author_id=event.get("author_id"),
                    created_at_ms=event.get("created_at_ms"),
                    observed_at_ms=int(event.get("observed_at_ms") or 0),
                    importance_score=float(event.get("importance_score") or 0),
                    sentiment=str(event.get("sentiment") or "unknown"),
                    assets_json=list(event.get("assets") or []),
                    metadata_json=redact_secrets(dict(event.get("metadata") or {})),
                )
                session.add(item)
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("autonomy_news_event_record_failed", error=type(exc).__name__)
            return None

    async def list_news_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(AutonomyNewsEvent).order_by(AutonomyNewsEvent.observed_at_ms.desc()).limit(limit))
            return [_news_event_to_dict(item) for item in result.scalars().all()]

    async def create_or_update_trade_signal(self, signal: dict[str, Any], approved_by: str | None = None, rejected_by: str | None = None) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TradeSignalRecord, str(signal["id"]))
            if item is None:
                item = TradeSignalRecord(id=str(signal["id"]), symbol=str(signal.get("symbol") or "").upper(), side=str(signal.get("side") or "long"), signal_type=str(signal.get("signal_type") or "unknown"), status=str(signal.get("status") or "candidate"), score=float(signal.get("score") or 0), confidence=float(signal.get("confidence") or 0), created_at_ms=int(signal.get("created_at_ms") or 0), expires_at_ms=int(signal.get("expires_at_ms") or 0), entry_px=float(signal.get("entry") or 0), stop_px=float(signal.get("stop") or 0))
                session.add(item)
            item.symbol = str(signal.get("symbol") or item.symbol).upper()
            item.side = str(signal.get("side") or item.side)
            item.signal_type = str(signal.get("signal_type") or item.signal_type)
            item.status = str(signal.get("status") or item.status)
            item.score = float(signal.get("score") or item.score)
            item.confidence = float(signal.get("confidence") or item.confidence)
            item.created_at_ms = int(signal.get("created_at_ms") or item.created_at_ms)
            item.expires_at_ms = int(signal.get("expires_at_ms") or item.expires_at_ms)
            item.entry_px = float(signal.get("entry") or item.entry_px)
            item.stop_px = float(signal.get("stop") or item.stop_px)
            item.take_profit_px = signal.get("take_profit")
            item.thesis = str(signal.get("thesis") or "")
            item.invalidation = str(signal.get("invalidation") or "")
            item.evidence_json = list(signal.get("evidence") or [])
            item.feature_snapshot_json = redact_secrets(dict(signal.get("feature_snapshot") or {}))
            item.risk_plan_json = redact_secrets(dict(signal.get("risk_plan") or {}))
            item.model_insight_json = redact_secrets(signal.get("model_insight")) if signal.get("model_insight") is not None else None
            item.discord_channel_id = signal.get("discord_channel_id")
            item.discord_message_id = signal.get("discord_message_id")
            if approved_by:
                item.approved_by_discord_user_id = approved_by
                item.approved_at = datetime.now(UTC)
            if rejected_by:
                item.rejected_by_discord_user_id = rejected_by
                item.rejected_at = datetime.now(UTC)
            await session.commit()

    async def get_autonomy_trade_signal(self, signal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(TradeSignalRecord, signal_id)
            return _trade_signal_to_dict(item) if item is not None else None

    async def list_autonomy_trade_signals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(TradeSignalRecord).order_by(TradeSignalRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(TradeSignalRecord.status == status)
            result = await session.execute(stmt)
            return [_trade_signal_to_dict(item) for item in result.scalars().all()]

    async def upsert_signal_evaluation(self, evaluation: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(SignalEvaluationRecord, str(evaluation["id"]))
                if item is None:
                    result = await session.execute(select(SignalEvaluationRecord).where(SignalEvaluationRecord.signal_id == str(evaluation["signal_id"])))
                    item = result.scalar_one_or_none()
                if item is None:
                    item = SignalEvaluationRecord(id=str(evaluation["id"]), signal_id=str(evaluation["signal_id"]), symbol=str(evaluation.get("symbol") or "").upper(), side=str(evaluation.get("side") or "long"), signal_type=str(evaluation.get("signal_type") or "unknown"), status=str(evaluation.get("status") or "open"), created_at_ms=int(evaluation.get("created_at_ms") or 0), entry_px=float(evaluation.get("entry") or 0), stop_px=float(evaluation.get("stop") or 0), signal_score=float(evaluation.get("signal_score") or 0), signal_confidence=float(evaluation.get("signal_confidence") or 0), signal_status_at_eval_start=str(evaluation.get("signal_status_at_eval_start") or "unknown"), terminal_outcome=str(evaluation.get("terminal_outcome") or "open"))
                    session.add(item)
                _apply_signal_evaluation(item, evaluation)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("signal_evaluation_upsert_failed", signal_id=evaluation.get("signal_id"), error=type(exc).__name__)

    async def upsert_signal_evaluation_mark(self, mark: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(SignalEvaluationMarkRecord, str(mark["id"]))
                if item is None:
                    result = await session.execute(select(SignalEvaluationMarkRecord).where(SignalEvaluationMarkRecord.signal_id == str(mark["signal_id"]), SignalEvaluationMarkRecord.horizon == str(mark["horizon"])))
                    item = result.scalar_one_or_none()
                if item is None:
                    item = SignalEvaluationMarkRecord(id=str(mark["id"]), evaluation_id=str(mark["evaluation_id"]), signal_id=str(mark["signal_id"]), symbol=str(mark.get("symbol") or "").upper(), horizon=str(mark.get("horizon") or ""), due_at_ms=int(mark.get("due_at_ms") or 0), status=str(mark.get("status") or "pending"))
                    session.add(item)
                _apply_signal_evaluation_mark(item, mark)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("signal_evaluation_mark_upsert_failed", signal_id=mark.get("signal_id"), horizon=mark.get("horizon"), error=type(exc).__name__)

    async def upsert_signal_evaluation_marks(self, marks: list[dict[str, Any]]) -> None:
        for mark in marks:
            await self.upsert_signal_evaluation_mark(mark)

    async def get_signal_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(SignalEvaluationRecord, evaluation_id)
            if item is None:
                return None
            return await self._signal_evaluation_to_dict(session, item)

    async def get_signal_evaluation_by_signal_id(self, signal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            result = await session.execute(select(SignalEvaluationRecord).where(SignalEvaluationRecord.signal_id == signal_id))
            item = result.scalar_one_or_none()
            if item is None:
                return None
            return await self._signal_evaluation_to_dict(session, item)

    async def list_signal_evaluations(self, status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(SignalEvaluationRecord).order_by(SignalEvaluationRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(SignalEvaluationRecord.status == status)
            if symbol:
                stmt = stmt.where(SignalEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._signal_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_open_signal_evaluations(self, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(SignalEvaluationRecord).where(SignalEvaluationRecord.status.in_(["open", "partial"])).order_by(SignalEvaluationRecord.created_at_ms.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(SignalEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._signal_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_due_signal_evaluation_marks(self, now_ms: int, limit: int = 500) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(SignalEvaluationMarkRecord).where(SignalEvaluationMarkRecord.status == "pending", SignalEvaluationMarkRecord.due_at_ms <= now_ms).order_by(SignalEvaluationMarkRecord.due_at_ms.asc()).limit(limit))
            return [_signal_evaluation_mark_to_dict(item) for item in result.scalars().all()]

    async def list_signal_evaluation_marks(self, evaluation_id: str | None = None, signal_id: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(SignalEvaluationMarkRecord).order_by(SignalEvaluationMarkRecord.due_at_ms.asc())
            if evaluation_id:
                stmt = stmt.where(SignalEvaluationMarkRecord.evaluation_id == evaluation_id)
            if signal_id:
                stmt = stmt.where(SignalEvaluationMarkRecord.signal_id == signal_id)
            result = await session.execute(stmt)
            return [_signal_evaluation_mark_to_dict(item) for item in result.scalars().all()]

    async def record_memory_observation(self, observation: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = MemoryObservationRecord(id=str(observation["id"]), source_type=str(observation.get("source_type") or "signal_evaluation"), source_id=str(observation.get("source_id") or ""), role=observation.get("role"), symbol=str(observation["symbol"]).upper() if observation.get("symbol") else None, signal_type=observation.get("signal_type"), market_regime=observation.get("market_regime"), observation=str(observation.get("observation") or ""), evidence_json=redact_secrets(list(observation.get("evidence") or [])), severity=str(observation.get("severity") or "info"), created_at_ms=int(observation.get("created_at_ms") or 0), metadata_json=redact_secrets(dict(observation.get("metadata") or {})))
            session.add(item)
            await session.commit()
            return item.id

    async def list_memory_observations(self, source_type: str | None = None, role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(MemoryObservationRecord).order_by(MemoryObservationRecord.created_at_ms.desc()).limit(limit)
            if source_type:
                stmt = stmt.where(MemoryObservationRecord.source_type == source_type)
            if role:
                stmt = stmt.where(MemoryObservationRecord.role == role)
            result = await session.execute(stmt)
            return [_memory_observation_to_dict(item) for item in result.scalars().all()]

    async def upsert_candidate_lesson(self, lesson: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(CandidateLessonRecord, str(lesson["id"]))
            if item is None:
                item = CandidateLessonRecord(id=str(lesson["id"]), lesson_type=str(lesson.get("lesson_type") or "role_behavior"), role=lesson.get("role"), scope_json={}, claim=str(lesson.get("claim") or ""), sample_size=int(lesson.get("sample_size") or 0), confidence=float(lesson.get("confidence") or 0), expected_future_behavior_change=str(lesson.get("expected_future_behavior_change") or ""), status=str(lesson.get("status") or "candidate"), created_at_ms=int(lesson.get("created_at_ms") or 0), expires_at_ms=int(lesson.get("expires_at_ms") or 0))
                session.add(item)
            _apply_candidate_lesson(item, lesson)
            await session.commit()

    async def list_candidate_lessons(self, status: str | None = None, role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(CandidateLessonRecord).order_by(CandidateLessonRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(CandidateLessonRecord.status == status)
            if role:
                stmt = stmt.where(CandidateLessonRecord.role == role)
            result = await session.execute(stmt)
            return [_candidate_lesson_to_dict(item) for item in result.scalars().all()]

    async def get_candidate_lesson(self, candidate_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(CandidateLessonRecord, candidate_id)
            return _candidate_lesson_to_dict(item) if item is not None else None

    async def set_candidate_lesson_status(self, candidate_id: str, status: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(CandidateLessonRecord, candidate_id)
            if item is not None:
                item.status = status
                await session.commit()

    async def upsert_shadow_role_lesson(self, lesson: dict[str, Any]) -> None:
        await self._upsert_role_lesson_record(ShadowRoleLessonRecord, lesson)

    async def upsert_role_lesson(self, lesson: dict[str, Any]) -> None:
        await self._upsert_role_lesson_record(RoleLessonRecord, lesson)

    async def _upsert_role_lesson_record(self, model: type[ShadowRoleLessonRecord] | type[RoleLessonRecord], lesson: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = cast(ShadowRoleLessonRecord | RoleLessonRecord | None, await session.get(model, str(lesson["id"])))
            if item is None:
                item = model(id=str(lesson["id"]), role=str(lesson.get("role") or "analyst"), lesson_type=str(lesson.get("lesson_type") or "role_behavior"), claim=str(lesson.get("claim") or ""), instruction=str(lesson.get("instruction") or ""), confidence=float(lesson.get("confidence") or 0), sample_size=int(lesson.get("sample_size") or 0), validation_status=str(lesson.get("validation_status") or "shadow"), created_at_ms=int(lesson.get("created_at_ms") or 0), expires_at_ms=int(lesson.get("expires_at_ms") or 0))
                session.add(item)
            _apply_role_lesson(item, lesson)
            await session.commit()

    async def list_role_lessons(self, role: str | None = None, status: str | None = "active", include_shadow: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            model = ShadowRoleLessonRecord if include_shadow else RoleLessonRecord
            stmt = select(model).order_by(model.created_at_ms.desc()).limit(limit)
            if role:
                stmt = stmt.where(model.role == role)
            if status:
                stmt = stmt.where(model.validation_status == status)
            result = await session.execute(stmt)
            return [_role_lesson_to_dict(cast(ShadowRoleLessonRecord | RoleLessonRecord, item)) for item in result.scalars().all()]

    async def get_role_lesson(self, lesson_id: str, include_shadow: bool = False) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            role_item = await session.get(RoleLessonRecord, lesson_id)
            if role_item is not None:
                return _role_lesson_to_dict(role_item)
            if include_shadow:
                shadow_item = await session.get(ShadowRoleLessonRecord, lesson_id)
                return _role_lesson_to_dict(shadow_item) if shadow_item is not None else None
            return None

    async def archive_role_lesson(self, lesson_id: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(RoleLessonRecord, lesson_id)
            if item is not None:
                item.validation_status = "archived"
                await session.commit()

    async def upsert_operator_output_lesson(self, lesson: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(OperatorOutputLessonRecord, str(lesson["id"]))
            if item is None:
                item = OperatorOutputLessonRecord(id=str(lesson["id"]), issue_or_pattern=str(lesson.get("issue_or_pattern") or ""), preferred_behavior=str(lesson.get("preferred_behavior") or ""), confidence=float(lesson.get("confidence") or 0), sample_size=int(lesson.get("sample_size") or 0), validation_status=str(lesson.get("validation_status") or "active"), created_at_ms=int(lesson.get("created_at_ms") or 0), expires_at_ms=int(lesson.get("expires_at_ms") or 0))
                session.add(item)
            _apply_operator_lesson(item, lesson)
            await session.commit()

    async def list_operator_output_lessons(self, status: str | None = "active", limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(OperatorOutputLessonRecord).order_by(OperatorOutputLessonRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(OperatorOutputLessonRecord.validation_status == status)
            result = await session.execute(stmt)
            return [_operator_lesson_to_dict(item) for item in result.scalars().all()]

    async def record_operator_feedback(self, feedback: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = OperatorFeedbackRecord(id=str(feedback["id"]), source=str(feedback.get("source") or "api"), actor_id=feedback.get("actor_id"), target_type=str(feedback.get("target_type") or "signal"), target_id=str(feedback.get("target_id") or ""), rating=str(feedback.get("rating") or "unclear"), note=str(feedback.get("note") or ""), created_at_ms=int(feedback.get("created_at_ms") or 0), metadata_json=redact_secrets(dict(feedback.get("metadata") or {})))
            session.add(item)
            await session.commit()
            return item.id

    async def list_operator_feedback(self, target_type: str | None = None, target_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(OperatorFeedbackRecord).order_by(OperatorFeedbackRecord.created_at_ms.desc()).limit(limit)
            if target_type:
                stmt = stmt.where(OperatorFeedbackRecord.target_type == target_type)
            if target_id:
                stmt = stmt.where(OperatorFeedbackRecord.target_id == target_id)
            result = await session.execute(stmt)
            return [_operator_feedback_to_dict(item) for item in result.scalars().all()]

    async def upsert_tuning_proposal(self, proposal: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TuningProposalRecord, str(proposal["id"]))
            if item is None:
                item = TuningProposalRecord(id=str(proposal["id"]), proposal_type=str(proposal.get("proposal_type") or "threshold_change"), status=str(proposal.get("status") or "draft"), title=str(proposal.get("title") or ""), summary=str(proposal.get("summary") or ""), expected_impact=str(proposal.get("expected_impact") or ""), risk_assessment=str(proposal.get("risk_assessment") or ""), blast_radius=str(proposal.get("blast_radius") or "low"), rollback_plan=str(proposal.get("rollback_plan") or ""), confidence=float(proposal.get("confidence") or 0), sample_size=int(proposal.get("sample_size") or 0), created_at_ms=int(proposal.get("created_at_ms") or 0), expires_at_ms=int(proposal.get("expires_at_ms") or 0), evaluation_window=str(proposal.get("evaluation_window") or "7d"))
                session.add(item)
            _apply_tuning_proposal(item, proposal)
            await session.commit()

    async def list_tuning_proposals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(TuningProposalRecord).order_by(TuningProposalRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(TuningProposalRecord.status == status)
            result = await session.execute(stmt)
            return [_tuning_proposal_to_dict(item) for item in result.scalars().all()]

    async def get_tuning_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(TuningProposalRecord, proposal_id)
            return _tuning_proposal_to_dict(item) if item is not None else None

    async def set_tuning_proposal_status(self, proposal_id: str, status: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TuningProposalRecord, proposal_id)
            if item is not None:
                item.status = status
                await session.commit()

    async def record_token_capital_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TokenCapitalSnapshotRecord, str(snapshot["id"]))
            if item is None:
                item = TokenCapitalSnapshotRecord(id=str(snapshot["id"]), timestamp_ms=int(snapshot.get("timestamp_ms") or 0), window=str(snapshot.get("window") or "daily"), total_score=float(snapshot.get("total_score") or 0), risk_adjusted_performance_score=float(snapshot.get("risk_adjusted_performance_score") or 0), signal_quality_score=float(snapshot.get("signal_quality_score") or 0), memory_compounding_score=float(snapshot.get("memory_compounding_score") or 0), risk_discipline_score=float(snapshot.get("risk_discipline_score") or 0), operator_communication_score=float(snapshot.get("operator_communication_score") or 0), reliability_score=float(snapshot.get("reliability_score") or 0))
                session.add(item)
            item.hard_gate_penalties_json = redact_secrets(list(snapshot.get("hard_gate_penalties") or []))
            item.component_details_json = redact_secrets(dict(snapshot.get("component_details") or {}))
            item.created_from_report_id = snapshot.get("created_from_report_id")
            item.metadata_json = redact_secrets(dict(snapshot.get("metadata") or {}))
            await session.commit()

    async def list_token_capital_snapshots(self, window: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(TokenCapitalSnapshotRecord).order_by(TokenCapitalSnapshotRecord.timestamp_ms.desc()).limit(limit)
            if window:
                stmt = stmt.where(TokenCapitalSnapshotRecord.window == window)
            result = await session.execute(stmt)
            return [_token_capital_snapshot_to_dict(item) for item in result.scalars().all()]

    async def upsert_autonomy_report(self, report: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        report_type = str(report.get("report_type") or "daily")
        model = WeeklyReportRecord if report_type == "weekly" else DailyReportRecord
        key_field = "week_key" if report_type == "weekly" else "report_date"
        key_value = str(report.get("key") or "")
        async with self.sessionmaker() as session:
            stmt = select(model).where(getattr(model, key_field) == key_value)
            result = await session.execute(stmt)
            item = cast(DailyReportRecord | WeeklyReportRecord | None, result.scalar_one_or_none())
            if item is None:
                common = dict(id=str(report["id"]), period_start_ms=int(report.get("period_start_ms") or 0), period_end_ms=int(report.get("period_end_ms") or 0), generated_at_ms=int(report.get("generated_at_ms") or 0), summary=str(report.get("summary") or ""))
                item = model(**common, **{key_field: key_value})
                session.add(item)
            item.period_start_ms = int(report.get("period_start_ms") or item.period_start_ms)
            item.period_end_ms = int(report.get("period_end_ms") or item.period_end_ms)
            item.generated_at_ms = int(report.get("generated_at_ms") or item.generated_at_ms)
            token_capital = dict(report.get("token_capital") or {})
            item.token_capital_score = token_capital.get("total_score")
            item.summary = str(report.get("summary") or item.summary)
            item.report_json = redact_secrets(dict(report.get("report") or {}))
            item.discord_channel_id = report.get("discord_channel_id")
            item.discord_message_id = report.get("discord_message_id")
            item.metadata_json = redact_secrets(dict(report.get("metadata") or {}))
            await session.commit()

    async def list_autonomy_reports(self, report_type: str = "daily", limit: int = 30) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        model = WeeklyReportRecord if report_type == "weekly" else DailyReportRecord
        async with self.sessionmaker() as session:
            result = await session.execute(select(model).order_by(model.generated_at_ms.desc()).limit(limit))
            return [_autonomy_report_to_dict(cast(DailyReportRecord | WeeklyReportRecord, item), report_type) for item in result.scalars().all()]

    async def get_autonomy_report(self, report_type: str, key: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        model = WeeklyReportRecord if report_type == "weekly" else DailyReportRecord
        key_field = "week_key" if report_type == "weekly" else "report_date"
        async with self.sessionmaker() as session:
            result = await session.execute(select(model).where(getattr(model, key_field) == key))
            item = cast(DailyReportRecord | WeeklyReportRecord | None, result.scalar_one_or_none())
            return _autonomy_report_to_dict(item, report_type) if item is not None else None

    async def create_or_get_paper_portfolio(self, name: str, initial_equity_usd: float, mode: str = "paper_signoff") -> dict[str, Any]:
        if self.sessionmaker is None:
            raise RuntimeError("repository disabled")
        async with self.sessionmaker() as session:
            result = await session.execute(select(PaperPortfolioRecord).where(PaperPortfolioRecord.name == name))
            item = result.scalar_one_or_none()
            if item is None:
                item = PaperPortfolioRecord(name=name, status="active", initial_equity_usd=initial_equity_usd, cash_usd=initial_equity_usd, realized_pnl_usd=0.0, metadata_json={"mode": mode}, updated_at=datetime.now(UTC))
                session.add(item)
                await session.flush()
            await session.commit()
            return _paper_portfolio_to_dict(item)

    async def create_paper_order(self, order: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperOrderRecord, str(order["id"]))
            if item is None:
                item = PaperOrderRecord(id=str(order["id"]), portfolio_id=str(order["portfolio_id"]), signal_id=order.get("signal_id"), symbol=str(order.get("symbol") or "").upper(), side=str(order.get("side") or "long"), order_type=str(order.get("order_type") or "market"), status=str(order.get("status") or "new"), quantity=float(order.get("quantity") or 0), fee_bps=float(order.get("fee_bps") or 0), slippage_bps=float(order.get("slippage_bps") or 0))
                session.add(item)
            item.status = str(order.get("status") or item.status)
            item.requested_px = order.get("requested_px")
            item.filled_px = order.get("filled_px")
            item.stop_px = order.get("stop_px")
            item.take_profit_px = order.get("take_profit_px")
            item.filled_at = _datetime_from_optional_ms(order.get("filled_at_ms"))
            item.cancelled_at = _datetime_from_optional_ms(order.get("cancelled_at_ms"))
            item.metadata_json = redact_secrets(dict(order.get("metadata") or {}))
            await session.commit()

    async def mark_paper_order_filled(self, order_id: str, filled_px: float, timestamp_ms: int) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperOrderRecord, order_id)
            if item is not None:
                item.status = "filled"
                item.filled_px = filled_px
                item.filled_at = _datetime_from_ms(timestamp_ms)
                await session.commit()

    async def record_paper_fill(self, fill: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(PaperFillRecord, str(fill["id"]))
            if existing is None:
                fee_usd = float(fill.get("fee_usd") or 0)
                session.add(PaperFillRecord(id=str(fill["id"]), order_id=str(fill["order_id"]), portfolio_id=str(fill["portfolio_id"]), symbol=str(fill.get("symbol") or "").upper(), side=str(fill.get("side") or "long"), quantity=float(fill.get("quantity") or 0), price=float(fill.get("price") or 0), fee_usd=fee_usd, slippage_usd=float(fill.get("slippage_usd") or 0), metadata_json=redact_secrets(dict(fill.get("metadata") or {}))))
                portfolio = await session.get(PaperPortfolioRecord, str(fill["portfolio_id"]))
                if portfolio is not None:
                    portfolio.cash_usd -= fee_usd
                    portfolio.updated_at = datetime.now(UTC)
                await session.commit()

    async def upsert_paper_position(self, position: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperPositionRecord, str(position["id"]))
            if item is None:
                item = PaperPositionRecord(id=str(position["id"]), portfolio_id=str(position["portfolio_id"]), signal_id=position.get("signal_id"), symbol=str(position.get("symbol") or "").upper(), side=str(position.get("side") or "long"), status=str(position.get("status") or "open"), quantity=float(position.get("quantity") or 0), avg_entry_px=float(position.get("avg_entry_px") or 0), stop_px=float(position.get("stop_px") or 0), opened_at=_datetime_from_ms(int(position.get("opened_at_ms") or 0)))
                session.add(item)
            item.status = str(position.get("status") or item.status)
            item.mark_px = position.get("mark_px")
            item.take_profit_px = position.get("take_profit_px")
            item.realized_pnl_usd = float(position.get("realized_pnl_usd") or 0)
            item.unrealized_pnl_usd = float(position.get("unrealized_pnl_usd") or 0)
            item.closed_at = _datetime_from_optional_ms(position.get("closed_at_ms"))
            item.metadata_json = redact_secrets(dict(position.get("metadata") or {}))
            await session.commit()

    async def close_paper_position(self, position_id: str, close_px: float, realized_pnl_usd: float, reason: str, timestamp_ms: int) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperPositionRecord, position_id)
            if item is not None:
                item.status = "closed"
                item.mark_px = close_px
                item.realized_pnl_usd = realized_pnl_usd
                item.unrealized_pnl_usd = 0.0
                item.closed_at = _datetime_from_ms(timestamp_ms)
                item.metadata_json = {**(item.metadata_json or {}), "close_reason": reason}
                portfolio = await session.get(PaperPortfolioRecord, item.portfolio_id)
                if portfolio is not None:
                    portfolio.cash_usd += realized_pnl_usd
                    portfolio.realized_pnl_usd += realized_pnl_usd
                    portfolio.updated_at = datetime.now(UTC)
                await session.commit()

    async def list_paper_positions(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(PaperPositionRecord).limit(limit)
            if status:
                stmt = stmt.where(PaperPositionRecord.status == status)
            result = await session.execute(stmt)
            return [_paper_position_to_dict(item) for item in result.scalars().all()]

    async def list_paper_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(PaperOrderRecord).order_by(PaperOrderRecord.created_at.desc()).limit(limit))
            return [_paper_order_to_dict(item) for item in result.scalars().all()]

    async def list_paper_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(PaperFillRecord).order_by(PaperFillRecord.created_at.desc()).limit(limit))
            return [_paper_fill_to_dict(item) for item in result.scalars().all()]

    async def record_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(PortfolioSnapshotRecord, str(snapshot["id"]))
            if existing is None:
                session.add(PortfolioSnapshotRecord(id=str(snapshot["id"]), portfolio_id=str(snapshot["portfolio_id"]), timestamp_ms=int(snapshot.get("timestamp_ms") or 0), cash_usd=float(snapshot.get("cash_usd") or 0), equity_usd=float(snapshot.get("equity_usd") or 0), gross_exposure_usd=float(snapshot.get("gross_exposure_usd") or 0), net_exposure_usd=float(snapshot.get("net_exposure_usd") or 0), realized_pnl_usd=float(snapshot.get("realized_pnl_usd") or 0), unrealized_pnl_usd=float(snapshot.get("unrealized_pnl_usd") or 0), total_pnl_usd=float(snapshot.get("total_pnl_usd") or 0), drawdown_pct=float(snapshot.get("drawdown_pct") or 0), sharpe=snapshot.get("sharpe"), metrics_json=redact_secrets(dict(snapshot.get("metrics") or {}))))
                await session.commit()

    async def get_latest_portfolio_snapshot(self, portfolio_id: str | None = None) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            stmt = select(PortfolioSnapshotRecord).order_by(PortfolioSnapshotRecord.timestamp_ms.desc()).limit(1)
            if portfolio_id:
                stmt = stmt.where(PortfolioSnapshotRecord.portfolio_id == portfolio_id)
            result = await session.execute(stmt)
            item = result.scalar_one_or_none()
            return _portfolio_snapshot_to_dict(item) if item is not None else None

    async def list_portfolio_snapshots(self, portfolio_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(PortfolioSnapshotRecord).order_by(PortfolioSnapshotRecord.timestamp_ms.desc()).limit(limit)
            if portfolio_id:
                stmt = stmt.where(PortfolioSnapshotRecord.portfolio_id == portfolio_id)
            result = await session.execute(stmt)
            return [_portfolio_snapshot_to_dict(item) for item in result.scalars().all()]

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

    async def _signal_evaluation_to_dict(self, session: AsyncSession, item: SignalEvaluationRecord) -> dict[str, Any]:
        result = await session.execute(select(SignalEvaluationMarkRecord).where(SignalEvaluationMarkRecord.evaluation_id == item.id).order_by(SignalEvaluationMarkRecord.due_at_ms.asc()))
        data = _signal_evaluation_to_dict(item)
        data["marks"] = [_signal_evaluation_mark_to_dict(mark) for mark in result.scalars().all()]
        return data


def _apply_signal_evaluation(item: SignalEvaluationRecord, data: dict[str, Any]) -> None:
    item.symbol = str(data.get("symbol") or item.symbol).upper()
    item.side = str(data.get("side") or item.side)
    item.signal_type = str(data.get("signal_type") or item.signal_type)
    item.status = str(data.get("status") or item.status)
    item.completed_at_ms = data.get("completed_at_ms")
    item.entry_px = _float_value(data.get("entry"), item.entry_px)
    item.stop_px = _float_value(data.get("stop"), item.stop_px)
    item.take_profit_px = data.get("take_profit")
    item.signal_score = _float_value(data.get("signal_score"), item.signal_score)
    item.signal_confidence = _float_value(data.get("signal_confidence"), item.signal_confidence)
    item.signal_status_at_eval_start = str(data.get("signal_status_at_eval_start") or item.signal_status_at_eval_start)
    item.first_price = data.get("first_price")
    item.latest_price = data.get("latest_price")
    item.latest_price_at_ms = data.get("latest_price_at_ms")
    item.max_favorable_price = data.get("max_favorable_price")
    item.max_adverse_price = data.get("max_adverse_price")
    item.max_favorable_bps = data.get("max_favorable_bps")
    item.max_adverse_bps = data.get("max_adverse_bps")
    item.max_favorable_r = data.get("max_favorable_r")
    item.max_adverse_r = data.get("max_adverse_r")
    item.stop_hit = bool(data.get("stop_hit"))
    item.stop_hit_at_ms = data.get("stop_hit_at_ms")
    item.take_profit_hit = bool(data.get("take_profit_hit"))
    item.take_profit_hit_at_ms = data.get("take_profit_hit_at_ms")
    item.terminal_outcome = str(data.get("terminal_outcome") or item.terminal_outcome)
    item.realized_or_marked_r = data.get("realized_or_marked_r")
    item.opportunity_cost_r = data.get("opportunity_cost_r")
    item.approved = bool(data.get("approved"))
    item.rejected = bool(data.get("rejected"))
    item.paper_ordered = bool(data.get("paper_ordered"))
    item.paper_position_id = data.get("paper_position_id")
    item.feature_snapshot_json = redact_secrets(dict(data.get("feature_snapshot") or {}))
    item.evidence_snapshot_json = redact_secrets(list(data.get("evidence_snapshot") or []))
    item.market_regime = str(data.get("market_regime") or "unknown")
    item.error = str(data.get("error") or "")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))
    item.updated_at = datetime.now(UTC)


def _float_value(value: Any, fallback: float) -> float:
    try:
        return float(fallback if value is None else value)
    except (TypeError, ValueError):
        return float(fallback)


def _apply_signal_evaluation_mark(item: SignalEvaluationMarkRecord, data: dict[str, Any]) -> None:
    item.evaluation_id = str(data.get("evaluation_id") or item.evaluation_id)
    item.signal_id = str(data.get("signal_id") or item.signal_id)
    item.symbol = str(data.get("symbol") or item.symbol).upper()
    item.horizon = str(data.get("horizon") or item.horizon)
    item.due_at_ms = int(data.get("due_at_ms") or item.due_at_ms)
    item.marked_at_ms = data.get("marked_at_ms")
    item.price = data.get("price")
    item.direction_adjusted_return_bps = data.get("direction_adjusted_return_bps")
    item.r_multiple = data.get("r_multiple")
    item.mfe_bps_until_mark = data.get("mfe_bps_until_mark")
    item.mae_bps_until_mark = data.get("mae_bps_until_mark")
    item.mfe_r_until_mark = data.get("mfe_r_until_mark")
    item.mae_r_until_mark = data.get("mae_r_until_mark")
    item.stop_hit_before_mark = bool(data.get("stop_hit_before_mark"))
    item.take_profit_hit_before_mark = bool(data.get("take_profit_hit_before_mark"))
    item.status = str(data.get("status") or item.status)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_candidate_lesson(item: CandidateLessonRecord, data: dict[str, Any]) -> None:
    item.lesson_type = str(data.get("lesson_type") or item.lesson_type)
    item.role = data.get("role")
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.claim = str(data.get("claim") or item.claim)
    item.evidence_json = redact_secrets(list(data.get("evidence") or []))
    item.source_observation_ids_json = list(data.get("source_observation_ids") or [])
    item.source_run_ids_json = list(data.get("source_run_ids") or [])
    item.source_signal_ids_json = list(data.get("source_signal_ids") or [])
    item.sample_size = int(data.get("sample_size") or 0)
    item.counterexamples_json = redact_secrets(list(data.get("counterexamples") or []))
    item.confidence = float(data.get("confidence") or 0)
    item.expected_future_behavior_change = str(data.get("expected_future_behavior_change") or "")
    item.strategy_affecting = bool(data.get("strategy_affecting"))
    item.risk_affecting = bool(data.get("risk_affecting"))
    item.execution_affecting = bool(data.get("execution_affecting"))
    item.capital_allocation_affecting = bool(data.get("capital_allocation_affecting"))
    item.status = str(data.get("status") or item.status)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_role_lesson(item: ShadowRoleLessonRecord | RoleLessonRecord, data: dict[str, Any]) -> None:
    item.role = str(data.get("role") or item.role)
    item.lesson_type = str(data.get("lesson_type") or item.lesson_type)
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.claim = str(data.get("claim") or item.claim)
    item.instruction = str(data.get("instruction") or item.instruction)
    item.evidence_json = redact_secrets(list(data.get("evidence") or []))
    item.source_candidate_id = data.get("source_candidate_id")
    item.source_run_ids_json = list(data.get("source_run_ids") or [])
    item.source_signal_ids_json = list(data.get("source_signal_ids") or [])
    item.confidence = float(data.get("confidence") or 0)
    item.sample_size = int(data.get("sample_size") or 0)
    item.counterexamples_json = redact_secrets(list(data.get("counterexamples") or []))
    item.validation_status = str(data.get("validation_status") or item.validation_status)
    item.strategy_affecting = bool(data.get("strategy_affecting"))
    item.risk_affecting = bool(data.get("risk_affecting"))
    item.execution_affecting = bool(data.get("execution_affecting"))
    item.capital_allocation_affecting = bool(data.get("capital_allocation_affecting"))
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.activated_at_ms = data.get("activated_at_ms")
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.last_revalidated_at_ms = data.get("last_revalidated_at_ms")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_operator_lesson(item: OperatorOutputLessonRecord, data: dict[str, Any]) -> None:
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.issue_or_pattern = str(data.get("issue_or_pattern") or item.issue_or_pattern)
    item.preferred_behavior = str(data.get("preferred_behavior") or item.preferred_behavior)
    item.bad_examples_json = redact_secrets(list(data.get("bad_examples") or []))
    item.good_examples_json = redact_secrets(list(data.get("good_examples") or []))
    item.confidence = float(data.get("confidence") or 0)
    item.sample_size = int(data.get("sample_size") or 0)
    item.validation_status = str(data.get("validation_status") or item.validation_status)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_tuning_proposal(item: TuningProposalRecord, data: dict[str, Any]) -> None:
    item.proposal_type = str(data.get("proposal_type") or item.proposal_type)
    item.status = str(data.get("status") or item.status)
    item.title = str(data.get("title") or item.title)
    item.summary = str(data.get("summary") or item.summary)
    item.affected_scope_json = redact_secrets(dict(data.get("affected_scope") or {}))
    item.current_behavior_json = redact_secrets(dict(data.get("current_behavior") or {}))
    item.proposed_diff_json = redact_secrets(dict(data.get("proposed_diff") or {}))
    item.evidence_json = redact_secrets(list(data.get("evidence") or []))
    item.source_lesson_ids_json = list(data.get("source_lesson_ids") or [])
    item.source_signal_ids_json = list(data.get("source_signal_ids") or [])
    item.expected_impact = str(data.get("expected_impact") or "")
    item.risk_assessment = str(data.get("risk_assessment") or "")
    item.blast_radius = str(data.get("blast_radius") or "low")
    item.rollback_plan = str(data.get("rollback_plan") or "")
    item.confidence = float(data.get("confidence") or 0)
    item.sample_size = int(data.get("sample_size") or 0)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.evaluation_window = str(data.get("evaluation_window") or item.evaluation_window)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _datetime_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _datetime_from_optional_ms(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return _datetime_from_ms(int(value))
    except (TypeError, ValueError, OSError):
        return None


def _ms_from_datetime(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value is not None else None


def _market_asset_to_dict(item: MarketAssetRecord) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "display_name": item.display_name,
        "kind": item.kind,
        "source": item.source,
        "dex": item.dex,
        "sz_decimals": item.sz_decimals,
        "max_leverage": item.max_leverage,
        "day_volume_usd": item.day_volume_usd,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _news_event_to_dict(item: AutonomyNewsEvent) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "provider": item.provider,
        "title": item.title,
        "text": item.text,
        "url": item.url,
        "author_id": item.author_id,
        "created_at_ms": item.created_at_ms,
        "observed_at_ms": item.observed_at_ms,
        "assets": item.assets_json,
        "importance_score": item.importance_score,
        "sentiment": item.sentiment,
        "freshness": str((item.metadata_json or {}).get("freshness") or "fresh"),
        "metadata": item.metadata_json,
    }


def _trade_signal_to_dict(item: TradeSignalRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "symbol": item.symbol,
        "side": item.side,
        "signal_type": item.signal_type,
        "status": item.status,
        "score": item.score,
        "confidence": item.confidence,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "entry": item.entry_px,
        "stop": item.stop_px,
        "take_profit": item.take_profit_px,
        "thesis": item.thesis,
        "invalidation": item.invalidation,
        "evidence": item.evidence_json,
        "feature_snapshot": item.feature_snapshot_json,
        "risk_plan": item.risk_plan_json,
        "model_insight": item.model_insight_json,
        "discord_channel_id": item.discord_channel_id,
        "discord_message_id": item.discord_message_id,
    }


def _paper_portfolio_to_dict(item: PaperPortfolioRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "initial_equity_usd": item.initial_equity_usd,
        "cash_usd": item.cash_usd,
        "realized_pnl_usd": item.realized_pnl_usd,
        "metadata": item.metadata_json,
        "created_at_ms": _ms_from_datetime(item.created_at) or int(datetime.now(UTC).timestamp() * 1000),
        "updated_at_ms": _ms_from_datetime(item.updated_at) or _ms_from_datetime(item.created_at) or int(datetime.now(UTC).timestamp() * 1000),
    }


def _paper_order_to_dict(item: PaperOrderRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "order_type": item.order_type,
        "status": item.status,
        "quantity": item.quantity,
        "requested_px": item.requested_px,
        "filled_px": item.filled_px,
        "stop_px": item.stop_px,
        "take_profit_px": item.take_profit_px,
        "fee_bps": item.fee_bps,
        "slippage_bps": item.slippage_bps,
        "created_at_ms": _ms_from_datetime(item.created_at),
        "filled_at_ms": _ms_from_datetime(item.filled_at),
        "cancelled_at_ms": _ms_from_datetime(item.cancelled_at),
        "metadata": item.metadata_json,
    }


def _paper_fill_to_dict(item: PaperFillRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "order_id": item.order_id,
        "portfolio_id": item.portfolio_id,
        "symbol": item.symbol,
        "side": item.side,
        "quantity": item.quantity,
        "price": item.price,
        "fee_usd": item.fee_usd,
        "slippage_usd": item.slippage_usd,
        "created_at_ms": _ms_from_datetime(item.created_at),
        "metadata": item.metadata_json,
    }


def _paper_position_to_dict(item: PaperPositionRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "status": item.status,
        "quantity": item.quantity,
        "avg_entry_px": item.avg_entry_px,
        "mark_px": item.mark_px,
        "stop_px": item.stop_px,
        "take_profit_px": item.take_profit_px,
        "realized_pnl_usd": item.realized_pnl_usd,
        "unrealized_pnl_usd": item.unrealized_pnl_usd,
        "opened_at_ms": _ms_from_datetime(item.opened_at),
        "closed_at_ms": _ms_from_datetime(item.closed_at),
        "metadata": item.metadata_json,
    }


def _portfolio_snapshot_to_dict(item: PortfolioSnapshotRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "timestamp_ms": item.timestamp_ms,
        "cash_usd": item.cash_usd,
        "equity_usd": item.equity_usd,
        "gross_exposure_usd": item.gross_exposure_usd,
        "net_exposure_usd": item.net_exposure_usd,
        "realized_pnl_usd": item.realized_pnl_usd,
        "unrealized_pnl_usd": item.unrealized_pnl_usd,
        "total_pnl_usd": item.total_pnl_usd,
        "drawdown_pct": item.drawdown_pct,
        "sharpe": item.sharpe,
        "metrics": item.metrics_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _signal_evaluation_to_dict(item: SignalEvaluationRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "signal_type": item.signal_type,
        "status": item.status,
        "created_at_ms": item.created_at_ms,
        "completed_at_ms": item.completed_at_ms,
        "entry": item.entry_px,
        "stop": item.stop_px,
        "take_profit": item.take_profit_px,
        "signal_score": item.signal_score,
        "signal_confidence": item.signal_confidence,
        "signal_status_at_eval_start": item.signal_status_at_eval_start,
        "first_price": item.first_price,
        "latest_price": item.latest_price,
        "latest_price_at_ms": item.latest_price_at_ms,
        "max_favorable_price": item.max_favorable_price,
        "max_adverse_price": item.max_adverse_price,
        "max_favorable_bps": item.max_favorable_bps,
        "max_adverse_bps": item.max_adverse_bps,
        "max_favorable_r": item.max_favorable_r,
        "max_adverse_r": item.max_adverse_r,
        "stop_hit": item.stop_hit,
        "stop_hit_at_ms": item.stop_hit_at_ms,
        "take_profit_hit": item.take_profit_hit,
        "take_profit_hit_at_ms": item.take_profit_hit_at_ms,
        "terminal_outcome": item.terminal_outcome,
        "realized_or_marked_r": item.realized_or_marked_r,
        "opportunity_cost_r": item.opportunity_cost_r,
        "approved": item.approved,
        "rejected": item.rejected,
        "paper_ordered": item.paper_ordered,
        "paper_position_id": item.paper_position_id,
        "feature_snapshot": item.feature_snapshot_json,
        "evidence_snapshot": item.evidence_snapshot_json,
        "market_regime": item.market_regime,
        "error": item.error,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _signal_evaluation_mark_to_dict(item: SignalEvaluationMarkRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "evaluation_id": item.evaluation_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "horizon": item.horizon,
        "due_at_ms": item.due_at_ms,
        "marked_at_ms": item.marked_at_ms,
        "price": item.price,
        "direction_adjusted_return_bps": item.direction_adjusted_return_bps,
        "r_multiple": item.r_multiple,
        "mfe_bps_until_mark": item.mfe_bps_until_mark,
        "mae_bps_until_mark": item.mae_bps_until_mark,
        "mfe_r_until_mark": item.mfe_r_until_mark,
        "mae_r_until_mark": item.mae_r_until_mark,
        "stop_hit_before_mark": item.stop_hit_before_mark,
        "take_profit_hit_before_mark": item.take_profit_hit_before_mark,
        "status": item.status,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _memory_observation_to_dict(item: MemoryObservationRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "role": item.role,
        "symbol": item.symbol,
        "signal_type": item.signal_type,
        "market_regime": item.market_regime,
        "observation": item.observation,
        "evidence": item.evidence_json,
        "severity": item.severity,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _candidate_lesson_to_dict(item: CandidateLessonRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "lesson_type": item.lesson_type,
        "role": item.role,
        "scope": item.scope_json,
        "claim": item.claim,
        "evidence": item.evidence_json,
        "source_observation_ids": item.source_observation_ids_json,
        "source_run_ids": item.source_run_ids_json,
        "source_signal_ids": item.source_signal_ids_json,
        "sample_size": item.sample_size,
        "counterexamples": item.counterexamples_json,
        "confidence": item.confidence,
        "expected_future_behavior_change": item.expected_future_behavior_change,
        "strategy_affecting": item.strategy_affecting,
        "risk_affecting": item.risk_affecting,
        "execution_affecting": item.execution_affecting,
        "capital_allocation_affecting": item.capital_allocation_affecting,
        "status": item.status,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "metadata": item.metadata_json,
    }


def _role_lesson_to_dict(item: ShadowRoleLessonRecord | RoleLessonRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "role": item.role,
        "lesson_type": item.lesson_type,
        "scope": item.scope_json,
        "claim": item.claim,
        "instruction": item.instruction,
        "evidence": item.evidence_json,
        "source_candidate_id": item.source_candidate_id,
        "source_run_ids": item.source_run_ids_json,
        "source_signal_ids": item.source_signal_ids_json,
        "confidence": item.confidence,
        "sample_size": item.sample_size,
        "counterexamples": item.counterexamples_json,
        "validation_status": item.validation_status,
        "strategy_affecting": item.strategy_affecting,
        "risk_affecting": item.risk_affecting,
        "execution_affecting": item.execution_affecting,
        "capital_allocation_affecting": item.capital_allocation_affecting,
        "created_at_ms": item.created_at_ms,
        "activated_at_ms": item.activated_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "last_revalidated_at_ms": item.last_revalidated_at_ms,
        "metadata": item.metadata_json,
    }


def _operator_lesson_to_dict(item: OperatorOutputLessonRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "scope": item.scope_json,
        "issue_or_pattern": item.issue_or_pattern,
        "preferred_behavior": item.preferred_behavior,
        "bad_examples": item.bad_examples_json,
        "good_examples": item.good_examples_json,
        "confidence": item.confidence,
        "sample_size": item.sample_size,
        "validation_status": item.validation_status,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "metadata": item.metadata_json,
    }


def _operator_feedback_to_dict(item: OperatorFeedbackRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "actor_id": item.actor_id,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "rating": item.rating,
        "note": item.note,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _tuning_proposal_to_dict(item: TuningProposalRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "proposal_type": item.proposal_type,
        "status": item.status,
        "title": item.title,
        "summary": item.summary,
        "affected_scope": item.affected_scope_json,
        "current_behavior": item.current_behavior_json,
        "proposed_diff": item.proposed_diff_json,
        "evidence": item.evidence_json,
        "source_lesson_ids": item.source_lesson_ids_json,
        "source_signal_ids": item.source_signal_ids_json,
        "expected_impact": item.expected_impact,
        "risk_assessment": item.risk_assessment,
        "blast_radius": item.blast_radius,
        "rollback_plan": item.rollback_plan,
        "confidence": item.confidence,
        "sample_size": item.sample_size,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "evaluation_window": item.evaluation_window,
        "metadata": item.metadata_json,
    }


def _token_capital_snapshot_to_dict(item: TokenCapitalSnapshotRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "timestamp_ms": item.timestamp_ms,
        "window": item.window,
        "total_score": item.total_score,
        "risk_adjusted_performance_score": item.risk_adjusted_performance_score,
        "signal_quality_score": item.signal_quality_score,
        "memory_compounding_score": item.memory_compounding_score,
        "risk_discipline_score": item.risk_discipline_score,
        "operator_communication_score": item.operator_communication_score,
        "reliability_score": item.reliability_score,
        "hard_gate_penalties": item.hard_gate_penalties_json,
        "component_details": item.component_details_json,
        "created_from_report_id": item.created_from_report_id,
        "metadata": item.metadata_json,
    }


def _autonomy_report_to_dict(item: DailyReportRecord | WeeklyReportRecord, report_type: str) -> dict[str, Any]:
    key = item.week_key if isinstance(item, WeeklyReportRecord) else item.report_date
    return {
        "id": item.id,
        "report_type": report_type,
        "key": key,
        "period_start_ms": item.period_start_ms,
        "period_end_ms": item.period_end_ms,
        "generated_at_ms": item.generated_at_ms,
        "token_capital_score": item.token_capital_score,
        "summary": item.summary,
        "report": item.report_json,
        "discord_channel_id": item.discord_channel_id,
        "discord_message_id": item.discord_message_id,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


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
