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
  -> exact StrategyVersionPolicy
  -> size-specific ExecutionCostQuote
  -> EVEstimate
  -> research / paper-eligible AllocationDecision
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
- `scorer.py` — strict native-horizon hierarchical empirical scorer and zero-edge fallback
- `promotion.py` — immutable exact-version promotion policy and fail-closed defaults
- `time_block_stats.py` — purged non-overlapping time-block confidence intervals
- `strategy_research.py` — predeclared OFI/liquidity-vacuum research and absorption redesign gates
- `portfolio_allocator.py` — portfolio-aware risk allocation
- `debate_adjudicator.py` — EvidencePack + DebateDecision authority boundary
- `execution.py` — venue/fee-tier/order-book depth simulation plus PaperAdapter / ShadowAdapter
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
ENGINE_APPROVED_SCORER_MODEL_ID=
ENGINE_SCORER_FALLBACK_MODE=no_edge
ENGINE_EXECUTION_FEE_ACCOUNT_ADDRESS=
ENGINE_EXECUTION_BOOK_MAX_AGE_MS=15000
ENGINE_EXECUTION_FEE_CACHE_TTL_MS=300000
ENGINE_PROMOTION_MIN_EFFECTIVE_BLOCKS=30
ENGINE_PROMOTION_BOOTSTRAP_ITERATIONS=10000
ENGINE_READINESS_MAX_STRICT_OUTCOME_ROWS=100000
ENGINE_ALPHA_CATALOG_MODE=integrated
ENGINE_CROSS_VENUE_DEXES=lighter,xyz,alpaca:paper
ENGINE_WAVE1C_ENABLED=true
ENGINE_WAVE2_ENABLED=true
AUTONOMY_CORE_UNIVERSE=BTC,ETH,HYPE,SOL,ZEC,LIT,AAVE,XMR,AERO
AUTONOMY_HIP3_DEXS=xyz
ENGINE_VALIDATION_DIGEST_ENABLED=true
ENGINE_VALIDATION_DIGEST_INTERVAL_SECONDS=3600
ENGINE_VALIDATION_ALERT_STALE_LOOP_SECONDS=180
ENGINE_VALIDATION_RISK_REJECT_SPIKE_COUNT=5
ENGINE_VALIDATION_MISSING_DATA_SECONDS=300

MARKET_UNIVERSE_ENABLED=true
LIGHTER_ENABLED=true
LIGHTER_READ_ONLY=true
ALPACA_PAPER_TRADING_ENABLED=false

NEWSWIRE_GATEWAY_ENABLED=true

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

When `ENGINE_ENABLED=true`, `ENGINE_VALIDATION_DIGEST_ENABLED=true`, `DISCORD_BOT_TOKEN` is set, and `AUTONOMY_ALERT_CHANNEL_ID` is configured, the app posts scheduled engine validation digests to Discord. The digest reports research and paper-eligible allocation scopes separately, execution-cost quality, effective non-overlapping blocks, confidence intervals, and the actual readiness blocker codes. Research candidates are deduplicated by strategy version, asset, side, horizon, and blocker set rather than candidate ID.

Alert conditions include stale engine loop, engine runtime errors, paper intents/reports in shadow-only mode, live mode enabled, risk reject spikes, missing/stale feature or regime data, and EV calibration drift once realized attribution samples exist.

## Discord operator proposals are a bounded sample

The engine evaluates and persists every candidate, but Discord proposal messages are intentionally a review sample rather than a trade ledger. A candidate can become an operator proposal only when it is directional, paper-eligible, counts toward active alpha breadth, clears the configured EV/utility/confidence/feature-coverage floors, has a positive allocation, and passes RiskGateway, Council, debate, and expiry checks.

Eligible candidates are ranked by net EV, risk-adjusted utility, confidence, and raw alpha score. Delivery is then capped by `ENGINE_OPERATOR_MAX_PROPOSALS_PER_LOOP` (default `3`), `ENGINE_OPERATOR_MAX_PROPOSALS_PER_DAY` (default `10`), candidate deduplication, and `ENGINE_OPERATOR_SYMBOL_COOLDOWN_MINUTES` (default `30`). Candidates that do not become Discord proposals remain in the candidate book, diagnostics, digest, readiness, and dashboard evidence. A proposal acknowledgment records review only and never creates a paper or live order.

## Paper-readiness scorecard

