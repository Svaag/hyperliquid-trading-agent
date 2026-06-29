from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.bus import NewswireBus
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, NewswireFilter

log = get_logger(__name__)


class AgentNewsConsumer:
    """Push-feeds newswire events into the autonomy market map (replacing the 60s poll).

    Bridges the canonical ``NewswireEvent`` back to the legacy ``NewsEvent`` the reducer
    and signal engine already understand, so news-sourced ``SignalEvidence`` keeps flowing
    unchanged — just push-driven now.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        bus: NewswireBus,
        autonomy_service: Any,
        repository: Any | None = None,
        event_evaluation_service: Any | None = None,
        world_model_service: Any | None = None,
    ):
        self.settings = settings
        self.bus = bus
        self.autonomy_service = autonomy_service
        self.repository = repository
        self.event_evaluation_service = event_evaluation_service
        self.world_model_service = world_model_service
        self._subscription_id: str | None = None

    async def start(self) -> None:
        if not self.settings.newswire_enabled or self.autonomy_service is None:
            return
        flt = NewswireFilter(min_importance=self.settings.newswire_agent_min_importance)
        self._subscription_id = await self.bus.subscribe(self._on_event, filter=flt)
        log.info("newswire_agent_consumer_started")

    async def stop(self) -> None:
        if self._subscription_id is not None:
            await self.bus.unsubscribe(self._subscription_id)
            self._subscription_id = None

    async def _on_event(self, event: NewswireEvent) -> None:
        news_event = event.to_news_event()
        self.autonomy_service.reducer.apply_news([news_event], timestamp_ms=event.received_at_ms)
        self.autonomy_service.news_events[news_event.id] = news_event
        if self.repository is not None and getattr(self.repository, "enabled", False):
            await self.repository.record_news_event(news_event.model_dump(mode="json"))
        if self.world_model_service is not None and callable(getattr(self.world_model_service, "observe_newswire_event", None)):
            await self.world_model_service.observe_newswire_event(event)
        if self.event_evaluation_service is not None:
            market_regime = self.autonomy_service.reducer.snapshot().risk_regime if self.autonomy_service is not None else "unknown"
            await self.event_evaluation_service.create_for_newswire_event(event, market_regime=market_regime)
