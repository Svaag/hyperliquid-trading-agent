from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError

import pytest

from hyperliquid_trading_agent.app import vault
from hyperliquid_trading_agent.app.vault import VaultConfigError, VaultLoadError, load_vault_environment


def test_vault_loader_hydrates_missing_env_values() -> None:
    env = {
        "VAULT_ENABLED": "true",
        "VAULT_TOKEN": "token",
        "VAULT_ADDR": "http://vault:8200",
        "VAULT_KV_MOUNT": "kv",
        "VAULT_SECRET_PATH": "hyperliquid-trading-agent/prod",
    }

    def fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        assert url == "http://vault:8200/v1/kv/data/hyperliquid-trading-agent/prod"
        assert headers == {"X-Vault-Token": "token"}
        assert timeout == 3.0
        return {
            "data": {
                "data": {
                    "OPENAI_API_KEY": "sk-test",
                    "HYPERLIQUID_PRIVATE_KEY": "0x" + "a" * 64,
                    "ENGINE_ENABLED": False,
                    "nested": {"ignored": True},
                }
            }
        }

    result = load_vault_environment(environ=env, fetch_json=fetch_json)

    assert env["OPENAI_API_KEY"] == "sk-test"
    assert env["HYPERLIQUID_PRIVATE_KEY"] == "0x" + "a" * 64
    assert env["ENGINE_ENABLED"] == "false"
    assert "nested" not in env
    assert result.loaded_keys == ("ENGINE_ENABLED", "HYPERLIQUID_PRIVATE_KEY", "OPENAI_API_KEY")
    assert result.skipped_keys == ("nested",)
    assert result.source == "kv-v2://kv/hyperliquid-trading-agent/prod"


def test_vault_loader_does_not_override_existing_values_by_default() -> None:
    env = {
        "VAULT_ENABLED": "true",
        "VAULT_TOKEN": "token",
        "OPENAI_API_KEY": "existing",
    }

    def fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        return {"data": {"data": {"OPENAI_API_KEY": "from-vault", "ANTHROPIC_API_KEY": "from-vault"}}}

    result = load_vault_environment(environ=env, fetch_json=fetch_json)

    assert env["OPENAI_API_KEY"] == "existing"
    assert env["ANTHROPIC_API_KEY"] == "from-vault"
    assert result.loaded_keys == ("ANTHROPIC_API_KEY",)
    assert result.skipped_keys == ("OPENAI_API_KEY",)


def test_vault_loader_can_override_existing_values() -> None:
    env = {
        "VAULT_ENABLED": "true",
        "VAULT_TOKEN": "token",
        "VAULT_ENV_OVERRIDE": "true",
        "OPENAI_API_KEY": "existing",
    }

    def fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        return {"data": {"data": {"OPENAI_API_KEY": "from-vault"}}}

    result = load_vault_environment(environ=env, fetch_json=fetch_json)

    assert env["OPENAI_API_KEY"] == "from-vault"
    assert result.loaded_keys == ("OPENAI_API_KEY",)
    assert result.skipped_keys == ()


def test_vault_loader_supports_token_file(tmp_path) -> None:
    token_file = tmp_path / "vault-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    env = {
        "VAULT_ENABLED": "true",
        "VAULT_TOKEN_FILE": str(token_file),
        "VAULT_KV_VERSION": "1",
        "VAULT_NAMESPACE": "admin",
    }

    def fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        assert url == "http://127.0.0.1:8200/v1/kv/hyperliquid-trading-agent/prod"
        assert headers == {"X-Vault-Token": "file-token", "X-Vault-Namespace": "admin"}
        return {"data": {"AGENT_API_BEARER_TOKEN": "agent-token"}}

    result = load_vault_environment(environ=env, fetch_json=fetch_json)

    assert env["AGENT_API_BEARER_TOKEN"] == "agent-token"
    assert result.source == "kv-v1://kv/hyperliquid-trading-agent/prod"


def test_vault_loader_requires_token_when_enabled() -> None:
    with pytest.raises(VaultConfigError):
        load_vault_environment(environ={"VAULT_ENABLED": "true"}, fetch_json=lambda url, headers, timeout: {})


def test_vault_loader_reports_sealed_state_with_recovery_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    def sealed(*args, **kwargs):
        raise HTTPError(
            "http://vault:8200/v1/kv/data/path",
            503,
            "service unavailable",
            {},
            BytesIO(b'{"errors":["Vault is sealed"]}'),
        )

    monkeypatch.setattr(vault, "urlopen", sealed)

    with pytest.raises(VaultLoadError, match="approval-gated recovery"):
        vault._fetch_json("http://vault:8200/v1/kv/data/path", {"X-Vault-Token": "token"}, 1.0)
