---
created: 2026-06-16T22:45:07.624Z
source: pi-plan-mode
status: accepted-for-execution
---

# Deterministic Paper-Readiness Scorecard + Shadow Validation Extensions

## Summary

Build a deterministic “ready for paper mode?” system on top of the current shadow engine. The implementation will add:

- A conservative paper-readiness scorecard.
- Shadow replay/backtest comparison using existing `replay_results`-compatible storage.
- Strategy-level candidate/allocation throttles.
- A completed simulated PnL attribution loop.
- A new unified in-app dashboard shell.
- An engine admin runbook with exact promotion and rollback steps.

We will **not** add Grafana/Prometheus stack yet.

## Answer: Are We Reinventing the Wheel by Adding This to the Existing Agent Dashboard?

Partially, but not badly.

The repo currently has two lightweight in-app HTML dashboards:

- `/governance/dashboard`
  - Existing “Agent/Governance Dashboard”
  - Hand-written HTML/JS
  - Uses localStorage bearer token
  - Fetches `/governance/dashboard/data`
- `/engine/dashboard`
  - Engine-specific static HTML report
  - Focused validation view

There is no React/Vite/Next frontend or separate dashboard app. So:

- **Adding engine readiness panels to the existing governance dashboard would be low-reinvention**: reuse the same localStorage auth, CSS style, fetch pattern, and in-app FastAPI HTML route.
- **Building a new unified dashboard shell is moderate reinvention**, but still acceptable because the current dashboard is a single HTML function, not a reusable UI framework.
- We should avoid creating a large frontend stack now. Instead, build a new lightweight unified shell in FastAPI using the same plain HTML/JS style, and point both governance and engine dashboards toward it over time.

Decision: implement a **new unified in-app dashboard shell** without introducing a frontend build system.

## Implementation Steps

1. Add deterministic engine paper-readiness scoring.
2. Add engine shadow replay/backtest comparison using existing replay-result-compatible storage.
3. Add strategy-level throttles for candidates and allocations.
4. Complete simulated PnL attribution and position marking loop.
5. Build the new unified in-app dashboard shell.
6. Extend Discord digest and alerts with readiness/replay/throttle/PnL sections.
7. Write the engine admin promotion and rollback runbook.
8. Add tests, docs, and rollout checks.

## Current State Inventory

### Already present

- Engine schemas and persistence.
- Shadow-only engine runtime.
- `/engine/validation-report`
- `/engine/dashboard`
- Scheduled Discord validation digest.
- Alerts for:
  - stale loop
  - paper intent in shadow-only mode
  - risk reject spike
  - EV drift
  - missing feature/regime data
- Governance dashboard:
  - `/governance/dashboard`
  - `/governance/dashboard/data`
- Existing replay-compatible tables:
  - `replay_results`
  - `shadow_comparisons`
- Existing repository methods:
  - `record_replay_result`
  - `list_replay_results`
  - `record_shadow_comparison`
  - `list_shadow_comparisons`
- Existing engine PnL attribution record shape:
  - `PnLAttributionRecord`
  - `record_pnl_attribution`
  - `list_pnl_attribution`

### Important current limitations

- Engine `ReplayLab.audit_only()` is currently a stub.
- Engine position theses open from simulated fills but are not regularly marked/closed.
- PnL attribution exists but is not scheduled end-to-end.
- Strategy dominance is only reported, not controlled.
- Readiness is human-interpreted from dashboard/digest, not deterministic pass/fail.
- Current `/engine/dashboard` is a simple server-rendered report, not an interactive unified dashboard.

## 1. Deterministic Paper-Readiness Scorecard

### New module

Add:

```text
hyperliquid_trading_agent/app/engine/readiness.py
```

### Main API

```python
async def build_paper_readiness_scorecard(
    repository: Repository,
    settings: Settings,
    engine_service: InstitutionalEngineService | None,
    *,
    window_hours: int | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    ...
```

