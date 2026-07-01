from __future__ import annotations

import asyncio
import time
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.formatting import format_trade_proposal
from hyperliquid_trading_agent.app.agent.high_stakes.json_io import model_to_jsonable
from hyperliquid_trading_agent.app.agent.high_stakes.roles import (
    HighStakesRoleRunner,
    RoleCallResult,
    fallback_draft,
    fallback_judge,
    fallback_opinion,
)
from hyperliquid_trading_agent.app.agent.high_stakes.routing import route_high_stakes
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import (
    DataCoverage,
    DataRequest,
    HighStakesRoute,
    JudgeDecision,
    MarketContextBundle,
    ProposalStatus,
    RoleOpinion,
    RoleScorecard,
    TradeProposal,
    TradeProposalRequest,
    TradeProposalResponse,
    TradeSetupDraft,
)
from hyperliquid_trading_agent.app.agent.identity_guard import guard_unsupported_public_claims
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.hyperliquid.validation import asset_validation_summary
from hyperliquid_trading_agent.app.metrics import DECISION_LATENCY, DECISION_RUNS
from hyperliquid_trading_agent.app.security import redact_text
from hyperliquid_trading_agent.app.tracking.levels import derive_position_tracking_plan, level_by_kind
from hyperliquid_trading_agent.app.tracking.schemas import PositionTrackingPlan
from hyperliquid_trading_agent.app.tracking.service import PositionTrackingService


class HighStakesGraphState(TypedDict, total=False):
    request: TradeProposalRequest
    agent_context: dict[str, Any]
    prompt: str
    route: HighStakesRoute
    run_id: str | None
    proposal_id: str | None
    context: MarketContextBundle
    draft: TradeSetupDraft
    role_outputs: list[RoleOpinion]
    judge_decision: JudgeDecision
    proposal: TradeProposal
    round: int
    data_escalation_count: int
    data_requests: list[DataRequest]
    data_coverage: DataCoverage
    warnings: list[str]
    errors: list[str]
    started_at: float


