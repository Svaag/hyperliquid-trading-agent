---
created: 2026-07-01T05:07:20.816Z
source: pi-plan-mode
status: accepted-for-execution
---

# Shadow-Only Full Alpha Model Catalog Expansion

## Summary
Expand the engine from the current tight Wave1A runtime set into a **shadow-only full alpha strategy catalog** using the dozens of strategy/model names already suggested in prior plans. The implementation will add many more candidate-generating alpha models while keeping **RiskGateway, Council review, paper/live disablement, and promotion gates unchanged**.

This plan interprets “models” as **alpha/strategy models**, not LLM provider models or EV scorer ML artifacts.

## Current State
- Runtime is effectively producing candidates from only:
  - `microstructure_ofi_v2`
  - `regime_defensive_flat_v1` no-trade candidates
- Existing source already contains or references:
  - Wave1A strategies
  - Wave1C deterministic strategies
  - pre-Wave1A comparison strategies
  - Wave2 deferred strategy specs
- Current registry behavior is too restrictive:
  - Wave1C is only active behind `ENGINE_WAVE1C_ENABLED`
  - pre-Wave1A strategies are disabled comparison specs
  - Wave2 specs are disabled/inert
- Current EV fallback is also too tight because it mostly ignores `candidate.expected_edge_bps`, causing strong-looking candidates to still score slightly negative and be RiskGateway-rejected.

## Implementation Steps
1. Add a shadow-only alpha catalog mode to configuration.
2. Refactor the strategy registry to support full shadow catalog activation.
3. Implement/activate the full strategy catalog: Wave1A, pre-Wave1A, Wave1C, optional Wave1C, and Wave2 shadow research strategies.
4. Add missing deterministic feature rollups needed by the expanded models.
5. Tune deterministic EV scoring to include a capped strategy-edge prior without bypassing RiskGateway.
6. Update readiness/reporting so shadow research breadth is visible but does not falsely imply paper eligibility.
7. Add dashboard/API visibility for the expanded model catalog.
8. Add tests covering config safety, registry breadth, candidate generation, EV scoring, and readiness accounting.
9. Update docs/runbooks with the new shadow-only catalog mode and rollout steps.

## Strategy Catalog to Support
The full shadow catalog should expose these 30 strategy/model choices:

### Pre-Wave1A / baseline crypto
- `directional_momentum_v2`
- `support_resistance_reversion_v2`
- `microstructure_ofi_v1`
- `news_event_alpha_v1`
- `equity_options_flow_v1` — spec-only until TradFi features are active

### Wave1A
- `microstructure_ofi_v2`
- `liquidation_cascade_v1`
- `liquidation_mean_revert_v1`
- `funding_carry_v1`
- `oi_breakout_v1`
- `legacy_signal_adapter_v1` — non-breadth bridge
- `regime_defensive_flat_v1` — non-breadth no-trade policy

### Wave1C
- `microstructure_absorption_v1`
- `funding_squeeze_v1`
- `basis_reversion_v1`
- `news_impulse_v1`
- `range_rotation_v1`
- `volatility_compression_breakout_v1`

### Wave2 shadow research
- `cross_venue_lead_lag_v1`
- `liquidity_vacuum_breakout_v1`
- `stop_cluster_hunt_v1`
- `cross_venue_liquidation_divergence_v1`
- `crowded_long_unwind_v1`
- `crowded_short_squeeze_v1`
- `liquidation_cluster_followthrough_v1`
- `liquidation_cluster_exhaustion_v1`
- `perp_basis_momentum_v1`
- `perp_basis_reversion_v2`
- `funding_curve_dislocation_v1`
- `carry_risk_off_v1`

## Config Contract
Add:

```env
ENGINE_ALPHA_CATALOG_MODE=wave1a_locked
```

Allowed values:

```text
wave1a_locked        # current behavior
wave1c               # Wave1A + deterministic Wave1C
shadow_full_catalog  # full catalog emits shadow candidates only
specs_only           # expose all specs but only Wave1A emits
```

Safety rule:

```text
ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog
```

must require:

```text
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_LIVE_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
```

If those are not true, settings validation must fail fast.

## Key Implementation Details

### Registry
Update `create_default_strategy_registry()` to accept `catalog_mode`.

Behavior:

- `wave1a_locked`: current behavior.
- `wave1c`: Wave1A + active Wave1C deterministic models.
- `shadow_full_catalog`:
  - active instances for Wave1A, pre-Wave1A crypto, Wave1C, optional Wave1C, and Wave2 shadow research.
  - all paper-ineligible research strategies get metadata:
    ```json
    {
      "activation_scope": "shadow_only",
      "paper_eligible": false,
      "operator_promotion_required": true
    }
    ```
- `specs_only`: persist all specs but do not activate non-Wave1A instances.

### Feature rollups
Add deterministic rollups in `feature_store.py`:

- `range_position`
- `depth_thinning_5m_pct`
- `basis_delta_15m_bps`
- `basis_zscore`
- `spread_velocity_5m_bps`
- `funding_change_15m`
- `volume_liquidity_score`

Add optional cross-venue features only when configured:

```env
ENGINE_CROSS_VENUE_DEXES=
```

