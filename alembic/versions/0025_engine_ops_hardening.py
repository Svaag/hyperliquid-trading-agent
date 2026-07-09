"""Engine ops hardening: composite feature_values recency index.

The engine loop, readiness scorecard, and validation monitor all list recent
feature values per asset across feature names; the existing
(asset, feature_name, computed_ts_ms) index cannot serve that ordering.

Revision ID: 0025_engine_ops_hardening
Revises: 0024_prediction_market_paper
"""

from __future__ import annotations

from alembic import op

revision = "0025_engine_ops_hardening"
down_revision = "0024_prediction_market_paper"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_feature_values_asset_computed",
        "feature_values",
        ["asset", "computed_ts_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_feature_values_asset_computed", table_name="feature_values")
