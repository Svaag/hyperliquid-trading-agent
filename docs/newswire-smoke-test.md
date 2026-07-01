# Newswire Smoke-Test Checklist

Run after enabling `ALPACA_NEWS_ENABLED=true` with valid API keys.

## Pre-flight

- [ ] `ALPACA_API_KEY` and `ALPACA_API_SECRET` are set in `.env` (get from https://app.alpaca.markets/)
- [ ] `ALPACA_NEWS_ENABLED=true`
- [ ] `NEWSWIRE_DISCORD_ENABLED=true`
- [ ] `DISCORD_BOT_TOKEN` is set for the send-only publisher (or the full bot runtime)
- [ ] `NEWSWIRE_NEWS_CHANNEL_ID` is set to a Discord channel ID (for the curated news feed)
- [ ] `ALPACA_NEWS_SYMBOLS=*` (or your watchlist like `AAPL,NVDA,MSFT,TSLA`)

## Start-up Checks

- [ ] Service starts without Alpaca config warnings: `curl /health/config | jq '.newswire.warnings'` should show no Alpaca-related warnings
- [ ] `curl /newswire/status` shows `"running": true` and the alpaca adapter is in the adapter list with `"authenticated": true`
- [ ] Check logs for `alpaca_news_subscribed` message with your symbols

## Ingestion Pipeline

- [ ] `curl /newswire/status` shows `"buffered_events"` increasing over time (news events arriving)
- [ ] `curl /newswire/status` shows `"last_event_per_source"` includes `"alpaca"` with a recent timestamp
- [ ] `curl /newswire/events?source=alpaca&limit=5` returns Alpaca-sourced events with populated fields:
  - `event_id` starts with `nw_`
  - `headline` and `body` are non-empty
  - `symbols` array is populated (e.g., `["AAPL","NVDA"]`)
  - `asset_class` is `"equity"` for stock news
  - `event_type` is classified (e.g., `"earnings"`, `"analyst_rating"`, `"press_release"`)
  - `importance_score` is between 0-100
  - `tradability.allow_auto_trade` is `false`
  - `tradability.halt_state_checked` is `true`

## Deduplication

- [ ] Wait a few minutes for repeated headlines
- [ ] `curl /newswire/status` — `"buffered_events"` should grow but not explode (dedup is working)
- [ ] No duplicate headlines in `/newswire/events?limit=50` (same `external_id` not appearing twice)

## Discord Delivery

- [ ] `POST /newswire/discord/test` sends a clearly-labeled test embed to #news
- [ ] Breaking news (urgency=breaking or score >= NEWSWIRE_BREAKING_MIN_IMPORTANCE) appears immediately in the #news channel
- [ ] Non-breaking news appears in periodic digest posts (every NEWSWIRE_DIGEST_INTERVAL_SECONDS)
- [ ] Each message/embed footer includes "News feed only — no trade was placed."
- [ ] Halt-state events (`⛔ Halt state`) show the halt warning when applicable

## WebSocket Stream

- [ ] Connect: `wscat -H "Authorization: Bearer <AGENT_API_BEARER_TOKEN>" wss://<host>/newswire/stream`
- [ ] Send optional filter frame: `{"filter":{"asset_classes":["equity"],"min_importance":50}}`
- [ ] Receive streaming `NewswireEvent` JSON objects in real-time

## Adapter Resilience

- [ ] Temporarily kill network: adapter should log `newswire_adapter_restart` and reconnect with backoff
- [ ] After reconnect, adapter should re-authenticate and re-subscribe
- [ ] `curl /newswire/status` — `"adapter_errors"` should increment on failures but not crash the service
- [ ] `curl /newswire/sources` — `"alpaca"` shows `"transport":"websocket"` with correct source score

## End-to-End with Autonomy

- [ ] `curl /autonomy/news` shows news events with `provider: "alpaca"` flowing into the autonomy market map
- [ ] Market map `/autonomy/market-map` reflects asset mentions from Alpaca news
- [ ] Signal generation still works for crypto (news feed doesn't interfere with crypto signals)
