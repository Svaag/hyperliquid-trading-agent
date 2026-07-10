from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.schemas import FeatureValue
from hyperliquid_trading_agent.app.metrics import ENGINE_NEWS_RISK_TRANSITIONS
from hyperliquid_trading_agent.app.newswire.assessment import ASSESSMENT_VERSION
from hyperliquid_trading_agent.app.newswire.schemas import NewswireAssessment, NewswireEvent, NewswireStory

NewsRiskMode = Literal["neutral", "risk_on", "risk_off", "shock"]
_MODE_CODE: dict[NewsRiskMode, float] = {"shock": -2.0, "risk_off": -1.0, "neutral": 0.0, "risk_on": 1.0}


class NewsRiskState(BaseModel):
    scope: str
    mode: NewsRiskMode = "neutral"
    signed_pressure: float = Field(default=0.0, ge=-1.0, le=1.0)
    risk_pressure: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_story_ids: list[str] = Field(default_factory=list)
    entered_at_ms: int
    updated_at_ms: int
    expires_at_ms: int
    assessment_version: str = ASSESSMENT_VERSION
    transition_reason: str = "initialized"
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _Contribution:
    story_id: str
    scope: str
    signed: float
    risk: float
    confidence: float
    impact: float
    source_quality: float
    independent_sources: int
    event_type: str
    created_at_ms: int
    expires_at_ms: int


