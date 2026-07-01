from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
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
        self.running = False
        self.received_events = 0
        self.recorded_events = 0
        self.features_created = 0
        self.skipped_events = 0
        self.error_count = 0
        self.last_event_id: str | None = None
        self.last_error: str | None = None
        self.skip_reasons: dict[str, int] = {}

    @property
    def effective_enabled(self) -> bool:
        return bool(self.settings.engine_enabled and self.settings.engine_newsfeed_enabled and self.engine_service is not None)

    async def start(self) -> None:
        if self.running or not self.effective_enabled:
            return
        flt = NewswireFilter(min_importance=float(self.settings.engine_news_min_importance))
        self._subscription_id = await self.bus.subscribe(self.handle_event, filter=flt)
        self.running = True
        log.info("engine_news_consumer_started", min_importance=self.settings.engine_news_min_importance)

    async def stop(self) -> None:
        if self._subscription_id is not None:
            await self.bus.unsubscribe(self._subscription_id)
            self._subscription_id = None
        self.running = False

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
            "last_error": self.last_error,
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
        self.received_events += 1
        self.last_event_id = event.event_id
        try:
            normalized = newswire_event_to_engine_event(event, settings=self.settings)
            if normalized is None:
                self._skip("no_engine_symbols")
                return
            await self.engine_service.ledger.record(normalized)
            self.recorded_events += 1
            if event.action == "removed":
                self._skip("removed_no_feature_derivation")
                return
            if event.source_score < float(self.settings.engine_news_min_source_score):
                self._skip("source_score_below_minimum")
                return
            features = await self.engine_service.feature_store.features_for_event(normalized)
            self.features_created += len(features)
            ENGINE_NEWS_EVENTS.labels(result="bridged").inc()
        except Exception as exc:  # pragma: no cover - bridge must not break news fanout
            self.error_count += 1
            self.last_error = type(exc).__name__
            ENGINE_NEWS_EVENTS.labels(result="error").inc()
            log.warning("engine_news_consumer_event_failed", event_id=event.event_id, error=type(exc).__name__)

    async def _on_event(self, event: NewswireEvent) -> None:
        await self.handle_event(event)

    def _skip(self, reason: str) -> None:
        self.skipped_events += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        ENGINE_NEWS_EVENTS.labels(result=reason).inc()


def newswire_event_to_engine_event(event: NewswireEvent, *, settings: Settings) -> NormalizedEvent | None:
    symbols = engine_symbols_for_newswire_event(event, settings=settings)
    if not symbols:
        return None
    received = int(event.received_at_ms)
    computed = max(now_ms(), received)
    staleness = None if event.published_at_ms is None else max(0, received - int(event.published_at_ms))
    payload = {
        "newswire_event_id": event.event_id,
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
        "importance_score": event.importance_score,
        "sentiment": event.sentiment,
        "freshness": event.freshness,
        "confidence": event.confidence,
        "source_score": event.source_score,
        "tradability": event.tradability.model_dump(mode="json"),
        "enrichment": event.enrichment,
    }
    return NormalizedEvent(
        event_id=f"evt_{event.event_id}",
        event_type="newswire",
        asset_class=event.asset_class,  # type: ignore[arg-type]
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
            "paper_only": True,
            "execution_authority": "none",
        },
    )


def engine_symbols_for_newswire_event(event: NewswireEvent, *, settings: Settings) -> list[str]:
    core = {symbol.upper() for symbol in settings.autonomy_core_symbols}
    symbols = {symbol.upper() for symbol in event.symbols if symbol.upper() in core}
    if event.asset_class == "macro" and float(event.importance_score or 0.0) >= float(settings.engine_news_macro_min_importance):
        symbols.update(symbol.upper() for symbol in settings.engine_news_macro_proxy_symbol_list)
    return sorted(symbols)
