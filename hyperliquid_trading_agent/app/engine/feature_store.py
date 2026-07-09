from __future__ import annotations

import hashlib
import math
import statistics
from bisect import bisect_right, insort
from collections import deque
from itertools import pairwise
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import FeatureSnapshot, FeatureValue, NormalizedEvent

FEATURE_VERSION = "engine_features_v1"

ROLLUP_SOURCE_FEATURES = (
    "mid",
    "open_interest",
    "top_depth_usd",
    "spread_bps",
    "perp_basis_bps",
    "funding_hourly",
    "day_volume_usd",
)
_LONG_WINDOW_FEATURES = {"funding_hourly"}
_POINT_TS = 0
_POINT_VALUE = 1
_POINT_FID = 2


class FeatureStore:
    """Point-in-time feature store with repository-backed persistence.

    In-memory state is bounded: scalar histories live in per-(asset, feature)
    time-ordered lists with age/length eviction, so per-event rollups touch only
    the points inside their lookback window instead of the full process history.
    """

    def __init__(
        self,
        repository: Any | None = None,
        *,
        cross_venue_dexes: list[str] | None = None,
        max_age_seconds: int = 7200,
        funding_max_age_seconds: int = 90000,
        max_points_per_series: int = 4096,
        full_universe_enabled: bool = False,
        recent_buffer_size: int = 512,
    ):
        self.repository = repository
        self.cross_venue_dexes = [dex.lower().strip() for dex in (cross_venue_dexes or []) if dex and dex.strip()]
        self.max_age_ms = max(60, int(max_age_seconds)) * 1000
        self.funding_max_age_ms = max(self.max_age_ms, int(funding_max_age_seconds) * 1000)
        self.max_points_per_series = max(16, int(max_points_per_series))
        self.full_universe_enabled = bool(full_universe_enabled)
        self._series: dict[tuple[str, str], list[tuple[int, float, str]]] = {}
        self._latest: dict[str, dict[str, FeatureValue]] = {}
        self._recent: dict[str, deque[FeatureValue]] = {}
        self._recent_buffer_size = max(32, int(recent_buffer_size))

    async def record(self, feature: FeatureValue) -> FeatureValue:
        asset = feature.asset
        by_name = self._latest.setdefault(asset, {})
        current = by_name.get(feature.feature_name)
        if current is None or feature.computed_ts_ms >= current.computed_ts_ms:
            by_name[feature.feature_name] = feature
        self._recent.setdefault(asset, deque(maxlen=self._recent_buffer_size)).append(feature)
        if feature.scalar_value is not None:
            self._append_point(asset, feature.feature_name, feature.computed_ts_ms, float(feature.scalar_value), feature.feature_id)
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_feature_value", None)
            if callable(record):
                await record(feature.model_dump(mode="json"))
        return feature

    def _append_point(self, asset: str, feature_name: str, ts_ms: int, value: float, feature_id: str) -> None:
        key = (asset, feature_name)
        points = self._series.setdefault(key, [])
        if points and points[-1][_POINT_FID] == feature_id:
            return
        point = (ts_ms, value, feature_id)
        if points and ts_ms < points[-1][_POINT_TS]:
            insort(points, point, key=lambda item: item[_POINT_TS])
        else:
            points.append(point)
        max_age_ms = self.funding_max_age_ms if feature_name in _LONG_WINDOW_FEATURES else self.max_age_ms
        horizon = points[-1][_POINT_TS] - max_age_ms
        if points[0][_POINT_TS] < horizon:
            del points[: bisect_right(points, horizon, key=lambda item: item[_POINT_TS])]
        if len(points) > self.max_points_per_series:
            del points[: len(points) - self.max_points_per_series]

    def _series_slice(self, asset: str, feature_name: str, *, as_of_ms: int) -> list[tuple[int, float]]:
        points = self._series.get((asset, feature_name)) or []
        idx = bisect_right(points, as_of_ms, key=lambda item: item[_POINT_TS])
        return [(ts, value) for ts, value, _ in points[:idx]]

    def _rollups_for(self, asset: str, *, as_of_ms: int | None = None) -> list[FeatureValue]:
        tails = [points[-1][_POINT_TS] for name in ROLLUP_SOURCE_FEATURES if (points := self._series.get((asset, name)))]
        cutoff = as_of_ms or (max(tails) if tails else now_ms())
        series = {name: self._series_slice(asset, name, as_of_ms=cutoff) for name in ROLLUP_SOURCE_FEATURES}
        return _compute_rollups(asset=asset, series=series, cutoff_ms=cutoff)

    async def features_for_event(self, event: NormalizedEvent) -> list[FeatureValue]:
        features = derive_features(event, cross_venue_dexes=self.cross_venue_dexes)
        allowed = {str(symbol).upper() for symbol in (event.symbols or [])}
        if allowed and not self.full_universe_enabled:
            features = [feature for feature in features if feature.asset in allowed]
        for feature in features:
            await self.record(feature)
        rollups: list[FeatureValue] = []
        for asset in sorted({feature.asset for feature in features}):
            rollups.extend(self._rollups_for(asset))
        for feature in rollups:
            await self.record(feature)
        return [*features, *rollups]

    async def features_for_world_model_snapshot(self, *, asset: str, snapshot: Any) -> list[FeatureValue]:
        features = derive_world_model_features(asset=asset, snapshot=snapshot)
        for feature in features:
            await self.record(feature)
        return features

    async def latest(self, *, asset: str, feature_name: str | None = None, limit: int = 100) -> list[FeatureValue]:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            list_values = getattr(self.repository, "list_feature_values", None)
            if callable(list_values):
                return [FeatureValue(**item) for item in await list_values(asset=asset, feature_name=feature_name, limit=limit)]
        asset = asset.upper()
        items = list(self._recent.get(asset) or [])
        if feature_name:
            items = [item for item in items if item.feature_name == feature_name]
        return sorted(items, key=lambda item: item.computed_ts_ms, reverse=True)[:limit]

    def snapshot(self, *, asset: str, as_of_ms: int | None = None, max_items: int = 200) -> FeatureSnapshot:
        asset = asset.upper()
        cutoff = as_of_ms or now_ms()
        latest_by_name: dict[str, FeatureValue] = {}
        for name, item in (self._latest.get(asset) or {}).items():
            if item.computed_ts_ms <= cutoff:
                latest_by_name[name] = item
                continue
            # Latest value is newer than the requested cutoff: rewind scalar
            # series to the last point at-or-before the cutoff when available.
            points = self._series.get((asset, name)) or []
            idx = bisect_right(points, cutoff, key=lambda point: point[_POINT_TS]) - 1
            if idx >= 0:
                ts, value, fid = points[idx]
                latest_by_name[name] = item.model_copy(
                    update={"feature_id": fid, "scalar_value": value, "computed_ts_ms": ts, "value": {name: value}}
                )
        selected = sorted(latest_by_name.values(), key=lambda item: item.computed_ts_ms)[-max_items:]
        features = {item.feature_name: item.value if item.scalar_value is None else item.scalar_value for item in selected}
        quality_flags = [f"low_quality:{item.feature_name}" for item in selected if item.quality_score < 0.5]
        snapshot_id = "fs_" + hashlib.sha1(f"{asset}:{cutoff}:{sorted(features.items())}".encode()).hexdigest()[:24]
        return FeatureSnapshot(
            snapshot_id=snapshot_id,
            asset=asset,
            as_of_ms=cutoff,
            feature_ids=[item.feature_id for item in selected],
            features=features,
            quality_flags=quality_flags,
        )


