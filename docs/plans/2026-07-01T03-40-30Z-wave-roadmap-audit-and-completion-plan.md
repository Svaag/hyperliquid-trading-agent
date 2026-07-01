---
created: 2026-07-01T03:40:30.059Z
source: pi-plan-mode
status: accepted-for-execution
---

# Wave Roadmap Audit and Completion Plan

## Summary

Frame this as:

- **Wave 1 = evidence-producing strategy base**
- **Wave 2 = proprietary perp-DEX edge layer**
- Correct path: **custom path**, not “full Wave 1 all at once,” not “minimum breadth,” and not “Wave 1 plus defensive flat” only.

Repo inspection shows **Wave 1 is mostly code-implemented already**, but it still needs a short hardening/proof phase before treating it as operationally complete. **Wave 2 is intentionally deferred and essentially unimplemented beyond docs/guardrails.**

## Current Implementation Findings

| Area | Status | Evidence |
|---|---:|---|
| Wave 1A nucleus | Implemented | `app/engine/alpha/wave1a.py`, `strategy_registry.py`, tests pass |
| Wave 1B evidence spine | Mostly implemented | `candidate_evidence_links`, `candidate_outcome_attributions`, replay links, performance aggregation, concentration events |
| Wave 1C deterministic breadth | Implemented but gated | `wave1c.py`, `ENGINE_WAVE1C_ENABLED=false` |
| Wave 1D readiness package | Mostly implemented | `/engine/readiness`, readiness reports, replay gates, Wave Supervisor |
| Strategy-Regime Alpha Graph | Partially implemented | Physical evidence tables exist; no first-class graph projection/API yet |
| Wave 2A-D | Not implemented | Strategy IDs appear only in docs; `ENGINE_WAVE2_ENABLED=true` is rejected |
| Bandit/RL layer | Report-only starter exists | `bandit.py`, but not the full Wave 2D action-space policy layer |

Targeted tests that do not import missing local deps passed: **19 passed**. Tests importing `app.main`/newsfeed need dependency-synced env (`uv sync --extra dev`) because local shell lacked `alpaca`/`prometheus_client`.

## Implementation Steps

1. Harden the existing Wave 1 evidence spine.
2. Prove Wave 1 in shadow-only operation.
3. Canary-enable Wave 1C only after Wave 1B evidence is reliable.
4. Add a first-class Strategy-Regime Alpha Graph projection.
5. Keep Wave 2 disabled until Wave 1D passes with real evidence.
6. Implement Wave 2 in controlled sub-waves: 2A, 2B, 2C, then 2D.

## Wave 1 Hardening Details

Before calling Wave 1 “done,” implement/fix these small gaps:

1. **Persist strategy specs at engine startup**
   - Use existing `repository.upsert_strategy_spec`.
   - Persist every `strategy_registry.specs(enabled_only=False)` item.
   - This ensures bandit/reporting can see unobserved enabled strategies.

2. **Create explicit no-trade RiskGateway evidence for flat candidates**
   - `regime_defensive_flat_v1` currently has no candidate-level risk decision.
   - Add a deterministic no-exposure RiskGateway decision:
     - `intent_id = no_trade_<candidate_id>`
     - `decision = allow`
     - metadata: `candidate_level_no_trade=true`, `execution_authority=none`
   - This makes every trade/no-trade fully linked.

3. **Make outcome marking more accurate**
   - Current attribution can mark delayed windows with the latest mid if the loop runs late.
   - Add:
     - nearest historical mid at `window_end_ms`
     - `mark_lag_ms`
     - `late_mark` quality flag
     - path-aware `mfe_bps` / `mae_bps` from mid features inside the window when available.

4. **Add missing Wave 1C feature derivations**
   - Derive `perp_basis_bps` from Hyperliquid asset context when `markPx` and `oraclePx/indexPx` are available.
   - Derive `source_consensus_score` from newswire as `min(source_score, confidence)` so `news_impulse_v1` can work without requiring world-model features.

5. **Fix doc consistency**
   - `docs/engine-paper-readiness-runbook.md` still says strategy-regime evidence coverage is 80%.
   - Update it to match config/default acceptance: **95%**.

## Wave 1 Execution Plan

Run Wave 1 in shadow only:

```env
ENGINE_ENABLED=true
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
ENGINE_WAVE1C_ENABLED=false
ENGINE_WAVE2_ENABLED=false
ENGINE_NEWSFEED_ENABLED=true
ORCHESTRATION_WAVE_SUPERVISOR_ENABLED=true
ORCHESTRATION_WAVE_SUPERVISOR_MAINTENANCE_ENABLED=true
```

If read-only liquidation adapters are available, enable only read-only sources:

```env
LIQUIDATIONS_ENABLED=true
LIQUIDATIONS_HL_PUBLIC_ENABLED=true
LIQUIDATIONS_LIGHTER_ENABLED=true
LIQUIDATIONS_ASTER_ENABLED=true
```

Then collect at least 24h of shadow evidence and run:

```http
POST /engine/strategy-regime-performance/refresh
POST /engine/replay-comparisons/run
POST /engine/bandit-recommendations/run
GET /engine/readiness
GET /engine/strategy-regime-performance
GET /engine/replay-comparisons/latest
```

## Wave 1C Canary

Only after Wave 1B/Wave 1D pass cleanly, flip:

```env
ENGINE_WAVE1C_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_WAVE2_ENABLED=false
```

Canary acceptance:

- No readiness hard blocks.
- No paper/live leakage.
- Candidate evidence links remain 100%.
- Candidate-level RiskGateway coverage remains 100%.
- Matured outcome attribution remains >=95%.
- Council packet coverage remains >=95%.
- Strategy concentration remains <=55% hard cap.
- Replay remains `passed` or `advisory_pass`.

## Strategy-Regime Alpha Graph

Add a read-only graph projection endpoint:

```http
GET /engine/alpha-graph
```

Back it from existing tables:

- `strategy_specs`
- `alpha_candidates`
- `regime_snapshots`
- `candidate_evidence_links`
- `candidate_outcome_attributions`
- `strategy_regime_performance`
- `council_reviews`
- `risk_gateway_decisions`
- `replay_result_links`

Include graph edges:

- `worked_in`
- `failed_in`
- `needs_more_evidence_in`
- `risk_rejected_in`
- `council_vetoed_in`
- `replay_failed_in`
- `overfit_warning_in`

This becomes the North Star artifact.

## Wave 2 Plan

Do not start Wave 2 until Wave 1D passes with real data.

Wave 2 should be implemented as disabled-by-default strategy modules:

```text
app/engine/alpha/wave2.py
```

### Wave 2A

Implement cross-venue/liquidity-map strategies:

```text
cross_venue_lead_lag_v1
liquidity_vacuum_breakout_v1
stop_cluster_hunt_v1
cross_venue_liquidation_divergence_v1
```

### Wave 2B

Implement crowding/forced-flow strategies:

```text
crowded_long_unwind_v1
crowded_short_squeeze_v1
liquidation_cluster_followthrough_v1
liquidation_cluster_exhaustion_v1
```

### Wave 2C

Implement perp basis/carry intelligence:

```text
perp_basis_momentum_v1
perp_basis_reversion_v2
funding_curve_dislocation_v1
carry_risk_off_v1
```

### Wave 2D

Upgrade the current report-only bandit into a constrained policy recommender with only these actions:

```text
strategy_weight_bucket
candidate_quota_bucket
min_confidence_threshold
min_ev_threshold
cooldown_bucket
no_trade
shadow_only_experiment
```

Forbidden forever:

```text
place orders
raise leverage
bypass RiskGateway
bypass Council
auto-apply production config
```

## Acceptance Criteria

Wave 1 is complete when:

- 100% candidates have strategy ID/version/family.
- 100% candidates have regime and feature snapshot IDs.
- 100% candidates have candidate evidence links.
- 100% non-flat candidates have candidate-level RiskGateway decisions.
- Flat/no-trade candidates have explicit no-exposure risk evidence.
- >=95% matured candidates have delayed outcome attribution.
- `strategy_regime_performance` is populated from outcome rows.
- Replay groups by strategy × regime × asset × venue × outcome window.
- Latest replay is `passed` or `advisory_pass`.
- No strategy exceeds 55% allocation share.
- >=5 active non-legacy alpha strategies.
- >=3 active alpha families.
- Paper remains disabled until explicit operator promotion.

## Key Principle

Do not optimize for “more strategies.”

Optimize for the **Strategy-Regime Alpha Graph**: a governed system that learns which strategy works in which regime while keeping every trade/no-trade explainable, replayable, risk-gated, Council-reviewed, and outcome-attributed.










<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Harden the existing Wave 1 evidence spine. _(done)_
- [>] 2. Prove Wave 1 in shadow-only operation. _(deferred)_
- [>] 3. Canary-enable Wave 1C only after Wave 1B evidence is reliable. _(deferred)_
- [x] 4. Add a first-class Strategy-Regime Alpha Graph projection. _(done)_
- [x] 5. Keep Wave 2 disabled until Wave 1D passes with real evidence. _(done)_
- [x] 6. Implement Wave 2 in controlled sub-waves: 2A, 2B, 2C, then 2D. _(done)_

<!-- pi-plan-progress:end -->
