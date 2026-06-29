from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.world_model.reducer import now_ms
from hyperliquid_trading_agent.app.world_model.schemas import PredictionMarketSignal, WorldEvent
from hyperliquid_trading_agent.app.world_model.service import WorldModelService


@dataclass
class AdapterStatus:
    name: str
    enabled: bool
    last_poll_at_ms: int | None = None
    next_poll_at_ms: int | None = None
    last_error: str | None = None
    last_skipped_reason: str | None = None
    error_count: int = 0
    last_counts: dict[str, int] = field(default_factory=dict)


class WorldSourceAdapter:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = AdapterStatus(name=self.name, enabled=self.enabled)
        self._seen_source_ids: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return False

    async def poll(self, service: WorldModelService) -> dict[str, int]:
        return {"events": 0, "prediction_signals": 0}

    def status_dict(self) -> dict[str, Any]:
        return {
            "name": self.status.name,
            "enabled": self.status.enabled,
            "last_poll_at_ms": self.status.last_poll_at_ms,
            "next_poll_at_ms": self.status.next_poll_at_ms,
            "last_error": self.status.last_error,
            "last_skipped_reason": self.status.last_skipped_reason,
            "error_count": self.status.error_count,
            "last_counts": self.status.last_counts,
            "execution_authority": "none",
        }

    async def run_poll(self, service: WorldModelService, *, force: bool = False) -> dict[str, Any]:
        self.status.enabled = self.enabled
        if not self.enabled:
            return {"name": self.name, "enabled": False, "counts": {"events": 0, "prediction_signals": 0}}
        current_ms = now_ms()
        interval_ms = int(max(0.0, self.settings.world_model_adapter_poll_interval_seconds) * 1000)
        if not force and self.status.last_poll_at_ms is not None and current_ms - self.status.last_poll_at_ms < interval_ms:
            next_poll = self.status.last_poll_at_ms + interval_ms
            self.status.next_poll_at_ms = next_poll
            self.status.last_skipped_reason = "poll_interval"
            return {
                "name": self.name,
                "enabled": True,
                "skipped": True,
                "reason": "poll_interval",
                "next_poll_at_ms": next_poll,
                "counts": {"events": 0, "prediction_signals": 0},
            }
        try:
            counts = await self.poll(service)
            self.status.last_poll_at_ms = now_ms()
            self.status.next_poll_at_ms = self.status.last_poll_at_ms + interval_ms if interval_ms else None
            self.status.last_error = None
            self.status.last_skipped_reason = None
            self.status.last_counts = counts
            return {"name": self.name, "enabled": True, "counts": counts}
        except Exception as exc:  # pragma: no cover - external APIs are best-effort
            self.status.error_count += 1
            self.status.last_error = type(exc).__name__
            return {"name": self.name, "enabled": True, "error": type(exc).__name__, "counts": {"events": 0, "prediction_signals": 0}}

    def _seen_recently(self, source_id: str, *, now: int | None = None) -> bool:
        current_ms = now or now_ms()
        ttl_ms = int(max(0.0, self.settings.world_model_adapter_dedupe_ttl_seconds) * 1000)
        expired = [key for key, seen_at in self._seen_source_ids.items() if ttl_ms and current_ms - seen_at > ttl_ms]
        for key in expired:
            self._seen_source_ids.pop(key, None)
        if source_id in self._seen_source_ids:
            return True
        self._seen_source_ids[source_id] = current_ms
        return False


class PolymarketAdapter(WorldSourceAdapter):
    name = "polymarket"

    @property
    def enabled(self) -> bool:
        return self.settings.world_model_adapters_enabled and self.settings.world_model_polymarket_enabled

    async def poll(self, service: WorldModelService) -> dict[str, int]:
        url = self.settings.world_model_polymarket_base_url.rstrip("/") + "/markets"
        params = {"active": "true", "closed": "false", "limit": self.settings.world_model_adapter_max_items}
        async with httpx.AsyncClient(timeout=self.settings.world_model_adapter_timeout_seconds) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        markets = data.get("markets", data) if isinstance(data, dict) else data
        count = 0
        duplicate_count = 0
        seen_signal_ids: set[str] = set()
        for market in list(markets or [])[: self.settings.world_model_adapter_max_items]:
            signals = _polymarket_signals(market, self.settings)
            for signal in signals:
                if signal.signal_id in seen_signal_ids:
                    duplicate_count += 1
                    continue
                seen_signal_ids.add(signal.signal_id)
                signal = _with_probability_delta(service, signal)
                await service.observe_prediction_market_signal(signal)
                count += 1
        return {"events": 0, "prediction_signals": count, "duplicates_skipped": duplicate_count}