### New route

Add read-only endpoint:

```http
GET /engine/readiness
```

Query params:

```text
window_hours=24
limit=1000
```

Response shape:

```json
{
  "generated_at_ms": 1780000000000,
  "ready_for_paper": false,
  "score": 72,
  "grade": "blocked",
  "window": {
    "hours": 24,
    "start_ms": 1779913600000,
    "end_ms": 1780000000000
  },
  "hard_blocks": [
    {
      "code": "insufficient_shadow_observation",
      "severity": "critical",
      "detail": "Need >=24h shadow observation; observed 7.3h"
    }
  ],
  "warnings": [
    {
      "code": "strategy_dominance",
      "severity": "warning",
      "detail": "directional_momentum produced 67% of allocations"
    }
  ],
  "checks": {
    "shadow_integrity": {...},
    "engine_reliability": {...},
    "data_completeness": {...},
    "decision_quality": {...},
    "risk_gateway": {...},
    "execution_simulation": {...},
    "strategy_diversity": {...},
    "pnl_calibration": {...}
  },
  "metrics": {...},
  "recommendation": "continue_shadow",
  "next_actions": [...]
}
```

### Conservative readiness thresholds

Use these defaults:

```env
ENGINE_READINESS_ENABLED=true
ENGINE_READINESS_WINDOW_HOURS=24
ENGINE_READINESS_MIN_RUNS=100
ENGINE_READINESS_MIN_CANDIDATES=250
ENGINE_READINESS_MIN_SHADOW_INTENTS=50
ENGINE_READINESS_MAX_RUNTIME_ERRORS=0
ENGINE_READINESS_MAX_CRITICAL_ALERTS=0
ENGINE_READINESS_MAX_PAPER_INTENTS_IN_SHADOW=0
ENGINE_READINESS_MAX_LIVE_INTENTS=0
ENGINE_READINESS_MIN_EV_COVERAGE_PCT=95
ENGINE_READINESS_MIN_FEATURE_COVERAGE_PCT=95
ENGINE_READINESS_MIN_REGIME_COVERAGE_PCT=95
ENGINE_READINESS_MAX_RISK_REJECT_RATE_PCT=25
ENGINE_READINESS_MIN_ALLOCATION_RATE_PCT=5
ENGINE_READINESS_MAX_ALLOCATION_RATE_PCT=60
ENGINE_READINESS_MAX_STRATEGY_ALLOCATION_SHARE_PCT=55
ENGINE_READINESS_MAX_AVG_SLIPPAGE_BPS=8
ENGINE_READINESS_MAX_FILL_FAILURE_RATE_PCT=5
ENGINE_READINESS_MIN_SCORE_TO_PASS=85
```

### Hard blocks

`ready_for_paper=false` if any hard block is present:

1. `live_enabled`
   - `ENGINE_LIVE_ENABLED=true`
2. `paper_intents_in_shadow_only`
   - paper intent/report exists while configured shadow-only
3. `engine_loop_stale`
   - no recent loop within stale threshold
4. `runtime_errors_present`
   - `engine_service.status().last_error` non-null or recent monitor critical alert
5. `insufficient_shadow_observation`
   - fewer than 24h since first shadow event/intent in window
6. `insufficient_sample_size`
   - fewer than 250 candidates or 50 shadow intents
7. `missing_core_data`
   - missing feature/regime snapshots for any core symbol
8. `risk_reject_spike_critical`
   - recent reject spike above threshold and dominated by stale/invalid data issues
9. `replay_comparison_failed`
   - latest required shadow replay comparison failed
10. `position_marking_unhealthy`
   - PnL attribution loop has not produced records after open positions exist long enough

### Scoring model

Start at 100, subtract deterministic penalties:

```text
- critical hard block present: readiness still scored but grade blocked
- warning alert: -3 each, capped at -15
- EV coverage below 95%: -10
- feature/regime coverage below 95%: -10
- risk reject rate >25%: -10
- allocation rate outside 5–60%: -8
- strategy allocation dominance >55%: -10
- avg slippage >8 bps: -8
- fill failure rate >5%: -8
- PnL attribution missing when required: -10
- positive-EV buckets with negative realized PnL after enough samples: -15
```

Grade mapping:

```text
score >= 85 and no hard blocks -> pass
score >= 70 and no hard blocks -> watch
otherwise -> blocked
```

### Recommendation values

```text
continue_shadow
fix_data_quality
tighten_strategy_throttles
review_risk_rejects
run_replay_comparison
ready_for_paper
rollback_to_shadow
```

## 2. Shadow Replay / Backtest Compare

### Goal

Compare decisions across runs/configs without adding new tables yet.

### Storage decision

Use existing `replay_results`-compatible storage.

Do **not** add a migration now.

### New module

Add:

```text
hyperliquid_trading_agent/app/engine/replay_compare.py
```

### Core service

```python
class EngineReplayComparisonService:
    async def compare_variant(
        self,
        *,
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
        window_start_ms: int,
        window_end_ms: int,
        universe: list[str],
        dataset_id: str | None = None,
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        ...
```

### Required replay artifact metadata

Each stored replay result must include:

```json
{
  "schema_version": 1,
  "artifact_type": "engine_shadow_comparison",
  "baseline_engine_version": "git_sha_or_app_version",
  "candidate_engine_version": "git_sha_or_app_version",
  "baseline_config_hash": "...",
  "candidate_config_hash": "...",
  "scorer_variant": "deterministic_fallback_v1",
  "threshold_variant": {
    "engine_min_net_ev_bps": 8,
    "engine_min_risk_adjusted_utility": 0.25
  },
  "data_window": {
    "start_ms": 1779913600000,
    "end_ms": 1780000000000
  },
  "replay_dataset_id": "engine_replay_2026_06_17_24h_btc_eth_hype",
  "market_universe": ["BTC", "ETH", "HYPE"],
  "verdict": "candidate_better|candidate_worse|inconclusive|failed",
  "promotion_decision": "do_not_promote|eligible_for_review|promote_to_paper",
  "notes": []
}
```

### Metrics to compare

Baseline and candidate metrics:

```json
{
  "candidate_count": 123,
  "ev_estimate_count": 123,
  "allocated_count": 42,
  "allocation_rate_pct": 34.1,
  "shadow_intent_count": 40,
  "risk_reject_count": 3,
  "risk_reject_rate_pct": 7.1,
  "avg_net_ev_bps": 12.4,
  "avg_risk_adjusted_utility": 0.41,
  "avg_slippage_bps": 2.2,
  "fees_usd": 12.5,
  "total_pnl_usd": 0.0,
  "strategy_allocation_share": {
    "directional_momentum": 0.43,
    "microstructure_ofi": 0.24
  }
}
```

Diffs:

```json
{
  "allocated_count_delta": 5,
  "allocation_rate_delta_pct": 4.2,
  "risk_reject_rate_delta_pct": -2.1,
  "avg_net_ev_delta_bps": 1.5,
  "avg_slippage_delta_bps": 0.3,
  "dominance_delta_pct": -6.0
}
```

### Variant comparison approach for first implementation

Because a full point-in-time engine replay is not wired yet, implement a conservative deterministic comparison:

1. Pull historical candidates/EVs/features/allocations/reports from the selected window.
2. Re-score allocation eligibility using candidate config thresholds.
3. Simulate how many existing candidates would have passed under baseline vs candidate thresholds.
4. Recompute allocation-rate, risk/reward eligibility, strategy distribution, and expected simulated execution cost.
5. Mark caveat:

```json
"caveats": ["ledger_replay_without_market_reconstruction_v1"]
```

This is acceptable for threshold/scorer/config comparison before full market reconstruction exists.

### New routes

