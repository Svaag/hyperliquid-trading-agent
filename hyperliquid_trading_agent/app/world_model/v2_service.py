from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent.app.world_model.v2_reducer import (
    DEFAULT_EXPOSURE_PROFILES,
    SERIES_FACTORS,
    admit_evidence,
    canonical_hypothesis,
    compute_macro_states,
    conditional_prediction_impacts,
    current_asset_impacts,
    map_prediction_market,
    stable_id,
    stable_key,
)
from hyperliquid_trading_agent.app.world_model.v2_schemas import (
    AssetImpactV2,
    EvidenceV2,
    ForecastHypothesisV2,
    MacroFactorStateV2,
    MacroObservationV2,
    PredictionMarketV2,
    PredictionQuoteV2,
    SupervisionV2,
    WorldModelSnapshotV2,
)


def now_ms() -> int:
    return int(time.time() * 1000)


class WorldModelV2Service:
    """Persisted, shadow-only world model. No method has execution authority."""

    def __init__(self, *, settings: Any, repository: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.last_error: str | None = None
        configured = str(getattr(settings, "world_model_v2_instruments", "BTC,ETH,HYPE,SPY,QQQ,DXY,GOLD,OIL,TLT"))
        self.desired_instruments = {item.strip().upper() for item in configured.split(",") if item.strip()}
        self.exposure_profiles = dict(DEFAULT_EXPOSURE_PROFILES)
        try:
            custom_profiles = json.loads(str(getattr(settings, "world_model_v2_exposure_profiles_json", "{}")))
            for instrument, profile in custom_profiles.items():
                if isinstance(profile, dict) and all(int(value) in {-1, 0, 1} for value in profile.values()):
                    self.exposure_profiles[str(instrument).upper()] = {str(factor): int(value) for factor, value in profile.items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            self.last_error = "invalid_exposure_profiles_json"
        self.evidence: dict[str, EvidenceV2] = {}
        self.observations: dict[str, MacroObservationV2] = {}
        self.states: dict[str, MacroFactorStateV2] = {}
        self.markets: dict[str, PredictionMarketV2] = {}
        self.quotes: dict[str, PredictionQuoteV2] = {}
        self.quote_history: dict[str, list[PredictionQuoteV2]] = defaultdict(list)
        self.hypotheses: dict[str, ForecastHypothesisV2] = {}
        self.impacts: dict[str, AssetImpactV2] = {}
        self.supervision: dict[str, SupervisionV2] = {}
        self._cached_snapshot = WorldModelSnapshotV2(snapshot_id="wm2_empty", as_of_ms=now_ms(), quality_flags=["not_hydrated"])
        self._last_snapshot_persisted_ms = 0
        self.error_count = 0

    def status(self) -> dict[str, Any]:
        return {
            "version": 2, "enabled": True, "shadow_only": True, "execution_authority": "none",
            "repository_enabled": self.repository is not None, "repository_available": self.repository is not None,
            "evidence_count": len(self.evidence), "macro_observation_count": len(self.observations),
            "macro_factor_count": len(self.states), "prediction_market_count": len(self.markets),
            "forecast_count": len(self.hypotheses), "impact_count": len(self.impacts),
            "last_error": self.last_error, "error_count": self.error_count,
            "snapshot_as_of_ms": self._cached_snapshot.as_of_ms,
        }

    async def hydrate(self) -> None:
        if self.repository is None:
            return
        try:
            latest = await self.repository.latest_world_model_v2_snapshot()
            if latest:
                self._cached_snapshot = WorldModelSnapshotV2.model_validate(latest)
                self.states = {item.factor_id: item for item in self._cached_snapshot.macro_states}
                self.hypotheses = {item.hypothesis_id: item for item in self._cached_snapshot.forecasts}
                self.impacts = {item.impact_id: item for item in self._cached_snapshot.asset_impacts}
                self.evidence = {item.evidence_id: item for item in self._cached_snapshot.evidence}
            for payload in await self.repository.list_world_model_v2_macro_observations(limit=5000):
                item = MacroObservationV2.model_validate(payload)
                self.observations[item.observation_id] = item
            for payload in await self.repository.list_world_model_v2_prediction_markets(limit=500, admission_status=None):
                item = PredictionMarketV2.model_validate(payload)
                self.markets[item.market_key] = item
            for payload in await self.repository.list_world_model_v2_prediction_quotes(limit=1000):
                item = PredictionQuoteV2.model_validate(payload)
                self.quotes[item.quote_key] = item
            await self.refresh_cache(persist=False)
        except Exception as exc:
            self._error(exc)

    async def refresh_read_cache(self) -> bool:
        if self.repository is None:
            return False
        try:
            latest = await self.repository.latest_world_model_v2_snapshot()
            if not latest or int(latest.get("as_of_ms") or 0) <= self._cached_snapshot.as_of_ms:
                return False
            snapshot = WorldModelSnapshotV2.model_validate(latest)
            self._cached_snapshot = snapshot
            self.states = {item.factor_id: item for item in snapshot.macro_states}
            self.hypotheses = {item.hypothesis_id: item for item in snapshot.forecasts}
            self.impacts = {item.impact_id: item for item in snapshot.asset_impacts}
            self.evidence = {item.evidence_id: item for item in snapshot.evidence}
            return True
        except Exception as exc:
            self._error(exc)
            return False

    async def observe_newswire_event(self, event: Any) -> list[Any]:
        ts = now_ms()
        topics = list(getattr(event, "topics", []) or [])
        symbols = [str(item).upper() for item in (getattr(event, "symbols", []) or [])]
        event_type = str(getattr(event, "event_type", "headline"))
        provider = str(getattr(event, "provider", "unknown"))
        factors = _factors_from_text(" ".join([str(getattr(event, "headline", "")), str(getattr(event, "body", "")), *topics]))
        evidence = EvidenceV2(
            evidence_id=stable_key("newswire", str(getattr(event, "event_id", stable_id("evidence", str(ts))))),
            source_type="macro_release" if event_type == "macro" else "newswire",
            source=str(getattr(event, "source", "unknown")), provider=provider,
            title=str(getattr(event, "headline", "")), body=str(getattr(event, "body", "")),
            url=getattr(event, "url", None), event_at_ms=getattr(event, "published_at_ms", None),
            available_at_ms=int(getattr(event, "received_at_ms", ts)), observed_at_ms=max(ts, int(getattr(event, "received_at_ms", ts))),
            admission_status="quarantined", factor_ids=factors, instrument_ids=symbols,
            quality_score=float(getattr(event, "confidence", 0.5)),
            payload={"event_type": event_type, "importance_score": getattr(event, "importance_score", 0)},
            metadata=dict(getattr(event, "metadata", {}) or {}),
        )
        evidence = admit_evidence(evidence, desired_instruments=self.desired_instruments)
        await self.observe_evidence(evidence)
        macro = _macro_observation_from_event(event, evidence)
        if macro is not None:
            await self.observe_macro_observation(macro)
        return []

    async def observe_evidence(self, evidence: EvidenceV2) -> None:
        self.evidence[evidence.evidence_id] = evidence
        if self.repository is not None:
            await self.repository.upsert_world_model_v2_evidence(evidence.model_dump(mode="json"))
        await self.refresh_cache()

    async def observe_macro_observation(self, observation: MacroObservationV2) -> None:
        await self.observe_macro_observations([observation])

    async def observe_macro_observations(self, observations: list[MacroObservationV2]) -> None:
        for observation in observations:
            self.observations[observation.observation_id] = observation
        if self.repository is not None:
            bulk = getattr(self.repository, "upsert_world_model_v2_macro_observations", None)
            payloads = [item.model_dump(mode="json") for item in observations]
            if callable(bulk):
                await bulk(payloads)
            else:
                for payload in payloads:
                    await self.repository.upsert_world_model_v2_macro_observation(payload)
        await self.refresh_cache()

    async def observe_prediction_market_signal(self, signal: Any) -> ForecastHypothesisV2 | None:
        # Keep the broad legacy catalog fresh for the independent paper/manual-search
        # subsystem. Only the deterministically admitted subset enters v2 state.
        persist_catalog = getattr(self.repository, "upsert_prediction_market_signal", None)
        if callable(persist_catalog) and callable(getattr(signal, "model_dump", None)):
            await persist_catalog(signal.model_dump(mode="json"))
        ts = int(getattr(signal, "as_of_ms", now_ms()))
        venue = str(getattr(signal, "venue", "unknown"))
        market_id = str(getattr(signal, "market_id", ""))
        outcome_id = str(getattr(signal, "outcome_id", "") or getattr(signal, "outcome_name", ""))
        market = map_prediction_market(
            venue=venue, market_id=market_id, question=str(getattr(signal, "question", "")),
            status=str(getattr(signal, "status", "open")), liquidity_usd=getattr(signal, "liquidity_usd", None),
            volume_usd=getattr(signal, "volume_usd", None), outcome_ids=[outcome_id], desired_instruments=self.desired_instruments,
        )
        if market.market_key not in self.markets and market.admission_status == "admitted":
            admitted_count = sum(item.admission_status == "admitted" for item in self.markets.values())
            if admitted_count >= int(self.settings.world_model_v2_prediction_max_markets):
                market = market.model_copy(update={"admission_status": "quarantined", "admission_reason_codes": sorted(set([*market.admission_reason_codes, "feature_market_cap_reached"]))})
        previous = self.markets.get(market.market_key)
        if previous:
            market = market.model_copy(update={"outcome_ids": sorted(set([*previous.outcome_ids, outcome_id]))})
        self.markets[market.market_key] = market
        probability = getattr(signal, "implied_probability", None)
        if probability is None:
            market = market.model_copy(update={"admission_status": "quarantined", "admission_reason_codes": sorted(set([*market.admission_reason_codes, "missing_probability"]))})
            self.markets[market.market_key] = market
            if self.repository is not None:
                await self.repository.upsert_world_model_v2_prediction_market(market.model_dump(mode="json"))
            return None
        bid, ask = getattr(signal, "best_bid", None), getattr(signal, "best_ask", None)
        quote = PredictionQuoteV2(
            quote_key=stable_key(venue, market_id, outcome_id), market_key=market.market_key,
            venue=venue.lower(), market_id=market_id, outcome_id=outcome_id,
            outcome_name=str(getattr(signal, "outcome_name", "") or outcome_id), probability=float(probability),
            best_bid=bid, best_ask=ask, spread=(float(ask) - float(bid)) if bid is not None and ask is not None else None,
            provider_at_ms=ts, observed_at_ms=now_ms(),
        )
        if bid is None or ask is None or quote.spread is None or quote.spread > 0.10:
            reason = "incomplete_quote" if bid is None or ask is None else "spread_above_10pct"
            market = market.model_copy(update={"admission_status": "quarantined", "admission_reason_codes": sorted(set([*market.admission_reason_codes, reason]))})
            self.markets[market.market_key] = market
        old = self.quotes.get(quote.quote_key)
        history = old is None or abs(old.probability - quote.probability) >= 0.001 or quote.observed_at_ms - old.observed_at_ms >= 300_000
        prior = self.quote_history[quote.quote_key]
        if old is not None and (not prior or prior[-1].observed_at_ms != old.observed_at_ms):
            prior.append(old)
        quote = quote.model_copy(update={
            "delta_5m": _quote_delta(prior, quote, 300_000),
            "delta_1h": _quote_delta(prior, quote, 3_600_000),
            "delta_24h": _quote_delta(prior, quote, 86_400_000),
        })
        if history:
            prior.append(quote)
            cutoff = quote.observed_at_ms - 86_400_000 * 2
            self.quote_history[quote.quote_key] = [item for item in prior if item.observed_at_ms >= cutoff]
        self.quotes[quote.quote_key] = quote
        if self.repository is not None:
            await self.repository.upsert_world_model_v2_prediction_market(market.model_dump(mode="json"))
            await self.repository.upsert_world_model_v2_prediction_quote(quote.model_dump(mode="json"), write_history=history)
        if market.status in {"closed", "settled"}:
            self.hypotheses.pop(stable_key("forecast", market.market_key), None)
            await self.refresh_cache()
            await self.persist_snapshot(force=True)
            return None
        market_quotes = [item for item in self.quotes.values() if item.market_key == market.market_key]
        hypothesis = canonical_hypothesis(market, market_quotes, now_ms=now_ms())
        if hypothesis is not None:
            self.hypotheses[hypothesis.hypothesis_id] = hypothesis
            if self.repository is not None:
                await self.repository.upsert_world_model_v2_hypothesis(hypothesis.model_dump(mode="json"))
        await self.refresh_cache()
        return hypothesis

    async def refresh_cache(self, *, persist: bool = True, as_of_ms: int | None = None) -> WorldModelSnapshotV2:
        ts = as_of_ms or now_ms()
        states = compute_macro_states(self.observations.values(), as_of_ms=ts)
        self.states = {item.factor_id: item for item in states}
        current = current_asset_impacts(states, as_of_ms=ts, profiles=self.exposure_profiles, desired_instruments=self.desired_instruments)
        conditional = [impact for forecast in self.hypotheses.values() for impact in conditional_prediction_impacts(forecast, profiles=self.exposure_profiles)]
        self.impacts = {item.impact_id: item for item in [*current, *conditional]}
        flags: list[str] = []
        if not any(item.coverage for item in states):
            flags.append("macro_baseline_unavailable")
        if not self.hypotheses:
            flags.append("no_feature_eligible_forecasts")
        snapshot = WorldModelSnapshotV2(
            snapshot_id=f"wm2_{ts // 300_000}", as_of_ms=ts, macro_states=states,
            asset_impacts=sorted(self.impacts.values(), key=lambda item: (item.instrument_id, item.factor_id, item.horizon)),
            forecasts=sorted(self.hypotheses.values(), key=lambda item: item.as_of_ms, reverse=True)[:100],
            evidence=sorted((item for item in self.evidence.values() if item.admission_status == "admitted"), key=lambda item: item.available_at_ms, reverse=True)[:100],
            quality_flags=flags, coverage={item.factor_id: item.coverage for item in states},
        )
        self._cached_snapshot = snapshot
        if persist and self.repository is not None:
            for state in states:
                await self.repository.upsert_world_model_v2_macro_state(state.model_dump(mode="json"))
            for impact in self.impacts.values():
                await self.repository.upsert_world_model_v2_asset_impact(impact.model_dump(mode="json"))
            if ts - self._last_snapshot_persisted_ms >= 300_000:
                await self.repository.upsert_world_model_v2_snapshot(snapshot.model_dump(mode="json"))
                self._last_snapshot_persisted_ms = ts
        return snapshot

    def snapshot(self, *, symbols: list[str] | None = None, topics: list[str] | None = None, max_beliefs: int = 20, as_of_ms: int | None = None) -> WorldModelSnapshotV2:
        snapshot = self._cached_snapshot
        if symbols:
            wanted = {item.upper() for item in symbols}
            return snapshot.model_copy(update={
                "asset_impacts": [item for item in snapshot.asset_impacts if item.instrument_id in wanted],
                "forecasts": [item for item in snapshot.forecasts if set(item.instrument_ids) & wanted],
                "evidence": [item for item in snapshot.evidence if set(item.instrument_ids) & wanted or item.factor_ids],
            })
        return snapshot

    def wiki_block(self, *, symbols: list[str] | None = None, topics: list[str] | None = None, max_chars: int = 2_000) -> str:
        snapshot = self.snapshot(symbols=symbols)
        lines = ["World Model v2 (shadow-only; probabilities are forecasts, not trade directions)"]
        lines.extend(f"- {state.factor_id}: {state.regime} (coverage {state.coverage:.0%})" for state in snapshot.macro_states if state.coverage)
        lines.extend(f"- {impact.instrument_id} / {impact.horizon}: {impact.direction} via {impact.factor_id}" for impact in snapshot.asset_impacts[:12] if impact.direction != "unknown")
        lines.extend(f"- Forecast: {forecast.yes_probability:.1%} Yes — {forecast.question}" for forecast in snapshot.forecasts[:6] if forecast.yes_probability is not None)
        return "\n".join(lines)[:max_chars]

    async def list_evidence(self, *, limit: int = 100, admission_status: str | None = None, **_: Any) -> list[dict[str, Any]]:
        if self.repository is not None:
            return await self.repository.list_world_model_v2_evidence(limit=limit, admission_status=admission_status)
        items = self.evidence.values()
        if admission_status:
            items = [item for item in items if item.admission_status == admission_status]
        return [item.model_dump(mode="json") for item in list(items)[:limit]]

    async def list_macro_states(self, *, limit: int = 100, **_: Any) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in list(self.states.values())[:limit]]

    async def list_asset_impacts(self, *, limit: int = 200, instrument_id: str | None = None, **_: Any) -> list[dict[str, Any]]:
        items = self.impacts.values()
        if instrument_id:
            items = [item for item in items if item.instrument_id == instrument_id.upper()]
        return [item.model_dump(mode="json") for item in list(items)[:limit]]

    async def list_forecasts(self, *, limit: int = 100, **_: Any) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in sorted(self.hypotheses.values(), key=lambda item: item.as_of_ms, reverse=True)[:limit]]

    async def list_prediction_markets(self, *, limit: int = 100, admission_status: str | None = "admitted", venue: str | None = None, **_: Any) -> list[dict[str, Any]]:
        if self.repository is not None:
            return await self.repository.list_world_model_v2_prediction_markets(limit=limit, admission_status=admission_status, venue=venue)
        return [item.model_dump(mode="json") for item in list(self.markets.values())[:limit] if (not admission_status or item.admission_status == admission_status)]

    async def list_snapshots(self, *, limit: int = 100, start_ms: int | None = None, end_ms: int | None = None, **_: Any) -> list[dict[str, Any]]:
        if self.repository is not None:
            return await self.repository.list_world_model_v2_snapshots(limit=limit, start_ms=start_ms, end_ms=end_ms)
        return [self._cached_snapshot.model_dump(mode="json")]

    async def nearest_snapshot(self, *, as_of_ms: int, **_: Any) -> dict[str, Any] | None:
        if self.repository is not None:
            return await self.repository.latest_world_model_v2_snapshot(as_of_ms=as_of_ms)
        return self._cached_snapshot.model_dump(mode="json") if self._cached_snapshot.as_of_ms <= as_of_ms else None

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        if self.repository is not None:
            return await self.repository.get_world_model_v2_snapshot(snapshot_id)
        return self._cached_snapshot.model_dump(mode="json") if self._cached_snapshot.snapshot_id == snapshot_id else None

    async def replay(self, *, start_ms: int, end_ms: int, limit: int = 200, **_: Any) -> dict[str, Any]:
        snapshots = await self.list_snapshots(limit=limit, start_ms=start_ms, end_ms=end_ms)
        return {"version": 2, "snapshots": snapshots, "count": len(snapshots), "historical_v1_replay": False}

    async def persist_snapshot(self, snapshot: WorldModelSnapshotV2 | None = None, *, force: bool = False) -> None:
        item = snapshot or self._cached_snapshot
        if self.repository is not None and (force or item.as_of_ms - self._last_snapshot_persisted_ms >= 300_000):
            await self.repository.upsert_world_model_v2_snapshot(item.model_dump(mode="json"))
            self._last_snapshot_persisted_ms = item.as_of_ms

    async def annotate(self, *, target_type: str, target_id: str, action: str, note: str = "", actor_id: str | None = None, metadata: dict[str, Any] | None = None, **_: Any) -> SupervisionV2:
        ts = now_ms()
        item = SupervisionV2(
            supervision_id=stable_id("wm2s", target_type, target_id, action, str(ts)), target_type=target_type,
            target_id=target_id, action=action, note=note, actor_id=actor_id, created_at_ms=ts,
            metadata={"shadow_only": True, "execution_authority": "none", **(metadata or {})},
        )
        self.supervision[item.supervision_id] = item
        if self.repository is not None:
            await self.repository.upsert_world_model_v2_supervision(item.model_dump(mode="json"))
        return item

    async def list_annotations(self, *, limit: int = 100, target_type: str | None = None, target_id: str | None = None, **_: Any) -> list[dict[str, Any]]:
        if self.repository is not None:
            return await self.repository.list_world_model_v2_supervision(limit=limit, target_type=target_type, target_id=target_id)
        return [item.model_dump(mode="json") for item in list(self.supervision.values())[:limit]]

    async def record_outcome(self, *, target_type: str, target_id: str, outcome: str, metadata: dict[str, Any] | None = None, **_: Any) -> SupervisionV2:
        return await self.annotate(target_type=target_type, target_id=target_id, action=f"outcome:{outcome}", metadata=metadata)

    async def list_outcomes(self, *, limit: int = 100, **_: Any) -> list[dict[str, Any]]:
        items = await self.list_annotations(limit=limit)
        return [item for item in items if str(item.get("action", "")).startswith("outcome:")]

    async def list_calibrations(self, **_: Any) -> list[dict[str, Any]]:
        return []

    async def repository_health(self) -> dict[str, Any]:
        return {"ping": {"ok": self.repository is not None}, "version": 2}

    def _error(self, exc: Exception) -> None:
        self.error_count += 1
        self.last_error = type(exc).__name__


