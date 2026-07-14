from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import defaultdict
from typing import Iterable

from hyperliquid_trading_agent.app.world_model.v2_schemas import (
    AssetImpactV2,
    EvidenceV2,
    ForecastHypothesisV2,
    MacroFactorStateV2,
    MacroObservationV2,
    PredictionMarketV2,
    PredictionQuoteV2,
)

MAPPING_VERSION = "macro-cross-asset-v1"
FACTOR_AXES = {
    "inflation": "inflation pressure",
    "labor": "labor-market tightness",
    "growth": "real-economy growth",
    "policy_stance": "monetary-policy restrictiveness",
    "rates": "nominal rates",
    "real_rates": "real rates",
    "usd": "US dollar strength",
    "liquidity": "system liquidity",
    "financial_conditions": "financial-condition tightness",
}

# Positive means a higher/tighter factor is supportive; negative means adverse.
DEFAULT_EXPOSURE_PROFILES: dict[str, dict[str, int]] = {
    "BTC": {"growth": 1, "liquidity": 1, "financial_conditions": -1, "policy_stance": -1, "real_rates": -1, "usd": -1},
    "ETH": {"growth": 1, "liquidity": 1, "financial_conditions": -1, "policy_stance": -1, "real_rates": -1, "usd": -1},
    "HYPE": {"growth": 1, "liquidity": 1, "financial_conditions": -1, "policy_stance": -1, "real_rates": -1, "usd": -1},
    "SPY": {"growth": 1, "inflation": -1, "policy_stance": -1, "rates": -1, "financial_conditions": -1},
    "QQQ": {"growth": 1, "inflation": -1, "policy_stance": -1, "real_rates": -1, "financial_conditions": -1},
    "DXY": {"growth": 1, "policy_stance": 1, "rates": 1, "financial_conditions": 1},
    "GOLD": {"inflation": 1, "real_rates": -1, "usd": -1, "financial_conditions": 1},
    "OIL": {"growth": 1, "inflation": 1, "usd": -1},
    "TLT": {"inflation": -1, "policy_stance": -1, "rates": -1, "growth": -1},
}

SERIES_FACTORS = {
    "CUSR0000SA0": "inflation", "CUSR0000SA0L1E": "inflation",
    "CES0000000001": "labor", "LNS14000000": "labor",
    "PCEPI": "inflation", "PCEPILFE": "inflation", "GDPC1": "growth",
    "INDPRO": "growth", "RSAFS": "growth", "ICSA": "labor",
    "FEDFUNDS": "policy_stance", "DFF": "policy_stance", "DTWEXBGS": "usd",
    "WALCL": "liquidity", "RRPONTSYD": "liquidity", "WTREGEN": "liquidity",
    "NFCI": "financial_conditions",
}
EXPECTED_FACTOR_SERIES = {"inflation": 4, "labor": 3, "growth": 3, "policy_stance": 2, "rates": 4, "real_rates": 3, "usd": 1, "liquidity": 3, "financial_conditions": 1}
SERIES_POLARITY = {"LNS14000000": -1, "ICSA": -1, "RRPONTSYD": -1, "WTREGEN": -1}

PREDICTION_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (("fed", "fomc", "interest rate", "rate cut", "rate hike"), ("policy_stance", "rates"), ("BTC", "ETH", "SPY", "QQQ", "TLT", "DXY")),
    (("inflation", "cpi", "pce"), ("inflation", "policy_stance"), ("BTC", "ETH", "SPY", "QQQ", "TLT", "DXY", "GOLD")),
    (("recession", "gdp", "unemployment", "payroll"), ("growth", "labor"), ("BTC", "ETH", "SPY", "QQQ", "TLT", "OIL")),
    (("bitcoin", "btc", "ethereum", "eth", "crypto"), ("liquidity",), ("BTC", "ETH", "HYPE")),
    (("taiwan", "china invasion", "strait"), ("financial_conditions", "growth"), ("SPY", "QQQ", "BTC", "OIL", "GOLD")),
    (("oil", "opec", "crude"), ("inflation", "growth"), ("OIL", "SPY", "TLT")),
)

EXCLUDED_PREDICTION_TERMS = (
    "world cup", "nba", "nfl", "mlb", "champions league", "gta vi", "album", "movie",
    "celebrity", "sentenced", "prison", "jesus christ", "oscars", "grammy",
)


