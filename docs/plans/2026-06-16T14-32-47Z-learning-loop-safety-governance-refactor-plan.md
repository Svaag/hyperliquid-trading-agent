---
created: 2026-06-16T14:32:47.664Z
source: pi-plan-mode
status: accepted-for-execution
---

# Learning Loop Safety + Governance Refactor Plan

## 1. Executive summary

The current system is **not a reckless self-authorizing trading bot**. It already has strong safety foundations:

- Hyperliquid exchange actions are disabled by config validation.
- Alpaca live trading is disabled by config validation.
- Autonomy is paper/signoff only.
- Tuning proposals are observe-and-recommend only.
- Newswire tradability gates never allow auto-trading.
- High-stakes debate produces proposals, not signed orders.
- Memory already has candidate/shadow/active concepts and excludes risk/execution/treasury by default.

The main improvement is **not a rewrite**. The highest-leverage plan is to harden the authority boundaries around the existing working loop:

> Let learning artifacts recommend changes; never let them silently mutate execution-affecting behavior.

Primary changes:

1. Add explicit governance schemas for config diffs, review packets, approvals, rollback, memory context permissions, and risk direction.
2. Tighten memory lifecycle semantics so “active” memories become **validated advisory** by default, not execution policy.
3. Add deterministic final risk gateway wrapping paper/live-intent paths.
4. Add decision-context audit records so every trade/proposal can be replayed with config version, prompt version, model route, memory IDs, market snapshot, and risk decision.
5. Add a strict promotion pipeline: observe → diagnose → propose → replay/backtest → shadow → review → human approval → canary → rollout → monitor → rollback.
6. Keep current runtime behavior initially; enforce new gates incrementally behind additive migration phases.

## Implementation Steps

1. Add governance/audit schemas and version snapshots around the existing loop.
2. Harden memory lifecycle and context injection permissions.
3. Replace loose tuning proposal payloads with structured candidate config diffs.
4. Add deterministic risk gateway as a final paper/live-intent gate.
5. Add replay/shadow comparison services for candidate diffs.
6. Add review packet, promotion decision, and rollback workflow.
7. Add operator API/CLI commands for proposals, memories, config versions, replay, freeze, and explain.
8. Add safety invariant tests and replay tests.
9. Migrate in phases with audit-only mode first, then enforcement.

---

## 2. Current architecture map

| Component | Current implementation | Authority / mutations | Live execution effect | Deterministic or agentic | Disposition |
|---|---|---:|---:|---|---|
| Runtime app / lifecycle | `hyperliquid_trading_agent/app/main.py:create_app`, `lifespan` wires repository, Hyperliquid, model gateway, autonomy, memory, reports, tuning, tracking, newswire, Discord | Starts background services, exposes APIs | Live execution disabled | Deterministic orchestration | **Keep but harden** |
| Config | `app/config.py:Settings` env/.env settings; validators reject `HYPERLIQUID_EXCHANGE_ENABLED=true` and `ALPACA_TRADING_ENABLED=true` | Runtime config from env only | Could affect runtime behavior, but learning does not write it | Deterministic | **Keep but harden** with config version snapshots |
| Hyperliquid data ingestion | `app/hyperliquid/client.py` read-only `/info`; `ws_worker.py` allMids | Reads market/account public data; no exchange endpoint | None | Deterministic | **Keep as-is** |
| TradFi data ingestion | `app/tradfi/alpaca_provider.py`; uses Alpaca data clients and `TradingClient` only for calendar/assets | Reads quotes/bars/options/corp actions/calendar/assets | No order submission | Deterministic | **Keep but harden** read-only adapter boundary |
| Newswire | `app/newswire/service.py`, `riskgate.py`, schemas | Ingests/normalizes/news bus; halt gate sets `allow_auto_trade=False` | None | Deterministic-first, optional LLM enrichment | **Keep as-is / harden audit** |
| Market map | `app/autonomy/market_map.py`, `orderflow.py`, `levels.py` | Maintains in-memory market state and persists observations | Indirectly affects signal generation | Deterministic | **Keep as-is** |
| Signal generation | `app/autonomy/signals.py:SignalEngine` | Generates `TradeSignal`; uses static settings thresholds and deterministic scoring | Paper signals only | Deterministic, optional model insight | **Keep but harden** with final risk gateway and decision context |
| Model insight | `signals.py:maybe_attach_model_insight` | Adds `model_insight` to signal using research memory | Advisory only; no execution | Agentic | **Keep but harden** memory ID audit |
| High-stakes debate | `app/agent/high_stakes/graph.py`, `roles.py`, `schemas.py` | Produces `TradeProposal`, persists decision runs/role outputs/snapshots | No exchange actions; can auto-arm tracking | Agentic with deterministic fallback | **Keep but harden** memory context + audit |
| Prompt management | `app/agent/prompts.py`, `app/agent/high_stakes/prompts.py`, `role_contracts.py` | Static code prompts | Affects recommendations, not live execution today | Static | **Keep but harden** prompt version hashes |
| Paper crypto portfolio | `app/autonomy/portfolio.py:PaperPortfolioService` | Creates paper orders/fills/positions after approval; closes paper stops/TP; flip closes paper opposing position before second approval | Paper only | Deterministic | **Keep but harden** with final risk gateway + paper outcome caveats |
| Paper equity portfolio | `app/tradfi/paper/simulator.py` | Creates equity paper orders/fills/positions | Paper only | Deterministic | **Keep but harden** |
| Legacy paper calculator | `app/paper/simulator.py` | Calculates paper sizing for `/ask` tools | Paper idea only | Deterministic | **Keep as-is** |
| Shadow / outcome evaluation | `app/autonomy/evaluation.py`, `event_evaluation.py` | Tracks signal/event outcomes, MFE/MAE, marks, terminal outcome | Feeds reports/memory/proposals only | Deterministic | **Keep but harden** paper-evidence caveats |
| Memory | `app/autonomy/memory.py` + DB tables `memory_observations`, `candidate_lessons`, `shadow_role_lessons`, `role_lessons`, `operator_output_lessons` | Persists observations/candidates/shadow/active lessons; injects allowed active role memories into prompts | Can influence recommendations | Mixed: deterministic promotion, text injected into LLM prompts | **Refactor semantics, not storage** |
| Tuning proposals | `app/autonomy/tuning.py` + `tuning_proposals` | Creates observe-only proposed diffs; status changes via API | No auto-apply | Deterministic | **Refactor schema** |
| Reports / Token Capital | `app/autonomy/reports.py` | Generates reports, scorecards, proposal summaries | Advisory only | Deterministic | **Keep but harden evidence links** |
| Position tracking | `app/tracking/service.py`, `levels.py` | Auto-arms deterministic live price alerts from proposals | Alerts only; no orders | Deterministic | **Keep as-is / harden audit** |
| Operator interface | FastAPI routes in `main.py`, Discord commands in `discord_bot.py`, `autonomy/discord.py` | Approve/reject paper signals, pause/resume, promote/reject memories, mark proposals reviewed | Paper mutations and advisory status mutations | Deterministic command handling | **Keep but harden auth/audit/RBAC** |
| Storage | `app/db/models.py`, `repository.py`, Alembic through `0008` | PostgreSQL source of truth; no vector DB | Stores all audit/learning/paper data | Deterministic | **Keep but extend** |
| Logging / audit | `Repository.record_audit_event`, `record_autonomy_event`, tool calls, decision runs | Best-effort structured events | Audit only | Deterministic | **Keep but harden** decision-context completeness |
| Deployment | Docker, FastAPI, env config | Runtime service | Live disabled | Deterministic | **Keep** |