class KalshiAdapter(WorldSourceAdapter):
    name = "kalshi"

    @property
    def enabled(self) -> bool:
        return self.settings.world_model_adapters_enabled and self.settings.world_model_kalshi_enabled

    async def poll(self, service: WorldModelService) -> dict[str, int]:
        url = self.settings.world_model_kalshi_base_url.rstrip("/") + "/markets"
        params = {"status": "open", "limit": self.settings.world_model_adapter_max_items}
        async with httpx.AsyncClient(timeout=self.settings.world_model_adapter_timeout_seconds) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        markets = data.get("markets", data) if isinstance(data, dict) else data
        count = 0
        duplicate_count = 0
        seen_signal_ids: set[str] = set()
        for market in list(markets or [])[: self.settings.world_model_adapter_max_items]:
            signal = _kalshi_signal(market, self.settings)
            if signal is not None:
                if signal.signal_id in seen_signal_ids:
                    duplicate_count += 1
                    continue
                seen_signal_ids.add(signal.signal_id)
                signal = _with_probability_delta(service, signal)
                await service.observe_prediction_market_signal(signal)
                count += 1
        return {"events": 0, "prediction_signals": count, "duplicates_skipped": duplicate_count}


class XRecentSearchAdapter(WorldSourceAdapter):
    name = "x"

    @property
    def enabled(self) -> bool:
        return self.settings.world_model_adapters_enabled and self.settings.world_model_x_enabled and bool(self.settings.x_bearer_token)

    async def poll(self, service: WorldModelService) -> dict[str, int]:
        headers = {"Authorization": f"Bearer {self.settings.x_bearer_token}"}
        params = {
            "query": self.settings.world_model_x_query,
            "max_results": min(100, max(10, self.settings.world_model_adapter_max_items)),
            "tweet.fields": "created_at,public_metrics,entities,author_id",
        }
        async with httpx.AsyncClient(timeout=self.settings.world_model_adapter_timeout_seconds) as client:
            response = await client.get("https://api.x.com/2/tweets/search/recent", headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
        count = 0
        duplicate_count = 0
        for tweet in data.get("data", [])[: self.settings.world_model_adapter_max_items]:
            source_id = f"x:{tweet.get('id') or _stable_id('x_text', tweet.get('text'))}"
            if self._seen_recently(source_id):
                duplicate_count += 1
                continue
            await service.observe_event(_x_event(tweet, self.settings))
            count += 1
        return {"events": count, "prediction_signals": 0, "duplicates_skipped": duplicate_count}


class TavilyAdapter(WorldSourceAdapter):
    name = "tavily"

    @property
    def enabled(self) -> bool:
        return self.settings.world_model_adapters_enabled and self.settings.world_model_tavily_enabled and bool(self.settings.tavily_api_key)

    async def poll(self, service: WorldModelService) -> dict[str, int]:
        queries = [item.strip() for item in self.settings.world_model_tavily_queries.split(",") if item.strip()]
        count = 0
        duplicate_count = 0
        async with httpx.AsyncClient(timeout=self.settings.world_model_adapter_timeout_seconds) as client:
            for query in queries[:5]:
                response = await client.post(
                    self.settings.world_model_tavily_base_url,
                    headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"},
                    json={"api_key": self.settings.tavily_api_key, "query": query, "max_results": min(10, self.settings.world_model_adapter_max_items)},
                )
                response.raise_for_status()
                data = response.json()
                for result in data.get("results", [])[: self.settings.world_model_adapter_max_items]:
                    source_id = f"tavily:{query}:{result.get('url') or result.get('title')}"
                    if self._seen_recently(source_id):
                        duplicate_count += 1
                        continue
                    await service.observe_event(_tavily_event(query, result, self.settings))
                    count += 1
        return {"events": count, "prediction_signals": 0, "duplicates_skipped": duplicate_count}


class WorldModelAdapterService:
    def __init__(self, *, settings: Settings, world_model_service: WorldModelService):
        self.settings = settings
        self.world_model_service = world_model_service
        self.adapters: dict[str, WorldSourceAdapter] = {
            adapter.name: adapter
            for adapter in [
                PolymarketAdapter(settings),
                KalshiAdapter(settings),
                XRecentSearchAdapter(settings),
                TavilyAdapter(settings),
            ]
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.world_model_adapters_enabled,
            "adapters": [adapter.status_dict() for adapter in self.adapters.values()],
            "execution_authority": "none",
        }

    async def poll(self, name: str | None = None, *, force: bool = False) -> dict[str, Any]:
        if name:
            adapter = self.adapters.get(name)
            if adapter is None:
                return {"error": "unknown_adapter", "name": name}
            return await adapter.run_poll(self.world_model_service, force=force)
        results = []
        for adapter in self.adapters.values():
            results.append(await adapter.run_poll(self.world_model_service, force=force))
        return {"results": results}


def _polymarket_signals(market: dict[str, Any], settings: Settings) -> list[PredictionMarketSignal]:
    market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or _stable_id("pm_poly_market", market))
    question = str(market.get("question") or market.get("title") or market.get("slug") or "")
    outcomes = _json_list(market.get("outcomes")) or ["YES"]
    prices = _json_list(market.get("outcomePrices")) or _json_list(market.get("outcome_prices")) or []
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    ts = now_ms()
    signals = []
    for idx, outcome in enumerate(outcomes[:4]):
        probability = _safe_float(prices[idx]) if idx < len(prices) else None
        source_id = f"polymarket:{market_id}:{idx}:{outcome}"
        signals.append(
            PredictionMarketSignal(
                signal_id=_stable_id("pm_poly", market_id, outcome),
                venue="polymarket",
                market_id=market_id,
                question=question,
                outcome_id=str(idx),
                outcome_name=str(outcome),
                symbols=_symbols_from_text(question, settings),
                topics=["prediction_market", "polymarket", *_topics_from_text(question)],
                implied_probability=probability,
                liquidity_usd=liquidity,
                volume_usd=volume,
                status="open",
                as_of_ms=ts,
                confidence=0.7 if probability is not None else 0.45,
                metadata={
                    "source": "polymarket_public_api",
                    "source_id": source_id,
                    "adapter": "polymarket",
                    "raw_market": market,
                    "paper_only": True,
                    "execution_authority": "none",
                },
            )
        )
    return signals