class HighStakesDebateGraph:
    def __init__(
        self,
        settings: Settings,
        context_builder: HighStakesContextBuilder,
        role_runner: HighStakesRoleRunner,
        repository: Repository | None = None,
        tracking_service: PositionTrackingService | None = None,
        decision_context_recorder: Any | None = None,
    ):
        self.settings = settings
        self.context_builder = context_builder
        self.role_runner = role_runner
        self.repository = repository
        self.tracking_service = tracking_service
        self.decision_context_recorder = decision_context_recorder
        self._compiled = self._build_graph()

    async def run(self, request: TradeProposalRequest, agent_context: dict[str, Any] | None = None) -> TradeProposalResponse:
        started = time.perf_counter()
        initial: HighStakesGraphState = {
            "request": request,
            "agent_context": agent_context or {},
            "prompt": redact_text(request.prompt),
            "round": 0,
            "data_escalation_count": 0,
            "data_requests": [],
            "data_coverage": DataCoverage(),
            "role_outputs": [],
            "warnings": [],
            "errors": [],
            "started_at": started,
        }
        try:
            state = await asyncio.wait_for(self._compiled.ainvoke(initial), timeout=self.settings.high_stakes_timeout_seconds)
            proposal = state.get("proposal") or _error_proposal("Debate graph returned no proposal")
            judge = state.get("judge_decision")
            status = proposal.status
            DECISION_RUNS.labels(status=status).inc()
            DECISION_LATENCY.labels(status=status).observe(time.perf_counter() - started)
            return TradeProposalResponse(
                run_id=state.get("run_id"),
                proposal_id=state.get("proposal_id"),
                status=status,
                content=format_trade_proposal(proposal, judge),
                proposal=proposal.model_dump(mode="json"),
                judge_decision=judge.model_dump(mode="json") if judge else {},
                rounds=int(state.get("round", 0)),
                role_count=len(state.get("role_outputs", [])),
                warnings=list(state.get("warnings", [])) + proposal.warnings,
            )
        except TimeoutError:
            return await self._deterministic_failure_response(request, agent_context, started, reason="timeout")
        except Exception as exc:
            return await self._deterministic_failure_response(request, agent_context, started, reason=type(exc).__name__)

    async def _deterministic_failure_response(
        self,
        request: TradeProposalRequest,
        agent_context: dict[str, Any] | None,
        started: float,
        *,
        reason: str,
    ) -> TradeProposalResponse:
        route = route_high_stakes(
            request.prompt,
            forced=request.force_debate,
            activation_policy=self.settings.high_stakes_activation_policy,
            max_coins=self.settings.high_stakes_max_coins,
        )
        warnings = [f"deterministic_debate_fallback:{reason}"]
        context: MarketContextBundle | None = None
        try:
            context_timeout = min(25.0, max(8.0, self.settings.high_stakes_timeout_seconds * 0.10))
            context = await asyncio.wait_for(self.context_builder.gather(request, route), timeout=context_timeout)
            warnings.extend(context.warnings)
        except Exception as exc:
            warnings.append(f"deterministic_context_unavailable:{type(exc).__name__}")

        state: HighStakesGraphState = {
            "request": request,
            "agent_context": agent_context or {},
            "prompt": redact_text(request.prompt),
            "route": route,
            "round": 0,
            "data_escalation_count": 0,
            "data_requests": [],
            "data_coverage": context.data_coverage if context else DataCoverage(),
            "role_outputs": [],
            "warnings": warnings,
            "errors": [reason],
            "started_at": started,
        }
        if context is not None:
            state["context"] = context

        draft = _merge_deterministic_setup(fallback_draft(dict(state), reason=reason), state)
        state["draft"] = draft
        role_outputs = _fallback_role_outputs(route, state, reason)
        state["role_outputs"] = role_outputs
        judge = fallback_judge(dict(state), reason=reason).model_copy(update={"call_status": "fallback", "data_coverage": state["data_coverage"]})
        state["judge_decision"] = judge
        proposal = self._apply_identity_guard(state, _build_proposal(state))
        proposal = self._apply_final_policy(state, proposal)
        proposal = await self._attach_decision_context(state, proposal)
        proposal = await self._auto_arm_tracking(state, proposal, proposal_id=None)

        DECISION_RUNS.labels(status=proposal.status).inc()
        DECISION_LATENCY.labels(status=proposal.status).observe(time.perf_counter() - started)
        return TradeProposalResponse(
            status=proposal.status,
            content=format_trade_proposal(proposal, judge),
            proposal=proposal.model_dump(mode="json"),
            judge_decision=judge.model_dump(mode="json"),
            rounds=0,
            role_count=len(role_outputs) + 1,
            warnings=proposal.warnings,
        )

    def _build_graph(self) -> Any:
        graph: StateGraph = StateGraph(HighStakesGraphState)
        graph.add_node("triage", self._triage)
        graph.add_node("gather_context", self._gather_context)
        graph.add_node("proposer", self._proposer)
        graph.add_node("parallel_reviews", self._parallel_reviews)
        graph.add_node("adversary_review", self._review_node("adversary"))
        graph.add_node("judge", self._judge)
        graph.add_node("gather_escalated_context", self._gather_escalated_context)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "triage")
        graph.add_edge("triage", "gather_context")
        graph.add_edge("gather_context", "proposer")
        graph.add_edge("proposer", "parallel_reviews")
        graph.add_edge("parallel_reviews", "adversary_review")
        graph.add_edge("adversary_review", "judge")
        graph.add_conditional_edges("judge", self._judge_route, {"escalate": "gather_escalated_context", "revise": "proposer", "finalize": "finalize"})
        graph.add_edge("gather_escalated_context", "proposer")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def _triage(self, state: HighStakesGraphState) -> dict[str, Any]:
        request = state["request"]
        route = route_high_stakes(
            request.prompt,
            forced=request.force_debate,
            activation_policy=self.settings.high_stakes_activation_policy,
            max_coins=self.settings.high_stakes_max_coins,
        )
        run_id = None
        if self.repository:
            run_id = await self.repository.create_decision_run(
                prompt=redact_text(request.prompt),
                route=route.model_dump(mode="json"),
                selected_roles=route.selected_roles,
                actor=str(state.get("agent_context", {}).get("actor") or state.get("agent_context", {}).get("source") or "api"),
            )
        updates: dict[str, Any] = {"route": route, "run_id": run_id, "warnings": list(state.get("warnings", [])) + route.warnings}
        await self._snapshot(_merged_state(state, updates), "triage")
        return updates

    async def _gather_context(self, state: HighStakesGraphState) -> dict[str, Any]:
        context = await self.context_builder.gather(state["request"], state["route"])
        updates = {"context": context, "data_coverage": context.data_coverage, "warnings": list(state.get("warnings", [])) + context.warnings}
        if self.repository and state.get("run_id"):
            await self.repository.update_decision_run_context(state.get("run_id"), context.model_dump(mode="json"))
        await self._snapshot(_merged_state(state, updates), "gather_context")
        return updates

    async def _gather_escalated_context(self, state: HighStakesGraphState) -> dict[str, Any]:
        existing = state.get("context")
        if existing is None:
            return await self._gather_context(state)
        requested = state.get("data_requests", [])
        extra = await self.context_builder.gather(state["request"], state["route"], data_requests=requested)
        context = self.context_builder.merge_contexts(existing, extra, state["request"])
        escalation_count = int(state.get("data_escalation_count", 0)) + 1
        warnings = list(state.get("warnings", [])) + extra.warnings + [f"data_escalation:{escalation_count}"]
        updates = {
            "context": context,
            "data_coverage": context.data_coverage,
            "data_escalation_count": escalation_count,
            "warnings": warnings,
        }
        if self.repository and state.get("run_id"):
            await self.repository.update_decision_run_context(state.get("run_id"), context.model_dump(mode="json"))
        await self._snapshot(_merged_state(state, updates), "gather_escalated_context")
        return updates

    async def _proposer(self, state: HighStakesGraphState) -> dict[str, Any]:
        next_round = int(state.get("round", 0)) + 1
        state_for_role: dict[str, Any] = dict(state)
        state_for_role["round"] = next_round
        result = await self.role_runner.draft_setup(state_for_role)
        draft = result.parsed if isinstance(result.parsed, TradeSetupDraft) else TradeSetupDraft()
        draft = _merge_deterministic_setup(draft, state)
        coverage = state.get("data_coverage", DataCoverage())
        opinion = RoleOpinion(
            role="analyst",
            stance="support" if not draft.needs else "mixed",
            confidence=draft.confidence,
            summary=draft.thesis or "Analyst draft produced.",
            key_points=[f"coin={draft.coin}", f"side={draft.side}", f"entry={draft.entry}", f"stop={draft.stop}"],
            risks=[f"Missing {item}" for item in draft.needs],
            recommendations=draft.needs,
            missing_evidence=list(coverage.missing_endpoints),
            scorecard=RoleScorecard(
                evidence_quality=3 if coverage.coverage_score >= 0.65 else 2,
                directional_edge=3 if draft.confidence >= 0.5 else 2,
                risk_asymmetry=3 if draft.take_profit and draft.stop else 1,
                liquidity_quality=3,
                execution_feasibility=3 if draft.entry and draft.stop else 1,
                invalidation_quality=4 if draft.stop and draft.invalidation else 1,
                final_score=18 if not draft.needs else 10,
                veto=bool({"entry", "stop", "side"} & set(draft.needs)),
                veto_reason="; ".join(draft.needs),
            ),
            requires_revision=bool(draft.needs),
            critical=bool({"entry", "stop", "side"} & set(draft.needs)),
            model=result.model,
            provider=result.provider,
            call_status=cast(Literal["ok", "fallback", "abstain", "error"], result.status),
            latency_ms=result.latency_ms,
        )
        role_outputs = _replace_round_role(state.get("role_outputs", []), opinion, next_round)
        updates = {"draft": draft, "round": next_round, "role_outputs": role_outputs}
        await self._record_role(state, "analyst", next_round, result, opinion)
        await self._snapshot(_merged_state(state, updates), "proposer")
        return updates

    async def _parallel_reviews(self, state: HighStakesGraphState) -> dict[str, Any]:
        route = state.get("route")
        selected = set(getattr(route, "selected_roles", []) or [])
        roles = [role for role in ["quant", "research", "risk", "treasury", "execution"] if role in selected]
        if not roles:
            updates = {"role_outputs": state.get("role_outputs", [])}
            await self._snapshot(_merged_state(state, updates), "parallel_reviews")
            return updates

        concurrency = max(1, int(getattr(self.settings, "high_stakes_review_concurrency", 3) or 1))
        semaphore = asyncio.Semaphore(concurrency)

        async def run_bounded(role: str) -> tuple[str, RoleCallResult, RoleOpinion]:
            async with semaphore:
                return await self._run_review_role(role, state)

        results = await asyncio.gather(*(run_bounded(role) for role in roles))
        round_index = int(state.get("round", 0))
        role_outputs = list(state.get("role_outputs", []))
        for role, result, opinion in results:
            role_outputs = _replace_round_role(role_outputs, opinion, round_index)
        await asyncio.gather(*(self._record_role(state, role, round_index, result, opinion) for role, result, opinion in results))
        updates = {"role_outputs": role_outputs}
        await self._snapshot(_merged_state(state, updates), "parallel_reviews")
        return updates

    async def _run_review_role(self, role: str, state: HighStakesGraphState) -> tuple[str, RoleCallResult, RoleOpinion]:
        result = await self.role_runner.review(role, dict(state))
        opinion = result.parsed if isinstance(result.parsed, RoleOpinion) else RoleOpinion(role=role, stance="error", summary="Invalid role output")
        opinion = opinion.model_copy(
            update={
                "role": role,
                "call_status": result.status,
                "latency_ms": result.latency_ms,
                "model": result.model or opinion.model,
                "provider": result.provider or opinion.provider,
            }
        )
        return role, result, opinion

    def _review_node(self, role: str):
        async def node(state: HighStakesGraphState) -> dict[str, Any]:
            role_name, result, opinion = await self._run_review_role(role, state)
            round_index = int(state.get("round", 0))
            role_outputs = _replace_round_role(state.get("role_outputs", []), opinion, round_index)
            updates = {"role_outputs": role_outputs}
            await self._record_role(state, role_name, round_index, result, opinion)
            await self._snapshot(_merged_state(state, updates), f"{role}_review")
            return updates

        return node

    async def _judge(self, state: HighStakesGraphState) -> dict[str, Any]:
        result = await self.role_runner.judge(dict(state))
        decision = result.parsed if isinstance(result.parsed, JudgeDecision) else JudgeDecision(status="error", summary="Invalid judge output")
        if int(state.get("round", 0)) >= self.settings.high_stakes_max_rounds and decision.revise:
            decision = decision.model_copy(update={"revise": False, "status": "manual_review_required", "converged": False})
        coverage = decision.data_coverage or state.get("data_coverage") or DataCoverage()
        decision = decision.model_copy(update={"data_coverage": coverage, "call_status": result.status, "latency_ms": result.latency_ms})
        data_requests = decision.data_requests or [request for opinion in state.get("role_outputs", []) for request in opinion.data_requests]
        updates = {"judge_decision": decision, "data_requests": data_requests, "data_coverage": coverage}
        await self._record_role(state, "judge", int(state.get("round", 0)), result, decision)
        await self._snapshot(_merged_state(state, updates), "judge")
        return updates

    def _judge_route(self, state: HighStakesGraphState) -> Literal["escalate", "revise", "finalize"]:
        decision = state.get("judge_decision")
        if decision and decision.data_requests and int(state.get("data_escalation_count", 0)) < self.settings.high_stakes_max_data_escalations:
            return "escalate"
        if decision and decision.revise and int(state.get("round", 0)) < self.settings.high_stakes_max_rounds:
            return "revise"
        return "finalize"

    async def _finalize(self, state: HighStakesGraphState) -> dict[str, Any]:
        proposal = _build_proposal(state)
        proposal = self._apply_identity_guard(state, proposal)
        proposal = self._apply_final_policy(state, proposal)
        proposal = await self._attach_decision_context(state, proposal)
        proposal_id = None
        if self.repository:
            proposal_id = await self.repository.record_trade_proposal(
                run_id=state.get("run_id"),
                status=proposal.status,
                coin=proposal.coin,
                side=proposal.side,
                proposal=proposal.model_dump(mode="json"),
                content=format_trade_proposal(proposal, state.get("judge_decision")),
            )
        proposal = await self._auto_arm_tracking(state, proposal, proposal_id)
        if self.repository:
            if proposal_id:
                await self.repository.update_trade_proposal(
                    proposal_id,
                    proposal=proposal.model_dump(mode="json"),
                    content=format_trade_proposal(proposal, state.get("judge_decision")),
                )
            if state.get("run_id"):
                await self.repository.complete_decision_run(
                    state.get("run_id"),
                    status=proposal.status,
                    round_count=int(state.get("round", 0)),
                    final_summary=proposal.judge_summary,
                    proposal_id=proposal_id,
                )
        updates = {"proposal": proposal, "proposal_id": proposal_id}
        await self._snapshot(_merged_state(state, updates), "finalize")
        return updates

    async def _attach_decision_context(self, state: HighStakesGraphState, proposal: TradeProposal) -> TradeProposal:
        if proposal.decision_context:
            return proposal
        recorder = self.decision_context_recorder
        if recorder is None:
            return proposal
        route = state.get("route")
        selected_roles = list(getattr(route, "selected_roles", []) or [])
        style = self.settings.high_stakes_prompt_style
        prompt_names: list[str] = []
        for role in selected_roles:
            prompt_names.extend([
                f"high_stakes.{role}.{style}.system",
                f"high_stakes.{role}.{style}.user",
                f"role_contract.{role}",
            ])
        context = recorder.new_decision_context(
            run_id=state.get("run_id"),
            source_type="high_stakes_proposal",
            source_id=None,
            prompt_names=prompt_names or None,
            market_snapshot_refs=[f"high_stakes_context:{getattr(state.get('context'), 'timestamp_ms', proposal.created_at_ms)}"],
            data_freshness={
                "context_timestamp_ms": getattr(state.get("context"), "timestamp_ms", None),
                "data_coverage_score": getattr(state.get("data_coverage"), "coverage_score", None),
            },
            metadata={
                "coin": proposal.coin,
                "side": proposal.side,
                "status": proposal.status,
                "paper_only": True,
            },
        )
        await recorder.record_decision_context(context, source_type="high_stakes_proposal", source_id=state.get("run_id"))
        return proposal.model_copy(update={"decision_context": context.model_dump(mode="json")})

    async def _auto_arm_tracking(self, state: HighStakesGraphState, proposal: TradeProposal, proposal_id: str | None) -> TradeProposal:
        if not proposal.tracking_plan:
            return proposal
        try:
            plan = PositionTrackingPlan.model_validate(proposal.tracking_plan)
        except Exception:
            return proposal
        metadata = dict(plan.metadata)
        if self.tracking_service is None:
            metadata["auto_arm_status"] = "not_armed:no_tracking_service"
            return proposal.model_copy(update={"tracking_plan": plan.model_copy(update={"metadata": metadata}).model_dump(mode="json")})
        tracker_id = await self.tracking_service.auto_arm(plan, proposal_id=proposal_id, run_id=state.get("run_id"))
        if tracker_id:
            metadata.update({"auto_arm_status": "armed", "tracker_id": tracker_id, "proposal_id": proposal_id})
        else:
            reason = str(getattr(self.tracking_service, "last_auto_arm_reason", "") or "unknown")
            metadata["auto_arm_status"] = f"not_armed:{reason}"
        updated = plan.model_copy(update={"id": tracker_id or plan.id, "proposal_id": proposal_id or plan.proposal_id, "run_id": state.get("run_id") or plan.run_id, "metadata": metadata})
        return proposal.model_copy(update={"tracking_plan": updated.model_dump(mode="json")})

    def _apply_final_policy(self, state: HighStakesGraphState, proposal: TradeProposal) -> TradeProposal:
        prompt = str(state.get("prompt", "")).lower()
        warnings = list(proposal.warnings)
        if (
            self.settings.high_stakes_require_account_for_autonomous
            and any(term in prompt for term in ["autonomous", "execute", "place order", "submit order"])
            and not proposal.account_address
        ):
            warnings.append("Configured policy requires an allowlisted account address for autonomous/execution-style proposals.")
            return proposal.model_copy(update={"status": "manual_review_required", "warnings": warnings})
        coverage = state.get("data_coverage")
        unresolved_data = bool(state.get("data_requests")) and int(state.get("data_escalation_count", 0)) >= self.settings.high_stakes_max_data_escalations
        if proposal.status == "paper_ready" and coverage and coverage.coverage_score < 0.65:
            warnings.append("Paper-ready status downgraded because endpoint coverage is below institutional threshold.")
            return proposal.model_copy(update={"status": "manual_review_required", "warnings": warnings})
        if proposal.status == "paper_ready" and unresolved_data:
            warnings.append("Paper-ready status downgraded because Judge/roles still requested unresolved data after escalation cap.")
            return proposal.model_copy(update={"status": "needs_more_data", "warnings": warnings})
        return proposal

    def _apply_identity_guard(self, state: HighStakesGraphState, proposal: TradeProposal) -> TradeProposal:
        context = state.get("context")
        tool_results = context.tool_results if context else []
        prompt = str(state.get("prompt") or "")
        public_text = "\n".join(
            [
                proposal.judge_summary,
                proposal.thesis,
                proposal.invalidation,
                *proposal.rationale,
                *proposal.risks,
            ]
        )
        verdict = guard_unsupported_public_claims(public_text, tool_results, prompt=prompt)
        if not verdict.blocked:
            return proposal
        safe_rationale = [
            item
            for item in proposal.rationale
            if not guard_unsupported_public_claims(str(item), tool_results, prompt=prompt).blocked
        ]
        safe_risks = [
            item
            for item in proposal.risks
            if not guard_unsupported_public_claims(str(item), tool_results, prompt=prompt).blocked
        ]
        warnings = list(proposal.warnings)
        warnings.append(verdict.warning)
        status: ProposalStatus = "manual_review_required" if proposal.status == "paper_ready" else proposal.status
        return proposal.model_copy(
            update={
                "status": status,
                "thesis": verdict.correction,
                "judge_summary": "Unsupported token-identity or catalyst claim removed; manual review required.",
                "rationale": [verdict.correction, *safe_rationale],
                "risks": safe_risks,
                "warnings": warnings,
            }
        )

    async def _record_role(
        self,
        state: HighStakesGraphState,
        role: str,
        round_index: int,
        result: RoleCallResult,
        parsed: Any,
    ) -> None:
        if not self.repository or not state.get("run_id"):
            return
        await self.repository.record_decision_role_output(
            run_id=state.get("run_id"),
            role=role,
            round_index=round_index,
            model=result.model,
            provider=result.provider,
            status=result.status,
            output_json=model_to_jsonable(parsed),
            raw_content=result.raw_content[:8000],
            latency_ms=result.latency_ms,
        )

    async def _snapshot(self, state: dict[str, Any], node: str) -> None:
        if not self.repository or not state.get("run_id"):
            return
        await self.repository.record_decision_state_snapshot(
            run_id=state.get("run_id"),
            round_index=int(state.get("round", 0)),
            node=node,
            state_json=model_to_jsonable({key: value for key, value in state.items() if key != "request"}),
        )


