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
ENGINE_ALPHA_CATALOG_MODE=wave1a_locked
ENGINE_CROSS_VENUE_DEXES=
ENGINE_VALIDATION_DIGEST_ENABLED=true
ENGINE_VALIDATION_DIGEST_INTERVAL_SECONDS=3600
ENGINE_VALIDATION_ALERT_STALE_LOOP_SECONDS=180
ENGINE_VALIDATION_RISK_REJECT_SPIKE_COUNT=5
ENGINE_VALIDATION_MISSING_DATA_SECONDS=300

NEWSWIRE_GATEWAY_ENABLED=true
AUTONOMY_LEGACY_NEWS_POLL_ENABLED=false
NEWS_SIGNAL_GENERATION_ENABLED=true
NEWS_EVENT_RISK_BLOCKS_ENABLED=true

# Bounded in-memory feature store (2h general / 25h funding series) and
# traded-symbol-only feature emission (escape hatch for research).
ENGINE_FEATURE_STORE_MAX_AGE_SECONDS=7200
ENGINE_FEATURE_STORE_FUNDING_MAX_AGE_SECONDS=90000
ENGINE_FEATURE_STORE_MAX_POINTS_PER_SERIES=4096
ENGINE_FEATURE_FULL_UNIVERSE_ENABLED=false

# DB-backed liquidation features in the trader (cascade/mean-revert strategies
# cannot fire without them). Requires the `liquidations` compose profile
# service running with at least one adapter, e.g.:
#   LIQUIDATIONS_HL_PUBLIC_ENABLED=true
#   docker compose --profile liquidations up -d liquidations
ENGINE_LIQUIDATION_FEATURES_ENABLED=true

