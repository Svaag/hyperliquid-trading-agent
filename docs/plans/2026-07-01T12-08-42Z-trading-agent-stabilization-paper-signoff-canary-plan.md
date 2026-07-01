---
created: 2026-07-01T12:08:42.923Z
source: pi-plan-mode
status: accepted-for-execution
---

# Trading Agent Stabilization → Paper-Signoff Canary Plan

## Summary

Plan the next roadmap as a **full local-first path through paper-signoff canary**, with these locked decisions:

- Target environment first: **local Docker Compose**.
- Runtime source posture: **keep current external sources enabled**, but add diagnostics and classifications.
- Command coverage approach: **audit/registry first, then implement real handlers**.
- Operator UX: **JSON API endpoints + simple `/runtime/dashboard` HTML page**.
- Vault: **defer; keep `VAULT_ENABLED=false`**.
- Canary mode: **paper-signoff immediately** for `BTC,ETH,HYPE`; no live exchange keys/actions.

## Grounded Current State

- Repo is clean on `main`.
- Latest commits:
  - `7e6018f Add miscellaneous session artifacts`
  - `d7f8e60 Refactor runtime into service-role workers`
- Local Compose is running the intended service-role shape:
  - `api` healthy on `127.0.0.1:8081->8080`
  - `newswire`, `world-model`, `trader`, `agent` running with no host app ports
  - legacy `bot` / `world-model-live` containers removed
- Current command-handler state:
  - Real handlers exist for:
    - `agent`: `ask`, `trade_proposal`
    - `world_model`: `world_model_adapter_poll`, `world_model_dev_seed`
    - `discord_publisher`: `discord_test`
  - Placeholder/no-op handlers currently exist for many `trader` and `scheduler` commands.
- Known runtime issues to address before deeper trading complexity:
  - `world_model` previously reported repository `IntegrityError`.
  - `newswire` reported adapter errors.
  - Scheduler-targeted commands exist, but `scheduler` is not currently a default Compose service.

## Scope

In scope:

- Service-role runtime stabilization.
- Runtime diagnostics and command operator UX.
- Command registry and full command-handler coverage.
- Newswire → World Model correctness and restart/resume testing.
- Shadow/paper-readiness validation.
- Autonomy/HIP4 hardening around command intent and auditability.
- Local paper-signoff canary for `BTC,ETH,HYPE`.

Out of scope for this plan:

- Live exchange execution.
- Re-enabling Vault as a required dependency.
- Removing legacy aliases before soak has passed.
- Frontend framework migration; dashboards stay simple FastAPI-served HTML/JS.

## Implementation Steps

1. Establish a baseline runtime audit and soak checklist.
2. Add a central command registry and coverage tests.
3. Add runtime operator APIs and `/runtime/dashboard`.
4. Fix World Model persistence `IntegrityError` risk with retry-safe/upsert-safe repository writes.
5. Improve Newswire adapter diagnostics and persisted-event correctness.
6. Implement real scheduler worker handlers and start `scheduler` by default.
7. Implement real trader worker handlers for engine, HIP4, autonomy, and tracking commands.
8. Harden command idempotency, retries, cancellation, and audit logging.
9. Add Newswire → World Model restart/resume and offset correctness tests.
10. Validate shadow trading readiness and engine evidence quality.
11. Harden autonomy/HIP4 command-intent boundaries and direct-mutation exceptions.
12. Run the local paper-signoff canary for `BTC,ETH,HYPE`.
13. Update docs/runbooks and define post-soak legacy-alias removal criteria.

## Detailed Implementation Design

### 1. Baseline Runtime Audit

Before changing code, capture the local runtime state in the implementation notes:

- `docker compose ps`
- `/health`
- `/ready`
- `/runtime/status`
- `/runtime/heartbeats`
- `/commands?status=pending`
- `/commands?status=failed`
- `/newswire/status`
- `/world-model/status`
- `/world-model/streams/status`

Record:

- active workers
- stale heartbeat count
- pending/failed command count
- World Model repository status
- Newswire adapter statuses
- current exposed ports

