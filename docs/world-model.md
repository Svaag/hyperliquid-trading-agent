# Market World Model

The market world model is an advisory evidence and memory layer for the paper/shadow trading engine.

It does not:

- place live orders
- sign payloads
- relax risk limits
- mutate strategy thresholds
- auto-promote prediction-market signals into execution

## Flow

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

- `SERVICE_ROLE=newswire` owns external news provider connections and persists normalized `newswire_events`.
- `SERVICE_ROLE=world_model` consumes persisted events through `consumer_offsets`, updates world-model tables/snapshots, and owns prediction-market streams.
- The deprecated `world-model-live` profile is a no-port compatibility alias and must not expose a dashboard.

For a blank local instance, `POST /world-model/dev/seed` creates a `world_model` worker command outside tests. The endpoint is disabled unless `WORLD_MODEL_DEV_SEED_ENABLED=true` and the environment is local/dev/test.

## Supervision

- Operator annotations are append-only audit marks: `confirmed`, `disputed`, `needs_review`, or `pinned`.
- Outcomes calibrate event/source credibility and prediction-market Brier scores.
- Time-travel uses persisted snapshots plus replay windows to answer "what did the model believe at this time?"
- Dashboard graph modes: belief tree, event timeline, contradiction graph, prediction-market consensus, and source reliability.

## Source Adapters

The live architecture is stream-first, with REST retained for discovery, manual repair, and backfill:

- `newswire`: persists Alpaca/RSS/Trading Economics/X events to `newswire_events`; World Model consumes them from Postgres, not an in-process API bus.
- `polymarket_ws`: subscribes to the public Polymarket market WebSocket and normalizes market updates into stable prediction signals.
- `polymarket`: REST discovery/backfill for active markets.
- `kalshi`: REST normalization remains available; WebSocket streaming is deferred.
- `x`: normalizes recent-search tweets into social events when `X_BEARER_TOKEN` is configured.
- `tavily`: normalizes search results into macro/newswire enrichment events when `TAVILY_API_KEY` is configured.

Adapters retain raw source payloads in metadata for audit/calibration and mark every item with `execution_authority=none`.

## Integration

- Newswire events feed the world model through the agent news consumer.
- HIP-4 outcome books become prediction-market probability signals.
- HIP-4 edge candidates become advisory evidence only.
- Completed signal and alpha-event evaluations reinforce source credibility and episodic memory.
- The institutional engine records world-model features such as `narrative_pressure`, `belief_conflict_score`, `source_consensus_score`, `prediction_implied_probability`, and `belief_salience`.
- High-stakes roles receive a compact wiki block labeled advisory-only.
- The engine feature boundary rejects world-model snapshots carrying execution authority, exchange actions, order intents, risk mutations, or config changes.
