---
created: 2026-07-01T00:26:26.525Z
source: pi-plan-mode
status: accepted-for-execution
---

# Newswire → Discord `#news` Redesign Plan

## Summary
Implement a redesigned Newswire Discord publisher that works from `RUNTIME_PROFILE=world_model_live`, posts Alpaca News WebSocket + RSS content to the known `#news` channel, uses Discord embeds, prevents restart/multi-process duplicate posts with a persistent publish ledger, avoids startup backlog blasts, and exposes health/metrics plus a protected admin test endpoint.

## Prior Art Discovered
- Existing Newswire pipeline already exists:
  - `app/newswire/service.py`: adapters → normalize → risk gate → bus → persistence.
  - `app/newswire/adapters/alpaca_ws.py`, `rss.py`, `trading_economics_ws.py`, `x_curated.py`.
  - `app/newswire/consumers/discord_news.py`: current plaintext Discord publisher.
  - `app/newswire/format.py`: current plaintext breaking/digest formatting.
  - `app/newswire/gateway.py`: `/newswire/events`, `/newswire/status`, `/newswire/stream`.
  - `tests/test_newswire.py`: covers pipeline and current Discord publisher behavior.
- Current blocker: `world_model_live` intentionally disables Discord and skips `newswire_discord.start()`.
- Current gap: dedupe is process-local, so RSS/backlog items can repost after restarts.
- Current docs already mention `NEWSWIRE_NEWS_CHANNEL_ID` and smoke testing.

## Implementation Steps
1. Add Newswire Discord config, docs, and Docker profile support.
2. Add a send-only Discord client for restricted runtimes.
3. Add Discord embed formatting for breaking news and digests.
4. Add a persistent Newswire Discord publish ledger.
5. Refactor `DiscordNewsPublisher` for embeds, ledger claims, fresh-only posting, buffering, and status.
6. Wire the publisher into `world_model_live` without enabling the full Discord trading bot.
7. Add protected admin test endpoint and observability.
8. Add unit/integration tests.
9. Deploy with Alpaca + RSS enabled and smoke test `#news`.

## Key Implementation Details

### Runtime/config
Add settings in `app/config.py`:

```env
NEWSWIRE_DISCORD_ENABLED=true
NEWSWIRE_NEWS_CHANNEL_ID=<known #news channel id>
NEWSWIRE_NEWS_MIN_IMPORTANCE=60
NEWSWIRE_BREAKING_MIN_IMPORTANCE=80
NEWSWIRE_DIGEST_INTERVAL_SECONDS=300
NEWSWIRE_SEND_MIN_INTERVAL_MS=1200
NEWSWIRE_DISCORD_DIGEST_MAX_ITEMS=10
NEWSWIRE_DISCORD_STARTUP_GRACE_SECONDS=300

ALPACA_NEWS_ENABLED=true
ALPACA_API_KEY=<secret>
ALPACA_API_SECRET=<secret>
ALPACA_NEWS_SYMBOLS=*

DISCORD_BOT_TOKEN=<bot token>
```

Update `docker-compose.yml` `world-model-live` profile:
- Remove the hard override `DISCORD_BOT_TOKEN: ""`.
- Keep trading/autonomy/engine disabled.
- Add/allow `NEWSWIRE_DISCORD_ENABLED=true`.

### Send-only Discord client
Create a lightweight send-only client, e.g. `app/discord_publish.py`.

Requirements:
- Uses `discord.py`.
- No `on_message` command handling.
- No runner/autonomy/tracking integration.
- Default intents only; no message-content intent needed.
- Methods:
  - `start()`
  - `stop()`
  - `wait_until_ready(timeout=30)`
  - `send(channel_id, content, embeds=None) -> message_id | None`

In full runtime, keep using the existing full `DiscordTradingBot` sink. In `world_model_live`, use the new send-only client.

### Embed formatting
Extend `app/newswire/format.py` with serializable embed payload helpers:
- `format_news_event_message(event)`
- `format_news_digest_message(events)`

Breaking embed:
- Title: headline.
- URL: event URL if present.
- Fields: source/provider, event type, asset class, symbols, score, sentiment.
- Description: concise body/enrichment summary.
- Footer: `News feed only — no trade was placed.`
- Color:
  - red for breaking,
  - orange for high importance,
  - blue/neutral for normal digest.

Digest embed:
- Title: `Newswire digest — N update(s)`.
- Include up to `NEWSWIRE_DISCORD_DIGEST_MAX_ITEMS=10` events per embed.
- If more than 10 buffered events, send multiple digest embeds, throttled.

### Persistent publish ledger
Add Alembic migration after current head:

`alembic/versions/0021_newswire_discord_publish_ledger.py`

Table: `newswire_publish_ledger`

Fields:
- `publish_id` primary key.
- `event_id`
- `destination` default `discord`
- `channel_id`
- `mode`: `breaking | digest`
- `status`: `pending | posted | failed | skipped`
- `discord_message_id`
- `attempt_count`
- `first_attempt_ms`
- `last_attempt_ms`
- `posted_at_ms`
- `last_error`
- `metadata_json`
- `created_at`

