# Institutional Engine Upgrade

This document tracks the new paper/shadow institutional trading engine path.

## Safety posture

The engine is **shadow-first and paper/shadow only**:

- defaults and examples keep paper disabled until readiness passes
- no private-key handling
- no signed exchange adapter
- no route capable of submitting live orders
- `ENGINE_LIVE_ENABLED=true` is rejected by settings validation
- execution reports are produced by `PaperAdapter` or `ShadowAdapter` only

## Canonical loop

```text
NormalizedEvent
  -> FeatureValue / FeatureSnapshot
  -> RegimeVector
  -> AlphaCandidate
  -> EVEstimate
  -> AllocationDecision
  -> RiskGateway.check_order_intent
  -> EvidencePack / DebateDecision when review value is high
  -> OrderIntent
  -> ExecutionReport
  -> PositionThesis
  -> Reconciliation / PnL attribution / replay
```

## New package

```text
hyperliquid_trading_agent/app/engine/
```

Key modules:

- `schemas.py` — canonical contracts
- `event_ledger.py` — append-only normalized event facade
- `feature_store.py` — point-in-time feature values/snapshots
- `regime.py` — `RegimeVector` computation
- `alpha/` — initial alpha families
- `scorer.py` — deterministic EV fallback and offline training scaffold
- `portfolio_allocator.py` — portfolio-aware risk allocation
- `debate_adjudicator.py` — EvidencePack + DebateDecision authority boundary
- `execution.py` — PaperAdapter / ShadowAdapter
- `position_manager.py` — PositionThesis state lifecycle
- `routes.py` — read-only `/engine/*` endpoints

## New configuration

```env
ENGINE_ENABLED=false
ENGINE_MODE=paper_shadow
ENGINE_EXECUTION_MODES=shadow
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_LIVE_ENABLED=false
ENGINE_VALIDATION_DIGEST_ENABLED=true
ENGINE_VALIDATION_DIGEST_INTERVAL_SECONDS=3600
ENGINE_VALIDATION_ALERT_STALE_LOOP_SECONDS=180
ENGINE_VALIDATION_RISK_REJECT_SPIKE_COUNT=5
ENGINE_VALIDATION_MISSING_DATA_SECONDS=300

NEWSWIRE_GATEWAY_ENABLED=true
AUTONOMY_LEGACY_NEWS_POLL_ENABLED=false
NEWS_SIGNAL_GENERATION_ENABLED=true
NEWS_EVENT_RISK_BLOCKS_ENABLED=true
```

## Discord validation digest

When `ENGINE_ENABLED=true`, `ENGINE_VALIDATION_DIGEST_ENABLED=true`, `DISCORD_BOT_TOKEN` is set, and `AUTONOMY_ALERT_CHANNEL_ID` is configured, the app posts scheduled engine validation digests to Discord. The digest summarizes shadow candidates, EV buckets, allocation rate, risk rejects, simulated execution, and PnL attribution by strategy.

Alert conditions include stale engine loop, engine runtime errors, paper intents/reports in shadow-only mode, live mode enabled, risk reject spikes, missing/stale feature or regime data, and EV calibration drift once realized attribution samples exist.

## Paper-readiness scorecard

`GET /engine/readiness` returns a deterministic conservative promotion scorecard. Paper readiness is blocked by live flags, paper leakage during shadow-only mode, stale engine loops, runtime errors, insufficient shadow observation/sample size, missing core feature/regime data, critical risk-reject spikes, failed replay comparisons, and unhealthy PnL marking.

The default gate requires 24h shadow observation, at least 100 engine runs, 250 candidates, 50 shadow intents, 95% EV/feature/regime coverage, 100% candidate strategy metadata coverage, 95%+ Council review coverage, 100% RiskGateway coverage, at least 5 non-legacy alpha strategies across 3 families, strategy/family/symbol-strategy concentration below 55%/60%/35%, a latest replay with `passed` or `advisory_pass`, strategy-regime evidence, no hard blocks, and score >=85.

## Strategy portfolio, Council, replay, and bandit reports

Wave 1 is the **evidence-producing strategy base**. Wave 2 is the deferred **proprietary perp-DEX edge layer**.

Wave 1A locks the strategy-regime candidate nucleus:

- `microstructure_ofi_v2`
- `liquidation_cascade_v1`
- `liquidation_mean_revert_v1`
- `funding_carry_v1`
- `oi_breakout_v1`
- `legacy_signal_adapter_v1`
- `regime_defensive_flat_v1`

Only the five non-legacy/non-defensive Wave 1A strategies count as active alpha breadth by default. Pre-Wave1A strategies remain registered only as disabled comparison specs. `legacy_signal_adapter_v1` and `regime_defensive_flat_v1` do not count as independent alpha breadth.

