---
created: 2026-06-16T20:10:47.810Z
source: pi-plan-mode
status: accepted-for-execution
---

# Institutional Trading Engine Upgrade Plan

## Summary

Replace the current signal-by-signal autonomy loop with a canonical institutional pipeline:

```text
Raw feeds
  -> normalized event ledger
  -> point-in-time feature store
  -> regime engine
  -> alpha ensemble
  -> candidate book
  -> learned EV scorer
  -> portfolio allocator
  -> deterministic risk gateway
  -> AI debate for high expected value of review
  -> paper/shadow execution gateway
  -> position manager
  -> reconciliation
  -> attribution/replay/research governance
```

Decisions locked from planning:

- **Scope:** full institutional roadmap.
- **Migration:** hard replace current `TradeSignal`-centric autonomy flow.
- **Safety:** paper/shadow only; no signed exchange adapter, no private keys, no live order route.
- **Storage:** Postgres normalized ledger + point-in-time feature tables with bounded retention + rollups.
- **AI debate:** adjudication layer triggered by expected value of review, not raw score.
- **ML:** add classical ML stack, using scikit-learn/joblib/pandas-style offline training and approved model registry.
- **API:** add read-only admin endpoints; do not add new mutating API controls in this milestone.
- **Execution:** runnable `PaperAdapter` and `ShadowAdapter` only.

## Current Repo Facts

Current relevant implementation:

- Autonomy loop: `hyperliquid_trading_agent/app/autonomy/service.py`
- Market map: `hyperliquid_trading_agent/app/autonomy/market_map.py`
- Signal engine: `hyperliquid_trading_agent/app/autonomy/signals.py`
- Paper portfolio: `hyperliquid_trading_agent/app/autonomy/portfolio.py`
- Risk gateway: `hyperliquid_trading_agent/app/governance/risk_gateway.py`
- Debate graph: `hyperliquid_trading_agent/app/agent/high_stakes/graph.py`
- Newswire: `hyperliquid_trading_agent/app/newswire/*`
- Governance/versioning already exists: `decision_contexts`, `config_versions`, `risk_gateway_decisions`, `replay_results`.
- Current live execution is explicitly disabled by config validators.

## Implementation Steps

1. Create the new institutional engine schema layer.
2. Add Postgres migrations and repository methods for event ledger, features, candidates, EV, allocation, debate, execution, and position theses.
3. Replace market-map/signal generation with normalized event ingestion, feature store, regime engine, alpha ensemble, and candidate book.
4. Add learned EV scorer infrastructure and offline training path.
5. Add portfolio allocator and upgraded deterministic risk gateway.
6. Convert high-stakes AI debate into EvidencePack-based adjudication.
7. Add paper/shadow execution gateway and deterministic position manager.
8. Add read-only engine API endpoints and Discord output updates.
9. Add retention, rollups, replay, attribution, and governance promotion flows.
10. Update tests, docs, config, metrics, and migration compatibility.

## New Module Layout

Create:

```text
hyperliquid_trading_agent/app/engine/
  __init__.py
  schemas.py
  event_ledger.py
  feature_store.py
  regime.py
  alpha/
    __init__.py
    base.py
    directional.py
    news_event.py
    microstructure.py
    equity.py
  candidate_book.py
  scorer.py
  portfolio_allocator.py
  debate_adjudicator.py
  execution.py
  position_manager.py
  reconciliation.py
  attribution.py
  retention.py
  replay.py
  routes.py
  metrics.py
```

Keep high-stakes role/model code where it is, but make it consume `EvidencePack`.

## Core Schemas

Implement in `app/engine/schemas.py`.

### `NormalizedEvent`

```python
class NormalizedEvent(BaseModel):
    event_id: str
    schema_version: int = 1
    event_type: str
    asset_class: Literal["crypto", "equity", "macro", "unknown"]
    symbols: list[str]
    source: str
    provider: str
    event_ts_ms: int | None
    received_ts_ms: int
    computed_ts_ms: int
    payload: dict[str, Any]
    quality_score: float
    staleness_ms: int | None
    metadata: dict[str, Any] = {}
```

