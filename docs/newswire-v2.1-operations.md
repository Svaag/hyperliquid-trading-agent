# Newswire V2.1 Operations

Newswire V2.1 is the canonical product feed and the advisory news input to the Institutional Engine. It never grants execution authority. Paper and live activation remain controlled by the engine's independent readiness, risk, and human-signoff gates.

## Routing contract

Every canonical story has one versioned `assessment` with:

- `audience_scope`: `watched_asset`, `broad_market`, `unwatched_single_name`, or `general`.
- `feed_action`: `drop`, `watch`, `standard`, `high`, or `breaking`.
- `engine_action`: `ignore`, `ledger_only`, `risk_only`, `directional_feature`, or `macro_proxy`.
- component scores, reason codes, penalty codes, model-review state, and a stable decision ID.

Unwatched single-name equities are capped at `watch`, including official exchange halts. A trusted shock can bypass the numeric `breaking` threshold only when its audience is a watched asset or the broad market. NASDAQ halt RSS headlines receive source-scoped bare-ticker extraction; the same extraction is not applied to arbitrary prose.

Model review is optional and subordinate to deterministic scoring. Only fresh stories within three points of a 35/50/70/80 boundary are eligible. Startup backlog, stale stories, unwatched equities, replay, and reclassification are excluded. The reviewer has a bounded queue and makes one model call with no repair call. A review can move at most one tier and cannot manufacture `breaking`.

## Calibration

```bash
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" \
  'http://127.0.0.1:8081/newswire/calibration?limit=2000'
```

The report shows score distributions and action counts overall and by source, provider, event type, asset class, watch priority, and audience scope. It also reports candidate-threshold inclusion rates and explicitly checks that no unwatched single-name equity escaped its cap.

## Safe reclassification

Start with a dry run:

```bash
curl -X POST -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8081/newswire/reclassify \
  -d '{"symbols":["BTC","ETH"],"limit":500,"dry_run":true}'
```

Poll the returned `/commands/{command_id}`. The `newswire` worker returns per-story action/version deltas. Setting `dry_run=false` updates the canonical story assessment and records a new decision, but does not publish a story revision to the live bus and does not move any consumer offset.

## Newswire-to-Engine replay

Replay is owned by the `trader` worker and defaults to a dry-run 24-hour window:

```bash
curl -X POST -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8081/newswire/replay \
  -d '{"window_hours":24,"symbols":["BTC","ETH","HYPE"],"min_importance":50,"limit":1000,"dry_run":true}'
```

After reviewing the command result, repeat with `dry_run=false` to write replay-tagged `normalized_events` and `feature_values`. Replay:

- preserves original publication and receive timestamps;
- adds `replay=true`, `replay_run_id`, and `execution_authority=none` metadata;
- uses distinct normalized-event IDs;
- derives through isolated replay ledger/feature-store facades, not the trader's live in-memory state;
- does not update `trader:engine_newswire`;
- does not update the current news-risk state;
- does not invoke strategies or create order intents/execution reports.

The command result includes scanned/matched/normalized/recorded counts, feature count, skip reasons, sample event IDs, and live offsets before and after the run.

## Runtime health and alerts

Machine-readable views:

- `/newswire/status` includes Engine Newsfeed runtime, durable offset, and health.
- `/engine/status` includes `newsfeed_health`.
- `/runtime/dashboard/data` includes a render-ready `newsfeed` object.
- `/runtime/dashboard` has an Engine Newsfeed panel.

The health object reports offset age, pump and consumer errors, invalid rows, processed/received/recorded/feature counts, and skip reasons. It becomes degraded when the consumer is not running, an active feed lacks an offset, the offset is stale, or pump/consumer errors occur. The trader-owned validation monitor emits deduplicated operational-outbox alerts for these conditions.

## Continuous soak evidence

```bash
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" \
  http://127.0.0.1:8081/newswire/readiness
```

Readiness requires at least 24 continuous hours by default. Its clock starts at the later of the current Newswire-worker and trader-worker start times, so either worker restarting resets the continuous window. A pass also requires:

- a healthy, advancing durable offset;
- ingested rows and consumed story revisions;
- normalized Newswire events and news feature rows;
- zero invalid rows and pump/consumer errors;
- zero paper/live order intents and execution reports during the window.

Until every condition passes, paper-signoff preflight exposes Newswire evidence as `advisory_until_soak_passes`. The endpoint deliberately cannot manufacture or backdate the required elapsed time.