Derived features:

- `cross_venue_mid_delta_bps`
- `cross_venue_volume_imbalance`
- `cross_venue_liq_imbalance`

Default empty means no external-rate-load increase.

### EV scorer
Update deterministic fallback scorer to include a capped strategy-edge prior:

```python
edge_prior_bps = min(max(candidate.expected_edge_bps, 0.0), 12.0)
net_ev_bps = existing_net_ev_bps + edge_prior_bps
```

Record scorer metadata:

```json
{
  "strategy_edge_prior_bps": edge_prior_bps,
  "strategy_edge_prior_cap_bps": 12.0,
  "source": "candidate.expected_edge_bps"
}
```

RiskGateway remains unchanged and still rejects candidates below `ENGINE_MIN_NET_EV_BPS`.

### Readiness reporting
Do not let shadow-only research models fake paper readiness.

Add separate metrics:

- `active_shadow_strategy_count`
- `active_shadow_family_count`
- `paper_eligible_active_strategy_count`
- `paper_eligible_active_family_count`
- `shadow_research_strategy_count`

Paper readiness should use `paper_eligible_*` for final promotion gates.

Dashboard can still show shadow breadth so operators see the expanded model catalog.

### API/dashboard
Add or extend:

```text
GET /engine/strategies
GET /engine/strategy-catalog
GET /dashboard/data
```

Expose grouped catalog:

```json
{
  "mode": "shadow_full_catalog",
  "total_specs": 30,
  "runtime_enabled": 27,
  "paper_eligible": 0,
  "shadow_only": 27,
  "spec_only": 3,
  "families": [...]
}
```

## Tests

Add/update tests:

1. Config safety
   - `shadow_full_catalog` passes only with shadow-only settings.
   - fails if paper/live is enabled.

2. Registry
   - `wave1a_locked` preserves current enabled count.
   - `shadow_full_catalog` registers all 30 specs.
   - expected runtime-enabled strategies are active.
   - non-breadth strategies remain non-breadth.

3. Strategy generation
   - each active strategy can safely return `[]`.
   - each feature-compatible strategy emits at least one candidate under a synthetic matching regime.
   - every emitted candidate has strategy ID/version/family, feature snapshot ID, regime snapshot ID, and source integrity metadata.

4. EV scoring
   - expected-edge prior is capped.
   - negative/no-edge candidates remain rejected.
   - positive-edge candidates can clear scorer threshold but still pass through RiskGateway.

5. Readiness
   - shadow breadth and paper-eligible breadth are reported separately.
   - paper readiness does not pass just because shadow-only models are active.

6. Routes/dashboard
   - strategy catalog endpoint returns grouped counts.
   - existing `/engine/strategies` behavior remains backward compatible.

Suggested test command:

```bash
uv run pytest -q \
  tests/test_engine_strategy_registry.py \
  tests/test_engine_service.py \
  tests/test_engine_wave2.py \
  tests/test_engine_regime_features.py \
  tests/test_engine_readiness.py \
  tests/test_engine_routes.py
```

## Rollout Plan
1. Deploy code with default `ENGINE_ALPHA_CATALOG_MODE=wave1a_locked`.
2. Confirm tests and dashboard behavior.
3. Set runtime env:
   ```env
   ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog
   ENGINE_SHADOW_ENABLED=true
   ENGINE_PAPER_ENABLED=false
   ENGINE_LIVE_ENABLED=false
   ENGINE_EXECUTION_MODES=shadow
   ```
4. Restart engine.
5. Observe for at least one clean 24h shadow window.
6. Validate:
   - >=5 active shadow strategies
   - >=3 active shadow families
   - no live/paper intents
   - RiskGateway/Council coverage remains intact
   - replay is `passed` or `advisory_pass`

## Acceptance Criteria
- Engine exposes ~30 strategy/model choices.
- Full catalog can run only in shadow-only mode.
- At least 5 non-legacy alpha strategies emit candidates in normal market conditions.
- At least 3 alpha families emit candidates over a 24h window.
- RiskGateway and Council are still mandatory.
- Paper/live remain disabled unless separately promoted.
- Readiness distinguishes shadow breadth from paper-eligible breadth.












<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add a shadow-only alpha catalog mode to configuration. _(done)_
- [x] 2. Refactor the strategy registry to support full shadow catalog activation. _(done)_
- [x] 3. Implement/activate the full strategy catalog: Wave1A, pre-Wave1A, Wave1C, optional Wave1C, and Wave2 shadow research strategies. _(done)_
- [x] 4. Add missing deterministic feature rollups needed by the expanded models. _(done)_
- [x] 5. Tune deterministic EV scoring to include a capped strategy-edge prior without bypassing RiskGateway. _(done)_
- [x] 6. Update readiness/reporting so shadow research breadth is visible but does not falsely imply paper eligibility. _(done)_
- [x] 7. Add dashboard/API visibility for the expanded model catalog. _(done)_
- [x] 8. Add tests covering config safety, registry breadth, candidate generation, EV scoring, and readiness accounting. _(done)_
- [x] 9. Update docs/runbooks with the new shadow-only catalog mode and rollout steps. _(done)_

<!-- pi-plan-progress:end -->
