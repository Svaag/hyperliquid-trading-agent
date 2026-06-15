---
created: 2026-06-15T04:30:20.251Z
source: pi-plan-mode
status: accepted-for-execution
---

# Persistent Alpha Memory + Signal Evaluation Loop

## Summary

Build the first “learning loop” on top of the current autonomous paper/signoff agent.

The immediate deliverable is **signal evaluation + daily/weekly reporting**, but the architecture should become the foundation for a persistent memory system where the agent improves its trading judgement, risk discipline, role-specific behavior, and Discord/operator communication over time.

The system remains:

- **paper-only**
- **human-signoff required**
- **observe-and-recommend only for strategy-affecting changes**
- **no live execution**
- **no automatic strategy/risk/weight/cooldown changes**

The long-term north star is a self-improving autonomous AI trading desk that compounds **Token Capital**: validation-weighted ability to improve risk-adjusted paper trading outcomes through durable memory, better signal interpretation, fewer repeated errors, stronger risk framing, and clearer operator-facing communication.

---

## Implementation Steps

1. Add signal evaluation and memory configuration.
2. Add migration `0005_signal_evaluation_memory.py`.
3. Add evaluation/memory/report schemas.
4. Implement signal outcome tracking.
5. Implement Token Capital scoring.
6. Implement daily and weekly report generation.
7. Implement evidence-gated memory promotion pipeline.
8. Implement role-specific hedge-fund daily checklist contracts.
9. Implement memory retrieval and prompt/report injection.
10. Implement TuningProposal generation in observe-and-recommend mode.
11. Add Discord commands for reports, signal feedback, memories, and tuning proposals.
12. Add FastAPI endpoints for evaluations, reports, memories, feedback, and tuning proposals.
13. Wire evaluation/report/memory services into `AutonomousTradingLoopService`.
14. Add metrics, audit events, readiness visibility, and safety gates.
15. Add tests, docs, and rollout runbook.

---

## Current State Grounding

Already implemented:

- `app/autonomy/`
  - deterministic signal engine
  - market map reducer
  - paper portfolio lifecycle
  - Discord signal posting/signoff
  - FastAPI `/autonomy/*` endpoints
  - Prometheus autonomy metrics
- DB tables:
  - `trade_signals`
  - `paper_portfolios`
  - `paper_orders`
  - `paper_fills`
  - `paper_positions`
  - `portfolio_snapshots`
  - `autonomy_events`
  - `market_observations`
  - `news_events`
- High-stakes roles:
  - Analyst
  - Quant
  - Research
  - Risk
  - Treasury
  - Execution
  - Adversary
  - Judge

Missing:

- no persistent signal outcome evaluation
- no MFE/MAE/R-multiple attribution
- no “expired/rejected but would have won/lost” analysis
- no daily/weekly PM report
- no durable role lesson memory
- no operator-output lesson memory
- no structured tuning proposals
- no memory retrieval layer for agent context
- no Token Capital score

---

## Product Decisions Locked

### Token Capital definition

Use a **hybrid score**, not raw PnL.

Token Capital means:

> The agent’s validation-weighted ability to improve risk-adjusted paper trading outcomes through better memory, signal interpretation, risk discipline, and operator-facing communication.

Paper equity growth is a lagging confirmation metric, not the only objective.

Track all underlying metrics separately so the aggregate score is debuggable.

### Learning authority

V1 is **observe-and-recommend only**.

The agent may:

- persist validated lessons
- score outputs
- generate reports
- identify repeated errors
- draft structured tuning proposals

The agent must not directly modify:

- strategy rules
- signal weights
- thresholds
- cooldowns
- position sizing
- risk limits
- live/paper trading parameters
- execution behavior

All strategy-affecting changes become `TuningProposal` records and require human action outside this phase.

### V1 memory scope

Implement:

1. `SignalOutcomeMemory`
2. `RoleLessonMemory`
3. `OperatorOutputLessonMemory`

Do not implement full operational memory yet.

Keep raw Discord feedback, model reliability data, news/catalyst postmortems, incident logs, and runbook issues as append-only observations unless promoted by schema-supported validation.

### Report cadence

- Daily Discord PM report: **00:05 UTC**
- Weekly deep review: **Monday 00:30 UTC**
- Reports go to current `AUTONOMY_ALERT_CHANNEL_ID`.
- Full structured reports are also available by API.

### Hedge-fund roles

Deepen the existing 8 roles.

Do not add new autonomous roles yet.

Performance Analyst, Data Engineer, and Compliance/Ops are implemented as validators/report generators/gates, not agent personas.

---

## Configuration

Add to `Settings`, `.env.example`, README, and `/health/config`:

```env
AUTONOMY_EVALUATION_ENABLED=true
AUTONOMY_MEMORY_ENABLED=true
AUTONOMY_REPORTS_ENABLED=true

AUTONOMY_EVAL_HORIZONS=15m,1h,4h,24h,expiry
AUTONOMY_EVAL_MAX_OPEN_SIGNALS=500
AUTONOMY_EVAL_PRICE_SOURCE=allMids

AUTONOMY_DAILY_REPORT_ENABLED=true
AUTONOMY_DAILY_REPORT_UTC=00:05
AUTONOMY_WEEKLY_REPORT_ENABLED=true
AUTONOMY_WEEKLY_REPORT_DAY=MON
AUTONOMY_WEEKLY_REPORT_UTC=00:30

AUTONOMY_MEMORY_ROLE_MAX_ACTIVE=200
AUTONOMY_MEMORY_OPERATOR_MAX_ACTIVE=100
AUTONOMY_MEMORY_CANDIDATE_TTL_DAYS=30
AUTONOMY_MEMORY_SHADOW_TTL_DAYS=60
AUTONOMY_MEMORY_ROLE_TTL_DAYS=30
AUTONOMY_MEMORY_PROCESS_TTL_DAYS=90
AUTONOMY_MEMORY_INCIDENT_TTL_DAYS=14

AUTONOMY_ROLE_LESSON_MIN_SAMPLES=5
AUTONOMY_OPERATOR_LESSON_MIN_SAMPLES=3
AUTONOMY_SIGNAL_LESSON_MIN_SAMPLES=20
AUTONOMY_LESSON_MIN_CONFIDENCE=0.70
AUTONOMY_STRATEGY_LESSON_MIN_CONFIDENCE=0.75

AUTONOMY_TUNING_PROPOSALS_ENABLED=true
AUTONOMY_TUNING_PROPOSAL_TTL_DAYS=14
```