```http
GET /engine/replay-comparisons
GET /engine/replay-comparisons/latest
POST /engine/replay-comparisons/run
```

Important: `POST /engine/replay-comparisons/run` is an operational analysis action, not a trading mutation. It must be auth-protected.

Request body:

```json
{
  "window_hours": 24,
  "universe": ["BTC", "ETH", "HYPE"],
  "baseline_config": {
    "engine_min_net_ev_bps": 8,
    "engine_min_risk_adjusted_utility": 0.25
  },
  "candidate_config": {
    "engine_min_net_ev_bps": 10,
    "engine_min_risk_adjusted_utility": 0.35
  },
  "variant_id": "tighten_ev_thresholds_v1"
}
```

Response returns stored replay artifact.

### Verdict logic

```text
candidate_better if:
- risk reject rate does not increase by >5 pct points
- avg slippage does not increase by >2 bps
- allocation dominance decreases or stays under cap
- candidate allocation count remains >=50% of baseline
- expected EV improves by >=1 bps

candidate_worse if:
- risk reject rate increases by >10 pct points
- allocation count collapses below 25% of baseline
- strategy dominance breaches cap
- avg slippage increases by >5 bps

otherwise inconclusive
```

## 3. Strategy-Level Throttles

### Goal

Prevent one strategy from dominating candidates, allocations, or simulated executions.

### New config

```env
ENGINE_STRATEGY_THROTTLES_ENABLED=true
ENGINE_STRATEGY_MAX_CANDIDATES_PER_LOOP=15
ENGINE_STRATEGY_MAX_ALLOCATIONS_PER_LOOP=3
ENGINE_STRATEGY_MAX_ALLOCATION_SHARE_PCT=55
ENGINE_STRATEGY_THROTTLE_LOOKBACK_HOURS=24
ENGINE_STRATEGY_THROTTLE_COOLDOWN_LOOPS=3
```

### New module

```text
hyperliquid_trading_agent/app/engine/throttles.py
```

### Main class

```python
class StrategyThrottleController:
    async def filter_candidates(
        self,
        candidates: list[AlphaCandidate],
        *,
        repository: Repository,
        timestamp_ms: int,
    ) -> tuple[list[AlphaCandidate], list[dict[str, Any]]]:
        ...

    async def allow_allocation(
        self,
        candidate: AlphaCandidate,
        *,
        current_loop_allocations: list[AllocationDecision],
        repository: Repository,
        timestamp_ms: int,
    ) -> tuple[bool, list[str], dict[str, Any]]:
        ...
```

### Candidate throttle behavior

Within each loop:

1. Group candidates by `strategy_id`.
2. Sort each strategy group by:
   - raw alpha score descending
   - confidence descending
   - newest first
3. Keep at most `ENGINE_STRATEGY_MAX_CANDIDATES_PER_LOOP`.
4. Candidates removed by throttle should still be persisted as candidates with:
   - `status="throttled"`
   - `metadata.throttle_reason="max_candidates_per_loop"`
   - `metadata.exchange_actions=[]`

### Allocation throttle behavior

Before producing an order intent:

1. Count current loop allocations by strategy.
2. Count recent lookback allocations by strategy from repository.
3. Compute strategy allocation share.
4. Block allocation if:
   - per-loop allocation cap exceeded
   - recent allocation share exceeds configured cap
   - strategy is in cooldown after repeated dominance
5. Allocation decision should be persisted with:
   - `status="skip"`
   - `reason_codes=["strategy_throttle"]`
   - detailed metadata

### Readiness integration

Strategy throttles feed readiness:

- warning if any strategy >45% allocation share
- hard block if any strategy >55% allocation share during readiness window
- recommendation: `tighten_strategy_throttles`

## 4. PnL Attribution Loop Completion

### Goal

Ensure simulated positions get marked and attributed regularly so EV calibration can become meaningful.

### New module

