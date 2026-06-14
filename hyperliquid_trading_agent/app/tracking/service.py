from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import HyperliquidWebSocketWorker, SubscriptionSpec
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    POSITION_TRACKERS,
    POSITION_TRACKING_ALERTS,
    POSITION_TRACKING_EVENTS,
    POSITION_TRACKING_PRICE_UPDATES,
)
from hyperliquid_trading_agent.app.tracking.commands import TrackingCommand
from hyperliquid_trading_agent.app.tracking.schemas import (
    CrossDirection,
    LevelHitEvent,
    LevelKind,
    PositionTrackingPlan,
    RecommendedAction,
    TrackedLevelSpec,
)

log = get_logger(__name__)


class PositionAlertSink(Protocol):
    async def send_level_hit(self, tracker: PositionTrackingPlan, level: TrackedLevelSpec, event: LevelHitEvent) -> str:
        """Deliver an alert and return an alert status string."""


@dataclass(frozen=True)
class CrossingResult:
    hit: bool = False
    already_breached: bool = False
    rearmed: bool = False


class PositionTrackingService:
    """No-LLM live position level tracker.

    It consumes Hyperliquid `allMids` messages, evaluates precomputed canonical
    levels, persists events, and optionally sends alerts. It never constructs or
    submits exchange actions.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository | None,
        ws_worker: HyperliquidWebSocketWorker,
        alert_sink: PositionAlertSink | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        self.repository = repository
        self.ws_worker = ws_worker
        self.alert_sink = alert_sink
        self.sleep = sleep
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._subscription_id: str | None = None
        self._trackers: dict[str, PositionTrackingPlan] = {}
        self._seen_price: set[str] = set()
        self._lock = asyncio.Lock()
        self._last_reload_at_ms: int | None = None
        self._last_price_update_at_ms: int | None = None
        self.last_auto_arm_reason: str = ""

    async def start(self) -> None:
        if not self.settings.position_tracking_enabled:
            log.info("position_tracking_disabled")
            return
        await self.reload_active_trackers()
        while not self._stop.is_set():
            await self._sync_subscription()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(1, self.settings.position_tracking_reload_seconds))
            except TimeoutError:
                pass
            await self.reload_active_trackers()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._subscription_id is not None:
            await self.ws_worker.unsubscribe(self._subscription_id)
            self._subscription_id = None

    async def auto_arm(self, plan: PositionTrackingPlan, *, proposal_id: str | None = None, run_id: str | None = None) -> str | None:
        self.last_auto_arm_reason = ""
        if not self.settings.position_tracking_enabled:
            self.last_auto_arm_reason = "tracking_disabled"
            return None
        if not self.settings.position_tracking_auto_arm:
            self.last_auto_arm_reason = "auto_arm_disabled"
            return None
        if self.repository is None:
            self.last_auto_arm_reason = "repository_unavailable"
            log.warning("position_tracker_not_armed", reason="repository_unavailable", coin=plan.coin)
            return None
        async with self._lock:
            if len(self._trackers) >= self.settings.position_tracking_max_active:
                self.last_auto_arm_reason = "max_active_reached"
                log.warning("position_tracker_not_armed", reason="max_active_reached", coin=plan.coin)
                return None
        plan_to_store = plan.model_copy(update={"proposal_id": proposal_id or plan.proposal_id, "run_id": run_id or plan.run_id})
        tracker_id = await self.repository.create_position_tracker(plan_to_store, proposal_id=proposal_id, run_id=run_id)
        if tracker_id is None:
            self.last_auto_arm_reason = "persistence_failed"
            return None
        armed_plan = plan_to_store.model_copy(update={"id": tracker_id})
        async with self._lock:
            self._trackers[tracker_id] = armed_plan
        self._wake.set()
        POSITION_TRACKING_EVENTS.labels(event_type="tracker_armed", level_kind="tracker").inc()
        await self.repository.record_tracking_event(
            tracker_id=tracker_id,
            event_type="tracker_armed",
            coin=plan.coin,
            price=plan.current_price_at_arm,
            payload=armed_plan.model_dump(mode="json"),
            alert_destination=_alert_destination(armed_plan),
            alert_status="not_sent",
        )
        self.last_auto_arm_reason = "armed"
        return tracker_id

    async def reload_active_trackers(self) -> None:
        if self.repository is None:
            return
        rows = await self.repository.get_active_position_trackers()
        now_ms = int(time.time() * 1000)
        loaded: dict[str, PositionTrackingPlan] = {}
        for row in rows[: self.settings.position_tracking_max_active]:
            plan = _plan_from_repository_row(row)
            if plan is None:
                continue
            if plan.expires_at_ms <= now_ms:
                await self.repository.set_position_tracker_status(plan.id, "expired", reason="tracking ttl elapsed")
                await self.repository.record_tracking_event(plan.id, "tracker_expired", plan.coin, payload={"expires_at_ms": plan.expires_at_ms})
                continue
            loaded[plan.id] = plan
        async with self._lock:
            self._trackers = loaded
            self._seen_price &= set(loaded.keys())
            self._last_reload_at_ms = now_ms
        _set_tracker_gauges(loaded.values())
        self._wake.set()

    async def process_all_mids(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        mids = data.get("mids", data) if isinstance(data, dict) else {}
        if not isinstance(mids, dict):
            return
        timestamp_ms = int(time.time() * 1000)
        for coin, raw_price in mids.items():
            price = _float_or_none(raw_price)
            if price is None or price <= 0:
                continue
            await self.process_price(str(coin).upper(), price, timestamp_ms)

    async def process_price(self, coin: str, current_price: float, timestamp_ms: int | None = None) -> list[LevelHitEvent]:
        timestamp_ms = timestamp_ms or int(time.time() * 1000)
        POSITION_TRACKING_PRICE_UPDATES.labels(coin=coin.upper()).inc()
        async with self._lock:
            candidates = [tracker for tracker in self._trackers.values() if tracker.coin.upper() == coin.upper() and tracker.status in {"pending", "active"}]
        events: list[LevelHitEvent] = []
        for tracker in candidates:
            events.extend(await self._process_tracker_price(tracker, current_price, timestamp_ms))
        self._last_price_update_at_ms = timestamp_ms
        return events

    async def handle_thread_command(self, command: TrackingCommand, discord_thread_id: str) -> str:
        if self.repository is None:
            return "Tracking storage is unavailable, so I cannot inspect or modify trackers."
        if command.action == "status":
            return await self._thread_status(discord_thread_id, command)
        if command.action == "events":
            return await self._thread_events(discord_thread_id, command)
        if command.action in {"stop", "pause", "resume"}:
            return await self._set_thread_status(discord_thread_id, command)
        if command.action == "set_ttl" and command.ttl_hours:
            return await self._set_thread_ttl(discord_thread_id, command)
        return "I understood this as a tracking command, but it is not supported yet."

    def status(self) -> dict[str, Any]:
        active_by_status: dict[str, int] = {}
        for tracker in self._trackers.values():
            active_by_status[tracker.status] = active_by_status.get(tracker.status, 0) + 1
        return {
            "enabled": self.settings.position_tracking_enabled,
            "auto_arm": self.settings.position_tracking_auto_arm,
            "active_count": len(self._trackers),
            "by_status": active_by_status,
            "all_mids_subscription_active": self._subscription_id is not None,
            "last_reload_at_ms": self._last_reload_at_ms,
            "last_price_update_at_ms": self._last_price_update_at_ms,
        }

    async def _thread_status(self, discord_thread_id: str, command: TrackingCommand) -> str:
        if self.repository is None:
            return "Tracking storage is unavailable."
        repo = self.repository
        rows = await repo.list_position_trackers(discord_thread_id=discord_thread_id, limit=20)
        rows = _filter_rows(rows, command)
        if not rows:
            return "No trackers are attached to this thread."
        lines = ["Tracking status:"]
        for row in rows[:10]:
            levels = row.get("levels", [])
            active_levels = [level for level in levels if level.get("armed")]
            current = row.get("current_price")
            lines.append(
                f"- {row.get('coin')} {row.get('side')} | status={row.get('status')} | current={_fmt_price(current)} | "
                f"entry={_fmt_price(row.get('entry'))} stop={_fmt_price(row.get('stop'))} | armed_levels={len(active_levels)}/{len(levels)} | id={row.get('id')}"
            )
        return "\n".join(lines)

    async def _thread_events(self, discord_thread_id: str, command: TrackingCommand) -> str:
        if self.repository is None:
            return "Tracking storage is unavailable."
        repo = self.repository
        rows = await repo.list_position_trackers(discord_thread_id=discord_thread_id, limit=20)
        rows = _filter_rows(rows, command)
        if not rows:
            return "No trackers are attached to this thread."
        events = await repo.list_tracking_events(str(rows[0]["id"]), limit=8)
        if not events:
            return f"No tracking events yet for {rows[0].get('coin')} tracker {rows[0].get('id')}."
        lines = [f"Recent tracking events for {rows[0].get('coin')}:"]
        for event in events:
            lines.append(f"- {event.get('event_type')} at {_fmt_price(event.get('price'))} ({event.get('created_at')})")
        return "\n".join(lines)

    async def _set_thread_status(self, discord_thread_id: str, command: TrackingCommand) -> str:
        if self.repository is None:
            return "Tracking storage is unavailable."
        repo = self.repository
        rows = await repo.list_position_trackers(discord_thread_id=discord_thread_id, limit=50)
        rows = [row for row in _filter_rows(rows, command) if row.get("status") in {"pending", "active", "paused"}]
        if not rows:
            return "No matching active trackers found in this thread."
        target_status = {"stop": "stopped", "pause": "paused", "resume": "active"}[command.action]
        for row in rows:
            await repo.set_position_tracker_status(str(row["id"]), target_status, reason=f"discord_command:{command.action}")
        await self.reload_active_trackers()
        verb = "Stopped" if command.action == "stop" else "Paused" if command.action == "pause" else "Resumed"
        return f"{verb} {len(rows)} tracker(s) in this thread."

    async def _set_thread_ttl(self, discord_thread_id: str, command: TrackingCommand) -> str:
        if self.repository is None:
            return "Tracking storage is unavailable."
        repo = self.repository
        rows = await repo.list_position_trackers(discord_thread_id=discord_thread_id, limit=50)
        rows = [row for row in _filter_rows(rows, command) if row.get("status") in {"pending", "active", "paused"}]
        if not rows:
            return "No matching active trackers found in this thread."
        assert command.ttl_hours is not None
        expires_at_ms = int(time.time() * 1000) + command.ttl_hours * 60 * 60 * 1000
        for row in rows:
            await repo.set_position_tracker_expiry(str(row["id"]), expires_at_ms)
            await repo.record_tracking_event(str(row["id"]), "tracker_ttl_updated", str(row.get("coin") or ""), payload={"ttl_hours": command.ttl_hours, "expires_at_ms": expires_at_ms})
        await self.reload_active_trackers()
        return f"Updated {len(rows)} tracker(s) to expire in {command.ttl_hours}h."

    async def _sync_subscription(self) -> None:
        async with self._lock:
            has_trackers = bool(self._trackers)
        if has_trackers and self._subscription_id is None:
            self._subscription_id = await self.ws_worker.subscribe(SubscriptionSpec("allMids"), self.process_all_mids)
            log.info("position_tracking_all_mids_subscribed")
        elif not has_trackers and self._subscription_id is not None:
            await self.ws_worker.unsubscribe(self._subscription_id)
            self._subscription_id = None
            log.info("position_tracking_all_mids_unsubscribed")

    async def _process_tracker_price(self, tracker: PositionTrackingPlan, current_price: float, timestamp_ms: int) -> list[LevelHitEvent]:
        previous_price = tracker.current_price_at_arm
        first_update = tracker.id not in self._seen_price
        if self.repository is not None:
            await self.repository.update_position_tracker_price(tracker.id, current_price, previous_price, timestamp_ms)
        updated_levels: list[TrackedLevelSpec] = []
        events: list[LevelHitEvent] = []
        terminal_hit = False
        for level in tracker.levels:
            result = evaluate_level(level, previous_price, current_price, first_update=first_update)
            updated_level = level
            if result.hit or result.already_breached:
                updated_level = level.model_copy(update={"armed": False, "hit_count": level.hit_count + 1})
                event = LevelHitEvent(
                    tracker_id=tracker.id,
                    coin=tracker.coin,
                    side=tracker.side,
                    level_id=level.id,
                    level_kind=level.kind,
                    level_price=level.price,
                    current_price=current_price,
                    direction=level.direction,
                    terminal=level.terminal,
                    recommended_action=_recommended_action(level),
                    exchange_actions=[],
                    metadata={"already_breached": result.already_breached, "previous_price": previous_price, "timestamp_ms": timestamp_ms},
                )
                await self._record_and_alert(tracker, updated_level, event, current_price, result)
                events.append(event)
                terminal_hit = terminal_hit or level.terminal
            elif result.rearmed:
                updated_level = level.model_copy(update={"armed": True})
                if self.repository is not None:
                    await self.repository.update_tracked_level_state(updated_level.id, armed=True, hit_count=updated_level.hit_count)
                    POSITION_TRACKING_EVENTS.labels(event_type="level_rearmed", level_kind=updated_level.kind).inc()
                    await self.repository.record_tracking_event(
                        tracker_id=tracker.id,
                        level_id=updated_level.id,
                        event_type="level_rearmed",
                        coin=tracker.coin,
                        price=current_price,
                        payload={"level": updated_level.model_dump(mode="json"), "current_price": current_price},
                        alert_destination=_alert_destination(tracker),
                        alert_status="not_sent",
                    )
            updated_levels.append(updated_level)

        new_status = "completed" if terminal_hit else "active"
        updated_tracker = tracker.model_copy(update={"current_price_at_arm": current_price, "levels": updated_levels, "status": new_status})
        async with self._lock:
            self._seen_price.add(tracker.id)
            if terminal_hit:
                self._trackers.pop(tracker.id, None)
            else:
                self._trackers[tracker.id] = updated_tracker
        if terminal_hit and self.repository is not None:
            await self.repository.set_position_tracker_status(tracker.id, "completed", reason="terminal level hit")
        self._wake.set()
        return events

    async def _record_and_alert(
        self,
        tracker: PositionTrackingPlan,
        level: TrackedLevelSpec,
        event: LevelHitEvent,
        current_price: float,
        crossing: CrossingResult,
    ) -> None:
        alert_status = "not_sent"
        if self.alert_sink is not None:
            try:
                alert_status = await self.alert_sink.send_level_hit(tracker, level, event)
            except Exception as exc:  # pragma: no cover - alert isolation
                alert_status = f"error:{type(exc).__name__}"
                log.warning("position_tracking_alert_failed", tracker_id=tracker.id, level_id=level.id, error=type(exc).__name__)
        POSITION_TRACKING_ALERTS.labels(destination=_alert_destination(tracker) or "none", result=alert_status).inc()
        POSITION_TRACKING_EVENTS.labels(event_type="level_already_breached" if crossing.already_breached else "level_hit", level_kind=level.kind).inc()
        if self.repository is not None:
            await self.repository.update_tracked_level_state(level.id, armed=False, hit_count=level.hit_count, last_triggered_at=datetime.now(UTC))
            await self.repository.record_tracking_event(
                tracker_id=tracker.id,
                level_id=level.id,
                event_type="level_already_breached" if crossing.already_breached else "level_hit",
                coin=tracker.coin,
                price=current_price,
                payload=event.model_dump(mode="json"),
                alert_destination=_alert_destination(tracker),
                alert_status=alert_status,
            )


def evaluate_level(level: TrackedLevelSpec, previous_price: float | None, current_price: float, *, first_update: bool = False) -> CrossingResult:
    if level.armed:
        if first_update and level.terminal and _beyond_level(level, current_price):
            return CrossingResult(already_breached=True)
        if previous_price is None:
            return CrossingResult()
        if level.direction == "cross_down" and previous_price > level.price >= current_price:
            return CrossingResult(hit=True)
        if level.direction == "cross_up" and previous_price < level.price <= current_price:
            return CrossingResult(hit=True)
        return CrossingResult()
    if not level.terminal and _rearm_condition(level, current_price):
        return CrossingResult(rearmed=True)
    return CrossingResult()


def _beyond_level(level: TrackedLevelSpec, current_price: float) -> bool:
    return current_price <= level.price if level.direction == "cross_down" else current_price >= level.price


def _rearm_condition(level: TrackedLevelSpec, current_price: float) -> bool:
    band = level.rearm_band_bps / 10_000
    if level.direction == "cross_down":
        return current_price >= level.price * (1 + band)
    return current_price <= level.price * (1 - band)


def _recommended_action(level: TrackedLevelSpec) -> RecommendedAction:
    if level.kind in {"hard_stop", "technical_exit"}:
        return "exit"
    if level.kind == "entry_trim":
        return "trim"
    if level.kind in {"entry_reclaim", "resistance_confirm", "support_confirm"}:
        return "confirm_hold"
    return "notify"


def _plan_from_repository_row(row: dict[str, Any]) -> PositionTrackingPlan | None:
    try:
        plan_data = dict(row.get("plan") or {})
        plan_data.update(
            {
                "id": row.get("id") or plan_data.get("id"),
                "proposal_id": row.get("proposal_id") or plan_data.get("proposal_id"),
                "run_id": row.get("run_id") or plan_data.get("run_id"),
                "coin": row.get("coin") or plan_data.get("coin"),
                "side": row.get("side") or plan_data.get("side"),
                "entry": row.get("entry") or plan_data.get("entry"),
                "stop": row.get("stop") or plan_data.get("stop"),
                "take_profit": row.get("take_profit"),
                "current_price_at_arm": row.get("current_price") or plan_data.get("current_price_at_arm"),
                "status": row.get("status") or plan_data.get("status", "pending"),
                "price_source": row.get("price_source") or plan_data.get("price_source", "allMids"),
                "expires_at_ms": _iso_to_ms(row.get("expires_at")) or plan_data.get("expires_at_ms"),
                "discord_guild_id": row.get("discord_guild_id") or plan_data.get("discord_guild_id"),
                "discord_channel_id": row.get("discord_channel_id") or plan_data.get("discord_channel_id"),
                "discord_thread_id": row.get("discord_thread_id") or plan_data.get("discord_thread_id"),
                "discord_user_id": row.get("discord_user_id") or plan_data.get("discord_user_id"),
                "levels": [_level_from_repository_row(level).model_dump(mode="json") for level in row.get("levels", [])],
                "metadata": row.get("metadata") or plan_data.get("metadata") or {},
            }
        )
        return PositionTrackingPlan.model_validate(plan_data)
    except Exception as exc:
        log.warning("position_tracker_row_parse_failed", error=type(exc).__name__)
        return None


def _level_from_repository_row(row: dict[str, Any]) -> TrackedLevelSpec:
    return TrackedLevelSpec(
        id=str(row.get("id")),
        kind=cast(LevelKind, row.get("kind")),
        label=str(row.get("label") or row.get("kind")),
        price=float(cast(float, row.get("price"))),
        direction=cast(CrossDirection, row.get("direction")),
        terminal=bool(row.get("terminal")),
        severity=row.get("severity") or "warning",
        armed=bool(row.get("armed")),
        hit_count=int(row.get("hit_count") or 0),
        rearm_band_bps=float(row.get("rearm_band_bps") or 10.0),
        metadata=row.get("metadata") or {},
    )


def _iso_to_ms(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _alert_destination(plan: PositionTrackingPlan) -> str | None:
    return f"discord_thread:{plan.discord_thread_id}" if plan.discord_thread_id else None


def _filter_rows(rows: list[dict[str, Any]], command: TrackingCommand) -> list[dict[str, Any]]:
    filtered = rows
    if command.tracker_id:
        filtered = [row for row in filtered if str(row.get("id", "")).lower() == command.tracker_id]
    if command.coin:
        filtered = [row for row in filtered if str(row.get("coin", "")).upper() == command.coin.upper()]
    return filtered


def _fmt_price(value: Any) -> str:
    parsed = _float_or_none(value)
    return "n/a" if parsed is None else f"{parsed:g}"


def _set_tracker_gauges(trackers: Any) -> None:
    counts: dict[str, int] = {"pending": 0, "active": 0, "paused": 0, "completed": 0, "expired": 0, "stopped": 0, "error": 0}
    for tracker in trackers:
        status = str(getattr(tracker, "status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    for status, count in counts.items():
        POSITION_TRACKERS.labels(status=status).set(count)