### `FeatureValue`

```python
class FeatureValue(BaseModel):
    feature_id: str
    asset: str
    feature_group: str
    feature_name: str
    value: dict[str, Any]
    scalar_value: float | None = None
    event_ts_ms: int | None
    received_ts_ms: int
    computed_ts_ms: int
    source_event_id: str | None
    source: str
    version: str
    quality_score: float
    staleness_ms: int | None
    metadata: dict[str, Any] = {}
```

### `RegimeVector`

Use the user-proposed vector exactly, with nullable fields where data is unavailable. Store both raw feature refs and derived labels.

### `AlphaCandidate`

```python
class AlphaCandidate(BaseModel):
    candidate_id: str
    strategy_id: str
    asset: str
    asset_class: str
    venue: str
    side: Literal["long", "short", "flat"]
    horizon: str
    proposed_entry: float
    stop: float
    targets: list[float]
    thesis: str
    invalidation_conditions: list[str]
    feature_snapshot_id: str
    regime_snapshot_id: str
    source_event_ids: list[str]
    raw_alpha_score: float
    confidence: float
    status: Literal[
        "new", "scored", "allocated", "risk_rejected", "debate_required",
        "debate_approved", "debate_downgraded", "debate_blocked",
        "approved_for_paper", "approved_for_shadow", "expired", "cancelled"
    ]
    created_at_ms: int
    expires_at_ms: int
    metadata: dict[str, Any] = {}
```

### `EVEstimate`

```python
class EVEstimate(BaseModel):
    estimate_id: str
    candidate_id: str
    model_version_id: str
    p_target: float
    p_stop: float
    p_timeout: float
    expected_favorable_bps: float
    expected_adverse_bps: float
    expected_holding_ms: int
    expected_fee_bps: float
    expected_spread_cost_bps: float
    expected_slippage_bps: float
    expected_market_impact_bps: float
    expected_funding_cost_bps: float
    tail_loss_bps: float
    net_ev_bps: float
    risk_adjusted_utility: float
    uncertainty: float
    calibration_bucket: str
    created_at_ms: int
```

### `EvidencePack`

```python
class EvidencePack(BaseModel):
    evidence_pack_id: str
    candidate_id: str
    strategy_id: str
    asset: str
    side: str
    horizon: str
    feature_snapshot_id: str
    market_regime_snapshot: dict[str, Any]
    orderflow_summary: dict[str, Any]
    news_summary: dict[str, Any]
    risk_summary: dict[str, Any]
    historical_analogs: list[dict[str, Any]]
    model_outputs: dict[str, Any]
    known_missing_data: list[str]
    data_quality_flags: list[str]
    proposed_trade_plan: dict[str, Any]
    invalidation_conditions: list[str]
    created_at_ms: int
```

### `DebateDecision`

```python
class DebateDecision(BaseModel):
    debate_decision_id: str
    evidence_pack_id: str
    candidate_id: str
    decision: Literal["approve", "downgrade", "block", "require_more_data"]
    confidence_adjustment: float
    max_size_multiplier: float
    reason_codes: list[str]
    required_invalidation_checks: list[str]
    audit_summary: str
    role_outputs: list[dict[str, Any]]
    judge_model: str | None
    created_at_ms: int
```

### `OrderIntent`

```python
class OrderIntent(BaseModel):
    intent_id: str
    parent_candidate_id: str
    portfolio_decision_id: str
    asset: str
    asset_class: str
    venue: str
    side: Literal["buy", "sell"]
    order_type: Literal["marketable_limit", "post_only", "twap", "vwap", "pov"]
    time_in_force: str
    target_size: float
    target_notional_usd: float
    max_slippage_bps: float
    price_limit: float | None
    reduce_only: bool
    post_only: bool
    deadline_ts_ms: int
    strategy_id: str
    model_version_id: str
    config_version_id: str
    risk_budget_id: str
    execution_mode: Literal["paper", "shadow"]
    created_at_ms: int
```

