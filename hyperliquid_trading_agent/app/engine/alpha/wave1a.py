from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.alpha.base import (
    CORE_CRYPTO_ASSETS,
    HYPERLIQUID_VENUES,
    candidate_contract_fields,
)
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, FeatureSnapshot, RegimeVector, StrategySpec


class MicrostructureOFIV2Strategy:
    spec = StrategySpec(
        strategy_id="microstructure_ofi_v2",
        version="2.0.0",
        family="microstructure_orderflow",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["3m", "5m"],
        required_features=["mid", "realized_vol_5m_bps", "spread_bps", "top_imbalance"],
        valid_regimes=["balanced", "buy_pressure", "sell_pressure"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=45_000,
        min_confidence=0.30,
        min_ev_bps=8.0,
        risk_tags=["microstructure", "ofi", "short_horizon"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if regime.spread_state == "wide" or regime.liquidity_state == "impaired":
            return []
        mid = _float(snapshot.features.get("mid"))
        spread = _float(snapshot.features.get("spread_bps"))
        imbalance = _float(snapshot.features.get("top_imbalance"))
        vol_5m = _float(snapshot.features.get("realized_vol_5m_bps"))
        if mid is None or mid <= 0 or spread is None or imbalance is None or vol_5m is None:
            return []
        if spread > 15 or abs(imbalance) < 0.25 or vol_5m > 120:
            return []
        side = "long" if imbalance > 0 else "short"
        stop_bps = max(12.0, min(35.0, vol_5m * 0.45))
        stop, target = _stop_target(mid, side, stop_bps=stop_bps, rr=1.45)
        score = min(100.0, 42.0 + abs(imbalance) * 42.0 + max(0.0, 15 - spread) + max(0.0, 80 - vol_5m) / 8.0)
        return [
            _candidate(
                self.spec,
                snapshot,
                regime,
                timestamp_ms=timestamp_ms,
                side=side,
                horizon="5m",
                entry=mid,
                stop=stop,
                target=target,
                score=score,
                confidence=min(0.86, 0.28 + score / 175.0),
                thesis=f"{snapshot.asset} {side} OFI v2: top-book imbalance {imbalance:.2f}, spread {spread:.1f} bps, vol {vol_5m:.1f} bps.",
                invalidation=["OFI imbalance mean-reverts", "Spread widens above 15 bps", f"Price trades through {stop:.6g}"],
                metadata={"top_imbalance": imbalance, "spread_bps": spread, "realized_vol_5m_bps": vol_5m},
                expected_edge_bps=max(0.0, score - 45.0) / 3.0,
            )
        ]


class LiquidationCascadeStrategy:
    spec = StrategySpec(
        strategy_id="liquidation_cascade_v1",
        version="1.0.0",
        family="liquidation_pressure",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["5m", "15m"],
        required_features=["liq_notional_5m", "long_vs_short_liq_imbalance_5m", "mid", "spread_bps"],
        valid_regimes=["long_flush", "mixed", "short_squeeze"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=120_000,
        min_confidence=0.35,
        min_ev_bps=10.0,
        risk_tags=["cascade", "liquidation", "momentum"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if regime.liquidation_state not in {"long_flush", "short_squeeze", "mixed"}:
            return []
        mid = _float(snapshot.features.get("mid"))
        spread = _float(snapshot.features.get("spread_bps"))
        notional = _float(snapshot.features.get("liq_notional_5m"))
        imbalance = _float(snapshot.features.get("long_vs_short_liq_imbalance_5m"))
        confirmed = _float(snapshot.features.get("confirmed_only_liq_score_5m")) or 0.0
        top_imbalance = _float(snapshot.features.get("top_imbalance")) or 0.0
        if mid is None or mid <= 0 or spread is None or notional is None or imbalance is None:
            return []
        if spread > 20 or notional < 150_000 or abs(imbalance) < 75_000 or confirmed < 0.15:
            return []
        side = "short" if imbalance > 0 else "long"
        if (side == "short" and top_imbalance > 0.15) or (side == "long" and top_imbalance < -0.15):
            return []
        pressure = min(1.0, abs(imbalance) / max(notional, 1.0))
        stop, target = _stop_target(mid, side, stop_bps=32.0, rr=1.8)
        score = min(100.0, 50.0 + pressure * 25.0 + min(20.0, notional / 100_000) + confirmed * 10.0)
        return [
            _candidate(
                self.spec,
                snapshot,
                regime,
                timestamp_ms=timestamp_ms,
                side=side,
                horizon="15m",
                entry=mid,
                stop=stop,
                target=target,
                score=score,
                confidence=min(0.9, 0.35 + score / 170.0),
                thesis=f"{snapshot.asset} {side} liquidation cascade: 5m liq notional ${notional:,.0f}, imbalance ${imbalance:,.0f}.",
                invalidation=["Liquidation pressure fades", "Orderflow flips against cascade", f"Price trades through {stop:.6g}"],
                metadata={"liq_notional_5m": notional, "long_vs_short_liq_imbalance_5m": imbalance, "confirmed_only_liq_score_5m": confirmed, "top_imbalance": top_imbalance},
                expected_edge_bps=max(0.0, score - 50.0) / 2.0,
            )
        ]


class LiquidationMeanRevertStrategy:
    spec = StrategySpec(
        strategy_id="liquidation_mean_revert_v1",
        version="1.0.0",
        family="liquidation_pressure",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m"],
        required_features=["largest_single_liq_5m", "liq_notional_5m", "mid", "spread_bps"],
        valid_regimes=["long_flush", "mixed", "short_squeeze"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=300_000,
        min_confidence=0.35,
        min_ev_bps=8.0,
        risk_tags=["flush_exhaustion", "liquidation", "mean_reversion"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        if regime.liquidation_state not in {"long_flush", "short_squeeze", "mixed"}:
            return []
        mid = _float(snapshot.features.get("mid"))
        spread = _float(snapshot.features.get("spread_bps"))
        notional = _float(snapshot.features.get("liq_notional_5m"))
        largest = _float(snapshot.features.get("largest_single_liq_5m")) or 0.0
        imbalance = _float(snapshot.features.get("long_vs_short_liq_imbalance_5m")) or 0.0
        top_imbalance = _float(snapshot.features.get("top_imbalance")) or 0.0
        if mid is None or mid <= 0 or spread is None or notional is None:
            return []
        if spread > 18 or notional < 120_000 or largest < 25_000:
            return []
        side = "long" if imbalance > 0 else "short"
        if (side == "long" and top_imbalance < -0.25) or (side == "short" and top_imbalance > 0.25):
            return []
        stop, target = _stop_target(mid, side, stop_bps=38.0, rr=1.55)
        exhaustion = min(1.0, largest / max(notional, 1.0) + (0.2 if abs(top_imbalance) < 0.2 else 0.0))
        score = min(100.0, 48.0 + exhaustion * 30.0 + min(15.0, notional / 150_000))
        return [
            _candidate(
                self.spec,
                snapshot,
                regime,
                timestamp_ms=timestamp_ms,
                side=side,
                horizon="15m",
                entry=mid,
                stop=stop,
                target=target,
                score=score,
                confidence=min(0.86, 0.32 + score / 180.0),
                thesis=f"{snapshot.asset} {side} post-liquidation mean reversion after ${notional:,.0f} 5m flush.",
                invalidation=["Fresh liquidation wave resumes", "Spread/liquidity degrades", f"Price trades through {stop:.6g}"],
                metadata={"liq_notional_5m": notional, "largest_single_liq_5m": largest, "long_vs_short_liq_imbalance_5m": imbalance, "top_imbalance": top_imbalance},
                expected_edge_bps=max(0.0, score - 48.0) / 2.5,
            )
        ]


class FundingCarryStrategy:
    spec = StrategySpec(
        strategy_id="funding_carry_v1",
        version="1.0.0",
        family="funding_basis",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["1h", "4h"],
        required_features=["funding_hourly", "mid", "realized_vol_15m_bps"],
        valid_regimes=["negative_extreme", "neutral", "positive_extreme"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=900_000,
        min_confidence=0.40,
        min_ev_bps=8.0,
        risk_tags=["basis", "carry", "funding"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        mid = _float(snapshot.features.get("mid"))
        funding = _float(snapshot.features.get("funding_hourly"))
        vol_15m = _float(snapshot.features.get("realized_vol_15m_bps"))
        if mid is None or mid <= 0 or funding is None or vol_15m is None:
            return []
        if abs(funding) < 0.0001 or regime.volatility_state == "extreme" or vol_15m > 180:
            return []
        side = "short" if funding > 0 else "long"
        if side == "short" and regime.trend_state == "bull" and regime.trend_confidence > 0.75:
            return []
        if side == "long" and regime.trend_state == "bear" and regime.trend_confidence > 0.75:
            return []
        stop, target = _stop_target(mid, side, stop_bps=max(45.0, vol_15m * 0.7), rr=1.35)
        funding_edge_bps = abs(funding) * 10_000 * 8.0
        score = min(100.0, 45.0 + funding_edge_bps * 1.5 + max(0.0, 120 - vol_15m) / 6.0 + regime.regime_stability_score * 15.0)
        return [
            _candidate(
                self.spec,
                snapshot,
                regime,
                timestamp_ms=timestamp_ms,
                side=side,
                horizon="1h",
                entry=mid,
                stop=stop,
                target=target,
                score=score,
                confidence=min(0.88, 0.34 + score / 180.0),
                thesis=f"{snapshot.asset} {side} funding carry: hourly funding {funding:.5f}, volatility {vol_15m:.1f} bps.",
                invalidation=["Funding normalizes", "Directional trend risk overwhelms carry", f"Price trades through {stop:.6g}"],
                metadata={"funding_hourly": funding, "realized_vol_15m_bps": vol_15m, "expected_funding_cost_bps": -funding_edge_bps},
                expected_edge_bps=funding_edge_bps,
            )
        ]


class OIBreakoutStrategy:
    spec = StrategySpec(
        strategy_id="oi_breakout_v1",
        version="1.0.0",
        family="trend_following",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m", "30m"],
        required_features=["mid", "mid_return_5m_bps", "oi_delta_5m_pct", "spread_bps"],
        valid_regimes=["bear", "bull", "expanding"],
        max_candidates_per_run=1,
        max_allocation_share_pct=45.0,
        cooldown_ms=300_000,
        min_confidence=0.35,
        min_ev_bps=8.0,
        risk_tags=["breakout", "open_interest", "trend"],
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        mid = _float(snapshot.features.get("mid"))
        ret_5m = _float(snapshot.features.get("mid_return_5m_bps"))
        oi_delta = _float(snapshot.features.get("oi_delta_5m_pct"))
        spread = _float(snapshot.features.get("spread_bps"))
        if mid is None or mid <= 0 or ret_5m is None or oi_delta is None or spread is None:
            return []
        if spread > 18 or oi_delta < 2.0 or abs(ret_5m) < 25.0:
            return []
        if regime.oi_state not in {"expanding", "unknown"} and oi_delta < 5.0:
            return []
        side = "long" if ret_5m > 0 else "short"
        if side == "long" and regime.trend_state == "bear" and regime.trend_confidence > 0.7:
            return []
        if side == "short" and regime.trend_state == "bull" and regime.trend_confidence > 0.7:
            return []
        stop, target = _stop_target(mid, side, stop_bps=max(35.0, abs(ret_5m) * 0.6), rr=1.75)
        score = min(100.0, 46.0 + min(25.0, abs(ret_5m) / 3.0) + min(20.0, oi_delta * 2.0) + max(0.0, 18 - spread))
        return [
            _candidate(
                self.spec,
                snapshot,
                regime,
                timestamp_ms=timestamp_ms,
                side=side,
                horizon="30m",
                entry=mid,
                stop=stop,
                target=target,
                score=score,
                confidence=min(0.9, 0.34 + score / 175.0),
                thesis=f"{snapshot.asset} {side} OI breakout: 5m return {ret_5m:.1f} bps with OI +{oi_delta:.1f}%.",
                invalidation=["OI expansion stalls", "Breakout level fails", f"Price trades through {stop:.6g}"],
                metadata={"mid_return_5m_bps": ret_5m, "oi_delta_5m_pct": oi_delta, "spread_bps": spread},
                expected_edge_bps=max(0.0, score - 46.0) / 2.5,
            )
        ]


class LegacySignalAdapterStrategy:
    spec = StrategySpec(
        strategy_id="legacy_signal_adapter_v1",
        version="1.0.0",
        family="legacy_bridge",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["15m", "30m", "1h"],
        required_features=["mid"],
        valid_regimes=["bear", "bull", "range", "unknown"],
        max_candidates_per_run=5,
        max_allocation_share_pct=25.0,
        cooldown_ms=60_000,
        min_confidence=0.30,
        min_ev_bps=8.0,
        risk_tags=["adapter", "legacy"],
        counts_for_breadth=False,
    )
    strategy_id = spec.strategy_id

    def __init__(self, signals: list[dict[str, Any]] | None = None):
        self._signals = signals or []

    async def refresh_from_repository(self, repository: Any | None, *, now_ms: int, limit: int = 50) -> None:
        if repository is None or not getattr(repository, "enabled", False):
            return
        list_signals = getattr(repository, "list_autonomy_trade_signals", None)
        if not callable(list_signals):
            return
        rows = await list_signals(status="candidate", limit=limit)
        self._signals = [row for row in rows if int(row.get("expires_at_ms") or 0) > now_ms]

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        out: list[AlphaCandidate] = []
        seen_dedupe_keys: set[str] = set()
        mid = _float(snapshot.features.get("mid"))
        for signal in self._signals:
            symbol = str(signal.get("symbol") or signal.get("asset") or "").upper()
            if symbol != snapshot.asset:
                continue
            if int(signal.get("expires_at_ms") or 0) <= timestamp_ms:
                continue
            side = str(signal.get("side") or "").lower()
            if side not in {"long", "short"}:
                continue
            entry = _float(signal.get("entry") or signal.get("entry_px")) or mid
            stop = _float(signal.get("stop") or signal.get("stop_px"))
            target = _float(signal.get("take_profit") or signal.get("take_profit_px"))
            if entry is None or entry <= 0 or stop is None or stop <= 0:
                continue
            if target is None or target <= 0:
                target = entry + abs(entry - stop) * (1.5 if side == "long" else -1.5)
            if target <= 0:
                continue
            signal_id = str(signal.get("id") or signal.get("signal_id") or "unknown")
            horizon = str((signal.get("metadata") or {}).get("horizon") or signal.get("horizon") or "30m")
            signal_type = str(signal.get("signal_type") or "legacy")
            dedupe_key = f"{signal_id}:{snapshot.asset}:{side}:{signal_type}:{horizon}"
            if dedupe_key in seen_dedupe_keys:
                continue
            seen_dedupe_keys.add(dedupe_key)
            cid = "cand_" + hashlib.sha1(f"{self.strategy_id}:{dedupe_key}".encode()).hexdigest()[:24]
            score = float(signal.get("score") or 0.0)
            confidence = float(signal.get("confidence") or 0.0)
            contract_fields = candidate_contract_fields(self.spec, snapshot, expected_edge_bps=max(0.0, score - 50.0) / 3.0)
            contract_fields["source_integrity"] = {**contract_fields.get("source_integrity", {}), "legacy_signal_id": signal_id, "adapter": self.strategy_id, "dedupe_key": dedupe_key}
            out.append(
                AlphaCandidate(
                    candidate_id=cid,
                    strategy_id=self.strategy_id,
                    **contract_fields,
                    asset=snapshot.asset,
                    asset_class=str((signal.get("metadata") or {}).get("asset_class") or "crypto"),  # type: ignore[arg-type]
                    venue=str((signal.get("metadata") or {}).get("venue") or "hyperliquid"),
                    side=side,  # type: ignore[arg-type]
                    horizon=horizon,
                    proposed_entry=entry,
                    stop=stop,
                    targets=[target],
                    thesis=str(signal.get("thesis") or f"Legacy autonomy signal {signal_id}"),
                    invalidation_conditions=[str(signal.get("invalidation") or "Legacy signal invalidation triggered")],
                    feature_snapshot_id=snapshot.snapshot_id,
                    regime_snapshot_id=regime.regime_snapshot_id,
                    source_event_ids=[],
                    raw_alpha_score=max(0.0, min(100.0, score)),
                    confidence=max(0.0, min(1.0, confidence)),
                    created_at_ms=timestamp_ms,
                    expires_at_ms=min(int(signal.get("expires_at_ms") or timestamp_ms + 30 * 60_000), timestamp_ms + 60 * 60_000),
                    metadata={"legacy_signal_id": signal_id, "legacy_signal_type": signal_type, "dedupe_key": dedupe_key, "regime_label": regime.regime_label},
                )
            )
            if len(out) >= self.spec.max_candidates_per_run:
                break
        return out


class RegimeDefensiveFlatStrategy:
    spec = StrategySpec(
        strategy_id="regime_defensive_flat_v1",
        version="1.0.0",
        family="risk_off_defensive",
        supported_assets=CORE_CRYPTO_ASSETS,
        supported_venues=HYPERLIQUID_VENUES,
        supported_horizons=["loop"],
        required_features=["mid"],
        valid_regimes=["extreme", "impaired", "risk_off"],
        max_candidates_per_run=3,
        max_allocation_share_pct=0.0,
        cooldown_ms=60_000,
        min_confidence=0.0,
        min_ev_bps=0.0,
        risk_tags=["defensive", "flat", "risk_off"],
        counts_for_breadth=False,
    )
    strategy_id = spec.strategy_id

    def generate(self, snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]:
        mid = _float(snapshot.features.get("mid"))
        if mid is None or mid <= 0:
            return []
        risk_off = regime.volatility_state == "extreme" or regime.liquidity_state == "impaired" or regime.spread_state == "wide" or regime.correlation_state == "breakdown"
        if not risk_off:
            return []
        score = 25.0 + (25.0 if regime.volatility_state == "extreme" else 0.0) + (25.0 if regime.liquidity_state == "impaired" or regime.spread_state == "wide" else 0.0)
        cid = "cand_" + hashlib.sha1(f"{self.strategy_id}:{snapshot.asset}:{regime.regime_snapshot_id}:{timestamp_ms // 60_000}".encode()).hexdigest()[:24]
        contract_fields = candidate_contract_fields(self.spec, snapshot, expected_edge_bps=0.0)
        contract_fields["source_integrity"] = {**contract_fields.get("source_integrity", {}), "policy": self.strategy_id, "execution_authority": "none"}
        return [
            AlphaCandidate(
                candidate_id=cid,
                strategy_id=self.strategy_id,
                **contract_fields,
                asset=snapshot.asset,
                asset_class="crypto",
                venue="hyperliquid",
                side="flat",
                horizon="loop",
                proposed_entry=mid,
                stop=mid,
                targets=[mid],
                thesis=f"Defensive flat candidate for {snapshot.asset}: {regime.regime_label}",
                invalidation_conditions=[],
                feature_snapshot_id=snapshot.snapshot_id,
                regime_snapshot_id=regime.regime_snapshot_id,
                source_event_ids=[],
                raw_alpha_score=min(100.0, score),
                confidence=1.0,
                created_at_ms=timestamp_ms,
                expires_at_ms=timestamp_ms + 5 * 60_000,
                metadata={"regime_label": regime.regime_label, "defensive_reason": "risk_off_regime"},
                portfolio_concentration_impact={"opens_position": False, "target_notional_usd": 0.0},
            )
        ]


def wave_1a_strategy_instances() -> list[Any]:
    return [
        MicrostructureOFIV2Strategy(),
        LiquidationCascadeStrategy(),
        LiquidationMeanRevertStrategy(),
        FundingCarryStrategy(),
        OIBreakoutStrategy(),
        LegacySignalAdapterStrategy(),
        RegimeDefensiveFlatStrategy(),
    ]


def wave_1a_specs() -> list[StrategySpec]:
    return [strategy.spec for strategy in wave_1a_strategy_instances()]


def _candidate(
    spec: StrategySpec,
    snapshot: FeatureSnapshot,
    regime: RegimeVector,
    *,
    timestamp_ms: int,
    side: str,
    horizon: str,
    entry: float,
    stop: float,
    target: float,
    score: float,
    confidence: float,
    thesis: str,
    invalidation: list[str],
    metadata: dict[str, Any],
    expected_edge_bps: float,
) -> AlphaCandidate:
    cid = "cand_" + hashlib.sha1(f"{spec.strategy_id}:{snapshot.asset}:{side}:{timestamp_ms // 30_000}:{round(entry, 6)}".encode()).hexdigest()[:24]
    return AlphaCandidate(
        candidate_id=cid,
        strategy_id=spec.strategy_id,
        **candidate_contract_fields(spec, snapshot, expected_edge_bps=expected_edge_bps),
        asset=snapshot.asset,
        asset_class="crypto",
        venue="hyperliquid",
        side=side,  # type: ignore[arg-type]
        horizon=horizon,
        proposed_entry=entry,
        stop=stop,
        targets=[max(target, 0.00000001)],
        thesis=thesis,
        invalidation_conditions=invalidation,
        feature_snapshot_id=snapshot.snapshot_id,
        regime_snapshot_id=regime.regime_snapshot_id,
        source_event_ids=[],
        raw_alpha_score=round(max(0.0, min(100.0, score)), 2),
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        created_at_ms=timestamp_ms,
        expires_at_ms=timestamp_ms + _horizon_ms(horizon),
        metadata={**metadata, "regime_label": regime.regime_label, "strategy_family": spec.family},
    )


def _stop_target(mid: float, side: str, *, stop_bps: float, rr: float) -> tuple[float, float]:
    stop_distance = mid * stop_bps / 10_000.0
    if side == "long":
        return mid - stop_distance, mid + stop_distance * rr
    return mid + stop_distance, mid - stop_distance * rr


def _horizon_ms(horizon: str) -> int:
    text = horizon.lower().strip()
    if text.endswith("m"):
        return int(float(text[:-1]) * 60_000)
    if text.endswith("h"):
        return int(float(text[:-1]) * 3_600_000)
    return 30 * 60_000


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
