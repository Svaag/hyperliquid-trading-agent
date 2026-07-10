from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from hyperliquid_trading_agent.app.security import redact_text

LOCAL_VAULT_HOSTS = {"127.0.0.1", "::1", "localhost", "vault"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/-]*$")
SEED_SECRET_KEYS = {
    "AGENT_API_BEARER_TOKEN",
    "AGENT_CORE_TRACE_COLLECTOR_TOKEN",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "ALPHA_VANTAGE_API_KEY",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "DISCORD_BOT_TOKEN",
    "HL_GRPC_API_KEY",
    "HYPERLIQUID_API_WALLET_ADDRESS",
    "HYPERLIQUID_PRIVATE_KEY",
    "HYPERLIQUID_PRIVATE_KEY_TESTNET",
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PEM",
    "KIMI_API_KEY",
    "METRICS_BEARER_TOKEN",
    "NEWSAPI_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ORCHESTRATION_WAVE_SUPERVISOR_GITHUB_TOKEN",
    "PERPLEXITY_API_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_SECRET",
    "POSTGRES_PASSWORD",
    "SERPAPI_API_KEY",
    "TAVILY_API_KEY",
    "TRADING_ECONOMICS_API_KEY",
    "X_BEARER_TOKEN",
}


class VaultAdminError(RuntimeError):
    """A safe local Vault administration operation could not complete."""


@dataclass(frozen=True)
class VaultResponse:
    status_code: int
    payload: dict[str, Any]


class VaultClient(Protocol):
    addr: str

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        payload: dict[str, Any] | None = None,
        acceptable: set[int] | None = None,
    ) -> VaultResponse: ...


class VaultHttpClient:
    def __init__(self, addr: str, *, timeout: float = 5.0):
        self.addr = addr.strip().rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        payload: dict[str, Any] | None = None,
        acceptable: set[int] | None = None,
    ) -> VaultResponse:
        url = f"{self.addr}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Vault-Token"] = token
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status_code = int(response.status)
                raw = response.read()
        except HTTPError as exc:
            status_code = int(exc.code)
            raw = exc.read()
        except URLError as exc:
            raise VaultAdminError(f"Vault request failed at {self.addr}: {exc.reason}") from exc

        try:
            response_payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as exc:
            raise VaultAdminError(f"Vault returned invalid JSON for {path}") from exc
        if not isinstance(response_payload, dict):
            raise VaultAdminError(f"Vault returned an invalid response object for {path}")

        accepted = acceptable or {200, 204}
        if status_code not in accepted:
            errors = response_payload.get("errors") or []
            detail = "; ".join(str(item) for item in errors[:3]) or "request rejected"
            raise VaultAdminError(f"Vault returned HTTP {status_code} for {path}: {redact_text(detail)}")
        return VaultResponse(status_code=status_code, payload=response_payload)


def vault_status(client: VaultClient) -> dict[str, Any]:
    response = client.request(
        "GET",
        "/v1/sys/health",
        acceptable={200, 429, 472, 473, 501, 503},
    )
    payload = response.payload
    return {
        "addr": client.addr,
        "http_status": response.status_code,
        "initialized": bool(payload.get("initialized", response.status_code != 501)),
        "sealed": bool(payload.get("sealed", response.status_code in {501, 503})),
        "standby": bool(payload.get("standby", response.status_code in {429, 473})),
        "performance_standby": bool(payload.get("performance_standby", response.status_code == 473)),
        "version": str(payload.get("version") or "unknown"),
    }


