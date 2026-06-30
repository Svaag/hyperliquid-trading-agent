---
created: 2026-06-30T14:59:50.906Z
source: pi-plan-mode
status: accepted-for-execution
---

# Institutional Engine Strategy-Portfolio Upgrade Plan

## Summary

Implement the upgrade as a full staged roadmap, with the first deliverable locked to **Wave 1A: strategy-regime candidate nucleus**.

Primary outcome: make the engine pass readiness by adding trustworthy evidence diversity, not by weakening the gate.

Key decisions locked:

- Default/runtime examples become **shadow-only** until readiness passes.
- Persistence uses **first-class tables/migrations**.
- First strategy wave is **Wave 1A**, not full Wave 1.
- Council is **deterministic by default**, with optional LLM debate only for high-priority packets.
- Learner is **offline/report-only first** and may only propose governance-reviewed config diffs.
- No paper/live enablement, no RiskGateway bypass, no RL auto-apply.

## Grounded Current State

Relevant existing repo facts:

- Engine code lives under `hyperliquid_trading_agent/app/engine/`.
- Current active strategies are hardcoded in `engine/service.py`:
  - `directional_momentum_v2`
  - `support_resistance_reversion_v2`
  - `microstructure_ofi_v1`
  - `news_event_alpha_v1`
- Readiness gate is in `engine/readiness.py`.
- Current hard strategy dominance cap is `engine_readiness_max_strategy_allocation_share_pct = 55.0`.
- Existing throttles are in `engine/throttles.py`, but allocation persistence currently happens inside `PortfolioAllocator` before service-level strategy metadata is attached, so recent allocation-share throttling can miss strategy metadata.
- Replay comparison endpoint already exists:
  - `POST /engine/replay-comparisons/run`
- Regime engine exists but is still narrow:
  - `engine/regime.py`
  - `RegimeVector` in `engine/schemas.py`
- Liquidation subsystem exists but is not connected to engine alpha generation:
  - `app/liquidations/signals.py`
  - `LiquidationSignalBridge`
- Legacy autonomy signals exist and can be adapted:
  - `app/autonomy/signals.py`
  - repository methods around `list_autonomy_trade_signals`
- `.env.example` and `Settings` currently default engine paper mode to enabled; this must become shadow-only.

## Implementation Steps

1. Make engine defaults and examples shadow-only.
2. Add strategy metadata contracts and a strategy registry.
3. Expand feature ingestion and deterministic regime labeling.
4. Implement Wave 1A strategies and adapters.
5. Add allocation diversity control and fix allocation metadata persistence.
6. Add CandidateTradePacket and deterministic Agentic Council reviews.
7. Add first-class persistence, repository methods, and API routes.
8. Build strategy-regime performance scorecards.
9. Upgrade replay comparison and readiness gate checks.
10. Add offline contextual-bandit report-only recommendations.
11. Update tests, docs, and runbooks.
12. Execute the clean shadow observation and paper-promotion package.

## Wave 1A Strategy Scope

### Active alpha strategies counting toward breadth

Implement these as deterministic strategies:

1. `microstructure_ofi_v2`
   - Family: `microstructure`
   - Uses: `mid`, `spread_bps`, `top_depth_usd`, `top_imbalance`
   - Valid when liquidity is `deep|normal`, spread is `tight|normal`, regime is not toxic.

2. `liquidation_cascade_v1`
   - Family: `liquidation_pressure`
   - Uses: liquidation notional/imbalance, source integrity, price impulse.
   - Long when short liquidations dominate and upside impulse confirms.
   - Short when long liquidations dominate and downside impulse confirms.

3. `liquidation_mean_revert_v1`
   - Family: `liquidation_pressure`
   - Uses: liquidation cluster plus stabilization/exhaustion features.
   - Trades post-flush reversion only when spread/liquidity are acceptable.

4. `funding_carry_v1`
   - Family: `funding_basis`
   - Uses: `funding_hourly`, funding z-score, volatility/liquidity state.
   - Positive funding → short carry candidate if trend risk allows.
   - Negative funding → long carry candidate if trend risk allows.