def _kalshi_signal(market: dict[str, Any], settings: Settings) -> PredictionMarketSignal | None:
    ticker = str(market.get("ticker") or market.get("market_id") or "")
    if not ticker:
        return None
    title = str(market.get("title") or market.get("subtitle") or ticker)
    yes_bid = _cents_probability(market.get("yes_bid"))
    yes_ask = _cents_probability(market.get("yes_ask"))
    probability = _mid_probability(yes_bid, yes_ask)
    ts = now_ms()
    source_id = f"kalshi:{ticker}:yes"
    return PredictionMarketSignal(
        signal_id=_stable_id("pm_kalshi", ticker, "yes"),
        venue="kalshi",
        market_id=ticker,
        question=title,
        outcome_id="yes",
        outcome_name="YES",
        symbols=_symbols_from_text(title, settings),
        topics=["prediction_market", "kalshi", *_topics_from_text(title)],
        implied_probability=probability,
        best_bid=yes_bid,
        best_ask=yes_ask,
        liquidity_usd=_safe_float(market.get("liquidity")),
        volume_usd=_safe_float(market.get("volume")),
        status=str(market.get("status") or "open").lower(),
        as_of_ms=ts,
        confidence=0.7 if probability is not None else 0.45,
        metadata={
            "source": "kalshi_public_api",
            "source_id": source_id,
            "adapter": "kalshi",
            "raw_market": market,
            "paper_only": True,
            "execution_authority": "none",
        },
    )