def derive_features(event: NormalizedEvent, *, cross_venue_dexes: list[str] | None = None) -> list[FeatureValue]:
    if event.event_type in {"all_mids", "mid", "price"}:
        return _price_features(event)
    if event.event_type in {"l2_book", "l2Book", "orderbook"}:
        return _orderbook_features(event)
    if event.event_type in {"news", "newswire"}:
        return _news_features(event)
    if event.event_type in {"asset_ctx", "meta_and_asset_ctxs", "funding_oi"}:
        return _funding_oi_features(event)
    if event.event_type in {"liquidation_signal", "liquidation_features"}:
        return _liquidation_features(event)
    if event.event_type in {"cross_venue", "cross_venue_market", "cross_venue_features"}:
        return _cross_venue_features(event, enabled_dexes=cross_venue_dexes or [])
    return []


def derive_world_model_features(*, asset: str, snapshot: Any) -> list[FeatureValue]:
    data = snapshot.model_dump(mode="json") if callable(getattr(snapshot, "model_dump", None)) else dict(snapshot or {})
    _assert_world_model_advisory(data)
    asset = asset.upper()
    ts = int(data.get("as_of_ms") or now_ms())
    pseudo_event = NormalizedEvent(
        event_id=str(data.get("snapshot_id") or f"world_model:{asset}:{ts}"),
        event_type="world_model_snapshot",
        asset_class="crypto",
        symbols=[asset],
        source="world_model",
        provider="internal",
        received_ts_ms=ts,
        computed_ts_ms=max(ts, now_ms()),
        payload=data,
        quality_score=1.0,
        metadata={"paper_only": True, "execution_authority": "none"},
    )
    clusters = [item for item in data.get("narrative_clusters") or [] if asset in [str(symbol).upper() for symbol in item.get("symbols", [])]]
    predictions = [item for item in data.get("prediction_market_signals") or [] if asset in [str(symbol).upper() for symbol in item.get("symbols", [])]]
    beliefs = [item for item in data.get("top_beliefs") or [] if asset in [str(symbol).upper() for symbol in item.get("symbols", [])]]
    out: list[FeatureValue] = []
    if clusters:
        strongest = max(clusters, key=lambda item: abs(float(item.get("pressure_score") or 0.0)))
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="narrative_pressure", value={"cluster_id": strongest.get("cluster_id"), "pressure": strongest.get("pressure_score")}, scalar=_float(strongest.get("pressure_score"))))
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="belief_conflict_score", value={"cluster_id": strongest.get("cluster_id"), "conflict": strongest.get("conflict_score")}, scalar=_float(strongest.get("conflict_score"))))
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="source_consensus_score", value={"cluster_id": strongest.get("cluster_id"), "consensus": strongest.get("consensus_score")}, scalar=_float(strongest.get("consensus_score"))))
    if predictions:
        top = max(predictions, key=lambda item: (float(item.get("confidence") or 0.0), float(item.get("liquidity_usd") or 0.0)))
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="prediction_implied_probability", value={"signal_id": top.get("signal_id"), "question": top.get("question"), "probability": top.get("implied_probability")}, scalar=_float(top.get("implied_probability"))))
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="prediction_probability_delta", value={"signal_id": top.get("signal_id"), "delta": top.get("probability_delta")}, scalar=_float(top.get("probability_delta"))))
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="prediction_liquidity_usd", value={"signal_id": top.get("signal_id"), "liquidity_usd": top.get("liquidity_usd")}, scalar=_float(top.get("liquidity_usd"))))
    if beliefs:
        avg_salience = sum(float(item.get("salience") or 0.0) for item in beliefs) / len(beliefs)
        out.append(_feature(pseudo_event, asset=asset, group="world_model", name="belief_salience", value={"belief_count": len(beliefs), "avg_salience": avg_salience}, scalar=avg_salience))
    return out


