from __future__ import annotations

from pathlib import Path


def test_hip4_migration_uses_next_observed_head_and_no_float_columns() -> None:
    migration = Path("alembic/versions/0015_hip4_outcomes.py")
    text = migration.read_text()

    assert "down_revision = \"0014_model_registry_retention\"" in text
    assert "hip4_raw_payloads" in text
    assert "hip4_edge_candidates" in text
    assert "sa.Float" not in text


def test_hip4_models_use_string_decimal_storage_not_float() -> None:
    text = Path("hyperliquid_trading_agent/app/db/models.py").read_text()
    hip4_section = text[text.index("class Hip4CapabilityProbeRecord") :]

    assert "Float" not in hip4_section
    assert "Hip4PaperFillRecord" in hip4_section
    assert "String(96)" in hip4_section


def test_hip4_repository_has_market_snapshot_and_settlement_persistence() -> None:
    text = Path("hyperliquid_trading_agent/app/db/repository.py").read_text()

    assert "record_hip4_market_snapshot" in text
    assert "Hip4MarketSnapshotRecord" in text
    assert "record_hip4_settlement" in text
    assert "Hip4SettlementRecord" in text
