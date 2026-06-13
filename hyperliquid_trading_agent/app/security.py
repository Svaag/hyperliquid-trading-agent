from __future__ import annotations

import re
from typing import Any

SECRET_KEY_RE = re.compile(r"(key|token|secret|password|private|mnemonic|seed)", re.IGNORECASE)
HEX_PRIVATE_KEY_RE = re.compile(r"0x[a-fA-F0-9]{64}")


def redact_text(value: str) -> str:
    value = HEX_PRIVATE_KEY_RE.sub("[REDACTED_HEX_SECRET]", value)
    return value


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_secrets(inner)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value
