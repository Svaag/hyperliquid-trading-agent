# Newswire Smoke-Test Checklist

Run after enabling `ALPACA_NEWS_ENABLED=true` with valid API keys.

## Pre-flight

- [ ] `ALPACA_API_KEY` and `ALPACA_API_SECRET` are set in `.env` (get from https://app.alpaca.markets/)
- [ ] `ALPACA_NEWS_ENABLED=true`
- [ ] `NEWSWIRE_ENABLED=true` for the `newswire` worker (Compose sets this on the service)
- [ ] `DISCORD_PUBLISHER_ENABLED=true` only for the optional `discord-publisher` worker
- [ ] `DISCORD_BOT_TOKEN` is set for the send-only publisher
- [ ] `NEWSWIRE_NEWS_CHANNEL_ID` is set to a Discord channel ID (for the curated news feed)
- [ ] `ALPACA_NEWS_SYMBOLS=*` (or your watchlist like `AAPL,NVDA,MSFT,TSLA`)
- [ ] `NEWSWIRE_ROUTING_MODE=active` (`shadow` computes V2 decisions but keeps legacy consumer routing)
- [ ] `ENGINE_NEWS_RISK_OVERLAY_MODE=shadow` for the initial observation window
- [ ] `ENGINE_NEWS_ALPHA_MODE=shadow` (or `off`) for the initial observation window

## Start-up Checks

```bash
docker compose up -d api newswire
# Optional Discord publishing:
docker compose --profile discord-publisher up -d discord-publisher
```

- [ ] API starts without Alpaca config warnings: `curl http://127.0.0.1:8081/health/config | jq '.newswire.warnings'` should show no Alpaca-related warnings
- [ ] `curl http://127.0.0.1:8081/runtime/heartbeats` shows a fresh `newswire` heartbeat
- [ ] `curl http://127.0.0.1:8081/newswire/status` shows worker-owned persisted status and a recent latest event after ingestion starts
- [ ] Check `docker compose logs newswire` for `alpaca_news_subscribed` with your symbols

## Ingestion Pipeline

- [ ] `curl http://127.0.0.1:8081/newswire/status` shows recent persisted Newswire activity
- [ ] `curl 'http://127.0.0.1:8081/newswire/feed?limit=5'` returns clustered stories with `story_id`, `revision`, and `assessment`
- [ ] `curl http://127.0.0.1:8081/newswire/events?source=alpaca&limit=5` returns Alpaca-sourced events with populated fields:
  - `event_id` starts with `nw_`
  - `headline` and `body` are non-empty
  - `symbols` array is populated (e.g., `["AAPL","NVDA"]`)
  - `asset_class` is `"equity"` for stock news
  - `event_type` is classified (e.g., `"earnings"`, `"analyst_rating"`, `"press_release"`)
  - `importance_score` is between 0-100
  - `assessment.assessment_version` is `newswire_assessment_v2.1`
  - `assessment.audience_scope` explains whether the story is watched, broad-market, or an unwatched single name
  - `assessment.feed_action` and `assessment.engine_action` are populated with reason codes
  - `tradability.allow_auto_trade` is `false`
  - `tradability.halt_state_checked` is `true`

## Deduplication

- [ ] Wait a few minutes for repeated headlines
- [ ] `curl http://127.0.0.1:8081/newswire/status` shows one latest event stream, not duplicate worker streams
- [ ] Repeated/corroborating reports appear as one `/newswire/feed` story with an increased `revision`/`independent_source_count`
- [ ] Raw `newswire_events` still dedupe the same upstream `external_id`

## Discord Delivery

- [ ] `POST /newswire/discord/test` returns an accepted command for `discord_publisher`; poll the returned `/commands/{id}` until completed
- [ ] V2 `high`/`breaking` stories appear immediately, capped by `NEWSWIRE_DISCORD_MAX_IMMEDIATE_PER_HOUR`
- [ ] V2 `standard` stories appear in periodic digest posts (every `NEWSWIRE_DIGEST_INTERVAL_SECONDS`)
- [ ] `/newswire/status` reports Discord gateway `ready=true`; a merely running worker is not reported as ready
- [ ] `/newswire/status` reports a healthy delivery outbox with no growing `failed`/old `pending` count
- [ ] Messages render as a single Discord embed without a duplicated plaintext digest
- [ ] Headlines render decoded punctuation such as apostrophes instead of HTML entities like `&#39;`
- [ ] Halt-state events (`⛔ Halt state`) show the halt warning when applicable

## Institutional Engine Delivery

The `trader` worker consumes persisted canonical story revisions through `trader:engine_newswire`; it does not own Alpaca/RSS/X/TradingEconomics connections.

- [ ] `curl http://127.0.0.1:8081/runtime/offsets` includes `trader:engine_newswire`
- [ ] `curl http://127.0.0.1:8081/runtime/heartbeats?service_role=trader` shows `metadata.engine_newsfeed.pump.running=true`
- [ ] `curl http://127.0.0.1:8081/newswire/readiness` reports a time-based continuous soak; worker restarts reset its 24-hour clock
- [ ] `/runtime/dashboard` shows Engine Newsfeed offset age, processed/recorded/features counts, skips, and degraded reasons
- [ ] Stories with `assessment.engine_action != ignore` appear as appropriate in `/engine/events?event_type=newswire`
- [ ] `GET /newswire/risk-state` exposes persisted state/evidence and transition history
- [ ] With overlay mode `shadow`, regime metadata shows the observed news mode while effective permissions/sizing remain unchanged
- [ ] `directional_feature` stories require trusted/corroborated sources and market confirmation before `news_event_alpha_v2` emits
- [ ] Engine news features are advisory only: no Discord news post or engine news event places a trade

## WebSocket Stream

- [ ] Connect: `wscat -H "Authorization: Bearer <AGENT_API_BEARER_TOKEN>" wss://<host>/newswire/stream`
- [ ] Send optional filter frame: `{"filter":{"asset_classes":["equity"],"min_importance":50}}`
- [ ] Receive streaming `NewswireEvent` JSON objects in real-time

## Adapter Resilience

- [ ] Temporarily kill network: adapter should log `newswire_adapter_restart` and reconnect with backoff
- [ ] After reconnect, adapter should re-authenticate and re-subscribe
- [ ] `curl http://127.0.0.1:8081/newswire/status` reports degraded/latest state but the API remains healthy
- [ ] `curl http://127.0.0.1:8081/newswire/sources` — `"alpaca"` shows `"transport":"websocket"` with correct source score

## Routing Reference

- Discord #news: `assessment.feed_action` drives drop/watch/standard/high/breaking routing. Legacy importance thresholds apply only when V2 routing is shadowed or an old event has no assessment.
- Institutional Engine: `assessment.engine_action` drives ledger/risk/directional/macro routing. `ENGINE_NEWS_MIN_SOURCE_SCORE` remains a final source-quality guard.
- Audience guardrail: an unwatched single-name equity can never exceed `watch`; a trusted shock reaches `breaking` only for a watched asset or broad-market scope.
- Model review: only fresh deterministic scores within ±3 points of a routing boundary are eligible. Startup backlog, stale stories, unwatched equities, replay, and reclassification are excluded; each item gets one model attempt with no repair call.
- Risk state: `ENGINE_NEWS_RISK_*` controls decay, TTLs, transition thresholds, hysteresis, and `shadow|active` application.

## End-to-End with Autonomy

- [ ] `curl http://127.0.0.1:8081/autonomy/news` shows news events with `provider: "alpaca"` flowing into the autonomy market map when the `trader` worker has autonomy enabled
- [ ] Market map `/autonomy/market-map` reflects asset mentions from Alpaca news
- [ ] Signal generation still works for crypto (news feed doesn't interfere with crypto signals)
