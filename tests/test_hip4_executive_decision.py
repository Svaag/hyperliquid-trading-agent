from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app

HIP4_ROOT = Path("hyperliquid_trading_agent/app/hip4")


def test_hip4_defaults_are_disabled_and_paper_shadow_only() -> None:
    settings = Settings(environment="test", hip4_enabled=False, hip4_scan_enabled=False, hip4_paper_execution_enabled=False, hip4_manual_ticket_export_enabled=False, _env_file=None)

    assert settings.hip4_enabled is False
    assert settings.hip4_mode == "paper_shadow"
    assert settings.hip4_scan_enabled is False
    assert settings.hip4_paper_execution_enabled is False
    assert settings.hip4_manual_ticket_export_enabled is False


def test_hip4_status_route_exposes_disabled_safe_posture() -> None:
    app = create_app(Settings(environment="test", position_tracking_enabled=False, autonomy_enabled=False, hip4_enabled=False, _env_file=None))
    client = TestClient(app)

    response = client.get("/hip4/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["status"] == "disabled"
    assert data["safety"]["signing_enabled"] is False
    assert data["safety"]["private_keys_enabled"] is False
    assert data["safety"]["exchange_mutation_enabled"] is False
    assert data["safety"]["live_orders_enabled"] is False
    assert data["safety"]["llm_controlled_execution_enabled"] is False
    assert data["safety"]["autonomy_promotion_enabled"] is False
    assert data["safety"]["perps_engine_promotion_enabled"] is False


def test_hip4_enabled_without_capability_probe_stays_degraded() -> None:
    app = create_app(Settings(environment="test", position_tracking_enabled=False, autonomy_enabled=False, hip4_enabled=True, _env_file=None))
    client = TestClient(app)

    response = client.get("/hip4/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["status"] == "degraded"
    assert "hip4_capability_probe_not_run" in data["degraded_reasons"]
    assert data["capabilities"]["scanner_implemented"] is True
    assert data["capabilities"]["paper_ledger_implemented"] is True
    assert data["capabilities"]["manual_ticket_export_registered"] is False


def test_hip4_package_does_not_import_live_execution_surfaces() -> None:
    forbidden_import_fragments = (
        "from hyperliquid.exchange import Exchange",
        "import hyperliquid.exchange",
        "from hyperliquid.utils.signing",
        "import eth_account",
    )
    for path in HIP4_ROOT.glob("*.py"):
        source = path.read_text()
        for fragment in forbidden_import_fragments:
            assert fragment not in source, f"{path} imports live execution surface: {fragment}"