Unique constraint:
- `(destination, channel_id, event_id)`

Repository methods:
- `claim_newswire_publish(event_id, channel_id, mode, now_ms) -> bool`
- `mark_newswire_publish_posted(event_ids, channel_id, message_id, now_ms)`
- `mark_newswire_publish_failed(event_ids, channel_id, error, now_ms)`
- `newswire_publish_status(channel_id) -> dict`

Behavior:
- If already `posted`, skip.
- If `pending` and not stale, skip.
- If `failed` or stale `pending`, retry with incremented attempt count.

### Fresh-only/no backlog behavior
`DiscordNewsPublisher` records `started_at_ms`.

Skip Discord posting when:
- Event score is below `NEWSWIRE_NEWS_MIN_IMPORTANCE`.
- Event is stale.
- `published_at_ms` exists and is older than `started_at_ms - NEWSWIRE_DISCORD_STARTUP_GRACE_SECONDS`.

This still ingests/persists Newswire events, but prevents a startup RSS backlog blast into `#news`.

### Lifecycle wiring
In `main.py`:
- For `world_model_live`:
  - Start send-only Discord client if `NEWSWIRE_DISCORD_ENABLED`, `DISCORD_BOT_TOKEN`, and `NEWSWIRE_NEWS_CHANNEL_ID` are set.
  - Start `DiscordNewsPublisher`.
  - Start `AgentNewsConsumer`.
  - Start `NewswireService`.
- For `full` runtime:
  - Keep full Discord bot behavior.
  - Use existing bot sink for Newswire posts.
- For `dashboard_only`:
  - Keep Discord and Newswire workers disabled.

### Admin test endpoint
Add protected endpoint in `app/newswire/gateway.py`:

`POST /newswire/discord/test`

Auth:
- Existing agent API bearer auth.

Request:
```json
{
  "channel_id": "optional override",
  "dry_run": false
}
```

Response:
```json
{
  "sent": true,
  "channel_id": "...",
  "message_id": "...",
  "publisher": { "...status..." }
}
```

The test message must clearly say it is a test and include:
`News feed only — no trade was placed.`

### Observability
Expose publisher status in:
- `/health/config`
- `/newswire/status`

Include:
- enabled/running
- channel configured
- Discord ready
- buffered count
- last post timestamp
- last error
- ledger counts
- thresholds/cadence

Add/extend metrics:
- existing `NEWSWIRE_DISCORD_POSTS`
- add skipped counter by reason if needed:
  - `low_importance`
  - `stale`
  - `duplicate`
  - `discord_not_ready`
  - `send_error`

## Test Plan
- Unit tests for embed payload formatting.
- Unit tests for fresh-only filtering.
- Unit tests for publisher ledger claim/skip behavior.
- Unit tests for breaking immediate post and digest batching with fake sink.
- Repository tests for unique `(destination, channel_id, event_id)` behavior.
- Route test for `POST /newswire/discord/test`.
- Lifecycle/config test: `world_model_live` can expose Newswire Discord enabled without starting the full Discord trading bot.

## Deployment / Smoke Test
1. Run migrations.
2. Configure env for `world_model_live`.
3. Deploy `world-model-live`.
4. Check:
   - `/health/config`
   - `/newswire/status`
   - `/newswire/sources`
5. Call:
   - `POST /newswire/discord/test`
6. Confirm test embed appears in `#news`.
7. Confirm Alpaca adapter authenticates and RSS adapter runs.
8. Wait for live Newswire event:
   - breaking posts immediately,
   - normal events appear in digest every 5 minutes.
9. Monitor metrics/logs for send errors and duplicate skips.

## Assumptions
- The `#news` channel ID is known and will be supplied as `NEWSWIRE_NEWS_CHANNEL_ID`.
- The Discord bot already has `View Channel` and `Send Messages` permissions in `#news`.
- Alpaca credentials are available.
- Trading/autonomy/engine remain disabled in `world_model_live`; this change only permits news-only Discord publishing.













<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add Newswire Discord config, docs, and Docker profile support. _(done)_
- [x] 2. Add a send-only Discord client for restricted runtimes. _(done)_
- [x] 3. Add Discord embed formatting for breaking news and digests. _(done)_
- [x] 4. Add a persistent Newswire Discord publish ledger. _(done)_
- [x] 5. Refactor DiscordNewsPublisher for embeds, ledger claims, fresh-only posting, buffering, and status. _(done)_
- [x] 6. Wire the publisher into worldmodellive without enabling the full Discord trading bot. _(done)_
- [x] 7. Add protected admin test endpoint and observability. _(done)_
- [x] 8. Add unit/integration tests. _(done)_
- [x] 9. Deploy with Alpaca + RSS enabled and smoke test #news. _(done)_

<!-- pi-plan-progress:end -->
