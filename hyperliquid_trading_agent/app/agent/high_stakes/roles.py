from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from hyperliquid_trading_agent.app.agent.high_stakes.json_io import compact_context, model_to_jsonable
from hyperliquid_trading_agent.app.agent.high_stakes.prompts import role_system_prompt, role_user_prompt
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import (
    JudgeDecision,
    ProposalStatus,
    RoleOpinion,
    RoleScorecard,
    TradeSetupDraft,
)
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.autonomy.role_contracts import role_contract_block
from hyperliquid_trading_agent.app.config import Settings


@dataclass(frozen=True)
class RoleCallResult:
    parsed: BaseModel
    raw_content: str
    model: str | None
    provider: str | None
    latency_ms: int
    status: str = "ok"


class HighStakesRoleRunner:
    def __init__(self, model_gateway: ModelGateway, settings: Settings, memory_service: Any | None = None, world_model_service: Any | None = None):
        self.model_gateway = model_gateway
        self.settings = settings
        self.memory_service = memory_service
        self.world_model_service = world_model_service

    async def draft_setup(self, state: dict[str, Any]) -> RoleCallResult:
        started = time.perf_counter()
        try:
            response = await self._complete_structured_with_timeout(
                role_user_prompt("analyst", self.settings.high_stakes_prompt_style),
                self._system_prompt("analyst"),
                TradeSetupDraft,
                model_chain=self.settings.role_model_chain("analyst"),
                temperature=0.15,
                max_tokens=1400,
                context={"state": compact_context(await self._state_for_role_model("analyst", state))},
                timeout_seconds=self._role_timeout_seconds(state, "analyst"),
            )
            return RoleCallResult(response.parsed, response.raw_content, response.model, response.provider, _elapsed_ms(started))
        except Exception as exc:
            fallback = fallback_draft(state, reason=_error_reason(exc))
            return RoleCallResult(fallback, fallback.model_dump_json(), None, None, _elapsed_ms(started), status="fallback")

    async def review(self, role: str, state: dict[str, Any]) -> RoleCallResult:
        started = time.perf_counter()
        route = state.get("route")
        selected_roles = getattr(route, "selected_roles", [])
        if role not in selected_roles:
            opinion = RoleOpinion(role=role, stance="abstain", summary="Role not activated for this route.")
            return RoleCallResult(opinion, opinion.model_dump_json(), None, None, _elapsed_ms(started), status="abstain")
        try:
            response = await self._complete_structured_with_timeout(
                role_user_prompt(role, self.settings.high_stakes_prompt_style),
                self._system_prompt(role),
                RoleOpinion,
                model_chain=self.settings.role_model_chain(role),
                temperature=0.1,
                max_tokens=2200,
                context={"state": compact_context(await self._state_for_role_model(role, state))},
                timeout_seconds=self._role_timeout_seconds(state, role),
            )
            parsed = response.parsed
            if isinstance(parsed, RoleOpinion):
                opinion = parsed.model_copy(update={"role": role, "model": response.model, "provider": response.provider})
            else:
                opinion = RoleOpinion(role=role, stance="error", summary="Structured role response had the wrong schema.")
            return RoleCallResult(opinion, response.raw_content, response.model, response.provider, _elapsed_ms(started))
        except Exception as exc:
            opinion = fallback_opinion(role, state, reason=_error_reason(exc))
            return RoleCallResult(opinion, opinion.model_dump_json(), None, None, _elapsed_ms(started), status="fallback")

    async def judge(self, state: dict[str, Any]) -> RoleCallResult:
        started = time.perf_counter()
        try:
            response = await self._complete_structured_with_timeout(
                role_user_prompt("judge", self.settings.high_stakes_prompt_style),
                self._system_prompt("judge"),
                JudgeDecision,
                model_chain=self.settings.role_model_chain("judge"),
                temperature=0.05,
                max_tokens=2200,
                context={"state": compact_context(await self._state_for_role_model("judge", state))},
                timeout_seconds=self._role_timeout_seconds(state, "judge"),
            )
            decision = response.parsed
            if isinstance(decision, JudgeDecision):
                decision = _force_no_execution(decision.model_copy(update={"model": response.model, "provider": response.provider}), state)
            return RoleCallResult(decision, response.raw_content, response.model, response.provider, _elapsed_ms(started))
        except Exception as exc:
            decision = fallback_judge(state, reason=_error_reason(exc))
            return RoleCallResult(decision, decision.model_dump_json(), None, None, _elapsed_ms(started), status="fallback")

    def _system_prompt(self, role: str) -> str:
        contract = role_contract_block(role)
        base = role_system_prompt(role, self.settings.high_stakes_prompt_style)
        return f"{base}\n\n{contract}" if contract else base

    async def _state_for_role_model(self, role: str, state: dict[str, Any]) -> dict[str, Any]:
        data = _state_for_model(state)
        data["role_contract"] = role_contract_block(role)
        data["persistent_memory"] = await self._memory_block(role, state)
        data["market_world_model"] = self._world_model_block(state)
        data["memory_policy"] = "Persistent memories are advisory context only. Do not auto-change strategy, sizing, risk limits, thresholds, cooldowns, or execution behavior from memory."
        return data

    async def _memory_block(self, role: str, state: dict[str, Any]) -> str:
        if self.memory_service is None:
            return ""
        block = getattr(self.memory_service, "memory_block_for_role", None)
        if not callable(block):
            return ""
        route = state.get("route")
        coins = getattr(route, "coins", []) or []
        symbol = coins[0] if coins else None
        context = state.get("context")
        features = getattr(context, "features", {}) if context else {}
        parsed = features.get("parsed_setup", {}) if isinstance(features, dict) else {}
        signal_type = parsed.get("signal_type") if isinstance(parsed, dict) else None
        market_regime = features.get("risk_regime") if isinstance(features, dict) else None
        try:
            return await block(role, symbol=symbol, signal_type=signal_type, market_regime=market_regime, max_items=5, run_id=state.get("run_id"))
        except Exception:
            return ""

    def _world_model_block(self, state: dict[str, Any]) -> str:
        if self.world_model_service is None:
            return ""
        block = getattr(self.world_model_service, "wiki_block", None)
        if not callable(block):
            return ""
        route = state.get("route")
        coins = getattr(route, "coins", []) or []
        try:
            return block(symbols=coins, max_chars=1800)
        except Exception:
            return ""

    async def _complete_structured_with_timeout(self, *args: Any, **kwargs: Any):
        # Models can hang or return malformed JSON. The gateway front-loads the role budget
        # across the fallback chain and enforces each slice with litellm's native (HTTP-level)
        # timeout, which actually interrupts an in-flight call — asyncio.wait_for is only a
        # coarse cancellation backstop above the gateway's own per-attempt deadlines.
        timeout_seconds = float(kwargs.pop("timeout_seconds", self._default_role_timeout_seconds()))
        kwargs.setdefault("attempt_timeout_budget", timeout_seconds)
        backstop_slack = min(5.0, max(2.0, timeout_seconds * 0.10))
        return await asyncio.wait_for(
            self.model_gateway.complete_structured(*args, **kwargs), timeout=timeout_seconds + backstop_slack
        )

    def _role_timeout_seconds(self, state: dict[str, Any], role: str) -> float:
        total = float(self.settings.high_stakes_timeout_seconds)
        reserve_seconds = min(45.0, max(20.0, total * 0.15))
        remaining = max(0.0, total - self._elapsed_seconds(state) - reserve_seconds)
        # Budget from the wall-clock that is actually left, divided by the phases still to
        # run in this round (not all phases). Dividing by *remaining* phases keeps the judge —
        # which runs last and used to be starved to ~27s — on an equal footing with earlier
        # phases, while staying self-limiting: fast phases leave more for the rest, slow phases
        # borrow from what is left. We never divide by max_rounds (revision rounds re-budget).
        phases = self._active_phases(state)
        position = phases.index(self._phase_of_role(role)) if self._phase_of_role(role) in phases else 0
        phases_remaining = max(1, len(phases) - position)
        return min(50.0, max(8.0, remaining / phases_remaining))

    def _active_phases(self, state: dict[str, Any]) -> list[str]:
        route = state.get("route")
        selected = set(getattr(route, "selected_roles", []) or [])
        phases = ["proposer"]
        if selected & {"quant", "research", "risk", "treasury", "execution"}:
            phases.append("reviewers")  # bounded concurrent reviewer batch
        if "adversary" in selected:
            phases.append("adversary")  # adversary sees reviewer outputs
        phases.append("judge")
        return phases

    @staticmethod
    def _phase_of_role(role: str) -> str:
        if role == "analyst":
            return "proposer"
        if role in {"adversary", "judge"}:
            return role
        return "reviewers"

    def _elapsed_seconds(self, state: dict[str, Any]) -> float:
        started_at = state.get("started_at")
        if not isinstance(started_at, (int, float)):
            return 0.0
        return max(0.0, time.perf_counter() - float(started_at))

    def _default_role_timeout_seconds(self) -> float:
        return min(30.0, max(8.0, self.settings.high_stakes_timeout_seconds / 8))