def _factors_from_text(text: str) -> list[str]:
    value = text.lower()
    mapping = {
        "inflation": ("inflation", "cpi", "pce"), "labor": ("payroll", "jobs", "unemployment", "labor"),
        "growth": ("gdp", "growth", "retail sales", "industrial production", "recession"),
        "policy_stance": ("fomc", "federal reserve", "fed rate", "rate cut", "rate hike"),
        "rates": ("treasury yield", "bond yield"), "usd": ("dollar index", "dxy"),
        "liquidity": ("liquidity", "balance sheet", "reverse repo"),
        "financial_conditions": ("credit spread", "financial conditions"),
    }
    return [factor for factor, terms in mapping.items() if any(term in value for term in terms)]


def _macro_observation_from_event(event: Any, evidence: EvidenceV2) -> MacroObservationV2 | None:
    if str(getattr(event, "event_type", "")) != "macro":
        return None
    if evidence.provider.lower() not in {"bls", "fred", "us_treasury", "bea", "federal_reserve"}:
        return None
    metadata = dict(getattr(event, "metadata", {}) or {})
    raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else metadata
    series_id = str(raw.get("series_id") or raw.get("ticker") or raw.get("category") or "").strip().upper().replace(" ", "_")
    factor = SERIES_FACTORS.get(series_id) or (evidence.factor_ids[0] if evidence.factor_ids else None)
    actual = _number(raw.get("actual"))
    if not series_id or factor is None or actual is None:
        return None
    forecast = _number(raw.get("forecast") or raw.get("teforecast"))
    previous = _number(raw.get("previous"))
    available = evidence.available_at_ms
    period = str(raw.get("reference") or raw.get("period") or available)
    return MacroObservationV2(
        observation_id=stable_id("macro", series_id, period, str(available)), series_id=series_id, factor_id=factor,
        period=period, value=actual, units=str(raw.get("unit") or "index"), frequency=str(raw.get("frequency") or "event"),
        vintage=str(raw.get("vintage") or available), event_at_ms=evidence.event_at_ms or available, available_at_ms=available,
        previous_value=previous, forecast_value=forecast, surprise=(actual - forecast) if forecast is not None else None,
        source=evidence.provider, evidence_id=evidence.evidence_id,
    )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _quote_delta(history: list[PredictionQuoteV2], quote: PredictionQuoteV2, horizon_ms: int) -> float | None:
    target = quote.observed_at_ms - horizon_ms
    candidates = [item for item in history if item.observed_at_ms <= target]
    if not candidates:
        return None
    baseline = max(candidates, key=lambda item: item.observed_at_ms)
    return max(-1.0, min(1.0, quote.probability - baseline.probability))
