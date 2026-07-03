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
- [ ] `ENGINE_NEWS_MIN_IMPORTANCE` is set to the desired Institutional Engine gate (default `35`)
- [ ] `NEWSWIRE_NEWS_MIN_IMPORTANCE` is set to the desired Discord #news gate (default `60`; lower values can make the feed noisy)

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
- [ ] `curl http://127.0.0.1:8081/newswire/events?source=alpaca&limit=5` returns Alpaca-sourced events with populated fields:
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
- [ ] `curl http://127.0.0.1:8081/newswire/status` shows one latest event stream, not duplicate worker streams
- [ ] No duplicate headlines in `http://127.0.0.1:8081/newswire/events?limit=50` (same `external_id` not appearing twice)

## Discord Delivery

- [ ] `POST /newswire/discord/test` returns an accepted command for `discord_publisher`; poll the returned `/commands/{id}` until completed
- [ ] Breaking news (urgency=breaking or score >= NEWSWIRE_BREAKING_MIN_IMPORTANCE) appears immediately in the #news channel
- [ ] Non-breaking news appears in periodic digest posts (every NEWSWIRE_DIGEST_INTERVAL_SECONDS)
- [ ] Messages render as a single Discord embed without a duplicated plaintext digest
- [ ] Headlines render decoded punctuation such as apostrophes instead of HTML entities like `&#39;`
- [ ] Halt-state events (`⛔ Halt state`) show the halt warning when applicable

## Institutional Engine Delivery

The `trader` worker consumes persisted Newswire rows through `trader:engine_newswire`; it does not own Alpaca/RSS/X/TradingEconomics connections.

- [ ] `curl http://127.0.0.1:8081/runtime/offsets` includes `trader:engine_newswire`
- [ ] `curl http://127.0.0.1:8081/runtime/heartbeats?service_role=trader` shows `metadata.engine_newsfeed.pump.running=true`
- [ ] Fresh events with `importance_score >= ENGINE_NEWS_MIN_IMPORTANCE` and symbols in the engine core universe (`BTC,ETH,HYPE` by default) appear in `/engine/events?event_type=newswire`
- [ ] Macro events without symbols require `importance_score >= ENGINE_NEWS_MACRO_MIN_IMPORTANCE` to proxy into core symbols
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

## Threshold Reference

- Discord #news: `NEWSWIRE_NEWS_MIN_IMPORTANCE`; breaking/immediate posts use `NEWSWIRE_BREAKING_MIN_IMPORTANCE`, otherwise qualifying items roll into `NEWSWIRE_DIGEST_INTERVAL_SECONDS` digests.
- Institutional Engine: `ENGINE_NEWS_MIN_IMPORTANCE` for event admission, `ENGINE_NEWS_MIN_SOURCE_SCORE` for feature derivation, `ENGINE_NEWS_MACRO_MIN_IMPORTANCE` for macro proxying, and `ENGINE_NEWS_CATALYST_THRESHOLD` for regime `news_state=catalyst`.

## End-to-End with Autonomy

- [ ] `curl http://127.0.0.1:8081/autonomy/news` shows news events with `provider: "alpaca"` flowing into the autonomy market map when the `trader` worker has autonomy enabled
- [ ] Market map `/autonomy/market-map` reflects asset mentions from Alpaca news
- [ ] Signal generation still works for crypto (news feed doesn't interfere with crypto signals)
