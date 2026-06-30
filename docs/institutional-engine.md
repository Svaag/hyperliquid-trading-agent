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

Wave 1A registers `microstructure_ofi_v2`, `liquidation_cascade_v1`, `liquidation_mean_revert_v1`, `funding_carry_v1`, and `oi_breakout_v1` as alpha breadth. `legacy_signal_adapter_v1` and `regime_defensive_flat_v1` are registered but do not count as independent alpha breadth.

Every allocated non-flat candidate builds a `CandidateTradePacket`, receives a deterministic role-based Council review, and must pass RiskGateway plus Council before a paper/shadow execution report can exist. The offline contextual-bandit endpoint is report-only: it writes recommendations with `auto_apply_allowed=false` and never mutates config, risk limits, or orders.

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
GET /engine/council-reviews
GET /engine/diversity-events
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

High-frequency event/feature data is intended for bounded retention and rollups; candidates, strategy specs, strategy-regime scorecards, Council reviews/votes, diversity events, bandit report-only recommendations, risk checks, evidence packs, execution reports, position theses, attribution, and governance records are durable audit artifacts.