### `ExecutionReport`

```python
class ExecutionReport(BaseModel):
    report_id: str
    intent_id: str
    execution_mode: Literal["paper", "shadow"]
    status: Literal["accepted", "rejected", "filled", "partial", "cancelled", "expired"]
    requested_size: float
    filled_size: float
    avg_fill_px: float | None
    fees_usd: float
    slippage_bps: float
    market_impact_bps: float | None
    adapter: Literal["paper", "shadow"]
    assumptions: dict[str, Any]
    created_at_ms: int
```

### `PositionThesis`

Implement user-proposed thesis/state machine with states:

```text
proposed, approved, opening, open, scaling_in, partial_take_profit,
de_risking, trailing, time_stop_pending, exit_pending, closed, under_review
```

## Database Migrations

Add sequential Alembic migrations.

### `0011_engine_event_feature_store.py`

Tables:

- `normalized_events`
- `feature_values`
- `feature_rollups`
- `regime_snapshots`

Indexes:

```text
normalized_events(received_ts_ms)
normalized_events(event_type, received_ts_ms)
normalized_events(asset_class, received_ts_ms)
feature_values(asset, feature_name, computed_ts_ms)
feature_values(source_event_id)
regime_snapshots(created_at_ms)
regime_snapshots(primary_asset, created_at_ms)
```

### `0012_candidate_ev_allocation_debate.py`

Tables:

- `alpha_candidates`
- `candidate_book_snapshots`
- `ev_estimates`
- `allocation_decisions`
- `evidence_packs`
- `debate_decisions`

Indexes:

```text
alpha_candidates(status, created_at_ms)
alpha_candidates(asset, status)
ev_estimates(candidate_id)
allocation_decisions(candidate_id)
evidence_packs(candidate_id)
debate_decisions(candidate_id)
```

### `0013_execution_position_reconciliation.py`

Tables:

- `order_intents`
- `execution_reports`
- `position_theses`
- `reconciliation_runs`
- `pnl_attribution_records`
- `kill_switch_events`

### `0014_model_registry_retention.py`

Tables:

- `model_versions`
- `model_training_runs`
- `feature_schema_versions`
- `retention_runs`

Model registry fields:

```text
model_version_id
model_type
artifact_uri
training_data_hash
feature_schema_hash
metrics_json
status: candidate | shadow | approved | deprecated
approved_by
approved_at_ms
created_at_ms
metadata_json
```

## Config Changes

Add settings:

```env
ENGINE_ENABLED=false
ENGINE_MODE=paper_shadow
ENGINE_EXECUTION_MODES=paper,shadow

NEWSWIRE_GATEWAY_ENABLED=true
AUTONOMY_LEGACY_NEWS_POLL_ENABLED=false
NEWS_SIGNAL_GENERATION_ENABLED=true
NEWS_EVENT_RISK_BLOCKS_ENABLED=true

ENGINE_EVENT_RETENTION_DAYS=7
ENGINE_FEATURE_RETENTION_DAYS=14
ENGINE_ROLLUP_RETENTION_DAYS=365

ENGINE_DEBATE_ENABLED=true
ENGINE_DEBATE_MAX_PER_DAY=8
ENGINE_DEBATE_PRIORITY_MIN=0.35

ENGINE_MIN_NET_EV_BPS=8
ENGINE_MIN_RISK_ADJUSTED_UTILITY=0.25
ENGINE_MAX_CANDIDATES_PER_LOOP=50
ENGINE_MAX_APPROVED_CANDIDATES_PER_LOOP=5

ENGINE_MODEL_ARTIFACT_DIR=/var/lib/hyperliquid-trading-agent/models
ENGINE_APPROVED_SCORER_MODEL_ID=
ENGINE_SCORER_FALLBACK_MODE=deterministic

ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=true
ENGINE_LIVE_ENABLED=false
```

