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
