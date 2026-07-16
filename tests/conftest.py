from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator

import pytest

from hyperliquid_trading_agent.app.config import Settings, load_settings


def _production_event_loop_policy():
    if sys.platform != "win32":
        try:
            import uvloop
        except ImportError:
            pass
        else:
            return uvloop.EventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


def pytest_asyncio_loop_factories(config, item):
    """Use pytest-asyncio's supported loop-factory hook for async tests."""

    if sys.platform != "win32":
        try:
            import uvloop
        except ImportError:
            pass
        else:
            return {"uvloop": uvloop.new_event_loop}
    return {"asyncio": asyncio.new_event_loop}


@pytest.fixture(scope="session", autouse=True)
def use_production_event_loop_policy() -> Iterator[None]:
    """Use uvloop in tests where the production runtime supports it.

    Thread-backed adapters such as Starlette's TestClient and aiosqlite can
    complete before the selector loop reaches its first poll.  The managed
    Python 3.12 runner can then miss that initial wakeup until an unrelated
    timer fires.  Production Uvicorn uses uvloop on supported platforms, so
    exercising the same loop also makes these tests deterministic.
    """

    previous_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(_production_event_loop_policy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous_policy)


@pytest.fixture(autouse=True)
def isolate_local_dotenv(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep local runtime configuration and services out of unit tests."""

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    monkeypatch.setenv("VAULT_ENABLED", "false")
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()