`GET /engine/readiness` returns a deterministic conservative promotion scorecard. Paper readiness is blocked by live flags, paper leakage during shadow-only mode, stale engine loops, runtime errors, insufficient shadow observation/sample size, missing core feature/regime data, critical risk-reject spikes, failed replay comparisons, unhealthy PnL marking, missing exact-version approval, or weak measured execution evidence.

The default gate requires 24h shadow observation, at least 100 engine runs, 250 candidates, 50 shadow intents, 95% EV/feature/regime coverage, 100% candidate strategy metadata coverage, 95%+ Council review coverage, 100% RiskGateway coverage, at least 5 explicitly paper-approved directional strategy versions across 3 families, at least 20 matured outcomes per active strategy, and at least 30 effective time blocks per strategy/horizon. Both measured execution-adjusted return and realized-R 95% lower bounds must be positive. Concentration must remain below 55%/60%/35%, the latest replay must be `passed` or `advisory_pass`, and there must be no hard blocks with score >=85. Concentration is report-only before 50 directional shadow intents.

Five- and fifteen-minute outcomes use one-hour blocks, one-hour outcomes use four-hour blocks, four-hour outcomes use daily blocks, and 24-hour outcomes use seven-day blocks. Boundary-crossing windows are purged. Candidates first collapse to an instrument/block mean; instruments are then equal-weighted inside each block. Fewer than eight blocks is descriptive only, and thousands of overlapping candidates never count as thousands of independent trials.

`regime_defensive_flat_v1` is an explicit no-trade control. Its candidates receive RiskGateway/Council evidence, but never enter allocation-share denominators and never create an order intent. Directional shadow sampling uses separate evidence-admission quotas (45% strategy target, 60% family cap, 35% symbol-strategy cap) after raw candidates and governance evidence are persisted. This balances learnable evidence without deleting candidates or weakening the paper gate.

## Strategy portfolio, Council, replay, and bandit reports

Wave 1 and Wave 2 are one **evidence-producing research portfolio**. Migration `0034` freezes every exact strategy version already in the catalog. A previously unseen exact version defaults to `research_only`; no runtime endpoint can mutate it to `paper_approved`. Environment flags alone therefore cannot promote the current versions. A separately reviewed governance change must approve a new exact version after its strict, measured evidence passes the gates.

Wave 1A locks the strategy-regime candidate nucleus:

- `microstructure_ofi_v2`
- `liquidation_cascade_v1`
- `liquidation_mean_revert_v1`
- `funding_carry_v1`
- `oi_breakout_v1`
- `regime_defensive_flat_v1`

The five directional Wave 1A strategies count as active alpha breadth by default. `regime_defensive_flat_v1` is a no-trade control and does not count as independent alpha breadth.

Wave 1B adds the evidence spine: every candidate receives candidate evidence links, fixed delayed outcome windows (`5m`, `15m`, `1h`, `4h`, `24h`), candidate-level RiskGateway coverage for non-flat candidates, Council packet/no-trade coverage, replay context links, and strategy-regime performance rows sourced from outcome attribution.

`ENGINE_ALPHA_CATALOG_MODE` controls runtime strategy breadth:

- `wave1a_locked` — baseline Wave 1A nucleus only; pre-Wave1A, Wave1C, and Wave2 remain specs/comparison-only.
- `wave1c` — Wave 1A plus deterministic Wave 1C strategies.
- `integrated` — the default unified Wave 1 + Wave 2A/2B/2C portfolio; all strategies use the standard paper-shadow contract and readiness path.
- `specs_only` — expose planned specs while keeping runtime emissions locked to Wave 1A.

Wave 1C deterministic strategies are enabled in the default `integrated` catalog. The deterministic set is `microstructure_absorption_v1`, `funding_squeeze_v1`, `basis_reversion_v1`, and `news_impulse_v1`; `range_rotation_v1` and `volatility_compression_breakout_v1` are also active but remain data-gated and replayable.

Canonical Newswire story revisions can be bridged into the engine with `ENGINE_NEWSFEED_ENABLED=true`. V2 routes stories explicitly as `ignore`, `ledger_only`, `risk_only`, `directional_feature`, or `macro_proxy`; consumers no longer depend on one scalar threshold. Routed stories derive catalyst/impact/source-consensus features and feed a persisted, decaying `neutral|risk_on|risk_off|shock` state machine with evidence story IDs. `ENGINE_NEWS_RISK_OVERLAY_MODE=shadow` records counterfactual blocks/sizing without applying them; `active` lets shocks block new risk and reduces risk-off long sizing. `news_event_alpha_v2` is independently controlled by `ENGINE_NEWS_ALPHA_MODE=off|shadow|paper` and requires trusted or corroborated news plus market confirmation. Neither path creates live authority or bypasses RiskGateway/Council. See [Newswire V2](newswire-v2.md).