Defaults are safe:

- evaluation enabled only within autonomy runtime
- memory observe/recommend only
- no automatic tuning
- no live execution

---

## New Package Layout

Add:

```text
hyperliquid_trading_agent/app/autonomy/
  evaluation.py
  memory.py
  reports.py
  tuning.py
  role_contracts.py
```

Responsibilities:

### `evaluation.py`

- Tracks every signal after creation/posting.
- Maintains live path metrics from allMids.
- Computes horizon marks.
- Computes MFE/MAE, R-multiple, stop/TP outcomes.
- Writes `SignalEvaluation` and `SignalEvaluationMark`.

### `memory.py`

- Converts logs/outcomes/feedback into structured observations.
- Promotes observations through:
  1. `MemoryObservation`
  2. `CandidateLesson`
  3. `ShadowRoleLessonMemory`
  4. `RoleLessonMemory`
- Stores `OperatorOutputLessonMemory`.
- Retrieves active scoped lessons for reports/model prompts.

### `reports.py`

- Generates daily PM report.
- Generates weekly deep review.
- Posts compact Discord report.
- Persists full JSON report.
- Includes Token Capital breakdown and tuning proposals.

### `tuning.py`

- Creates observe-only `TuningProposal` records.
- Never applies changes.
- Provides exact proposed diff, expected impact, blast radius, rollback, and evaluation window.

### `role_contracts.py`

- Defines role purpose, daily checklist, allowed inputs, forbidden claims, output criteria, memory types, escalation conditions.

---

## Database Migration

Add:

```text
alembic/versions/0005_signal_evaluation_memory.py
```

### New tables

```text
signal_evaluations
signal_evaluation_marks
memory_observations
candidate_lessons
shadow_role_lessons
role_lessons
operator_output_lessons
operator_feedback
tuning_proposals
token_capital_snapshots
daily_reports
weekly_reports
```

---

## Schemas

### `SignalEvaluation`

```python
class SignalEvaluation(BaseModel):
    id: str
    signal_id: str
    symbol: str
    side: Literal["long", "short"]
    signal_type: str
    status: Literal[
        "open",
        "complete",
        "partial",
        "expired_no_data",
        "error"
    ]

    created_at_ms: int
    completed_at_ms: int | None = None

    entry: float
    stop: float
    take_profit: float | None = None

    signal_score: float
    signal_confidence: float
    signal_status_at_eval_start: str

    first_price: float | None = None
    latest_price: float | None = None
    latest_price_at_ms: int | None = None

    max_favorable_price: float | None = None
    max_adverse_price: float | None = None
    max_favorable_bps: float | None = None
    max_adverse_bps: float | None = None
    max_favorable_r: float | None = None
    max_adverse_r: float | None = None

    stop_hit: bool = False
    stop_hit_at_ms: int | None = None
    take_profit_hit: bool = False
    take_profit_hit_at_ms: int | None = None

    terminal_outcome: Literal[
        "tp_hit",
        "stop_hit",
        "expired_positive",
        "expired_negative",
        "expired_flat",
        "insufficient_data",
        "open"
    ] = "open"

    realized_or_marked_r: float | None = None
    opportunity_cost_r: float | None = None

    approved: bool = False
    rejected: bool = False
    paper_ordered: bool = False
    paper_position_id: str | None = None

    feature_snapshot: dict[str, Any]
    evidence_snapshot: list[dict[str, Any]]
    market_regime: str = "unknown"

    error: str = ""
    metadata: dict[str, Any] = {}
```

### `SignalEvaluationMark`

One row per horizon.

```python
class SignalEvaluationMark(BaseModel):
    id: str
    evaluation_id: str
    signal_id: str
    symbol: str

    horizon: Literal["15m", "1h", "4h", "24h", "expiry"]
    due_at_ms: int
    marked_at_ms: int | None = None

    price: float | None = None
    direction_adjusted_return_bps: float | None = None
    r_multiple: float | None = None

    mfe_bps_until_mark: float | None = None
    mae_bps_until_mark: float | None = None
    mfe_r_until_mark: float | None = None
    mae_r_until_mark: float | None = None

    stop_hit_before_mark: bool = False
    take_profit_hit_before_mark: bool = False

    status: Literal["pending", "marked", "missed_no_price", "error"] = "pending"
    metadata: dict[str, Any] = {}
```

### `MemoryObservation`

Raw structured observation before lesson promotion.

```python
class MemoryObservation(BaseModel):
    id: str
    source_type: Literal[
        "signal_evaluation",
        "daily_report",
        "weekly_report",
        "operator_feedback",
        "role_output",
        "schema_validation",
        "incident"
    ]
    source_id: str
    role: str | None = None
    symbol: str | None = None
    signal_type: str | None = None
    market_regime: str | None = None

    observation: str
    evidence: list[dict[str, Any]]
    severity: Literal["info", "warning", "critical"] = "info"

    created_at_ms: int
    metadata: dict[str, Any] = {}
```

### `CandidateLesson`

