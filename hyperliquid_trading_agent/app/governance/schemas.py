from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

RiskDirection = Literal[
    "tightens_risk",
    "relaxes_risk",
    "increases_exposure",
    "decreases_exposure",
    "neutral",
    "unknown",
]

MemoryStatus = Literal["candidate", "validated_advisory", "approved_policy", "deprecated", "reverted"]
DecisionExecutionMode = Literal["paper", "shadow", "manual_review", "live"]


class VersionSnapshot(BaseModel):
    """Immutable hash-addressed runtime artifact version."""

    id: str
    scope: str
    version_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    code_version: str | None = None
    created_at_ms: int
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptVersionSnapshot(BaseModel):
    """Immutable hash-addressed prompt/role-contract version."""

    id: str
    prompt_name: str
    version_hash: str
    content_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    code_version: str | None = None
    created_at_ms: int
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionContextRef(BaseModel):
    """Audit reference attached to trade/proposal decisions.

    This is intentionally descriptive only. It does not grant authority to mutate
    config, prompts, risk limits, sizing, model routes, broker permissions, or code.
    """

    decision_id: str
    run_id: str | None = None
    config_version_id: str
    risk_config_version_id: str
    prompt_version_ids: list[str] = Field(default_factory=list)
    model_route: dict[str, Any] = Field(default_factory=dict)
    injected_memory_ids: list[str] = Field(default_factory=list)
    market_snapshot_refs: list[str] = Field(default_factory=list)
    data_freshness: dict[str, Any] = Field(default_factory=dict)
    code_version: str | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskGatewayDecision(BaseModel):
    decision_id: str
    intent_id: str
    mode: DecisionExecutionMode = "paper"
    decision: Literal["allow", "reject", "halt", "tighten"]
    violations: list[dict[str, Any]] = Field(default_factory=list)
    limits_snapshot: dict[str, Any] = Field(default_factory=dict)
    market_snapshot: dict[str, Any] = Field(default_factory=dict)
    portfolio_snapshot: dict[str, Any] = Field(default_factory=dict)
    config_version_id: str | None = None
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class TradeIntent(BaseModel):
    intent_id: str
    source_type: Literal["high_stakes_proposal", "operator", "shadow"]
    source_id: str
    strategy_id: str = "autonomy_v1"
    asset_class: Literal["crypto", "equity", "unknown"] = "crypto"
    symbol: str
    venue: str
    side: Literal["long", "short"]
    order_type: Literal["paper_market", "paper_plan", "manual_plan"] = "paper_plan"
    entry: float
    stop: float
    take_profit: float | None = None
    quantity: float | None = None
    notional_usd: float | None = None
    risk_usd: float | None = None
    risk_pct: float | None = None
    confidence: float
    thesis: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    decision_context: DecisionContextRef
    execution_mode: DecisionExecutionMode = "paper"
    exchange_actions: list[dict[str, Any]] = Field(default_factory=list)


class PaperTradeOutcome(BaseModel):
    outcome_id: str
    paper_order_id: str | None = None
    paper_position_id: str | None = None
    strategy_id: str
    symbol: str
    venue: str
    side: str
    entry_px: float
    fill_px: float | None = None
    exit_px: float | None = None
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    realized_pnl_usd: float | None = None
    realized_r: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    terminal_outcome: str
    execution_assumptions: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(
        default_factory=lambda: [
            "fill assumptions",
            "slippage modeling",
            "queue position",
            "latency",
            "partial fills",
            "market impact",
            "fees",
            "funding",
            "spread behavior",
            "venue handling",
            "liquidation mechanics",
            "stale data",
            "liquidity cliffs",
            "news/event regimes",
            "recent-market overfitting",
        ]
    )
    evidence_quality: Literal["paper_simulation", "shadow", "live_observation"] = "paper_simulation"
    created_at_ms: int


