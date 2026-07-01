# Runtime Baseline Audit and Soak Checklist — 2026-07-01

This captures the local Docker Compose baseline before implementing the Trading Agent stabilization → paper-signoff canary plan.

## Commands Run

```bash
docker compose ps
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -fsS http://127.0.0.1:8081/health
curl -fsS http://127.0.0.1:8081/ready
curl -fsS http://127.0.0.1:8081/runtime/status
curl -fsS http://127.0.0.1:8081/runtime/heartbeats
curl -fsS 'http://127.0.0.1:8081/commands?status=pending'
curl -fsS 'http://127.0.0.1:8081/commands?status=failed'
curl -fsS http://127.0.0.1:8081/newswire/status
curl -fsS http://127.0.0.1:8081/world-model/status
curl -fsS http://127.0.0.1:8081/world-model/streams/status
docker compose logs --tail=300 world-model newswire | rg 'IntegrityError|RuntimeError|world_model_repository_unavailable|newswire_adapter_restart|adapter|error|warning|failed'
```

## Compose State

Default service-role stack is running locally:

| Service | Status | Host app port |
| --- | --- | --- |
| `api` | healthy | `127.0.0.1:8081->8080/tcp` |
| `newswire` | up | none |
| `world-model` | up | none |
| `trader` | up | none |
| `agent` | up | none |
| `postgres` | healthy | none |
| `vault` | healthy | `8200` only |

Legacy `bot` and `world-model-live` containers are not running. No app service publishes `8091`.

## API Health

- `/health`: `ok`
- `/ready`: `ready`
- `/ready.checks.service_role`: `api`
- `/ready.checks.world_model_repository`: `ok`
- `/ready.checks.worker_heartbeats`: `4`

## Runtime Heartbeats

- `worker_count`: `4`
- `stale_worker_count`: `0`
- `heartbeat_stale_seconds`: `90`
- Workers present:
  - `world_model`: running, not stale
  - `newswire`: running, not stale
  - `agent`: running, not stale
  - `trader`: running, not stale

## Command Queue Baseline

- Pending commands: `0`
- Failed commands: `0`

## Observed Issues to Fix

### World Model worker persistence

Worker heartbeat metadata reports recurring repository errors:

- `world_model.repository_available`: `false`
- `world_model.repository_last_error`: `RuntimeError`
- `world_model.repository_error_count`: `22`
- `pump.processed`: `5118`
- `pump.last_error`: `null`

Recent `world-model` logs include repeated persistence failures:

```text
world_model_repository_unavailable operation=persist_world_model_state error=RuntimeError
world_model_repository_unavailable operation=persist_world_model_state error=IntegrityError
```

This confirms step 4 is necessary: retry-safe/upsert-safe World Model repository writes plus better diagnostics.

### Newswire adapter errors

Newswire worker heartbeat metadata reports:

- `newswire.running`: `true`
- adapters: `rss`, `alpaca`
- `adapter_errors`: `3`

Recent `newswire` logs show Alpaca WebSocket connection-limit failures:

```text
newswire_adapter_restart adapter=alpaca error=RuntimeError detail=alpaca_news_error:connection limit exceeded
```

This confirms step 5 is necessary: per-adapter diagnostics and error classification.

### API status endpoints do not yet surface worker-owned state consistently

The current API `/newswire/status` and `/world-model/status` responses are based on API-local passive services and do not fully reflect worker heartbeat metadata. `/runtime/status` has the worker-owned truth. This should be addressed by the runtime dashboard/API work in step 3.

### Scheduler worker absent by default

`scheduler` is not currently running by default, while several API endpoints enqueue scheduler-targeted commands. This should be addressed in step 6 after real scheduler handlers are implemented.

## Soak Checklist

During stabilization, check every 30–60 minutes or after each change:

- [ ] `docker compose ps` shows `api`, `newswire`, `world-model`, `trader`, `agent`, `postgres` running.
- [ ] Only `api` publishes the app port `127.0.0.1:8081->8080`.
- [ ] No `bot` container is running.
- [ ] No `world-model-live` container is running.
- [ ] No host app port `8091` is listening.
- [ ] `/health` returns `ok`.
- [ ] `/ready` returns `ready`.
- [ ] `/runtime/status.stale_worker_count == 0`.
- [ ] `/commands?status=pending` has no indefinitely old commands.
- [ ] `/commands?status=failed` has only explained/triaged failures.
- [ ] World Model worker heartbeat has no growing repository error count.
- [ ] Newswire adapter errors are classified by adapter/source.
- [ ] `newswire_events` continue deduping by `event_id`.
- [ ] `consumer_offsets` advance after successful World Model processing.
- [ ] No live exchange flags are enabled.
- [ ] `VAULT_ENABLED=false` remains non-blocking.

## Baseline Acceptance Result

Passed:

- exactly one app host port (`api` on `127.0.0.1:8081`)
- no `8091`
- no running `bot` / `world-model-live`
- no stale workers
- no pending or failed commands

Failed/degraded but expected for follow-up steps:

- World Model worker reports repository persistence errors.
- Newswire Alpaca adapter reports connection-limit errors.
- API-local `/newswire/status` and `/world-model/status` are less authoritative than `/runtime/status`.
- Scheduler is absent by default while scheduler commands exist.
