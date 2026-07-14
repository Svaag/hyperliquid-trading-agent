from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.news_risk import NewsRiskStateMachine
from hyperliquid_trading_agent.app.engine.schemas import NormalizedEvent
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import ENGINE_NEWS_EVENTS
from hyperliquid_trading_agent.app.newswire.bus import NewswireBus
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter

log = get_logger(__name__)


class EngineNewsConsumer:
    """Bridge canonical Newswire events into the institutional engine feature spine.

    The bridge is intentionally advisory: it records point-in-time news evidence and
    derives regime features, but it never enables strategies, changes config, or creates
    order intents. Strategy selection remains under the engine's existing shadow/paper
    gates and wave flags.
    """

    def __init__(self, *, settings: Settings, bus: NewswireBus, engine_service: Any):
        self.settings = settings
        self.bus = bus
        self.engine_service = engine_service
        self._subscription_id: str | None = None
        self._refresh_task: asyncio.Task | None = None
        self.running = False
        self.received_events = 0
        self.recorded_events = 0
        self.features_created = 0
        self.skipped_events = 0
        self.error_count = 0
        self.consecutive_error_count = 0
        self.last_event_id: str | None = None
        self.last_error: str | None = None
        self.last_error_at_ms: int | None = None
        self.last_success_at_ms: int | None = None
        self.skip_reasons: dict[str, int] = {}
        repository = getattr(engine_service, "repository", None) if engine_service is not None else None
        self.risk_state = NewsRiskStateMachine(settings, repository)

    @property
    def effective_enabled(self) -> bool:
        return bool(self.settings.engine_enabled and self.settings.engine_newsfeed_enabled and self.engine_service is not None)

    async def start(self) -> None:
        if self.running or not self.effective_enabled:
            return
        # Route inside the consumer so every story revision is counted and explained.
        min_importance = 0.0
        flt = NewswireFilter(min_importance=0.0)
        self._subscription_id = await self.bus.subscribe(self.handle_event, filter=flt)
        await self.risk_state.hydrate()
        self._refresh_task = asyncio.create_task(self._refresh_risk_state_loop(), name="engine-news-risk-refresh")
        self.running = True
        log.info("engine_news_consumer_started", min_importance=min_importance)

    async def stop(self) -> None:
        if self._subscription_id is not None:
            await self.bus.unsubscribe(self._subscription_id)
            self._subscription_id = None
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        self.running = False

    async def _refresh_risk_state_loop(self) -> None:
        interval = max(10, min(60, int(self.settings.engine_news_risk_half_life_seconds) // 10))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.risk_state.refresh(feature_store=self.engine_service.feature_store)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - runtime persistence behavior
                self._record_error(exc)
                log.warning("engine_news_risk_refresh_failed", error=type(exc).__name__)
            else:
                self._record_success()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_newsfeed_enabled,
            "effective_enabled": self.effective_enabled,
            "running": self.running,
            "subscription_id": self._subscription_id,
            "received_events": self.received_events,
            "recorded_events": self.recorded_events,
            "features_created": self.features_created,
            "skipped_events": self.skipped_events,
            "skip_reasons": dict(self.skip_reasons),
            "last_event_id": self.last_event_id,
            "error_count": self.error_count,
            "consecutive_error_count": self.consecutive_error_count,
            "last_error": self.last_error,
            "last_error_at_ms": self.last_error_at_ms,
            "last_success_at_ms": self.last_success_at_ms,
            "risk_state": self.risk_state.status(),
            "settings": {
                "min_importance": self.settings.engine_news_min_importance,
                "min_source_score": self.settings.engine_news_min_source_score,
                "macro_min_importance": self.settings.engine_news_macro_min_importance,
                "macro_proxy_symbols": self.settings.engine_news_macro_proxy_symbol_list,
                "catalyst_threshold": self.settings.engine_news_catalyst_threshold,
                "catalyst_ttl_seconds": self.settings.engine_news_catalyst_ttl_seconds,
            },
        }

    async def handle_event(self, event: NewswireEvent) -> None:
        # Preserve the legacy scalar threshold for old/unassessed event producers.
        # Canonical V2 stories route by their explicit assessment actions instead.
        if (
            not _active_policy_decision(event)
            and _legacy_importance(event) < float(self.settings.engine_news_min_importance)
        ):
            return
        self.received_events += 1
        self.last_event_id = event.event_id
        failed = False
        try:
            normalized = newswire_event_to_engine_event(event, settings=self.settings)
            if normalized is None:
                self._skip("no_engine_symbols")
                return
            await self.engine_service.ledger.record(normalized)
            self.recorded_events += 1
            routed_event = event.model_copy(update={"symbols": list(normalized.symbols)})
            decision = _active_policy_decision(event)
            engine_action = str(decision.get("engine_action") or "")
            if event.action == "removed":
                await self.risk_state.retract(routed_event, feature_store=self.engine_service.feature_store)
                await self._record_story_invalidation(normalized, decision)
                self._skip("removed_no_feature_derivation")
                return
            if engine_action in {"ignore", "ledger_only"}:
                if _is_story_revision(event):
                    await self.risk_state.retract(routed_event, feature_store=self.engine_service.feature_store)
                    await self._record_story_invalidation(normalized, decision)
                self._skip(f"policy_{engine_action or 'ignored'}")
                return
            if event.source_score < float(self.settings.engine_news_min_source_score):
                self._skip("source_score_below_minimum")
                return
            features = await self.engine_service.feature_store.features_for_event(normalized)
            self.features_created += len(features)
            if decision:
                await self.risk_state.observe(routed_event, feature_store=self.engine_service.feature_store)
            ENGINE_NEWS_EVENTS.labels(result="bridged").inc()
        except Exception as exc:  # pragma: no cover - bridge must not break news fanout
            failed = True
            self._record_error(exc)
            ENGINE_NEWS_EVENTS.labels(result="error").inc()
            log.warning("engine_news_consumer_event_failed", event_id=event.event_id, error=type(exc).__name__)
        finally:
            if not failed:
                self._record_success()

    def _record_error(self, exc: Exception) -> None:
        self.error_count += 1
        self.consecutive_error_count += 1
        self.last_error = type(exc).__name__
        self.last_error_at_ms = now_ms()

    def _record_success(self) -> None:
        self.consecutive_error_count = 0
        self.last_success_at_ms = now_ms()
        self.last_error = None

    async def _record_story_invalidation(
        self,
        normalized: NormalizedEvent,
        decision: dict[str, Any],
    ) -> None:
        invalidation_decision = {
            **decision,
            "engine_action": "risk_only",
            "market_impact_score": 0.0,
            "quality_score": 0.0,
            "relevance_score": 0.0,
            "novelty_score": 0.0,
            "urgency_score": 0.0,
            "confidence": 0.0,
            "source_score": 0.0,
            "direction_score": 0.0,
            "direction_confidence": 0.0,
            "risk_score": 0.0,
        }
        payload = {**normalized.payload, "newswire_policy_decision": invalidation_decision}
        metadata = {
            **normalized.metadata,
            "newswire_policy_decision": invalidation_decision,
            "newswire_story_invalidated": True,
        }
        invalidation = normalized.model_copy(
            update={
                "event_id": f"{normalized.event_id}_invalidation",
                "payload": payload,
                "metadata": metadata,
            }
        )
        features = await self.engine_service.feature_store.features_for_event(invalidation)
        self.features_created += len(features)

    async def _on_event(self, event: NewswireEvent) -> None:
        await self.handle_event(event)

    def _skip(self, reason: str) -> None:
        self.skipped_events += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        ENGINE_NEWS_EVENTS.labels(result=reason).inc()


def newswire_event_to_engine_event(event: NewswireEvent, *, settings: Settings) -> NormalizedEvent | None:
    raw_decision = _policy_decision(event)
    decision = _active_policy_decision(event)
    if (
        event.action != "removed"
        and not _is_story_revision(event)
        and decision
        and str(decision.get("engine_action") or "ignore") == "ignore"
    ):
        return None
    symbols = engine_symbols_for_newswire_event(event, settings=settings)
    if not symbols:
        return None
    received = int(event.received_at_ms)
    computed = max(now_ms(), received)
    staleness = None if event.published_at_ms is None else max(0, received - int(event.published_at_ms))
    payload = {
        "newswire_event_id": event.event_id,
        "story_id": event.story_id or event.metadata.get("story_id"),
        "story_revision": event.story_revision or event.metadata.get("story_revision"),
        "story_sources": list(event.metadata.get("story_sources") or []),
        "story_member_event_ids": list(event.metadata.get("story_member_event_ids") or []),
        "headline": event.headline,
        "body": event.body,
        "url": event.url,
        "author": event.author,
        "published_at_ms": event.published_at_ms,
        "updated_at_ms": event.updated_at_ms,
        "action": event.action,
        "symbols": list(event.symbols),
        "engine_symbols": symbols,
        "asset_class": event.asset_class,
        "event_type": event.event_type,
        "urgency": event.urgency,
        "importance_score": event.importance_score if decision else _legacy_importance(event),
        "sentiment": event.sentiment,
        "freshness": event.freshness,
        "confidence": event.confidence,
        "source_score": event.source_score,
        "newswire_policy_decision": decision or None,
        "newswire_shadow_policy_decision": raw_decision if raw_decision and not decision else None,
        "tradability": event.tradability.model_dump(mode="json"),
        "enrichment": event.enrichment,
    }
    return NormalizedEvent(
        event_id=f"evt_{event.event_id}",
        event_type="newswire",
        asset_class=event.asset_class,
        symbols=symbols,
        source=event.source,
        provider=event.provider,
        event_ts_ms=event.published_at_ms,
        received_ts_ms=received,
        computed_ts_ms=computed,
        payload=payload,
        quality_score=max(float(event.confidence or 0.0), float(event.source_score or 0.0)),
        staleness_ms=staleness,
        metadata={
            "source_newswire_event_id": event.event_id,
            "source_newswire_metadata": event.metadata,
            "newswire_policy_decision": decision or None,
            "paper_only": True,
            "execution_authority": "none",
        },
    )


def engine_symbols_for_newswire_event(event: NewswireEvent, *, settings: Settings) -> list[str]:
    decision = _active_policy_decision(event)
    if (
        event.action != "removed"
        and not _is_story_revision(event)
        and decision
        and str(decision.get("engine_action") or "ignore") == "ignore"
    ):
        return []
    core = {symbol.upper() for symbol in settings.autonomy_core_symbols}
    symbols = {symbol.upper() for symbol in event.symbols if symbol.upper() in core}
    macro_proxy = str(decision.get("engine_action") or "") == "macro_proxy"
    broad_crypto_risk = (
        event.asset_class == "crypto"
        and str(decision.get("audience_scope") or "") == "broad_market"
        and str(decision.get("engine_action") or "") == "risk_only"
    )
    if broad_crypto_risk:
        symbols.update(core)
    if macro_proxy or (event.asset_class == "macro" and float(event.importance_score or 0.0) >= float(settings.engine_news_macro_min_importance)):
        symbols.update(symbol.upper() for symbol in settings.engine_news_macro_proxy_symbol_list)
    return sorted(symbols)


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


def _is_story_revision(event: NewswireEvent) -> bool:
    try:
        return int(event.story_revision or event.metadata.get("story_revision") or 1) > 1
    except (TypeError, ValueError):
        return event.action in {"updated", "removed"}