5. `oi_breakout_v1`
   - Family: `trend_following`
   - Uses: price return, OI velocity, liquidity/spread confirmation.
   - Long on upside breakout + OI expansion.
   - Short on downside breakout + OI expansion.

### Bridge strategy

6. `legacy_signal_adapter_v1`
   - Family: `legacy_bridge`
   - Wraps legacy autonomy `TradeSignal` rows into `AlphaCandidate`.
   - Must set `counts_for_breadth=false`.
   - Must dedupe by `legacy_signal_id`, asset, side, signal type, and horizon.
   - Does not count as independent alpha breadth unless explicitly promoted later.

### Defensive policy

7. `regime_defensive_flat_v1`
   - Family: `risk_off_defensive`
   - Emits `side="flat"` no-trade/risk-off candidates.
   - Must set `counts_for_breadth=false`.
   - Must never create an `OrderIntent`.
   - Allocator returns `status="skip"` with reason `defensive_flat_no_trade`.

## Schema and Contract Details

### Update `engine/schemas.py`

Add optional/defaulted fields to `AlphaCandidate` for backward compatibility:

```python
strategy_version: str = "unknown"
strategy_family: str = "unknown"
valid_regimes: list[str] = Field(default_factory=list)
required_features: list[str] = Field(default_factory=list)
feature_coverage_pct: float = Field(default=0.0, ge=0.0, le=100.0)
expected_edge_bps: float = 0.0
risk_tags: list[str] = Field(default_factory=list)
counts_for_breadth: bool = True
portfolio_concentration_impact: dict[str, Any] = Field(default_factory=dict)
source_integrity: dict[str, Any] = Field(default_factory=dict)
```

Add new models:

- `StrategySpec`
- `CandidateTradePacket`
- `CouncilVote`
- `CouncilReview`
- `StrategyRegimePerformance`
- `BanditRecommendation`
- `BanditPolicySnapshot`

### StrategySpec

Add in `engine/alpha/base.py` or new `engine/strategy_registry.py`:

```python
class StrategySpec(BaseModel):
    strategy_id: str
    version: str
    family: str
    supported_assets: list[str]
    supported_venues: list[str]
    supported_horizons: list[str]
    required_features: list[str]
    valid_regimes: list[str]
    max_candidates_per_run: int
    max_allocation_share_pct: float
    cooldown_ms: int
    min_confidence: float
    min_ev_bps: float
    risk_tags: list[str]
    counts_for_breadth: bool = True
```

Each strategy must expose:

```python
spec: StrategySpec
strategy_id: str
generate(snapshot: FeatureSnapshot, regime: RegimeVector, *, timestamp_ms: int) -> list[AlphaCandidate]
```

Existing strategies must be registered too.

## Feature and Regime Work

### Engine feature ingestion

Update `engine/service.py` to record these additional inputs:

1. `meta_and_asset_ctxs`
   - Normalize as `event_type="meta_and_asset_ctxs"` or `funding_oi`.
   - Derive:
     - `funding_hourly`
     - `open_interest`
     - `day_volume_usd`

2. Liquidation bridge features, if `liquidations_enabled=true`
   - Pass `LiquidationSignalBridge` into `InstitutionalEngineService`.
   - For each core symbol, record features:
     - `liq_notional_1m`
     - `liq_notional_5m`
     - `long_vs_short_liq_imbalance_5m`
     - `largest_single_liq_5m`
     - `confirmed_only_liq_score_5m`
     - `liq_event_count_5m`
     - `source_mix_5m`

3. Rolling price/OI features
   - Add deterministic rollups:
     - `mid_return_1m_bps`
     - `mid_return_5m_bps`
     - `mid_return_15m_bps`
     - `realized_vol_5m_bps`
     - `realized_vol_15m_bps`
     - `oi_delta_5m_pct`
     - `oi_velocity_z`

### Regime expansion

Extend `RegimeVector` with defaulted fields:

```python
volatility_state: str = "unknown"
funding_state: str = "unknown"
oi_state: str = "unknown"
liquidation_state: str = "unknown"
orderflow_state: str = "unknown"
news_state: str = "no_event"
correlation_state: str = "unknown"
session_state: str = "unknown"
feature_coverage_pct: float = 0.0
regime_label: str = "unknown"
```

