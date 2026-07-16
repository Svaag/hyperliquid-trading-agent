from __future__ import annotations

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.execution import ExecutionCostService, ShadowAdapter
from hyperliquid_trading_agent.app.engine.position_manager import PositionManager
from hyperliquid_trading_agent.app.engine.promotion import StrategyPromotionPolicyService
from hyperliquid_trading_agent.app.engine.readiness import _strict_performance_groups
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, OrderIntent, StrategySpec
from hyperliquid_trading_agent.app.engine.scorer import ConservativeNoEdgeScorer, train_empirical_artifact
from hyperliquid_trading_agent.app.engine.strategy_research import RESEARCH_GRIDS, _matches_slice, _parameter_grid
from hyperliquid_trading_agent.app.engine.time_block_stats import non_overlapping_block_statistics
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report


def _candidate(*, strategy_version: str = "1.0.0", edge_bps: float = 75.0) -> AlphaCandidate:
    return AlphaCandidate(
        candidate_id=f"cand_hardening_{strategy_version}",
        strategy_id="test_strategy_v1",
        strategy_version=strategy_version,
        strategy_family="test_family",
        expected_edge_bps=edge_bps,
        feature_coverage_pct=100.0,
        asset="BTC",
        venue="hyperliquid",
        side="long",
        horizon="5m",
        proposed_entry=100.0,
        stop=99.0,
        targets=[102.0],
        thesis="Hardening test candidate.",
        invalidation_conditions=["test invalidation"],
        feature_snapshot_id="features_1",
        regime_snapshot_id="regime_1",
        raw_alpha_score=70.0,
        confidence=0.7,
        created_at_ms=1_000,
        expires_at_ms=601_000,
    )


def test_exact_current_versions_freeze_and_unseen_versions_fail_closed() -> None:
    async def run() -> tuple[StrategyPromotionPolicyService, AlphaCandidate]:
        service = StrategyPromotionPolicyService()
        v1 = StrategySpec(strategy_id="test_strategy_v1", version="1.0.0", family="test")
        v2 = StrategySpec(strategy_id="test_strategy_v1", version="2.0.0", family="test")
        await service.ensure_registry([v1])
        await service.ensure_registry([v1, v2])
        return service, service.apply(_candidate())

    service, candidate = anyio.run(run)

    assert service.get("test_strategy_v1", "1.0.0").state == "frozen"
    assert service.get("test_strategy_v1", "2.0.0").state == "research_only"
    assert service.get("test_strategy_v1", "3.0.0").reason_codes == ["missing_exact_version_policy"]
    assert candidate.source_integrity["promotion_state"] == "frozen"
    assert candidate.source_integrity["paper_eligible"] is False
    assert service.allocation_scope(candidate) == "research"


def test_future_or_expired_exact_version_policy_fails_closed() -> None:
    class PolicyRepository:
        enabled = True

        async def list_strategy_version_policies(self, limit: int) -> list[dict[str, object]]:
            assert limit == 10_000
            return [
                {
                    "strategy_version_key": "test_strategy_v1@1.0.0",
                    "strategy_id": "test_strategy_v1",
                    "strategy_version": "1.0.0",
                    "state": "paper_approved",
                    "reason_codes": ["external_approval"],
                    "effective_from_ms": 9_999_999_999_999,
                    "effective_until_ms": None,
                    "created_at_ms": 1,
                    "updated_at_ms": 1,
                    "metadata": {},
                }
            ]

    async def run() -> StrategyPromotionPolicyService:
        service = StrategyPromotionPolicyService(PolicyRepository())
        await service.ensure_registry(
            [StrategySpec(strategy_id="test_strategy_v1", version="1.0.0", family="test")]
        )
        return service

    service = anyio.run(run)

    policy = service.get("test_strategy_v1", "1.0.0")
    assert policy.state == "research_only"
    assert "strategy_version_policy_not_yet_effective" in policy.reason_codes


