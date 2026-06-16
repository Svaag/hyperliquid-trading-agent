from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.agent import prompts as agent_prompts
from hyperliquid_trading_agent.app.agent.high_stakes import prompts as high_stakes_prompts
from hyperliquid_trading_agent.app.autonomy.role_contracts import role_contract_block
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.schemas import DecisionContextRef, PromptVersionSnapshot, VersionSnapshot
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.security import redact_secrets

log = get_logger(__name__)

RISK_CONFIG_KEYS = {
    "hyperliquid_exchange_enabled",
    "alpaca_trading_enabled",
    "autonomy_mode",
    "autonomy_require_human_signoff",
    "autonomy_max_signals_per_day",
    "autonomy_min_signal_score",
    "autonomy_paper_initial_equity_usd",
    "autonomy_paper_risk_pct_per_trade",
    "autonomy_paper_max_gross_leverage",
    "autonomy_paper_max_single_name_exposure_pct",
    "autonomy_paper_taker_fee_bps",
    "autonomy_paper_maker_fee_bps",
    "autonomy_paper_default_slippage_bps",
    "autonomy_equity_paper_initial_equity_usd",
    "autonomy_equity_paper_risk_pct_per_trade",
    "autonomy_equity_paper_max_gross_leverage",
    "autonomy_equity_paper_max_single_name_exposure_pct",
    "autonomy_equity_paper_taker_fee_bps",
    "autonomy_equity_paper_maker_fee_bps",
    "autonomy_equity_paper_default_slippage_bps",
    "autonomy_memory_require_change_control_for_risk_execution",
    "position_tracking_enabled",
    "position_tracking_auto_arm",
    "position_tracking_max_active",
}


