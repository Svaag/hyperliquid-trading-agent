from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def model_to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): model_to_jsonable(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [model_to_jsonable(item) for item in value]
    return value


def compact_context(value: Any, max_chars: int = 24_000) -> str:
    text = str(model_to_jsonable(value))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"