def fallback_draft(state: dict[str, Any], reason: str = "") -> TradeSetupDraft:
    context = state.get("context")
    features = getattr(context, "features", {}) if context else {}
    parsed = features.get("parsed_setup", {}) if isinstance(features, dict) else {}
    route = state.get("route")
    coins = getattr(route, "coins", []) or []
    needs: list[str] = []
    if not parsed.get("entry"):
        needs.append("entry")
    if not parsed.get("stop"):
        needs.append("stop")
    if not parsed.get("side"):
        needs.append("side")
    assumptions = ["LLM role call unavailable; draft is parsed deterministically."]
    if reason:
        assumptions.append(f"model_fallback:{reason}")
    return TradeSetupDraft(
        coin=coins[0] if coins else None,
        side=parsed.get("side"),
        entry=parsed.get("entry"),
        stop=parsed.get("stop"),
        take_profit=parsed.get("take_profit"),
        timeframe=parsed.get("timeframe"),
        thesis="Deterministic fallback draft from prompt and gathered Hyperliquid context.",
        confidence=0.35,
        assumptions=assumptions,
        risk_pct=parsed.get("risk_pct"),
        account_equity_usd=parsed.get("account_equity_usd"),
        invalidation=f"Stop at {parsed.get('stop')}" if parsed.get("stop") else "Missing stop; invalidation unavailable.",
        needs=needs,
    )


