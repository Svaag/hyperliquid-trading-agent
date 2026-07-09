from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.engine.alpha.directional import DirectionalMomentumStrategy
from hyperliquid_trading_agent.app.engine.candidate_book import CandidateBook
from hyperliquid_trading_agent.app.engine.debate_adjudicator import EvidencePackBuilder, debate_priority
from hyperliquid_trading_agent.app.engine.event_ledger import EventLedger
from hyperliquid_trading_agent.app.engine.execution import ExecutionGateway
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.portfolio_allocator import PortfolioAllocator
from hyperliquid_trading_agent.app.engine.position_manager import PositionManager
from hyperliquid_trading_agent.app.engine.regime import RegimeEngine
from hyperliquid_trading_agent.app.engine.scorer import EVScorerService


def test_event_feature_regime_candidate_ev_allocation_execution_pipeline():
    async def run():
        ledger = EventLedger()
        store = FeatureStore()
        price_events = []
        for idx, px in enumerate([100, 101, 102, 103, 104], start=1):
            event = await ledger.normalize_and_record(
                event_type="all_mids",
                source="hyperliquid",
                provider="test",
                payload={"BTC": px},
                asset_class="crypto",
                symbols=["BTC"],
                received_ts_ms=idx * 100,
            )
            price_events.append(event)
            await store.features_for_event(event)
        book_event = await ledger.normalize_and_record(
            event_type="l2_book",
            source="hyperliquid",
            provider="test",
            payload={"coin": "BTC", "levels": [[[103.9, 10]], [[104.1, 8]]]},
            asset_class="crypto",
            symbols=["BTC"],
            received_ts_ms=600,
        )
        await store.features_for_event(book_event)

        features = await store.latest(asset="BTC", limit=50)
        regime = RegimeEngine().compute(features, primary_asset="BTC")
        snapshot = store.snapshot(asset="BTC")
        candidates = DirectionalMomentumStrategy().generate(snapshot, regime, timestamp_ms=1_000)
        assert candidates

        scorer = EVScorerService()
        ev = await scorer.score(candidates[0], regime)
        assert ev.model_version_id == "deterministic_fallback_v1"

        allocator = PortfolioAllocator(min_net_ev_bps=-100, min_risk_adjusted_utility=-100)
        allocation = await allocator.allocate(candidates[0], ev, regime=regime, portfolio_state={"equity_usd": 100_000})
        assert allocation.status == "allocate"
        assert allocation.allocated_notional_usd > 0

        book = CandidateBook()
        await book.add_many(candidates)
        book_snapshot = await book.snapshot({ev.candidate_id: ev}, as_of_ms=1_000)
        assert book_snapshot.ranked_candidate_ids == [candidates[0].candidate_id]

        priority = debate_priority(candidates[0], ev, allocation, regime, portfolio_equity=100_000)
        assert priority >= 0

        pack = EvidencePackBuilder().build(
            candidates[0],
            ev,
            allocation,
            regime,
            feature_snapshot=snapshot.features,
        )
        assert pack.proposed_trade_plan["exchange_actions"] == []

        from hyperliquid_trading_agent.app.engine.schemas import OrderIntent

        intent = OrderIntent(
            intent_id="intent_test",
            parent_candidate_id=candidates[0].candidate_id,
            portfolio_decision_id=allocation.allocation_id,
            asset="BTC",
            venue="hyperliquid",
            side="buy",
            order_type="marketable_limit",
            time_in_force="ioc",
            target_size=allocation.allocated_size,
            target_notional_usd=allocation.allocated_notional_usd,
            max_slippage_bps=5,
            price_limit=candidates[0].proposed_entry,
            reduce_only=False,
            post_only=False,
            deadline_ts_ms=2_000,
            strategy_id=candidates[0].strategy_id,
            model_version_id=ev.model_version_id,
            config_version_id="cfg_test",
            risk_budget_id="risk_test",
            execution_mode="paper",
            created_at_ms=1_000,
        )
        report = await ExecutionGateway().submit(intent)
        assert report.status == "filled"
        assert report.execution_mode == "paper"

        position = await PositionManager().open_from_execution(candidates[0], report)
        assert position is not None
        assert position.position_state == "open"

    anyio.run(run)


def test_feature_store_bounds_series_and_filters_universe():
    from hyperliquid_trading_agent.app.engine.schemas import FeatureValue

    def _scalar(name: str, value: float, ts: int, asset: str = "BTC") -> FeatureValue:
        return FeatureValue(
            feature_id=f"feat_{asset}_{name}_{ts}",
            asset=asset,
            feature_group="test",
            feature_name=name,
            value={name: value},
            scalar_value=value,
            received_ts_ms=ts,
            computed_ts_ms=ts,
            source="test",
            version="test_v1",
        )

    async def run():
        ledger = EventLedger()
        store = FeatureStore(max_age_seconds=600, max_points_per_series=16)

        # Age eviction: points older than max-age relative to the newest fall off.
        for ts in (0, 100_000, 400_000, 900_000):
            await store.record(_scalar("mid", 100 + ts / 100_000, ts))
        series = store._series[("BTC", "mid")]
        assert [point[0] for point in series] == [400_000, 900_000]

        # Length cap (constructor clamps the cap to a floor of 16).
        for idx in range(24):
            await store.record(_scalar("spread_bps", 4 + idx, 1_000_000 + idx))
        assert len(store._series[("BTC", "spread_bps")]) == 16

        # Snapshot rewinds scalar series for historical cutoffs.
        snapshot = store.snapshot(asset="BTC", as_of_ms=400_000)
        assert snapshot.features["mid"] == 104.0

        # Universe filter: meta event payload covers many assets, but only the
        # event's declared symbols are recorded.
        meta_event = await ledger.normalize_and_record(
            event_type="meta_and_asset_ctxs",
            source="hyperliquid",
            provider="test",
            payload={
                "meta": {"universe": [{"name": "BTC"}, {"name": "DOGE"}, {"name": "PEPE"}]},
                "asset_ctxs": [
                    {"funding": "0.0001", "openInterest": "1"},
                    {"funding": "0.0002", "openInterest": "2"},
                    {"funding": "0.0003", "openInterest": "3"},
                ],
            },
            asset_class="crypto",
            symbols=["BTC"],
            received_ts_ms=2_000_000,
        )
        recorded = await store.features_for_event(meta_event)
        assert {feature.asset for feature in recorded} == {"BTC"}
        assert "DOGE" not in store._latest

        full_store = FeatureStore(full_universe_enabled=True)
        recorded_full = await full_store.features_for_event(meta_event)
        assert {"BTC", "DOGE", "PEPE"} <= {feature.asset for feature in recorded_full}

    anyio.run(run)
