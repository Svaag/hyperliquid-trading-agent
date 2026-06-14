from __future__ import annotations

import asyncio
import time
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.formatting import format_trade_proposal
from hyperliquid_trading_agent.app.agent.high_stakes.json_io import model_to_jsonable
from hyperliquid_trading_agent.app.agent.high_stakes.roles import HighStakesRoleRunner, RoleCallResult
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
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.hyperliquid.validation import asset_validation_summary
from hyperliquid_trading_agent.app.metrics import DECISION_LATENCY, DECISION_RUNS
from hyperliquid_trading_agent.app.security import redact_text


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
    ):
        self.settings = settings
        self.context_builder = context_builder
        self.role_runner = role_runner
        self.repository = repository
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
            proposal = _error_proposal("High-stakes debate timed out before convergence", status="manual_review_required")
            DECISION_RUNS.labels(status=proposal.status).inc()
            DECISION_LATENCY.labels(status=proposal.status).observe(time.perf_counter() - started)
            return TradeProposalResponse(status=proposal.status, content=format_trade_proposal(proposal), proposal=proposal.model_dump(mode="json"), warnings=proposal.warnings)
        except Exception as exc:
            proposal = _error_proposal(f"High-stakes debate failed: {type(exc).__name__}")
            DECISION_RUNS.labels(status=proposal.status).inc()
            DECISION_LATENCY.labels(status=proposal.status).observe(time.perf_counter() - started)
            return TradeProposalResponse(status=proposal.status, content=format_trade_proposal(proposal), proposal=proposal.model_dump(mode="json"), warnings=proposal.warnings)

    def _build_graph(self) -> Any:
        graph: StateGraph = StateGraph(HighStakesGraphState)
        graph.add_node("triage", self._triage)
        graph.add_node("gather_context", self._gather_context)
        graph.add_node("proposer", self._proposer)
        graph.add_node("quant_review", self._review_node("quant"))
        graph.add_node("research_review", self._review_node("research"))
        graph.add_node("risk_review", self._review_node("risk"))
        graph.add_node("treasury_review", self._review_node("treasury"))
        graph.add_node("execution_review", self._review_node("execution"))
        graph.add_node("adversary_review", self._review_node("adversary"))
        graph.add_node("judge", self._judge)
        graph.add_node("gather_escalated_context", self._gather_escalated_context)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "triage")
        graph.add_edge("triage", "gather_context")
        graph.add_edge("gather_context", "proposer")
        graph.add_edge("proposer", "quant_review")
        graph.add_edge("quant_review", "research_review")
        graph.add_edge("research_review", "risk_review")
        graph.add_edge("risk_review", "treasury_review")
        graph.add_edge("treasury_review", "execution_review")
        graph.add_edge("execution_review", "adversary_review")
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
        )
        role_outputs = _replace_round_role(state.get("role_outputs", []), opinion, next_round)
        updates = {"draft": draft, "round": next_round, "role_outputs": role_outputs}
        await self._record_role(state, "analyst", next_round, result, opinion)
        await self._snapshot(_merged_state(state, updates), "proposer")
        return updates

    def _review_node(self, role: str):
        async def node(state: HighStakesGraphState) -> dict[str, Any]:
            result = await self.role_runner.review(role, dict(state))
            opinion = result.parsed if isinstance(result.parsed, RoleOpinion) else RoleOpinion(role=role, stance="error", summary="Invalid role output")
            role_outputs = _replace_round_role(state.get("role_outputs", []), opinion, int(state.get("round", 0)))
            updates = {"role_outputs": role_outputs}
            await self._record_role(state, role, int(state.get("round", 0)), result, opinion)
            await self._snapshot(_merged_state(state, updates), f"{role}_review")
            return updates

        return node

    async def _judge(self, state: HighStakesGraphState) -> dict[str, Any]:
        result = await self.role_runner.judge(dict(state))
        decision = result.parsed if isinstance(result.parsed, JudgeDecision) else JudgeDecision(status="error", summary="Invalid judge output")
        if int(state.get("round", 0)) >= self.settings.high_stakes_max_rounds and decision.revise:
            decision = decision.model_copy(update={"revise": False, "status": "manual_review_required", "converged": False})
        coverage = decision.data_coverage or state.get("data_coverage") or DataCoverage()
        decision = decision.model_copy(update={"data_coverage": coverage})
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
        proposal = self._apply_final_policy(state, proposal)
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
        "Re-check Hyperliquid mark/oracle, funding, spread, and depth immediately before acting.",
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

    deterministic_rationale = _deterministic_position_rationale(features, draft)
    if deterministic_rationale and judge.model is None:
        rationale = deterministic_rationale
    else:
        rationale = list(judge.final_rationale) or deterministic_rationale
    return TradeProposal(
        status=status,
        coin=draft.coin,
        side=draft.side,
        entry=draft.entry,
        stop=draft.stop,
        take_profit=draft.take_profit,
        timeframe=draft.timeframe,
        risk_usd=risk.get("risk_usd"),
        risk_pct=risk.get("risk_pct"),
        size_units=risk.get("size_units"),
        notional_usd=risk.get("notional_usd"),
        thesis=draft.thesis,
        invalidation=draft.invalidation or (f"Stop at {draft.stop}" if draft.stop else "Missing explicit stop/invalidation."),
        rationale=rationale,
        risks=list(judge.final_risks) + [risk for opinion in role_outputs for risk in opinion.risks[:2]],
        warnings=warnings,
        checklist=checklist,
        account_address=account_address,
        role_summaries=role_summaries,
        judge_summary=judge.summary,
        autonomous_execution_allowed=False,
        exchange_actions=[],
        tool_summary=features.get("tool_summary", []) if isinstance(features, dict) else [],
    )