---

## 3. Current strengths to preserve

1. **Live execution is structurally absent**
   - `HyperliquidClient` intentionally implements `/info` only.
   - `Settings.hyperliquid_exchange_enabled` validator rejects true.
   - `Settings.alpaca_trading_enabled` validator rejects true.
   - High-stakes `TradeProposal.exchange_actions=[]`.

2. **Working paper/signoff loop**
   - `AutonomousTradingLoopService` posts signals and requires approval.
   - `PaperPortfolioService` creates paper orders/fills/positions only.
   - Discord/API approval flows are already operational.

3. **Deterministic signal and portfolio controls**
   - `SignalEngine.risk_vetoes` rejects duplicates, poor RR, wide spread, thin depth.
   - `PaperPortfolioService._sized_quantity` enforces risk %, gross leverage, single-name exposure.
   - Equity paper simulator enforces gross leverage and single-name caps.

4. **Strong learning primitives already exist**
   - Signal evaluation, event evaluation, memory observations, candidate lessons, shadow lessons, reports, Token Capital, tuning proposals.

5. **Good audit foundation**
   - Decision runs, role outputs, state snapshots, tool calls, trade proposals, autonomy events, paper orders/fills/positions are persisted.

6. **Newswire is safety-aware**
   - Halt-state gate always sets `allow_auto_trade=False`.

7. **Role separation already exists logically**
   - Analyst/quant/research/risk/treasury/execution/adversary/judge roles are distinct prompts/model routes in high-stakes debate.

---

## 4. High-risk gaps

### 4.1 Memory status semantics are too loose

Current status words:

- `candidate_lessons.status`: `candidate`, `shadow`, `promoted`, `rejected`, `expired`
- `role_lessons.validation_status`: `active`, `needs_human_review`, `shadow`, `archived`, `expired`, `rejected`

Problem:

- `active` currently means “injectable advisory prompt memory,” not necessarily “approved policy.”
- This is acceptable while live execution is disabled, but unsafe as a reusable self-improvement pattern.

Required fix:

- Introduce explicit lifecycle:
  - `candidate`
  - `validated_advisory`
  - `approved_policy`
  - `deprecated`
  - `reverted`

### 4.2 Advisory memory can enter proposal-producing contexts

Current default:

```env
AUTONOMY_MEMORY_PROMPT_ROLES=analyst,quant,research,adversary,judge
```

Current injection path:

- `HighStakesRoleRunner._state_for_role_model`
- `MemoryService.memory_block_for_role`

Risk:

- Analyst/judge memories can alter recommendations.
- Still paper/manual today, but this needs explicit context permission.

Required fix:

- Memory records must carry `allowed_contexts` and `forbidden_contexts`.
- Candidate and validated advisory memories must be blocked from execution/risk/live contexts.
- Default validated advisory injection should be limited to:
  - `research`
  - `quant`
  - `adversary`
  - `reviewer`
  - `shadow`
- Analyst/judge consumption should require `allowed_contexts` and be auditable.

### 4.3 Tuning proposals are structured, but not strict enough

Current `TuningProposal` has:

- `proposal_type`
- `affected_scope`
- `current_behavior`
- `proposed_diff`
- `evidence`
- `risk_assessment`
- `rollback_plan`

Missing:

- `risk_direction`
- `requires_human_approval`
- `validation_required`
- `known_risks`
- `candidate_diff_status`
- `review_packet_id`
- approval actor/decision metadata
- test/replay/shadow result links

Required fix:

- Keep `tuning_proposals`, but either extend it or introduce `candidate_config_diffs` as the strict source of truth.

### 4.4 No explicit config/prompt/model version attached to every decision

Current storage captures many pieces:

- `decision_runs`
- `decision_role_outputs`
- `decision_state_snapshots`
- `trade_signals`
- `paper_orders`

Missing standard decision context:

- config version hash
- risk config version
- prompt version hash
- model route version
- injected memory IDs
- market snapshot refs
- data freshness summary
- risk gateway decision ID

Required fix:

- Add `DecisionContextRef` and attach it to `TradeSignal.metadata`, `TradeProposal.proposal_json`, paper orders, and risk decisions.

### 4.5 Risk checks are distributed

Current risk controls exist in:

- `SignalEngine.risk_vetoes`
- `PaperPortfolioService._sized_quantity`
- equity paper simulator checks
- high-stakes risk role prompt
- Hyperliquid validation helper
- Newswire halt gate

Problem:

- There is no single final deterministic gateway before paper/live order creation.
- High-stakes risk role is agentic and should never be confused with the deterministic gateway.

Required fix:

- Add final deterministic `RiskGateway`.
- Keep existing risk checks as upstream filters.
- Risk gateway becomes final authority for any order-like object.

### 4.6 Paper outcomes are treated as useful but not explicitly caveated

Current signal evaluation records MFE/MAE/R, but paper execution assumptions are not promoted to first-class evidence caveats.

Required fix:

- Add `PaperTradeOutcome` and `ExecutionAssumptionSet`.
- Every learning artifact derived from paper must carry `evidence_quality="paper_simulation"` and caveats.

### 4.7 Promotion API is under-specified

Current endpoint:

- `POST /autonomy/memory/candidates/{candidate_id}/promote-active`

Inputs:

- `human_review_confirmed`
- `reviewer`
- `change_control_id`
- `approved_for_role_injection_roles`

Missing:

- reviewer identity authorization
- proposer != approver invariant
- tests/evidence links
- rollback target
- explicit status target
- allowed/forbidden contexts
- policy version

Required fix:

- Replace promotion semantics with `PromotionDecision`.
- Keep endpoint compatibility but route internally through governance service.

---

## 5. Recommended target architecture, grounded in existing system

Add one governance layer around existing services.

```text
Existing loop:
Market/news/tradfi data
  -> MarketMapReducer
  -> SignalEngine / EquitySignalGenerator
  -> Discord/API human signoff
  -> PaperPortfolioService / EquityPaperSimulator
  -> SignalEvaluation / AlphaEventEvaluation
  -> MemoryService
  -> TuningProposalService
  -> Reports

Hardened loop:
Existing loop
  + DecisionContextRecorder
  + MemoryPolicyEngine
  + CandidateConfigDiff schema
  + RiskGateway
  + ReviewPacket / PromotionDecision / RollbackPlan
  + ShadowComparisonService
```

New modules:

```text
hyperliquid_trading_agent/app/governance/
  __init__.py
  schemas.py
  policy.py
  risk_gateway.py
  decision_context.py
  review.py
  shadow.py
  cli.py
```

Do not move existing core logic initially.

### New authority boundary components

| New component | Purpose | Mutates live behavior? |
|---|---|---:|
| `DecisionContextRecorder` | Capture config/prompt/model/memory/market/risk refs per decision | No |
| `MemoryPolicyEngine` | Enforce memory status/context permissions before prompt injection | No |
| `CandidateDiffService` | Normalize tuning proposals into strict diffs | No |
| `RiskGateway` | Final deterministic gate for paper/live order intents | Can reject/halt only |
| `ReviewPacketService` | Bundle evidence, tests, shadow results, risk delta, rollback | No |
| `PromotionService` | Records human approval/rejection decisions | Only changes governance status; no auto-apply |
| `ShadowComparisonService` | Baseline vs candidate diff on same data | No |
| `SafetyOverrideService` | Auto-halt or bounded tightening only | Can halt/tighten within bounds |

---

## 6. Minimal viable hardening path

### Phase A: audit-only wrappers

Add decision context and memory injection audit without changing behavior.

- `DecisionContextRecorder.snapshot_startup_config(settings)`
- `DecisionContextRecorder.prompt_hashes()`
- `MemoryService.memory_block_for_role` returns both text and memory IDs internally.
- `HighStakesRoleRunner` logs injected memory IDs in role state.
- `SignalEngine`/`maybe_attach_model_insight` logs memory IDs used for research insight.

### Phase B: enforce memory context permissions

Change default policy:

- Candidate memories: never injected.
- Shadow/validated advisory: only research/reviewer/shadow contexts.
- Approved policy: can enter broader contexts only with promotion metadata.

### Phase C: strict diffs and review packets

Keep `TuningProposalService`, but make it emit `CandidateConfigDiff` records.

### Phase D: deterministic risk gateway

Wrap paper order creation:

- `AutonomousTradingLoopService.approve_signal`
- `approve_equity_signal`
- future live execution path if added later

### Phase E: replay/shadow

Add replay/shadow services, no live change.

---

## 7. Deeper refactors worth considering

### Worth doing after guardrails are proven

1. **Unify crypto and equity paper simulators**
   - Current crypto: `app/autonomy/portfolio.py`
   - Current equity: `app/tradfi/paper/simulator.py`
   - Refactor to shared `PaperExecutionEngine` only after risk gateway is stable.

2. **Introduce `TradeIntent` as canonical intent**
   - Current `TradeSignal` and `TradeProposal` overlap.
   - Add `TradeIntent` adapter first; do not delete existing schemas.

3. **Split repository by domain**
   - `Repository` is large.
   - Defer until migrations and governance tables stabilize.

4. **External config source of truth**
   - Current runtime config is env.
   - Add DB config snapshots now; defer GitOps/config-repo promotion until live trading exists.

### Do not do yet

- Do not add live broker execution.
- Do not add vector DB as source of truth.
- Do not split into microservices.
- Do not auto-apply tuning proposals.
- Do not let LLMs mutate code/config.

---

## 8. Data model changes

### 8.1 Add governance schemas

Create:

```text
hyperliquid_trading_agent/app/governance/schemas.py
```

Add these Pydantic models.

#### `DecisionContextRef`

```python
class DecisionContextRef(BaseModel):
    decision_id: str
    run_id: str | None = None
    config_version_id: str
    risk_config_version_id: str
    prompt_version_ids: list[str] = []
    model_route: dict[str, Any] = {}
    injected_memory_ids: list[str] = []
    market_snapshot_refs: list[str] = []
    data_freshness: dict[str, Any] = {}
    code_version: str | None = None
    created_at_ms: int
```

Attach to:

- `TradeSignal.metadata["decision_context"]`
- `TradeProposal.proposal_json["decision_context"]`
- `PaperOrder.metadata["decision_context"]`
- `RiskGatewayDecision.decision_context_id`

#### `TradeIntent`

Do not replace `TradeSignal` yet. Add adapter.

```python
class TradeIntent(BaseModel):
    intent_id: str
    source_type: Literal["autonomy_signal", "high_stakes_proposal", "operator", "shadow"]
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
    evidence: list[dict[str, Any]]
    decision_context: DecisionContextRef
    execution_mode: Literal["paper", "shadow", "manual_review", "live"] = "paper"
    exchange_actions: list[dict[str, Any]] = []
```

#### `PaperTradeOutcome`

```python
class PaperTradeOutcome(BaseModel):
    outcome_id: str
    paper_order_id: str | None = None
    paper_position_id: str | None = None
    signal_id: str | None = None
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
    execution_assumptions: dict[str, Any]
    caveats: list[str]
    evidence_quality: Literal["paper_simulation", "shadow", "live_observation"] = "paper_simulation"
    created_at_ms: int
```

Required caveats default list:

```text
fill assumptions, slippage modeling, queue position, latency, partial fills,
market impact, fees, funding, spread behavior, venue handling, liquidation mechanics,
stale data, liquidity cliffs, news/event regimes, recent-market overfitting
```

#### `AdvisoryMemory`

Current `RoleLessonMemory` remains. Add normalized wrapper fields.

```python
class AdvisoryMemory(BaseModel):
    memory_id: str
    status: Literal["candidate", "validated_advisory", "approved_policy", "deprecated", "reverted"]
    scope: dict[str, Any]
    strategy_id: str | None = None
    symbols: list[str] = []
    venues: list[str] = []
    regimes: list[str] = []
    claim: str
    instruction: str
    evidence_links: list[str]
    source_run_ids: list[str]
    confidence: float
    created_at_ms: int
    expires_at_ms: int | None = None
    allowed_contexts: list[str]
    forbidden_contexts: list[str]
    promotion_history: list[dict[str, Any]]
    rollback_target: str | None = None
```

#### `CandidateConfigDiff`

Use this exact target shape.

```json
{
  "proposal_id": "tp_...",
  "strategy_id": "autonomy_v1",
  "scope": {
    "symbols": ["BTC"],
    "venues": ["hyperliquid"],
    "regimes": ["risk_on"]
  },
  "change_type": "threshold_adjustment",
  "current_value": {
    "autonomy_min_signal_score": 75
  },
  "proposed_value": {
    "asset_overrides.BTC.trend_continuation.min_signal_score": 82
  },
  "rationale": "...",
  "evidence": ["eval_...", "report_..."],
  "expected_effect": "...",
  "known_risks": ["reduced trade frequency", "missed early momentum"],
  "validation_required": ["replay", "out_of_sample_backtest", "shadow_run"],
  "risk_direction": "tightens_risk",
  "requires_human_approval": true,
  "auto_apply_allowed": false,
  "created_by": "autonomy_tuning",
  "created_at_ms": 0,
  "status": "proposed"
}
```

Enums:

```python
RiskDirection = Literal[
    "tightens_risk",
    "relaxes_risk",
    "increases_exposure",
    "decreases_exposure",
    "neutral",
    "unknown",
]
```

Rule:

- `relaxes_risk` or `increases_exposure` always requires human approval.
- Execution-affecting proposals always require human approval.
- Auto-apply always false for config diffs.

#### `ReviewPacket`

```python
class ReviewPacket(BaseModel):
    review_packet_id: str
    proposal_id: str
    evidence_links: list[str]
    affected_strategies: list[str]
    affected_symbols: list[str]
    affected_venues: list[str]
    risk_direction: str
    expected_effect: str
    known_risks: list[str]
    replay_results: dict[str, Any] | None = None
    shadow_results: dict[str, Any] | None = None
    reviewer_findings: list[dict[str, Any]] = []
    approval_requirements: list[str]
    rollback_plan_id: str
    created_at_ms: int
```

#### `PromotionDecision`

```python
class PromotionDecision(BaseModel):
    decision_id: str
    proposal_id: str
    reviewer: str
    decision: Literal["approved", "rejected", "needs_more_evidence"]
    rationale: str
    evidence_reviewed: list[str]
    tests_reviewed: list[str]
    proposer_actor: str
    approver_actor: str
    change_control_id: str
    approved_contexts: list[str]
    rollback_plan_id: str
    created_at_ms: int
```

Invariant:

```text
proposer_actor != approver_actor
```

#### `RollbackPlan`

```python
class RollbackPlan(BaseModel):
    rollback_plan_id: str
    target_type: Literal["config", "memory", "prompt", "model_route", "risk_limit"]
    target_id: str
    previous_version_id: str
    rollback_steps: list[str]
    verification_steps: list[str]
    owner: str
    created_at_ms: int
```

### 8.2 Database migration

Add migration:

```text
alembic/versions/0009_governance_authority.py
```

Add tables:

- `config_versions`
- `prompt_versions`
- `candidate_config_diffs`
- `review_packets`
- `promotion_decisions`
- `rollback_plans`
- `risk_gateway_decisions`
- `memory_injection_events`
- `runtime_safety_overrides`

Extend existing tables:

- `role_lessons`
- `shadow_role_lessons`
- `tuning_proposals`

Minimum new columns:

```text
role_lessons:
  memory_status
  allowed_contexts_json
  forbidden_contexts_json
  promotion_history_json
  rollback_target

shadow_role_lessons:
  memory_status
  allowed_contexts_json
  forbidden_contexts_json
  promotion_history_json
  rollback_target

tuning_proposals:
  strategy_id
  change_type
  risk_direction
  requires_human_approval
  validation_required_json
  known_risks_json
  review_packet_id
```

Backward-compatible migration defaults:

- Existing `shadow_role_lessons` → `memory_status="validated_advisory"`
- Existing `role_lessons.validation_status="active"`:
  - if `metadata_json.change_control_id` exists → `memory_status="approved_policy"`
  - else → `memory_status="validated_advisory"`
- Existing `archived` → `deprecated`

---

## 9. State machine changes

### 9.1 Memory state machine

Target:

```text
candidate
  -> validated_advisory
  -> approved_policy
  -> deprecated
  -> reverted
```

Implementation mapping:

| Current | New |
|---|---|
| `candidate_lessons.status="candidate"` | `candidate` |
| `candidate_lessons.status="shadow"` | `candidate` with shadow evidence |
| `shadow_role_lessons.validation_status="shadow"` | `validated_advisory` |
| `role_lessons.validation_status="active"` without policy metadata | `validated_advisory` |
| `role_lessons.validation_status="active"` with change control | `approved_policy` |
| `archived` / `expired` | `deprecated` |
| manual rollback | `reverted` |

### 9.2 Candidate config diff state machine

```text
draft
  -> proposed
  -> replay_required
  -> replay_passed | replay_failed
  -> shadow_required
  -> shadow_running
  -> shadow_passed | shadow_failed
  -> review_ready
  -> approved | rejected | needs_more_evidence
  -> canary_paper
  -> canary_live_pending
  -> rolled_out
  -> reverted
  -> expired
```

Current system stops at `proposed` / `accepted_manually`. Replace `accepted_manually` with:

- `reviewed_no_apply` for current compatibility
- `approved` only with `PromotionDecision`
- no runtime mutation unless future manual config deployment path exists

### 9.3 Runtime safety override state machine

For auto-halt/tightening only:

```text
proposed_by_gateway
  -> active
  -> expired
  -> manually_extended
  -> reverted
```

Rules:

- `halt` can auto-activate.
- `tighten` can auto-activate only inside predefined bounds.
- `relax` cannot auto-activate.
- `increase_exposure` cannot auto-activate.

---

## 10. Risk gateway changes

Add:

```text
hyperliquid_trading_agent/app/governance/risk_gateway.py
```

### 10.1 Keep existing risk controls

Do not remove:

- `SignalEngine.risk_vetoes`
- `PaperPortfolioService._sized_quantity`
- equity simulator risk checks
- high-stakes role prompts
- Hyperliquid validation helpers
- Newswire halt gate

### 10.2 Add final deterministic gate

Create:

```python
class RiskGateway:
    def check_trade_intent(self, intent: TradeIntent, context: RiskContext) -> RiskGatewayDecision: ...
    def check_paper_order(self, signal: TradeSignal, portfolio_state: dict, market_state: dict) -> RiskGatewayDecision: ...
```

Checks to implement now:

- max daily loss
- max position notional
- max per-symbol exposure
- max portfolio exposure
- max leverage
- max order size
- max order rate
- max spread
- price collars
- stale data lockout
- duplicate signal/order detection
- venue allowlist
- instrument allowlist
- kill switch
- circuit breaker after repeated risk/model/tool errors
- circuit breaker after abnormal paper slippage

Add later / defer until live execution exists:

- max cancels/replaces
- broker rejects/error rate from live broker
- liquidation mechanics from signed account state

### 10.3 Integration points

Add risk gateway calls in:

- `AutonomousTradingLoopService.approve_signal`
  - before `self.portfolio.approve_signal`
- `AutonomousTradingLoopService.approve_equity_signal`
  - before `equity_portfolio.place_order`
- `SignalEngine.generate`
  - keep existing vetoes, but attach `risk_gateway_precheck` to signal metadata when available
- future live execution adapter
  - mandatory final gate before broker call

### 10.4 Risk decision persistence

Add `risk_gateway_decisions` table with:

- `decision_id`
- `intent_id`
- `mode`
- `decision`: `allow`, `reject`, `halt`, `tighten`
- `violations_json`
- `limits_snapshot_json`
- `market_snapshot_json`
- `portfolio_snapshot_json`
- `config_version_id`
- `created_at_ms`

Attach `risk_gateway_decision_id` to paper order metadata.

---

## 11. Memory lifecycle changes

### 11.1 Keep current tables, change semantics

Do not destructively rewrite `MemoryService`.

Refactor:

```text
app/autonomy/memory.py
  -> continue owning observations/candidates/lessons
app/governance/policy.py
  -> own context permission checks
```

### 11.2 Default context rules

| Memory status | Allowed contexts | Forbidden contexts |
|---|---|---|
| `candidate` | none by default | all prompt injection |
| `validated_advisory` | `research`, `reviewer`, `shadow`, `report` | `execution`, `risk_gateway`, `live`, `order_router` |
| `approved_policy` | explicitly approved contexts only | all unlisted contexts |
| `deprecated` | none | all |
| `reverted` | none | all |

### 11.3 Replace current injection check

Current:

```python
_lesson_injection_allowed_for_role(lesson, role, settings)
```

Replace/wrap with:

```python
MemoryPolicyEngine.can_inject(
    memory=lesson,
    context_type="research" | "reviewer" | "strategy" | "risk" | "execution",
    role=role,
    mode="paper" | "shadow" | "live",
)
```

High-stakes mapping:

| Role | Context type |
|---|---|
| analyst | `strategy` |
| quant | `reviewer` |
| research | `research` |
| risk | `risk` |
| treasury | `reviewer` or `capital_review` |
| execution | `execution_review` |
| adversary | `reviewer` |
| judge | `reviewer` |

Default policy:

- `validated_advisory` can enter `research`, `reviewer`, `shadow`.
- It cannot enter `strategy` unless explicitly allowed.
- It cannot enter `risk`, `execution`, `risk_gateway`, or `live`.

### 11.4 Required memory fields

Every normalized memory must include:

- `memory_id`
- `status`
- `scope`
- `strategy_id`
- `symbols`
- `venues`
- `regimes`
- `claim`
- `evidence_links`
- `source_run_ids`
- `confidence`
- `created_at`
- `expires_at`
- `allowed_contexts`
- `forbidden_contexts`
- `promotion_history`
- `rollback_target`