```text
hyperliquid_trading_agent/app/engine/pnl_loop.py
```

### Main service

```python
class EnginePnLAttributionLoopService:
    async def run_once(self) -> dict[str, Any]:
        ...
```

### New config

```env
ENGINE_PNL_ATTRIBUTION_ENABLED=true
ENGINE_PNL_ATTRIBUTION_INTERVAL_SECONDS=300
ENGINE_PNL_ATTRIBUTION_MARK_SOURCE=all_mids
ENGINE_PNL_ATTRIBUTION_CLOSE_ON_EXPIRED_HORIZON=true
ENGINE_PNL_ATTRIBUTION_MAX_POSITION_AGE_HOURS=48
ENGINE_PNL_ATTRIBUTION_MIN_MARK_INTERVAL_SECONDS=60
```

### Marking logic

For every open `PositionThesis`:

1. Fetch latest mark price from Hyperliquid `all_mids`.
2. Determine entry price from linked execution report.
3. Determine size from execution report.
4. Compute unrealized PnL:
   - long: `(mark - entry) * size`
   - short: `(entry - mark) * size`
5. Fees from execution report.
6. Funding remains `0.0` initially unless available later.
7. Record attribution window:
   - `window_start_ms = previous attribution window_end_ms` if found, else `opened_at_ms`
   - `window_end_ms = now_ms`
8. Persist `PnLAttributionRecord`.

### Stop/target/invalidation handling

Close simulated position thesis if any condition holds:

1. Long stop: `mark <= stop`
2. Short stop: `mark >= stop`
3. Long target: `mark >= first target`
4. Short target: `mark <= first target`
5. Max position age exceeded
6. Expected horizon expired when parseable

Close should update `PositionThesis`:

```json
{
  "position_state": "closed",
  "closed_at_ms": 1780000000000,
  "degradation_reasons": ["stop_hit|target_hit|max_age|horizon_expired"]
}
```

### Attribution fields

Use:

```json
{
  "alpha_pnl_usd": gross_mark_to_market_pnl,
  "timing_pnl_usd": 0,
  "execution_pnl_usd": -slippage_cost_usd,
  "fees_usd": fees_usd,
  "funding_usd": 0,
  "residual_pnl_usd": 0,
  "total_pnl_usd": gross_mark_to_market_pnl - fees_usd - slippage_cost_usd,
  "metrics": {
    "entry_px": 100000,
    "mark_px": 100250,
    "size": 0.01,
    "side": "long",
    "unrealized_pnl_usd": 2.5,
    "return_bps": 25,
    "holding_ms": 300000,
    "source": "all_mids"
  }
}
```

### Scheduling

Wire into app lifecycle as a background task only when:

```text
ENGINE_ENABLED=true
ENGINE_PNL_ATTRIBUTION_ENABLED=true
```

Run every `ENGINE_PNL_ATTRIBUTION_INTERVAL_SECONDS`.

### Readiness integration

Readiness warning:

```text
open positions exist but no attribution record in last 2 intervals
```

Readiness hard block:

```text
open positions exist for >15 minutes and zero attribution records exist
```

## 5. Unified In-App Dashboard Shell

### Goal

Build a new lightweight unified dashboard without React/Grafana.

### New route

```http
GET /dashboard
```

Optional redirects/links:

```http
GET /governance/dashboard -> keep existing initially, add banner/link to /dashboard
GET /engine/dashboard -> keep existing initially, add banner/link to /dashboard
```

### New data endpoint

```http
GET /dashboard/data
```

Auth-protected like existing governance dashboard data.

Response shape:

```json
{
  "runtime": {...},
  "governance": {...},
  "engine": {
    "status": {...},
    "validation_report": {...},
    "readiness": {...},
    "latest_replay_comparison": {...},
    "throttles": {...},
    "pnl": {...}
  },
  "alerts": [...]
}
```

### UI sections

Use the same plain HTML/JS/CSS pattern as current governance dashboard.