Use deterministic labels:

- `volatility_state`
  - `<25%`: `compressed`
  - `<70%`: `normal`
  - `<90%`: `elevated`
  - otherwise: `extreme`

- `funding_state`
  - z-score `>=2`: `positive_extreme`
  - z-score `<=-2`: `negative_extreme`
  - `abs(z)<1`: `neutral`

- `oi_state`
  - z-score `>1`: `expanding`
  - z-score `<-1`: `contracting`
  - else: `flat`

- `liquidation_state`
  - large positive long-minus-short imbalance: `long_flush`
  - large negative long-minus-short imbalance: `short_squeeze`
  - both active: `mixed`
  - otherwise: `calm`

- `orderflow_state`
  - top imbalance `>0.2`: `buy_pressure`
  - `<-0.2`: `sell_pressure`
  - else: `balanced`

- `session_state`
  - weekend if Saturday/Sunday
  - else UTC session bucket: `asia`, `europe`, `us`, or `rollover`

## Allocation Diversity Controller

Add `engine/diversity.py` with `PortfolioDiversityController`.

Settings to add:

```python
engine_diversity_controller_enabled: bool = True
engine_diversity_lookback_hours: int = 24
engine_diversity_strategy_target_share_pct: float = 45.0
engine_diversity_strategy_hard_share_pct: float = 55.0
engine_diversity_family_hard_share_pct: float = 60.0
engine_diversity_symbol_strategy_hard_share_pct: float = 35.0
engine_diversity_min_active_strategies_24h: int = 5
engine_diversity_min_active_families_24h: int = 3
engine_diversity_min_window_samples: int = 10
```

Controller behavior:

- Simulate allocation impact before intent creation.
- Hard skip if projected:
  - strategy share `>55%`
  - family share `>60%`
  - symbol+strategy share `>35%`
- Target throttle if recent strategy share is already `>=45%`.
- Persist every throttle/allow decision to `allocation_diversity_events`.
- Expose status in `/engine/status`.

Also fix allocation persistence:

- Remove repository persistence from `PortfolioAllocator.allocate`, or make it optional.
- Persist allocation only after service enriches it with:
  - `strategy_id`
  - `strategy_version`
  - `strategy_family`
  - `asset`
  - `venue`
  - diversity metadata

## Agentic Council

Add deterministic Council module:

```text
engine/council.py
```

Roles:

- Risk Council
- Regime Council
- Replay Council
- Portfolio Council
- Microstructure Council
- News/Event Council
- Execution Council

Pipeline in `engine/service.py` becomes:

```text
candidate
-> EV estimate
-> allocation
-> diversity preview/control
-> CandidateTradePacket
-> provisional OrderIntent only if allocated and non-flat
-> RiskGateway.check_order_intent
-> deterministic CouncilReview
-> optional high-priority debate
-> submit only if RiskGateway allowed AND Council allowed
```

Council output:

```json
{
  "decision": "allow_shadow | allow_paper | reject | needs_more_evidence",
  "vetoes": [],
  "warnings": [],
  "required_evidence": [],
  "regime_fit_score": 0.0,
  "strategy_regime_score": 0.0,
  "portfolio_impact_score": 0.0
}
```

Hard vetoes:

- RiskGateway reject.
- Strategy invalid for current regime.
- Concentration cap breach.
- Latest replay stale/failed/missing.
- Critical data coverage missing.

## Persistence and Migrations

Add Alembic revision:

```text
0019_engine_strategy_regime_council_learning.py
down_revision = "0018_liquidations"
```

Create tables:

1. `strategy_specs`
2. `strategy_regime_performance`
3. `council_reviews`
4. `council_votes`
5. `allocation_diversity_events`
6. `bandit_policy_snapshots`
7. `bandit_recommendations`

Add matching SQLAlchemy models in `app/db/models.py`.

Add repository methods:

```python
upsert_strategy_spec
list_strategy_specs
get_strategy_spec

upsert_strategy_regime_performance
list_strategy_regime_performance

record_council_review
list_council_reviews

record_council_vote
list_council_votes

record_allocation_diversity_event
list_allocation_diversity_events

upsert_bandit_policy_snapshot
record_bandit_recommendation
list_bandit_recommendations
```