def stable_key(*parts: str) -> str:
    return ":".join(str(part).strip().lower() for part in parts)


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def admit_evidence(evidence: EvidenceV2, *, desired_instruments: set[str]) -> EvidenceV2:
    official_macro = evidence.source_type == "macro_release" and evidence.provider.lower() in {"bls", "fred", "us_treasury", "bea", "federal_reserve"}
    routed_macro = bool(set(evidence.factor_ids) & set(FACTOR_AXES))
    mapped_asset = bool(set(evidence.instrument_ids) & {item.upper() for item in desired_instruments})
    if official_macro or routed_macro or mapped_asset:
        return evidence.model_copy(update={"admission_status": "admitted", "admission_reason_codes": ["official_macro" if official_macro else "deterministic_mapping"]})
    suggested = bool(evidence.metadata.get("llm_suggested_mapping"))
    return evidence.model_copy(update={
        "admission_status": "quarantined" if suggested else "rejected",
        "admission_reason_codes": ["llm_mapping_requires_review" if suggested else "no_relevant_factor_or_instrument"],
    })


def map_prediction_market(
    *, venue: str, market_id: str, question: str, status: str = "open", accepting_orders: bool = True,
    closes_at_ms: int | None = None, liquidity_usd: float | None = None, volume_usd: float | None = None,
    outcome_ids: list[str] | None = None, desired_instruments: set[str] | None = None,
) -> PredictionMarketV2:
    text = question.lower()
    factors: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    reasons: list[str] = []
    if any(_prediction_term_matches(text, term) for term in EXCLUDED_PREDICTION_TERMS):
        admission = "rejected"
        reasons.append("excluded_non_market_topic")
    else:
        for terms, mapped_factors, mapped_instruments in PREDICTION_RULES:
            if any(_prediction_term_matches(text, term) for term in terms):
                factors = tuple(sorted(set(factors) | set(mapped_factors)))
                instruments = tuple(sorted(set(instruments) | set(mapped_instruments)))
        if desired_instruments is not None:
            instruments = tuple(item for item in instruments if item in desired_instruments)
        admission = "admitted" if factors and instruments else "quarantined"
        reasons.append("deterministic_macro_asset_mapping" if admission == "admitted" else "unmapped_prediction_topic")
    if status != "open" or not accepting_orders:
        admission = "quarantined"
        reasons.append("market_not_tradeable")
    if liquidity_usd is not None and liquidity_usd < 25_000:
        admission = "quarantined"
        reasons.append("liquidity_below_25000")
    scenario_shocks = _prediction_scenario_shocks(text)
    return PredictionMarketV2(
        market_key=stable_key(venue, market_id), venue=venue.lower(), market_id=market_id, question=question,
        status=status if status in {"open", "closed", "settled", "stale"} else "stale", accepting_orders=accepting_orders,
        closes_at_ms=closes_at_ms, liquidity_usd=liquidity_usd, volume_usd=volume_usd,
        factor_ids=list(factors), instrument_ids=list(instruments), admission_status=admission,
        admission_reason_codes=sorted(set(reasons)), outcome_ids=outcome_ids or [], metadata={"yes_scenario_factor_shocks": scenario_shocks},
    )


def quote_is_feature_eligible(market: PredictionMarketV2, quote: PredictionQuoteV2, *, now_ms: int) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    age_ms = max(0, now_ms - quote.provider_at_ms)
    if market.admission_status != "admitted":
        reasons.append("market_not_admitted")
    if market.status != "open" or not market.accepting_orders:
        reasons.append("market_not_tradeable")
    if age_ms > 120_000:
        reasons.append("quote_older_than_120s")
    if quote.best_bid is None or quote.best_ask is None:
        reasons.append("incomplete_quote")
    if quote.spread is None or quote.spread > 0.10:
        reasons.append("spread_above_10pct")
    if market.venue == "polymarket" and (market.liquidity_usd or 0) < 25_000:
        reasons.append("liquidity_below_25000")
    return not reasons, reasons