```python
class CandidateLesson(BaseModel):
    id: str
    lesson_type: Literal[
        "role_behavior",
        "signal_quality",
        "risk_discipline",
        "operator_output",
        "data_quality",
        "incident_warning"
    ]

    role: str | None = None
    scope: dict[str, Any]

    claim: str
    evidence: list[dict[str, Any]]
    source_observation_ids: list[str]
    source_run_ids: list[str]
    source_signal_ids: list[str]

    sample_size: int
    counterexamples: list[dict[str, Any]]
    confidence: float

    expected_future_behavior_change: str
    strategy_affecting: bool = False
    risk_affecting: bool = False
    execution_affecting: bool = False
    capital_allocation_affecting: bool = False

    status: Literal[
        "candidate",
        "shadow",
        "promoted",
        "rejected",
        "expired"
    ] = "candidate"

    created_at_ms: int
    expires_at_ms: int
    metadata: dict[str, Any] = {}
```

### `RoleLessonMemory`

```python
class RoleLessonMemory(BaseModel):
    id: str
    role: Literal[
        "analyst",
        "quant",
        "research",
        "risk",
        "treasury",
        "execution",
        "adversary",
        "judge"
    ]

    lesson_type: str
    scope: dict[str, Any]

    claim: str
    instruction: str
    evidence: list[dict[str, Any]]
    source_candidate_id: str
    source_run_ids: list[str]
    source_signal_ids: list[str]

    confidence: float
    sample_size: int
    counterexamples: list[dict[str, Any]]

    validation_status: Literal[
        "active",
        "needs_human_review",
        "shadow",
        "archived",
        "expired",
        "rejected"
    ]

    strategy_affecting: bool = False
    risk_affecting: bool = False
    execution_affecting: bool = False
    capital_allocation_affecting: bool = False

    created_at_ms: int
    activated_at_ms: int | None = None
    expires_at_ms: int
    last_revalidated_at_ms: int | None = None

    metadata: dict[str, Any] = {}
```

### `OperatorOutputLessonMemory`

```python
class OperatorOutputLessonMemory(BaseModel):
    id: str
    scope: dict[str, Any]

    issue_or_pattern: str
    preferred_behavior: str
    bad_examples: list[dict[str, Any]]
    good_examples: list[dict[str, Any]]

    confidence: float
    sample_size: int

    validation_status: Literal[
        "active",
        "shadow",
        "archived",
        "expired",
        "rejected"
    ]

    created_at_ms: int
    expires_at_ms: int
    metadata: dict[str, Any] = {}
```

### `OperatorFeedback`

```python
class OperatorFeedback(BaseModel):
    id: str
    source: Literal["discord", "api"]
    actor_id: str | None = None

    target_type: Literal[
        "signal",
        "report",
        "lesson",
        "discord_message",
        "tuning_proposal"
    ]
    target_id: str

    rating: Literal["good", "bad", "unclear", "too_noisy", "useful", "wrong"]
    note: str = ""

    created_at_ms: int
    metadata: dict[str, Any] = {}
```

### `TuningProposal`

```python
class TuningProposal(BaseModel):
    id: str

    proposal_type: Literal[
        "threshold_change",
        "weight_change",
        "cooldown_change",
        "risk_rule_change",
        "universe_change",
        "messaging_change",
        "data_quality_gate",
        "role_prompt_change"
    ]

    status: Literal[
        "draft",
        "proposed",
        "accepted_manually",
        "rejected",
        "expired",
        "superseded"
    ] = "draft"

    title: str
    summary: str

    affected_scope: dict[str, Any]
    current_behavior: dict[str, Any]
    proposed_diff: dict[str, Any]

    evidence: list[dict[str, Any]]
    source_lesson_ids: list[str]
    source_signal_ids: list[str]

    expected_impact: str
    risk_assessment: str
    blast_radius: Literal["low", "medium", "high"]
    rollback_plan: str

    confidence: float
    sample_size: int

    created_at_ms: int
    expires_at_ms: int
    evaluation_window: str

    metadata: dict[str, Any] = {}
```

### `TokenCapitalSnapshot`

```python
class TokenCapitalSnapshot(BaseModel):
    id: str
    timestamp_ms: int
    window: Literal["daily", "weekly", "rolling_30d"]

    total_score: float

    risk_adjusted_performance_score: float
    signal_quality_score: float
    memory_compounding_score: float
    risk_discipline_score: float
    operator_communication_score: float
    reliability_score: float

    hard_gate_penalties: list[dict[str, Any]]
    component_details: dict[str, Any]

    created_from_report_id: str | None = None
    metadata: dict[str, Any] = {}
```

---

## Signal Evaluation Logic

### Creation

When a `TradeSignal` is created or posted:

1. Create one `SignalEvaluation`.
2. Create five `SignalEvaluationMark` rows:
   - `15m`
   - `1h`
   - `4h`
   - `24h`
   - `expiry`
3. Persist signal score, confidence, entry, stop, TP, evidence snapshot, and feature snapshot.
4. Status starts as `open`.

### Price path tracking

Hook into the current autonomy allMids path:

```text
Hyperliquid allMids
  -> AutonomousTradingLoopService._on_all_mids
  -> MarketMapReducer.apply_all_mids
  -> PaperPortfolioService.mark_to_market
  -> SignalEvaluationService.on_price
```

`SignalEvaluationService.on_price(symbol, price, timestamp_ms)` updates all open evaluations for that symbol.

For each open evaluation:

- update latest price
- update max favorable price
- update max adverse price
- update MFE/MAE in bps
- update MFE/MAE in R
- detect first stop hit
- detect first TP hit

### Long calculation

For long:

```text
directional_return = (price - entry) / entry
risk_per_unit = entry - stop
r_multiple = (price - entry) / risk_per_unit
MFE = max(price - entry)
MAE = min(price - entry)
stop hit if price <= stop
TP hit if take_profit and price >= take_profit
```

### Short calculation

For short:

```text
directional_return = (entry - price) / entry
risk_per_unit = stop - entry
r_multiple = (entry - price) / risk_per_unit
MFE = max(entry - price)
MAE = min(entry - price)
stop hit if price >= stop
TP hit if take_profit and price <= take_profit
```

### Horizon marking

Every loop iteration:

1. Query pending marks due by `now_ms`.
2. Use latest observed price for symbol.
3. If no price exists within 5 minutes, mark `missed_no_price`.
4. Record:
   - price
   - direction-adjusted return
   - R multiple
   - MFE/MAE up to mark
   - stop/TP hit before mark

### Completion

An evaluation completes when:

- expiry mark is done, or
- TP hit and all shorter due marks are resolved, or
- stop hit and all shorter due marks are resolved.

Terminal outcome:

```text
tp_hit
stop_hit
expired_positive
expired_negative
expired_flat
insufficient_data
```

### Rejected/expired opportunity-cost analysis

Rejected or unapproved signals still evaluate.

For rejected/expired signals:

- If +1R or TP would have hit before stop, count as missed opportunity.
- If stop would have hit, count as good rejection/filter.
- Include in reports.

---

## Token Capital Score

Compute a 0–100 score with component breakdown.

### Component weights

```text
risk_adjusted_performance_score   30%
signal_quality_score              20%
memory_compounding_score          20%
risk_discipline_score             15%
operator_communication_score      10%
reliability_score                  5%
```

### Risk-adjusted performance score

Inputs:

- paper equity return
- Sharpe
- max drawdown
- average realized R
- open risk
- gross exposure

Raw formula:

```text
score = 50
+ pnl_return_component
+ sharpe_component
+ avg_r_component
- drawdown_penalty
- excessive_exposure_penalty
```

Clamp 0–100.

### Signal quality score

Inputs:

- average marked R at 1h/4h/24h
- hit rate
- TP-before-stop rate
- stop-before-TP rate
- calibration between signal score and realized outcome
- MFE/MAE ratio
- rejected-signal opportunity cost

### Memory compounding score

Inputs:

- number of validated active lessons
- lesson hit rate after activation
- repeated-error reduction
- stale/expired lesson cleanup
- candidate-to-promoted quality ratio

### Risk discipline score

Inputs:

- signals missing stop
- RR below threshold
- stale data usage
- risk-limit violations
- oversized paper positions
- stop quality failures
- liquidation/inferred certainty labeling

Hard gate examples:

- hallucinated fill/order
- ignored risk limit
- claimed live execution
- stale market data used as fresh
- schema-invalid signal/report

Hard gates cap or penalize the score.

Example:

```text
if hallucinated_order_claim:
    total_score = min(total_score, 20)
if live_execution_claim:
    total_score = min(total_score, 10)
if schema_broken:
    total_score -= 10
```

### Operator communication score

Inputs:

- human feedback
- report clarity
- Discord actionability
- repeated phrasing issues
- missing risk context
- schema adherence
- concise formatting

### Reliability score

Inputs:

- service loop health
- failed evaluations
- report generation errors
- stale data windows
- model-insight fallback rate
- DB write failures

---

## Daily Report

Post daily at **00:05 UTC** to `AUTONOMY_ALERT_CHANNEL_ID`.

Persist full JSON to `daily_reports`.

### Discord format

```text
📊 AI Trading Desk Daily Report — 2026-06-16 UTC

Token Capital: 71/100 (+3)
Paper equity: $100,420 (+0.42%)
Realized PnL: +$180
Unrealized PnL: +$240
Max DD: 0.35%
Sharpe: n/a / 1.42

Signals:
- Posted: 3
- Approved: 1
- Rejected: 1
- Expired/unapproved: 1
- Avg 4h R: +0.42R
- TP before stop: 1
- Stop before TP: 0

Best:
- HYPE long trend_continuation: +1.8R MFE, TP hit

Worst:
- ZEC short risk_off_deleveraging: -0.6R MAE, no stop hit

Missed opportunities:
- SOL long rejected: would have reached +1.2R before stop

Role lessons:
- Quant candidate: BTC 10bps imbalance worked only when spread < 12bps.
- Risk candidate: tight stops inside 0.8% volatility band underperformed.

Operator-output lessons:
- Good: compact score/entry/stop/RR format got clearer.
- Needs work: add “why now” line to signals.

Tuning proposals:
- TP-20260616-001: Raise ZEC min score to 82 for 7 days.
  Evidence: 3 weak signals, avg -0.4R, high spread.

No live trades placed. Paper/signoff mode only.
```

### Full JSON sections

```json
{
  "date": "2026-06-16",
  "token_capital": {},
  "portfolio": {},
  "signals": {},
  "approved_signals": [],
  "rejected_signals": [],
  "expired_signals": [],
  "missed_opportunities": [],
  "role_lesson_candidates": [],
  "operator_output_lessons": [],
  "tuning_proposals": [],
  "hard_gates": [],
  "data_quality": {},
  "model_reliability": {}
}
```

---

## Weekly Report

Post weekly at **Monday 00:30 UTC**.

Weekly report is longer and should include:

- 7-day Token Capital trend
- paper PnL
- Sharpe/drawdown
- signal-type attribution
- asset attribution
- regime attribution
- role lesson validation
- operator-output lessons
- recurring errors
- stale memories expiring
- TuningProposal list
- suggested next experimental focus

Persist full JSON to `weekly_reports`.

---

## Persistent Memory Promotion Pipeline

### Flow

```text
Raw logs/outcomes/feedback
  -> MemoryObservation
  -> CandidateLesson
  -> ShadowRoleLessonMemory
  -> RoleLessonMemory
```

### Candidate creation

Candidate lessons can come from:

- signal evaluations
- daily reports
- weekly reports
- role outputs
- Discord/API operator feedback
- schema validation failures
- critical incidents

### Promotion rules

#### Operator-output lessons

Auto-promote to active if:

```text
sample_size >= 3
confidence >= 0.70
not strategy_affecting
not risk_affecting
not execution_affecting
```

Examples:

- “Signal alerts should include a `why now` line.”
- “Avoid repeating generic no-live-trade caveat more than once.”
- “When tracking commands fail, explain exact accepted commands.”

#### Routine role lessons

Auto-promote to active if:

```text
sample_size >= 5
confidence >= 0.70
counterexample_rate <= 20%
not strategy_affecting
not risk_affecting
not execution_affecting
not capital_allocation_affecting
```

Examples:

- Analyst: “For HYPE breakouts, require acceptance above level, not just wick.”
- Research: “ETF headlines stale after 4h unless new flow data confirms.”

#### Signal/strategy lessons

Require higher evidence and human review:

```text
sample_size >= 20
confidence >= 0.75
strategy_affecting = true
validation_status = needs_human_review
```

They do **not** become active durable memory until manually approved.

Examples:

- “Raise ZEC threshold to 82.”
- “Downweight funding/OI squeeze signals during mixed BTC regime.”
- “Require 10bps depth > $50k for WLD signals.”

#### Critical incident memories

May promote immediately only if:

```text
severity = critical
scope is narrow
expires_at_ms set
blast radius documented
strategy_affecting broad rule = false
```

Examples:

- “Do not treat inferred liquidation clusters as direct facts.”
- “Do not claim paper order filled unless `paper_fills` row exists.”

These expire after 14 days unless reinforced.

---

## Memory Retention

### Permanent

- `SignalEvaluation`
- `SignalEvaluationMark`
- `daily_reports`
- `weekly_reports`
- `token_capital_snapshots`

### TTL with archival

- `CandidateLesson`: 30 days
- `ShadowRoleLessonMemory`: 60 days
- market/regime-specific `RoleLessonMemory`: 30 days
- process/schema/operator lessons: 90 days
- critical incident warnings: 14 days
- tuning proposals: 14 days

Expired memories are archived, not deleted.

Archived memories:

- not injected into prompts
- still visible by API
- still usable for historical audit

---

## Memory Retrieval

Implement deterministic SQL/filter retrieval first, no vector database in V1.

### Retrieval inputs

```python
MemoryQuery(
    role="quant",
    symbol="HYPE",
    signal_type="trend_continuation",
    market_regime="risk_on",
    timeframe="1h",
    max_items=8,
)
```

### Ranking

Sort by:

1. active status
2. exact role match
3. exact symbol match
4. exact signal type match
5. current market regime match
6. confidence
7. recency
8. sample size

### Budget

Prompt/report injection limits:

```text
role_lessons_per_role: 5
operator_lessons: 3
incident_warnings: 3
max_chars_per_role_memory_block: 1500
```

### Usage rules

Active role memories may:

- inform role prompts
- inform model insight prompts
- appear in reports
- create tuning proposals

Active role memories may not:

- directly change deterministic signal weights
- directly change thresholds
- directly change risk limits
- directly alter position sizing
- directly approve/reject trades

Operator-output lessons may influence formatting and Discord/report wording if non-strategy and non-risk-affecting.

---

## Hedge-Fund Role Contracts

Each current role gets a daily checklist and memory contract.

### Analyst / Proposer

Human hedge-fund analog:

- morning market scan
- idea generation
- thesis framing
- level mapping
- setup journaling
- post-trade thesis review

Daily checklist:

- identify strongest asymmetric setups
- classify setup type
- separate fact vs inference
- define entry, invalidation, expected path
- check whether prior similar setups worked
- avoid inventing trades without evidence

May create memories:

- setup pattern lessons
- thesis quality lessons
- asset-specific behavior lessons
- failed idea-framing lessons

Forbidden claims:

- cannot claim execution
- cannot invent catalysts
- cannot broaden one-off lesson into universal rule

Escalates to:

- Quant if market structure edge is unclear
- Research if catalyst uncertain
- Risk if stop/invalidation weak
- Judge if thesis conflicts with evidence

### Quant

Human hedge-fund analog:

- feature research
- signal attribution
- return distribution analysis
- backtest/post-trade review
- regime classification
- model calibration

Daily checklist:

- evaluate signal features vs outcomes
- compare score to realized R
- inspect MFE/MAE
- identify feature decay
- flag overfit patterns
- validate thresholds only as proposals

May create memories:

- feature predictive-power lessons
- regime-specific signal lessons
- threshold/cooldown candidate lessons
- calibration lessons

Forbidden claims:

- cannot treat small samples as robust
- cannot auto-change weights
- cannot hide counterexamples

Escalates to:

- Risk for drawdown or tail-risk issue
- Execution for liquidity/slippage feature
- Judge for strategy-affecting proposal

### Research

Human hedge-fund analog:

- news monitoring
- catalyst validation
- macro calendar review
- social/reflexivity analysis
- post-catalyst decay review

Daily checklist:

- track high-importance news
- tag assets
- evaluate source quality
- compare catalyst sentiment vs price outcome
- identify stale narratives
- flag rumor-only setups

May create memories:

- source reliability lessons
- catalyst half-life lessons
- narrative crowding lessons
- news/price divergence lessons

Forbidden claims:

- cannot treat unsourced X posts as fact
- cannot claim catalyst caused move without evidence
- cannot ignore timestamp/freshness

Escalates to:

- Adversary for contradiction/crowding
- Judge for major catalyst regime shifts

### Risk

Human hedge-fund analog:

- exposure monitoring
- drawdown control
- stop quality review
- limit enforcement
- stress testing
- loss postmortems

Daily checklist:

- verify every signal has valid stop
- evaluate stop distance vs volatility
- check R/R and expected loss
- inspect concentration
- identify repeated stop-outs
- enforce hard veto memories

May create memories:

- stop-quality lessons
- loss-control lessons
- risk-limit incident memories
- drawdown lessons

Forbidden claims:

- cannot relax hard risk limits
- cannot approve missing-stop signals
- cannot ignore stale/unknown prices

Escalates to:

- Judge for hard veto
- Treasury for exposure/margin
- Execution for slippage risk

### Treasury

Human hedge-fund analog:

- capital allocation
- cash/margin tracking
- funding/fee drag
- exposure inventory
- portfolio constraints

Daily checklist:

- track cash/equity/exposure
- inspect funding drag
- check portfolio concentration
- evaluate capital efficiency
- identify opportunity cost of unused capital
- compare approved vs rejected trade outcomes

May create memories:

- funding drag lessons
- concentration lessons
- capital allocation warnings
- portfolio fit lessons

Forbidden claims:

- cannot assume unavailable account data
- cannot recommend leverage changes directly
- cannot auto-allocate capital

Escalates to:

- Risk for concentration/drawdown
- Judge for capital-allocation proposal

### Execution

Human hedge-fund analog:

- liquidity monitoring
- order book analysis
- slippage estimation
- venue constraints
- fill-quality postmortems

Daily checklist:

- check spread/depth/slippage
- compare paper fill assumptions to order book
- inspect time-of-day liquidity
- validate Hyperliquid tick/lot assumptions
- identify bad-fill-prone assets
- produce messaging improvements for manual actionability

May create memories:

- liquidity/slippage lessons
- time-of-day execution lessons
- venue constraint lessons
- operator actionability lessons

Forbidden claims:

- cannot provide signed payloads
- cannot claim live orders
- cannot infer hidden order types as fact

Escalates to:

- Risk for slippage exceeding risk
- Judge for execution veto

### Adversary / Red Team

Human hedge-fund analog:

- pre-mortems
- bias checks
- crowded-trade review
- failure-mode analysis
- post-loss challenge

Daily checklist:

- find recurring false positives
- attack stale narratives
- identify unsupported assumptions
- detect repeated hallucination patterns
- flag lessons that overgeneralize
- inspect counterexamples before promotion

May create memories:

- false-positive lessons
- contradiction lessons
- hallucination guard lessons
- overconfidence warnings

Forbidden claims:

- cannot veto without evidence
- cannot convert suspicion into fact
- cannot ignore successful counterexamples

Escalates to:

- Judge for critical unresolved flaw
- Risk for capital-defense issue

### Judge / CIO

Human hedge-fund analog:

- daily PM meeting
- final decision synthesis
- portfolio-level accountability
- strategy review
- governance/change control

Daily checklist:

- review signal outcomes
- review role lessons
- resolve conflicting memories
- approve/reject lesson promotions requiring human review
- summarize Token Capital trajectory
- prioritize tuning proposals

May create memories:

- decision-quality lessons
- governance lessons
- role-performance lessons
- accepted/rejected critique lessons

Forbidden claims:

- cannot average away critical objection
- cannot auto-apply tuning proposals
- cannot approve execution

Escalates to:

- human operator for strategy/risk/execution-affecting tuning proposals

---

## Validators Instead of New Roles

### Performance Analyst function

Implemented in `reports.py`.

Responsibilities:

- signal attribution
- paper PnL attribution
- R-multiple summaries
- win/loss analysis
- Token Capital snapshot
- daily/weekly report

### Data Engineer function

Implemented as data-quality gates.

Checks:

- stale price data
- missing horizon marks
- missing market observations
- schema invalidity
- source/freshness metadata
- failed DB writes
- incomplete feature snapshots

### Compliance/Ops function

Implemented as audit and policy gates.

Checks:

- no live execution
- no private keys
- no hallucinated fills/orders
- no strategy auto-change
- tuning proposal has rollback/blast radius
- all memory promotion has provenance

---

## TuningProposal Rules

Tuning proposals are recommendations only.

A proposal must include:

- exact scope
- exact proposed diff
- evidence
- source lesson IDs
- source signal IDs
- sample size
- expected impact
- confidence
- risk assessment
- blast radius
- rollback plan
- expiry
- evaluation window

Example:

```json
{
  "proposal_type": "threshold_change",
  "title": "Raise ZEC min signal score to 82 for 7 days",
  "affected_scope": {
    "symbol": "ZEC",
    "signal_type": "trend_continuation"
  },
  "current_behavior": {
    "autonomy_min_signal_score": 75
  },
  "proposed_diff": {
    "asset_overrides.ZEC.min_signal_score": 82
  },
  "evidence": [
    {
      "signal_count": 5,
      "avg_4h_r": -0.42,
      "mae_r_median": -0.71,
      "spread_bps_median": 28
    }
  ],
  "expected_impact": "Reduce low-quality ZEC alerts in high-spread conditions.",
  "risk_assessment": "May miss valid ZEC momentum signals.",
  "blast_radius": "low",
  "rollback_plan": "Remove ZEC override or set back to 75.",
  "confidence": 0.77,
  "sample_size": 5,
  "evaluation_window": "7d"
}
```

Even if accepted manually, V1 only records status. It does not mutate runtime config.

---

## Discord Commands

Extend `#ai-bot-alerts` autonomy commands.

### Reports

```text
daily report
weekly report
token capital
```

### Signal evaluation

```text
signal outcome <signal_id>
signal eval <signal_id>
```

### Feedback

```text
mark signal <signal_id> good
mark signal <signal_id> bad
mark signal <signal_id> unclear
feedback signal <signal_id> <note>
feedback bot <note>
```

### Memory

```text
memories
memories analyst
memories quant
memory <lesson_id>
```

### Tuning proposals

```text
tuning proposals
tuning proposal <id>
```

No Discord command applies tuning changes in V1.

If a user tries:

```text
apply tuning proposal <id>
```

Respond:

```text
Tuning proposals are observe-and-recommend only in this phase. Apply manually after review. No runtime strategy settings were changed.
```

---

## FastAPI Endpoints