def _deterministic_position_rationale(features: dict[str, Any], draft: TradeSetupDraft) -> list[str]:
    if not draft.coin or draft.entry is None or draft.stop is None:
        return []
    market = features.get("market", {}) if isinstance(features, dict) else {}
    asset = market.get(draft.coin) if isinstance(market, dict) else None
    if not isinstance(asset, dict):
        return []
    mid = asset.get("mid") or asset.get("mark")
    if mid is None:
        return []
    entry = float(draft.entry)
    stop = float(draft.stop)
    current = float(mid)
    pnl_pct = ((current - entry) / entry) * 100 if draft.side == "long" else ((entry - current) / entry) * 100
    stop_distance_pct = (abs(current - stop) / current) * 100 if current else None
    funding = asset.get("funding")
    premium = asset.get("premium")
    prev_day = asset.get("prev_day_px")
    day_change = ((current - float(prev_day)) / float(prev_day)) * 100 if prev_day else None
    lines = [
        f"Position context: {draft.coin} {draft.side or 'position'} is ~{pnl_pct:.2f}% from entry {entry:g}; stop {stop:g} is ~{stop_distance_pct:.2f}% away from current {current:g}.",
    ]
    if day_change is not None:
        lines.append(f"Tape check: current is {day_change:.2f}% vs prior day reference; this is {'constructive' if day_change > 0 else 'pressure/drag'} for a hold decision.")
    if funding is not None:
        lines.append(f"Funding is {funding}; no standalone funding squeeze signal from this snapshot.")
    if premium is not None:
        lines.append(f"Premium is {premium}; use mark/oracle divergence as a short-term perp positioning tell, not a thesis by itself.")
    lines.append("Action frame: hold only while price respects your invalidation and intraday structure; if you do not want overnight/open risk, scale/exit before the event rather than improvising below plan.")
    return lines


def _asset_validation(features: dict[str, Any], draft: TradeSetupDraft, risk: dict[str, Any]) -> str:
    market = features.get("market", {}) if isinstance(features, dict) else {}
    asset = market.get(draft.coin) if draft.coin and isinstance(market, dict) else None
    summary = asset_validation_summary(asset, draft.entry, risk.get("size_units"))
    if summary.get("status") == "asset_context_missing":
        return "asset context missing"
    return f"asset_id={summary.get('asset_id')} price_valid={summary.get('price_valid')} size_rounded={summary.get('rounded_size')}"


def _error_proposal(message: str, status: str = "error") -> TradeProposal:
    return TradeProposal(status=cast(ProposalStatus, status), judge_summary=message, warnings=[message], autonomous_execution_allowed=False, exchange_actions=[])