def _feature(
    event: NormalizedEvent,
    *,
    asset: str,
    group: str,
    name: str,
    value: dict[str, Any],
    scalar: float | None = None,
    quality: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> FeatureValue:
    computed = now_ms()
    fid = "feat_" + hashlib.sha1(f"{event.event_id}:{asset}:{group}:{name}".encode()).hexdigest()[:24]
    return FeatureValue(
        feature_id=fid,
        asset=asset,
        feature_group=group,
        feature_name=name,
        value=value,
        scalar_value=scalar,
        event_ts_ms=event.event_ts_ms,
        received_ts_ms=event.received_ts_ms,
        computed_ts_ms=max(computed, event.received_ts_ms),
        source_event_id=event.event_id,
        source=event.source,
        version=FEATURE_VERSION,
        quality_score=quality if quality is not None else event.quality_score,
        staleness_ms=event.staleness_ms,
        metadata={**dict(event.metadata or {}), **(metadata or {})},
    )


def _assert_world_model_advisory(data: dict[str, Any]) -> None:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    authority = metadata.get("execution_authority")
    if authority not in {None, "none"}:
        raise ValueError("world model snapshots must be advisory-only at engine feature boundary")
    forbidden_keys = {
        "exchange_actions",
        "order_intents",
        "orders",
        "risk_mutations",
        "config_changes",
        "execution_requests",
    }
    for container in (data, metadata):
        for key in forbidden_keys:
            if container.get(key):
                raise ValueError(f"world model snapshot cannot carry {key}")


def _price_features(event: NormalizedEvent) -> list[FeatureValue]:
    payload = event.payload
    mids = payload.get("mids") if isinstance(payload.get("mids"), dict) else payload
    out: list[FeatureValue] = []
    for raw_symbol, raw_px in mids.items():
        px = _float(raw_px)
        if px is None or px <= 0:
            continue
        out.append(_feature(event, asset=str(raw_symbol).upper(), group="price", name="mid", value={"mid": px}, scalar=px))
    return out


def _orderbook_features(event: NormalizedEvent) -> list[FeatureValue]:
    symbol = (event.symbols[0] if event.symbols else str(event.payload.get("coin") or event.payload.get("symbol") or "")).upper()
    if not symbol:
        return []
    levels = event.payload.get("levels") or []
    bids = levels[0] if len(levels) > 0 else []
    asks = levels[1] if len(levels) > 1 else []
    bid_px, bid_sz = _level_px_sz(bids[0]) if bids else (None, None)
    ask_px, ask_sz = _level_px_sz(asks[0]) if asks else (None, None)
    out: list[FeatureValue] = []
    if bid_px and ask_px and bid_px > 0 and ask_px > 0:
        mid = (bid_px + ask_px) / 2
        spread_bps = (ask_px - bid_px) / mid * 10_000
        top_depth = (bid_px * (bid_sz or 0)) + (ask_px * (ask_sz or 0))
        imbalance = ((bid_sz or 0) - (ask_sz or 0)) / max((bid_sz or 0) + (ask_sz or 0), 1e-9)
        out.extend(
            [
                _feature(event, asset=symbol, group="microstructure", name="spread_bps", value={"spread_bps": spread_bps}, scalar=spread_bps),
                _feature(event, asset=symbol, group="microstructure", name="top_depth_usd", value={"top_depth_usd": top_depth}, scalar=top_depth),
                _feature(event, asset=symbol, group="microstructure", name="top_imbalance", value={"top_imbalance": imbalance}, scalar=imbalance),
            ]
        )
    return out


def _news_features(event: NormalizedEvent) -> list[FeatureValue]:
    payload = event.payload or {}
    decision = _news_policy_decision(payload, event.metadata)
    if decision and decision.get("shadow_only") is False:
        return _news_policy_features(event, decision)
    importance = _float(payload.get("importance_score")) or _float(payload.get("importance")) or 0.0
    sentiment = str(payload.get("sentiment") or "unknown").lower()
    confidence = _float(payload.get("confidence"))
    if confidence is None:
        confidence = float(event.quality_score or 0.0)
    source_score = _float(payload.get("source_score"))
    if source_score is None:
        source_score = float(event.quality_score or 0.0)
    weighted = max(0.0, min(1.0, importance / 100.0 * max(0.0, min(1.0, confidence)) * max(0.0, min(1.0, source_score))))
    direction = 1.0 if sentiment == "bullish" else -1.0 if sentiment == "bearish" else 0.0
    catalyst_pressure = direction * weighted
    newswire_event_id = str(payload.get("newswire_event_id") or event.metadata.get("source_newswire_event_id") or event.event_id)
    news_metadata = {
        "newswire_event_id": newswire_event_id,
        "headline": payload.get("headline"),
        "event_type": payload.get("event_type") or event.event_type,
        "urgency": payload.get("urgency"),
        "importance_score": importance,
        "sentiment": sentiment,
        "confidence": confidence,
        "source_score": source_score,
    }
    out: list[FeatureValue] = []
    for symbol in event.symbols:
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="catalyst_pressure",
                value={
                    "importance_score": importance,
                    "sentiment": sentiment,
                    "confidence": confidence,
                    "source_score": source_score,
                    "pressure": catalyst_pressure,
                    "weighted_pressure": weighted,
                },
                scalar=catalyst_pressure,
                metadata=news_metadata,
            )
        )
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="event_risk_pressure",
                value={
                    "importance_score": importance,
                    "sentiment": sentiment,
                    "confidence": confidence,
                    "source_score": source_score,
                    "pressure": weighted,
                    "directional_pressure": catalyst_pressure,
                },
                scalar=weighted,
                metadata=news_metadata,
            )
        )
        consensus = min(max(0.0, min(1.0, confidence)), max(0.0, min(1.0, source_score)))
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="source_consensus_score",
                value={"confidence": confidence, "source_score": source_score, "consensus": consensus},
                scalar=consensus,
                metadata=news_metadata,
            )
        )
    return out