Tabs:

1. Overview
2. Engine Readiness
3. Shadow Replay
4. Strategy Throttles
5. PnL Attribution
6. Governance
7. Raw JSON

### Overview cards

Display:

- paper readiness grade
- readiness score
- hard block count
- warning count
- engine run count
- shadow intents
- risk rejects
- open simulated positions
- latest replay verdict
- strategy dominance

### Engine Readiness tab

Show:

- `ready_for_paper`
- score
- hard blocks
- warnings
- metrics
- next actions

### Shadow Replay tab

Show:

- latest replay comparison
- baseline metrics
- candidate metrics
- diffs
- verdict
- promotion decision
- caveats

Include button:

```text
Run 24h default comparison
```

This calls:

```http
POST /engine/replay-comparisons/run
```

with default body:

```json
{
  "window_hours": 24,
  "universe": ["BTC", "ETH", "HYPE"],
  "baseline_config": "current",
  "candidate_config": "current",
  "variant_id": "current_self_check"
}
```

### Strategy Throttles tab

Show:

- current throttle config
- allocation share by strategy
- candidates throttled by strategy
- allocations blocked by throttle
- recommended throttle changes

No mutating throttle controls initially.

### PnL Attribution tab

Show:

- open simulated positions
- latest marks
- attribution records by strategy
- EV bucket realized sample counts
- total simulated PnL
- stale attribution warnings

### Governance tab

Reuse current governance dashboard data:

- review-ready proposals
- replay results
- shadow comparisons
- risk decisions
- memory injections

## 6. Discord Digest Extensions

Extend existing engine validation digest with:

### Readiness section

```text
Readiness: BLOCKED 72/100
Hard blocks: 2 | Warnings: 4
Top block: insufficient_shadow_observation
Recommendation: continue_shadow
```

### Replay section

```text
Latest replay: inconclusive
Variant: tighten_ev_thresholds_v1
EV Δ: +1.2 bps | Reject Δ: -0.5% | Dominance: 42%
```

### Throttle section

```text
Strategy dominance: directional_momentum 49%
Throttled candidates: 7
Throttle blocks: 2
```

### PnL section

```text
Sim PnL: $12.40
Open positions: 4
Attribution records: 19
EV realized samples: 8
```

### Alert additions

Add alerts for:

- readiness changed from pass/watch to blocked
- latest replay verdict `candidate_worse`
- strategy throttle repeatedly blocking same strategy
- attribution loop stale
- positive EV bucket underperforming after enough samples

## 7. Engine Admin Runbook

### New doc

```text
docs/engine-paper-readiness-runbook.md
```

### Contents

Include exact sections:

1. Current safety posture
2. Shadow mode config
3. Observation checklist
4. Readiness scorecard interpretation
5. Required dashboard checks
6. Required Discord digest checks
7. Required replay comparison
8. Promotion criteria
9. Paper promotion procedure
10. Rollback procedure
11. Incident response
12. What remains explicitly forbidden

### Promotion criteria

Paper mode may be enabled only when:

```text
/engine/readiness ready_for_paper=true
score >= 85
hard_blocks=[]
latest replay comparison verdict != candidate_worse
shadow observation >= 24h
candidate_count >= 250
shadow_intent_count >= 50
paper_intent_count == 0 during shadow-only
live_enabled == false
risk reject rate <= 25%
strategy allocation share <= 55%
avg simulated slippage <= 8 bps
dashboard and Discord digest operational
```

### Promotion env change

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=true
ENGINE_EXECUTION_MODES=paper,shadow
ENGINE_LIVE_ENABLED=false
```

### Rollback env change

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
```

### Rollback triggers

Immediately roll back to shadow-only if:

- readiness becomes blocked
- any unexpected live route/flag appears
- paper orders occur outside engine paper adapter
- risk rejects spike due to stale/invalid data
- attribution loop stale for >2 intervals
- strategy dominance >70%
- avg slippage >15 bps over the latest 20 reports