Keep validators that reject live exchange enablement.

## News Fallback Fix

Replace the overloaded `NEWSWIRE_ENABLED` behavior with separate flags:

```text
NEWSWIRE_GATEWAY_ENABLED       # source/gateway runtime
AUTONOMY_LEGACY_NEWS_POLL_ENABLED
NEWS_SIGNAL_GENERATION_ENABLED
NEWS_EVENT_RISK_BLOCKS_ENABLED
```

Implementation rule:

```python
events = []

if settings.newswire_gateway_enabled:
    events.extend(await newswire_gateway.latest_or_poll())

if settings.autonomy_legacy_news_poll_enabled:
    events.extend(await legacy_news_poller.poll())

events = dedupe_and_rank_news(events)
```

No single flag may control all of:

- ingestion source
- polling behavior
- signal usage
- risk-block usage

## Engine Runtime Flow

Replace `AutonomousTradingLoopService._run_iteration()` with:

```text
1. Collect raw market/news/tradfi events.
2. Normalize into NormalizedEvent.
3. Persist append-only normalized events.
4. Compute FeatureValue records.
5. Build RegimeVector snapshot.
6. Run alpha ensemble to generate AlphaCandidate records.
7. Insert all candidates, including rejected/expired candidates.
8. Estimate EV via approved model or deterministic fallback.
9. Build CandidateBook.
10. Allocate portfolio risk.
11. Run deterministic risk checks.
12. Compute debate_priority.
13. Run AI debate only when priority >= threshold or top-N daily budget.
14. Convert approved candidates to OrderIntent.
15. Execute via PaperAdapter or ShadowAdapter.
16. Update PositionThesis.
17. Mark outcomes and attribution.
18. Persist metrics, retention, and research records.
```

## Debate Priority Formula

Implement deterministic priority:

```python
edge_score = min(abs(ev.net_ev_bps) / 50.0, 1.0)
uncertainty_score = ev.uncertainty
capital_score = min(allocation.risk_usd / max(portfolio_equity * 0.01, 1), 1.0)
novelty_score = candidate.metadata.get("novelty_score", 0.5)
conflict_score = candidate.metadata.get("conflict_score", 0.0)
regime_instability = 1.0 - regime.regime_stability_score

debate_priority = (
    edge_score
    * max(0.25, uncertainty_score)
    * max(0.25, capital_score)
    * max(0.25, novelty_score)
    * max(0.25, conflict_score)
    * max(0.25, regime_instability)
)
```

Trigger debate when:

```text
debate_priority >= ENGINE_DEBATE_PRIORITY_MIN
or candidate is in top 2 by allocated risk for the loop
or risk gateway returns "tighten" rather than "reject"
```

Daily cap: `ENGINE_DEBATE_MAX_PER_DAY`.

## AI Debate Changes

Modify high-stakes graph to accept either:

- existing `TradeProposalRequest`, for manual/API debate
- new `EvidencePack`, for engine adjudication

Add new roles:

```text
bull_quant
bear_quant
microstructure_skeptic
news_macro_analyst
execution_analyst
risk_officer
judge
```

For implementation reuse, map to existing model chains initially:

```text
bull_quant -> quant
bear_quant -> adversary
microstructure_skeptic -> execution
news_macro_analyst -> research
execution_analyst -> execution
risk_officer -> risk
judge -> judge
```

Judge must return `DebateDecision`.

AI cannot:

- create exchange actions
- increase max size above allocator output
- override risk rejection
- relax stale-data or kill-switch blocks

AI can:

- approve
- downgrade confidence
- reduce size via `max_size_multiplier`
- block
- require more data

## Regime Engine

Implement `RegimeEngine.compute()` consuming feature store snapshots.

Minimum first-version fields:

```text
trend_state
trend_confidence
realized_vol_percentile
liquidity_state
spread_state
funding_stress_z
open_interest_velocity_z
liquidation_imbalance_z
cross_asset_risk_on_z
news_catalyst_pressure
correlation_breakdown_prob
regime_stability_score
```