def bootstrap_local(
    *,
    client: VaultClient,
    credentials_file: Path,
    app_token_file: Path,
    env_file: Path,
    mount: str = "kv",
    secret_path: str = "hyperliquid-trading-agent/local",
    policy_name: str = "hyperliquid-trading-agent-readonly",
    token_period: str = "24h",
) -> dict[str, Any]:
    """Initialize/configure local development Vault without ever resetting it."""

    _require_local_address(client.addr)
    _validate_names(mount=mount, secret_path=secret_path, policy_name=policy_name)
    status = vault_status(client)
    initialized_here = False

    if not status["initialized"]:
        init = client.request(
            "POST",
            "/v1/sys/init",
            payload={"secret_shares": 1, "secret_threshold": 1},
            acceptable={200},
        ).payload
        keys = init.get("keys_base64") or init.get("keys") or []
        root_token = str(init.get("root_token") or "")
        if not keys or not root_token:
            raise VaultAdminError("Vault initialization did not return an unseal key and root token")
        credentials = {
            "format_version": 1,
            "vault_addr": client.addr,
            "unseal_key": str(keys[0]),
            "root_token": root_token,
            "created_at_ms": int(time.time() * 1000),
        }
        _atomic_write_json(credentials_file, credentials)
        initialized_here = True
        status = vault_status(client)
    else:
        credentials = _read_credentials(credentials_file, expected_addr=client.addr)

    if not credentials_file.exists():  # defensive: initialization credentials must be durable before unseal
        raise VaultAdminError("Refusing to continue because local Vault credentials were not persisted")

    unseal_key = str(credentials.get("unseal_key") or "")
    root_token = str(credentials.get("root_token") or "")
    if not unseal_key or not root_token:
        raise VaultAdminError(f"Local credential file is incomplete: {credentials_file}")

    if status["sealed"]:
        unseal = client.request(
            "POST",
            "/v1/sys/unseal",
            payload={"key": unseal_key},
            acceptable={200},
        ).payload
        if bool(unseal.get("sealed", True)):
            raise VaultAdminError("Vault remained sealed after the local unseal operation")
        status = vault_status(client)
    if status["sealed"]:
        raise VaultAdminError("Vault is sealed")

    _ensure_kv_v2(client, root_token=root_token, mount=mount)
    seeded_values = _load_seed_values(env_file)
    secret_api_path = f"/v1/{_quote_path(mount)}/data/{_quote_path(secret_path)}"
    existing = client.request("GET", secret_api_path, token=root_token, acceptable={200, 404})
    existing_values = existing.payload.get("data", {}).get("data", {}) if existing.status_code == 200 else {}
    merged_values = {**(existing_values if isinstance(existing_values, dict) else {}), **seeded_values}
    if merged_values:
        client.request(
            "POST",
            secret_api_path,
            token=root_token,
            payload={"data": merged_values},
            acceptable={200, 204},
        )

    policy_path = f"{mount}/data/{secret_path}"
    policy = f'path "{policy_path}" {{\n  capabilities = ["read"]\n}}\n'
    client.request(
        "PUT",
        f"/v1/sys/policies/acl/{quote(policy_name, safe='')}",
        token=root_token,
        payload={"policy": policy},
        acceptable={200, 204},
    )

    token_reused = _app_token_is_valid(client, app_token_file=app_token_file, secret_api_path=secret_api_path)
    if not token_reused:
        token_response = client.request(
            "POST",
            "/v1/auth/token/create",
            token=root_token,
            payload={
                "policies": [policy_name],
                "period": token_period,
                "renewable": True,
                "no_default_policy": True,
                "display_name": "hyperliquid-trading-agent-local",
            },
            acceptable={200},
        ).payload
        app_token = str((token_response.get("auth") or {}).get("client_token") or "")
        if not app_token:
            raise VaultAdminError("Vault did not return an application token")
        _atomic_write_text(app_token_file, app_token + "\n")

    return {
        "status": "ready",
        "addr": client.addr,
        "initialized_here": initialized_here,
        "sealed": False,
        "kv_mount": mount,
        "secret_path": secret_path,
        "seeded_key_names": sorted(seeded_values),
        "seeded_key_count": len(seeded_values),
        "policy_name": policy_name,
        "credentials_file": str(credentials_file),
        "app_token_file": str(app_token_file),
        "app_token_reused": token_reused,
        "secrets_printed": False,
        "destructive_actions": [],
    }


def reset_plan(*, client: VaultClient, compose_project: str) -> dict[str, Any]:
    """Return, but never execute, the destructive recovery sequence for lost dev keys."""

    _require_local_address(client.addr)
    status = vault_status(client)
    volume_name = f"{compose_project}_vault_data"
    return {
        "status": status,
        "reason": "Use only when the local Vault is initialized and its sole unseal key is irrecoverably lost.",
        "destructive_step_requires_explicit_approval": True,
        "automatic_execution": False,
        "affected_volume": volume_name,
        "commands": [
            "docker compose stop api newswire world-model trader agent scheduler discord-publisher discord-bot liquidations",
            "docker compose --profile vault stop vault",
            "docker compose --profile vault rm -f vault",
            f"docker volume rm {volume_name}",
            "docker compose --profile vault up -d vault",
            "python -m hyperliquid_trading_agent.app.vault_admin bootstrap-local",
            "docker compose up -d api newswire world-model trader agent scheduler",
        ],
        "warning": "Do not run the volume-removal command without a separate explicit approval and confirmation that no recoverable key exists.",
    }


