"""Add canonical instruments, versioned watchlists, and venue snapshots.

Revision ID: 0030_canonical_market_universe
Revises: 0029_engine_strategy_evaluations
"""

from __future__ import annotations

import hashlib

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
    op.create_index("ix_venue_market_snapshots_instrument_time", "venue_market_snapshots", ["instrument_id", "received_ts_ms"])
    op.create_index("ix_venue_market_snapshots_underlying_venue_time", "venue_market_snapshots", ["underlying_id", "venue_id", "received_ts_ms"])

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
    op.create_index("ix_cross_venue_feature_snapshots_underlying_time", "cross_venue_feature_snapshots", ["underlying_id", "as_of_ms"])
    op.create_index("ix_cross_venue_feature_snapshots_pair_time", "cross_venue_feature_snapshots", ["reference_instrument_id", "comparison_instrument_id", "as_of_ms"])

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
    """Give pre-registry evidence an explicit, provider-specific identity."""

    bind = op.get_bind()
    identity_tables = (
        "feature_values",
        "engine_strategy_evaluations",
        "alpha_candidates",
        "candidate_evidence_links",
        "candidate_outcome_attributions",
        "order_intents",
    )
    assets: set[str] = set()
    for table_name in identity_tables:
        rows = bind.execute(sa.text(f"SELECT DISTINCT asset FROM {table_name} WHERE asset IS NOT NULL"))
        assets.update(str(row[0]).strip().upper() for row in rows if row[0])

    registry = sa.table(
        "instrument_registry",
        sa.column("instrument_id", sa.String),
        sa.column("underlying_id", sa.String),
        sa.column("venue_id", sa.String),
        sa.column("provider_symbol", sa.String),
        sa.column("display_symbol", sa.String),
        sa.column("instrument_type", sa.String),
        sa.column("quote_currency", sa.String),
        sa.column("session_timezone", sa.String),
        sa.column("tradability_status", sa.String),
        sa.column("capabilities_json", sa.JSON),
        sa.column("mapping_version", sa.Integer),
        sa.column("first_observed_at_ms", sa.BigInteger),
        sa.column("last_observed_at_ms", sa.BigInteger),
        sa.column("metadata_json", sa.JSON),
    )
    identities: list[dict[str, str]] = []
    for asset in sorted(assets):
        provider_symbol = asset
        venue_id = "hyperliquid:main"
        if asset.startswith("XYZ:"):
            provider_symbol = "xyz:" + asset.split(":", 1)[1]
            venue_id = "hyperliquid:xyz"
        display = provider_symbol.split(":", 1)[-1].upper()
        underlying_id = f"CRYPTO:{display}"
        key = f"{venue_id}|{provider_symbol}"
        instrument_id = "ins_" + hashlib.sha256(key.encode()).hexdigest()[:32]
        bind.execute(
            registry.insert().values(
                instrument_id=instrument_id,
                underlying_id=underlying_id,
                venue_id=venue_id,
                provider_symbol=provider_symbol,
                display_symbol=display,
                instrument_type="crypto_perp",
                quote_currency="USDC",
                session_timezone="UTC",
                tradability_status="active",
                capabilities_json={"legacy_backfill": True},
                mapping_version=1,
                first_observed_at_ms=0,
                last_observed_at_ms=0,
                metadata_json={"migration": revision},
            )
        )
        identities.append(
            {
                "asset": asset,
                "instrument_id": instrument_id,
                "underlying_id": underlying_id,
                "venue_id": venue_id,
                "provider_symbol": provider_symbol,
            }
        )

    # Backfill each evidence table in one set-based pass. The previous per-asset
    # UPDATE loop forced a full scan of large evidence tables for every symbol.
    if identities:
        bind.execute(
            sa.text(
                "CREATE TEMPORARY TABLE legacy_instrument_identity_map ("
                "asset VARCHAR(128) PRIMARY KEY, instrument_id VARCHAR(64) NOT NULL, "
                "underlying_id VARCHAR(128) NOT NULL, venue_id VARCHAR(96) NOT NULL, "
                "provider_symbol VARCHAR(128) NOT NULL)"
            )
        )
        bind.execute(
            sa.text(
                "INSERT INTO legacy_instrument_identity_map "
                "(asset, instrument_id, underlying_id, venue_id, provider_symbol) "
                "VALUES (:asset, :instrument_id, :underlying_id, :venue_id, :provider_symbol)"
            ),
            identities,
        )
        for table_name in (
            "feature_values",
            "engine_strategy_evaluations",
            "alpha_candidates",
            "order_intents",
        ):
            bind.execute(
                sa.text(
                    f"UPDATE {table_name} SET "
                    "instrument_id=identity.instrument_id, underlying_id=identity.underlying_id, "
                    "venue_id=identity.venue_id, provider_symbol=identity.provider_symbol "
                    "FROM legacy_instrument_identity_map AS identity "
                    f"WHERE UPPER({table_name}.asset)=identity.asset"
                )
            )
        for table_name in ("candidate_evidence_links", "candidate_outcome_attributions"):
            bind.execute(
                sa.text(
                    f"UPDATE {table_name} SET "
                    "instrument_id=identity.instrument_id, underlying_id=identity.underlying_id, "
                    "venue_id=identity.venue_id "
                    "FROM legacy_instrument_identity_map AS identity "
                    f"WHERE UPPER({table_name}.asset)=identity.asset"
                )
            )
        bind.execute(sa.text("DROP TABLE legacy_instrument_identity_map"))
    bind.execute(
        sa.text(
            "UPDATE alpha_candidates SET evidence_epoch_id='pre_0030' "
            "WHERE evidence_epoch_id IS NULL OR evidence_epoch_id=''"
        )
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