All protected with existing `_require_agent_api`.

### Evaluations

```http
GET  /autonomy/evaluations/signals
GET  /autonomy/evaluations/signals/{signal_id}
POST /autonomy/evaluations/run
POST /autonomy/evaluations/backfill
```

### Reports

```http
GET  /autonomy/reports/daily
GET  /autonomy/reports/daily/{date}
POST /autonomy/reports/daily/run

GET  /autonomy/reports/weekly
GET  /autonomy/reports/weekly/{week}
POST /autonomy/reports/weekly/run
```

### Token Capital

```http
GET /autonomy/token-capital
GET /autonomy/token-capital/history
```

### Memory

```http
GET  /autonomy/memory/observations
GET  /autonomy/memory/candidates
GET  /autonomy/memory/shadow
GET  /autonomy/memory/lessons
GET  /autonomy/memory/lessons/{lesson_id}
POST /autonomy/memory/lessons/{lesson_id}/archive
POST /autonomy/memory/candidates/{candidate_id}/reject
POST /autonomy/memory/candidates/{candidate_id}/promote-shadow
```

For strategy/risk/execution/capital-affecting candidates:

```http
POST /autonomy/memory/candidates/{candidate_id}/promote-active
```

must return `409` unless request has:

```json
{
  "human_review_confirmed": true,
  "reviewer": "..."
}
```

Even then, active memory does not mutate strategy parameters.

### Feedback

```http
POST /autonomy/feedback
GET  /autonomy/feedback
```

### Tuning proposals

```http
GET  /autonomy/tuning-proposals
GET  /autonomy/tuning-proposals/{proposal_id}
POST /autonomy/tuning-proposals/{proposal_id}/mark-reviewed
POST /autonomy/tuning-proposals/{proposal_id}/reject
POST /autonomy/tuning-proposals/{proposal_id}/expire
```

No endpoint applies proposals.

---

## Service Wiring

Add services in lifespan:

```python
evaluation_service = SignalEvaluationService(...)
memory_service = MemoryService(...)
report_service = AutonomyReportService(...)
tuning_service = TuningProposalService(...)
```

Pass them into `AutonomousTradingLoopService`.

### Runtime flow

```text
Signal created
  -> persist TradeSignal
  -> create SignalEvaluation + marks

allMids tick
  -> update market map
  -> mark portfolio
  -> update signal evaluation path metrics

loop interval
  -> mark due horizons
  -> complete expired evaluations
  -> create observations from completed evaluations

daily 00:05 UTC
  -> generate report
  -> compute Token Capital
  -> create CandidateLessons
  -> create TuningProposals
  -> post Discord summary

weekly Monday 00:30 UTC
  -> deep attribution
  -> role lesson review
  -> stale memory/archive pass
  -> post Discord summary
```

---

## Memory Injection

### High-stakes role prompts

Before each role call:

1. Retrieve active role memories scoped to:
   - role
   - symbol
   - signal type/setup type
   - market regime
2. Add a compact memory block to the role prompt:

```text
Relevant validated role memories:
- [risk:HYPE:trend_continuation] Tight stops inside 0.8% 1h realized vol caused repeated stop-outs. Check stop-vs-noise before supporting. confidence=0.78 sample=12 expires=2026-07-15
```

### Model insight prompts

For high-score autonomous signals:

- include relevant active lessons as advisory context
- model may support/oppose/needs_more_data
- model cannot approve or alter parameters

### Deterministic signal engine

V1 does **not** alter deterministic scoring from memories.

Allowed:

- attach relevant warnings to evidence
- create observations
- create tuning proposals

Forbidden:

- direct threshold/weight changes
- automatic cooldowns
- automatic risk changes

### Discord/report formatting

Active non-strategy `OperatorOutputLessonMemory` may influence report/signal wording.

Example:

```text
Operator memory: Always include a one-line “why now” summary in signal alerts.
```

This is allowed because it affects presentation, not strategy.

---

## Metrics

Add:

```python
SIGNAL_EVALUATIONS_CREATED
SIGNAL_EVALUATION_MARKS_COMPLETED
SIGNAL_EVALUATION_ERRORS
SIGNAL_OUTCOMES_BY_TYPE
TOKEN_CAPITAL_SCORE
MEMORY_OBSERVATIONS_CREATED
CANDIDATE_LESSONS_CREATED
ROLE_LESSONS_ACTIVE
ROLE_LESSONS_ARCHIVED
OPERATOR_FEEDBACK_TOTAL
TUNING_PROPOSALS_CREATED
AUTONOMY_DAILY_REPORTS_POSTED
AUTONOMY_WEEKLY_REPORTS_POSTED
```

Labels:

- `symbol`
- `signal_type`
- `role`
- `status`
- `outcome`
- `lesson_type`

---

## Readiness and Health

Extend `/health/config` autonomy section:

```json
"evaluation": {
  "enabled": true,
  "open_evaluations": 12,
  "pending_marks": 41,
  "last_mark_at_ms": 1780000000000,
  "errors": []
},
"memory": {
  "enabled": true,
  "active_role_lessons": 18,
  "shadow_lessons": 9,
  "candidate_lessons": 22,
  "operator_lessons": 4
},
"reports": {
  "daily_enabled": true,
  "weekly_enabled": true,
  "last_daily_report_at_ms": 1780000000000,
  "last_weekly_report_at_ms": null
},
"token_capital": {
  "latest_score": 71.2,
  "latest_snapshot_at_ms": 1780000000000
}
```

Readiness degradation:

- evaluation enabled but no price marks completed in > 2h while signals are open
- daily report generation failed
- DB unavailable
- memory promotion errors repeatedly
- stale market data

Do not degrade readiness just because no signals exist.

---

## Audit Events

Record:

```text
signal_evaluation_created
signal_evaluation_marked
signal_evaluation_completed
daily_report_generated
weekly_report_generated
token_capital_snapshot_created
memory_observation_created
candidate_lesson_created
shadow_lesson_created
role_lesson_promoted
role_lesson_archived
operator_feedback_recorded
tuning_proposal_created
tuning_proposal_rejected
tuning_proposal_marked_reviewed
```

Every event payload includes:

```json
{
  "exchange_actions": [],
  "source": "autonomy_memory_loop"
}
```

---

## Safety Gates

Hard constraints:

1. No live exchange execution.
2. No SDK `Exchange`.
3. No private keys.
4. No automatic strategy mutation.
5. No automatic risk mutation.
6. No automatic position sizing mutation.
7. Tuning proposals are recommendations only.
8. Inferred liquidation/stop memories must be labeled inferred.
9. Signal evaluations must not claim fill/order unless backed by `paper_orders`/`paper_fills`.
10. Reports must distinguish:
    - posted
    - approved
    - rejected
    - expired
    - paper ordered
    - hypothetical/rejected outcome

---

## Tests

### Signal evaluation tests

- long signal MFE/MAE
- short signal MFE/MAE
- stop hit before TP
- TP hit before stop
- rejected signal would-have-won
- rejected signal would-have-lost
- horizon marks at 15m/1h/4h/24h/expiry
- missed mark when no fresh price
- restart reloads open evaluations

### Token Capital tests

- component score calculation
- hard gate caps score
- all component details exposed
- paper PnL does not dominate hybrid score

### Report tests

- daily report includes required sections
- weekly report includes attribution
- Discord report is compact
- report persists JSON
- no live execution claim

### Memory tests

- observation -> candidate
- candidate -> shadow
- shadow -> active role lesson
- strategy-affecting lesson requires human review
- critical incident warning gets narrow TTL
- expired memories archived not deleted
- retrieval filters by role/symbol/signal_type/regime
- prompt memory budget enforced

### Tuning proposal tests

- proposal includes exact diff
- includes rollback plan
- includes blast radius
- cannot apply proposal
- API reject/mark-reviewed works
- Discord cannot apply proposal

### Operator feedback tests

- `mark signal <id> good`
- `mark signal <id> bad`
- `feedback bot <note>`
- feedback creates observation
- repeated feedback creates operator lesson candidate

### API tests

- auth required outside dev/local
- all endpoints return expected shape
- 404 for unknown IDs
- 409 for strategy-affecting promote without human review

### Safety tests

- `rg` / import guard: no `Exchange`
- all generated events include `exchange_actions=[]`
- reports do not claim live trades
- signal evaluations do not hallucinate fills

Validation commands:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy hyperliquid_trading_agent
uv run alembic upgrade head --sql >/tmp/memory_migration.sql
docker compose config
```

---

## Documentation Updates

Update:

- `README.md`
- `.env.example`
- `docs/deploy/step-10-resumption-runbook.md`
- add `docs/autonomy-memory.md`

Document:

- Token Capital definition
- Signal evaluation fields
- Daily/weekly report examples
- Memory promotion rules
- Role contracts
- TuningProposal workflow
- Safety constraints
- Discord commands
- API endpoints

---

## Rollout Plan

1. Ship code with current autonomy still paper/signoff only.
2. Run migration SQL dry run.
3. Deploy with:
   ```env
   AUTONOMY_EVALUATION_ENABLED=true
   AUTONOMY_MEMORY_ENABLED=true
   AUTONOMY_REPORTS_ENABLED=true
   AUTONOMY_TUNING_PROPOSALS_ENABLED=true
   ```
4. Keep:
   ```env
   AUTONOMY_MAX_SIGNALS_PER_DAY=3
   AUTONOMY_UNIVERSE_TOP_N_PERPS=5
   ```
5. Let run for 48h.
6. Verify:
   - evaluations created
   - horizon marks complete
   - daily report posts
   - no tuning proposal is applied
   - no false live-execution language
7. After one week:
   - review weekly report
   - inspect candidates/shadow memories
   - manually decide whether any strategy-affecting proposal should become future config.

---

## Acceptance Criteria

- Every posted signal creates a persistent evaluation record.
- Every signal has horizon marks for 15m, 1h, 4h, 24h, and expiry.
- Daily report posts to Discord at 00:05 UTC.
- Weekly report posts Monday at 00:30 UTC.
- Token Capital score is computed with full component breakdown.
- Rejected/expired signals are evaluated for opportunity cost.
- RoleLessonMemory exists for all 8 roles.
- Candidate lessons require evidence and provenance.
- Strategy/risk/execution/capital-affecting lessons cannot become active without human review.
- TuningProposal records include exact diff, evidence, blast radius, rollback, expiry, and confidence.
- No tuning proposal is automatically applied.
- Active role memories can be injected into role/model prompts.
- Deterministic signal scoring does not change from memory in V1.
- Operator-output lessons can improve formatting only.
- Reports never claim live execution.
- All safety/no-execution tests pass.

---

## Non-Goals for This Phase

Do not implement:

- vector database
- full operational memory
- automatic threshold tuning
- automatic signal-weight tuning
- live execution
- private-key handling
- new autonomous hedge-fund roles
- automatic capital allocation changes
- model-only strategy changes
- broad unscoped “remember everything” behavior

---

## Final Architecture Shape

```text
Market Data / News / Signals / Paper Portfolio
        |
        v
Signal Evaluation Service
        |
        v
SignalOutcomeMemory + Reports
        |
        v
Memory Observations
        |
        v
Candidate Lessons
        |
        v
Shadow Memories
        |
        v
Validated Role/Operator Memories
        |
        v
Role Prompt Context + Reports + Tuning Proposals
        |
        v
Human Review / Manual Config Changes
```

This gives the agent its first compounding loop:

```text
observe -> evaluate -> remember -> report -> recommend -> human review -> improve
```

without allowing it to mutate strategy or risk controls on its own.