Acceptance baseline:

- exactly one app host port: `127.0.0.1:8081->8080`
- no `8091`
- no `bot` / `world-model-live` running
- no stale workers before implementation starts

### 2. Command Registry

Add a central command registry, for example:

```text
hyperliquid_trading_agent/app/runtime_commands.py
```

Define a `WorkerCommandSpec` with:

- `command_type`
- `target_role`
- `payload_model`
- `source_endpoints`
- `description`
- `paper_state_mutation`
- `external_side_effect`
- `idempotency_key_fields`
- `handler_required`
- `handler_status`

Registry must include at minimum:

| Command | Role | Required handler |
| --- | --- | --- |
| `ask` | `agent` | `AgentWorker._handle_ask` |
| `trade_proposal` | `agent` | `AgentWorker._handle_trade_proposal` |
| `world_model_adapter_poll` | `world_model` | `WorldModelWorker._handle_adapter_poll` |
| `world_model_dev_seed` | `world_model` | `WorldModelWorker._handle_dev_seed` |
| `discord_test` | `discord_publisher` | `DiscordPublisherWorker._handle_discord_test` |
| `engine_strategy_regime_refresh` | `trader` | real implementation |
| `engine_bandit_run` | `trader` | real implementation |
| `engine_replay_comparison_run` | `trader` | real implementation |
| `hip4_loop_run_once` | `trader` | real implementation |
| `hip4_scan_run` | `trader` | real implementation |
| `hip4_paper_execute` | `trader` | real implementation |
| `hip4_reconcile_run` | `trader` | real implementation |
| `autonomy_pause` | `trader` | real implementation |
| `autonomy_resume` | `trader` | real implementation |
| `autonomy_signal_approve` | `trader` | real implementation |
| `autonomy_signal_reject` | `trader` | real implementation |
| `autonomy_signal_expire` | `trader` | real implementation |
| `autonomy_equity_signal_approve` | `trader` | real implementation |
| `autonomy_equity_signal_reject` | `trader` | real implementation |
| `tracking_pause` | `trader` | new command |
| `tracking_resume` | `trader` | new command |
| `tracking_stop` | `trader` | new command |
| `orchestration_wave_run_once` | `scheduler` | real implementation |
| `autonomy_evaluations_run` | `scheduler` | real implementation |
| `autonomy_evaluations_backfill` | `scheduler` | real implementation or explicit unsupported result |
| `autonomy_event_evaluations_backfill` | `scheduler` | real implementation or explicit unsupported result |
| `autonomy_daily_report_run` | `scheduler` | real implementation |
| `autonomy_weekly_report_run` | `scheduler` | real implementation |

Tests must assert:

- every API `enqueue_worker_command(...)` command string is in the registry
- every registry command has a worker handler
- no worker handler exists outside the registry unless explicitly marked internal
- no registered command is still handled by `_accepted_noop`

### 3. Runtime Operator APIs and Dashboard

Add API endpoints:

```http
GET  /runtime/dashboard
GET  /runtime/dashboard/data
GET  /runtime/command-registry
GET  /runtime/command-health
GET  /runtime/offsets
GET  /runtime/offsets/{consumer_name}
POST /commands/{command_id}/retry
POST /commands/{command_id}/cancel
```

Behavior:

- `/runtime/dashboard` is simple FastAPI-served HTML, same style as existing dashboards.
- Dashboard sections:
  - worker heartbeats
  - stale workers
  - command counts by role/status/type
  - latest failed commands and errors
  - pending/claimed command age
  - consumer offsets
  - command registry coverage
  - Newswire adapter status summary
  - World Model repository status summary
- `/commands/{id}/retry` creates a new command row with:
  - same `target_role`
  - same `command_type`
  - same payload
  - metadata: `{"retry_of": "<old_id>"}`
  - new idempotency key: `retry:<old_id>:<attempt_number>`
