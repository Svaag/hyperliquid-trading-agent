from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, MutableMapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import dotenv_values

ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class VaultConfigError(RuntimeError):
    """Vault is enabled, but the local configuration is incomplete."""


class VaultLoadError(RuntimeError):
    """Vault was contacted but the secret could not be loaded."""


@dataclass(frozen=True)
class VaultEnvLoadResult:
    loaded_keys: tuple[str, ...]
    skipped_keys: tuple[str, ...]
    source: str


JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any]]


def load_vault_environment(
    *,
    environ: MutableMapping[str, str] | None = None,
    fetch_json: JsonFetcher | None = None,
    env_file: str = ".env",
) -> VaultEnvLoadResult:
    """Load one Vault KV secret into process environment variables.

    The app remains env-driven, but this lets the process hydrate those env vars
    from Vault during startup. Values already present in the environment win
    unless VAULT_ENV_OVERRIDE=true.
    """

    env = environ if environ is not None else os.environ
    if environ is None:
        _load_vault_bootstrap_dotenv(env, env_file=env_file)
    if not _truthy(env.get("VAULT_ENABLED", "")):
        return VaultEnvLoadResult(loaded_keys=(), skipped_keys=(), source="")

    token = _vault_token(env)
    if not token:
        raise VaultConfigError("VAULT_ENABLED=true requires VAULT_TOKEN or VAULT_TOKEN_FILE")

    addr = env.get("VAULT_ADDR", "http://127.0.0.1:8200").strip().rstrip("/")
    mount = env.get("VAULT_KV_MOUNT", "kv").strip().strip("/")
    secret_path = env.get("VAULT_SECRET_PATH", "hyperliquid-trading-agent/prod").strip().strip("/")
    kv_version = env.get("VAULT_KV_VERSION", "2").strip()
    timeout = _float_env(env.get("VAULT_TIMEOUT_SECONDS", "3"), default=3.0)
    override = _truthy(env.get("VAULT_ENV_OVERRIDE", "false"))
    namespace = env.get("VAULT_NAMESPACE", "").strip()

    if not addr or not mount or not secret_path:
        raise VaultConfigError("VAULT_ADDR, VAULT_KV_MOUNT, and VAULT_SECRET_PATH must be set when Vault is enabled")

    url = _secret_url(addr=addr, mount=mount, secret_path=secret_path, kv_version=kv_version)
    headers = {"X-Vault-Token": token}
    if namespace:
        headers["X-Vault-Namespace"] = namespace

    payload = (fetch_json or _fetch_json)(url, headers, timeout)
    values = _extract_secret_data(payload, kv_version=kv_version)
    loaded: list[str] = []
    skipped: list[str] = []
    for key, raw_value in values.items():
        if not ENV_KEY_RE.fullmatch(str(key)):
            skipped.append(str(key))
            continue
        value = _coerce_env_value(raw_value)
        if value is None:
            skipped.append(str(key))
            continue
        if env.get(str(key)) and not override:
            skipped.append(str(key))
            continue
        env[str(key)] = value
        loaded.append(str(key))

    source = f"kv-v{kv_version}://{mount}/{secret_path}"
    return VaultEnvLoadResult(loaded_keys=tuple(sorted(loaded)), skipped_keys=tuple(sorted(skipped)), source=source)


def _vault_token(env: MutableMapping[str, str]) -> str:
    token = env.get("VAULT_TOKEN", "").strip()
    if token:
        return token
    token_file = env.get("VAULT_TOKEN_FILE", "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultConfigError(f"Could not read VAULT_TOKEN_FILE: {token_file}") from exc


def _load_vault_bootstrap_dotenv(env: MutableMapping[str, str], *, env_file: str) -> None:
    for key, value in dotenv_values(env_file).items():
        if not key.startswith("VAULT_") or value is None or key in env:
            continue
        env[key] = value


def _secret_url(*, addr: str, mount: str, secret_path: str, kv_version: str) -> str:
    mount_part = _quote_path(mount)
    path_part = _quote_path(secret_path)
    if kv_version == "1":
        return f"{addr}/v1/{mount_part}/{path_part}"
    if kv_version == "2":
        return f"{addr}/v1/{mount_part}/data/{path_part}"
    raise VaultConfigError("VAULT_KV_VERSION must be 1 or 2")


def _quote_path(value: str) -> str:
    return "/".join(quote(part, safe="") for part in value.strip("/").split("/") if part)


def _fetch_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise VaultLoadError(f"Vault returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise VaultLoadError(f"Vault request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise VaultLoadError("Vault returned invalid JSON") from exc


def _extract_secret_data(payload: dict[str, Any], *, kv_version: str) -> dict[str, Any]:
    if kv_version == "1":
        data = payload.get("data")
    else:
        data = payload.get("data", {}).get("data")
    if not isinstance(data, dict):
        raise VaultLoadError("Vault response did not contain a KV secret object")
    return data


def _coerce_env_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return None


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(value: str, *, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default