def canonical_hypothesis(market: PredictionMarketV2, quotes: list[PredictionQuoteV2], *, now_ms: int) -> ForecastHypothesisV2 | None:
    if len(quotes) < 2:
        return None
    eligibility = [quote_is_feature_eligible(market, quote, now_ms=now_ms) for quote in quotes]
    if not all(eligible for eligible, _ in eligibility):
        return None
    quality_reasons = sorted({reason for _, reasons in eligibility for reason in reasons})
    by_name = {quote.outcome_name.strip().lower(): quote for quote in quotes}
    if set(by_name) == {"yes", "no"}:
        yes = by_name["yes"]
        no = by_name["no"]
        total = yes.probability + no.probability
        if total <= 0:
            return None
        yes_probability = yes.probability / total
        return ForecastHypothesisV2(
            hypothesis_id=stable_key("forecast", market.market_key), market_key=market.market_key,
            question=market.question, as_of_ms=max(yes.provider_at_ms, no.provider_at_ms), yes_probability=yes_probability,
            factor_ids=market.factor_ids, instrument_ids=market.instrument_ids,
            confidence=max(0.0, min(1.0, 1.0 - float(yes.spread or 0.0))),
            evidence_ids=[], metadata={"quote_keys": [yes.quote_key, no.quote_key], "quality_reasons": quality_reasons, "yes_scenario_factor_shocks": market.metadata.get("yes_scenario_factor_shocks", {})},
        )
    total = sum(quote.probability for quote in quotes)
    if total <= 0:
        return None
    return ForecastHypothesisV2(
        hypothesis_id=stable_key("forecast", market.market_key), market_key=market.market_key, question=market.question,
        as_of_ms=max(quote.provider_at_ms for quote in quotes),
        outcome_probabilities={quote.outcome_name: quote.probability / total for quote in quotes},
        factor_ids=market.factor_ids, instrument_ids=market.instrument_ids,
        confidence=max(0.0, 1.0 - max(float(quote.spread or 0.0) for quote in quotes)),
        metadata={"quote_keys": [quote.quote_key for quote in quotes], "quality_reasons": quality_reasons, "yes_scenario_factor_shocks": market.metadata.get("yes_scenario_factor_shocks", {})},
    )


def compute_macro_states(observations: Iterable[MacroObservationV2], *, as_of_ms: int) -> list[MacroFactorStateV2]:
    grouped: dict[str, list[MacroObservationV2]] = defaultdict(list)
    for observation in observations:
        if observation.available_at_ms <= as_of_ms:
            grouped[observation.factor_id].append(observation)
    states: list[MacroFactorStateV2] = []
    for factor_id in FACTOR_AXES:
        items = sorted(grouped.get(factor_id, []), key=lambda item: (item.available_at_ms, item.period))
        if not items:
            states.append(MacroFactorStateV2(factor_id=factor_id, semantic_axis=FACTOR_AXES[factor_id], as_of_ms=as_of_ms, quality_flags=["no_official_observations"]))
            continue
        latest_by_series: dict[str, MacroObservationV2] = {}
        for item in items:
            latest_by_series[item.series_id] = item
        level_values: list[float] = []
        momentum_values: list[float] = []
        for series_id, latest in latest_by_series.items():
            series = [item for item in items if item.series_id == series_id]
            values = [item.value for item in series]
            polarity = SERIES_POLARITY.get(series_id, 1)
            level_values.append(_robust_z(values, values[-1]) * polarity)
            if len(values) >= 2:
                changes = [values[index] - values[index - 1] for index in range(1, len(values))]
                momentum_values.append(_robust_z(changes, changes[-1]) * polarity)
        normalized_surprises = [value for series_id, latest in latest_by_series.items() if (value := _normalized_surprise([item for item in items if item.series_id == series_id], latest)) is not None]
        level = _bounded_mean(level_values)
        momentum = _bounded_mean(momentum_values)
        surprise = _bounded_mean(normalized_surprises)
        composite = next((value for value in (momentum, level, surprise) if value is not None), 0.0)
        regime = "high/rising" if composite > 0.75 else "low/falling" if composite < -0.75 else "balanced"
        newest = max(item.available_at_ms for item in items)
        expected = EXPECTED_FACTOR_SERIES.get(factor_id, max(1, len({item.series_id for item in items})))
        coverage = min(1.0, len(latest_by_series) / expected)
        states.append(MacroFactorStateV2(
            factor_id=factor_id, semantic_axis=FACTOR_AXES[factor_id], as_of_ms=as_of_ms,
            level_score=level, momentum_score=momentum, surprise_score=surprise, regime=regime,
            freshness_ms=max(0, as_of_ms - newest), coverage=coverage,
            source_observation_ids=[item.observation_id for item in latest_by_series.values()],
        ))
    return states