def fallback_opinion(role: str, state: dict[str, Any], reason: str = "") -> RoleOpinion:
    draft = state.get("draft")
    context = state.get("context")
    features = getattr(context, "features", {}) if context else {}
    risk = features.get("risk", {}) if isinstance(features, dict) else {}
    data_coverage = getattr(context, "data_coverage", None)
    missing_evidence = list(getattr(data_coverage, "missing_endpoints", []) or [])
    risks: list[str] = []
    recommendations: list[str] = []
    critical = False
    if not getattr(draft, "entry", None) or not getattr(draft, "stop", None):
        risks.append("Entry/stop are incomplete; cannot produce a high-confidence setup.")
        recommendations.append("Require explicit entry and stop before paper/autonomous proposal.")
        critical = True
    if role == "risk" and risk.get("risk_reward_ratio") is not None and risk["risk_reward_ratio"] < 1:
        risks.append("Risk/reward is below 1.0 based on parsed entry/stop/take-profit.")
        critical = True
    if reason:
        risks.append(f"Model fallback for {role}: {reason}")
    return RoleOpinion(
        role=role,
        stance="mixed" if not critical else "oppose",
        confidence=0.3,
        summary=f"Deterministic fallback {role} review.",
        key_points=["Review generated from parsed prompt and deterministic features."],
        risks=risks,
        recommendations=recommendations or ["Use manual confirmation before acting."],
        missing_evidence=missing_evidence,
        scorecard=RoleScorecard(
            evidence_quality=2 if missing_evidence else 3,
            directional_edge=2,
            risk_asymmetry=2,
            liquidity_quality=2,
            execution_feasibility=2,
            invalidation_quality=1 if critical else 3,
            final_score=11 if critical else 16,
            veto=critical,
            veto_reason="; ".join(risks[:2]),
        ),
        requires_revision=critical,
        critical=critical,
    )