- `/commands/{id}/cancel` marks only `pending` commands as `cancelled`.
- Claimed commands are not force-killed; dashboard marks them stale once claim age exceeds `WORKER_COMMAND_CLAIM_STALE_SECONDS`.

Accepted command responses should include:

```json
{
  "accepted": true,
  "command_id": "...",
  "status_url": "/commands/...",
  "target_role": "...",
  "command_type": "...",
  "status": "pending",
  "target_worker_state": "running|stale|missing",
  "target_worker_warning": null
}
```

### 4. World Model Persistence Fix

Observed risk: World Model repository `IntegrityError`, likely from concurrent insert/update races between stream ingestion and Newswire pump persistence.

Implement repository-safe upserts for World Model tables:

- `world_events`
- `market_beliefs`
- `narrative_clusters`
- `prediction_market_signals`
- `source_credibility`
- `world_memory_atoms`
- `world_model_snapshots`
- `prediction_market_calibrations`

Implementation rule:

- For PostgreSQL, use SQLAlchemy PostgreSQL `insert(...).on_conflict_do_update(...)`.
- For SQLite tests, keep current `session.get` fallback path.
- If an `IntegrityError` still occurs, rollback once and retry as update.
- Duplicate/upsert races must not put `WorldModelService` into global repository cooldown after a successful retry.

Improve diagnostics:

- Store/report:
  - operation name
  - table/model name
  - exception type
  - constraint name when available
  - last error timestamp
  - error count by operation
- Include this in `WorldModelService.status()` and heartbeat metadata.

Acceptance:

- No repeated `IntegrityError` in World Model heartbeat metadata during a 30-minute local soak.
- `repository_available=true` after cooldown expires.
- World Model continues processing Newswire events and Polymarket stream updates.

### 5. Newswire Diagnostics and Correctness

Keep current external sources enabled.

Add per-adapter status fields:

```json
{
  "name": "alpaca|rss|trading_economics|x",
  "running": true,
  "authenticated": true,
  "last_success_at_ms": 0,
  "last_error_at_ms": 0,
  "last_error_type": null,
  "last_error_detail": null,
  "error_count": 0,
  "reconnect_count": 0,
  "error_class": "none|transient_network|auth_config|parse|rate_limit|unknown"
}
```

Rules:

- Auth/config errors are `degraded`.
- Transient network reconnects are warnings, not fatal.
- RSS parse errors are per-feed and must not restart the whole Newswire service.
- `record_newswire_event` remains idempotent by `event_id`.
- Duplicate created events do not advance consumer offsets twice.

Acceptance:

- `/newswire/status` identifies which adapter is failing.
- Adapter errors are classified, not just counted.
- RSS and Alpaca can fail independently without stopping the other.
- `newswire_events` remains deduped by `event_id`.

### 6. Scheduler Worker Real Handlers

Make `scheduler` a default no-port Compose service after real handlers exist.

Compose behavior:

```yaml
scheduler:
  SERVICE_ROLE: scheduler
  AUTONOMY_ENABLED: "${SCHEDULER_AUTONOMY_ENABLED:-false}"
  ENGINE_ENABLED: "false"
  HIP4_ENABLED: "false"
  NEWSWIRE_ENABLED: "false"
  DISCORD_BOT_ENABLED: "false"
  DISCORD_PUBLISHER_ENABLED: "false"
```

Implement handlers:

- `orchestration_wave_run_once`
  - call `WaveSupervisor.run_once(WaveSupervisorRunOptions(...))`
- `autonomy_evaluations_run`
  - load open signal evaluations
  - call `SignalEvaluationService.mark_due()`
  - call `SignalEvaluationService.expire_overdue_signals()`
  - load open alpha-event evaluations
  - call `AlphaEventEvaluationService.mark_due()`
  - call `AlphaEventEvaluationService.expire_overdue_events()`
  - return counts and mark IDs
- `autonomy_evaluations_backfill`
  - explicitly create missing evaluations for persisted signals that have no evaluation rows, bounded by limit
  - return created count