class AdvisoryMemory(BaseModel):
    memory_id: str
    status: MemoryStatus
    scope: dict[str, Any] = Field(default_factory=dict)
    strategy_id: str | None = None
    symbols: list[str] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)
    regimes: list[str] = Field(default_factory=list)
    claim: str
    instruction: str
    evidence_links: list[str] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at_ms: int
    expires_at_ms: int | None = None
    allowed_contexts: list[str] = Field(default_factory=list)
    forbidden_contexts: list[str] = Field(default_factory=list)
    promotion_history: list[dict[str, Any]] = Field(default_factory=list)
    rollback_target: str | None = None


class CandidateConfigDiff(BaseModel):
    proposal_id: str
    strategy_id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    change_type: str
    current_value: dict[str, Any] = Field(default_factory=dict)
    proposed_value: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    expected_effect: str
    known_risks: list[str] = Field(default_factory=list)
    validation_required: list[str] = Field(default_factory=list)
    risk_direction: RiskDirection = "unknown"
    requires_human_approval: bool = True
    auto_apply_allowed: bool = False
    created_by: str = "unknown"
    created_at_ms: int
    status: str = "proposed"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enforce_authority_boundary(self) -> CandidateConfigDiff:
        if self.risk_direction in {"relaxes_risk", "increases_exposure"}:
            self.requires_human_approval = True
            self.auto_apply_allowed = False
        if self.auto_apply_allowed:
            raise ValueError("candidate config diffs cannot auto-apply in governance audit mode")
        if not self.evidence:
            raise ValueError("candidate config diffs must link to evidence")
        return self


class ReplayResult(BaseModel):
    replay_id: str
    proposal_id: str | None = None
    decision_id: str | None = None
    status: Literal["passed", "failed", "insufficient_data", "audit_only"] = "audit_only"
    baseline_metrics: dict[str, Any] = Field(default_factory=dict)
    candidate_metrics: dict[str, Any] = Field(default_factory=dict)
    diffs: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ShadowComparisonResult(BaseModel):
    comparison_id: str
    proposal_id: str
    status: Literal["shadow_passed", "shadow_failed", "insufficient_data", "audit_only"] = "audit_only"
    baseline_metrics: dict[str, Any] = Field(default_factory=dict)
    candidate_metrics: dict[str, Any] = Field(default_factory=dict)
    metric_deltas: dict[str, Any] = Field(default_factory=dict)
    recommendation: Literal["promote_to_review", "needs_more_evidence", "reject", "audit_only"] = "audit_only"
    created_at_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewPacket(BaseModel):
    review_packet_id: str
    proposal_id: str
    evidence_links: list[str] = Field(default_factory=list)
    affected_strategies: list[str] = Field(default_factory=list)
    affected_symbols: list[str] = Field(default_factory=list)
    affected_venues: list[str] = Field(default_factory=list)
    risk_direction: RiskDirection = "unknown"
    expected_effect: str = ""
    known_risks: list[str] = Field(default_factory=list)
    replay_results: dict[str, Any] | None = None
    shadow_results: dict[str, Any] | None = None
    reviewer_findings: list[dict[str, Any]] = Field(default_factory=list)
    approval_requirements: list[str] = Field(default_factory=list)
    rollback_plan_id: str
    created_at_ms: int


class PromotionDecision(BaseModel):
    decision_id: str
    proposal_id: str
    reviewer: str
    decision: Literal["approved", "rejected", "needs_more_evidence"]
    rationale: str
    evidence_reviewed: list[str] = Field(default_factory=list)
    tests_reviewed: list[str] = Field(default_factory=list)
    proposer_actor: str
    approver_actor: str
    change_control_id: str
    approved_contexts: list[str] = Field(default_factory=list)
    rollback_plan_id: str
    created_at_ms: int

    @model_validator(mode="after")
    def _separate_proposer_and_approver(self) -> PromotionDecision:
        if self.proposer_actor and self.approver_actor and self.proposer_actor == self.approver_actor:
            raise ValueError("the same actor cannot propose and approve a governance change")
        return self


class RollbackPlan(BaseModel):
    rollback_plan_id: str
    target_type: Literal["config", "memory", "prompt", "model_route", "risk_limit"]
    target_id: str
    previous_version_id: str
    rollback_steps: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    owner: str
    created_at_ms: int