def fallback_judge(state: dict[str, Any], reason: str = "") -> JudgeDecision:
    draft = state.get("draft")
    role_outputs = state.get("role_outputs", [])
    criticals = [item.summary for item in role_outputs if getattr(item, "critical", False)]
    missing = list(getattr(draft, "needs", []) or []) if draft else ["draft_missing"]
    model_fallbacks = [risk for item in role_outputs for risk in getattr(item, "risks", []) if "Model fallback" in risk or "model_fallback" in risk]
    if criticals:
        status = "manual_review_required"
        summary = "Critical reviewer concerns remain unresolved."
    elif missing:
        status = "needs_more_data"
        summary = "The setup is missing required trade parameters."
    elif model_fallbacks:
        status = "manual_review_required"
        summary = "Deterministic high-stakes review completed, but one or more role models timed out; use manual confirmation."
    elif draft and draft.entry and draft.stop and draft.side:
        status = "paper_ready"
        summary = "Paper proposal can be produced, but live execution remains disabled."
    else:
        status = "manual_review_required"
        summary = "Insufficient evidence for a confident proposal."
    warnings = [f"judge_model_fallback:{reason}"] if reason else []
    return _force_no_execution(
        JudgeDecision(
            status=cast(ProposalStatus, status),
            converged=True,
            revise=False,
            confidence=0.35,
            summary=summary,
            accepted_critiques=criticals,
            deferred_critiques=missing,
            final_rationale=[summary],
            final_risks=criticals,
            data_requests=[],
            data_coverage=getattr(state.get("context"), "data_coverage", None),
            final_warnings=warnings,
        ),
        state,
    )


def _force_no_execution(decision: JudgeDecision, state: dict[str, Any]) -> JudgeDecision:
    prompt = str(state.get("prompt", "")).lower()
    if any(term in prompt for term in ["autonomous", "execute", "place order", "submit order"]):
        warnings = list(decision.final_warnings)
        if "Live/autonomous exchange execution is disabled; proposal is paper/manual only." not in warnings:
            warnings.append("Live/autonomous exchange execution is disabled; proposal is paper/manual only.")
        status = decision.status if decision.status != "paper_ready" else "not_executable"
        return decision.model_copy(update={"status": status, "final_warnings": warnings, "converged": True, "revise": False})
    return decision


def _state_for_model(state: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "prompt": state.get("prompt"),
        "round": state.get("round"),
        "route": state.get("route"),
        "context": _context_for_model(state.get("context")),
        "draft": state.get("draft"),
        "role_outputs": state.get("role_outputs"),
        "judge_decision": state.get("judge_decision"),
        "data_escalation_count": state.get("data_escalation_count"),
        "data_coverage": state.get("data_coverage"),
        "data_requests": state.get("data_requests"),
        "warnings": state.get("warnings"),
        "errors": state.get("errors"),
    }
    return model_to_jsonable(allowed)


def _context_for_model(context: Any) -> dict[str, Any] | None:
    if context is None:
        return None
    features = getattr(context, "features", {}) or {}
    if not isinstance(features, dict):
        features = {}
    return {
        "features": _trim_features_for_model(features),
        "data_profiles": getattr(context, "data_profiles", []),
        "data_coverage": getattr(context, "data_coverage", None),
        "warnings": getattr(context, "warnings", []),
        "tool_summary": list(features.get("tool_summary", []))[:20],
        "timestamp_ms": getattr(context, "timestamp_ms", None),
    }


def _trim_features_for_model(features: dict[str, Any]) -> dict[str, Any]:
    trimmed = dict(features)
    # Tool outputs can contain full books, candle arrays, and account histories. The
    # deterministic feature layer already distilled those into market/candle/funding/
    # risk/execution summaries; cap verbose leaf lists so role calls stay fast and
    # structured JSON is less likely to be truncated.
    for section in ["account", "fills"]:
        value = trimmed.get(section)
        if isinstance(value, dict):
            trimmed[section] = _cap_nested_lists(value, limit=10)
    return _cap_nested_lists(trimmed, limit=20)


def _cap_nested_lists(value: Any, *, limit: int) -> Any:
    if isinstance(value, list):
        return [_cap_nested_lists(item, limit=limit) for item in value[:limit]]
    if isinstance(value, dict):
        return {key: _cap_nested_lists(inner, limit=limit) for key, inner in value.items()}
    return value


def _error_reason(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValidationError):
        text = " ".join(str(exc).split())
        if "Invalid JSON" in text:
            return "invalid_structured_json"
    text = " ".join(str(exc).split())
    if text.startswith("All configured model attempts failed or lacked credentials:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        text = type(exc).__name__
    return text[:500]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
