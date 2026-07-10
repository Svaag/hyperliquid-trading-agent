# Optional Newswire Adapter Contract

This document records the provider contract and live-smoke boundary for the optional
Trading Economics, curated X, GlobeNewswire, Business Wire, and ECB sources. These
adapters only emit `RawNewsItem`; canonical clustering, V2.1 assessment, Discord
routing, and engine consumption are unchanged.

## Trading Economics calendar stream

The adapter follows the provider's
[Economic Calendar Streaming](https://docs.tradingeconomics.com/economic_calendar/streaming/)
contract: connect to `wss://stream.tradingeconomics.com/?client=<key:secret>` and send
`{"topic":"subscribe","to":"calendar"}`.

| Provider field | Canonical use |
| --- | --- |
| `calendarId` / `CalendarId` | stable `external_id` used for update correlation |
| `date` | `published_at_ms` |
| `country` + `event` | headline prefix |
| `actual`, `forecast`, `forecast `, `teforecast`, `previous`, `revised` | release surprise/revision headline |
| `category`, `importance`, `reference`, `ticker`, `unit` | explanatory body/raw provenance |

The provider's streaming example contains a historical `forecast ` key with trailing
whitespace and a separate `teforecast`; the parser deliberately accepts all three
spellings. Repeated identical calendar messages are dropped. A changed payload with the
same calendar ID is emitted as `updated`. Calendar streams have no delete contract.
Credentials are redacted from supervisor error details.

Fixture: `tests/fixtures/newswire/trading_economics_calendar.json`.

## Curated X recent search

The query is one parenthesized OR expression followed by `-is:retweet`, so the exclusion
applies to both `from:username` and `$CASHTAG` branches. Usernames are normalized by
removing a leading `@` and rejecting values outside X's username character contract.
Queries are built atom-by-atom and never cut mid-expression when enforcing the 512-byte
self-serve limit. See X's official
[Search Operators](https://docs.x.com/x-api/posts/search/integrate/operators) reference.

`includes.users` is joined to each post so canonical items retain the username rather
than only an opaque author ID. `edit_history_tweet_ids[0]` is the stable external ID;
later post IDs become `updated` revisions during the adapter lifetime. Recent Search
does not deliver delete events. Stored X content can only be kept deletion-current by
adding an entitled
[X compliance stream](https://docs.x.com/x-api/stream/stream-posts-compliance-data),
which is intentionally not implied by `X_NEWSWIRE_ENABLED`.

Fixture: `tests/fixtures/newswire/x_recent_search.json`.

## RSS sources

Each RSS URL is fetched independently and exposes redacted per-feed success/error
telemetry under `/newswire/status -> adapters[rss].feed_health`. One failed feed no
longer restarts or suppresses healthy feeds. If every configured feed fails, the RSS
adapter raises a provider-neutral error so the existing supervisor reconnect/backoff
metrics still fire. Query strings and URL credentials are never included in health
keys or persisted raw feed metadata.

- ECB's official [RSS page](https://www.ecb.europa.eu/home/html/rss.en.html) publishes a
  stable press feed at `https://www.ecb.europa.eu/rss/press.html`; it is enabled by
  default as source `ecb`, event type `macro`.
- GlobeNewswire's official [RSS list](https://www.globenewswire.com/rss/list) publishes
  stable subject feeds. The M&A subject feed is enabled by default as source
  `globe_newswire`, event type `press_release`; using the selected subject avoids the
  noise of the entire public-company firehose.
- Business Wire documents [custom RSS/Atom feed options](https://www.businesswire.com/help/feed-options)
  but does not publish a stable account-independent URL suitable for a repository
  default. A licensed/custom URL can be appended to `NEWSWIRE_RSS_FEEDS` and maps to
  `business_wire`, event type `press_release`.

Redacted parser fixtures live under `tests/fixtures/newswire/` for all three shapes.

## Live smoke evidence — 2026-07-10 UTC

| Adapter | Result | Evidence / limitation |
| --- | --- | --- |
| ECB RSS | pass | HTTP 200; three items parsed in a bounded read-only fetch |
| GlobeNewswire M&A RSS | pass | HTTP 200; three items parsed in a bounded read-only fetch |
| Business Wire RSS | provider-limited | custom/licensed feed URL required; parser fixture and source mapping pass |
| Trading Economics | provider-limited | no credential configured; documented guest REST checks returned HTTP 410, so fixture mapping is validated but a credentialed live calendar release remains opt-in |
| Curated X | provider-limited | no bearer token or usernames configured; query/edit regression tests pass, while delete compliance requires a separate entitled stream |

No payload values, bearer tokens, provider keys, or connection URLs containing
credentials were captured in this evidence.

## Regression and operator checks

```bash
uv run pytest -q tests/test_newswire_optional_adapters.py
curl -H "Authorization: Bearer ${AGENT_API_BEARER_TOKEN}" \
  http://127.0.0.1:8081/newswire/status | jq '.adapters'
```

Enabling or disabling one optional adapter must not alter the canonical story revision
schema, Discord outbox policy, engine consumer offset, or execution authority.
