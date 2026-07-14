from __future__ import annotations

import re
from typing import Any

HEX_PRIVATE_KEY_RE = re.compile(r"0x[a-fA-F0-9]{64}")
CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")

_SECRET_FIELD_TOKENS = {
    "key",
    "mnemonic",
    "passwd",
    "password",
    "private",
    "pwd",
    "secret",
    "seed",
    "token",
}
_COMPACT_SECRET_FIELDS = {
    "accesstoken",
    "apikey",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "privatekey",
    "secretaccesskey",
    "secretkey",
    "seedphrase",
}


def redact_text(value: str) -> str:
    value = HEX_PRIVATE_KEY_RE.sub("[REDACTED_HEX_SECRET]", value)
    return value


def is_secret_field_name(value: Any) -> bool:
    """Match credential-shaped field names without treating symbols as secrets.

    The old substring matcher corrupted dictionary keys such as ``KEYS`` and
    ``WKEY``. Delimiter- and camel-case-aware tokenization still catches names
    such as ``api_key``, ``apiKey``, ``private-key``, and ``secretToken``.
    """

    name = str(value).strip()
    if not name:
        return False
    separated = CAMEL_CASE_BOUNDARY_RE.sub("_", name)
    tokens = [token.lower() for token in NON_ALNUM_RE.split(separated) if token]
    if any(token in _SECRET_FIELD_TOKENS for token in tokens):
        return True
    compact = "".join(tokens)
    return compact in _COMPACT_SECRET_FIELDS


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            if is_secret_field_name(key):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_secrets(inner)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value