Every candidate builds a `CandidateTradePacket`, receives a deterministic role-based Council review, and must pass RiskGateway plus Council before a paper/shadow execution report can exist. The offline contextual-bandit endpoint is report-only: it writes recommendations with `auto_apply_allowed=false` and never mutates config, risk limits, or orders.

All twelve Wave 2A/2B/2C strategies remain defined and run through the common research evidence path: 2A lead/lag, liquidity vacuum, stop-cluster, and liquidation divergence; 2B crowded long/short unwind and liquidation-cluster followthrough/exhaustion; 2C perp-basis momentum/reversion, funding-curve dislocation, and carry-risk-off. Their exact current versions are frozen, their allocations are labeled `research`, and they cannot produce paper intents. Wave 2D remains a report-only policy recommender and may not place orders, raise leverage, bypass RiskGateway/Council, or auto-apply production config.

## Canonical watchlist and provider identities

Migration `0030` adds a provider-specific instrument registry, persistent pinned/broad memberships, immutable universe snapshots, venue market snapshots, and pairwise cross-venue feature snapshots. The bootstrap set contains 85 provider instruments representing 62 requested underlyings: 9 Hyperliquid main crypto perps, 53 TradeXYZ HIP-3 instruments, and 23 Alpaca Paper equities/ETFs. Read-only Lighter discovery adds provider identities for matching core perps after the first successful sync without inflating the underlying count. Eight requested HIP-3 symbols remain visible as `delisted` or `absent` until provider metadata proves otherwise; they are never silently enabled. Duplicate names such as IBM are deduplicated per provider; an MSFT HIP-3 perp and Alpaca MSFT have different `instrument_id` values but share `underlying_id=EQUITY:MSFT`.

Discord admins can use:

```text
watchlist list [tier=pinned|broad] [venue=hyperliquid:xyz]
watchlist add NVDA,AAPL,MSFT venue=alpaca:paper tier=broad
watchlist move <instrument_id> tier=pinned|broad
watchlist remove <instrument_id>
watchlist unresolved
watchlist history
watchlist import us-large-cap
watchlist confirm <change_id>
```

Remove and official SPY daily-holdings imports require a second confirmation. Imported holdings remain data-only until Alpaca metadata verifies the exact tradable asset. Every applied change republishes an atomic versioned snapshot.