If a data source is unavailable:

- field is `None`
- quality flag records missing input
- strategies requiring that field are blocked or downgraded, never silently allowed

Strategy permissions:

```text
momentum_allowed
mean_reversion_allowed
market_making_allowed
news_event_allowed
carry_allowed
relative_value_allowed
```

## Alpha Ensemble

Initial strategy families:

1. `directional_momentum_v2`
   - Port current trend continuation logic.
   - Require regime permission.
   - Add OI/funding/liquidity checks.

2. `support_resistance_reversion_v2`
   - Port support bounce/resistance rejection.
   - Require range/mean-reversion regime.

3. `news_event_alpha_v1`
   - Uses normalized event ontology, first-notice score, source reliability, contradiction score, freshness decay.

4. `microstructure_ofi_v1`
   - Uses orderbook imbalance persistence, spread velocity, depth collapse/replenishment, microprice deviation.

5. `equity_options_flow_v1`
   - Port current equity/options flow logic into candidate format.

All candidates are persisted whether accepted or rejected.

## Learned Scorer

Add dependencies:

```toml
scikit-learn>=1.5
pandas>=2.2
joblib>=1.4
```

First model stack:

```text
Meta-label classifier:
  sklearn HistGradientBoostingClassifier

EV components:
  p_target, p_stop, p_timeout from calibrated classifier probabilities
  expected favorable/adverse bps from HistGradientBoostingRegressor
  execution cost from deterministic model first, later trained

Calibration:
  CalibratedClassifierCV or isotonic calibration where sample size permits
```

If no approved model exists:

```text
ENGINE_SCORER_FALLBACK_MODE=deterministic
```

Fallback still emits an `EVEstimate` with:

```text
model_version_id = "deterministic_fallback_v1"
uncertainty >= 0.65
```

Training labels:

```text
+1 target reached net of fees/slippage/funding before stop
-1 stop reached net of fees/slippage/funding before target
 0 timeout/no edge/ambiguous
```

Training command:

```bash
uv run python -m hyperliquid_trading_agent.app.engine.scorer train \
  --start-ms ... \
  --end-ms ... \
  --output-dir $ENGINE_MODEL_ARTIFACT_DIR
```

Model promotion remains human-reviewed through existing governance patterns.

## Portfolio Allocator

Implement `PortfolioAllocator.allocate(candidate_book, portfolio_state)`.

Inputs:

```text
EV estimates
current paper/shadow positions
correlation matrix from recent returns
liquidity constraints
regime permissions
strategy exposure
asset exposure
gross/net exposure
daily loss and drawdown state
```

Sizing:

```python
size_usd = min(
    risk_budget_usd / expected_loss_at_stop,
    max_book_depth_participation_usd,
    max_volume_participation_usd,
    max_correlation_adjusted_exposure_usd,
    max_regime_leverage_usd,
    max_exchange_limit_usd,
)
```

Skip candidate if:

```text
net_ev_bps < ENGINE_MIN_NET_EV_BPS
risk_adjusted_utility < ENGINE_MIN_RISK_ADJUSTED_UTILITY
portfolio opportunity cost is worse than another candidate
correlation-adjusted exposure is too high
regime disallows strategy
```

## Risk Gateway Upgrade

Replace signal-specific check with:

```python
RiskGateway.check_order_intent(intent, context) -> RiskGatewayDecision
```

Required deterministic checks:

```text
Data:
  stale price
  stale orderbook
  stale funding/OI
  disconnected exchange feed
  abnormal spread
  abnormal latency

Trade:
  max order notional
  max quantity
  max leverage
  price collar
  fat-finger
  minimum edge after costs
  max spread/slippage
  max participation
  stop/invalidation/horizon present

Portfolio:
  gross/net exposure
  correlated exposure
  drawdown
  daily loss
  open risk
  liquidation distance

Strategy:
  cooldown
  daily strategy loss
  hit-rate degradation
  model drift
  regime permission
  post-news embargo
  macro no-trade window

Operational:
  duplicate intent
  approved config version
  approved model version
  kill switch inactive
  operator permission where required
```

