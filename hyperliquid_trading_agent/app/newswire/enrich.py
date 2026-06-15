from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import NEWSWIRE_ENRICH_CALLS
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent

log = get_logger(__name__)

_SYSTEM = (
    "You are a markets-desk editor for a crypto and macro trading audience. "
    "Given one news item, write a neutral one-sentence summary and a one-sentence "
    "'why it matters' for traders. Be concise and factual. Do NOT give trade advice, "
    "price targets, or buy/sell calls."
)


class NewsEnrichment(BaseModel):
    summary: str = ""
    why_it_matters: str = ""


class Enricher:
    """Optional LLM second pass — strictly after deterministic parsing, only for output.

    Rate-limited per hour; on any failure it returns ``None`` and the caller falls back
    to the deterministic event. Never used to gate tradability.
    """

    def __init__(self, *, settings: Settings, model_gateway: Any | None):
        self.settings = settings
        self.model_gateway = model_gateway
        self._call_times: list[float] = []

    def should_enrich(self, event: NewswireEvent) -> bool:
        return (
            self.settings.newswire_llm_enrich_enabled
            and self.model_gateway is not None
            and event.importance_score >= self.settings.newswire_llm_enrich_min_importance
        )

    def _within_budget(self) -> bool:
        now = time.time()
        self._call_times = [ts for ts in self._call_times if now - ts < 3600]
        return len(self._call_times) < max(0, self.settings.newswire_llm_enrich_max_calls_per_hour)

    async def maybe_enrich(self, event: NewswireEvent) -> dict[str, str] | None:
        gateway = self.model_gateway
        if gateway is None or not self.should_enrich(event) or not self._within_budget():
            return None
        self._call_times.append(time.time())
        prompt = (
            f"Headline: {event.headline}\n"
            f"Body: {event.body[:1200]}\n"
            f"Source: {event.source} | type: {event.event_type} | symbols: {', '.join(event.symbols) or 'n/a'}"
        )
        try:
            response = await gateway.complete_structured(prompt, _SYSTEM, NewsEnrichment, max_tokens=300)
            data: NewsEnrichment = response.parsed
            NEWSWIRE_ENRICH_CALLS.labels(result="ok").inc()
            return {"summary": data.summary, "why_it_matters": data.why_it_matters}
        except Exception as exc:  # pragma: no cover - model behavior
            NEWSWIRE_ENRICH_CALLS.labels(result="error").inc()
            log.warning("newswire_enrich_failed", error=type(exc).__name__)
            return None