Wave 1B adds the evidence spine: every candidate receives candidate evidence links, fixed delayed outcome windows (`5m`, `15m`, `1h`, `4h`, `24h`), candidate-level RiskGateway coverage for non-flat candidates, Council packet/no-trade coverage, replay context links, and strategy-regime performance rows sourced from outcome attribution.

Wave 1C deterministic strategies are implemented but gated behind `ENGINE_WAVE1C_ENABLED=false` by default until Wave 1B outcome evidence is reliable. The gated active set is `microstructure_absorption_v1`, `funding_squeeze_v1`, `basis_reversion_v1`, and `news_impulse_v1`; optional `range_rotation_v1` and `volatility_compression_breakout_v1` remain disabled pending replay depth.

Every candidate builds a `CandidateTradePacket`, receives a deterministic role-based Council review, and must pass RiskGateway plus Council before a paper/shadow execution report can exist. The offline contextual-bandit endpoint is report-only: it writes recommendations with `auto_apply_allowed=false` and never mutates config, risk limits, or orders.

Wave 2 is explicitly deferred. `ENGINE_WAVE2_ENABLED=true` is rejected until Wave 1 outcome attribution, replay grouping, and readiness gates are reliable. Wave 2 is not “more simple strategies”; it is reserved for DEX-native, cross-venue, regime-aware proprietary strategies: lead/lag, liquidity vacuum, stop-cluster, liquidation divergence, crowded long/short unwind, perp-basis momentum/reversion, carry-risk intelligence, and constrained policy recommendations.

## Shadow replay, throttles, and PnL marking

`POST /engine/replay-comparisons/run` stores immutable engine shadow comparison summaries in the existing `replay_results` storage shape with `proposal_id="engine:{variant_id}"` and `metadata.artifact_type="engine_shadow_comparison"`.

Strategy throttles cap candidates and allocations per strategy and annotate throttled candidates/allocations without creating exchange actions. The diversity controller additionally enforces 45% target strategy share, 55% hard strategy share, 60% family share, and 35% symbol+strategy share once the evidence window has enough samples.

The engine PnL attribution loop marks simulated paper/shadow positions from Hyperliquid `all_mids`, records `pnl_attribution_records`, and closes simulated theses on stop/target/max-age conditions.

See `docs/engine-paper-readiness-runbook.md` for promotion and rollback steps.

## Read-only API

Protected by the existing agent API token outside dev/test/local:

```http
GET /engine/status
GET /engine/events
GET /engine/events/{event_id}
GET /engine/features?asset=BTC
GET /engine/regime/latest
GET /engine/candidates
GET /engine/candidates/{candidate_id}
GET /engine/candidate-book/latest
GET /engine/ev-estimates
GET /engine/allocations
GET /engine/strategies
GET /engine/strategies/{strategy_id}
GET /engine/strategy-regime-performance
GET /engine/strategy-regime-performance/{strategy_id}
POST /engine/strategy-regime-performance/refresh
GET /engine/candidate-trade-packets
GET /engine/candidate-evidence-links
GET /engine/candidate-outcome-attributions
GET /engine/council-reviews
GET /engine/diversity-events
GET /engine/portfolio-concentration-events
GET /engine/replay-result-links
GET /engine/bandit-recommendations
POST /engine/bandit-recommendations/run
GET /engine/evidence-packs/{evidence_pack_id}
GET /engine/debate-decisions
GET /engine/order-intents
GET /engine/execution-reports
GET /engine/positions
GET /engine/reconciliation
GET /engine/model-versions
GET /engine/risk-rejects
GET /engine/pnl-attribution
GET /engine/validation-report
GET /engine/readiness
GET /engine/replay-comparisons
GET /engine/replay-comparisons/latest
POST /engine/replay-comparisons/run
GET /engine/dashboard
GET /dashboard
GET /dashboard/data
GET /engine/retention
```

## Persistence

Alembic revisions:

- `0011_engine_event_feature_store`
- `0012_candidate_ev_allocation_debate`
- `0013_execution_position_reconciliation`
- `0014_model_registry_retention`
- `0019_engine_strategy_regime_council_learning`
- `0020_engine_candidate_outcome_evidence_spine`

High-frequency event/feature data is intended for bounded retention and rollups; candidates, candidate evidence links, delayed outcome attributions, replay result links, strategy specs, strategy-regime scorecards, Council reviews/votes, diversity/concentration events, bandit report-only recommendations, risk checks, evidence packs, execution reports, position theses, attribution, and governance records are durable audit artifacts.
