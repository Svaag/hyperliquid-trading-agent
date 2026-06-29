from __future__ import annotations

import hashlib
import statistics
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import FeatureSnapshot, FeatureValue, NormalizedEvent

FEATURE_VERSION = "engine_features_v1"


class FeatureStore:
    """Point-in-time feature store with repository-backed persistence."""

    def __init__(self, repository: Any | None = None):
        self.repository = repository
        self._features: dict[str, FeatureValue] = {}

    async def record(self, feature: FeatureValue) -> FeatureValue:
        self._features[feature.feature_id] = feature
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_feature_value", None)
            if callable(record):
                await record(feature.model_dump(mode="json"))
        return feature

    async def features_for_event(self, event: NormalizedEvent) -> list[FeatureValue]:
        features = derive_features(event)
        for feature in features:
            await self.record(feature)
        return features

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
        items = [item for item in self._features.values() if item.asset == asset]
        if feature_name:
            items = [item for item in items if item.feature_name == feature_name]
        return sorted(items, key=lambda item: item.computed_ts_ms, reverse=True)[:limit]

    def snapshot(self, *, asset: str, as_of_ms: int | None = None, max_items: int = 200) -> FeatureSnapshot:
        asset = asset.upper()
        cutoff = as_of_ms or now_ms()
        latest_by_name: dict[str, FeatureValue] = {}
        for item in sorted(self._features.values(), key=lambda feature: feature.computed_ts_ms):
            if item.asset == asset and item.computed_ts_ms <= cutoff:
                latest_by_name[item.feature_name] = item
        selected = list(latest_by_name.values())[-max_items:]
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


def derive_features(event: NormalizedEvent) -> list[FeatureValue]:
    if event.event_type in {"all_mids", "mid", "price"}:
        return _price_features(event)
    if event.event_type in {"l2_book", "l2Book", "orderbook"}:
        return _orderbook_features(event)
    if event.event_type in {"news", "newswire"}:
        return _news_features(event)
    if event.event_type in {"asset_ctx", "meta_and_asset_ctxs", "funding_oi"}:
        return _funding_oi_features(event)
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


def _feature(event: NormalizedEvent, *, asset: str, group: str, name: str, value: dict[str, Any], scalar: float | None = None, quality: float | None = None) -> FeatureValue:
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
        metadata=dict(event.metadata or {}),
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
    importance = _float(event.payload.get("importance_score")) or _float(event.payload.get("importance")) or 0.0
    sentiment = str(event.payload.get("sentiment") or "unknown")
    direction = 1.0 if sentiment == "bullish" else -1.0 if sentiment == "bearish" else 0.0
    out: list[FeatureValue] = []
    for symbol in event.symbols:
        out.append(
            _feature(
                event,
                asset=symbol,
                group="news",
                name="catalyst_pressure",
                value={"importance_score": importance, "sentiment": sentiment, "pressure": direction * importance / 100.0},
                scalar=direction * importance / 100.0,
            )
        )
    return out


def _funding_oi_features(event: NormalizedEvent) -> list[FeatureValue]:
    symbol = (event.symbols[0] if event.symbols else str(event.payload.get("coin") or event.payload.get("symbol") or "")).upper()
    if not symbol:
        return []
    out: list[FeatureValue] = []
    funding = _float(event.payload.get("funding") or event.payload.get("funding_hourly"))
    oi = _float(event.payload.get("openInterest") or event.payload.get("open_interest"))
    if funding is not None:
        out.append(_feature(event, asset=symbol, group="funding_oi", name="funding_hourly", value={"funding_hourly": funding}, scalar=funding))
    if oi is not None:
        out.append(_feature(event, asset=symbol, group="funding_oi", name="open_interest", value={"open_interest": oi}, scalar=oi))
    return out


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


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _level_px_sz(level: Any) -> tuple[float | None, float | None]:
    if isinstance(level, dict):
        return _float(level.get("px")), _float(level.get("sz"))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _float(level[0]), _float(level[1])
    return None, None
