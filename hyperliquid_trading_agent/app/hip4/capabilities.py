from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.schemas import Hip4CapabilityProbe
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import SubscriptionSpec

EXPECTED_OUTCOME_FIELDS = {"outcome", "name", "description", "sideSpecs", "quoteToken"}
EXPECTED_QUESTION_FIELDS = {"question", "name", "description", "fallbackOutcome", "namedOutcomes", "settledNamedOutcomes"}
SIZE_METADATA_FIELDS = {"szDecimals", "sizeDecimals", "lotSize"}
TICK_METADATA_FIELDS = {"tickSize", "pxDecimals", "priceDecimals"}


def schema_hash(payload: Any) -> str:
    import hashlib

    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def top_level_schema_hash(payload: Any) -> str:
    return schema_hash(_shape(payload))


class Hip4CapabilityProbeService:
    def __init__(self, *, settings: Settings, hip4_client: Any | None = None, repository: Any | None = None, ws_worker: Any | None = None):
        self.settings = settings
        self.hip4_client = hip4_client
        self.repository = repository
        self.ws_worker = ws_worker

    async def probe(self) -> Hip4CapabilityProbe:
        now_ms = int(time.time() * 1000)
        if self.hip4_client is None:
            return Hip4CapabilityProbe(
                network=self.settings.hyperliquid_network,
                probed_at_ms=now_ms,
                outcome_meta_error="hip4_client_unavailable",
                degraded_reasons=["hip4_client_unavailable"],
            )

        try:
            payload = await self.hip4_client.outcome_meta()
        except Exception as exc:
            probe = Hip4CapabilityProbe(
                network=self.settings.hyperliquid_network,
                probed_at_ms=now_ms,
                outcome_meta_error=type(exc).__name__,
                degraded_reasons=["outcome_meta_unavailable"],
            )
            await self._record_probe(probe)
            return probe

        probe = build_capability_probe(payload, settings=self.settings, probed_at_ms=now_ms)
        probe = await self._with_ws_probe(probe)
        await self._record_probe(probe)
        return probe

    async def _with_ws_probe(self, probe: Hip4CapabilityProbe) -> Hip4CapabilityProbe:
        if not self.settings.hip4_probe_outcome_meta_ws:
            return probe
        if self.ws_worker is None:
            reasons = [item for item in probe.degraded_reasons if item != "outcome_meta_ws_unconfirmed_rest_polling"]
            reasons.append("outcome_meta_ws_unsupported_no_worker")
            return probe.model_copy(update={"supports_outcome_meta_ws": False, "outcome_meta_ws_status": "unsupported", "degraded_reasons": reasons})
        event = asyncio.Event()

        async def _callback(_message: dict[str, Any]) -> None:
            event.set()

        sub_id: str | None = None
        try:
            sub_id = await self.ws_worker.subscribe(SubscriptionSpec("outcomeMetaUpdates"), _callback)
            await asyncio.wait_for(event.wait(), timeout=self.settings.hip4_outcome_meta_ws_probe_timeout_seconds)
            reasons = [item for item in probe.degraded_reasons if item != "outcome_meta_ws_unconfirmed_rest_polling"]
            return probe.model_copy(update={"supports_outcome_meta_ws": True, "outcome_meta_ws_status": "confirmed", "degraded_reasons": reasons})
        except TimeoutError:
            return probe.model_copy(update={"supports_outcome_meta_ws": False, "outcome_meta_ws_status": "unconfirmed"})
        except Exception:
            reasons = [item for item in probe.degraded_reasons if item != "outcome_meta_ws_unconfirmed_rest_polling"]
            reasons.append("outcome_meta_ws_unsupported")
            return probe.model_copy(update={"supports_outcome_meta_ws": False, "outcome_meta_ws_status": "unsupported", "degraded_reasons": reasons})
        finally:
            if sub_id is not None:
                try:
                    await self.ws_worker.unsubscribe(sub_id)
                except Exception:
                    pass

    async def _record_probe(self, probe: Hip4CapabilityProbe) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_hip4_capability_probe", None)
        if callable(record):
            await record(probe.model_dump(mode="json"))


