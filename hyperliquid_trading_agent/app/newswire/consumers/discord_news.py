from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from hyperliquid_trading_agent.app.autonomy.discord import AutonomyAlertSink
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import NEWSWIRE_DISCORD_POSTS, NEWSWIRE_DISCORD_SKIPS
from hyperliquid_trading_agent.app.newswire.bus import NewswireBus
from hyperliquid_trading_agent.app.newswire.enrich import Enricher
from hyperliquid_trading_agent.app.newswire.format import format_news_event_message
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter

log = get_logger(__name__)


class DiscordNewsPublisher:
    """Routes the curated Newswire feed to a dedicated #news channel.

    Breaking / high-importance events post immediately; standard events are released on
    a periodic schedule. Every published event gets its own rich message and feedback
    controls. Posting is fresh-only to avoid startup backlog blasts and uses the
    repository publish ledger when available to prevent restart/multi-process dupes.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        bus: NewswireBus,
        alert_sink: AutonomyAlertSink | None = None,
        enricher: Enricher | None = None,
        repository: Any | None = None,
    ):
        self.settings = settings
        self.bus = bus
        self.alert_sink = alert_sink
        self.enricher = enricher
        self.repository = repository
        self._buffer: list[NewswireEvent] = []
        self._buffer_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._last_send_ms = 0
        self._subscription_id: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._delivery_task: asyncio.Task | None = None
        self._running = False
        self._started_at_ms: int | None = None
        self._last_post_at_ms: int | None = None
        self._last_error: str | None = None
        self._posted_count = 0
        self._skip_counts: Counter[str] = Counter()
        self._process_claimed: set[str] = set()
        self._immediate_send_times: list[int] = []

    @property
    def _channel_id(self) -> str:
        return self.settings.newswire_news_channel_id

    @property
    def enabled(self) -> bool:
        owns_news_channel = self.settings.newswire_enabled or self.settings.discord_publisher_enabled
        return bool(
            owns_news_channel
            and self.settings.newswire_discord_enabled
            and self.settings.newswire_news_channel_configured
        )

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        if self.alert_sink is None or not self.settings.discord_bot_token:
            self._last_error = "discord_sink_or_token_missing"
            log.info("newswire_discord_publisher_disabled", reason=self._last_error)
            return
        self._started_at_ms = _now_ms()
        # Every canonical story revision reaches the consumer so skip/routing reasons
        # remain observable. V2 assessment actions replace subscription thresholds.
        min_importance = 0.0
        flt = NewswireFilter(min_importance=0.0)
        self._subscription_id = await self.bus.subscribe(self._on_event, filter=flt)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="newswire-discord-digest")
        if self.repository is not None and callable(getattr(self.repository, "list_due_newswire_deliveries", None)):
            self._delivery_task = asyncio.create_task(self._delivery_loop(), name="newswire-discord-delivery-outbox")
        self._running = True
        log.info("newswire_discord_publisher_started", channel=self._channel_id, min_importance=min_importance)

    async def stop(self) -> None:
        self._running = False
        if self._subscription_id is not None:
            await self.bus.unsubscribe(self._subscription_id)
            self._subscription_id = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        if self._delivery_task is not None:
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
            self._delivery_task = None

    async def _on_event(self, event: NewswireEvent) -> None:
        reason = self._skip_reason(event)
        if self.repository is not None and callable(getattr(self.repository, "schedule_newswire_delivery", None)):
            delivered = getattr(self.repository, "was_newswire_story_delivered", None)
            if event.action == "removed" and callable(delivered):
                story_id = str(event.story_id or event.metadata.get("story_id") or event.event_id)
                if await delivered(story_id, self._channel_id):
                    reason = None
            await self._schedule_delivery(event, skip_reason=reason)
            if reason is not None:
                self._skip(reason)
            return
        if reason is not None:
            self._skip(reason)
            return
        decision = _active_policy_decision(event)
        breaking = (
            str(decision.get("newswire_action") or "") in {"high", "breaking"}
            if decision
            else event.urgency == "breaking"
            or _legacy_importance(event) >= self.settings.newswire_breaking_min_importance
        )
        if breaking:
            claimed = await self._claim(event, mode="breaking")
            if not claimed:
                self._skip("duplicate")
                return
            await self._enrich(event)
            payload = format_news_event_message(event)
            message_id = await self._send_payload(payload, mode="breaking")
            if message_id:
                await self._mark_posted([event.event_id], message_id)
            else:
                await self._mark_failed([event.event_id], self._last_error or "send_skipped")
        else:
            async with self._buffer_lock:
                self._buffer.append(event)

    async def _schedule_delivery(self, event: NewswireEvent, *, skip_reason: str | None) -> None:
        repo = self.repository
        if repo is None or not callable(getattr(repo, "schedule_newswire_delivery", None)):
            return
        decision = _active_policy_decision(event)
        action = (
            "breaking"
            if event.action == "removed"
            else str(decision.get("newswire_action") or "standard")
            if decision
            else "breaking"
            if event.urgency == "breaking"
            or _legacy_importance(event) >= self.settings.newswire_breaking_min_importance
            else "standard"
        )
        now = _now_ms()
        mode = "breaking" if action in {"high", "breaking"} else "digest"
        if (
            mode == "breaking"
            and event.action != "removed"
            and skip_reason is None
            and not self._within_immediate_budget(now)
        ):
            mode = "digest"
        scheduled_at = (
            now if mode == "breaking" else _next_digest_at(now, self.settings.newswire_digest_interval_seconds)
        )
        story_id = str(event.story_id or event.metadata.get("story_id") or event.event_id)
        story_revision = int(event.story_revision or event.metadata.get("story_revision") or 1)
        status = "skipped" if skip_reason is not None else "pending"
        await repo.schedule_newswire_delivery(
            story_id=story_id,
            story_revision=story_revision,
            channel_id=self._channel_id,
            mode=mode,
            scheduled_at_ms=scheduled_at,
            payload={"event": event.model_dump(mode="json")},
            status=status,
            skip_reason=skip_reason,
        )

    def _within_immediate_budget(self, now_ms: int) -> bool:
        cutoff = now_ms - 60 * 60 * 1000
        self._immediate_send_times = [item for item in self._immediate_send_times if item >= cutoff]
        limit = max(0, int(self.settings.newswire_discord_max_immediate_per_hour))
        if len(self._immediate_send_times) >= limit:
            return False
        self._immediate_send_times.append(now_ms)
        return True

    async def _delivery_loop(self) -> None:
        while True:
            try:
                await self._dispatch_due_deliveries()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime repository behavior
                self._last_error = type(exc).__name__
                log.warning("newswire_delivery_dispatch_failed", error=type(exc).__name__)
            await asyncio.sleep(1.0)

    async def _dispatch_due_deliveries(self) -> None:
        repo = self.repository
        if repo is None:
            return
        claim = getattr(repo, "claim_due_newswire_deliveries", None)
        fallback = getattr(repo, "list_due_newswire_deliveries", None)
        list_due = claim if callable(claim) else fallback
        if not callable(list_due):
            return
        rows = await list_due(
            channel_id=self._channel_id,
            now_ms=_now_ms(),
            limit=max(10, self.settings.newswire_discord_digest_max_items * 2),
        )
        if not rows:
            return
        breaking = [row for row in rows if row.get("mode") == "breaking"]
        digests = [row for row in rows if row.get("mode") != "breaking"]
        for row in breaking:
            event = _delivery_event(row)
            if event is None:
                await repo.mark_newswire_deliveries_failed(
                    [str(row["delivery_id"])], error="invalid_event_payload", now_ms=_now_ms()
                )
                continue
            await self._enrich(event)
            message_id = await self._send_payload(format_news_event_message(event), mode="breaking")
            if message_id:
                await repo.mark_newswire_deliveries_posted(
                    [str(row["delivery_id"])], message_id=message_id, now_ms=_now_ms()
                )
                self._last_post_at_ms = _now_ms()
                self._posted_count += 1
            else:
                await repo.mark_newswire_deliveries_failed(
                    [str(row["delivery_id"])], error=self._last_error or "send_skipped", now_ms=_now_ms()
                )
        for row in digests:
            event = _delivery_event(row)
            if event is None:
                await repo.mark_newswire_deliveries_failed(
                    [str(row["delivery_id"])], error="invalid_event_payload", now_ms=_now_ms()
                )
                continue
            await self._enrich(event)
            message_id = await self._send_payload(format_news_event_message(event), mode="digest")
            delivery_id = str(row["delivery_id"])
            if message_id:
                await repo.mark_newswire_deliveries_posted([delivery_id], message_id=message_id, now_ms=_now_ms())
                self._last_post_at_ms = _now_ms()
                self._posted_count += 1
            else:
                await repo.mark_newswire_deliveries_failed(
                    [delivery_id], error=self._last_error or "send_skipped", now_ms=_now_ms()
                )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(max(30, self.settings.newswire_digest_interval_seconds))
            try:
                await self._flush()
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self._last_error = type(exc).__name__
                log.warning("newswire_digest_flush_failed", error=type(exc).__name__)

    async def _flush(self) -> None:
        async with self._buffer_lock:
            events, self._buffer = self._buffer, []
        if not events:
            return
        publishable: list[NewswireEvent] = []
        for event in sorted(events, key=lambda item: _legacy_importance(item), reverse=True):
            reason = self._skip_reason(event)
            if reason is not None:
                self._skip(reason)
                continue
            if await self._claim(event, mode="digest"):
                publishable.append(event)
            else:
                self._skip("duplicate")
        if not publishable:
            return
        for event in publishable:
            await self._enrich(event)
            message_id = await self._send_payload(format_news_event_message(event), mode="digest")
            if message_id:
                await self._mark_posted([event.event_id], message_id)
            else:
                await self._mark_failed([event.event_id], self._last_error or "send_skipped")

    async def _enrich(self, event: NewswireEvent) -> None:
        if self.enricher is None or event.enrichment is not None:
            return
        enrichment = await self.enricher.maybe_enrich(event)
        if enrichment:
            event.enrichment = enrichment

    async def _claim(self, event: NewswireEvent, *, mode: str) -> bool:
        repo = self.repository
        now_ms = _now_ms()
        if repo is not None and callable(getattr(repo, "claim_newswire_publish", None)):
            return bool(
                await repo.claim_newswire_publish(
                    event.event_id,
                    self._channel_id,
                    mode,
                    now_ms,
                    metadata={"headline": event.headline, "source": event.source},
                )
            )
        key = f"{self._channel_id}:{event.event_id}"
        if key in self._process_claimed:
            return False
        self._process_claimed.add(key)
        return True

    async def _mark_posted(self, event_ids: list[str], message_id: str | None) -> None:
        self._last_post_at_ms = _now_ms()
        self._posted_count += len(event_ids)
        repo = self.repository
        if repo is not None and callable(getattr(repo, "mark_newswire_publish_posted", None)):
            await repo.mark_newswire_publish_posted(event_ids, self._channel_id, message_id, self._last_post_at_ms)

    async def _mark_failed(self, event_ids: list[str], error: str) -> None:
        repo = self.repository
        if repo is not None and callable(getattr(repo, "mark_newswire_publish_failed", None)):
            await repo.mark_newswire_publish_failed(event_ids, self._channel_id, error, _now_ms())

    async def _send_payload(self, payload: dict[str, Any], *, mode: str) -> str | None:
        if self.alert_sink is None or not self._channel_id:
            self._last_error = "discord_sink_missing"
            NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="skipped").inc()
            return None
        content = str(payload.get("content") or "")[: self.settings.discord_max_response_chars]
        fallback_content = str(payload.get("fallback_content") or content)[: self.settings.discord_max_response_chars]
        embeds = payload.get("embeds") if isinstance(payload.get("embeds"), list) else None
        sink: Any = self.alert_sink
        async with self._send_lock:
            await self._throttle()
            try:
                try:
                    result = await sink.send(
                        self._channel_id, content, embeds=embeds, components=payload.get("components")
                    )
                except TypeError:
                    try:
                        # Compatibility with older/fake sinks that accept embeds but no components.
                        result = await sink.send(self._channel_id, content, embeds=embeds)
                    except TypeError:
                        # Compatibility with sinks that accept only content.
                        result = await sink.send(self._channel_id, fallback_content)
                NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="ok" if result else "skipped").inc()
                if result:
                    self._last_error = None
                else:
                    self._last_error = "discord_send_skipped"
                return result
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self._last_error = type(exc).__name__
                NEWSWIRE_DISCORD_POSTS.labels(mode=mode, result="error").inc()
                log.warning("newswire_discord_send_failed", error=type(exc).__name__)
                return None

    async def _throttle(self) -> None:
        min_interval = max(0, self.settings.newswire_send_min_interval_ms)
        now = _now_ms()
        wait_ms = self._last_send_ms + min_interval - now
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        self._last_send_ms = _now_ms()

    def _skip_reason(self, event: NewswireEvent) -> str | None:
        decision = _active_policy_decision(event)
        if decision:
            action = str(decision.get("newswire_action") or "drop")
            if action in {"drop", "watch"}:
                return f"assessment_{action}"
        elif _legacy_importance(event) < self.settings.newswire_news_min_importance:
            return "low_importance"
        if event.freshness == "stale":
            return "stale"
        if self._started_at_ms is not None and event.published_at_ms is not None:
            grace_ms = max(0, self.settings.newswire_discord_startup_grace_seconds) * 1000
            if int(event.published_at_ms) < self._started_at_ms - grace_ms:
                return "startup_backlog"
        return None

    async def handle_feedback_component(
        self, custom_id: str, user_id: str | None, message_id: str | None
    ) -> str | None:
        parts = str(custom_id or "").split(":", 3)
        if len(parts) != 4 or parts[0] != "nwfb":
            return None
        _, event_id, raw_label_type, raw_value = parts
        label_type, label_value = _feedback_label(raw_label_type, raw_value)
        repo = self.repository
        if repo is None or not callable(getattr(repo, "record_newswire_eval", None)):
            return "Feedback received but no repository is configured."
        decision_id = None
        policy_version = None
        if callable(getattr(repo, "list_newswire_decisions", None)):
            decisions = await repo.list_newswire_decisions(event_id=event_id, limit=1)
            if decisions:
                decision_id = decisions[0].get("decision_id")
                policy_version = decisions[0].get("policy_version")
        if decision_id is None and callable(getattr(repo, "get_newswire_story", None)):
            story = await repo.get_newswire_story(event_id)
            assessment: dict[str, Any] = {}
            if isinstance(story, dict):
                raw_assessment = story.get("assessment")
                if isinstance(raw_assessment, dict):
                    assessment = raw_assessment
            decision_id = assessment.get("decision_id")
            policy_version = assessment.get("assessment_version")
        await repo.record_newswire_eval(
            {
                "event_id": event_id,
                "decision_id": decision_id,
                "policy_version": policy_version,
                "evaluator_type": "human",
                "evaluator_id": user_id,
                "label_type": label_type,
                "label_value": label_value,
                "confidence": 1.0,
                "reason": "discord_button",
                "created_at_ms": _now_ms(),
                "metadata": {"discord_message_id": message_id, "custom_id": custom_id},
            }
        )
        return f"Feedback recorded: {label_type}={label_value}."

    def _skip(self, reason: str) -> None:
        self._skip_counts[reason] += 1
        NEWSWIRE_DISCORD_SKIPS.labels(reason=reason).inc()

    async def send_test_message(self, *, channel_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        target = str(channel_id or self._channel_id)
        payload = {
            "content": "",
            "fallback_content": "🧪 Newswire Discord test — no market event.",
            "embeds": [
                {
                    "title": "🧪 Newswire Discord test",
                    "description": "This verifies the send-only Newswire publisher can post to #news.",
                    "color": 0x3498DB,
                }
            ],
        }
        if dry_run:
            return {
                "sent": False,
                "dry_run": True,
                "channel_id": target,
                "payload": payload,
                "publisher": self.status(),
            }
        original_channel = self.settings.newswire_news_channel_id
        try:
            if channel_id:
                self.settings.newswire_news_channel_id = target
            message_id = await self._send_payload(payload, mode="test")
            if message_id:
                self._last_post_at_ms = _now_ms()
            return {
                "sent": bool(message_id),
                "channel_id": target,
                "message_id": message_id,
                "publisher": self.status(),
            }
        finally:
            self.settings.newswire_news_channel_id = original_channel

    def status(self) -> dict[str, Any]:
        discord_status = _sink_status(self.alert_sink)
        ready = bool(self._running and discord_status.get("ready"))
        return {
            "enabled": self.enabled,
            "running": self._running,
            "ready": ready,
            "healthy": ready and self._last_error is None,
            "channel_configured": self.settings.newswire_news_channel_configured,
            "channel_id": self._channel_id if self.settings.newswire_news_channel_configured else None,
            "buffered": len(self._buffer),
            "started_at_ms": self._started_at_ms,
            "last_post_at_ms": self._last_post_at_ms,
            "last_error": self._last_error,
            "posted_count": self._posted_count,
            "skip_counts": dict(self._skip_counts),
            "thresholds": {
                "news_min_importance": self.settings.newswire_news_min_importance,
                "breaking_min_importance": self.settings.newswire_breaking_min_importance,
                "digest_interval_seconds": self.settings.newswire_digest_interval_seconds,
                "digest_max_items": self.settings.newswire_discord_digest_max_items,
                "startup_grace_seconds": self.settings.newswire_discord_startup_grace_seconds,
                "max_immediate_per_hour": self.settings.newswire_discord_max_immediate_per_hour,
            },
            "discord": discord_status,
        }

    async def status_async(self) -> dict[str, Any]:
        status = self.status()
        repo = self.repository
        if (
            self._running
            and repo is not None
            and callable(getattr(repo, "newswire_publish_status", None))
            and self._channel_id
        ):
            status["ledger"] = await repo.newswire_publish_status(self._channel_id)
        if (
            self._running
            and repo is not None
            and callable(getattr(repo, "newswire_delivery_status", None))
            and self._channel_id
        ):
            status["delivery_outbox"] = await repo.newswire_delivery_status(self._channel_id)
        return status


def _sink_status(sink: Any | None) -> dict[str, Any]:
    if sink is None:
        return {"configured": False, "ready": False}
    client = getattr(sink, "client", None)
    if client is not None and callable(getattr(client, "status", None)):
        return {"configured": True, **client.status()}
    bot = getattr(sink, "bot", None)
    discord_client = getattr(bot, "client", None)
    ready = bool(
        discord_client is not None and callable(getattr(discord_client, "is_ready", None)) and discord_client.is_ready()
    )
    return {"configured": True, "ready": ready}


def _delivery_event(row: dict[str, Any]) -> NewswireEvent | None:
    raw_payload = row.get("payload")
    if not isinstance(raw_payload, dict):
        return None
    raw_event = raw_payload.get("event")
    if not isinstance(raw_event, dict):
        return None
    try:
        return NewswireEvent.model_validate(raw_event)
    except Exception:
        return None


def _next_digest_at(now_ms: int, interval_seconds: int) -> int:
    interval_ms = max(30, int(interval_seconds)) * 1000
    return ((int(now_ms) // interval_ms) + 1) * interval_ms


def _active_policy_enabled(settings: Settings) -> bool:
    return bool(settings.newswire_policy_enabled and not settings.newswire_policy_shadow_only)


def _policy_decision(event: NewswireEvent) -> dict[str, Any]:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    decision = metadata.get("newswire_policy_decision")
    return decision if isinstance(decision, dict) else {}


def _active_policy_decision(event: NewswireEvent) -> dict[str, Any]:
    decision = _policy_decision(event)
    return {} if bool(decision.get("shadow_only")) else decision


def _legacy_importance(event: NewswireEvent) -> float:
    try:
        return float(event.metadata.get("legacy_importance_score", event.importance_score))
    except (TypeError, ValueError):
        return float(event.importance_score)


def _feedback_label(label_type: str, raw_value: str) -> tuple[str, Any]:
    normalized_type = str(label_type or "").strip().lower()
    normalized_value = str(raw_value or "").strip().lower()
    if normalized_type == "quality":
        return "quality", normalized_value == "useful"
    if normalized_type == "duplicate":
        return "duplicate", normalized_value == "true"
    if normalized_type == "stale":
        return "stale", normalized_value == "true"
    if normalized_type == "symbol":
        return "symbol_correct", normalized_value != "false"
    if normalized_type == "direction":
        return "direction_correct", normalized_value != "false"
    if normalized_type == "engine_action":
        return "correct_engine_action", normalized_value
    if normalized_type == "newswire_action":
        return "correct_newswire_action", normalized_value
    return normalized_type or "unknown", normalized_value


def _now_ms() -> int:
    return int(time.time() * 1000)
