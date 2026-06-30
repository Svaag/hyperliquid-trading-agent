from __future__ import annotations

import hashlib
import statistics
from itertools import pairwise
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
        rollups: list[FeatureValue] = []
        for asset in sorted({feature.asset for feature in features}):
            rollups.extend(derive_rolling_features(asset=asset, features=list(self._features.values())))
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
    if event.event_type in {"liquidation_signal", "liquidation_features"}:
        return _liquidation_features(event)
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
    out: list[FeatureValue] = []
    for symbol, ctx in _iter_asset_contexts(event):
        funding = _float(ctx.get("funding") or ctx.get("funding_hourly"))
        oi = _float(ctx.get("openInterest") or ctx.get("open_interest"))
        day_volume = _float(ctx.get("dayNtlVlm") or ctx.get("day_volume_usd") or ctx.get("day_volume"))
        if funding is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="funding_hourly", value={"funding_hourly": funding}, scalar=funding))
        if oi is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="open_interest", value={"open_interest": oi}, scalar=oi))
        if day_volume is not None:
            out.append(_feature(event, asset=symbol, group="funding_oi", name="day_volume_usd", value={"day_volume_usd": day_volume}, scalar=day_volume))
    return out


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


def derive_rolling_features(*, asset: str, features: list[FeatureValue], as_of_ms: int | None = None) -> list[FeatureValue]:
    asset = asset.upper()
    cutoff = as_of_ms or max((item.computed_ts_ms for item in features if item.asset == asset), default=now_ms())
    mids = _series(features, asset=asset, feature_name="mid", as_of_ms=cutoff)
    oi = _series(features, asset=asset, feature_name="open_interest", as_of_ms=cutoff)
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
    if len(oi) >= 2:
        latest_ts, latest_oi = oi[-1]
        baseline = _baseline(oi, latest_ts - 300_000)
        if baseline is not None and baseline > 0:
            out.append(_rollup_feature(asset=asset, name="oi_delta_5m_pct", value=(latest_oi - baseline) / baseline * 100.0, computed_ts_ms=cutoff))
        changes = [cur - prev for prev, cur in pairwise([value for _, value in oi[-12:]])]
        velocity = zscore(changes)
        if velocity is not None:
            out.append(_rollup_feature(asset=asset, name="oi_velocity_z", value=velocity, computed_ts_ms=cutoff))
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