## 8. Adjacent Tasks While Waiting for Shadow Results

These are useful now and do not require paper mode.

### A. Data quality scoring

Add feature/regime quality metrics:

```json
{
  "asset": "BTC",
  "feature_coverage_pct": 100,
  "latest_feature_age_seconds": 12,
  "latest_regime_age_seconds": 12,
  "quality_flags": []
}
```

Use in readiness and dashboard.

### B. Config snapshot hashing

Add deterministic config hash utility:

```python
engine_config_hash(settings) -> str
```

Include hash in:

- readiness
- replay metadata
- Discord digest
- dashboard
- order intent metadata

### C. Engine dataset IDs

Create deterministic dataset IDs for replay windows:

```text
engine_dataset_{start_ms}_{end_ms}_{universe_hash}_{config_hash}
```

### D. More precise strategy dominance metrics

Track both:

- candidate dominance
- allocation dominance
- notional dominance
- simulated PnL dominance

### E. Operator audit event

Record audit events for:

- readiness generated
- replay comparison generated
- paper promotion checklist passed
- paper rollback recommended

No mutating trade controls; only audit trail.

## API Additions Summary

### New read-only endpoints

```http
GET /engine/readiness
GET /engine/replay-comparisons
GET /engine/replay-comparisons/latest
GET /dashboard
GET /dashboard/data
```

### New operational analysis endpoint

```http
POST /engine/replay-comparisons/run
```

This must be auth-protected and must not create exchange actions.

## Config Additions Summary

```env
ENGINE_READINESS_ENABLED=true
ENGINE_READINESS_WINDOW_HOURS=24
ENGINE_READINESS_MIN_RUNS=100
ENGINE_READINESS_MIN_CANDIDATES=250
ENGINE_READINESS_MIN_SHADOW_INTENTS=50
ENGINE_READINESS_MAX_RUNTIME_ERRORS=0
ENGINE_READINESS_MAX_CRITICAL_ALERTS=0
ENGINE_READINESS_MAX_PAPER_INTENTS_IN_SHADOW=0
ENGINE_READINESS_MAX_LIVE_INTENTS=0
ENGINE_READINESS_MIN_EV_COVERAGE_PCT=95
ENGINE_READINESS_MIN_FEATURE_COVERAGE_PCT=95
ENGINE_READINESS_MIN_REGIME_COVERAGE_PCT=95
ENGINE_READINESS_MAX_RISK_REJECT_RATE_PCT=25
ENGINE_READINESS_MIN_ALLOCATION_RATE_PCT=5
ENGINE_READINESS_MAX_ALLOCATION_RATE_PCT=60
ENGINE_READINESS_MAX_STRATEGY_ALLOCATION_SHARE_PCT=55
ENGINE_READINESS_MAX_AVG_SLIPPAGE_BPS=8
ENGINE_READINESS_MAX_FILL_FAILURE_RATE_PCT=5
ENGINE_READINESS_MIN_SCORE_TO_PASS=85

ENGINE_STRATEGY_THROTTLES_ENABLED=true
ENGINE_STRATEGY_MAX_CANDIDATES_PER_LOOP=15
ENGINE_STRATEGY_MAX_ALLOCATIONS_PER_LOOP=3
ENGINE_STRATEGY_MAX_ALLOCATION_SHARE_PCT=55
ENGINE_STRATEGY_THROTTLE_LOOKBACK_HOURS=24
ENGINE_STRATEGY_THROTTLE_COOLDOWN_LOOPS=3

ENGINE_PNL_ATTRIBUTION_ENABLED=true
ENGINE_PNL_ATTRIBUTION_INTERVAL_SECONDS=300
ENGINE_PNL_ATTRIBUTION_MARK_SOURCE=all_mids
ENGINE_PNL_ATTRIBUTION_CLOSE_ON_EXPIRED_HORIZON=true
ENGINE_PNL_ATTRIBUTION_MAX_POSITION_AGE_HOURS=48
ENGINE_PNL_ATTRIBUTION_MIN_MARK_INTERVAL_SECONDS=60
```