def current_asset_impacts(
    states: Iterable[MacroFactorStateV2], *, as_of_ms: int,
    profiles: dict[str, dict[str, int]] | None = None, desired_instruments: set[str] | None = None,
) -> list[AssetImpactV2]:
    profiles = profiles or DEFAULT_EXPOSURE_PROFILES
    impacts: list[AssetImpactV2] = []
    for instrument, exposure in profiles.items():
        if desired_instruments is not None and instrument not in desired_instruments:
            continue
        for state in states:
            sign = exposure.get(state.factor_id)
            if not sign:
                continue
            by_horizon = {"intraday": state.surprise_score, "swing": state.momentum_score, "regime": state.level_score}
            for horizon, score in by_horizon.items():
                if score is None or abs(score) < 0.15:
                    direction, strength = "unknown", 0.0
                else:
                    direction = "supportive" if score * sign > 0 else "adverse"
                    strength = min(1.0, abs(score) / 3.0) * state.coverage
                impacts.append(AssetImpactV2(
                    impact_id=stable_key("impact", instrument, state.factor_id, horizon), instrument_id=instrument,
                    factor_id=state.factor_id, horizon=horizon, direction=direction, strength=strength,
                    as_of_ms=as_of_ms, rationale=f"{state.semantic_axis}: {state.regime}",
                    source_ids=state.source_observation_ids, mapping_version=MAPPING_VERSION,
                ))
    return impacts


def conditional_prediction_impacts(hypothesis: ForecastHypothesisV2, *, profiles: dict[str, dict[str, int]] | None = None) -> list[AssetImpactV2]:
    profiles = profiles or DEFAULT_EXPOSURE_PROFILES
    impacts: list[AssetImpactV2] = []
    for instrument in hypothesis.instrument_ids:
        for factor in hypothesis.factor_ids:
            sign = profiles.get(instrument, {}).get(factor)
            shock = int((hypothesis.metadata.get("yes_scenario_factor_shocks") or {}).get(factor) or 0)
            effect = (sign or 0) * shock
            direction = "supportive" if effect > 0 else "adverse" if effect < 0 else "unknown"
            impacts.append(AssetImpactV2(
                impact_id=stable_key("conditional", hypothesis.hypothesis_id, instrument, factor), instrument_id=instrument,
                factor_id=factor, horizon="swing", direction=direction, mode="conditional", strength=hypothesis.confidence,
                as_of_ms=hypothesis.as_of_ms, condition=f"If Yes: {hypothesis.question}",
                rationale="Deterministic exposure mapping; probability is not treated as direction.",
                source_ids=[hypothesis.hypothesis_id], mapping_version=MAPPING_VERSION,
            ))
    return impacts


def _robust_z(values: list[float], value: float) -> float:
    if len(values) < 5:
        return 0.0
    median = statistics.median(values)
    deviations = [abs(item - median) for item in values]
    mad = statistics.median(deviations)
    if math.isclose(mad, 0.0):
        std = statistics.pstdev(values)
        return 0.0 if math.isclose(std, 0.0) else max(-5.0, min(5.0, (value - median) / std))
    return max(-5.0, min(5.0, 0.6745 * (value - median) / mad))


def _bounded_mean(values: list[float]) -> float | None:
    return max(-5.0, min(5.0, statistics.fmean(values))) if values else None


def _normalized_surprise(series: list[MacroObservationV2], latest: MacroObservationV2) -> float | None:
    if latest.surprise is None:
        return None
    polarity = SERIES_POLARITY.get(latest.series_id, 1)
    surprises = [float(item.surprise) for item in series if item.surprise is not None]
    if len(surprises) >= 5:
        return _robust_z(surprises, float(latest.surprise)) * polarity
    changes = [abs(series[index].value - series[index - 1].value) for index in range(1, len(series))]
    nonzero = [value for value in changes if not math.isclose(value, 0.0)]
    scale = statistics.median(nonzero) if nonzero else None
    if scale is None:
        return None
    return max(-5.0, min(5.0, float(latest.surprise) / scale)) * polarity


def _prediction_scenario_shocks(text: str) -> dict[str, int]:
    if _prediction_term_matches(text, "rate cut") or (_prediction_term_matches(text, "fed") and _prediction_term_matches(text, "cut")):
        return {"policy_stance": -1, "rates": -1}
    if _prediction_term_matches(text, "rate hike") or (_prediction_term_matches(text, "fed") and _prediction_term_matches(text, "hike")):
        return {"policy_stance": 1, "rates": 1}
    if _prediction_term_matches(text, "recession"):
        return {"growth": -1, "labor": -1, "financial_conditions": 1}
    if _prediction_term_matches(text, "taiwan") and any(_prediction_term_matches(text, term) for term in ("invasion", "invade", "war")):
        return {"growth": -1, "financial_conditions": 1}
    if any(_prediction_term_matches(text, term) for term in ("inflation above", "cpi above", "inflation rise", "inflation higher")):
        return {"inflation": 1, "policy_stance": 1}
    return {}


def _prediction_term_matches(text: str, term: str) -> bool:
    """Match complete words/phrases so short symbols never hit inside names."""
    words = [re.escape(item) for item in term.lower().split() if item]
    if not words:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(words) + r"(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None