Decision values remain:

```text
allow, reject, halt, tighten
```

`tighten` can reduce size or require debate. It cannot relax risk.

## Execution Gateway

Implement:

```python
class ExecutionAdapter(Protocol):
    async def submit(intent: OrderIntent) -> ExecutionReport: ...
```

Adapters:

```text
PaperAdapter:
  simulates fill, fee, slippage, partial fill assumptions

ShadowAdapter:
  records what would have been submitted
  never calls exchange
  status = accepted or rejected based on simulated adapter validation
```

No live adapter class in this milestone.

Execution algorithms supported as intent types:

```text
marketable_limit
post_only
twap
vwap
pov
```

First implementation behavior:

- `marketable_limit`: immediate simulated fill inside slippage cap.
- `post_only`: shadow accepted but paper fill only if mid crosses simulated limit.
- `twap/vwap/pov`: split into child paper reports internally; no live order placement.

## Position Manager

Implement `PositionManager.on_execution_report()` and `PositionManager.on_features()`.

Rules:

```text
Partial TP:
  reduce 25–50% at first target when EV decays or vol spikes

Trailing:
  activate only after favorable excursion threshold

Time stop:
  exit if horizon expires without confirmation

Thesis degradation:
  de-risk if key feature group flips

News shock:
  freeze scaling; recompute EV; tighten or exit

Vol expansion:
  do not widen stop unless pre-approved strategy rule says market-wide noise
```

All actions create new `OrderIntent` in paper/shadow mode only.

## Read-only API Endpoints

Register in `app/engine/routes.py`.

Protected with existing `AGENT_API_BEARER_TOKEN` outside dev/test/local.

```http
GET /engine/status
GET /engine/events
GET /engine/events/{event_id}
GET /engine/features
GET /engine/regime/latest
GET /engine/candidates
GET /engine/candidates/{candidate_id}
GET /engine/candidate-book/latest
GET /engine/ev-estimates
GET /engine/allocations
GET /engine/evidence-packs/{evidence_pack_id}
GET /engine/debate-decisions
GET /engine/order-intents
GET /engine/execution-reports
GET /engine/positions
GET /engine/reconciliation
GET /engine/model-versions
GET /engine/retention
```

No new mutating API endpoints.

## Discord Changes

Replace signal alert copy with candidate alert copy:

```text
🚨 AI Candidate — BTC LONG
Strategy: directional_momentum_v2
Net EV: +12.4 bps
Risk utility: 0.42
Regime: bull / unstable 0.31
Allocator size: $X
Debate: approved/downgraded/not required
Mode: paper or shadow

approve candidate <candidate_id>
reject candidate <candidate_id>
```

Existing approval command parser should support:

```text
approve candidate <id>
reject candidate <id>
candidate <id>
```

Existing `approve signal` can be removed or mapped to candidate lookup during one migration release.

## Retention and Rollups

Implement `RetentionService`.

Defaults:

```text
normalized high-frequency events: 7 days
feature_values: 14 days
feature_rollups: 365 days
regime snapshots: 365 days
candidates/evidence/debate/risk/execution/positions/attribution: indefinite
```

Rollups:

```text
1m, 5m, 1h aggregates by asset/feature
min, max, avg, last, count, quality_avg
```

Retention runs daily and records to `retention_runs`.

## Replay Lab

Implement three replay modes:

```text
Signal replay:
  did alpha fire using only point-in-time data?

Decision replay:
  would scorer/allocator/risk/debate approve?

Execution replay:
  would paper/shadow order fill under stored assumptions?
```

Replay must use:

```text
event_ts_ms
received_ts_ms
computed_ts_ms
feature version
model version
config version
```

No replay may query current feature state for historical decisions.

