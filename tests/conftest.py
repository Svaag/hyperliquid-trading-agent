from __future__ import annotations

from collections.abc import Iterator

import pytest

from hyperliquid_trading_agent.app.config import Settings, load_settings


@pytest.fixture(autouse=True)
def isolate_local_dotenv(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep ignored local .env feature flags from changing unit-test defaults."""

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("VAULT_ENABLED", "false")
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()