Lighter market data uses the official [`elliottech/lighter-python`](https://github.com/elliottech/lighter-python) SDK pinned to v1.1.0. The adapter constructs only public REST/WebSocket clients, has no signer or transaction interface, accepts market ID zero, detects exposed sequence/nonce regressions, and feeds a local depth-walking paper simulator. Because the SDK currently declares an obsolete upper bound for `urllib3`, the lockfile explicitly overrides that transitive constraint to a maintained release; adapter tests run against the resolved override. Alpaca uses separate Paper credentials, accepts only `https://paper-api.alpaca.markets`, submits broker-hosted bracket orders, and mirrors broker account/order/fill/position state as the source of truth. HIP-3/Alpaca and Hyperliquid/Lighter features are stored pairwise with explicit clock-skew/staleness flags; venue prices are never averaged. A bounded rotating HIP-3 depth scan feeds canonical spread/depth/funding/OI/basis features into the shadow strategy loop, so Wave 2 can evaluate requested HIP-3 equities, indices, FX, and commodities without expanding Wave 1's crypto scope.

## Shadow replay, diagnostics, and PnL marking

`POST /engine/replay-comparisons/run` stores immutable engine shadow comparison summaries in the existing `replay_results` storage shape with `proposal_id="engine:{variant_id}"` and `metadata.artifact_type="engine_shadow_comparison"`.

Strategy throttles cap candidates and allocations per strategy and annotate throttled candidates/allocations without creating exchange actions. The diversity controller additionally enforces 45% target strategy share, 55% hard strategy share, 60% family share, and 35% symbol+strategy share once the evidence window has enough samples.

The engine PnL attribution loop marks simulated paper/shadow positions from Hyperliquid `all_mids`, records `pnl_attribution_records`, and closes simulated theses on stop/target/max-age conditions.

`GET /engine/candidate-funnel` reconstructs the first terminal stage for each candidate from its pre-Council packet through shadow intent and matured attribution. It reports downstream reason codes separately, so a Council veto cannot also masquerade as an allocator root cause. `GET /engine/strategy-funnel` records every strategy/asset evaluation after migration `0029`, including selector gates, feature presence/age, trigger outcome, candidate count, and structured no-candidate reasons. Historical periods without that telemetry are reported as unavailable, not as zero activity.

`GET /engine/signal-quality` uses the fixed grain `(candidate_id, outcome_window)`, canonical regime/allocation joins, and strict `feature_store_mid` marks. It never pools horizons for promotion. Gross return, modeled net return, and measured execution-adjusted return are distinct; configured, stale, top-of-book-only, or unavailable costs never qualify as measured. Confidence intervals use the purged block method above.

`GET /engine/strategy-research` runs predeclared grids for positive-gross OFI and liquidity-vacuum slices. Absorption uses a separate, immediately measurable redesign gate requiring large imbalance, two-sided visible depth, depth replenishment, and constrained five-minute price response before a new research-only version can be proposed; aggressive-trade-to-depth is retained as a future feature backlog item. Multiple tests use Benjamini-Hochberg control and expanding walk-forward stability; passing results create no runtime strategy or promotion automatically. `POST /engine/news-risk-counterfactuals/run` remains research-only and cannot replace the readiness replay.

See `docs/engine-paper-readiness-runbook.md` for promotion and rollback steps.

## Agentic wave orchestration

The optional Wave Supervisor automates observation, diagnosis, report-only maintenance, bounded blocker escalation, and verification prep without directly mutating config. It may refresh strategy-regime performance, run current-config replay comparisons, emit `agent-core` traces, and render LHP-compatible handoff payloads for Engineering Loop/NOC review. Actual paper promotion and deploys still require a draft PR or signed operator change; the supervisor never flips the integrated catalog, paper, or live settings by itself.

Key flags:

- `ORCHESTRATION_WAVE_SUPERVISOR_ENABLED=false`
- `ORCHESTRATION_WAVE_SUPERVISOR_ESCALATION_ENABLED=false`
- `ORCHESTRATION_WAVE_SUPERVISOR_ESCALATION_TRANSPORT=disabled|github_issue`
- `ORCHESTRATION_GATE_SNAPSHOTS_ENABLED=true`
- `ORCHESTRATION_GATE_SNAPSHOT_MILESTONE_HOURS=24,72`
- `ORCHESTRATION_GATE_SNAPSHOT_GITHUB_ENABLED=true`
- `AGENT_CORE_TRACE_ENABLED=false`

At each due milestone the scheduler stores one immutable, SHA-256-addressed evidence payload in `wave_supervisor_runs`. The clean-window anchor is the later of the current trader and Newswire starts, so either worker restarting moves future milestones. Components fail independently and include readiness, exact current-config replay, candidate/strategy funnels, fixed-horizon signal quality, Newswire soak and counterfactual, Discord feedback, and paper/live side-effect checks. When a GitHub token is configured, bounded projections are posted once to issues `#10`, `#16`, and `#21` using hidden idempotency markers.

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
GET /engine/universe
GET /engine/universe/unresolved
GET /engine/universe/history
GET /engine/venue-market-snapshots
GET /engine/cross-venue-feature-snapshots
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
GET /engine/strategy-version-policies
GET /engine/execution-cost-quotes
GET /engine/strategy-research
GET /engine/risk-rejects
GET /engine/pnl-attribution
GET /engine/validation-report
GET /engine/readiness
GET /engine/universe
GET /engine/universe/unresolved
GET /engine/universe/history
POST /engine/admin/watchlist/changes
POST /engine/admin/watchlist/changes/{change_id}/confirm
GET /engine/venue-market-snapshots
GET /engine/cross-venue-feature-snapshots
GET /engine/candidate-funnel
GET /engine/strategy-funnel
GET /engine/signal-quality
GET /engine/replay-comparisons
GET /engine/replay-comparisons/latest
POST /engine/replay-comparisons/run
GET /engine/news-risk-counterfactuals
GET /engine/news-risk-counterfactuals/latest
POST /engine/news-risk-counterfactuals/run
GET /orchestration/wave/status
POST /orchestration/wave/run-once
GET /orchestration/wave/runs?artifact_type=gate_evidence_snapshot
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
- `0029_engine_strategy_evaluations`

High-frequency event/feature data is intended for bounded retention and rollups; candidates, per-run strategy evaluations, candidate evidence links, delayed outcome attributions, replay result links, strategy specs, strategy-regime scorecards, Council reviews/votes, diversity/concentration events, bandit report-only recommendations, risk checks, evidence packs, execution reports, position theses, attribution, and governance records are durable audit artifacts.
