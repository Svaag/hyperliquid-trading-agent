# Newswire V2.1

Newswire V2.1 treats news as a durable product feed and as bounded trading-engine evidence. It replaces one opaque importance threshold with canonical stories, explainable audience-aware assessment actions, and a persisted risk-state overlay.

## Data flow

```text
RSS / Alpaca / Trading Economics / curated X
  -> bounded ingest queue
  -> normalize + entity/topic resolution
  -> canonical story clustering and revision
  -> deterministic V2.1 audience-aware assessment
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

The assessment includes `audience_scope` (`watched_asset`, `broad_market`, `unwatched_single_name`, or `general`) and emits two explicit routes:

- Feed: `drop`, `watch`, `standard`, `high`, or `breaking`.
- Engine: `ignore`, `ledger_only`, `risk_only`, `directional_feature`, or `macro_proxy`.

Watched assets receive a minimum `standard` route for fresh, credible first reports even when the legacy keyword score would have missed the old Discord threshold. Unwatched single-name equities are capped at `watch`, and trusted shocks bypass the numeric breaking threshold only for watched or broad-market audiences. Directional engine features require high impact and direction confidence plus either a primary-quality source or independent corroboration. A story never grants execution authority.

Set `NEWSWIRE_ROUTING_MODE=shadow` to persist V2 decisions while consumers continue using legacy thresholds. `active` is the default for the new feed.

## Selective model review

Deterministic rules remain authoritative. A structured model review is requested only for fresh stories within three points of a routing boundary. Startup backlog, stale rows, unwatched equities, replay, and reclassification are ineligible. Reviews use a bounded queue, one model call with no repair call, a five-second default timeout, an hourly call cap, and a content cache. The model can move a feed decision by at most one tier and cannot create a `breaking` route by itself.

Use:

```text
NEWSWIRE_MODEL_CLASSIFY_ENABLED=true
NEWSWIRE_MODEL_CLASSIFY_MAX_CALLS_PER_HOUR=30
NEWSWIRE_MODEL_CLASSIFY_TIMEOUT_SECONDS=5
NEWSWIRE_MODEL_CLASSIFY_QUEUE_SIZE=32
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
- `GET /newswire/calibration`: persisted score/action distributions and guardrail evidence.
- `POST /newswire/reclassify`: bounded dry-run/apply reassessment owned by the Newswire worker.
- `POST /newswire/replay`: timestamp-preserving, no-authority engine evidence replay owned by the trader.
- `GET /newswire/readiness`: continuous 24-hour soak evidence that resets on worker restart.
- `GET /autonomy/news`: compatibility projection backed by canonical stories.
- `WS /newswire/stream`: canonical story revisions projected onto the additive `NewswireEvent` schema.

`GET /newswire/status` includes latest story, feed/engine action counts, worker heartbeats, risk state, Discord outbox health, and Engine Newsfeed health. `/runtime/dashboard` renders offset age, consumer counters, skips, and degraded reasons; the trader validation monitor sends deduplicated operational alerts.

Apply migration `0026_newswire_v2` before starting workers. The world-model, trader, and Discord publisher pumps now read `newswire_story_revisions`; existing raw events are retained but are not replayed as canonical history automatically.
