# Newswire V2

Newswire V2 treats news as a durable product feed and as bounded trading-engine evidence. It replaces one opaque importance threshold with canonical stories, explainable assessment actions, and a persisted risk-state overlay.

## Data flow

```text
RSS / Alpaca / Trading Economics / curated X
  -> bounded ingest queue
  -> normalize + entity/topic resolution
  -> canonical story clustering and revision
  -> deterministic V2 assessment
  -> optional selective model review
  -> newswire_stories + append-only newswire_story_revisions
       -> API / WebSocket feed
       -> durable Discord delivery outbox
       -> World Model beliefs
       -> Engine ledger, features, and persisted risk state
```

Raw `newswire_events` remain available for audit and compatibility. Consumers use `newswire_story_revisions`, so one story can gain independent confirmations, corrections, or a retraction without appearing as unrelated headlines.

## Watch set and assessment

The watch set refreshes from configured/core symbols, open position theses, paper positions, active alpha candidates/signals, and top-volume assets. Entity matching records why each symbol matched and requires safer evidence for ambiguous short tickers.

Each story revision receives independently inspectable scores:

- relevance: 35%
- impact: 30%
- urgency: 15%
- source quality: 10%
- novelty: 10%

The assessment emits two explicit routes:

- Feed: `drop`, `watch`, `standard`, `high`, or `breaking`.
- Engine: `ignore`, `ledger_only`, `risk_only`, `directional_feature`, or `macro_proxy`.

Watched assets receive a minimum `standard` route for fresh, credible first reports even when the legacy keyword score would have missed the old Discord threshold. Directional engine features require high impact and direction confidence plus either a primary-quality source or independent corroboration. A story never grants execution authority.

Set `NEWSWIRE_ROUTING_MODE=shadow` to persist V2 decisions while consumers continue using legacy thresholds. `active` is the default for the new feed.

## Selective model review

Deterministic rules remain authoritative. A structured model review is requested only for uncertain/high-value classifications, with a five-second default timeout, an hourly call cap, and a content cache. The model can move a feed decision by at most one tier and cannot create a `breaking` route by itself.

Use:

```text
NEWSWIRE_MODEL_CLASSIFY_ENABLED=true
NEWSWIRE_MODEL_CLASSIFY_MAX_CALLS_PER_HOUR=30
NEWSWIRE_MODEL_CLASSIFY_TIMEOUT_SECONDS=5
```

## Discord delivery

Each story revision receives a deterministic outbox identity per destination/channel. Breaking/high stories post immediately subject to `NEWSWIRE_DISCORD_MAX_IMMEDIATE_PER_HOUR`; standard stories are digested. Failed sends remain durable and retry with bounded exponential backoff. Publisher status distinguishes a running worker from a ready Discord gateway and reports pending/failed outbox counts.

## Engine risk and alpha

The engine consumes all routed revisions into its evidence ledger, then maintains decaying per-symbol and global news risk state: `neutral`, `risk_on`, `risk_off`, or `shock`. State transitions and supporting story IDs are persisted. Negative primary-source shocks enter quickly; positive risk-on requires corroboration and market confirmation; exits use TTL, decay, minimum hold, and hysteresis. Retractions remove the source contribution and supersede World Model beliefs.

`ENGINE_NEWS_RISK_OVERLAY_MODE=shadow` records the observed state and allocation counterfactual without changing permissions or sizing. `active` applies shock blocks and risk-off long-size reductions. This switch is independent from Newswire ingestion.

`news_event_alpha_v2` is independently gated by `ENGINE_NEWS_ALPHA_MODE=off|shadow|paper`. It requires a `directional_feature` route, material impact, high direction confidence, a trusted/corroborated story, and confirming price/order-book features. Its default is shadow; it has no live execution path.

## API and operations

- `GET /newswire/feed`: current canonical stories with action/symbol/topic filters.
- `GET /newswire/stories/{story_id}`: current story plus revision history.
- `GET /newswire/risk-state`: persisted engine risk states and transitions.
- `GET /newswire/events`: raw-event compatibility/audit view.
- `GET /autonomy/news`: compatibility projection backed by canonical stories.
- `WS /newswire/stream`: canonical story revisions projected onto the additive `NewswireEvent` schema.

`GET /newswire/status` includes latest story, feed/engine action counts, worker heartbeats, risk state, and Discord outbox health. Prometheus metrics expose assessment routes, story revisions, model-review outcomes, Discord delivery results, and risk transitions.

Apply migration `0026_newswire_v2` before starting workers. The world-model, trader, and Discord publisher pumps now read `newswire_story_revisions`; existing raw events are retained but are not replayed as canonical history automatically.