def test_depth_walk_uses_verified_fee_tier_and_shadow_fill_never_opens_position() -> None:
    class HyperliquidFees:
        async def user_fees(self, address: str) -> dict[str, str]:
            assert address == "0xfee-account"
            return {"userCrossRate": "0.00035"}

    async def run():
        settings = Settings(
            _env_file=None,
            environment="test",
            engine_execution_fee_account_address="0xfee-account",
            engine_execution_book_max_age_ms=1_000,
        )
        candidate = _candidate()
        service = ExecutionCostService(settings=settings, hyperliquid=HyperliquidFees())
        quote = await service.quote_candidate(
            candidate,
            requested_size=1.5,
            requested_notional_usd=150.0,
            order_book={
                "snapshot_id": "book_1",
                "time": 9_500,
                "levels": [
                    [[99.9, 2.0], [99.8, 3.0]],
                    [[100.1, 0.5], [100.2, 1.0]],
                ],
            },
            created_at_ms=10_000,
        )
        intent = OrderIntent(
            intent_id="intent_hardening",
            parent_candidate_id=candidate.candidate_id,
            portfolio_decision_id="alloc_1",
            asset="BTC",
            venue="hyperliquid",
            side="buy",
            order_type="marketable_limit",
            time_in_force="ioc",
            target_size=1.5,
            target_notional_usd=150.0,
            max_slippage_bps=50.0,
            strategy_id=candidate.strategy_id,
            model_version_id="model_1",
            config_version_id="config_1",
            risk_budget_id="risk_1",
            execution_mode="shadow",
            created_at_ms=10_000,
            deadline_ts_ms=20_000,
        )
        estimate = ConservativeNoEdgeScorer().score(candidate, cost_quote=quote)
        report = await ShadowAdapter().submit(intent, quote)
        position = await PositionManager().open_from_execution(candidate, report)
        return quote, estimate, report, position

    quote, estimate, report, position = anyio.run(run)

    assert quote.cost_quality == "measured"
    assert quote.fee_bps == 3.5
    assert quote.simulated_fill_size == 1.5
    assert quote.simulated_avg_fill_px is not None and quote.simulated_avg_fill_px > 100.1
    assert quote.market_impact_bps > 0
    assert quote.fee_schedule_id is not None and quote.fee_schedule_id.startswith("hyperliquid:user_fees:")
    assert round(
        estimate.expected_fee_bps
        + estimate.expected_spread_cost_bps
        + estimate.expected_slippage_bps
        + estimate.expected_market_impact_bps,
        8,
    ) == round(quote.total_execution_cost_bps, 8)
    assert estimate.net_ev_bps == -quote.total_execution_cost_bps
    assert report.status == "filled"
    assert report.assumptions["hypothetical"] is True
    assert report.execution_cost_quote_id == quote.quote_id
    assert position is None


def test_missing_order_book_timestamp_cannot_be_measured() -> None:
    class HyperliquidFees:
        async def user_fees(self, address: str) -> dict[str, str]:
            return {"userCrossRate": "0.00035"}

    async def run():
        settings = Settings(
            _env_file=None,
            environment="test",
            engine_execution_fee_account_address="0xfee-account",
        )
        return await ExecutionCostService(settings=settings, hyperliquid=HyperliquidFees()).quote_candidate(
            _candidate(),
            requested_size=1.0,
            requested_notional_usd=100.0,
            order_book={"levels": [[[99.9, 2.0], [99.8, 3.0]], [[100.1, 2.0], [100.2, 3.0]]]},
            created_at_ms=10_000,
        )

    quote = anyio.run(run)

    assert quote.cost_quality == "unavailable"
    assert "order_book_timestamp_unavailable" in quote.reason_codes