### 11.5 Migration behavior

Existing `active` memories should not be deleted.

Migration default:

```text
active -> validated_advisory
allowed_contexts -> ["research", "reviewer", "shadow", "report"]
forbidden_contexts -> ["execution", "risk_gateway", "live", "order_router"]
```

If existing memory has:

```json
{
  "change_control_id": "...",
  "approved_for_role_injection_roles": ["risk"]
}
```

then migrate to:

```text
memory_status="approved_policy"
allowed_contexts=["risk"]
promotion_history includes change_control_id
```

---

## 12. Proposal / review / promotion workflow

### 12.1 Current flow to preserve

Current:

```text
SignalEvaluation / AlphaEventEvaluation
  -> MemoryObservation
  -> CandidateLesson
  -> TuningProposal
  -> daily/weekly report
```

Keep it.

### 12.2 Hardened promotion flow

Target:

```text
Observe
  -> Diagnose
  -> Propose
  -> Replay/backtest
  -> Shadow
  -> Review
  -> Human approval
  -> Canary
  -> Rollout
  -> Monitor
  -> Rollback or promote further
```

### 12.3 No skip rules

Enforce:

- Paper outcome cannot directly create active config.
- Paper outcome can create `CandidateConfigDiff`.
- `CandidateConfigDiff` cannot become `approved` without:
  - evidence links
  - replay/backtest result
  - shadow result
  - review packet
  - rollback plan
  - human approval
- Proposal relaxing risk or increasing exposure cannot auto-apply.
- Proposal tightening risk can only become `runtime_safety_override` inside bounded safe rules, not permanent config.

### 12.4 Review packet contents

Every review packet must show:

- proposed diff
- current value
- proposed value
- affected strategies
- affected symbols
- affected venues
- affected regimes
- evidence links
- paper caveats
- shadow comparison
- risk direction
- expected effect
- known risks
- validation required
- rollback target
- proposer
- reviewer eligibility

---

## 13. Audit and replay model

### 13.1 Add startup version snapshots

At app startup in `lifespan`:

- compute config hash from redacted `Settings`
- compute prompt hashes:
  - `SYSTEM_PROMPT`
  - high-stakes role prompts
  - role contracts
- compute model route hash:
  - `agent_model_chain`
  - role-specific model chains
- store in:
  - `config_versions`
  - `prompt_versions`

### 13.2 Attach decision context to every decision

For every `TradeSignal` and `TradeProposal`, include:

```json
"decision_context": {
  "config_version_id": "...",
  "risk_config_version_id": "...",
  "prompt_version_ids": ["..."],
  "model_route": {"role": "research", "model": "..."},
  "injected_memory_ids": ["mem_..."],
  "market_snapshot_refs": ["obs_..."],
  "data_freshness": {"allMids": "fresh", "l2Book": "15s"},
  "code_version": "git_sha_or_package_version"
}
```

### 13.3 Add memory injection audit

Add table `memory_injection_events`:

- `id`
- `run_id`
- `role`
- `context_type`
- `memory_ids_json`
- `blocked_memory_ids_json`
- `policy_decision_json`
- `created_at_ms`

### 13.4 Replay support

Implement:

```text
app/governance/decision_context.py
  explain_trade_decision(signal_id | proposal_id)
  load_replay_bundle(decision_id)
```

Replay bundle must include:

- market snapshot
- freshness
- signal inputs
- prompt version
- model version
- memory IDs
- strategy config version
- risk config version
- trade intent/signal/proposal
- validator/risk decision
- paper/broker response
- fill result
- post-trade annotations
- proposals generated afterward

---

## 14. Operator CLI/UI improvements

### 14.1 Keep current FastAPI + Discord

Do not build a web UI now.

### 14.2 Add FastAPI governance endpoints

Add to `main.py` or `app/governance/routes.py` and register in `create_app`.

```http
GET  /governance/config/active
GET  /governance/prompts
GET  /governance/decisions/{decision_id}
GET  /governance/runs/{run_id}/injected-memories
GET  /governance/signals/{signal_id}/explain
GET  /governance/proposals
GET  /governance/proposals/{proposal_id}
POST /governance/proposals/{proposal_id}/request-replay
POST /governance/proposals/{proposal_id}/request-shadow
POST /governance/proposals/{proposal_id}/approve
POST /governance/proposals/{proposal_id}/reject
POST /governance/proposals/{proposal_id}/needs-more-evidence
GET  /governance/review-packets/{review_packet_id}
GET  /governance/rollback-plans/{rollback_plan_id}
POST /governance/memories/{memory_id}/promote-policy
POST /governance/memories/{memory_id}/deprecate
POST /governance/freeze-live
POST /governance/paper-only
```

All protected by `_require_agent_api`.

### 14.3 Add minimal CLI

Create:

```text
hyperliquid_trading_agent/app/governance/cli.py
```

Use `argparse` + `httpx`, no new dependency required.

Commands:

```bash
python -m hyperliquid_trading_agent.app.governance.cli list-proposals
python -m hyperliquid_trading_agent.app.governance.cli show-proposal tp_...
python -m hyperliquid_trading_agent.app.governance.cli approve-proposal tp_... --change-control CC-123
python -m hyperliquid_trading_agent.app.governance.cli reject-proposal tp_...
python -m hyperliquid_trading_agent.app.governance.cli run-replay tp_...
python -m hyperliquid_trading_agent.app.governance.cli run-shadow tp_...
python -m hyperliquid_trading_agent.app.governance.cli show-active-config
python -m hyperliquid_trading_agent.app.governance.cli explain-signal sig_...
python -m hyperliquid_trading_agent.app.governance.cli freeze-live
```

### 14.4 Discord additions

Extend `parse_autonomy_command`:

- `proposal <id>`
- `approve proposal <id>`
- `reject proposal <id>`
- `request replay <id>`
- `request shadow <id>`
- `explain signal <id>`
- `injected memories <run_id>`
- `freeze live`
- `paper only`

Keep `apply tuning proposal <id>` denied.

---

## 15. Tests and invariants

Add tests in new files:

```text
tests/test_governance_authority.py
tests/test_memory_policy.py
tests/test_candidate_config_diff.py
tests/test_risk_gateway.py
tests/test_decision_replay.py
tests/test_promotion_pipeline.py
```

### Required invariant tests

| Invariant | Test |
|---|---|
| Paper outcome cannot directly mutate live config | Create completed `SignalEvaluation`; assert no `config_versions.active` mutation and no Settings mutation |
| Candidate memory cannot be injected into execution context | `MemoryPolicyEngine.can_inject(candidate, "execution") is False` |
| Validated advisory memory can only enter research/reviewer contexts | assert allowed for `research`, `reviewer`; blocked for `strategy`, `risk_gateway`, `execution`, `live` |
| Approved policy requires explicit promotion metadata | promote without `PromotionDecision` fails |
| Proposal relaxing risk cannot auto-apply | candidate diff `risk_direction="relaxes_risk"` remains proposed/review |
| Proposal increasing exposure cannot auto-apply | same for `increases_exposure` |
| Auto-halt can trigger without human approval | simulated drawdown/tool errors create active halt override |
| Auto-tightening only within bounds | tightening beyond configured cap rejected |
| Risk gateway rejects hard-limit violations | max notional, stale data, venue allowlist, instrument allowlist |
| Same agent cannot propose and approve own change | `proposer_actor == approver_actor` fails validation |
| Every trade decision includes versions and memory IDs | generated `TradeSignal.metadata.decision_context` complete |
| Every proposal links to evidence | `CandidateConfigDiff.evidence` non-empty |
| Every promoted change has rollback target | `PromotionDecision.rollback_plan_id` required |
| Live trading can be frozen while paper/shadow learning continues | freeze flag blocks live intents; paper eval still runs |

### Existing tests to keep green

- `tests/test_autonomy.py`
- `tests/test_autonomy_memory_loop.py`
- `tests/test_high_stakes.py`
- `tests/test_runtime_components.py`
- `tests/test_newswire.py`
- `tests/test_tradfi.py`

### Property-style tests without new dependency

Use randomized loops, not Hypothesis:

- Generate random risk limits/intents; assert gateway never allows notional > max.
- Generate random memory statuses/contexts; assert forbidden contexts always blocked.
- Generate random `risk_direction`; assert only `tightens_risk` can produce safety override.

---

## 16. Migration phases

### Phase 0: Baseline map and current behavior tests

Files:

- no production changes
- tests only if implementation begins

What changes:

- Add snapshot tests documenting current behavior.

What should not change:

- Autonomy loop behavior.
- Paper approvals.
- Existing endpoints.

Acceptance:

- Existing test suite passes.
- New baseline tests prove `exchange_actions=[]` everywhere current code promises.

Rollback:

- Remove baseline tests if needed.

---

### Phase 1: Add audit/version snapshots

Files:

- `app/governance/schemas.py`
- `app/governance/decision_context.py`
- `app/governance/routes.py`
- `app/main.py`
- `app/db/models.py`
- `app/db/repository.py`
- `alembic/versions/0009_governance_authority.py`

What changes:

- Add config/prompt version tables.
- Snapshot versions at startup.
- Attach `decision_context` to new `TradeSignal`/`TradeProposal` metadata.

What should not change:

- Signal scoring.
- Paper fills.
- Memory injection behavior.

Tests:

- Startup creates config/prompt version records.
- TradeSignal has config/prompt/model fields.

Rollback:

- Feature flag `GOVERNANCE_DECISION_CONTEXT_ENABLED=false`.

Acceptance:

- Every new signal/proposal carries decision context.

---

### Phase 2: Add strict candidate diff schema

Files:

- `app/governance/schemas.py`
- `app/autonomy/tuning.py`
- `app/autonomy/schemas.py`
- `app/db/models.py`
- `app/db/repository.py`

What changes:

- `TuningProposalService` emits/normalizes `CandidateConfigDiff`.
- Existing `tuning_proposals` endpoint returns strict fields.

What should not change:

- No auto-apply.
- Reports still show proposals.

Tests:

- Tuning proposal has `risk_direction`, `requires_human_approval`, `validation_required`, evidence.

Rollback:

- Keep old `proposed_diff` in `tuning_proposals`; disable new endpoint with flag.

Acceptance:

- No proposal can be created without evidence and rollback text.

---

### Phase 3: Add memory lifecycle states

Files:

- `app/governance/policy.py`
- `app/autonomy/memory.py`
- `app/agent/high_stakes/roles.py`
- `app/autonomy/signals.py`
- `app/db/models.py`
- `app/db/repository.py`

What changes:

- Add `memory_status`, allowed/forbidden contexts.
- `memory_block_for_role` uses `MemoryPolicyEngine`.
- Return/log injected memory IDs.

What should not change:

- Memories remain queryable.
- Existing candidate/lesson records migrate.

Tests:

- Candidate blocked.
- Validated advisory blocked from strategy/risk/execution/live.
- Approved policy requires promotion metadata.

Rollback:

- `MEMORY_CONTEXT_POLICY_AUDIT_ONLY=true` initially.

Acceptance:

- No unapproved memory can enter execution/risk context.

---

### Phase 4: Add approval gates

Files:

- `app/governance/review.py`
- `app/governance/routes.py`
- `app/main.py`
- `app/autonomy/memory.py`
- `app/autonomy/tuning.py`

What changes:

- Add `ReviewPacket`, `PromotionDecision`, `RollbackPlan`.
- Promotion endpoints require decision metadata.
- Current `mark-reviewed` becomes `reviewed_no_apply`.

What should not change:

- Tuning proposals remain observe-only.

Tests:

