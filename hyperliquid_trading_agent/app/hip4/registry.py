from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import schema_hash
from hyperliquid_trading_agent.app.hip4.schemas import OutcomeSpec, QuestionSpec, RawPayloadRecord


class Hip4Registry:
    def __init__(self, *, settings: Settings, hip4_client: Any | None = None, repository: Any | None = None):
        self.settings = settings
        self.hip4_client = hip4_client
        self.repository = repository
        self.outcomes: dict[int, OutcomeSpec] = {}
        self.questions: dict[int, QuestionSpec] = {}
        self.raw_payload: dict[str, Any] | None = None
        self.raw_schema_hash: str | None = None
        self.last_refresh_at_ms: int | None = None
        self.last_error: str | None = None

    async def refresh(self) -> None:
        if self.hip4_client is None:
            self.last_error = "hip4_client_unavailable"
            return
        try:
            payload = await self.hip4_client.outcome_meta()
        except Exception as exc:
            self.last_error = type(exc).__name__
            return
        self.load_raw(payload, observed_at_ms=int(time.time() * 1000))
        await self._persist_current()

    def load_raw(self, payload: dict[str, Any], *, observed_at_ms: int | None = None) -> None:
        now_ms = observed_at_ms or int(time.time() * 1000)
        self.raw_payload = dict(payload)
        self.raw_schema_hash = schema_hash(payload)
        self.outcomes = {item.outcome_id: item for item in parse_outcomes(payload)}
        self.questions = {item.question_id: item for item in parse_questions(payload)}
        self.last_refresh_at_ms = now_ms
        self.last_error = None

    def status(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        age_ms = None if self.last_refresh_at_ms is None else now_ms - self.last_refresh_at_ms
        stale = age_ms is None or age_ms > self.settings.hip4_registry_max_staleness_ms
        return {
            "outcome_count": len(self.outcomes),
            "question_count": len(self.questions),
            "last_refresh_at_ms": self.last_refresh_at_ms,
            "age_ms": age_ms,
            "stale": stale,
            "last_error": self.last_error,
            "raw_schema_hash": self.raw_schema_hash,
        }

    def list_outcomes(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in sorted(self.outcomes.values(), key=lambda item: item.outcome_id)]

    def list_questions(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in sorted(self.questions.values(), key=lambda item: item.question_id)]

    async def _persist_current(self) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False) or self.raw_payload is None or self.last_refresh_at_ms is None:
            return
        raw = RawPayloadRecord(
            source="outcomeMeta",
            network=self.settings.hyperliquid_network,
            payload_json=self.raw_payload,
            schema_hash=self.raw_schema_hash or schema_hash(self.raw_payload),
            schema_version=1,
            observed_at_ms=self.last_refresh_at_ms,
        )
        record_raw = getattr(self.repository, "record_hip4_raw_payload", None)
        if callable(record_raw):
            await record_raw(raw.model_dump(mode="json"))
        upsert_outcomes = getattr(self.repository, "upsert_hip4_outcome_specs", None)
        if callable(upsert_outcomes):
            await upsert_outcomes([item.model_dump(mode="json") for item in self.outcomes.values()], as_of_ms=self.last_refresh_at_ms)
        upsert_questions = getattr(self.repository, "upsert_hip4_question_specs", None)
        if callable(upsert_questions):
            await upsert_questions([item.model_dump(mode="json") for item in self.questions.values()], as_of_ms=self.last_refresh_at_ms)


def parse_outcomes(payload: dict[str, Any]) -> list[OutcomeSpec]:
    raw_outcomes = payload.get("outcomes")
    if not isinstance(raw_outcomes, list):
        return []
    out: list[OutcomeSpec] = []
    for item in raw_outcomes:
        if not isinstance(item, dict):
            continue
        outcome_id = _to_int(item.get("outcome"))
        if outcome_id is None:
            continue
        side0, side1 = _side_names(item.get("sideSpecs"))
        out.append(
            OutcomeSpec(
                outcome_id=outcome_id,
                name=str(item.get("name") or f"Outcome {outcome_id}"),
                description=str(item.get("description") or ""),
                quote_token=str(item.get("quoteToken")) if item.get("quoteToken") is not None else None,
                side0_name=side0,
                side1_name=side1,
                settled=bool(item.get("settled", False)),
                raw=item,
            )
        )
    return out


def parse_questions(payload: dict[str, Any]) -> list[QuestionSpec]:
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return []
    out: list[QuestionSpec] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        question_id = _to_int(item.get("question"))
        if question_id is None:
            continue
        fallback = _to_int(item.get("fallbackOutcome"))
        named = _int_list(item.get("namedOutcomes"))
        settled = _int_list(item.get("settledNamedOutcomes"))
        outcome_ids: list[int] = []
        if fallback is not None:
            outcome_ids.append(fallback)
        for outcome_id in named:
            if outcome_id not in outcome_ids:
                outcome_ids.append(outcome_id)
        if settled and len(set(settled)) >= len(set(named)) and named:
            status = "settled"
        elif settled:
            status = "partial_settled"
        else:
            status = "open"
        out.append(
            QuestionSpec(
                question_id=question_id,
                name=str(item.get("name") or f"Question {question_id}"),
                description=str(item.get("description") or ""),
                fallback_outcome_id=fallback,
                named_outcome_ids=named,
                settled_named_outcome_ids=settled,
                outcome_ids=outcome_ids,
                status=status,  # type: ignore[arg-type]
                raw=item,
            )
        )
    return out


def _side_names(value: Any) -> tuple[str, str]:
    if isinstance(value, list) and len(value) >= 2:
        return _side_name(value[0], "YES"), _side_name(value[1], "NO")
    return "YES", "NO"


def _side_name(value: Any, default: str) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("side") or default)
    if value is not None:
        return str(value)
    return default


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        parsed = _to_int(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _to_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