def _with_probability_delta(service: WorldModelService, signal: PredictionMarketSignal) -> PredictionMarketSignal:
    previous = service.reducer.prediction_signals.get(signal.signal_id)
    if previous is None or previous.implied_probability is None or signal.implied_probability is None:
        return signal
    return signal.model_copy(update={"probability_delta": signal.implied_probability - previous.implied_probability})


def _x_event(tweet: dict[str, Any], settings: Settings) -> WorldEvent:
    ts = now_ms()
    text = str(tweet.get("text") or "")
    metrics = tweet.get("public_metrics") or {}
    importance = min(100.0, 20.0 + _safe_float(metrics.get("like_count"), 0.0) / 50.0 + _safe_float(metrics.get("retweet_count"), 0.0) / 25.0)
    return WorldEvent(
        event_id=_stable_id("wevt_x", tweet.get("id") or text),
        source_type="social",
        source="x_recent_search",
        provider="x",
        event_type="social_post",
        asset_class="mixed",
        symbols=_symbols_from_text(text, settings),
        topics=["social", "x", *_topics_from_text(text)],
        title=text[:180],
        body=text,
        url=f"https://x.com/i/web/status/{tweet.get('id')}" if tweet.get("id") else None,
        received_ts_ms=ts,
        computed_ts_ms=ts,
        importance_score=importance,
        sentiment="unknown",
        confidence=0.45,
        source_score=0.45,
        quality_score=0.45,
        payload={"tweet_id": tweet.get("id"), "author_id": tweet.get("author_id"), "public_metrics": metrics},
        metadata={"paper_only": True, "execution_authority": "none"},
    )


def _tavily_event(query: str, result: dict[str, Any], settings: Settings) -> WorldEvent:
    ts = now_ms()
    title = str(result.get("title") or result.get("url") or query)
    content = str(result.get("content") or result.get("raw_content") or "")
    score = _safe_float(result.get("score"), 0.5)
    return WorldEvent(
        event_id=_stable_id("wevt_tavily", query, result.get("url") or title),
        source_type="newswire",
        source="tavily",
        provider="tavily",
        event_type="search_enrichment",
        asset_class="mixed",
        symbols=_symbols_from_text(f"{title} {content}", settings),
        topics=["news", "macro", *_topics_from_text(f"{query} {title} {content}")],
        title=title,
        body=content,
        url=result.get("url"),
        received_ts_ms=ts,
        computed_ts_ms=ts,
        importance_score=min(100.0, max(10.0, score * 100.0)),
        sentiment="unknown",
        confidence=min(0.85, max(0.35, score)),
        source_score=min(0.85, max(0.35, score)),
        quality_score=min(0.9, max(0.35, score)),
        payload={"query": query, "published_date": result.get("published_date")},
        metadata={"paper_only": True, "execution_authority": "none"},
    )


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _symbols_from_text(text: str, settings: Settings) -> list[str]:
    lowered = text.lower()
    symbols = []
    for symbol in settings.autonomy_core_symbols:
        if symbol.lower() in lowered or f"${symbol.lower()}" in lowered:
            symbols.append(symbol)
    if "bitcoin" in lowered:
        symbols.append("BTC")
    if "ethereum" in lowered:
        symbols.append("ETH")
    if "hyperliquid" in lowered:
        symbols.append("HYPE")
    return sorted(set(symbols))


def _topics_from_text(text: str) -> list[str]:
    lowered = text.lower()
    return [term for term in ("fed", "fomc", "cpi", "rates", "inflation", "election", "crypto", "bitcoin", "ethereum", "hyperliquid") if term in lowered]


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _cents_probability(value: Any) -> float | None:
    raw = _safe_float(value)
    if raw is None:
        return None
    return max(0.0, min(1.0, raw / 100.0 if raw > 1 else raw))


def _mid_probability(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return max(0.0, min(1.0, (bid + ask) / 2.0))
    return bid if bid is not None else ask


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha1(":".join(json.dumps(part, sort_keys=True, default=str) for part in parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"