def _news_policy_features(event: NormalizedEvent, decision: dict[str, Any]) -> list[FeatureValue]:
    engine_action = str(decision.get("engine_action") or "ignore")
    if engine_action in {"ignore", "ledger_only"}:
        return []
    impact01 = _bounded01((_float(decision.get("market_impact_score")) or 0.0) / 100.0)
    relevance01 = _bounded01((_float(decision.get("relevance_score")) or 0.0) / 100.0)
    novelty01 = _bounded01((_float(decision.get("novelty_score")) or 0.0) / 100.0)
    urgency01 = _bounded01((_float(decision.get("urgency_score")) or 0.0) / 100.0)
    quality01 = _bounded01((_float(decision.get("quality_score")) or 0.0) / 100.0)
    confidence = _bounded01(_float(decision.get("confidence")) or 0.0)
    source_score = _bounded01(_float(decision.get("source_score")) or 0.0)
    direction_score = max(-1.0, min(1.0, _float(decision.get("direction_score")) or 0.0))
    direction_confidence = _bounded01(_float(decision.get("direction_confidence")) or 0.0)
    risk_score = _bounded01(_float(decision.get("risk_score")) or 0.0)
    weighted = impact01 * confidence * source_score * relevance01 * max(0.35, novelty01) * max(0.50, urgency01)
    catalyst_pressure = 0.0 if engine_action == "risk_only" else weighted * direction_score * direction_confidence
    event_risk_pressure = weighted * risk_score
    source_consensus = min(1.0, 0.50 * confidence + 0.30 * source_score + 0.20 * quality01)
    payload = event.payload or {}
    newswire_event_id = str(payload.get("newswire_event_id") or event.metadata.get("source_newswire_event_id") or event.event_id)
    news_metadata = {
        "newswire_event_id": newswire_event_id,
        "headline": payload.get("headline"),
        "event_type": payload.get("event_type") or event.event_type,
        "urgency": payload.get("urgency"),
        "policy_version": decision.get("policy_version"),
        "decision_id": decision.get("decision_id"),
        "newswire_action": decision.get("newswire_action"),
        "engine_action": engine_action,
        "market_impact_score": _float(decision.get("market_impact_score")) or 0.0,
        "quality_score": _float(decision.get("quality_score")) or 0.0,
        "relevance_score": _float(decision.get("relevance_score")) or 0.0,
        "novelty_score": _float(decision.get("novelty_score")) or 0.0,
        "urgency_score": _float(decision.get("urgency_score")) or 0.0,
        "confidence": confidence,
        "source_score": source_score,
        "direction_score": direction_score,
        "direction_confidence": direction_confidence,
        "risk_score": risk_score,
    }
    out: list[FeatureValue] = []
    for symbol in event.symbols:
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="catalyst_pressure",
                value={**news_metadata, "pressure": catalyst_pressure, "weighted_pressure": weighted},
                scalar=catalyst_pressure,
                quality=quality01,
                metadata=news_metadata,
            )
        )
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="event_risk_pressure",
                value={**news_metadata, "pressure": event_risk_pressure, "weighted_pressure": weighted},
                scalar=event_risk_pressure,
                quality=quality01,
                metadata=news_metadata,
            )
        )
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="source_consensus_score",
                value={**news_metadata, "consensus": source_consensus},
                scalar=source_consensus,
                quality=quality01,
                metadata=news_metadata,
            )
        )
    return out