def build_capability_probe(payload: Any, *, settings: Settings, probed_at_ms: int | None = None) -> Hip4CapabilityProbe:
    now_ms = probed_at_ms or int(time.time() * 1000)
    degraded: list[str] = []
    if not isinstance(payload, dict):
        return Hip4CapabilityProbe(
            network=settings.hyperliquid_network,
            probed_at_ms=now_ms,
            outcome_meta_error="unexpected_payload_type",
            degraded_reasons=["outcome_meta_unexpected_payload_type"],
        )

    outcomes = payload.get("outcomes")
    questions = payload.get("questions")
    supports_outcomes = isinstance(outcomes, list)
    supports_questions = isinstance(questions, list)
    if not supports_outcomes:
        degraded.append("outcomes_missing")
    question_fields_seen: set[str] = set()
    if supports_questions:
        for item in questions:
            if isinstance(item, dict):
                question_fields_seen.update(item.keys())
    missing_question_fields = sorted(EXPECTED_QUESTION_FIELDS - question_fields_seen) if supports_questions else sorted(EXPECTED_QUESTION_FIELDS)
    supports_question_fields = supports_questions and not missing_question_fields
    if not supports_questions:
        degraded.append("questions_missing_binary_only")
    elif not supports_question_fields:
        degraded.append("question_fields_unstable")

    quote_tokens: set[str] = set()
    outcome_fields_seen: set[str] = set()
    if supports_outcomes:
        for item in outcomes:
            if not isinstance(item, dict):
                continue
            outcome_fields_seen.update(item.keys())
            quote = item.get("quoteToken")
            if quote is not None and str(quote).strip():
                quote_tokens.add(str(quote))
    supports_quote_token = bool(quote_tokens)
    if not supports_quote_token:
        degraded.append("quote_token_missing")
    supports_authoritative_size_metadata = bool(outcome_fields_seen & SIZE_METADATA_FIELDS)
    supports_authoritative_tick_metadata = bool(outcome_fields_seen & TICK_METADATA_FIELDS)
    size_metadata_source = "outcomeMeta" if supports_authoritative_size_metadata else "unknown"
    tick_metadata_source = "outcomeMeta" if supports_authoritative_tick_metadata else "unknown"

    outcome_meta_ws_status = "unconfirmed" if settings.hip4_probe_outcome_meta_ws else "disabled"
    if outcome_meta_ws_status != "confirmed":
        degraded.append("outcome_meta_ws_unconfirmed_rest_polling")

    undocumented_fields: dict[str, list[str]] = {}
    if outcome_fields_seen:
        extra_outcome = sorted(outcome_fields_seen - EXPECTED_OUTCOME_FIELDS)
        if extra_outcome:
            undocumented_fields["outcomes"] = extra_outcome
    if question_fields_seen:
        extra_question = sorted(question_fields_seen - EXPECTED_QUESTION_FIELDS)
        if extra_question:
            undocumented_fields["questions"] = extra_question

    docs_scope_status = settings.hip4_docs_scope_status
    if settings.hyperliquid_network == "mainnet" and docs_scope_status == "testnet_only":
        degraded.append("hip4_docs_mark_testnet_only")
    supports_abstract_native_mechanics = supports_outcomes and supports_quote_token and not (settings.hyperliquid_network == "mainnet" and docs_scope_status == "testnet_only")
    supports_user_outcome_action_json = False
    supports_native_action_modeling = supports_abstract_native_mechanics
    supports_question_mechanics = supports_abstract_native_mechanics and supports_question_fields
    supports_manual_ticket_export = supports_outcomes and supports_quote_token and not (settings.hyperliquid_network == "mainnet" and docs_scope_status == "testnet_only")

    probe = Hip4CapabilityProbe(
        network=settings.hyperliquid_network,
        probed_at_ms=now_ms,
        outcome_meta_available=True,
        outcome_meta_top_level_keys=sorted(payload.keys()),
        outcome_meta_schema_hash=top_level_schema_hash(payload),
        supports_outcomes=supports_outcomes,
        supports_questions=supports_questions,
        supports_question_fields=supports_question_fields,
        question_fields_seen=sorted(question_fields_seen),
        missing_question_fields=missing_question_fields,
        supports_outcome_meta_ws=outcome_meta_ws_status == "confirmed",
        outcome_meta_ws_status=outcome_meta_ws_status,  # type: ignore[arg-type]
        supports_quote_token=supports_quote_token,
        quote_tokens_seen=sorted(quote_tokens),
        supports_authoritative_size_metadata=supports_authoritative_size_metadata,
        size_metadata_source=size_metadata_source,  # type: ignore[arg-type]
        supports_authoritative_tick_metadata=supports_authoritative_tick_metadata,
        tick_metadata_source=tick_metadata_source,  # type: ignore[arg-type]
        supports_abstract_native_mechanics=supports_abstract_native_mechanics,
        supports_user_outcome_action_json=supports_user_outcome_action_json,
        supports_native_action_modeling=supports_native_action_modeling,
        supports_question_mechanics=supports_question_mechanics,
        supports_manual_ticket_export=supports_manual_ticket_export,
        docs_scope_status=docs_scope_status,
        undocumented_fields=undocumented_fields,
        network_dependent_fields=[],
        degraded_reasons=degraded,
    )
    return probe


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _shape(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        if not value:
            return []
        return [_shape(value[0])]
    return type(value).__name__