def _ensure_kv_v2(client: VaultClient, *, root_token: str, mount: str) -> None:
    mounts = client.request("GET", "/v1/sys/mounts", token=root_token, acceptable={200}).payload
    mount_data = mounts.get("data") if isinstance(mounts.get("data"), dict) else mounts
    existing = mount_data.get(f"{mount}/") if isinstance(mount_data, dict) else None
    if existing is None:
        client.request(
            "POST",
            f"/v1/sys/mounts/{quote(mount, safe='')}",
            token=root_token,
            payload={"type": "kv", "options": {"version": "2"}},
            acceptable={200, 204},
        )
        return
    options = existing.get("options") or {}
    if existing.get("type") != "kv" or str(options.get("version") or "1") != "2":
        raise VaultAdminError(f"Existing mount {mount!r} is not KV v2; refusing to replace it")


def _app_token_is_valid(client: VaultClient, *, app_token_file: Path, secret_api_path: str) -> bool:
    if not app_token_file.is_file():
        return False
    try:
        token = app_token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not token:
        return False
    response = client.request("GET", secret_api_path, token=token, acceptable={200, 403, 404})
    return response.status_code in {200, 404}


def _load_seed_values(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        return {}
    values = dotenv_values(env_file)
    return {
        key: str(value)
        for key, value in values.items()
        if key in SEED_SECRET_KEYS and value is not None and str(value).strip()
    }


def _read_credentials(path: Path, *, expected_addr: str) -> dict[str, Any]:
    if not path.is_file():
        raise VaultAdminError(
            "Vault is already initialized but the local unseal credentials are missing. "
            "Run reset-plan and obtain separate approval before deleting the Vault volume."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultAdminError(f"Could not read local Vault credentials from {path}") from exc
    if not isinstance(payload, dict):
        raise VaultAdminError(f"Local Vault credentials are invalid: {path}")
    stored_addr = str(payload.get("vault_addr") or "").rstrip("/")
    if stored_addr and stored_addr != expected_addr.rstrip("/"):
        raise VaultAdminError(f"Credential file belongs to {stored_addr}, not {expected_addr}")
    return payload


def _require_local_address(addr: str) -> None:
    parsed = urlparse(addr)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOCAL_VAULT_HOSTS:
        raise VaultAdminError("bootstrap-local and reset-plan are restricted to a local development Vault address")


def _validate_names(*, mount: str, secret_path: str, policy_name: str) -> None:
    if not SAFE_NAME_RE.fullmatch(mount):
        raise VaultAdminError("Vault mount must contain only letters, numbers, underscores, and hyphens")
    if not SAFE_PATH_RE.fullmatch(secret_path) or ".." in secret_path.split("/"):
        raise VaultAdminError("Vault secret path contains unsupported characters")
    if not SAFE_NAME_RE.fullmatch(policy_name):
        raise VaultAdminError("Vault policy name contains unsupported characters")


def _quote_path(value: str) -> str:
    return "/".join(quote(part, safe="") for part in value.strip("/").split("/") if part)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe local Vault recovery and bootstrap CLI")
    parser.add_argument("--addr", default=os.getenv("VAULT_ADDR", "http://127.0.0.1:8200"))
    parser.add_argument("--timeout", type=float, default=5.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")

    bootstrap = sub.add_parser("bootstrap-local")
    bootstrap.add_argument("--credentials-file", type=Path, default=Path(".local/vault/admin/dev-credentials.json"))
    bootstrap.add_argument("--app-token-file", type=Path, default=Path(".local/vault/app/token"))
    bootstrap.add_argument("--env-file", type=Path, default=Path(".env"))
    bootstrap.add_argument("--mount", default="kv")
    bootstrap.add_argument("--secret-path", default="hyperliquid-trading-agent/local")
    bootstrap.add_argument("--policy-name", default="hyperliquid-trading-agent-readonly")
    bootstrap.add_argument("--token-period", default="24h")

    reset = sub.add_parser("reset-plan")
    reset.add_argument("--compose-project", default=os.getenv("COMPOSE_PROJECT_NAME", Path.cwd().name))
    args = parser.parse_args()
    client = VaultHttpClient(args.addr, timeout=args.timeout)

    try:
        if args.command == "status":
            result = vault_status(client)
        elif args.command == "bootstrap-local":
            result = bootstrap_local(
                client=client,
                credentials_file=args.credentials_file,
                app_token_file=args.app_token_file,
                env_file=args.env_file,
                mount=args.mount,
                secret_path=args.secret_path,
                policy_name=args.policy_name,
                token_period=args.token_period,
            )
        elif args.command == "reset-plan":
            result = reset_plan(client=client, compose_project=args.compose_project)
        else:  # pragma: no cover
            raise VaultAdminError(f"Unknown command: {args.command}")
    except VaultAdminError as exc:
        parser.exit(2, f"vault admin error: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