- `autonomy_event_evaluations_backfill`
  - create missing alpha-event evaluations for eligible persisted Newswire/autonomy news events, bounded by limit
  - return created count
- `autonomy_daily_report_run`
  - call `AutonomyReportService.generate_daily(post=false)`
- `autonomy_weekly_report_run`
  - call `AutonomyReportService.generate_weekly(post=false)`

### 7. Trader Worker Real Handlers

Add a shared runtime factory to avoid duplicating FastAPI lifespan setup:

```text
hyperliquid_trading_agent/app/runtime_context.py
```

Factory responsibilities:

- build repository/session
- build Hyperliquid client
- build World Model service
- build Engine service
- build HIP4 service
- build Autonomy service
- build evaluation/report/memory/tuning services
- expose cleanup/close hooks

Implement trader handlers:

#### Engine

- `engine_strategy_regime_refresh`
  - payload: `window_hours`
  - call `refresh_strategy_regime_performance(repository, window_start_ms, window_end_ms)`
  - return row count and `report_only=true`

- `engine_bandit_run`
  - payload: `window_hours`
  - call `OfflineContextualBanditReporter(repository).run(...)`
  - return policy/recommendation summary
  - always `auto_apply_allowed=false`

- `engine_replay_comparison_run`
  - payload: `window_hours`, `universe`, `baseline_config`, `candidate_config`, `variant_id`
  - call `EngineReplayComparisonService.compare_variant(...)`
  - return replay artifact IDs/status

#### HIP4

- `hip4_loop_run_once`
  - add public `Hip4Service.run_proactive_once()` if needed
  - perform one bounded scan/reconcile cycle
  - only paper-execute if existing HIP4 proactive paper flags allow it

- `hip4_scan_run`
  - call `Hip4Service.run_scan(send_digest=true)`

- `hip4_paper_execute`
  - call `Hip4Service.execute_paper_candidate(candidate_id)`
  - enforce existing risk/capability/mode guards

- `hip4_reconcile_run`
  - call `Hip4Service.reconcile_paper()`

#### Autonomy

- `autonomy_pause`
  - call `AutonomousTradingLoopService.pause(actor)`

- `autonomy_resume`
  - call `AutonomousTradingLoopService.resume(actor)`

- `autonomy_signal_approve`
  - call `approve_signal(signal_id, actor)`
  - idempotent result if already paper ordered

- `autonomy_signal_reject`
  - call `reject_signal(signal_id, actor, reason)`

- `autonomy_signal_expire`
  - call `expire_signal(signal_id, actor)`

- `autonomy_equity_signal_approve`
  - call `approve_equity_signal(signal_id, actor)`

- `autonomy_equity_signal_reject`
  - call `reject_equity_signal(signal_id, actor, reason)`

#### Tracking

Convert tracking status changes to commands:

- `tracking_pause`
- `tracking_resume`
- `tracking_stop`

API endpoints:

```http
POST /tracking/positions/{tracker_id}/pause
POST /tracking/positions/{tracker_id}/resume
POST /tracking/positions/{tracker_id}/stop
```

must enqueue commands instead of mutating tracking state directly.

### 8. Command Idempotency and Audit

Add idempotency keys for high-risk commands:

| Command | Idempotency key |
| --- | --- |
| `autonomy_signal_approve` | `autonomy_signal_approve:{signal_id}` |
| `autonomy_signal_reject` | `autonomy_signal_reject:{signal_id}` |
| `autonomy_signal_expire` | `autonomy_signal_expire:{signal_id}` |
| `autonomy_equity_signal_approve` | `autonomy_equity_signal_approve:{signal_id}` |
| `autonomy_equity_signal_reject` | `autonomy_equity_signal_reject:{signal_id}` |
| `hip4_paper_execute` | `hip4_paper_execute:{candidate_id}` |
| `tracking_pause/resume/stop` | `tracking:{action}:{tracker_id}` |
| `discord_test` | no idempotency by default |
| report/evaluation runs | no idempotency by default |

Record audit events for:

- command enqueued
- command claimed
- command completed
- command failed
- command cancelled
- command retried

