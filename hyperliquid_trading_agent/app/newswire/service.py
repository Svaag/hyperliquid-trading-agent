from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    NEWSWIRE_ADAPTER_RECONNECTS,
    NEWSWIRE_ADAPTER_UP,
    NEWSWIRE_ASSESSMENTS,
    NEWSWIRE_BUS_DROPPED,
    NEWSWIRE_EVENTS,
    NEWSWIRE_MODEL_REVIEWS,
    NEWSWIRE_STORY_REVISIONS,
)
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter
from hyperliquid_trading_agent.app.newswire.adapters.rss import RssAdapter
from hyperliquid_trading_agent.app.newswire.assessment import (
    ASSESSMENT_VERSION,
    NewswireAssessor,
    SelectiveAssessmentReviewer,
    assessment_to_decision,
)
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus, NewswireBus
from hyperliquid_trading_agent.app.newswire.normalize import normalize, now_ms
from hyperliquid_trading_agent.app.newswire.riskgate import HaltStateGate
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter, NewswireStory, RawNewsItem
from hyperliquid_trading_agent.app.newswire.stories import NewswireStoryClusterer
from hyperliquid_trading_agent.app.newswire.watchlist import DynamicNewswireWatchSet, EntityMatch, resolve_entities

log = get_logger(__name__)