class DecisionContextRecorder:
    """Build and persist audit-only version snapshots for trade decisions.

    The recorder creates immutable references. It never changes live config,
    prompts, risk limits, model routes, broker permissions, or code.
    """

    def __init__(self, *, settings: Settings, repository: Any | None = None, code_version: str | None = None):
        self.settings = settings
        self.repository = repository
        self.code_version = code_version
        self.config_version: VersionSnapshot | None = None
        self.risk_config_version: VersionSnapshot | None = None
        self.model_route_version: VersionSnapshot | None = None
        self.prompt_versions: dict[str, PromptVersionSnapshot] = {}

    async def snapshot_startup(self) -> dict[str, Any]:
        """Capture current runtime artifacts and best-effort persist them."""

        created_at_ms = _now_ms()
        self.config_version = self._build_config_version(created_at_ms)
        self.risk_config_version = self._build_risk_config_version(created_at_ms)
        self.model_route_version = self._build_model_route_version(created_at_ms)
        self.prompt_versions = self._build_prompt_versions(created_at_ms)

        if self.repository is not None and getattr(self.repository, "enabled", False):
            try:
                upsert_config = getattr(self.repository, "upsert_config_version", None)
                upsert_prompt = getattr(self.repository, "upsert_prompt_version", None)
                if callable(upsert_config):
                    persisted = await upsert_config(self.config_version.model_dump(mode="json"))
                    if persisted is None:
                        return self.active_refs()
                    await upsert_config(self.risk_config_version.model_dump(mode="json"))
                    await upsert_config(self.model_route_version.model_dump(mode="json"))
                if callable(upsert_prompt):
                    for prompt in self.prompt_versions.values():
                        await upsert_prompt(prompt.model_dump(mode="json"))
                record_audit = getattr(self.repository, "record_audit_event", None)
                if callable(record_audit):
                    await record_audit("governance_version_snapshot", actor="system", payload=self.active_refs())
            except Exception as exc:  # pragma: no cover - audit persistence must never block startup
                log.warning("governance_version_snapshot_failed", error=type(exc).__name__)
        return self.active_refs()

    def active_refs(self) -> dict[str, Any]:
        return {
            "config_version_id": self.config_version.id if self.config_version else "cfg_unavailable",
            "risk_config_version_id": self.risk_config_version.id if self.risk_config_version else "risk_cfg_unavailable",
            "model_route_version_id": self.model_route_version.id if self.model_route_version else "model_routes_unavailable",
            "prompt_version_ids": [item.id for item in self.prompt_versions.values()],
            "code_version": self.code_version,
        }

    def new_decision_context(
        self,
        *,
        run_id: str | None = None,
        source_type: str = "unknown",
        source_id: str | None = None,
        prompt_names: list[str] | None = None,
        injected_memory_ids: list[str] | None = None,
        market_snapshot_refs: list[str] | None = None,
        data_freshness: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DecisionContextRef:
        prompt_ids = self._prompt_ids(prompt_names)
        now_ms = _now_ms()
        return DecisionContextRef(
            decision_id=f"dcx_{uuid4().hex}",
            run_id=run_id,
            config_version_id=self.config_version.id if self.config_version else "cfg_unavailable",
            risk_config_version_id=self.risk_config_version.id if self.risk_config_version else "risk_cfg_unavailable",
            prompt_version_ids=prompt_ids,
            model_route=self._model_route_ref(),
            injected_memory_ids=injected_memory_ids or [],
            market_snapshot_refs=market_snapshot_refs or [],
            data_freshness=data_freshness or {},
            code_version=self.code_version,
            created_at_ms=now_ms,
            metadata={"source_type": source_type, "source_id": source_id, **(metadata or {})},
        )

    async def record_decision_context(
        self,
        context: DecisionContextRef,
        *,
        source_type: str = "unknown",
        source_id: str | None = None,
    ) -> str | None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return context.decision_id
        record = getattr(self.repository, "record_decision_context", None)
        if not callable(record):
            return context.decision_id
        try:
            await record(context.model_dump(mode="json"), source_type=source_type, source_id=source_id)
        except Exception as exc:  # pragma: no cover
            log.warning("decision_context_record_failed", decision_id=context.decision_id, error=type(exc).__name__)
        return context.decision_id

    def _build_config_version(self, created_at_ms: int) -> VersionSnapshot:
        payload = redact_secrets(self.settings.model_dump(mode="json"))
        version_hash = _hash_payload(payload)
        return VersionSnapshot(
            id=_version_id("cfg", "runtime_settings", version_hash),
            scope="runtime_settings",
            version_hash=version_hash,
            payload=payload,
            code_version=self.code_version,
            created_at_ms=created_at_ms,
            metadata={"source": "settings.model_dump", "authority": "audit_only"},
        )

    def _build_risk_config_version(self, created_at_ms: int) -> VersionSnapshot:
        settings_payload = redact_secrets(self.settings.model_dump(mode="json"))
        payload = {key: settings_payload.get(key) for key in sorted(RISK_CONFIG_KEYS) if key in settings_payload}
        payload["autonomy_config_warnings"] = self.settings.autonomy_config_warnings()
        payload["tradfi_config_warnings"] = self.settings.tradfi_config_warnings()
        payload["newswire_config_warnings"] = self.settings.newswire_config_warnings()
        version_hash = _hash_payload(payload)
        return VersionSnapshot(
            id=_version_id("risk", "risk_settings", version_hash),
            scope="risk_settings",
            version_hash=version_hash,
            payload=payload,
            code_version=self.code_version,
            created_at_ms=created_at_ms,
            metadata={"source": "settings.risk_subset", "authority": "audit_only"},
        )

    def _build_model_route_version(self, created_at_ms: int) -> VersionSnapshot:
        payload = {
            "agent_model_chain": self.settings.model_chain,
            "high_stakes_debate_enabled": self.settings.high_stakes_debate_enabled,
            "high_stakes_activation_policy": self.settings.high_stakes_activation_policy,
            "high_stakes_prompt_style": self.settings.high_stakes_prompt_style,
            "debate_role_chains": {role: self.settings.role_model_chain(role) for role in self.settings.debate_role_names},
            "debate_model_contract": self.settings.debate_model_contract(),
        }
        payload = redact_secrets(payload)
        version_hash = _hash_payload(payload)
        return VersionSnapshot(
            id=_version_id("model", "model_routes", version_hash),
            scope="model_routes",
            version_hash=version_hash,
            payload=payload,
            code_version=self.code_version,
            created_at_ms=created_at_ms,
            metadata={"source": "settings.model_routes", "authority": "audit_only"},
        )

    def _build_prompt_versions(self, created_at_ms: int) -> dict[str, PromptVersionSnapshot]:
        prompts: dict[str, dict[str, Any]] = {
            "agent.system": {"content": agent_prompts.SYSTEM_PROMPT, "kind": "system"},
            "agent.response_template": {"content": agent_prompts.DEFAULT_RESPONSE_TEMPLATE, "kind": "template"},
        }
        style = self.settings.high_stakes_prompt_style
        for role in high_stakes_prompts.ROLES:
            prompts[f"high_stakes.{role}.{style}.system"] = {
                "content": high_stakes_prompts.role_system_prompt(role, style),
                "kind": "system",
                "role": role,
                "style": style,
            }
            prompts[f"high_stakes.{role}.{style}.user"] = {
                "content": high_stakes_prompts.role_user_prompt(role, style),
                "kind": "user",
                "role": role,
                "style": style,
            }
            prompts[f"role_contract.{role}"] = {
                "content": role_contract_block(role),
                "kind": "role_contract",
                "role": role,
            }
        out: dict[str, PromptVersionSnapshot] = {}
        for name, payload in prompts.items():
            content = str(payload.get("content") or "")
            content_hash = _hash_text(content)
            version_hash = _hash_payload(payload)
            out[name] = PromptVersionSnapshot(
                id=_version_id("prompt", name, version_hash),
                prompt_name=name,
                version_hash=version_hash,
                content_hash=content_hash,
                payload=payload,
                code_version=self.code_version,
                created_at_ms=created_at_ms,
                metadata={"authority": "audit_only"},
            )
        return out

    def _prompt_ids(self, prompt_names: list[str] | None) -> list[str]:
        if not self.prompt_versions:
            return []
        if not prompt_names:
            return [item.id for item in self.prompt_versions.values()]
        ids: list[str] = []
        for name in prompt_names:
            item = self.prompt_versions.get(name)
            if item is not None:
                ids.append(item.id)
        return ids

    def _model_route_ref(self) -> dict[str, Any]:
        return {
            "version_id": self.model_route_version.id if self.model_route_version else "model_routes_unavailable",
            "version_hash": self.model_route_version.version_hash if self.model_route_version else None,
            "agent_model_chain": self.settings.model_chain,
            "debate_role_primary_models": self.settings.debate_role_primary_models,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_payload(payload: Any) -> str:
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _version_id(prefix: str, scope: str, version_hash: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in scope.lower()).strip("_")[:32]
    return f"{prefix}_{normalized}_{version_hash[:16]}"
