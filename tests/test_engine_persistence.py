from __future__ import annotations

from pathlib import Path

from hyperliquid_trading_agent.app.engine.schemas import BanditRecommendation


def test_engine_strategy_regime_council_learning_migration_declares_tables():
    text = Path("alembic/versions/0019_engine_strategy_regime_council_learning.py").read_text()

    assert 'revision = "0019_strategy_regime_council"' in text
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


def test_engine_candidate_outcome_evidence_spine_migration_declares_tables():
    text = Path("alembic/versions/0020_engine_candidate_outcome_evidence_spine.py").read_text()

    assert 'revision = "0020_candidate_outcome_spine"' in text
    assert 'down_revision = "0019_strategy_regime_council"' in text
    for table in [
        "candidate_evidence_links",
        "candidate_outcome_attributions",
        "replay_result_links",
        "portfolio_concentration_events",
    ]:
        assert f'"{table}"' in text
    assert "outcome_window" in text
    assert "strategy_regime_performance" in text


def test_canonical_market_universe_migration_declares_identity_and_backfill():
    text = Path("alembic/versions/0030_canonical_market_universe.py").read_text()

    assert 'revision = "0030_canonical_market_universe"' in text
    assert 'down_revision = "0029_engine_strategy_evaluations"' in text
    for table in [
        "instrument_registry",
        "watchlist_memberships",
        "watchlist_change_events",
        "universe_snapshots",
        "venue_market_snapshots",
        "cross_venue_feature_snapshots",
    ]:
        assert f'"{table}"' in text
    for identity_column in ["instrument_id", "underlying_id", "venue_id", "provider_symbol"]:
        assert identity_column in text
    assert "_backfill_legacy_instrument_identity" in text
    assert "pre_0030" in text


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