## Persistence Decisions

### No new migration initially

Use existing tables:

- `replay_results`
- `shadow_comparisons`
- `pnl_attribution_records`
- `position_theses`
- `allocation_decisions`

### Replay comparison persistence

Store engine replay comparisons in `replay_results` with:

```text
proposal_id = "engine:{variant_id}"
decision_id = optional readiness/check ID
status = completed|failed|inconclusive
baseline_metrics_json = baseline metrics
candidate_metrics_json = candidate metrics
diffs_json = metric deltas
caveats_json = caveats
metadata_json = structured artifact metadata
```

Add repository helper methods for convenience:

```python
record_engine_replay_comparison(...)
list_engine_replay_comparisons(...)
latest_engine_replay_comparison(...)
```

These wrap existing `ReplayResultRecord` and filter by:

```json
metadata.artifact_type == "engine_shadow_comparison"
```

If JSON filtering is awkward, use `proposal_id` prefix `engine:`.

## Test Plan

### Unit tests

Add tests for:

- readiness score pass/watch/blocked
- each hard block
- scoring penalties
- replay comparison verdict logic
- config hash stability
- throttle candidate filtering
- throttle allocation blocking
- PnL marking for long and short positions
- stop/target/max-age closure
- dashboard data response shape
- Discord digest formatting

### Integration tests

Add route tests:

```text
GET /engine/readiness
GET /engine/replay-comparisons
GET /engine/replay-comparisons/latest
POST /engine/replay-comparisons/run
GET /dashboard
GET /dashboard/data
```

### Regression tests

Confirm:

- shadow-only mode still forbids paper/live leakage
- `ENGINE_LIVE_ENABLED=true` still fails config validation
- no signed exchange adapter introduced
- full test suite passes
- ruff passes

## Acceptance Criteria

Implementation is complete when:

1. `/engine/readiness` returns deterministic scorecard.
2. Discord digest includes readiness status.
3. Shadow replay comparison can be run and persisted without a new migration.
4. Strategy throttles can throttle candidates and allocations deterministically.
5. PnL attribution loop records attribution for simulated open positions.
6. `/dashboard` provides a unified operator shell.
7. Runbook documents exact promotion and rollback commands.
8. No live trading path is introduced.
9. Tests pass.
10. Existing shadow-only deployment remains healthy.

## Rollout Plan

1. Merge code with all new features disabled or conservative.
2. Deploy with current shadow-only config.
3. Confirm `/engine/readiness` returns blocked until sample thresholds are met.
4. Confirm Discord digest posts readiness state.
5. Confirm PnL attribution records begin appearing after simulated positions exist.
6. Run one 24h replay comparison.
7. Keep paper disabled until readiness returns pass and runbook checklist is satisfied.

## Non-Goals

- No Grafana/Prometheus dashboard stack.
- No React/Vite/Next frontend.
- No live order route.
- No private key handling.
- No signed exchange adapter.
- No mutating admin controls to enable paper/live mode from the API.




<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add deterministic engine paper-readiness scoring. _(done)_
- [x] 2. Add engine shadow replay/backtest comparison using existi... _(done)_
- [x] 3. Add strategy-level throttles for candidates and allocations. _(done)_
- [x] 4. Complete simulated PnL attribution and position marking l... _(done)_
- [x] 5. Build the new unified in-app dashboard shell. _(done)_
- [x] 6. Extend Discord digest and alerts with readiness/replay/th... _(done)_
- [x] 7. Write the engine admin promotion and rollback runbook. _(done)_
- [x] 8. Add tests, docs, and rollout checks. _(done)_

<!-- pi-plan-progress:end -->
