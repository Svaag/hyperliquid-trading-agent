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
```

These endpoints are protected by the existing agent API token outside dev/test/local.

## Dashboard-Only Runtime

Use the Compose dashboard profile for local supervision:

```bash
docker compose --profile dashboard up dashboard
```

The profile applies migrations, connects to Compose Postgres, and starts only the FastAPI dashboard/API. It does not start Discord, Alpaca/TradFi, HIP-4, autonomy, engine loops, position tracking, newswire workers, or Hyperliquid WebSocket streaming.

For a blank local instance, the dashboard can seed advisory-only demo data through `POST /world-model/dev/seed`. The endpoint is disabled unless `WORLD_MODEL_DEV_SEED_ENABLED=true` and the process is running in local/test/dev or `dashboard_only` mode.

## Supervision

- Operator annotations are append-only audit marks: `confirmed`, `disputed`, `needs_review`, or `pinned`.
- Outcomes calibrate event/source credibility and prediction-market Brier scores.
- Time-travel uses persisted snapshots plus replay windows to answer "what did the model believe at this time?"
- Dashboard graph modes: belief tree, event timeline, contradiction graph, prediction-market consensus, and source reliability.

## Source Adapters

Live source adapters are explicit pollers and are off by default:

- `polymarket`: normalizes active markets into stable market/outcome signal IDs.
- `kalshi`: normalizes open markets into stable YES probability signals.
- `x`: normalizes recent-search tweets into social events when `X_BEARER_TOKEN` is configured.
- `tavily`: normalizes search results into macro/newswire enrichment events when `TAVILY_API_KEY` is configured.

Adapters enforce a poll interval, retain raw source payloads in metadata for audit/calibration, and mark every item with `execution_authority=none`.

## Integration

- Newswire events feed the world model through the agent news consumer.
- HIP-4 outcome books become prediction-market probability signals.
- HIP-4 edge candidates become advisory evidence only.
- Completed signal and alpha-event evaluations reinforce source credibility and episodic memory.
- The institutional engine records world-model features such as `narrative_pressure`, `belief_conflict_score`, `source_consensus_score`, `prediction_implied_probability`, and `belief_salience`.
- High-stakes roles receive a compact wiki block labeled advisory-only.
- The engine feature boundary rejects world-model snapshots carrying execution authority, exchange actions, order intents, risk mutations, or config changes.
