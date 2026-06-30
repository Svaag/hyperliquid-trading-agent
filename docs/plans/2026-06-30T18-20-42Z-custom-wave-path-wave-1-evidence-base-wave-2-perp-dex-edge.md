---
created: 2026-06-30T18:20:42.879Z
source: pi-plan-mode
status: accepted-for-execution
---

# Custom Wave Path: Wave 1 Evidence Base, Wave 2 Perp-DEX Edge

## Decision

Choose the **custom path**, not any of the three flat options.

Lock:

```text
Wave 1A = strategy-regime candidate nucleus
```

Then finish Wave 1 in controlled sub-waves:

```text
Wave 1B = evidence spine
Wave 1C = deterministic alpha breadth
Wave 1D = readiness / gate-clearing package
```

Frame the roadmap as:

```text
Wave 1 = evidence-producing strategy base.
Wave 2 = proprietary perp-DEX edge layer.
```

Do **not** treat “more strategies” as the goal. The moat is the **Strategy-Regime Alpha Graph**.

## Grounded Current State

Repo inspection shows Wave 1A is already effectively present:

- `hyperliquid_trading_agent/app/engine/alpha/wave1a.py`
- `hyperliquid_trading_agent/app/engine/strategy_registry.py`
- `docs/institutional-engine.md`
- `docs/plans/2026-06-30T14-59-50Z-institutional-engine-strategy-portfolio-upgrade-plan.md`

Existing Wave 1A nucleus includes:

```text
microstructure_ofi_v2
liquidation_cascade_v1
liquidation_mean_revert_v1
funding_carry_v1
oi_breakout_v1
legacy_signal_adapter_v1
regime_defensive_flat_v1
```

Existing infrastructure already includes partial evidence components:

- `regime_snapshots`
- `strategy_regime_performance`
- `candidate_trade_packets`
- `council_reviews`
- `allocation_diversity_events`
- replay comparison storage via `replay_results`

Main gap: the engine still needs a stronger **candidate-level outcome attribution spine** with fixed delayed windows and complete ID linkage.

## Implementation Steps

1. Lock Wave 1A as the candidate nucleus and do not expand strategy breadth yet.
2. Implement Wave 1B evidence spine and delayed outcome attribution.
3. Upgrade strategy-regime performance and replay grouping around strategy × regime × asset × venue × horizon.
4. Implement Wave 1C deterministic strategy breadth after Wave 1B is measurable.
5. Implement Wave 1D readiness reports and stricter gate-clearing checks.
6. Keep Wave 2 deferred until Wave 1 outcome attribution and replay gates are reliable.
7. Document Wave 2 as the proprietary perp-DEX edge roadmap, not as immediate strategy sprawl.

## Wave 1B — Evidence Spine

Build Wave 1B immediately after Wave 1A.

Use existing tables where possible, but add missing first-class evidence contracts.

### New or upgraded contracts

Add a migration after `0019_engine_strategy_regime_council_learning`, e.g.:

```text
0020_engine_candidate_outcome_evidence_spine.py
```

Add:

```text
candidate_evidence_links
candidate_outcome_attributions
replay_result_links
portfolio_concentration_events
```

Use existing `regime_snapshots` as the physical backing for `market_regime_snapshots`; do not duplicate the table.

Use existing `candidate_trade_packets` + `council_reviews` as the physical backing for `council_review_packets`; expose joined report/API shape if needed.

### Required candidate evidence IDs

Every candidate must be linked to:

```text
candidate_id
strategy_id
strategy_version
strategy_family
regime_snapshot_id
feature_snapshot_id
risk_decision_id
council_review_id
replay_context_id
outcome_window_ids
```

For all non-flat candidates, add candidate-level RiskGateway precheck coverage.

For executable allocations, keep the existing order-level RiskGateway check before execution.

### Outcome windows

Pre-create one attribution row per candidate per window:

```text
5m
15m
1h
4h
24h
```

Each row should track:

```text
candidate_id
strategy_id/version/family
asset
venue
side
candidate_horizon
regime_snapshot_id
feature_snapshot_id
risk_decision_id
council_review_id
replay_context_id
outcome_window
window_start_ms
window_end_ms
entry_px
mark_px
gross_return_bps
fees_bps
slippage_bps
funding_bps
net_return_bps
realized_r
mfe_bps
mae_bps
risk_decision
council_decision
allocation_status
terminal_state
quality_flags
```

The attribution service must answer:

```text
When strategy X fired in regime Y on asset Z and venue V,
what happened after fees, slippage, risk rejects, Council vetoes, and drawdown?
```

### Strategy-regime performance upgrade

Upgrade `strategy_regime_performance` to aggregate from `candidate_outcome_attributions`, grouped by:

```text
strategy_id
strategy_version
strategy_family
regime_label / regime_snapshot_id
asset
venue
outcome_window
```

Include metrics for:

```text
candidate_count
allocation_count
risk_reject_count
council_veto_count
concentration_event_count
win_rate_pct
avg_net_return_bps
avg_realized_r
avg_drawdown_bps
avg_fees_bps
avg_slippage_bps
realized_pnl_usd
score
```

## Wave 1C — Deterministic Alpha Breadth

Only after Wave 1B is producing measurable outcomes, add deterministic strategies in `hyperliquid_trading_agent/app/engine/alpha/wave1c.py`.

Active Wave 1C strategies:

```text
microstructure_absorption_v1
funding_squeeze_v1
basis_reversion_v1
news_impulse_v1
```

Register but keep disabled by default unless tests prove deterministic replay quality:

```text
range_rotation_v1
volatility_compression_breakout_v1
```

Rules:

- Each strategy must expose `StrategySpec`.
- Each must declare valid regimes and required features.
- Each must be replayable from stored features/events.
- `legacy_signal_adapter_v1` remains comparison infrastructure, not alpha breadth.
- Breadth counts only if the strategy is independent, regime-declared, replayable, and outcome-attributed.

## Wave 1D — Readiness Package

Build reports for:

```text
readiness by strategy family
readiness by market regime
latest clean replay comparison
strategy concentration
Council veto/rejects
RiskGateway coverage
shadow-mode outcomes
```

Update readiness gate targets:

```text
latest replay passes or advisory_pass
no strategy >55% hard allocation share
target warning above 45%
>=5 active non-legacy alpha strategies
>=3 active alpha families
>=95% regime coverage
>=95% Council packet coverage
100% RiskGateway coverage
>=95% matured candidate outcome attribution
paper mode remains disabled until explicit promotion
```

Also raise strategy-regime evidence coverage from the current `80%` default to `95%`.

## Wave 2 — Proprietary Perp-DEX Edge Layer

Wave 2 starts only after Wave 1D passes.

### Wave 2A

```text
cross_venue_lead_lag_v1
liquidity_vacuum_breakout_v1
stop_cluster_hunt_v1
cross_venue_liquidation_divergence_v1
```

### Wave 2B

```text
crowded_long_unwind_v1
crowded_short_squeeze_v1
liquidation_cluster_followthrough_v1
liquidation_cluster_exhaustion_v1
```

### Wave 2C

```text
perp_basis_momentum_v1
perp_basis_reversion_v2
funding_curve_dislocation_v1
carry_risk_off_v1
```

### Wave 2D

Add constrained contextual bandit / RL policy recommendations only.

Allowed action space:

```text
strategy_weight_bucket
candidate_quota_bucket
min_confidence_threshold
min_ev_threshold
cooldown_bucket
no_trade
shadow_only_experiment
```

Forbidden:

```text
placing orders
raising leverage
bypassing RiskGateway
bypassing Council
auto-applying production config
```

## Acceptance Criteria

Wave 1 is complete when:

```text
100% candidates have regime_snapshot_id
100% candidates have strategy_id/version/family
100% candidates have candidate evidence links
100% candidates have Council packet coverage or explicit no-trade packet coverage
100% candidates have RiskGateway candidate-level decision
100% executable intents have order-level RiskGateway decision
>=95% matured candidates receive delayed outcome attribution
strategy_regime_performance is populated from attribution rows
replay groups results by strategy_id x regime_id x asset x venue x outcome_window
latest replay passes/advisory_pass
paper mode remains disabled until explicit promotion
```

## Test Plan

Add/update tests for:

```text
tests/test_engine_wave1b_evidence_spine.py
tests/test_engine_candidate_outcome_attribution.py
tests/test_engine_strategy_performance.py
tests/test_engine_replay_compare.py
tests/test_engine_readiness.py
tests/test_engine_wave1c_strategies.py
tests/test_engine_routes.py
```

Run:

```bash
uv run pytest -q
```

## North Star

The North Star is the **Strategy-Regime Alpha Graph**:

```text
For this asset,
on this venue,
in this market regime,
at this horizon,
which strategy has historically produced the best risk-adjusted outcome,
with enough evidence for the Agentic Council to allow it?
```

Strategies are replaceable. The evidence graph is the compounding asset.










<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Lock Wave 1A as the candidate nucleus and do not expand strategy breadth yet. _(done)_
- [x] 2. Implement Wave 1B evidence spine and delayed outcome attribution. _(done)_
- [x] 3. Upgrade strategy-regime performance and replay grouping around strategy × regime × asset × venue × horizon. _(done)_
- [x] 4. Implement Wave 1C deterministic strategy breadth after Wave 1B is measurable. _(done)_
- [x] 5. Implement Wave 1D readiness reports and stricter gate-clearing checks. _(done)_
- [x] 6. Keep Wave 2 deferred until Wave 1 outcome attribution and replay gates are reliable. _(done)_
- [x] 7. Document Wave 2 as the proprietary perp-DEX edge roadmap, not as immediate strategy sprawl. _(done)_

<!-- pi-plan-progress:end -->