Audit payload must include:

- command ID
- command type
- target role
- actor/requested_by
- idempotency key
- result status
- `exchange_actions: []`

### 9. Newswire → World Model Correctness Tests

Add tests for:

- `newswire_events` duplicate created event does not create duplicate rows.
- updated/removed events update the existing row.
- `StoredNewswirePump` advances `consumer_offsets` only after successful callback.
- callback failure does not advance offset.
- pump restart resumes after last event ID/timestamp.
- equal-timestamp event ordering uses `(received_at_ms, event_id)`.
- World Model consumes Newswire events from DB, not in-process API state.
- only one Newswire worker owns external adapters.
- only one World Model worker owns prediction streams.

### 10. Shadow Trading Readiness Validation

Before canary, validate:

- engine candidates are being generated or explainably absent
- EV estimates exist
- allocation decisions exist
- risk rejects are persisted
- replay comparison can run
- strategy regime performance refresh works
- bandit report writes recommendations but cannot auto-apply
- `/dashboard/data` readiness report loads
- no live execution flags are enabled

Hard blocks:

- `HYPERLIQUID_EXCHANGE_ENABLED=true`
- `ENGINE_LIVE_ENABLED=true`
- any API service with side-effect flags enabled
- stale `trader`, `agent`, `newswire`, or `world_model` worker
- failed migration
- unresolved World Model repository error growth

### 11. Autonomy/HIP4 Boundary Hardening

Audit all POST endpoints.

Categories:

1. **Command-intent required**
   - autonomy approvals/rejections/expiry
   - HIP4 scan/paper/reconcile
   - tracking pause/resume/stop
   - engine run/refresh/replay/bandit
   - orchestration wave run
   - report/evaluation runs

2. **Direct DB/audit-safe allowed**
   - governance proposal review
   - memory lesson archive
   - memory candidate promotion only if it cannot trigger execution
   - tuning proposal mark/reject/expire because `auto_apply_enabled=false`
   - World Model annotations/outcomes

For every allowed direct mutation, add or verify:

- auth requirement
- audit event
- `exchange_actions: []`
- no worker/session/network side effects
- tests proving no command worker is needed

### 12. Local Paper-Signoff Canary

Canary starts only after previous acceptance criteria pass.

Use role-scoped Compose env mapping so `api` remains passive.

Recommended local canary env:

```env
VAULT_ENABLED=false

TRADER_AUTONOMY_ENABLED=true
TRADER_ENGINE_ENABLED=true
TRADER_HIP4_ENABLED=false

SCHEDULER_AUTONOMY_ENABLED=true

AUTONOMY_MODE=paper_signoff
AUTONOMY_REQUIRE_HUMAN_SIGNOFF=true
AUTONOMY_CORE_UNIVERSE=BTC,ETH,HYPE
AUTONOMY_UNIVERSE_TOP_N_PERPS=3
AUTONOMY_MAX_TRACKED_ASSETS=3
AUTONOMY_MAX_HOT_L2_ASSETS=3
AUTONOMY_MAX_SIGNALS_PER_DAY=3
AUTONOMY_MIN_SIGNAL_SCORE=75
AUTONOMY_PAPER_INITIAL_EQUITY_USD=100000
AUTONOMY_PAPER_RISK_PCT_PER_TRADE=0.25

ENGINE_EXECUTION_MODES=shadow
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_LIVE_ENABLED=false
ENGINE_READINESS_ENABLED=true

HYPERLIQUID_EXCHANGE_ENABLED=false
```

Canary behavior:

- Autonomy may generate signals.
- Human approval creates paper orders/fills/positions only.
- Engine remains shadow-only.
- HIP4 remains disabled unless separately testing HIP4 paper commands.
- No live order signing.
- No exchange mutation.

Canary monitoring:

- `/runtime/status`
- `/runtime/dashboard`
- `/commands?status=failed`
- `/autonomy/status`
- `/autonomy/signals`
- `/autonomy/portfolio`
- `/engine/status`
- `/engine/validation-report`
- `/dashboard/data`