## API Routes

Add to `engine/routes.py`:

```http
GET /engine/strategies
GET /engine/strategies/{strategy_id}
GET /engine/strategy-regime-performance
GET /engine/strategy-regime-performance/{strategy_id}
POST /engine/strategy-regime-performance/refresh
GET /engine/council-reviews
GET /engine/diversity-events
GET /engine/bandit-recommendations
POST /engine/bandit-recommendations/run
```

All routes remain auth-protected outside dev/test/local.

## Readiness Gate Upgrades

Add settings:

```python
engine_readiness_clean_window_start_ms: int = 0
engine_readiness_min_active_strategy_count_24h: int = 5
engine_readiness_min_active_strategy_family_count_24h: int = 3
engine_readiness_max_strategy_family_allocation_share_pct: float = 60.0
engine_readiness_max_symbol_strategy_allocation_share_pct: float = 35.0
engine_readiness_min_candidate_strategy_metadata_coverage_pct: float = 100.0
engine_readiness_min_council_review_coverage_pct: float = 95.0
engine_readiness_min_strategy_regime_evidence_coverage_pct: float = 80.0
engine_readiness_min_strategy_regime_sample_count: int = 5
engine_readiness_min_strategy_regime_score: float = 45.0
engine_readiness_require_latest_replay: bool = True
engine_readiness_min_replay_window_hours: int = 24
engine_readiness_min_replay_sample_size: int = 50
```

Add hard blocks:

- `insufficient_active_strategy_count`
- `insufficient_active_strategy_family_count`
- `strategy_family_allocation_dominance`
- `symbol_strategy_allocation_dominance`
- `candidate_strategy_metadata_missing`
- `candidate_regime_missing`
- `council_review_coverage_low`
- `risk_gateway_coverage_low`
- `strategy_regime_evidence_coverage_low`
- `strategy_regime_score_low`
- `replay_comparison_missing`
- `replay_comparison_stale`
- `replay_comparison_failed`

Readiness must exclude:

- `legacy_bridge` from active alpha breadth unless `counts_for_breadth=true`.
- `risk_off_defensive` from active alpha breadth.
- flat/no-trade candidates from allocation breadth.

## Replay Upgrade

Update `engine/replay_compare.py`.

Add `advisory_pass` to replay status literals.

Replay status mapping:

- `candidate_better` → `passed`
- safe inconclusive with no critical regression → `advisory_pass`
- unsafe regression → `failed`

Add replay metrics:

- active non-legacy alpha strategy count
- active alpha family count
- dominant strategy share
- dominant family share
- symbol+strategy share
- regime coverage
- strategy metadata coverage
- council coverage
- RiskGateway coverage
- strategy-regime evidence coverage

A newer passing/advisory replay supersedes older failed replay blockers.

## Offline Learner

Add `engine/learner.py`.

Stage 1 only:

- report-only contextual bandit
- no config mutation
- no order mutation
- no sizing/leverage changes
- no live/paper enablement

Inputs:

- `strategy_regime_performance`
- Council veto/reject rates
- Risk reject rates
- concentration metrics
- replay status

Allowed recommendations:

- adjust strategy quota
- adjust candidate quota
- adjust min confidence
- adjust min EV
- adjust cooldown
- recommend no-trade

Persist to `bandit_recommendations`.

Later governance integration:

- create `CandidateConfigDiff`
- status remains `proposed`
- cannot become `review_ready` without replay + shadow evidence
- `auto_apply_allowed=false` always

## Safe Defaults

Update `Settings` defaults:

```python
engine_execution_modes: str = "shadow"
engine_shadow_enabled: bool = True
engine_paper_enabled: bool = False
engine_live_enabled: bool = False
```

Update `.env.example`:

```env
ENGINE_ENABLED=false
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
```

Paper mode remains an explicit post-readiness operator action only.

## Test Plan

Add/update tests for:

1. Strategy registry
   - all existing strategies registered
   - Wave 1A specs valid
   - duplicate strategy IDs rejected