def _merged_state(state: HighStakesGraphState, updates: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(state)
    merged.update(updates)
    return merged


def _replace_round_role(outputs: list[RoleOpinion], opinion: RoleOpinion, round_index: int) -> list[RoleOpinion]:
    # Keep a full audit trail in DB; in-state, keep latest opinion per role per round for the judge context.
    kept = [item for item in outputs if not (item.role == opinion.role and getattr(item, "round_index", round_index) == round_index)]
    kept.append(opinion)
    return kept


def _fallback_role_outputs(route: HighStakesRoute, state: HighStakesGraphState, reason: str) -> list[RoleOpinion]:
    selected = set(route.selected_roles or [])
    ordered = ["analyst", "quant", "research", "risk", "treasury", "execution", "adversary"]
    outputs: list[RoleOpinion] = []
    for role in ordered:
        if role != "analyst" and role not in selected:
            continue
        opinion = fallback_opinion(role, dict(state), reason=reason).model_copy(update={"call_status": "fallback"})
        if role == "analyst":
            draft = state.get("draft")
            opinion = opinion.model_copy(
                update={
                    "summary": "",
                    "key_points": [
                        f"coin={getattr(draft, 'coin', None)}",
                        f"side={getattr(draft, 'side', None)}",
                        f"entry={getattr(draft, 'entry', None)}",
                        f"stop={getattr(draft, 'stop', None)}",
                    ],
                }
            )
        outputs.append(opinion)
    return outputs


def _merge_deterministic_setup(draft: TradeSetupDraft, state: HighStakesGraphState) -> TradeSetupDraft:
    context = state.get("context")
    features = context.features if context else {}
    parsed = features.get("parsed_setup", {}) if isinstance(features, dict) else {}
    route = state.get("route")
    coins = getattr(route, "coins", []) or []
    updates: dict[str, Any] = {}
    if coins and (not draft.coin or str(draft.coin).upper() not in {str(coin).upper() for coin in coins}):
        updates["coin"] = coins[0]
    for field_name in ["side", "entry", "stop", "take_profit", "timeframe", "risk_pct", "account_equity_usd"]:
        value = parsed.get(field_name)
        if value is not None:
            updates[field_name] = value
    if parsed.get("stop") is not None:
        updates["invalidation"] = f"Stop at {parsed.get('stop')}"
    merged = draft.model_copy(update=updates) if updates else draft
    needs = []
    if merged.entry is None:
        needs.append("entry")
    if merged.stop is None:
        needs.append("stop")
    if merged.side is None:
        needs.append("side")
    return merged.model_copy(update={"needs": needs})


def _debate_participation(role_outputs: list[RoleOpinion], judge: JudgeDecision) -> list[dict[str, Any]]:
    order = ["analyst", "quant", "research", "risk", "treasury", "execution", "adversary"]
    by_role = {item.role: item for item in role_outputs}
    rows: list[dict[str, Any]] = []
    for role in order:
        opinion = by_role.get(role)
        if opinion is None:
            rows.append({"role": role, "status": "not_run", "summary": "Role did not run."})
            continue
        rows.append(
            {
                "role": role,
                "status": opinion.call_status,
                "model": opinion.model,
                "provider": opinion.provider,
                "latency_ms": opinion.latency_ms,
                "summary": opinion.summary,
                "fallback_reason": _role_fallback_reason(opinion),
            }
        )
    rows.append(
        {
            "role": "judge",
            "status": judge.call_status,
            "model": judge.model,
            "provider": judge.provider,
            "latency_ms": judge.latency_ms,
            "summary": judge.summary,
            "fallback_reason": _judge_fallback_reason(judge),
        }
    )
    return rows


def _role_fallback_reason(opinion: RoleOpinion) -> str:
    for item in list(opinion.risks) + list(opinion.recommendations):
        text = str(item)
        if "Model fallback" in text:
            return text.split(":", 1)[1].strip() if ":" in text else text
        if "model_fallback" in text:
            return text.split(":", 1)[1].strip() if ":" in text else text
    return ""


def _judge_fallback_reason(judge: JudgeDecision) -> str:
    for item in judge.final_warnings:
        text = str(item)
        if text.startswith("judge_model_fallback:"):
            return text.split(":", 1)[1].strip()
    return ""


def _build_proposal(state: HighStakesGraphState) -> TradeProposal:
    draft = state.get("draft") or TradeSetupDraft()
    judge = state.get("judge_decision") or JudgeDecision(status="manual_review_required", summary="Judge did not produce a decision.")
    context = state.get("context")
    features = context.features if context else {}
    risk = features.get("risk", {}) if isinstance(features, dict) else {}
    parsed = features.get("parsed_setup", {}) if isinstance(features, dict) else {}
    account_address = parsed.get("account_address")
    role_outputs = state.get("role_outputs", [])
    role_summaries = {opinion.role: opinion.summary for opinion in role_outputs}
    debate_participation = _debate_participation(role_outputs, judge)
    coverage = context.data_coverage if context else DataCoverage()
    warnings = list(state.get("warnings", [])) + list(judge.final_warnings)
    if coverage.missing_endpoints:
        warnings.append(f"Missing endpoint evidence: {', '.join(coverage.missing_endpoints[:8])}")
    status = judge.status
    if status == "paper_ready" and not (draft.side and draft.entry and draft.stop):
        status = "needs_more_data"
        warnings.append("Paper-ready status downgraded because side/entry/stop are incomplete.")

    checklist = [
        "Manual confirmation required; this service does not sign or submit orders.",
        "Confirm stop, invalidation, and max loss outside Discord/LLM context.",
    ]
    validation = _asset_validation(features, draft, risk)
    if validation:
        checklist.append(f"Hyperliquid validation: {validation}")
    execution = features.get("execution", {}) if isinstance(features, dict) else {}
    if execution:
        checklist.append(
            f"Execution readiness: asset_id={execution.get('asset_id')} spread_bps={execution.get('spread_bps')} "
            f"top_depth=${execution.get('top_depth_notional')} est_slippage_bps={execution.get('estimated_slippage_bps')}"
        )
    checklist.append(f"Endpoint coverage: {coverage.coverage_score:.0%} ({len(coverage.used_endpoints)}/{len(coverage.required_endpoints)} endpoints used).")

    tracking_plan = derive_position_tracking_plan(
        coin=draft.coin,
        side=draft.side,
        entry=draft.entry,
        stop=draft.stop,
        take_profit=draft.take_profit,
        features=features,
        run_id=state.get("run_id"),
        agent_context=state.get("agent_context", {}),
    )
    deterministic_rationale = _deterministic_position_rationale(features, draft, tracking_plan)
    if deterministic_rationale and draft.coin and draft.entry is not None and draft.stop is not None:
        rationale = deterministic_rationale
    else:
        rationale = list(judge.final_rationale) or deterministic_rationale
    has_explicit_equity = not bool(risk.get("equity_is_assumed", False))
    has_explicit_risk_pct = not bool(risk.get("risk_pct_is_assumed", False))
    return TradeProposal(
        status=status,
        coin=draft.coin,
        side=draft.side,
        entry=draft.entry,
        stop=draft.stop,
        take_profit=draft.take_profit,
        timeframe=draft.timeframe,
        risk_usd=risk.get("risk_usd") if has_explicit_equity else None,
        risk_pct=risk.get("risk_pct") if has_explicit_risk_pct else None,
        size_units=risk.get("size_units") if has_explicit_equity else None,
        notional_usd=risk.get("notional_usd") if has_explicit_equity else None,
        thesis=draft.thesis,
        invalidation=draft.invalidation or (f"Stop at {draft.stop}" if draft.stop else "Missing explicit stop/invalidation."),
        rationale=rationale,
        risks=list(judge.final_risks) + [risk for opinion in role_outputs for risk in opinion.risks[:2]],
        warnings=warnings,
        checklist=checklist,
        account_address=account_address,
        role_summaries=role_summaries,
        debate_participation=debate_participation,
        judge_summary=judge.summary,
        autonomous_execution_allowed=False,
        exchange_actions=[],
        tool_summary=features.get("tool_summary", []) if isinstance(features, dict) else [],
        tracking_plan=tracking_plan.model_dump(mode="json") if tracking_plan else None,
    )


def _deterministic_position_rationale(features: dict[str, Any], draft: TradeSetupDraft, tracking_plan: PositionTrackingPlan | None = None) -> list[str]:
    if not draft.coin or draft.entry is None or draft.stop is None:
        return []
    market = features.get("market", {}) if isinstance(features, dict) else {}
    asset = _feature_section_for_coin(market, draft.coin) if isinstance(market, dict) else None
    if not isinstance(asset, dict):
        return []
    mid = asset.get("mid") or asset.get("mark")
    if mid is None:
        return []
    entry = float(draft.entry)
    stop = float(draft.stop)
    current = float(mid)
    is_long = draft.side != "short"
    pnl_pct = ((current - entry) / entry) * 100 if is_long else ((entry - current) / entry) * 100
    stop_distance_pct = (abs(current - stop) / current) * 100 if current else None
    candles_by_coin = features.get("candles", {}) if isinstance(features, dict) else {}
    candles = _feature_section_for_coin(candles_by_coin, draft.coin) if isinstance(candles_by_coin, dict) else {}
    recent_support = _float_or_none(candles.get("recent_support"))
    recent_resistance = _float_or_none(candles.get("recent_resistance"))
    recent_change_pct = _float_or_none(candles.get("recent_change_pct"))
    last_3_change_pct = _float_or_none(candles.get("last_3_change_pct"))
    atr_pct = _float_or_none(candles.get("atr_pct"))
    funding = _float_or_none(asset.get("funding"))
    mark_oracle_bps = _float_or_none(asset.get("mark_oracle_divergence_bps"))
    prev_day = asset.get("prev_day_px")
    day_change = ((current - float(prev_day)) / float(prev_day)) * 100 if prev_day else None
    structure_ok = True
    if is_long and recent_support is not None:
        structure_ok = current >= recent_support
    elif not is_long and recent_resistance is not None:
        structure_ok = current <= recent_resistance
    accelerating_lower = bool(is_long and ((last_3_change_pct is not None and last_3_change_pct < -0.75) or (recent_change_pct is not None and recent_change_pct < -1.5)))
    accelerating_higher = bool((not is_long) and ((last_3_change_pct is not None and last_3_change_pct > 0.75) or (recent_change_pct is not None and recent_change_pct > 1.5)))

    lines = [
        f"Position: {draft.coin} {draft.side or 'position'} is {_fmt_pct(pnl_pct)} from entry {entry:g}; hard stop {stop:g} is {_fmt_abs_pct(stop_distance_pct)} away from current {current:g}.",
    ]
    if day_change is not None:
        day_bias = "constructive while price holds structure" if day_change > 0 else "a drag until price reclaims structure"
        lines.append(f"Tape vs prior day: {_fmt_pct(day_change)}. That is {day_bias}.")
    if tracking_plan is not None and tracking_plan.levels:
        hard = level_by_kind(tracking_plan, "hard_stop")
        technical = level_by_kind(tracking_plan, "technical_exit")
        trim = level_by_kind(tracking_plan, "entry_trim")
        reclaim = level_by_kind(tracking_plan, "entry_reclaim")
        resistance = level_by_kind(tracking_plan, "resistance_confirm")
        support = level_by_kind(tracking_plan, "support_confirm")
        if is_long:
            structure_levels = []
            if technical:
                structure_levels.append(f"technical exit ≈ {technical.price:g}")
            if trim:
                structure_levels.append(f"entry trim/caution ≈ {trim.price:g}")
            if reclaim:
                structure_levels.append(f"entry reclaim ≈ {reclaim.price:g}")
            if resistance:
                structure_levels.append(f"resistance confirmation ≈ {resistance.price:g}")
            if structure_levels:
                lines.append(
                    f"Intraday structure: {'; '.join(structure_levels)}. Structure is {'still intact' if structure_ok else 'already damaged'}; "
                    f"momentum is {'accelerating lower' if accelerating_lower else 'not accelerating lower'} on the sampled candles."
                )
            management = []
            if technical and hard:
                management.append(f"below {technical.price:g} is the technical reduce/exit trigger before hard stop {hard.price:g}")
            elif hard:
                management.append(f"hard invalidation remains {hard.price:g}")
            if trim:
                management.append(f"losing {trim.price:g} is a trim/caution level")
            if reclaim:
                management.append(f"reclaim/hold above {reclaim.price:g} improves the hold case")
            if resistance:
                management.append(f"push through {resistance.price:g} confirms momentum")
            if management:
                lines.append(f"Trade management: {'; '.join(management)}.")
        else:
            structure_levels = []
            if technical:
                structure_levels.append(f"technical exit ≈ {technical.price:g}")
            if trim:
                structure_levels.append(f"entry trim/caution ≈ {trim.price:g}")
            if reclaim:
                structure_levels.append(f"entry reclaim ≈ {reclaim.price:g}")
            if support:
                structure_levels.append(f"support confirmation ≈ {support.price:g}")
            if structure_levels:
                lines.append(
                    f"Intraday structure: {'; '.join(structure_levels)}. Structure is {'still intact' if structure_ok else 'already damaged'}; "
                    f"momentum is {'accelerating higher' if accelerating_higher else 'not accelerating higher'} on the sampled candles."
                )
            management = []
            if technical and hard:
                management.append(f"above {technical.price:g} is the technical reduce/exit trigger before hard stop {hard.price:g}")
            elif hard:
                management.append(f"hard invalidation remains {hard.price:g}")
            if trim:
                management.append(f"crossing back through {trim.price:g} is a trim/caution level")
            if reclaim:
                management.append(f"reclaim/hold below {reclaim.price:g} improves the hold case")
            if support:
                management.append(f"break below {support.price:g} confirms momentum")
            if management:
                lines.append(f"Trade management: {'; '.join(management)}.")
    if funding is not None:
        lines.append(f"Funding: {_fmt_pct(funding * 100, decimals=4)}/hr (~{_fmt_pct(funding * 24 * 100, decimals=3)}/day); this is small, not a decisive carry/squeeze signal.")
    if mark_oracle_bps is not None:
        lines.append(f"Mark/oracle: {_fmt_bps(mark_oracle_bps)} divergence; {'flat enough to ignore' if abs(mark_oracle_bps) < 2 else 'watch this as a perp positioning tell'}.")
    if atr_pct is not None and stop_distance_pct is not None:
        lines.append(f"Volatility context: stop distance is ~{stop_distance_pct / atr_pct:.1f}x the sampled ATR proxy, so the stop is {'outside ordinary noise' if stop_distance_pct / atr_pct > 1.5 else 'inside normal noise'}.")
    return lines


def _fmt_pct(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "unknown"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def _fmt_abs_pct(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "unknown"
    return f"{abs(value):.{decimals}f}%"


def _fmt_bps(value: float | None) -> str:
    if value is None:
        return "unknown"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} bps"


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _asset_validation(features: dict[str, Any], draft: TradeSetupDraft, risk: dict[str, Any]) -> str:
    market = features.get("market", {}) if isinstance(features, dict) else {}
    asset = _feature_section_for_coin(market, draft.coin) if draft.coin and isinstance(market, dict) else None
    summary = asset_validation_summary(asset, draft.entry, risk.get("size_units"))
    if summary.get("status") == "asset_context_missing":
        return "asset context missing"
    return f"asset_id={summary.get('asset_id')} price_valid={summary.get('price_valid')} size_rounded={summary.get('rounded_size')}"


def _feature_section_for_coin(section: Any, coin: str | None) -> dict[str, Any]:
    if not coin or not isinstance(section, dict):
        return {}
    target = str(coin).upper()
    for key, value in section.items():
        if isinstance(value, dict) and _coin_matches_feature(target, str(key), value):
            return value
    if len(section) == 1:
        only_value = next(iter(section.values()))
        return only_value if isinstance(only_value, dict) else {}
    return {}


def _coin_matches_feature(target: str, key: str, value: dict[str, Any]) -> bool:
    candidates = {key.upper(), key.upper().split(":", 1)[-1]}
    for field in ["coin", "query_symbol"]:
        raw = value.get(field)
        if raw is None:
            continue
        text = str(raw).upper()
        candidates.add(text)
        candidates.add(text.split(":", 1)[-1])
    return target in candidates or target.split(":", 1)[-1] in candidates


def _error_proposal(message: str, status: str = "error") -> TradeProposal:
    return TradeProposal(status=cast(ProposalStatus, status), judge_summary=message, warnings=[message], autonomous_execution_allowed=False, exchange_actions=[])