class NewswireService:
    """Free-standing ingestion gateway: supervises adapters, normalizes + scores + gates
    deterministically, then publishes canonical events to the bus and persists them."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any | None = None,
        bus: NewswireBus | None = None,
        model_gateway: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.bus: NewswireBus = bus or InProcessNewswireBus()
        self.halt_gate = HaltStateGate()
        self.adapters: list[NewswireAdapter] = []
        self.running = False
        self._tasks: list[asyncio.Task] = []
        self._adapter_tasks: list[asyncio.Task] = []
        self._worker_tasks: list[asyncio.Task] = []
        self._ingest_queue: asyncio.Queue[RawNewsItem] = asyncio.Queue(maxsize=max(1, settings.newswire_ingest_queue_size))
        # Story revision assignment, model review, persistence, and fanout form one
        # ordered transaction. Serializing that section prevents concurrent workers
        # from publishing revision 2 before revision 1 or overwriting a newer story.
        self._story_pipeline_lock = asyncio.Lock()
        self._by_id: dict[str, NewswireEvent] = {}
        self._symbols_universe = settings.newswire_symbols_universe
        self.last_event_at_ms: int | None = None
        self.last_event_per_source: dict[str, int] = {}
        self.adapter_errors = 0
        self.adapter_errors_by_name: dict[str, int] = {}
        self.adapter_reconnects_by_name: dict[str, int] = {}
        self.adapter_last_error: dict[str, dict[str, Any]] = {}
        self.dropped_events_by_reason: dict[str, int] = {}
        self.persisted_event_count = 0
        self.persisted_decision_count = 0
        self.persisted_story_count = 0
        self.persisted_story_revision_count = 0
        self.persistence_errors = 0
        self.last_persistence_error: dict[str, Any] | None = None
        self._policy_params: dict[str, Any] = {}
        self._policy_version = ASSESSMENT_VERSION
        self._policy_loaded_at_ms = 0
        self.watch_set = DynamicNewswireWatchSet(settings, repository)
        self.story_clusterer = NewswireStoryClusterer(max_stories=settings.newswire_story_max_buffer)
        self.assessor = NewswireAssessor(settings)
        self.model_reviewer = SelectiveAssessmentReviewer(settings, model_gateway)
        self.story_revision_count = 0
        self.model_review_count = 0
        self._story_hydrated = False

    def build_adapters(self) -> list[NewswireAdapter]:
        adapters: list[NewswireAdapter] = []
        settings = self.settings
        if settings.newswire_rss_feed_urls:
            adapters.append(RssAdapter(settings.newswire_rss_feed_urls, poll_seconds=settings.newswire_rss_poll_seconds))
        if settings.alpaca_news_enabled and settings.alpaca_api_key and settings.alpaca_api_secret:
            from hyperliquid_trading_agent.app.newswire.adapters.alpaca_ws import AlpacaNewsAdapter

            adapters.append(
                AlpacaNewsAdapter(
                    ws_url=settings.alpaca_news_ws_url,
                    api_key=settings.alpaca_api_key,
                    api_secret=settings.alpaca_api_secret,
                    symbols=settings.alpaca_news_symbol_list,
                )
            )
        if settings.trading_economics_enabled and settings.trading_economics_api_key:
            from hyperliquid_trading_agent.app.newswire.adapters.trading_economics_ws import TradingEconomicsAdapter

            adapters.append(TradingEconomicsAdapter(ws_url=settings.trading_economics_ws_url, api_key=settings.trading_economics_api_key))
        if settings.x_newswire_enabled and settings.x_bearer_token:
            from hyperliquid_trading_agent.app.newswire.adapters.x_curated import XCuratedAdapter

            adapters.append(XCuratedAdapter(settings=settings))
        return adapters

    async def start(self) -> None:
        if not self.settings.newswire_enabled or self.running:
            return
        self.running = True
        await self.watch_set.refresh_if_due(force=True)
        await self._hydrate_stories()
        worker_count = max(1, int(self.settings.newswire_ingest_worker_count))
        for index in range(worker_count):
            task = asyncio.create_task(self._ingest_worker(), name=f"newswire-ingest-{index}")
            self._worker_tasks.append(task)
            self._tasks.append(task)
        self.adapters = self.build_adapters()
        for adapter in self.adapters:
            task = asyncio.create_task(self._supervise(adapter), name=f"newswire-{adapter.name}")
            self._adapter_tasks.append(task)
            self._tasks.append(task)
        log.info("newswire_started", adapters=[a.name for a in self.adapters])

    async def stop(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:  # pragma: no cover - adapter cleanup best-effort
                pass
        for task in self._adapter_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._adapter_tasks = []
        if not self._ingest_queue.empty():
            try:
                await asyncio.wait_for(self._ingest_queue.join(), timeout=10)
            except TimeoutError:
                pass
        self.running = False
        for task in self._worker_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_tasks = []
        self._tasks = []

    async def _supervise(self, adapter: NewswireAdapter) -> None:
        backoff = 5
        while self.running:
            NEWSWIRE_ADAPTER_UP.labels(adapter=adapter.name).set(1)
            try:
                await adapter.run(self._enqueue)
                break  # clean return = stop requested
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - external source behavior
                self.adapter_errors += 1
                self.adapter_errors_by_name[adapter.name] = self.adapter_errors_by_name.get(adapter.name, 0) + 1
                self.adapter_reconnects_by_name[adapter.name] = self.adapter_reconnects_by_name.get(adapter.name, 0) + 1
                self.adapter_last_error[adapter.name] = {"error": type(exc).__name__, "detail": str(exc)[:500], "at_ms": now_ms(), "next_backoff_seconds": backoff}
                NEWSWIRE_ADAPTER_UP.labels(adapter=adapter.name).set(0)
                NEWSWIRE_ADAPTER_RECONNECTS.labels(adapter=adapter.name).inc()
                log.warning("newswire_adapter_restart", adapter=adapter.name, error=type(exc).__name__, detail=str(exc)[:200], backoff_seconds=backoff)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(60, backoff * 2)
            else:
                backoff = 5
        NEWSWIRE_ADAPTER_UP.labels(adapter=adapter.name).set(0)

    async def _enqueue(self, raw: RawNewsItem) -> None:
        try:
            self._ingest_queue.put_nowait(raw)
        except asyncio.QueueFull:
            self._record_drop("ingest_queue_full")
            NEWSWIRE_BUS_DROPPED.labels(reason="ingest_queue_full").inc()

    async def _ingest_worker(self) -> None:
        while self.running or not self._ingest_queue.empty():
            try:
                raw = await self._ingest_queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._ingest(raw)
            except Exception as exc:  # pragma: no cover - per-item isolation
                self._record_drop("classification_error")
                log.warning("newswire_ingest_item_failed", error=type(exc).__name__)
            finally:
                self._ingest_queue.task_done()

    async def _ingest(self, raw: RawNewsItem) -> NewswireEvent | None:
        async with self._story_pipeline_lock:
            return await self._ingest_serial(raw)

    async def _ingest_serial(self, raw: RawNewsItem) -> NewswireEvent | None:
        event = normalize(raw, symbols_universe=self._symbols_universe, received_at_ms=now_ms())
        if event is None:
            return None
        if event.action == "created" and event.event_id in self._by_id:
            self._record_drop("duplicate")
            NEWSWIRE_BUS_DROPPED.labels(reason="duplicate").inc()
            return None
        event = self.halt_gate.apply(event)
        snapshot = await self.watch_set.refresh_if_due()
        entity = resolve_entities(event, snapshot)
        event = event.model_copy(update={"symbols": entity.symbols, "topics": entity.topics})
        story, update_type = self.story_clusterer.upsert(event)
        if update_type == "duplicate" and story.assessment is not None:
            projection = story.to_event(update_type="duplicate")
            event = event.model_copy(
                update={
                    "schema_version": 2,
                    "story_id": story.story_id,
                    "story_revision": story.revision,
                    "topics": list(story.topics),
                    "assessment": story.assessment,
                    "importance_score": story.assessment.priority_score,
                    "metadata": {
                        **event.metadata,
                        **projection.metadata,
                        "legacy_importance_score": event.importance_score,
                    },
                }
            )
            self._index(event)
            self._record_drop("story_duplicate")
            NEWSWIRE_BUS_DROPPED.labels(reason="story_duplicate").inc()
            NEWSWIRE_EVENTS.labels(provider=event.provider).inc()
            if self.repository is not None and getattr(self.repository, "enabled", False):
                await self._persist_event(event)
            return event
        story_entity = EntityMatch(
            symbols=list(story.symbols),
            reasons={**entity.reasons, **{symbol: entity.reasons.get(symbol, ["story_member_symbol"]) for symbol in story.symbols}},
            topics=list(story.topics),
            watch_priority=snapshot.priority_for(story.symbols),
        )
        assessment_event = story.to_event(update_type=update_type)
        assessment = self.assessor.assess(assessment_event, story, story_entity)
        if assessment.model_review_state == "pending":
            review, review_state = await self.model_reviewer.review(assessment_event, assessment)
            assessment = self.assessor.apply_model_review(
                assessment_event,
                story,
                story_entity,
                assessment,
                review,
                state=review_state,  # type: ignore[arg-type]
            )
            if assessment.model_review_state == "applied":
                self.model_review_count += 1
            NEWSWIRE_MODEL_REVIEWS.labels(result=assessment.model_review_state).inc()
        story = story.model_copy(
            update={
                "assessment": assessment,
                "metadata": {
                    **story.metadata,
                    "last_update_type": update_type,
                    "newswire_routing_mode": self.settings.newswire_routing_mode,
                    "legacy_importance_score": event.importance_score,
                },
            }
        )
        self.story_clusterer.replace(story)
        NEWSWIRE_ASSESSMENTS.labels(
            feed_action=assessment.feed_action,
            engine_action=assessment.engine_action,
            watch_priority=assessment.watch_priority,
        ).inc()
        NEWSWIRE_STORY_REVISIONS.labels(update_type=update_type).inc()
        event = event.model_copy(
            update={
                "schema_version": 2,
                "story_id": story.story_id,
                "story_revision": story.revision,
                "topics": list(story.topics),
                "assessment": assessment,
                "importance_score": assessment.priority_score,
                "sentiment": assessment.direction,
                "metadata": {
                    **event.metadata,
                    "story_id": story.story_id,
                    "story_revision": story.revision,
                    "legacy_importance_score": event.importance_score,
                    "newswire_assessment": assessment.model_dump(mode="json"),
                },
            }
        )
        decision = assessment_to_decision(event, story, assessment)
        event.metadata["newswire_policy_decision"] = {
            "decision_id": assessment.decision_id,
            "policy_version": assessment.assessment_version,
            "policy_type": "static",
            "shadow_only": self.settings.newswire_routing_mode == "shadow",
            "newswire_action": assessment.feed_action,
            "engine_action": assessment.engine_action,
            "quality_score": decision.quality_score,
            "market_impact_score": assessment.impact_score,
            "relevance_score": assessment.relevance_score,
            "novelty_score": assessment.novelty_score,
            "urgency_score": assessment.urgency_score,
            "source_score": assessment.source_quality_score / 100.0,
            "confidence": event.confidence,
            "direction_score": decision.direction_score,
            "direction_confidence": assessment.direction_confidence,
            "risk_score": assessment.risk_severity,
            "reasons": assessment.reason_codes,
            "penalties": assessment.penalty_codes,
        }
        self._index(event)
        NEWSWIRE_EVENTS.labels(provider=event.provider).inc()
        if self.repository is not None and getattr(self.repository, "enabled", False):
            await self._persist_event(event)
            await self._persist_decision(decision.model_dump(mode="json"))
            await self._persist_story(story.model_dump(mode="json"))
            revision = self.story_clusterer.revision(story, update_type)
            await self._persist_story_revision(revision.model_dump(mode="json"))
        self.story_revision_count += 1
        await self.bus.publish(story.to_event(update_type=update_type))
        return event

    async def _hydrate_stories(self) -> None:
        if self._story_hydrated:
            return
        self._story_hydrated = True
        repository = self.repository
        if repository is None or not getattr(repository, "enabled", False):
            return
        method = getattr(repository, "list_newswire_stories", None)
        if not callable(method):
            return
        try:
            from hyperliquid_trading_agent.app.newswire.schemas import NewswireStory

            rows = await method(limit=self.settings.newswire_story_max_buffer)
            self.story_clusterer.hydrate(NewswireStory.model_validate(row) for row in rows)
        except Exception as exc:  # pragma: no cover
            log.warning("newswire_story_hydration_failed", error=type(exc).__name__)

    async def _persist_story(self, story: dict[str, Any]) -> None:
        method = getattr(self.repository, "upsert_newswire_story", None)
        if not callable(method):
            return
        try:
            result = await method(story)
        except Exception as exc:  # pragma: no cover
            self._record_persistence_failure("story", story.get("story_id"), exc)
            return
        if result:
            self.persisted_story_count += 1
        else:
            self._record_persistence_failure("story", story.get("story_id"), RuntimeError("record_returned_none"))

    async def _persist_story_revision(self, revision: dict[str, Any]) -> None:
        method = getattr(self.repository, "record_newswire_story_revision", None)
        if not callable(method):
            return
        try:
            result = await method(revision)
        except Exception as exc:  # pragma: no cover
            self._record_persistence_failure("story_revision", revision.get("revision_id"), exc)
            return
        if result:
            self.persisted_story_revision_count += 1
        else:
            self._record_persistence_failure(
                "story_revision",
                revision.get("revision_id"),
                RuntimeError("record_returned_none"),
            )

    def _record_persistence_failure(self, record_type: str, record_id: Any, exc: Exception) -> None:
        self.persistence_errors += 1
        self.last_persistence_error = {
            "record_type": record_type,
            "record_id": record_id,
            "error": type(exc).__name__,
            "detail": str(exc)[:500],
            "at_ms": now_ms(),
        }
        log.warning(
            "newswire_record_persist_failed",
            record_type=record_type,
            record_id=record_id,
            error=type(exc).__name__,
        )

    async def _persist_decision(self, decision: dict[str, Any]) -> None:
        repository = self.repository
        if repository is None or not callable(getattr(repository, "record_newswire_decision", None)):
            return
        try:
            result = await repository.record_newswire_decision(decision)
        except Exception as exc:  # pragma: no cover
            self.persistence_errors += 1
            self.last_persistence_error = {"decision_id": decision.get("decision_id"), "error": type(exc).__name__, "detail": str(exc)[:500], "at_ms": now_ms()}
            log.warning("newswire_decision_persist_failed", decision_id=decision.get("decision_id"), error=type(exc).__name__, detail=str(exc)[:200])
            return
        if result:
            self.persisted_decision_count += 1

    async def _policy_context(self) -> tuple[str, dict[str, Any]]:
        configured = self.settings.newswire_active_policy_version.strip()
        fallback = configured or ASSESSMENT_VERSION
        repository = self.repository
        if repository is None or not getattr(repository, "enabled", False) or not callable(getattr(repository, "list_newswire_policy_versions", None)):
            return fallback, {}
        current = now_ms()
        if current - self._policy_loaded_at_ms < 30_000:
            return self._policy_version, dict(self._policy_params)
        self._policy_loaded_at_ms = current
        try:
            if configured:
                policies = await repository.list_newswire_policy_versions(limit=1000)
                selected = next((item for item in policies if item.get("policy_version") == configured), None)
            else:
                policies = await repository.list_newswire_policy_versions(status="promoted", limit=1)
                selected = policies[0] if policies else None
        except Exception as exc:  # pragma: no cover
            log.warning("newswire_policy_lookup_failed", error=type(exc).__name__)
            return self._policy_version or fallback, dict(self._policy_params)
        if selected:
            self._policy_version = str(selected.get("policy_version") or fallback)
            self._policy_params = dict(selected.get("params") or {})
        else:
            self._policy_version = fallback
            self._policy_params = {}
        return self._policy_version, dict(self._policy_params)

    async def _persist_event(self, event: NewswireEvent) -> None:
        repository = self.repository
        if repository is None:
            return
        try:
            result = await repository.record_newswire_event(event.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - persistence must not break ingestion
            self.persistence_errors += 1
            self.last_persistence_error = {"event_id": event.event_id, "error": type(exc).__name__, "detail": str(exc)[:500], "at_ms": now_ms()}
            log.warning("newswire_event_persist_failed", event_id=event.event_id, error=type(exc).__name__, detail=str(exc)[:200])
            return
        if result is None:
            self.persistence_errors += 1
            self.last_persistence_error = {"event_id": event.event_id, "error": "record_returned_none", "detail": "repository did not acknowledge event", "at_ms": now_ms()}
            log.warning("newswire_event_persist_unacknowledged", event_id=event.event_id)
            return
        self.persisted_event_count += 1
        self.last_persistence_error = None

    def _record_drop(self, reason: str) -> None:
        self.dropped_events_by_reason[reason] = self.dropped_events_by_reason.get(reason, 0) + 1

    def _index(self, event: NewswireEvent) -> None:
        self._by_id.pop(event.event_id, None)  # move-to-end on update
        self._by_id[event.event_id] = event
        cap = max(1, self.settings.newswire_max_events_buffer)
        while len(self._by_id) > cap:
            oldest = next(iter(self._by_id))
            self._by_id.pop(oldest, None)
        self.last_event_at_ms = event.received_at_ms
        self.last_event_per_source[event.source] = event.received_at_ms

    # --- query surface for the HTTP gateway ---------------------------------

    def get_event(self, event_id: str) -> NewswireEvent | None:
        return self._by_id.get(event_id)

    def list_events(self, *, filter: NewswireFilter | None = None, limit: int = 100) -> list[NewswireEvent]:
        events = list(reversed(self._by_id.values()))
        if filter is not None:
            events = [event for event in events if filter.matches(event)]
        return events[:limit]

    def get_story(self, story_id: str) -> NewswireStory | None:
        return self.story_clusterer.get(story_id)

    def list_stories(self, *, limit: int = 100) -> list[NewswireStory]:
        return self.story_clusterer.list(limit=limit)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.newswire_enabled,
            "running": self.running,
            "adapters": [self._adapter_status(adapter) for adapter in self.adapters],
            "configured_adapter_names": [adapter.name for adapter in self.adapters],
            "adapter_errors": self.adapter_errors,
            "adapter_errors_by_name": dict(self.adapter_errors_by_name),
            "adapter_reconnects_by_name": dict(self.adapter_reconnects_by_name),
            "adapter_last_error": dict(self.adapter_last_error),
            "dropped_events_by_reason": dict(self.dropped_events_by_reason),
            "repository_enabled": bool(self.repository is not None and getattr(self.repository, "enabled", False)),
            "persisted_event_count": self.persisted_event_count,
            "persisted_decision_count": self.persisted_decision_count,
            "persisted_story_count": self.persisted_story_count,
            "persisted_story_revision_count": self.persisted_story_revision_count,
            "persistence_errors": self.persistence_errors,
            "last_persistence_error": self.last_persistence_error,
            "policy": {
                "enabled": self.settings.newswire_policy_enabled,
                "shadow_only": self.settings.newswire_routing_mode == "shadow",
                "routing_mode": self.settings.newswire_routing_mode,
                "active_policy_version": self._policy_version,
                "configured_policy_version": self.settings.newswire_active_policy_version.strip() or None,
                "loaded_at_ms": self._policy_loaded_at_ms or None,
                "learner": self._policy_params.get("learner"),
                "ready": self._policy_params.get("ready"),
            },
            "buffered_events": len(self._by_id),
            "ingest_queue_depth": self._ingest_queue.qsize(),
            "ingest_worker_count": len(self._worker_tasks),
            "story_revisions": self.story_revision_count,
            "model_reviews_applied": self.model_review_count,
            "stories": self.story_clusterer.status(),
            "watch_set": self.watch_set.status(),
            "model_reviewer": self.model_reviewer.status(),
            "last_event_at_ms": self.last_event_at_ms,
            "last_event_per_source": self.last_event_per_source,
            "halted_symbols": self.halt_gate.halted_symbols(),
            "bus": self.bus.status(),
        }

    def _adapter_status(self, adapter: NewswireAdapter) -> dict[str, Any]:
        status = dict(adapter.status())
        status.setdefault("name", adapter.name)
        status["errors"] = self.adapter_errors_by_name.get(adapter.name, 0)
        status["reconnects"] = self.adapter_reconnects_by_name.get(adapter.name, 0)
        status["last_error"] = self.adapter_last_error.get(adapter.name)
        return status
