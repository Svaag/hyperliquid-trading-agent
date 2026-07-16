"""Add canonical instruments, versioned watchlists, and venue snapshots.

Revision ID: 0030_canonical_market_universe
Revises: 0029_engine_strategy_evaluations
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030_canonical_market_universe"
down_revision = "0029_engine_strategy_evaluations"
branch_labels = None
depends_on = None


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'")


def upgrade() -> None:
    op.create_table(
        "instrument_registry",
        sa.Column("instrument_id", sa.String(length=64), primary_key=True),
        sa.Column("underlying_id", sa.String(length=128), nullable=False),
        sa.Column("venue_id", sa.String(length=96), nullable=False),
        sa.Column("provider_symbol", sa.String(length=128), nullable=False),
        sa.Column("display_symbol", sa.String(length=128), nullable=False),
        sa.Column("instrument_type", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("quote_currency", sa.String(length=32), nullable=False, server_default="USD"),
        sa.Column("session_timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("tradability_status", sa.String(length=32), nullable=False, server_default="absent"),
        sa.Column("capabilities_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("mapping_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("venue_id", "provider_symbol", name="uq_instrument_registry_venue_symbol"),
    )
    op.create_index("ix_instrument_registry_underlying", "instrument_registry", ["underlying_id"])
    op.create_index("ix_instrument_registry_venue_status", "instrument_registry", ["venue_id", "tradability_status"])

    op.create_table(
        "watchlist_memberships",
        sa.Column("membership_id", sa.String(length=96), primary_key=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False, server_default="pinned"),
        sa.Column("desired", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="admin"),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("instrument_id", name="uq_watchlist_memberships_instrument"),
    )
    op.create_index("ix_watchlist_memberships_tier_enabled", "watchlist_memberships", ["tier", "enabled"])

    op.create_table(
        "watchlist_change_events",
        sa.Column("change_id", sa.String(length=96), primary_key=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("before_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("after_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("result_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=128)),
        sa.Column("confirmed_at_ms", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_watchlist_change_events_status_created", "watchlist_change_events", ["status", "created_at_ms"])
    op.create_index("ix_watchlist_change_events_actor_created", "watchlist_change_events", ["actor", "created_at_ms"])

    op.create_table(
        "universe_snapshots",
        sa.Column("snapshot_id", sa.String(length=96), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("desired_instrument_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("active_instrument_ids_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=96), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_universe_snapshots_version", "universe_snapshots", ["version"])

    op.create_table(
        "venue_market_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), primary_key=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("underlying_id", sa.String(length=128), nullable=False),
        sa.Column("venue_id", sa.String(length=96), nullable=False),
        sa.Column("provider_symbol", sa.String(length=128), nullable=False),
        sa.Column("bid_px", sa.Float()),
        sa.Column("ask_px", sa.Float()),
        sa.Column("mid_px", sa.Float()),
        sa.Column("mark_px", sa.Float()),
        sa.Column("index_px", sa.Float()),
        sa.Column("last_trade_px", sa.Float()),
        sa.Column("volume_24h", sa.Float()),
        sa.Column("open_interest", sa.Float()),
        sa.Column("funding_rate", sa.Float()),
        sa.Column("depth_bands_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("exchange_ts_ms", sa.BigInteger()),
        sa.Column("received_ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("source_integrity", sa.String(length=32), nullable=False, server_default="confirmed"),
        sa.Column("staleness_ms", sa.BigInteger()),
        sa.Column("sequence", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_venue_market_snapshots_instrument_time", "venue_market_snapshots", ["instrument_id", "received_ts_ms"]
    )
    op.create_index(
        "ix_venue_market_snapshots_underlying_venue_time",
        "venue_market_snapshots",
        ["underlying_id", "venue_id", "received_ts_ms"],
    )

    op.create_table(
        "cross_venue_feature_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), primary_key=True),
        sa.Column("underlying_id", sa.String(length=128), nullable=False),
        sa.Column("reference_instrument_id", sa.String(length=64), nullable=False),
        sa.Column("comparison_instrument_id", sa.String(length=64), nullable=False),
        sa.Column("reference_venue_id", sa.String(length=96), nullable=False),
        sa.Column("comparison_venue_id", sa.String(length=96), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("price_delta_bps", sa.Float()),
        sa.Column("volume_imbalance", sa.Float()),
        sa.Column("depth_divergence", sa.Float()),
        sa.Column("liquidation_divergence", sa.Float()),
        sa.Column("lead_lag_windows_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("max_clock_skew_ms", sa.BigInteger()),
        sa.Column("quality_flags_json", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_cross_venue_feature_snapshots_underlying_time",
        "cross_venue_feature_snapshots",
        ["underlying_id", "as_of_ms"],
    )
    op.create_index(
        "ix_cross_venue_feature_snapshots_pair_time",
        "cross_venue_feature_snapshots",
        ["reference_instrument_id", "comparison_instrument_id", "as_of_ms"],
    )

    for table in ("feature_values", "engine_strategy_evaluations", "alpha_candidates", "order_intents"):
        op.add_column(table, sa.Column("instrument_id", sa.String(length=64)))
        op.add_column(table, sa.Column("underlying_id", sa.String(length=128)))
        op.add_column(table, sa.Column("venue_id", sa.String(length=96)))
        op.add_column(table, sa.Column("provider_symbol", sa.String(length=128)))
    op.add_column("alpha_candidates", sa.Column("evidence_epoch_id", sa.String(length=96)))
    for table in ("candidate_evidence_links", "candidate_outcome_attributions"):
        op.add_column(table, sa.Column("instrument_id", sa.String(length=64)))
        op.add_column(table, sa.Column("underlying_id", sa.String(length=128)))
        op.add_column(table, sa.Column("venue_id", sa.String(length=96)))

    op.create_index("ix_feature_values_instrument_computed", "feature_values", ["instrument_id", "computed_ts_ms"])
    op.create_index("ix_alpha_candidates_instrument_created", "alpha_candidates", ["instrument_id", "created_at_ms"])
    op.create_index("ix_alpha_candidates_evidence_epoch", "alpha_candidates", ["evidence_epoch_id", "created_at_ms"])
    op.create_index("ix_order_intents_instrument_created", "order_intents", ["instrument_id", "created_at_ms"])
    _backfill_legacy_instrument_identity()


def _backfill_legacy_instrument_identity() -> None:
    """Give pre-registry evidence an explicit, provider-specific identity.

    Keep the backfill entirely in SQL so Alembic can render it in offline
    mode.  The SHA-256 expression is identical to the former Python loop.
    """

    identity_tables = (
        "feature_values",
        "engine_strategy_evaluations",
        "alpha_candidates",
        "candidate_evidence_links",
        "candidate_outcome_attributions",
        "order_intents",
    )
    asset_union = "\nUNION\n".join(
        f"SELECT UPPER(BTRIM(asset)) AS asset FROM {table_name} WHERE asset IS NOT NULL AND BTRIM(asset) <> ''"
        for table_name in identity_tables
    )
    op.execute(
        sa.text(
            f"""
            CREATE TEMPORARY TABLE legacy_instrument_identity_map ON COMMIT DROP AS
            WITH legacy_assets AS (
                {asset_union}
            ), normalized AS (
                SELECT
                    asset,
                    CASE
                        WHEN asset LIKE 'XYZ:%' THEN 'hyperliquid:xyz'
                        ELSE 'hyperliquid:main'
                    END AS venue_id,
                    CASE
                        WHEN asset LIKE 'XYZ:%'
                            THEN 'xyz:' || SUBSTRING(asset FROM POSITION(':' IN asset) + 1)
                        ELSE asset
                    END AS provider_symbol
                FROM legacy_assets
            ), identities AS (
                SELECT
                    asset,
                    venue_id,
                    provider_symbol,
                    CASE
                        WHEN POSITION(':' IN provider_symbol) > 0
                            THEN UPPER(SUBSTRING(provider_symbol FROM POSITION(':' IN provider_symbol) + 1))
                        ELSE UPPER(provider_symbol)
                    END AS display_symbol
                FROM normalized
            )
            SELECT
                asset,
                'ins_' || SUBSTRING(
                    ENCODE(SHA256(CONVERT_TO(venue_id || '|' || provider_symbol, 'UTF8')), 'hex')
                    FROM 1 FOR 32
                ) AS instrument_id,
                'CRYPTO:' || display_symbol AS underlying_id,
                venue_id,
                provider_symbol,
                display_symbol
            FROM identities
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO instrument_registry (
                instrument_id, underlying_id, venue_id, provider_symbol, display_symbol,
                instrument_type, quote_currency, session_timezone, tradability_status,
                capabilities_json, mapping_version, first_observed_at_ms, last_observed_at_ms,
                metadata_json
            )
            SELECT
                instrument_id, underlying_id, venue_id, provider_symbol, display_symbol,
                'crypto_perp', 'USDC', 'UTC', 'active',
                CAST('{{"legacy_backfill": true}}' AS JSON), 1, 0, 0,
                CAST('{{"migration": "{revision}"}}' AS JSON)
            FROM legacy_instrument_identity_map
            """
        )
    )
    for table_name in (
        "feature_values",
        "engine_strategy_evaluations",
        "alpha_candidates",
        "order_intents",
    ):
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name} AS target SET
                    instrument_id=identity.instrument_id,
                    underlying_id=identity.underlying_id,
                    venue_id=identity.venue_id,
                    provider_symbol=identity.provider_symbol
                FROM legacy_instrument_identity_map AS identity
                WHERE UPPER(BTRIM(target.asset))=identity.asset
                """
            )
        )
    for table_name in ("candidate_evidence_links", "candidate_outcome_attributions"):
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name} AS target SET
                    instrument_id=identity.instrument_id,
                    underlying_id=identity.underlying_id,
                    venue_id=identity.venue_id
                FROM legacy_instrument_identity_map AS identity
                WHERE UPPER(BTRIM(target.asset))=identity.asset
                """
            )
        )
    op.execute("DROP TABLE legacy_instrument_identity_map")
    op.execute(
        "UPDATE alpha_candidates SET evidence_epoch_id='pre_0030' "
        "WHERE evidence_epoch_id IS NULL OR evidence_epoch_id=''"
    )


def downgrade() -> None:
    op.drop_index("ix_order_intents_instrument_created", table_name="order_intents")
    op.drop_index("ix_alpha_candidates_evidence_epoch", table_name="alpha_candidates")
    op.drop_index("ix_alpha_candidates_instrument_created", table_name="alpha_candidates")
    op.drop_index("ix_feature_values_instrument_computed", table_name="feature_values")
    for table in ("candidate_outcome_attributions", "candidate_evidence_links"):
        op.drop_column(table, "venue_id")
        op.drop_column(table, "underlying_id")
        op.drop_column(table, "instrument_id")
    op.drop_column("alpha_candidates", "evidence_epoch_id")
    for table in ("order_intents", "alpha_candidates", "engine_strategy_evaluations", "feature_values"):
        op.drop_column(table, "provider_symbol")
        op.drop_column(table, "venue_id")
        op.drop_column(table, "underlying_id")
        op.drop_column(table, "instrument_id")
    op.drop_index("ix_cross_venue_feature_snapshots_pair_time", table_name="cross_venue_feature_snapshots")
    op.drop_index("ix_cross_venue_feature_snapshots_underlying_time", table_name="cross_venue_feature_snapshots")
    op.drop_table("cross_venue_feature_snapshots")
    op.drop_index("ix_venue_market_snapshots_underlying_venue_time", table_name="venue_market_snapshots")
    op.drop_index("ix_venue_market_snapshots_instrument_time", table_name="venue_market_snapshots")
    op.drop_table("venue_market_snapshots")
    op.drop_index("ix_universe_snapshots_version", table_name="universe_snapshots")
    op.drop_table("universe_snapshots")
    op.drop_index("ix_watchlist_change_events_actor_created", table_name="watchlist_change_events")
    op.drop_index("ix_watchlist_change_events_status_created", table_name="watchlist_change_events")
    op.drop_table("watchlist_change_events")
    op.drop_index("ix_watchlist_memberships_tier_enabled", table_name="watchlist_memberships")
    op.drop_table("watchlist_memberships")
    op.drop_index("ix_instrument_registry_venue_status", table_name="instrument_registry")
    op.drop_index("ix_instrument_registry_underlying", table_name="instrument_registry")
    op.drop_table("instrument_registry")
