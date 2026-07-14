from __future__ import annotations

from pathlib import Path

import anyio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hyperliquid_trading_agent.app.db.models import Base
from hyperliquid_trading_agent.app.db.repository import Repository
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


def test_operational_incident_and_newswire_repair_migrations_are_chained():
    incidents = Path("alembic/versions/0031_operational_incidents.py").read_text()
    repair = Path("alembic/versions/0032_repair_newswire_symbol_reasons.py").read_text()

    assert 'revision = "0031_operational_incidents"' in incidents
    assert 'down_revision = "0030_canonical_market_universe"' in incidents
    assert '"operational_incidents"' in incidents
    assert 'revision = "0032_repair_newswire_reasons"' in repair
    assert 'down_revision = "0031_operational_incidents"' in repair
    assert "symbol_match_reasons" in repair


def test_exact_readiness_aggregates_execute_against_repository_schema(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'readiness.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        repository = Repository(async_sessionmaker(engine, expire_on_commit=False))
        await repository.record_alpha_candidate(
            {
                "candidate_id": "candidate_1",
                "strategy_id": "strategy_1",
                "strategy_version": "v1",
                "strategy_family": "momentum",
                "asset": "BTC",
                "asset_class": "crypto",
                "venue": "hyperliquid",
                "side": "long",
                "horizon": "5m",
                "proposed_entry": 100.0,
                "stop": 99.0,
                "feature_snapshot_id": "feature_1",
                "regime_snapshot_id": "regime_1",
                "feature_coverage_pct": 100.0,
                "source_integrity": {"paper_eligible": True, "activation_scope": "paper_shadow"},
                "created_at_ms": 1_000,
                "expires_at_ms": 10_000,
            }
        )

        aggregates = await repository.get_engine_readiness_aggregates(start_ms=500, end_ms=20_000)
        await engine.dispose()

        assert aggregates["counts"]["candidate_count"] == 1
        assert aggregates["coverage"]["candidate_strategy_metadata_covered_count"] == 1
        assert aggregates["breadth"]["raw_paper_eligible_strategies"] == ["strategy_1"]
        assert aggregates["window"]["semantics"] == "[start_ms,end_ms)"

    anyio.run(run)


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