Canary success after 48h:

- zero stale required workers
- zero live exchange actions
- zero unclassified command failures
- paper orders only after human approval
- paper portfolio state consistent
- no duplicate Newswire/World Model ingestion
- World Model repository remains available
- command dashboard shows no stuck claimed commands older than claim stale window

## Test Plan

Run after implementation:

```bash
uv run ruff check hyperliquid_trading_agent tests
uv run python -m compileall -q hyperliquid_trading_agent/app
VAULT_ENABLED=false ... uv run pytest -q
VAULT_ENABLED=false docker compose config >/tmp/compose.yml
```

Static Compose assertions:

- only `api` publishes an app port
- no `8091`
- `scheduler` has no host port
- `NEWSWIRE_ENABLED=true` only on `newswire`
- `SERVICE_ROLE` set for every app service

Focused tests to add:

- `tests/test_runtime_command_registry.py`
- `tests/test_runtime_dashboard.py`
- `tests/test_worker_command_handlers.py`
- `tests/test_world_model_repository_upserts.py`
- `tests/test_newswire_world_model_offsets.py`
- `tests/test_paper_canary_config.py`

## Rollout Plan

1. Implement registry, tests, and runtime dashboard.
2. Implement persistence and adapter diagnostics.
3. Implement scheduler/trader real handlers.
4. Rebuild local Compose.
5. Run local soak with current sources.
6. Fix any command/runtime failures.
7. Enable local paper-signoff canary config.
8. Run 48h local paper canary.
9. Only after successful canary, plan Hyrule/prod rollout.
10. Only after a successful production-like soak, remove or further neuter legacy aliases.

## Acceptance Criteria

The plan is complete when:

- all registered commands have real handlers or are explicitly documented as unsupported with tests
- no command endpoint silently queues to a permanently absent worker
- `/runtime/dashboard` shows commands, workers, offsets, and failures
- World Model no longer reports recurring repository `IntegrityError`
- Newswire adapter errors are classified by adapter/source
- Newswire offsets resume correctly after restart
- `scheduler` runs by default with no host port
- `trader` commands perform real paper/shadow-safe actions
- API remains passive
- only `api` exposes a host app port
- Vault remains disabled and non-blocking
- local `BTC,ETH,HYPE` paper-signoff canary runs without live execution

## Assumptions

- Local Compose remains the first validation environment.
- Existing model/API provider keys may be used for analysis, but no exchange signing keys are used.
- `HYPERLIQUID_EXCHANGE_ENABLED=true` remains rejected.
- Paper-signoff means paper orders/fills/positions are allowed only after explicit human/operator approval.
- Vault remediation is deferred to a later production-hardening plan.













<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Establish a baseline runtime audit and soak checklist. _(done)_
- [x] 2. Add a central command registry and coverage tests. _(done)_
- [x] 3. Add runtime operator APIs and /runtime/dashboard. _(done)_
- [x] 4. Fix World Model persistence IntegrityError risk with retry-safe/upsert-safe repository writes. _(done)_
- [x] 5. Improve Newswire adapter diagnostics and persisted-event correctness. _(done)_
- [x] 6. Implement real scheduler worker handlers and start scheduler by default. _(done)_
- [x] 7. Implement real trader worker handlers for engine, HIP4, autonomy, and tracking commands. _(done)_
- [x] 8. Harden command idempotency, retries, cancellation, and audit logging. _(done)_
- [x] 9. Add Newswire → World Model restart/resume and offset correctness tests. _(done)_
- [x] 10. Validate shadow trading readiness and engine evidence quality. _(done)_
- [x] 11. Harden autonomy/HIP4 command-intent boundaries and direct-mutation exceptions. _(done)_
- [x] 12. Run the local paper-signoff canary for BTC,ETH,HYPE. _(done)_
- [x] 13. Update docs/runbooks and define post-soak legacy-alias removal criteria. _(done)_

<!-- pi-plan-progress:end -->