- Same actor cannot approve own proposal.
- Approval without rollback plan fails.
- Relax/increase exposure cannot auto-apply.

Rollback:

- Disable promotion enforcement flag; statuses remain persisted.

Acceptance:

- Every approved proposal has evidence, tests, approver, rollback target.

---

### Phase 5: Harden deterministic risk gateway

Files:

- `app/governance/risk_gateway.py`
- `app/autonomy/service.py`
- `app/autonomy/portfolio.py`
- `app/tradfi/paper/simulator.py`
- `app/db/models.py`
- `app/db/repository.py`

What changes:

- Add final risk gateway before paper order creation.
- Persist risk decisions.
- Add auto-halt/bounded-tighten safety overrides.

What should not change:

- Existing paper portfolio sizing remains.
- Existing risk vetoes remain.

Tests:

- Stale data reject.
- Max notional reject.
- Duplicate reject.
- Auto-halt allowed.
- Auto-relax forbidden.

Rollback:

- `RISK_GATEWAY_MODE=audit` to observe decisions without blocking paper.

Acceptance:

- Risk gateway decision exists for every paper order approval attempt.

---

### Phase 6: Add shadow comparison and replay

Files:

- `app/governance/shadow.py`
- `app/governance/review.py`
- `app/db/models.py`
- `app/db/repository.py`

What changes:

- Add baseline-vs-candidate diff comparison using persisted market observations.
- Add replay bundles for decisions.

What should not change:

- Runtime signal generation untouched.

Tests:

- Candidate diff cannot become review-ready without replay/shadow result.
- Replay reconstructs signal inputs.

Rollback:

- Disable shadow service; stored proposals remain.

Acceptance:

- Review packet includes replay/shadow result before approval.

---

### Phase 7: Canary/promotion/rollback workflow

Files:

- `app/governance/review.py`
- `app/governance/routes.py`
- `app/governance/cli.py`

What changes:

- Add paper canary state.
- Add rollback endpoints.
- Add operator CLI.

What should not change:

- No live canary until explicitly enabled in a future project phase.

Tests:

- Rollback plan required.
- Rollback changes governance status but does not mutate live config automatically.

Rollback:

- Disable CLI; endpoints remain.

Acceptance:

- Operator can list/show/approve/reject/request evidence/rollback.

---

### Phase 8: Clean up/refactor after boundaries proven

Files:

- Optional future:
  - split `Repository`
  - unify paper engines
  - introduce canonical `TradeIntent` usage everywhere

What changes:

- Internal cleanup only.

What should not change:

- Governance safety invariants.

Acceptance:

- No behavior drift without tests.

---

## 17. Rollback plan

Implementation rollback strategy:

1. All new enforcement should be behind flags:
   - `GOVERNANCE_DECISION_CONTEXT_ENABLED`
   - `MEMORY_CONTEXT_POLICY_AUDIT_ONLY`
   - `RISK_GATEWAY_MODE=audit|enforce`
   - `SHADOW_COMPARISON_ENABLED`
2. Database migrations are additive.
3. Existing endpoints remain backward-compatible.
4. Old `tuning_proposals` fields remain populated.
5. Old memory tables remain; new lifecycle fields default safely.
6. If enforcement blocks expected behavior:
   - set `MEMORY_CONTEXT_POLICY_AUDIT_ONLY=true`
   - set `RISK_GATEWAY_MODE=audit`
   - continue paper/shadow learning.
7. Never roll back by enabling live execution.
8. If a promoted policy must be reverted:
   - mark `PromotionDecision` superseded/reverted
   - mark memory/proposal `reverted`
   - restore previous config version manually
   - record rollback event.

---

## 18. Acceptance criteria

The plan is complete when:

- Paper outcomes cannot directly mutate live config.
- Candidate memories cannot be injected into execution/risk/live contexts.
- Validated advisory memories only enter approved advisory contexts.
- Approved policy memories require versioned promotion metadata and rollback target.
- Tuning proposals are structured diffs with evidence, risk direction, validation requirements, and rollback.
- No proposal can skip replay/shadow/review before approval.
- Risk gateway is deterministic and outside LLM authority.
- Auto-halt works without human approval.
- Auto-tightening is bounded and temporary.
- Auto-relaxation and exposure increase are impossible without explicit approval.
- Every signal/proposal/order has config version, prompt version, model route, memory IDs, and risk decision.
- Every proposal links to evidence.
- Every promotion has proposer, approver, tests reviewed, change-control ID, and rollback plan.
- Live trading can be frozen while paper/shadow learning continues.
- Existing paper/signoff loop remains operational.

---

## 19. Open questions

No blocking questions; defaults chosen for implementation:

1. **Approval identity**
   - Default: `AGENT_API_BEARER_TOKEN` + Discord admin IDs/roles.
   - Future: external SSO/change-management integration.

2. **Config source of truth**
   - Default: env remains runtime config; DB stores immutable config snapshots.
   - Future: GitOps/config repository promotion.

3. **Live trading**
   - Default: remains disabled.
   - Future live execution must be a separate approved project behind deterministic risk gateway and change control.

4. **Vector memory**
   - Default: no vector DB.
   - If added later, it must be retrieval/index only, never source of truth.








<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add governance/audit schemas and version snapshots around... _(done)_
- [x] 2. Harden memory lifecycle and context injection permissions. _(done)_
- [x] 3. Replace loose tuning proposal payloads with structured ca... _(done)_
- [x] 4. Add deterministic risk gateway as a final paper/live-inte... _(done)_
- [x] 5. Add replay/shadow comparison services for candidate diffs. _(done)_
- [x] 6. Add review packet, promotion decision, and rollback workf... _(done)_
- [x] 7. Add operator API/CLI commands for proposals, memories, co... _(done)_
- [x] 8. Add safety invariant tests and replay tests. _(done)_
- [x] 9. Migrate in phases with audit-only mode first, then enforc... _(done)_

<!-- pi-plan-progress:end -->