def test_non_overlapping_statistics_collapse_candidates_before_inference() -> None:
    rows = [
        {
            "candidate_horizon": "5m",
            "instrument_id": "BTC",
            "window_start_ms": 60_000,
            "window_end_ms": 360_000,
            "net_return_bps": 10.0,
        },
        {
            "candidate_horizon": "5m",
            "instrument_id": "BTC",
            "window_start_ms": 120_000,
            "window_end_ms": 420_000,
            "net_return_bps": 20.0,
        },
        {
            "candidate_horizon": "5m",
            "instrument_id": "ETH",
            "window_start_ms": 180_000,
            "window_end_ms": 480_000,
            "net_return_bps": 30.0,
        },
        {
            "candidate_horizon": "5m",
            "instrument_id": "BTC",
            "window_start_ms": 3_660_000,
            "window_end_ms": 3_960_000,
            "net_return_bps": 5.0,
        },
        {
            "candidate_horizon": "5m",
            "instrument_id": "BTC",
            "window_start_ms": 3_590_000,
            "window_end_ms": 3_610_000,
            "net_return_bps": 1_000.0,
        },
    ]

    stats = non_overlapping_block_statistics(rows, bootstrap_iterations=100)

    assert stats["raw_outcome_count"] == 5
    assert stats["effective_block_count"] == 2
    assert stats["included_instrument_block_count"] == 3
    assert stats["purged_cross_boundary_count"] == 1
    assert stats["mean"] == 13.75
    assert stats["descriptive_ci"] is False
    assert stats["promotion_eligible"] is False


def test_strict_performance_cannot_borrow_blocks_from_another_exact_version() -> None:
    def row(version: str, block: int) -> dict[str, object]:
        start = block * 3_600_000 + 60_000
        return {
            "candidate_id": f"cand_{version}_{block}",
            "strategy_id": "microstructure_ofi_v2",
            "strategy_version": version,
            "strategy_family": "microstructure_orderflow",
            "asset": "BTC",
            "instrument_id": "BTC",
            "side": "long",
            "candidate_horizon": "5m",
            "outcome_window": "5m",
            "gross_return_bps": 12.0,
            "net_return_bps": 10.0,
            "execution_adjusted_return_bps": 8.0,
            "execution_cost_quality": "measured",
            "execution_report_id": f"report_{version}_{block}",
            "realized_r": 0.5,
            "terminal_state": "matured",
            "window_start_ms": start,
            "window_end_ms": start + 300_000,
            "metadata": {"mark_source": "feature_store_mid"},
        }

    groups, exclusions = _strict_performance_groups(
        [row("1.0.0", block) for block in range(30)] + [row("2.0.0", 31)],
        bootstrap_iterations=100,
        min_promotion_blocks=30,
        allowed_strategy_versions={"microstructure_ofi_v2@2.0.0"},
    )

    assert [group["strategy_version_key"] for group in groups] == ["microstructure_ofi_v2@2.0.0"]
    assert groups[0]["non_overlapping_block_count"] == 1
    assert groups[0]["promotion_eligible"] is False
    assert exclusions["not_paper_approved_exact_version"] == 30


def test_empirical_training_uses_strict_native_horizon_block_outcomes() -> None:
    rows: list[dict[str, object]] = []
    for block_index, gross_return in enumerate((10.0, 10.0, 10.0, 10.0)):
        start = block_index * 3_600_000 + 60_000
        rows.append(
            {
                "candidate_id": f"cand_{block_index}",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_version": "2.0.0",
                "strategy_family": "microstructure_orderflow",
                "asset": "BTC",
                "asset_class": "crypto",
                "instrument_id": "BTC",
                "side": "long",
                "candidate_horizon": "5m",
                "outcome_window": "5m",
                "gross_return_bps": gross_return,
                "terminal_state": "matured",
                "window_start_ms": start,
                "window_end_ms": start + 300_000,
                "metadata": {"mark_source": "feature_store_mid", "regime_label": "range"},
            }
        )
    rows.append({**rows[0], "candidate_id": "cand_overlap", "gross_return_bps": 30.0})
    rows.append(
        {
            **rows[0],
            "candidate_id": "cand_non_strict",
            "gross_return_bps": 1_000.0,
            "window_start_ms": 4 * 3_600_000 + 60_000,
            "window_end_ms": 4 * 3_600_000 + 360_000,
            "metadata": {"mark_source": "latest_market_fallback"},
        }
    )

    artifact = train_empirical_artifact(rows, model_version_id="empirical_test", created_at_ms=123)

    assert artifact["global_bucket"]["count"] == 4
    assert artifact["global_bucket"]["mean_gross_return_bps"] == 12.5
    assert artifact["training_semantics"]["raw_strict_outcome_count"] == 5
    assert artifact["training_semantics"]["effective_training_block_count"] == 4
    assert artifact["training_semantics"]["strategy_supplied_edge_priors"] == "excluded"
    assert artifact["metrics"]["folds"] > 0