## Testing Plan

### Unit tests

Add tests for:

- `NormalizedEvent` and `FeatureValue` timestamp integrity.
- Feature store point-in-time reads.
- Regime vector computation with missing data.
- Strategy permission gates.
- Alpha candidate generation.
- EV fallback and trained-model inference.
- Debate priority formula.
- EvidencePack construction.
- DebateDecision constraints.
- Portfolio allocation constraints.
- RiskGateway `check_order_intent`.
- PaperAdapter and ShadowAdapter.
- PositionThesis transitions.
- Retention rollups.

### Integration tests

Add tests for:

```text
raw newswire event -> normalized event -> feature -> candidate
candidate -> EV -> allocation -> risk -> no debate -> paper execution
candidate -> EV -> allocation -> risk -> debate -> downgrade -> reduced paper intent
risk rejected candidate is still persisted
shadow mode records ExecutionReport but no paper position
position manager time stop emits paper exit intent
```

### API tests

Add read-only tests for every `/engine/*` endpoint.

### Migration tests

Run:

```bash
uv run pytest -q
uv run alembic upgrade head
uv run alembic downgrade -1
```

where supported by existing test harness.

## Acceptance Criteria

Implementation is complete when:

1. Current `SignalEngine` no longer controls the canonical autonomy decision path.
2. Every raw market/news/tradfi input used by decisions has `received_ts_ms`.
3. All generated candidates are persisted, including rejected/expired candidates.
4. Candidate decisions use EV + portfolio allocation, not scalar score threshold alone.
5. AI debate consumes `EvidencePack` and returns `DebateDecision`.
6. Risk gateway can block any order intent deterministically.
7. Only `PaperAdapter` and `ShadowAdapter` can run.
8. No code path can submit signed live exchange orders.
9. Read-only `/engine/*` endpoints expose ledger/features/candidates/decisions/execution reports.
10. Replay can reconstruct at least signal and decision replay point-in-time.
11. Tests cover no-live-execution invariants.
12. Docs explain migration from old signal loop to new engine.

## Rollout Plan

1. Land schemas, migrations, and repository methods.
2. Land engine services behind `ENGINE_ENABLED=false`.
3. Replace autonomy service internals with engine path.
4. Enable in local/test with deterministic fallback scorer.
5. Enable paper mode in staging.
6. Enable shadow mode in staging.
7. Run replay and attribution reports.
8. Promote scorer models only through governance review.
9. Keep live execution disabled indefinitely until a separate security/key-isolation project is approved.

## Non-goals

This plan explicitly does **not** implement:

- signed Hyperliquid exchange adapter
- private key handling
- live order routes
- live canary trading
- automatic config/model promotion
- AI-controlled risk overrides
- AI in execution hot path

## Assumptions

- Postgres remains the only required storage system.
- scikit-learn/joblib/pandas dependencies are acceptable.
- Model artifacts are local filesystem artifacts referenced by DB rows.
- Current API compatibility can break where necessary.
- Discord behavior should remain operational, but command names may change from `signal` to `candidate`.
- Paper/shadow correctness and auditability are higher priority than low-latency execution.








<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Create the new institutional engine schema layer. _(done)_
- [x] 2. Add Postgres migrations and repository methods for event ... _(done)_
- [x] 3. Replace market-map/signal generation with normalized even... _(done)_
- [x] 4. Add learned EV scorer infrastructure and offline training... _(done)_
- [x] 5. Add portfolio allocator and upgraded deterministic risk g... _(done)_
- [x] 6. Convert high-stakes AI debate into EvidencePack-based adj... _(done)_
- [x] 7. Add paper/shadow execution gateway and deterministic posi... _(done)_
- [x] 8. Add read-only engine API endpoints and Discord output upd... _(done)_
- [x] 9. Add retention, rollups, replay, attribution, and governan... _(done)_
- [x] 10. Update tests, docs, config, metrics, and migration compat... _(done)_

<!-- pi-plan-progress:end -->
