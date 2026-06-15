from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

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
    def __init__(self, model_gateway: ModelGateway, settings: Settings, memory_service: Any | None = None):
        self.model_gateway = model_gateway
        self.settings = settings
        self.memory_service = memory_service

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
                timeout_seconds=self._role_timeout_seconds(state),
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
                max_tokens=1400,
                context={"state": compact_context(await self._state_for_role_model(role, state))},
                timeout_seconds=self._role_timeout_seconds(state),
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
                max_tokens=1600,
                context={"state": compact_context(await self._state_for_role_model("judge", state))},
                timeout_seconds=self._role_timeout_seconds(state),
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
            return await block(role, symbol=symbol, signal_type=signal_type, market_regime=market_regime, max_items=5)
        except Exception:
            return ""

    async def _complete_structured_with_timeout(self, *args: Any, **kwargs: Any):
        # Free/dev models can hang or return malformed JSON. Keep each role on a
        # short leash and let deterministic fallbacks complete the debate rather
        # than timing out the entire graph with an unusable final answer.
        timeout_seconds = float(kwargs.pop("timeout_seconds", self._default_role_timeout_seconds()))
        return await asyncio.wait_for(self.model_gateway.complete_structured(*args, **kwargs), timeout=timeout_seconds)

    def _role_timeout_seconds(self, state: dict[str, Any]) -> float:
        route = state.get("route")
        selected = set(getattr(route, "selected_roles", []) or [])
        parallel_roles = {"quant", "research", "risk", "treasury", "execution"}
        sequential_phases = 1  # analyst/proposer
        if selected & parallel_roles:
            sequential_phases += 1  # bounded concurrent reviewer batch
        if "adversary" in selected:
            sequential_phases += 1  # adversary sees reviewer outputs
        sequential_phases += 1  # judge
        rounds = max(1, int(getattr(self.settings, "high_stakes_max_rounds", 1) or 1))
        reserve_seconds = min(45.0, max(20.0, self.settings.high_stakes_timeout_seconds * 0.15))
        budget = max(30.0, self.settings.high_stakes_timeout_seconds - reserve_seconds)
        return min(35.0, max(8.0, budget / max(1, sequential_phases * rounds)))

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
        "context": state.get("context"),
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


def _error_reason(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    text = " ".join(str(exc).split())
    if text.startswith("All configured model attempts failed or lacked credentials:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        text = type(exc).__name__
    return text[:500]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