def test_research_grids_evaluate_predeclared_feature_slices() -> None:
    ofi_grid = _parameter_grid(RESEARCH_GRIDS["microstructure_ofi_v2"]["grid"])
    assert len(ofi_grid) == 12

    row = {
        "metadata": {
            "research_features": {
                "top_imbalance": -0.5,
                "top_depth_usd": 75_000,
                "spread_bps": 3.0,
            }
        }
    }
    assert _matches_slice(
        "microstructure_ofi_v2",
        row,
        {"min_abs_top_imbalance": 0.45, "min_depth_usd": 50_000, "max_spread_bps": 4.0},
    )
    assert not _matches_slice(
        "microstructure_ofi_v2",
        row,
        {"min_abs_top_imbalance": 0.6, "min_depth_usd": 50_000, "max_spread_bps": 4.0},
    )


def test_absorption_redesign_slice_requires_new_observed_features() -> None:
    parameters = {
        "min_abs_top_imbalance": 0.45,
        "min_depth_replenishment_rate": 0.1,
        "min_visible_depth_usd": 50_000,
        "max_abs_mid_return_5m_bps": 2.0,
    }
    assert not _matches_slice("microstructure_absorption_v1", {"metadata": {}}, parameters)
    assert _matches_slice(
        "microstructure_absorption_v1",
        {
            "metadata": {
                "research_features": {
                    "top_imbalance": -0.5,
                    "bid_depth_usd": 30_000,
                    "ask_depth_usd": 25_000,
                    "depth_replenishment_rate": 0.2,
                    "mid_return_5m_bps": 1.5,
                }
            }
        },
        parameters,
    )


def test_validation_report_uses_uncapped_execution_and_blocker_aggregates() -> None:
    class AggregateRepository:
        async def get_engine_validation_counts(self, **_: object) -> dict[str, object]:
            return {
                "execution_report_count": 800,
                "measured_execution_report_count": 600,
                "measured_slippage_total_bps": 1_200.0,
                "measured_fees_total_usd": 90.0,
                "execution_status_counts": {"filled": 700, "rejected": 100},
                "execution_cost_quality_counts": {"measured": 600, "configured_ceiling": 200},
                "risk_violation_counts": {
                    "strategy_version_not_approved": 25,
                    "execution_cost_not_measured": 10,
                },
                "allocation_scope_research_count": 80,
                "allocation_scope_research_allocated_count": 20,
                "allocation_scope_paper_eligible_count": 10,
                "allocation_scope_paper_eligible_allocated_count": 2,
                "allocation_scope_defensive_count": 5,
                "allocation_scope_defensive_allocated_count": 0,
                "allocation_scope_unknown_count": 0,
                "allocation_scope_unknown_allocated_count": 0,
            }

        def __getattr__(self, _name: str):
            async def empty(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
                return []

            return empty

    report = anyio.run(build_engine_validation_report, AggregateRepository())
    execution = report["execution_simulations"]

    assert execution["measurement_state"] == "measured"
    assert execution["measured_report_count"] == 600
    assert execution["avg_slippage_bps"] == 2.0
    assert execution["fees_usd"] == 90.0
    assert execution["status_counts"] == {"filled": 700, "rejected": 100}
    assert report["risk_rejects"]["hard_block_codes"] == [
        "execution_cost_not_measured",
        "strategy_version_not_approved",
    ]
    assert report["allocation_scope_semantics"]["scope_counts_are_detail_sample"] is False