def _news_policy_decision(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("newswire_policy_decision")
    if isinstance(direct, dict):
        return direct
    nested = metadata.get("newswire_policy_decision")
    if isinstance(nested, dict):
        return nested
    source_metadata = metadata.get("source_newswire_metadata")
    if isinstance(source_metadata, dict) and isinstance(source_metadata.get("newswire_policy_decision"), dict):
        return source_metadata["newswire_policy_decision"]
    return {}


def _funding_oi_features(event: NormalizedEvent) -> list[FeatureValue]:
    out: list[FeatureValue] = []
    for symbol, ctx in _iter_asset_contexts(event):
        funding = _first_float(ctx, "funding", "funding_hourly")
        oi = _first_float(ctx, "openInterest", "open_interest")
        day_volume = _first_float(ctx, "dayNtlVlm", "day_volume_usd", "day_volume")
        basis_bps = _perp_basis_bps(ctx)
        if funding is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="funding_hourly", value={"funding_hourly": funding}, scalar=funding))
        if oi is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="open_interest", value={"open_interest": oi}, scalar=oi))
        if day_volume is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="day_volume_usd", value={"day_volume_usd": day_volume}, scalar=day_volume))
        if basis_bps is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="perp_basis_bps", value={"perp_basis_bps": basis_bps}, scalar=basis_bps))
    return out


def _perp_basis_bps(ctx: dict[str, Any]) -> float | None:
    direct = _first_float(ctx, "perp_basis_bps", "perpBasisBps", "basis_bps", "basisBps")
    if direct is not None:
        return direct
    mark = _first_float(ctx, "markPx", "mark_price", "markPrice", "mid")
    index = _first_float(ctx, "oraclePx", "oracle_price", "indexPx", "index_price", "oraclePrice")
    if mark is None or index is None or mark <= 0 or index <= 0:
        return None
    return (mark / index - 1.0) * 10_000.0


