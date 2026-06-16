from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

ProposalStatus = Literal[
    "paper_ready",
    "manual_review_required",
    "no_trade",
    "needs_more_data",
    "rejected_by_guardrails",
    "not_executable",
    "error",
]
RiskLevel = Literal["low", "medium", "high", "critical"]
RoleStance = Literal["support", "oppose", "mixed", "abstain", "error"]
EvidencePriority = Literal["low", "medium", "high", "critical"]
CritiqueSeverity = Literal["low", "medium", "high", "critical"]
CritiqueResolutionStatus = Literal["accepted", "rejected", "deferred"]


class EndpointEvidence(BaseModel):
    endpoint: str
    source: str
    freshness: str = "unknown"
    used_by_role: str
    summary: str = ""
    limitations: list[str] = Field(default_factory=list)


class DataCoverage(BaseModel):
    required_endpoints: list[str] = Field(default_factory=list)
    used_endpoints: list[str] = Field(default_factory=list)
    missing_endpoints: list[str] = Field(default_factory=list)
    stale_or_failed_endpoints: list[str] = Field(default_factory=list)
    coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)


class RoleScorecard(BaseModel):
    evidence_quality: int = Field(default=0, ge=0, le=5)
    directional_edge: int = Field(default=0, ge=0, le=5)
    risk_asymmetry: int = Field(default=0, ge=0, le=5)
    liquidity_quality: int = Field(default=0, ge=0, le=5)
    execution_feasibility: int = Field(default=0, ge=0, le=5)
    invalidation_quality: int = Field(default=0, ge=0, le=5)
    final_score: int = Field(default=0, ge=0, le=30)
    veto: bool = False
    veto_reason: str = ""


class DataRequest(BaseModel):
    reason: str
    endpoint_family: str
    coin: str | None = None
    address: str | None = None
    interval: str | None = None
    priority: EvidencePriority = "medium"


class CritiqueResolution(BaseModel):
    critique: str
    source_role: str
    severity: CritiqueSeverity = "medium"
    resolution: CritiqueResolutionStatus
    rationale: str


class HighStakesRoute(BaseModel):
    activate: bool = False
    forced: bool = False
    reason: str = ""
    risk_level: RiskLevel = "low"
    selected_roles: list[str] = Field(default_factory=list)
    coins: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    intent: str = "general"
    warnings: list[str] = Field(default_factory=list)


class TradeSetupDraft(BaseModel):
    coin: str | None = None
    side: Literal["long", "short"] | None = None
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    timeframe: str | None = None
    thesis: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    risk_pct: float | None = None
    account_equity_usd: float | None = None
    invalidation: str = ""
    needs: list[str] = Field(default_factory=list)


class RoleOpinion(BaseModel):
    role: str
    stance: RoleStance = "mixed"
    call_status: Literal["ok", "fallback", "abstain", "error"] = "ok"
    latency_ms: int | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    evidence: list[EndpointEvidence] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    scorecard: RoleScorecard = Field(default_factory=RoleScorecard)
    data_requests: list[DataRequest] = Field(default_factory=list)
    requires_revision: bool = False
    critical: bool = False
    model: str | None = None
    provider: str | None = None


class RiskAssessment(BaseModel):
    status: ProposalStatus = "manual_review_required"
    max_loss_usd: float | None = None
    risk_pct: float | None = None
    risk_reward_ratio: float | None = None
    position_size_units: float | None = None
    notional_usd: float | None = None
    leverage_warning: str = ""
    liquidation_warning: str = ""
    notes: list[str] = Field(default_factory=list)


class JudgeDecision(BaseModel):
    status: ProposalStatus = "manual_review_required"
    call_status: Literal["ok", "fallback", "abstain", "error"] = "ok"
    latency_ms: int | None = None
    converged: bool = False
    revise: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    accepted_critiques: list[str] = Field(default_factory=list)
    rejected_critiques: list[str] = Field(default_factory=list)
    deferred_critiques: list[str] = Field(default_factory=list)
    critique_resolutions: list[CritiqueResolution] = Field(default_factory=list)
    data_requests: list[DataRequest] = Field(default_factory=list)
    data_coverage: DataCoverage | None = None
    required_changes: list[str] = Field(default_factory=list)
    final_rationale: list[str] = Field(default_factory=list)
    final_risks: list[str] = Field(default_factory=list)
    final_warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    provider: str | None = None


class MarketContextBundle(BaseModel):
    prompt: str
    route: HighStakesRoute
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    data_profiles: list[str] = Field(default_factory=list)
    data_coverage: DataCoverage = Field(default_factory=DataCoverage)
    warnings: list[str] = Field(default_factory=list)
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class TradeProposal(BaseModel):
    status: ProposalStatus = "manual_review_required"
    coin: str | None = None
    side: Literal["long", "short"] | None = None
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    timeframe: str | None = None
    order_type: str = "paper_plan"
    risk_usd: float | None = None
    risk_pct: float | None = None
    size_units: float | None = None
    notional_usd: float | None = None
    thesis: str = ""
    invalidation: str = ""
    rationale: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    account_address: str | None = None
    role_summaries: dict[str, str] = Field(default_factory=dict)
    debate_participation: list[dict[str, Any]] = Field(default_factory=list)
    judge_summary: str = ""
    autonomous_execution_allowed: bool = False
    exchange_actions: list[dict[str, Any]] = Field(default_factory=list)
    tool_summary: list[str] = Field(default_factory=list)
    tracking_plan: dict[str, Any] | None = None
    decision_context: dict[str, Any] | None = None
    created_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class TradeProposalRequest(BaseModel):
    prompt: str
    account_address: str | None = None
    account_equity_usd: float | None = None
    risk_pct: float | None = None
    dry_run: bool = True
    force_debate: bool = True


class TradeProposalResponse(BaseModel):
    run_id: str | None = None
    proposal_id: str | None = None
    status: ProposalStatus = "manual_review_required"
    content: str
    proposal: dict[str, Any] = Field(default_factory=dict)
    judge_decision: dict[str, Any] = Field(default_factory=dict)
    rounds: int = 0
    role_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class HighStakesDecisionState(BaseModel):
    run_id: str | None = None
    prompt: str
    route: HighStakesRoute
    context: MarketContextBundle | None = None
    draft: TradeSetupDraft | None = None
    role_outputs: list[RoleOpinion] = Field(default_factory=list)
    judge_decision: JudgeDecision | None = None
    proposal: TradeProposal | None = None
    proposal_id: str | None = None
    rounds: int = 0
    data_escalation_count: int = 0
    data_requests: list[DataRequest] = Field(default_factory=list)
    data_coverage: DataCoverage = Field(default_factory=DataCoverage)
    status: ProposalStatus = "manual_review_required"
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
