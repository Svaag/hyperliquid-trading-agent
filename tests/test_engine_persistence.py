from __future__ import annotations

from pathlib import Path

from hyperliquid_trading_agent.app.engine.schemas import BanditRecommendation


def test_engine_strategy_regime_council_learning_migration_declares_tables():
    text = Path("alembic/versions/0019_engine_strategy_regime_council_learning.py").read_text()

    assert 'down_revision = "0018_liquidations"' in text
    for table in [
        "strategy_specs",
        "strategy_regime_performance",
        "allocation_diversity_events",
        "candidate_trade_packets",
        "council_reviews",
        "council_votes",
        "bandit_policy_snapshots",
        "bandit_recommendations",
    ]:
        assert f'"{table}"' in text


def test_bandit_recommendations_are_report_only_contracts():
    recommendation = BanditRecommendation(
        recommendation_id="rec_1",
        policy_id="policy_1",
        strategy_id="microstructure_ofi_v2",
        recommendation="increase observation weight only",
        confidence=0.5,
        created_at_ms=1,
    )

    assert recommendation.auto_apply_allowed is False