# Scheduled evidence refreshers (trader-owned): hourly strategy-regime
# scorecard refresh + daily baseline-equivalence replay comparison.
ENGINE_STRATEGY_REGIME_REFRESH_ENABLED=true
ENGINE_STRATEGY_REGIME_REFRESH_INTERVAL_SECONDS=3600
ENGINE_REPLAY_COMPARISON_SCHEDULE_ENABLED=true
ENGINE_REPLAY_COMPARISON_INTERVAL_SECONDS=86400
ENGINE_EVIDENCE_REFRESH_WINDOW_HOURS=24
ENGINE_REPLAY_MIN_SAMPLE_CANDIDATES=50
```

## Discord validation digest

When `ENGINE_ENABLED=true`, `ENGINE_VALIDATION_DIGEST_ENABLED=true`, `DISCORD_BOT_TOKEN` is set, and `AUTONOMY_ALERT_CHANNEL_ID` is configured, the app posts scheduled engine validation digests to Discord. The digest summarizes shadow candidates, EV buckets, allocation rate, risk rejects, simulated execution, and PnL attribution by strategy.

Alert conditions include stale engine loop, engine runtime errors, paper intents/reports in shadow-only mode, live mode enabled, risk reject spikes, missing/stale feature or regime data, and EV calibration drift once realized attribution samples exist.

## Paper-readiness scorecard

`GET /engine/readiness` returns a deterministic conservative promotion scorecard. Paper readiness is blocked by live flags, paper leakage during shadow-only mode, stale engine loops, runtime errors, insufficient shadow observation/sample size, missing core feature/regime data, critical risk-reject spikes, failed replay comparisons, and unhealthy PnL marking.

The default gate requires 24h shadow observation, at least 100 engine runs, 250 candidates, 50 shadow intents, 95% EV/feature/regime coverage, 100% candidate strategy metadata coverage, 95%+ Council review coverage, 100% RiskGateway coverage, at least 5 paper-eligible non-legacy alpha strategies across 3 paper-eligible families, strategy/family/symbol-strategy concentration below 55%/60%/35%, a latest replay with `passed` or `advisory_pass`, strategy-regime evidence, no hard blocks, and score >=85. Shadow-only research breadth is reported separately and does not satisfy paper-promotion breadth gates.

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

`ENGINE_ALPHA_CATALOG_MODE` controls runtime strategy breadth:

- `wave1a_locked` — default Wave 1A nucleus only; pre-Wave1A, Wave1C, and Wave2 remain specs/comparison-only.
- `wave1c` — Wave 1A plus deterministic Wave 1C strategies.
- `specs_only` — expose planned specs while keeping runtime emissions locked to Wave 1A.
- `shadow_full_catalog` — activates the full shadow catalog while requiring `ENGINE_SHADOW_ENABLED=true`, `ENGINE_PAPER_ENABLED=false`, `ENGINE_EXECUTION_MODES=shadow`, and `ENGINE_LIVE_ENABLED=false`.

Wave 1C deterministic strategies are implemented but gated by catalog mode / `ENGINE_WAVE1C_ENABLED=false` by default until Wave 1B outcome evidence is reliable. The deterministic set is `microstructure_absorption_v1`, `funding_squeeze_v1`, `basis_reversion_v1`, and `news_impulse_v1`; optional `range_rotation_v1` and `volatility_compression_breakout_v1` emit only when the full shadow catalog enables their specs.

Newswire events can be bridged into the engine with `ENGINE_NEWSFEED_ENABLED=true`. Qualifying canonical `NewswireEvent`s become engine `newswire` normalized events and derive `catalyst_pressure` plus `event_risk_pressure` features. Macro news proxies to `ENGINE_NEWS_MACRO_PROXY_SYMBOLS` or, when empty, the core autonomy universe. Regime snapshots only consider news features inside `ENGINE_NEWS_CATALYST_TTL_SECONDS` and expose `derived_labels.news_risk_tier`. The conservative strategy selector can suppress reversion/range strategies during `event_risk` and additionally suppress microstructure/funding-basis strategies during `event_shock`; it never enables disabled strategies, promotes Wave 1C, changes paper/live flags, or creates order authority.

Every candidate builds a `CandidateTradePacket`, receives a deterministic role-based Council review, and must pass RiskGateway plus Council before a paper/shadow execution report can exist. The offline contextual-bandit endpoint is report-only: it writes recommendations with `auto_apply_allowed=false` and never mutates config, risk limits, or orders.

Wave 2 remains paper/live deferred. `ENGINE_WAVE2_ENABLED=true` is rejected until Wave 1 outcome attribution, replay grouping, and readiness gates are reliable. In `shadow_full_catalog`, the Wave 2 research strategies can emit shadow candidates with `activation_scope=shadow_only`, `paper_eligible=false`, and `operator_promotion_required=true`; they still cannot bypass RiskGateway/Council or create paper/live authority. Wave 2 is not “more simple strategies”; it is reserved for DEX-native, cross-venue, regime-aware proprietary strategies. The planned Wave 2 specs cover: 2A lead/lag, liquidity vacuum, stop-cluster, and liquidation divergence; 2B crowded long/short unwind and liquidation-cluster followthrough/exhaustion; 2C perp-basis momentum/reversion, funding-curve dislocation, and carry-risk-off. Wave 2D remains constrained report-only policy recommendation metadata and may not place orders, raise leverage, bypass RiskGateway/Council, or auto-apply production config.

## Shadow replay, throttles, and PnL marking

`POST /engine/replay-comparisons/run` stores immutable engine shadow comparison summaries in the existing `replay_results` storage shape with `proposal_id="engine:{variant_id}"` and `metadata.artifact_type="engine_shadow_comparison"`.

Strategy throttles cap candidates and allocations per strategy and annotate throttled candidates/allocations without creating exchange actions. The diversity controller additionally enforces 45% target strategy share, 55% hard strategy share, 60% family share, and 35% symbol+strategy share once the evidence window has enough samples.

The engine PnL attribution loop marks simulated paper/shadow positions from Hyperliquid `all_mids`, records `pnl_attribution_records`, and closes simulated theses on stop/target/max-age conditions.

See `docs/engine-paper-readiness-runbook.md` for promotion and rollback steps.

## Agentic wave orchestration

The optional Wave Supervisor automates observation, diagnosis, report-only maintenance, bounded blocker escalation, and verification prep without directly mutating config. It may refresh strategy-regime performance, run current-config replay comparisons, emit `agent-core` traces, and render LHP-compatible handoff payloads for Engineering Loop/NOC review. Actual Wave 1C enablement, paper promotion, deploys, and any Wave 2 work still require a draft PR or signed operator change; the supervisor never flips `ENGINE_WAVE1C_ENABLED`, `ENGINE_WAVE2_ENABLED`, paper, or live flags by itself.

Key flags:

- `ORCHESTRATION_WAVE_SUPERVISOR_ENABLED=false`
- `ORCHESTRATION_WAVE_SUPERVISOR_ESCALATION_ENABLED=false`
- `ORCHESTRATION_WAVE_SUPERVISOR_ESCALATION_TRANSPORT=disabled|github_issue`
- `AGENT_CORE_TRACE_ENABLED=false`

Endpoints:

- `GET /orchestration/wave/status`
- `POST /orchestration/wave/run-once`

## Dashboard regime history

The unified dashboard (`GET /dashboard`) has a Regime tab backed by persisted `regime_snapshots`. It charts per-asset `news_risk_tier`, volatility score, stability, and regime-change markers over time. The client tries the Percept/TitanCharts React package documented at `https://percept.one/docs/quickstart.md`; when the package/license is unavailable in the static dashboard runtime, it falls back to the built-in canvas chart without changing the API shape.

The underlying read API is `GET /engine/regime/history?primary_asset=BTC&limit=500`.

## Read-only API

Protected by the existing agent API token outside dev/test/local:

```http
GET /engine/status
GET /engine/events
GET /engine/events/{event_id}
GET /engine/features?asset=BTC
GET /engine/regime/latest
GET /engine/regime/history
GET /engine/candidates
GET /engine/candidates/{candidate_id}
GET /engine/candidate-book/latest
GET /engine/ev-estimates
GET /engine/allocations
GET /engine/strategies
GET /engine/strategy-catalog
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
GET /orchestration/wave/status
POST /orchestration/wave/run-once
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