2. Strategy generation
   - each Wave 1A strategy emits candidates only in valid regimes
   - missing required features prevents candidate emission
   - defensive flat emits `side="flat"` and never creates intent
   - legacy adapter emits `counts_for_breadth=false`

3. Regime engine
   - volatility/funding/OI/liquidation/orderflow/session labels
   - `regime_label` stable and non-empty
   - feature coverage computed

4. Diversity controller
   - strategy target cap throttles at `45%`
   - hard cap blocks above `55%`
   - family cap blocks above `60%`
   - symbol+strategy cap blocks above `35%`
   - events persisted

5. Engine service
   - allocation persistence includes strategy metadata
   - allocated candidates call RiskGateway
   - CouncilReview exists before any execution report
   - shadow-only mode creates only shadow intents

6. Readiness
   - blocks missing replay
   - blocks failed/stale replay
   - blocks insufficient strategy/family breadth
   - blocks low Council coverage
   - blocks missing strategy-regime evidence
   - passes with clean diversified fixture

7. Replay
   - old failed replay superseded by newer pass/advisory pass
   - `advisory_pass` accepted by readiness
   - dominance metrics included

8. API routes
   - new routes registered
   - auth enforced outside dev/test/local

Recommended command:

```bash
uv run pytest -q
```

## Rollout Execution Plan

1. Deploy code with shadow-only settings.
2. Enable read-only data feeds required for Wave 1A:
   - Hyperliquid mids/L2/meta contexts
   - liquidation read-only adapters if available
   - no signed exchange adapters
3. Set clean window start:

```env
ENGINE_READINESS_CLEAN_WINDOW_START_MS=<deployment_ms>
```

4. Run shadow observation for 24h minimum.
5. Run replay:

```http
POST /engine/replay-comparisons/run
```

6. Verify:

```http
GET /engine/readiness
GET /engine/validation-report
GET /engine/strategy-regime-performance
GET /engine/replay-comparisons/latest
```

7. Do not enable paper until readiness returns:

```json
{
  "ready_for_paper": true,
  "grade": "pass",
  "hard_blocks": []
}
```

8. Only then prepare paper-only promotion env:

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=true
ENGINE_EXECUTION_MODES=paper,shadow
ENGINE_LIVE_ENABLED=false
```

## Final Acceptance Criteria

Implementation is complete when:

- `>=5` active non-legacy alpha strategies exist.
- `>=3` active alpha families exist.
- `regime_defensive_flat_v1` exists and does not count as alpha breadth.
- `legacy_signal_adapter_v1` exists and does not count as independent alpha breadth.
- 100% of new candidates have:
  - `strategy_id`
  - `strategy_version`
  - `strategy_family`
  - `regime_snapshot_id`
  - `feature_coverage_pct`
- Portfolio diversity controller enforces:
  - `45%` target strategy share
  - `55%` hard strategy cap
  - `60%` family cap
  - `35%` symbol+strategy cap
- 100% of allocated candidates have RiskGateway decisions.
- 95%+ of allocated candidates have CouncilReviews.
- Strategy-regime performance rows are populated.
- Latest replay is `passed` or `advisory_pass`.
- Readiness hard blocks clear before paper promotion.
- Defaults and examples are shadow-only.
- No live execution path is introduced.























<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Make engine defaults and examples shadow-only. _(done)_
- [x] 2. Add strategy metadata contracts and a strategy registry. _(done)_
- [x] 3. Expand feature ingestion and deterministic regime labeling. _(done)_
- [x] 4. Implement Wave 1A strategies and adapters. _(done)_
- [x] 5. Add allocation diversity control and fix allocation metadata persistence. _(done)_
- [x] 6. Add CandidateTradePacket and deterministic Agentic Council reviews. _(done)_
- [x] 7. Add first-class persistence, repository methods, and API routes. _(done)_
- [x] 8. Build strategy-regime performance scorecards. _(done)_
- [x] 9. Upgrade replay comparison and readiness gate checks. _(done)_
- [x] 10. Add offline contextual-bandit report-only recommendations. _(done)_
- [x] 11. Update tests, docs, and runbooks. _(done)_
- [ ] 12. Execute the clean shadow observation and paper-promotion package. _(pending)_

<!-- pi-plan-progress:end -->