class NewsRiskStateMachine:
    """Asymmetric, persisted news-risk overlay used by the institutional engine."""

    def __init__(self, settings: Settings, repository: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.states: dict[str, NewsRiskState] = {}
        self._contributions: dict[tuple[str, str], _Contribution] = {}
        self._exit_observations: dict[str, int] = {}

    async def hydrate(self) -> None:
        repo = self.repository
        method = getattr(repo, "list_newswire_risk_states", None) if repo is not None else None
        if not callable(method):
            return
        for row in await method(limit=1000):
            state = NewsRiskState.model_validate(row)
            self.states[state.scope] = state
        story_method = getattr(repo, "list_newswire_stories", None)
        if not callable(story_method):
            return
        now = _now_ms()
        try:
            rows = await story_method(limit=1000)
        except Exception:
            return
        for row in reversed(rows):
            try:
                story = NewswireStory.model_validate(row)
            except Exception:
                continue
            if story.status == "retracted":
                continue
            event = story.to_event(update_type="updated")
            assessment = story.assessment
            if assessment is None or assessment.engine_action in {"ignore", "ledger_only"}:
                continue
            expires_at_ms = event.received_at_ms + self._ttl_ms(event.event_type)
            if expires_at_ms < now:
                continue
            for scope in self._scopes(event):
                contribution = self._build_contribution(event, assessment, story.story_id, scope)
                self._contributions[(scope, story.story_id)] = contribution

    async def observe(self, event: NewswireEvent, *, feature_store: Any | None = None) -> list[NewsRiskState]:
        assessment = _assessment(event)
        if assessment is None or assessment.engine_action in {"ignore", "ledger_only"}:
            return []
        story_id = str(event.story_id or event.metadata.get("story_id") or event.event_id)
        scopes = self._scopes(event)
        if not scopes:
            return []
        now_ms = int(event.received_at_ms)
        changed: list[NewsRiskState] = []
        for scope in scopes:
            self._contributions[(scope, story_id)] = self._build_contribution(event, assessment, story_id, scope)
            state = await self._recompute(scope, now_ms=now_ms, feature_store=feature_store)
            changed.append(state)
            if feature_store is not None:
                await self._record_features(feature_store, event, state)
        return changed

    def _scopes(self, event: NewswireEvent) -> list[str]:
        scopes = set(event.symbols)
        if event.asset_class == "macro":
            scopes.add("GLOBAL")
            proxies = self.settings.engine_news_macro_proxy_symbol_list or self.settings.autonomy_core_symbols
            scopes.update(proxies)
        return sorted(scope.upper() for scope in scopes if scope)

    def _build_contribution(
        self,
        event: NewswireEvent,
        assessment: NewswireAssessment,
        story_id: str,
        scope: str,
    ) -> _Contribution:
        independent_sources = int(
            event.metadata.get("story_independent_source_count")
            or len(event.metadata.get("story_sources") or [])
            or 1
        )
        corroboration = 1.0 if assessment.source_quality_score >= 90 else 0.90 if independent_sources >= 2 else 0.65
        signed = (
            assessment.risk_bias
            * (assessment.impact_score / 100.0)
            * assessment.direction_confidence
            * (assessment.source_quality_score / 100.0)
            * corroboration
        )
        risk = assessment.risk_severity * (assessment.source_quality_score / 100.0) * corroboration
        created_at_ms = int(event.received_at_ms)
        return _Contribution(
            story_id=story_id,
            scope=scope,
            signed=max(-1.0, min(1.0, signed)),
            risk=max(0.0, min(1.0, risk)),
            confidence=max(assessment.direction_confidence, risk),
            impact=assessment.impact_score,
            source_quality=assessment.source_quality_score,
            independent_sources=independent_sources,
            event_type=event.event_type,
            created_at_ms=created_at_ms,
            expires_at_ms=created_at_ms + self._ttl_ms(event.event_type),
        )

    async def retract(self, event: NewswireEvent, *, feature_store: Any | None = None) -> list[NewsRiskState]:
        """Remove a retracted story's live contribution and emit the corrected state."""
        story_id = str(event.story_id or event.metadata.get("story_id") or event.event_id)
        scopes = sorted({scope for scope, candidate_story_id in self._contributions if candidate_story_id == story_id})
        for key in [key for key in self._contributions if key[1] == story_id]:
            self._contributions.pop(key, None)
        changed: list[NewsRiskState] = []
        for scope in scopes:
            state = await self._recompute(
                scope,
                now_ms=int(event.received_at_ms),
                feature_store=feature_store,
                force_recalculate=True,
            )
            changed.append(state)
            if feature_store is not None:
                await self._record_features(feature_store, event, state)
        return changed

    async def refresh(self, *, now_ms: int | None = None, feature_store: Any | None = None) -> list[NewsRiskState]:
        ts = int(now_ms or _now_ms())
        scopes = sorted({*self.states, *(scope for scope, _ in self._contributions)})
        states: list[NewsRiskState] = []
        for scope in scopes:
            state = await self._recompute(scope, now_ms=ts, feature_store=feature_store)
            states.append(state)
            if feature_store is not None:
                await self._record_features(feature_store, None, state)
        return states

    def current(self, scope: str, *, now_ms: int | None = None) -> NewsRiskState | None:
        state = self.states.get(scope.upper())
        if state is None:
            return None
        ts = int(now_ms or _now_ms())
        if state.expires_at_ms < ts and state.mode != "neutral":
            return state.model_copy(
                update={
                    "mode": "neutral",
                    "signed_pressure": 0.0,
                    "risk_pressure": 0.0,
                    "confidence": 0.0,
                    "updated_at_ms": ts,
                    "expires_at_ms": ts,
                    "transition_reason": "expired",
                }
            )
        return state

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.settings.engine_news_risk_overlay_mode,
            "states": {scope: state.model_dump(mode="json") for scope, state in self.states.items()},
            "active_contributions": len(self._contributions),
        }

    async def _recompute(
        self,
        scope: str,
        *,
        now_ms: int,
        feature_store: Any | None,
        force_recalculate: bool = False,
    ) -> NewsRiskState:
        contributions = []
        half_life_ms = max(1, int(self.settings.engine_news_risk_half_life_seconds)) * 1000
        for key, item in list(self._contributions.items()):
            if item.expires_at_ms < now_ms:
                self._contributions.pop(key, None)
                continue
            if item.scope != scope:
                continue
            decay = math.exp(-math.log(2.0) * max(0, now_ms - item.created_at_ms) / half_life_ms)
            contributions.append((item, decay))
        signed = max(-1.0, min(1.0, sum(item.signed * decay for item, decay in contributions)))
        risk = max([item.risk * decay for item, decay in contributions], default=0.0)
        confidence = max([item.confidence * decay for item, decay in contributions], default=0.0)
        evidence = [item.story_id for item, _ in sorted(contributions, key=lambda pair: abs(pair[0].signed) + pair[0].risk, reverse=True)[:10]]
        previous = self.states.get(scope)
        market = _market_confirmation(feature_store, scope)
        mode, reason = self._target_mode(
            None if force_recalculate else previous,
            contributions,
            signed=signed,
            risk=risk,
            market=market,
            now_ms=now_ms,
        )
        if force_recalculate and mode == "neutral":
            reason = "source_story_retracted"
        entered_at = previous.entered_at_ms if previous is not None and previous.mode == mode else now_ms
        expires_at = max([item.expires_at_ms for item, _ in contributions], default=now_ms)
        state = NewsRiskState(
            scope=scope,
            mode=mode,
            signed_pressure=round(signed, 6),
            risk_pressure=round(risk, 6),
            confidence=round(confidence, 6),
            evidence_story_ids=evidence,
            entered_at_ms=entered_at,
            updated_at_ms=now_ms,
            expires_at_ms=expires_at,
            transition_reason=reason,
            metadata={"market_confirmation": market, "overlay_mode": self.settings.engine_news_risk_overlay_mode},
        )
        self.states[scope] = state
        await self._persist(state, previous)
        return state

    def _target_mode(
        self,
        previous: NewsRiskState | None,
        contributions: list[tuple[_Contribution, float]],
        *,
        signed: float,
        risk: float,
        market: dict[str, bool],
        now_ms: int,
    ) -> tuple[NewsRiskMode, str]:
        shock = any(
            (item.signed < 0 or (item.event_type in {"halt", "regulatory", "crypto_protocol", "exchange_status"} and item.signed <= 0))
            and item.impact >= 85
            and item.risk >= 0.75
            and item.source_quality >= 90
            and item.event_type in {"halt", "regulatory", "crypto_protocol", "exchange_status", "macro"}
            for item, _ in contributions
        )
        if shock:
            self._exit_observations[previous.scope if previous else ""] = 0
            return "shock", "primary_source_negative_shock"
        independent = max([item.independent_sources for item, _ in contributions], default=0)
        if signed <= -float(self.settings.engine_news_risk_off_threshold) and (independent >= 2 or market["risk_off"]):
            return "risk_off", "negative_pressure_confirmed"
        if signed >= float(self.settings.engine_news_risk_on_threshold) and independent >= 2 and market["risk_on"]:
            return "risk_on", "positive_pressure_and_market_confirmed"
        if previous is not None and previous.mode != "neutral":
            held_ms = now_ms - previous.entered_at_ms
            below_exit = abs(signed) < float(self.settings.engine_news_risk_exit_threshold) and risk < float(self.settings.engine_news_risk_exit_threshold)
            count = self._exit_observations.get(previous.scope, 0) + 1 if below_exit else 0
            self._exit_observations[previous.scope] = count
            if held_ms < max(1, int(self.settings.engine_news_risk_min_hold_seconds)) * 1000 or self._exit_observations[previous.scope] < 2:
                return previous.mode, "hysteresis_hold"
        return "neutral", "pressure_below_transition_threshold"

    async def _persist(self, state: NewsRiskState, previous: NewsRiskState | None) -> None:
        from_mode: NewsRiskMode = previous.mode if previous is not None else "neutral"
        transitioned = from_mode != state.mode
        if transitioned:
            ENGINE_NEWS_RISK_TRANSITIONS.labels(from_mode=from_mode, to_mode=state.mode).inc()
        repo = self.repository
        if repo is None:
            return
        upsert = getattr(repo, "upsert_newswire_risk_state", None)
        if callable(upsert):
            await upsert(state.model_dump(mode="json"))
        if not transitioned:
            return
        record = getattr(repo, "record_newswire_risk_transition", None)
        if callable(record):
            digest = hashlib.sha1(f"{state.scope}:{from_mode}:{state.mode}:{state.updated_at_ms}".encode()).hexdigest()[:24]
            await record(
                {
                    "transition_id": "nwrt_" + digest,
                    "scope": state.scope,
                    "from_mode": from_mode,
                    "to_mode": state.mode,
                    "signed_pressure": state.signed_pressure,
                    "risk_pressure": state.risk_pressure,
                    "confidence": state.confidence,
                    "evidence_story_ids": state.evidence_story_ids,
                    "reason": state.transition_reason,
                    "created_at_ms": state.updated_at_ms,
                    "metadata": state.metadata,
                }
            )

    async def _record_features(
        self,
        feature_store: Any,
        event: NewswireEvent | None,
        state: NewsRiskState,
    ) -> None:
        event_id = event.event_id if event is not None else f"risk_refresh_{state.scope}"
        received_at_ms = event.received_at_ms if event is not None else state.updated_at_ms
        for name, value in (
            ("news_signed_pressure", state.signed_pressure),
            ("news_risk_pressure", state.risk_pressure),
            ("news_risk_mode_code", _MODE_CODE[state.mode]),
        ):
            digest = hashlib.sha1(f"{event_id}:{state.scope}:{name}:{state.updated_at_ms}".encode()).hexdigest()[:24]
            feature = FeatureValue(
                feature_id="feat_" + digest,
                asset=state.scope,
                feature_group="news",
                feature_name=name,
                value={"value": value, "mode": state.mode, "evidence_story_ids": state.evidence_story_ids},
                scalar_value=float(value),
                event_ts_ms=event.published_at_ms if event is not None else None,
                received_ts_ms=received_at_ms,
                computed_ts_ms=max(received_at_ms, _now_ms()),
                source_event_id=event.event_id if event is not None else (state.evidence_story_ids[0] if state.evidence_story_ids else None),
                source="newswire_risk_state",
                version=ASSESSMENT_VERSION,
                quality_score=state.confidence,
                staleness_ms=(
                    None
                    if event is None or event.published_at_ms is None
                    else max(0, event.received_at_ms - event.published_at_ms)
                ),
                metadata={
                    "story_id": event.story_id if event is not None else None,
                    "evidence_story_ids": state.evidence_story_ids,
                    "news_risk_mode": state.mode,
                    "refresh": event is None,
                },
            )
            await feature_store.record(feature)

    def _ttl_ms(self, event_type: str) -> int:
        if event_type == "macro":
            seconds = self.settings.engine_news_risk_macro_ttl_seconds
        elif event_type in {"crypto_protocol", "exchange_status", "halt"}:
            seconds = self.settings.engine_news_risk_protocol_ttl_seconds
        else:
            seconds = self.settings.engine_news_risk_default_ttl_seconds
        return max(60, int(seconds)) * 1000


def _assessment(event: NewswireEvent) -> NewswireAssessment | None:
    if event.assessment is not None:
        return event.assessment
    raw = event.metadata.get("newswire_assessment") if isinstance(event.metadata, dict) else None
    try:
        return NewswireAssessment.model_validate(raw) if isinstance(raw, dict) else None
    except Exception:
        return None


def _market_confirmation(feature_store: Any | None, scope: str) -> dict[str, bool]:
    if feature_store is None or scope == "GLOBAL":
        return {"risk_on": False, "risk_off": False}
    snapshot = feature_store.snapshot(asset=scope)
    features = snapshot.features
    returns = _float(features.get("mid_return_5m_bps")) or 0.0
    imbalance = _float(features.get("top_imbalance")) or 0.0
    spread = _float(features.get("spread_bps"))
    liquidity_ok = spread is None or spread <= 20.0
    return {
        "risk_on": bool(returns >= 20 and imbalance >= 0 and liquidity_ok),
        "risk_off": bool(returns <= -20 or imbalance <= -0.2 or (spread is not None and spread > 20.0)),
    }


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