def _first_float(ctx: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in ctx and ctx.get(key) is not None:
            return _float(ctx.get(key))
    return None


def _iter_asset_contexts(event: NormalizedEvent) -> list[tuple[str, dict[str, Any]]]:
    payload = event.payload or {}
    contexts: list[Any] = []
    universe: list[Any] = []
    if isinstance(payload.get("asset_ctxs"), list):
        contexts = payload.get("asset_ctxs") or []
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
        universe = meta.get("universe", []) if isinstance(meta.get("universe"), list) else []
    elif isinstance(payload.get("contexts"), list):
        contexts = payload.get("contexts") or []
        universe = payload.get("universe", []) if isinstance(payload.get("universe"), list) else []
    elif isinstance(payload.get("raw"), list) and len(payload["raw"]) >= 2:
        meta = payload["raw"][0] if isinstance(payload["raw"][0], dict) else {}
        contexts = payload["raw"][1] if isinstance(payload["raw"][1], list) else []
        universe = meta.get("universe", []) if isinstance(meta.get("universe"), list) else []
    else:
        contexts = [payload]
    out: list[tuple[str, dict[str, Any]]] = []
    for idx, raw_ctx in enumerate(contexts):
        if not isinstance(raw_ctx, dict):
            continue
        symbol = str(raw_ctx.get("coin") or raw_ctx.get("symbol") or raw_ctx.get("name") or "").upper()
        if not symbol and idx < len(universe) and isinstance(universe[idx], dict):
            symbol = str(universe[idx].get("name") or universe[idx].get("coin") or universe[idx].get("symbol") or "").upper()
        if not symbol and idx < len(event.symbols):
            symbol = event.symbols[idx]
        if symbol:
            out.append((symbol.upper(), raw_ctx))
    return out


def _liquidation_features(event: NormalizedEvent) -> list[FeatureValue]:
    symbol = (event.symbols[0] if event.symbols else str(event.payload.get("symbol") or event.payload.get("coin") or "")).upper()
    if not symbol:
        return []
    out: list[FeatureValue] = []
    feature_map = {
        "liq_notional_1m": "liq_notional_1m",
        "liq_notional_5m": "liq_notional_5m",
        "long_vs_short_liq_imbalance_5m": "long_vs_short_liq_imbalance_5m",
        "largest_single_liq_5m": "largest_single_liq_5m",
        "confirmed_only_liq_score_5m": "confirmed_only_liq_score_5m",
        "event_count_5m": "liq_event_count_5m",
        "liq_event_count_5m": "liq_event_count_5m",
    }
    for source_key, feature_name in feature_map.items():
        value = _float(event.payload.get(source_key))
        if value is not None:
            out.append(_feature(event, asset=symbol, group="liquidations", name=feature_name, value={feature_name: value}, scalar=value))
    source_mix = event.payload.get("source_mix_5m")
    if isinstance(source_mix, dict):
        out.append(_feature(event, asset=symbol, group="liquidations", name="source_mix_5m", value={"source_mix_5m": source_mix}, scalar=None))
    return out


def _cross_venue_features(event: NormalizedEvent, *, enabled_dexes: list[str]) -> list[FeatureValue]:
    if not enabled_dexes:
        return []
    symbol = (event.symbols[0] if event.symbols else str(event.payload.get("symbol") or event.payload.get("coin") or "")).upper()
    if not symbol:
        return []
    payload = event.payload or {}
    venues = payload.get("venues") if isinstance(payload.get("venues"), dict) else {}
    enabled = {dex.lower() for dex in enabled_dexes}
    home_mid = _first_float(payload, "hyperliquid_mid", "hl_mid", "mid") or _venue_float(venues, "hyperliquid", "mid", "mark", "mark_px")
    external_mids = [_venue_float(venues, venue, "mid", "mark", "mark_px") for venue in enabled]
    external_mids.extend(_first_float(payload, f"{venue}_mid", f"{venue}_mark") for venue in enabled)
    external_mids = [value for value in external_mids if value is not None and value > 0]
    out: list[FeatureValue] = []
    metadata = {"enabled_cross_venue_dexes": sorted(enabled), "read_only": True, "advisory_only": True}
    if home_mid is not None and home_mid > 0 and external_mids:
        external_mid = sum(external_mids) / len(external_mids)
        delta_bps = (external_mid / home_mid - 1.0) * 10_000.0
        out.append(_feature(event, asset=symbol, group="cross_venue", name="cross_venue_mid_delta_bps", value={"hyperliquid_mid": home_mid, "external_mid": external_mid, "delta_bps": delta_bps}, scalar=delta_bps, metadata=metadata))
    home_volume = _first_float(payload, "hyperliquid_volume_usd", "hl_volume_usd", "day_volume_usd") or _venue_float(venues, "hyperliquid", "volume_usd", "day_volume_usd")
    external_volumes = [_venue_float(venues, venue, "volume_usd", "day_volume_usd") for venue in enabled]
    external_volumes.extend(_first_float(payload, f"{venue}_volume_usd") for venue in enabled)
    external_volumes = [value for value in external_volumes if value is not None and value >= 0]
    if home_volume is not None and external_volumes:
        external_volume = sum(external_volumes)
        denom = max(home_volume + external_volume, 1e-9)
        imbalance = (external_volume - home_volume) / denom
        out.append(_feature(event, asset=symbol, group="cross_venue", name="cross_venue_volume_imbalance", value={"hyperliquid_volume_usd": home_volume, "external_volume_usd": external_volume, "imbalance": imbalance}, scalar=imbalance, metadata=metadata))
    direct_liq = _first_float(payload, "cross_venue_liq_imbalance")
    if direct_liq is None:
        home_liq = _first_float(payload, "hyperliquid_liq_imbalance", "hl_liq_imbalance") or _venue_float(venues, "hyperliquid", "liq_imbalance", "long_vs_short_liq_imbalance") or 0.0
        external_liqs = [_venue_float(venues, venue, "liq_imbalance", "long_vs_short_liq_imbalance") for venue in enabled]
        external_liqs.extend(_first_float(payload, f"{venue}_liq_imbalance") for venue in enabled)
        external_liqs = [value for value in external_liqs if value is not None]
        if external_liqs:
            direct_liq = sum(external_liqs) - home_liq
    if direct_liq is not None:
        out.append(_feature(event, asset=symbol, group="cross_venue", name="cross_venue_liq_imbalance", value={"cross_venue_liq_imbalance": direct_liq}, scalar=direct_liq, metadata=metadata))
    return out


def derive_rolling_features(*, asset: str, features: list[FeatureValue], as_of_ms: int | None = None) -> list[FeatureValue]:
    asset = asset.upper()
    cutoff = as_of_ms or max((item.computed_ts_ms for item in features if item.asset == asset), default=now_ms())
    series = {name: _series(features, asset=asset, feature_name=name, as_of_ms=cutoff) for name in ROLLUP_SOURCE_FEATURES}
    return _compute_rollups(asset=asset, series=series, cutoff_ms=cutoff)


def _compute_rollups(*, asset: str, series: dict[str, list[tuple[int, float]]], cutoff_ms: int) -> list[FeatureValue]:
    asset = asset.upper()
    cutoff = cutoff_ms
    mids = series.get("mid") or []
    oi = series.get("open_interest") or []
    depth = series.get("top_depth_usd") or []
    spread = series.get("spread_bps") or []
    basis = series.get("perp_basis_bps") or []
    funding = series.get("funding_hourly") or []
    volume = series.get("day_volume_usd") or []
    out: list[FeatureValue] = []
    if len(mids) >= 2:
        latest_ts, latest_mid = mids[-1]
        for window_ms, name in ((60_000, "mid_return_1m_bps"), (300_000, "mid_return_5m_bps"), (900_000, "mid_return_15m_bps")):
            baseline = _baseline(mids, latest_ts - window_ms)
            if baseline is not None and baseline > 0:
                value = (latest_mid - baseline) / baseline * 10_000
                out.append(_rollup_feature(asset=asset, name=name, value=value, computed_ts_ms=cutoff))
        for window_ms, name in ((300_000, "realized_vol_5m_bps"), (900_000, "realized_vol_15m_bps")):
            values = [mid for ts, mid in mids if ts >= latest_ts - window_ms]
            returns = [(cur - prev) / prev * 10_000 for prev, cur in pairwise(values) if prev > 0]
            if len(returns) >= 2:
                out.append(_rollup_feature(asset=asset, name=name, value=statistics.pstdev(returns), computed_ts_ms=cutoff))
        range_values = [mid for ts, mid in mids if ts >= latest_ts - 3_600_000]
        if len(range_values) >= 3:
            low = min(range_values)
            high = max(range_values)
            if high > low:
                range_position = (latest_mid - low) / (high - low)
                out.append(_rollup_feature(asset=asset, name="range_position", value=max(0.0, min(1.0, range_position)), computed_ts_ms=cutoff))
                distance = min(abs(latest_mid - low), abs(high - latest_mid)) / latest_mid * 10_000
                out.append(_rollup_feature(asset=asset, name="stop_cluster_distance_bps", value=distance, computed_ts_ms=cutoff))
    if len(oi) >= 2:
        latest_ts, latest_oi = oi[-1]
        baseline = _baseline(oi, latest_ts - 300_000)
        if baseline is not None and baseline > 0:
            out.append(_rollup_feature(asset=asset, name="oi_delta_5m_pct", value=(latest_oi - baseline) / baseline * 100.0, computed_ts_ms=cutoff))
        changes = [cur - prev for prev, cur in pairwise([value for _, value in oi[-12:]])]
        velocity = zscore(changes)
        if velocity is not None:
            out.append(_rollup_feature(asset=asset, name="oi_velocity_z", value=velocity, computed_ts_ms=cutoff))
    if len(depth) >= 2:
        latest_ts, latest_depth = depth[-1]
        baseline = _baseline(depth, latest_ts - 300_000)
        if baseline is not None and baseline > 0:
            thinning = max(0.0, (baseline - latest_depth) / baseline * 100.0)
            out.append(_rollup_feature(asset=asset, name="depth_thinning_5m_pct", value=thinning, computed_ts_ms=cutoff))
    if len(spread) >= 2:
        latest_ts, latest_spread = spread[-1]
        baseline = _baseline(spread, latest_ts - 300_000)
        if baseline is not None:
            out.append(_rollup_feature(asset=asset, name="spread_velocity_5m_bps", value=latest_spread - baseline, computed_ts_ms=cutoff))
    if len(basis) >= 2:
        latest_ts, latest_basis = basis[-1]
        baseline = _baseline(basis, latest_ts - 900_000)
        if baseline is not None:
            out.append(_rollup_feature(asset=asset, name="basis_delta_15m_bps", value=latest_basis - baseline, computed_ts_ms=cutoff))
        z = zscore([value for _, value in basis[-24:]])
        if z is not None:
            out.append(_rollup_feature(asset=asset, name="basis_zscore", value=z, computed_ts_ms=cutoff))
    if len(funding) >= 2:
        latest_ts, latest_funding = funding[-1]
        baseline = _baseline(funding, latest_ts - 900_000)
        if baseline is not None:
            change = latest_funding - baseline
            out.append(_rollup_feature(asset=asset, name="funding_change_15m", value=change, computed_ts_ms=cutoff))
            out.append(_rollup_feature(asset=asset, name="funding_curve_slope", value=change, computed_ts_ms=cutoff))
        trailing_abs = [abs(value) for ts, value in funding if ts >= latest_ts - 86_400_000]
        if len(trailing_abs) >= 24:
            trailing_abs.sort()
            p90 = trailing_abs[max(0, math.ceil(0.9 * len(trailing_abs)) - 1)]
            out.append(_rollup_feature(asset=asset, name="funding_abs_p90_24h", value=p90, computed_ts_ms=cutoff))
    if volume and depth and spread:
        liquidity_score = _volume_liquidity_score(volume[-1][1], depth[-1][1], spread[-1][1])
        out.append(_rollup_feature(asset=asset, name="volume_liquidity_score", value=liquidity_score, computed_ts_ms=cutoff))
    return out


def _series(features: list[FeatureValue], *, asset: str, feature_name: str, as_of_ms: int) -> list[tuple[int, float]]:
    points = [
        (item.computed_ts_ms, float(item.scalar_value))
        for item in features
        if item.asset == asset and item.feature_name == feature_name and item.scalar_value is not None and item.computed_ts_ms <= as_of_ms
    ]
    return sorted(points, key=lambda item: item[0])


def _baseline(series: list[tuple[int, float]], target_ts_ms: int) -> float | None:
    before = [value for ts, value in series if ts <= target_ts_ms]
    if before:
        return before[-1]
    return series[0][1] if series else None


def _rollup_feature(*, asset: str, name: str, value: float, computed_ts_ms: int) -> FeatureValue:
    fid = "feat_" + hashlib.sha1(f"engine_rollup:{asset}:{name}:{computed_ts_ms}".encode()).hexdigest()[:24]
    return FeatureValue(
        feature_id=fid,
        asset=asset,
        feature_group="rollup",
        feature_name=name,
        value={name: value},
        scalar_value=value,
        received_ts_ms=computed_ts_ms,
        computed_ts_ms=computed_ts_ms,
        source="engine_rollup",
        version=FEATURE_VERSION,
        metadata={"deterministic": True},
    )


def percentile_rank(values: list[float], latest: float) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value <= latest) / len(values)


def zscore(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    sigma = statistics.pstdev(values)
    if sigma <= 0:
        return 0.0
    return (values[-1] - statistics.mean(values)) / sigma


def _volume_liquidity_score(day_volume_usd: float, top_depth_usd: float, spread_bps: float) -> float:
    volume_component = min(1.0, math.log10(max(day_volume_usd, 0.0) + 1.0) / 9.0)
    depth_component = min(1.0, math.log10(max(top_depth_usd, 0.0) + 1.0) / 7.0)
    spread_component = max(0.0, min(1.0, 1.0 - max(spread_bps, 0.0) / 25.0))
    return round(volume_component * 0.45 + depth_component * 0.4 + spread_component * 0.15, 4)


def _venue_float(venues: dict[str, Any], venue: str, *keys: str) -> float | None:
    raw = venues.get(venue) or venues.get(venue.lower()) or venues.get(venue.upper())
    if not isinstance(raw, dict):
        return None
    return _first_float(raw, *keys)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _bounded01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _level_px_sz(level: Any) -> tuple[float | None, float | None]:
    if isinstance(level, dict):
        return _float(level.get("px")), _float(level.get("sz"))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _float(level[0]), _float(level[1])
    return None, None
