from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from hyperliquid_trading_agent.app.vault_admin import (
    VaultAdminError,
    VaultResponse,
    bootstrap_local,
    reset_plan,
    vault_status,
)


class FakeVaultClient:
    addr = "http://127.0.0.1:8200"

    def __init__(self, *, initialized: bool = False, sealed: bool = True) -> None:
        self.initialized = initialized
        self.sealed = sealed
        self.mounts: dict[str, dict[str, Any]] = {}
        self.secret: dict[str, str] = {}
        self.app_token = ""
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        payload: dict[str, Any] | None = None,
        acceptable: set[int] | None = None,
    ) -> VaultResponse:
        self.calls.append({"method": method, "path": path, "token": token, "payload": payload})
        if path == "/v1/sys/health":
            status = 501 if not self.initialized else 503 if self.sealed else 200
            return VaultResponse(
                status,
                {"initialized": self.initialized, "sealed": self.sealed, "standby": False, "version": "2.0.3"},
            )
        if path == "/v1/sys/init":
            self.initialized = True
            self.sealed = True
            return VaultResponse(200, {"keys_base64": ["dev-unseal-key"], "root_token": "dev-root-token"})
        if path == "/v1/sys/unseal":
            assert payload == {"key": "dev-unseal-key"}
            self.sealed = False
            return VaultResponse(200, {"sealed": False})
        if path == "/v1/sys/mounts" and method == "GET":
            return VaultResponse(200, {"data": self.mounts})
        if path == "/v1/sys/mounts/kv" and method == "POST":
            self.mounts["kv/"] = {"type": "kv", "options": {"version": "2"}}
            return VaultResponse(204, {})
        if path == "/v1/kv/data/hyperliquid-trading-agent/local" and method == "GET":
            if token not in {"dev-root-token", self.app_token}:
                return VaultResponse(403, {"errors": ["permission denied"]})
            if not self.secret:
                return VaultResponse(404, {"errors": []})
            return VaultResponse(200, {"data": {"data": self.secret}})
        if path == "/v1/kv/data/hyperliquid-trading-agent/local" and method == "POST":
            assert token == "dev-root-token"
            self.secret = dict((payload or {}).get("data") or {})
            return VaultResponse(200, {})
        if path.startswith("/v1/sys/policies/acl/"):
            assert token == "dev-root-token"
            assert 'capabilities = ["read"]' in str((payload or {}).get("policy"))
            return VaultResponse(204, {})
        if path == "/v1/auth/token/create":
            assert token == "dev-root-token"
            self.app_token = "read-only-app-token"
            return VaultResponse(200, {"auth": {"client_token": self.app_token}})
        raise AssertionError(f"unexpected request: {method} {path}")


def test_vault_status_contains_no_credentials() -> None:
    status = vault_status(FakeVaultClient(initialized=True, sealed=True))

    assert status == {
        "addr": "http://127.0.0.1:8200",
        "http_status": 503,
        "initialized": True,
        "sealed": True,
        "standby": False,
        "performance_standby": False,
        "version": "2.0.3",
    }


def test_bootstrap_local_initializes_seeds_allowlist_and_writes_private_files(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-local\nDISCORD_BOT_TOKEN=discord-local\nENGINE_ENABLED=true\nEMPTY_SECRET=\n",
        encoding="utf-8",
    )
    credentials_file = tmp_path / "admin" / "dev-credentials.json"
    app_token_file = tmp_path / "app" / "token"
    client = FakeVaultClient()

    result = bootstrap_local(
        client=client,
        credentials_file=credentials_file,
        app_token_file=app_token_file,
        env_file=env_file,
    )

    assert result["status"] == "ready"
    assert result["initialized_here"] is True
    assert result["seeded_key_names"] == ["DISCORD_BOT_TOKEN", "OPENAI_API_KEY"]
    assert result["secrets_printed"] is False
    assert result["destructive_actions"] == []
    assert "sk-local" not in str(result)
    assert client.secret == {"DISCORD_BOT_TOKEN": "discord-local", "OPENAI_API_KEY": "sk-local"}
    assert stat.S_IMODE(credentials_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(app_token_file.stat().st_mode) == 0o600
    assert app_token_file.read_text(encoding="utf-8").strip() == "read-only-app-token"

    repeated = bootstrap_local(
        client=client,
        credentials_file=credentials_file,
        app_token_file=app_token_file,
        env_file=env_file,
    )
    assert repeated["initialized_here"] is False
    assert repeated["app_token_reused"] is True
    assert sum(call["path"] == "/v1/sys/init" for call in client.calls) == 1
    assert sum(call["path"] == "/v1/auth/token/create" for call in client.calls) == 1


def test_bootstrap_refuses_initialized_vault_when_unseal_credentials_are_missing(tmp_path: Path) -> None:
    client = FakeVaultClient(initialized=True, sealed=True)

    with pytest.raises(VaultAdminError, match="separate approval"):
        bootstrap_local(
            client=client,
            credentials_file=tmp_path / "missing.json",
            app_token_file=tmp_path / "token",
            env_file=tmp_path / ".env",
        )

    assert [call["path"] for call in client.calls] == ["/v1/sys/health"]


def test_reset_plan_is_read_only_and_marks_volume_removal_as_approval_gated() -> None:
    client = FakeVaultClient(initialized=True, sealed=True)

    plan = reset_plan(client=client, compose_project="hta")

    assert plan["automatic_execution"] is False
    assert plan["destructive_step_requires_explicit_approval"] is True
    assert plan["affected_volume"] == "hta_vault_data"
    assert "docker volume rm hta_vault_data" in plan["commands"]
    assert [call["path"] for call in client.calls] == ["/v1/sys/health"]


def test_bootstrap_is_restricted_to_local_vault(tmp_path: Path) -> None:
    client = FakeVaultClient()
    client.addr = "https://vault.example.com"

    with pytest.raises(VaultAdminError, match="local development"):
        bootstrap_local(
            client=client,
            credentials_file=tmp_path / "credentials.json",
            app_token_file=tmp_path / "token",
            env_file=tmp_path / ".env",
        )

    assert client.calls == []
