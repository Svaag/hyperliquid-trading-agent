# Market World Model

The market world model is an advisory evidence and memory layer for the paper/shadow trading engine.

It does not:

- place live orders
- sign payloads
- relax risk limits
- mutate strategy thresholds
- auto-promote prediction-market signals into execution

## World Model v2

Set `WORLD_MODEL_V2_ENABLED=true` on the `api`, `world-model`, `agent`, and `trader` services for the clean v2 cutover. V2 writes only `world_model_v2_*` tables; v1 tables and prediction-paper positions remain untouched. The new `world_model_v2:newswire` consumer bootstraps from the latest story revision, so corrupt v1 history is not replayed.

V2 separates concepts that v1 conflated:

- forecasts contain a canonical binary Yes probability or a multi-outcome distribution; probability is never a bullish/bearish direction
- macro states use factor-specific axes, point-in-time official observations, level, momentum, normalized surprise, freshness, and coverage
- asset impacts are `supportive|adverse|neutral|unknown`, scoped to `intraday|swing|regime`, and marked `current` or explicitly `conditional`
- evidence is admitted, quarantined, or rejected by deterministic factor/instrument mappings

Official baseline sources are keyless BLS and US Treasury nominal/real curves. FRED breadth is enabled only when `WORLD_MODEL_V2_FRED_API_KEY` is configured; otherwise the model reports partial coverage and does not synthesize missing inputs. Custom cross-asset exposure profiles use `WORLD_MODEL_V2_EXPOSURE_PROFILES_JSON` with factor weights restricted to `-1`, `0`, or `1`.

Prediction discovery remains broad for paper/manual search, while the v2 feature subset is relevance-gated, volume ordered, quality checked, and capped. Sports, entertainment, celebrity, crime, and unmapped political markets cannot enter v2 forecasts.
`WORLD_MODEL_V2_PREDICTION_SCAN_MARKETS` controls how many volume-ordered Polymarket rows are paged through for relevant contracts (default `1000`); `WORLD_MODEL_V2_PREDICTION_MAX_MARKETS` caps the admitted feature subset (default `100`).

Set `WORLD_MODEL_V2_SHADOW_FEATURES_ENABLED=true` to persist v2 engine features for comparison. They bypass the active in-memory feature snapshot and cannot affect paper or live strategy decisions.

## Legacy v1 Flow

```text
Newswire / social / HIP-4 / evaluations
  -> WorldEvent
  -> MarketBelief + NarrativeCluster + PredictionMarketSignal
  -> WorldMemoryAtom + SourceCredibility
  -> WorldModelSnapshot
  -> autonomy signal metadata / engine features / high-stakes prompt context
```

## Evidence Types

- `WorldEvent`: canonical evidence item with provenance, timestamps, source score, and payload.
- `MarketBelief`: evidence-backed belief with confidence, salience, status, and contradiction links.
- `NarrativeCluster`: grouped beliefs by symbol/topic with pressure, consensus, and conflict scores.
- `PredictionMarketSignal`: implied probability from HIP-4 outcome books and optional Polymarket/Kalshi-style adapters.
- `WorldMemoryAtom`: wiki-style compact memory for current catalysts, episodic outcomes, source reliability, and prediction priors.

## Read-Only API

```http
GET /world-model/status
GET /world-model/snapshot?symbol=BTC
GET /world-model/events
GET /world-model/beliefs
GET /world-model/prediction-markets
GET /world-model/macro-state
GET /world-model/asset-impacts
GET /world-model/quality
GET /world-model/memory
GET /world-model/dashboard/data
GET /world-model/snapshots
GET /world-model/snapshots/nearest
GET /world-model/replay
POST /world-model/annotations
POST /world-model/outcomes
GET /world-model/prediction-calibration
GET /world-model/streams/status
```

These endpoints are protected by the existing agent API token outside dev/test/local.

## Service-Role Runtime

Use the single public API for local supervision:

```bash
docker compose up api
```

The `api` service applies no side-effect workers. It exposes dashboards/API on `127.0.0.1:${HOST_PORT:-8081}` and reads persisted world-model state from Postgres.

Real-time world state is owned by workers with no public ports:

```bash
docker compose up newswire world-model
```

- `SERVICE_ROLE=newswire` owns external news provider connections and persists raw events plus canonical `newswire_stories` / append-only `newswire_story_revisions`.
- `SERVICE_ROLE=world_model` consumes persisted story revisions through `world_model_v2:newswire` when v2 is enabled (otherwise `world_model:newswire`), updates the matching versioned stores, and owns prediction-market streams.
- `SERVICE_ROLE=trader` consumes the same revision stream through `trader:engine_newswire` to derive Institutional Engine news evidence/features/risk state. It keeps `NEWSWIRE_ENABLED=false` and never opens news provider connections.
- The deprecated `world-model-live` profile is a no-port compatibility alias and must not expose a dashboard.

For a blank local instance, `POST /world-model/dev/seed` creates a `world_model` worker command outside tests. The endpoint is disabled unless `WORLD_MODEL_DEV_SEED_ENABLED=true` and the environment is local/dev/test.

## Supervision

- Operator annotations are append-only audit marks: `confirmed`, `disputed`, `needs_review`, or `pinned`.
- Outcomes calibrate event/source credibility and prediction-market Brier scores.
- Time-travel uses persisted snapshots plus replay windows to answer "what did the model believe at this time?"
- The v2 dashboard is split into a template, stylesheet, and JavaScript asset and presents Macro State, Cross-Asset Impact, Relevant Forecasts, Evidence, and Quarantine/Quality.

## Source Adapters

The live architecture is stream-first, with REST retained for discovery, manual repair, and backfill:

- `newswire`: persists Alpaca/RSS/Trading Economics/X events and clustered story revisions; World Model consumes revisions from Postgres, not an in-process API bus.
- `polymarket_ws`: subscribes to the public Polymarket market WebSocket and normalizes market updates into stable prediction signals.
- `polymarket`: REST discovery/backfill for active markets.
- `kalshi`: REST normalization remains available; WebSocket streaming is deferred.
- `x`: normalizes recent-search tweets into social events when `X_BEARER_TOKEN` is configured.
- `tavily`: normalizes search results into macro/newswire enrichment events when `TAVILY_API_KEY` is configured.

Adapters retain raw source payloads in metadata for audit/calibration and mark every item with `execution_authority=none`.

## Integration

- Canonical Newswire story revisions feed the world model through the persisted `world_model:newswire` consumer.
- HIP-4 outcome books become prediction-market probability signals.
- HIP-4 edge candidates become advisory evidence only.
- Completed signal and alpha-event evaluations reinforce source credibility and episodic memory.
- V1 records features such as `narrative_pressure`, `belief_conflict_score`, `source_consensus_score`, `prediction_implied_probability`, and `belief_salience`. V2 features are separately named and persist shadow-only without entering active feature state.
- The institutional engine also records direct Newswire features from `trader:engine_newswire`, including story impact/direction/source context and persisted `neutral|risk_on|risk_off|shock` state. V2 engine actions and source-quality guards route them; the overlay is shadow by default and remains paper-only.
- High-stakes roles receive a compact wiki block labeled advisory-only.
- The engine feature boundary rejects world-model snapshots carrying execution authority, exchange actions, order intents, risk mutations, or config changes.
