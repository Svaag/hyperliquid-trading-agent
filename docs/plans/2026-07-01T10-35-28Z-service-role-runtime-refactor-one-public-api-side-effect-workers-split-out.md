---
created: 2026-07-01T10:35:28.902Z
source: pi-plan-mode
status: accepted-for-execution
---

# Service-Role Runtime Refactor: One Public API, Side-Effect Workers Split Out

## Summary

Refactor the app so `RUNTIME_PROFILE` no longer decides process behavior. Introduce `SERVICE_ROLE` as the only process-role switch.

Final invariant:

> Exactly one public HTTP service exists: `api`. Workers expose no ports, perform one role each, and external feeds are connected once.

Chosen decisions:
- Use **Postgres-only** first. No Redis in this refactor.
- Public API may keep safe authenticated DB writes, but must not directly start workers, connect feeds, publish Discord, call LLMs, run trading loops, or place orders.
- External-action endpoints become **command intents** stored in DB and executed by workers.
- Add an `agent` worker for `/ask` and trade-proposal LLM work.
- Keep deprecated Compose aliases briefly, but make them fail-safe/no-duplicate-port.

## Implementation Steps

1. Add service-role configuration and hard validation.
2. Add runtime infrastructure tables and repository helpers.
3. Split FastAPI into a passive `api` process.
4. Add non-web worker entrypoint and role workers.
5. Convert external-action API endpoints to command intents.
6. Refactor Newswire/World Model/Discord flows onto persisted events and offsets.
7. Refactor Docker Compose so only `api` exposes a port.
8. Update docs, examples, and deployment runbooks.
9. Add tests and verification coverage.
10. Roll out with stop-bleed compatibility aliases and monitor heartbeats.

## Current Repo Facts

- `docker-compose.yml` currently has:
  - `bot` exposing `${HOST_PORT:-8080}:8080`
  - `world-model-live` exposing `${WORLD_MODEL_LIVE_HOST_PORT:-8091}:8080`
- Both run the same FastAPI app and same DB.
- `hyperliquid_trading_agent/app/main.py` currently creates FastAPI routes **and** starts background loops:
  - Discord bot
  - Newswire
  - World Model streams
  - HIP4
  - autonomy
  - engine monitor / PnL attribution
  - liquidation service
- Existing persisted primitives:
  - `newswire_events`
  - `newswire_publish_ledger`
  - world model tables/snapshots
- Existing `newswire_events.event_id` is deterministic enough for dedupe.
- Existing code has no Redis dependency; use Postgres.

## New Runtime Roles

Add `SERVICE_ROLE` with these values:

```text
api
newswire
world_model
trader
discord_publisher
discord_bot
agent
liquidations
scheduler
```

### Role Matrix

| Role | Allowed | Forbidden |
|---|---|---|
| `api` | FastAPI routes, dashboards, DB reads, safe DB writes, command-intent creation | background loops, external feeds, LLM calls, Discord login/publish, trading, HIP4 loops |
| `newswire` | RSS/Alpaca/TradingEconomics/X ingestion, normalization, `newswire_events` persistence | dashboard port, Discord publish, trading, world-model mutation |
| `world_model` | consume persisted events, update world model tables/snapshots, own prediction-market streams | dashboard port, external news providers, trading, Discord |
| `trader` | engine/autonomy/HIP4/tracking loops, paper/shadow trading logic under existing safety guards | dashboard port, external news providers, Discord bot |
| `discord_publisher` | consume persisted events/snapshots, publish to Discord news channel | dashboard port, external news providers, trading |
| `discord_bot` | command/control Discord bot only | market/news ingestion loops, dashboard port |
| `agent` | execute LLM `/ask` and trade-proposal command intents | dashboard port, persistent ingestion loops, direct order execution |
| `liquidations` | liquidation feed adapters and persistence | dashboard port, trading |
| `scheduler` | maintenance jobs, reports, wave supervisor, backfills | dashboard port, external provider ownership unless explicitly delegated |

## Config Changes

### Add settings

In `hyperliquid_trading_agent/app/config.py`:

```python
class ServiceRole(StrEnum):
    API = "api"
    NEWSWIRE = "newswire"
    WORLD_MODEL = "world_model"
    TRADER = "trader"
    DISCORD_PUBLISHER = "discord_publisher"
    DISCORD_BOT = "discord_bot"
    AGENT = "agent"
    LIQUIDATIONS = "liquidations"
    SCHEDULER = "scheduler"
```

Add fields:

```python
service_role: ServiceRole = Field(default=ServiceRole.API, validation_alias="SERVICE_ROLE")
runtime_profile: str = "dev"  # environment/deployment label only; no behavior control

discord_bot_enabled: bool = False
discord_publisher_enabled: bool = False

service_heartbeat_interval_seconds: int = 15
service_heartbeat_stale_seconds: int = 90
worker_command_poll_seconds: float = 1.0
worker_command_claim_stale_seconds: int = 300
consumer_poll_seconds: float = 1.0
consumer_batch_size: int = 100
```

### Deprecate old `RUNTIME_PROFILE`

`RUNTIME_PROFILE` values `full`, `dashboard_only`, and `world_model_live` must no longer start workers.

Validation rule:
- In `prod`, legacy role-style `RUNTIME_PROFILE` values raise `ValueError`.
- In `dev`, `local`, and `test`, they are allowed only as deprecated labels and must not change behavior.
- `SERVICE_ROLE` is always authoritative.

### Role validation

Add `model_validator(mode="after")`.

Examples:

```text
SERVICE_ROLE=api
  requires:
    no worker side-effect flags
  forbids:
    NEWSWIRE_ENABLED=true
    WORLD_MODEL_STREAMS_ENABLED=true
    WORLD_MODEL_ADAPTERS_ENABLED=true
    ENGINE_ENABLED=true
    ENGINE_PNL_ATTRIBUTION_ENABLED=true
    POSITION_TRACKING_ENABLED=true
    AUTONOMY_ENABLED=true
    HIP4_ENABLED=true
    ORCHESTRATION_WAVE_SUPERVISOR_ENABLED=true
    LIQUIDATIONS_ENABLED=true
    TRADFI_ENABLED=true
    HYPERLIQUID_WS_ENABLED=true
    DISCORD_BOT_ENABLED=true
    DISCORD_PUBLISHER_ENABLED=true

SERVICE_ROLE=newswire
  requires:
    NEWSWIRE_ENABLED=true
  forbids:
    DISCORD_PUBLISHER_ENABLED=true
    ENGINE_ENABLED=true
    AUTONOMY_ENABLED=true
    HIP4_ENABLED=true
    WORLD_MODEL_STREAMS_ENABLED=true

SERVICE_ROLE=world_model
  allows:
    WORLD_MODEL_STREAMS_ENABLED=true
    WORLD_MODEL_POLYMARKET_WS_ENABLED=true
  forbids:
    NEWSWIRE_ENABLED=true
    ENGINE_ENABLED=true
    AUTONOMY_ENABLED=true
    HIP4_ENABLED=true

SERVICE_ROLE=trader
  allows:
    ENGINE_ENABLED=true
    AUTONOMY_ENABLED=true
    HIP4_ENABLED=true
    POSITION_TRACKING_ENABLED=true
  forbids:
    NEWSWIRE_ENABLED=true
    DISCORD_BOT_ENABLED=true
    DISCORD_PUBLISHER_ENABLED=true

SERVICE_ROLE=discord_publisher
  requires:
    DISCORD_PUBLISHER_ENABLED=true
    DISCORD_BOT_TOKEN set
    NEWSWIRE_NEWS_CHANNEL_ID set
  forbids:
    NEWSWIRE_ENABLED=true
    ENGINE_ENABLED=true
    AUTONOMY_ENABLED=true

SERVICE_ROLE=agent
  allows:
    model provider keys / LLM calls
  forbids:
    NEWSWIRE_ENABLED=true
    ENGINE_ENABLED=true
    AUTONOMY_ENABLED=true
    HIP4_ENABLED=true
```

Keep existing live-execution guardrails unchanged.

## Database Migration

Create new Alembic revision after `0021_newswire_publish_ledger`, e.g.:

```text
0022_service_runtime_boundaries.py
```

### `service_heartbeats`

```sql
service_role TEXT NOT NULL
instance_id TEXT NOT NULL
hostname TEXT
pid INTEGER
version TEXT
started_at_ms BIGINT NOT NULL
updated_at_ms BIGINT NOT NULL
status TEXT NOT NULL
metadata_json JSON NOT NULL DEFAULT '{}'
PRIMARY KEY (service_role, instance_id)
```

Indexes:
- `(service_role, updated_at_ms)`
- `(status, updated_at_ms)`

### `consumer_offsets`

Use string event IDs because current `newswire_events.event_id` is string.

```sql
consumer_name TEXT PRIMARY KEY
source_table TEXT NOT NULL
last_event_id TEXT
last_event_ts_ms BIGINT NOT NULL DEFAULT 0
updated_at_ms BIGINT NOT NULL
metadata_json JSON NOT NULL DEFAULT '{}'
```

### `worker_commands`

```sql
command_id TEXT PRIMARY KEY
target_role TEXT NOT NULL
command_type TEXT NOT NULL
status TEXT NOT NULL DEFAULT 'pending'
idempotency_key TEXT
requested_by TEXT
requested_at_ms BIGINT NOT NULL
claimed_by TEXT
claimed_at_ms BIGINT
completed_at_ms BIGINT
attempt_count INTEGER NOT NULL DEFAULT 0
payload_json JSON NOT NULL DEFAULT '{}'
result_json JSON
last_error TEXT
metadata_json JSON NOT NULL DEFAULT '{}'
```

Indexes:
- `(target_role, status, requested_at_ms)`
- unique nullable `idempotency_key` where present

## Repository Helpers

Add methods in `app/db/repository.py`:

```python
upsert_service_heartbeat(...)
list_service_heartbeats(...)
mark_service_stopping(...)

get_consumer_offset(...)
update_consumer_offset(...)
list_newswire_events_after(last_event_ts_ms, last_event_id, limit, filters=None)

enqueue_worker_command(...)
get_worker_command(...)
claim_next_worker_command(target_role, instance_id, stale_after_ms)
complete_worker_command(...)
fail_worker_command(...)
list_worker_commands(...)
```

Add Postgres advisory lock helper under:

```text
hyperliquid_trading_agent/app/infra/leader_lock.py
```

Lock names:
```text
service:newswire
service:world_model
service:trader
service:discord_publisher
service:discord_bot
service:agent
service:liquidations
service:scheduler
newswire:alpaca
newswire:rss
newswire:trading_economics
newswire:x
trader:orders
world_model:writer
discord:publisher:<channel_id>
```

Workers with external connections or trading authority must fail closed if they cannot acquire their lock.

## FastAPI Refactor

`hyperliquid_trading_agent/app/main.py` becomes API-only.

### `create_app()`

- Must raise if `settings.service_role != ServiceRole.API`.
- Initialize only passive resources:
  - DB engine/sessionmaker
  - repository
  - read facades for dashboards
- Do **not** start:
  - Discord bot
  - Newswire
  - World Model streams
  - HIP4
  - autonomy
  - engine loops
  - PnL attribution loop
  - liquidation adapters
  - wave supervisor

### API state

Keep only:

```python
app.state.engine
app.state.repository
app.state.world_model_service  # read facade only
app.state.settings
```

Do not attach live worker services like `newswire_service`, `discord_bot`, `ws_worker`, etc.

### API status routes

Add:

```http
GET /runtime/status
GET /runtime/heartbeats
GET /commands/{command_id}
GET /commands
```

Dashboard should use these instead of local process profile to show worker health.

### Newswire API

Refactor routes:

- `GET /newswire/events`
  - read from `repository.list_newswire_events(...)`
  - not from in-process `NewswireService`
- `GET /newswire/status`
  - read heartbeats for `SERVICE_ROLE=newswire`
  - include latest persisted event timestamp/count
  - include Discord publisher heartbeat if present
- `GET /newswire/stream`
  - implement Postgres polling WebSocket over `newswire_events`
  - no in-process bus dependency
- `POST /newswire/discord/test`
  - enqueue `worker_commands` row:
    - `target_role=discord_publisher`
    - `command_type=discord_test`
  - return `202 Accepted` with `command_id`

### World Model API

- `GET /world-model/dashboard/data`
  - read persisted world model data and latest worker heartbeat
- `GET /world-model/snapshot`
  - return latest persisted snapshot from repository
  - do not compute/persist a new snapshot inside API
- `POST /world-model/adapters/poll`
  - enqueue command:
    - `target_role=world_model`
    - `command_type=world_model_adapter_poll`
- `POST /world-model/dev/seed`
  - only allowed in `ENVIRONMENT in {dev, local, test}`
  - either:
    - direct DB seed if purely local/test, or
    - command intent to `world_model`
  - recommended: command intent outside tests

### Agent / LLM API

Convert:
- `POST /ask`
- `POST /trade/proposals`

to command-intent flow:

Response:
```json
{
  "accepted": true,
  "command_id": "...",
  "status_url": "/commands/...",
  "target_role": "agent"
}
```

The `agent` worker writes `result_json`, and clients poll `GET /commands/{command_id}`.

### Trading / engine / HIP4 run endpoints

Convert direct run endpoints to commands:

| Current endpoint | New target role |
|---|---|
| `/engine/strategy-regime-performance/refresh` | `trader` |
| `/engine/bandit-recommendations/run` | `trader` or `scheduler` |
| `/engine/replay-comparisons/run` | `trader` or `scheduler` |
| `/hip4/loop/run-once` | `trader` |
| `/hip4/scan/run` | `trader` |
| `/hip4/paper/execute/{candidate_id}` | `trader` |
| `/hip4/reconcile/run` | `trader` |
| `/orchestration/wave/run-once` | `scheduler` |
| report/backfill/evaluation run routes | `scheduler` |

Safe DB-only routes may remain synchronous:
- annotations
- outcomes
- governance review state
- memory review/archive/reject
- tuning proposal review/reject/expire

## Worker Entrypoint

Add:

```text
hyperliquid_trading_agent/app/runtime.py
```

Add script in `pyproject.toml`:

```toml
hyperliquid-trading-agent-runtime = "hyperliquid_trading_agent.app.runtime:main"
```

Usage:

```bash
hyperliquid-trading-agent-runtime newswire
hyperliquid-trading-agent-runtime world_model
hyperliquid-trading-agent-runtime trader
hyperliquid-trading-agent-runtime discord_publisher
hyperliquid-trading-agent-runtime discord_bot
hyperliquid-trading-agent-runtime agent
hyperliquid-trading-agent-runtime liquidations
hyperliquid-trading-agent-runtime scheduler
```

Runtime must verify:

```text
CLI role == SERVICE_ROLE
```

Mismatch exits non-zero.

## Worker Modules

Create:

```text
hyperliquid_trading_agent/app/workers/
  __init__.py
  base.py
  newswire_worker.py
  world_model_worker.py
  trader_worker.py
  discord_publisher_worker.py
  discord_bot_worker.py
  agent_worker.py
  liquidations_worker.py
  scheduler_worker.py
  stored_newswire_pump.py
```

### Base worker

Responsibilities:
- create DB engine/repository
- acquire leader lock if required
- update heartbeat every `SERVICE_HEARTBEAT_INTERVAL_SECONDS`
- handle SIGTERM/SIGINT gracefully
- mark heartbeat `stopping` on shutdown

### `newswire_worker`

- Owns all external news provider connections.
- Starts `NewswireService`.
- Persists events to `newswire_events`.
- Does not start in-process consumers except internal persistence.
- Heartbeat metadata includes adapter status and last event timestamp.

### `world_model_worker`

- Uses `StoredNewswirePump` with consumer name:
  - `world_model:newswire`
- Reads `newswire_events` from DB.
- Calls `WorldModelService.observe_newswire_event`.
- Owns prediction-market streams:
  - `WorldModelStreamService`
  - Polymarket WS if enabled
- Does not connect Alpaca/RSS/TradingEconomics/X.
- Processes commands:
  - `world_model_adapter_poll`
  - `world_model_dev_seed`

### `discord_publisher_worker`

- Uses consumer name:
  - `discord_publisher:newswire`
- Reads persisted `newswire_events`.
- Publishes through existing `DiscordNewsPublisher`.
- Keep `newswire_publish_ledger` as idempotency guard.
- Starts send-only Discord client.
- Processes commands:
  - `discord_test`

### `trader_worker`

Owns:
- `HyperliquidWebSocketWorker`
- `PositionTrackingService`
- `Hip4Service`
- `AutonomousTradingLoopService`
- `InstitutionalEngineService`
- `EngineValidationMonitorService`
- `EnginePnLAttributionLoopService`

Consumes persisted newswire events through `StoredNewswirePump`:
- `trader:engine_news`
- `trader:agent_news`

No news provider adapters.

Processes commands:
- engine refresh/run commands
- HIP4 run/scan/paper/reconcile commands
- autonomy control commands

Existing live-execution guardrails remain unchanged.

### `agent_worker`

- Processes:
  - `ask`
  - `trade_proposal`
- Owns LLM/model calls.
- Writes full response to `worker_commands.result_json`.
- Does not start market/news/trading loops.
- May read Hyperliquid public data through tools if needed for request context.

### `discord_bot_worker`

- Starts full Discord command bot if `DISCORD_BOT_ENABLED=true`.
- Does not own newswire providers.
- Does not own trading loops.
- Commands requiring external work should enqueue `worker_commands`.

### `liquidations_worker`

- Starts `LiquidationService`.
- Owns liquidation provider connections.
- Persists to existing liquidation tables.
- API reads persisted liquidation data only.

### `scheduler_worker`

- Runs maintenance/report/wave-supervisor style jobs.
- Owns scheduled command execution not tied to trading authority.
- Must not connect news providers unless explicitly delegated.

## Stored Event Pump

Implement reusable DB-backed consumer:

```python
class StoredNewswirePump:
    def __init__(consumer_name, repository, callbacks, poll_seconds, batch_size)
```

Behavior:
1. Read `consumer_offsets`.
2. Query `newswire_events` ordered by `(received_at_ms, event_id)`.
3. Convert rows to `NewswireEvent`.
4. Await all callbacks for each event.
5. Advance offset only after callbacks return.
6. Heartbeat degraded if repeated failures occur.

Repository query:

```sql
WHERE
  received_at_ms > :last_ts
  OR (received_at_ms = :last_ts AND event_id > :last_event_id)
ORDER BY received_at_ms ASC, event_id ASC
LIMIT :batch_size
```

## Docker Compose Refactor

Use Postgres-only. Add a one-shot migration service.

```yaml
x-app-common: &app-common
  build: .
  restart: unless-stopped
  env_file:
    - path: .env
      required: false
  environment:
    DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-hlagent}:${POSTGRES_PASSWORD:-hlagent}@postgres:5432/${POSTGRES_DB:-hlagent}
  depends_on:
    postgres:
      condition: service_healthy
    migrate:
      condition: service_completed_successfully
```

### Services

```yaml
migrate:
  build: .
  restart: "no"
  env_file:
    - path: .env
      required: false
  environment:
    DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-hlagent}:${POSTGRES_PASSWORD:-hlagent}@postgres:5432/${POSTGRES_DB:-hlagent}
  command: alembic upgrade head
  depends_on:
    postgres:
      condition: service_healthy

api:
  <<: *app-common
  environment:
    SERVICE_ROLE: api
    DATABASE_URL: ...
    NEWSWIRE_ENABLED: "false"
    WORLD_MODEL_STREAMS_ENABLED: "false"
    WORLD_MODEL_ADAPTERS_ENABLED: "false"
    ENGINE_ENABLED: "false"
    ENGINE_PNL_ATTRIBUTION_ENABLED: "false"
    POSITION_TRACKING_ENABLED: "false"
    AUTONOMY_ENABLED: "false"
    HIP4_ENABLED: "false"
    ORCHESTRATION_WAVE_SUPERVISOR_ENABLED: "false"
    LIQUIDATIONS_ENABLED: "false"
    TRADFI_ENABLED: "false"
    HYPERLIQUID_WS_ENABLED: "false"
    DISCORD_BOT_ENABLED: "false"
    DISCORD_PUBLISHER_ENABLED: "false"
  command: hyperliquid-trading-agent
  ports:
    - "${DASHBOARD_BIND:-127.0.0.1}:${HOST_PORT:-8081}:8080"

newswire:
  <<: *app-common
  environment:
    SERVICE_ROLE: newswire
    NEWSWIRE_ENABLED: "true"
    DISCORD_PUBLISHER_ENABLED: "false"
  command: hyperliquid-trading-agent-runtime newswire
  ports: []

world-model:
  <<: *app-common
  environment:
    SERVICE_ROLE: world_model
    NEWSWIRE_ENABLED: "false"
    WORLD_MODEL_STREAMS_ENABLED: "${WORLD_MODEL_STREAMS_ENABLED:-true}"
    WORLD_MODEL_POLYMARKET_WS_ENABLED: "${WORLD_MODEL_POLYMARKET_WS_ENABLED:-true}"
  command: hyperliquid-trading-agent-runtime world_model
  ports: []

trader:
  <<: *app-common
  environment:
    SERVICE_ROLE: trader
    NEWSWIRE_ENABLED: "false"
  command: hyperliquid-trading-agent-runtime trader
  ports: []

discord-publisher:
  <<: *app-common
  environment:
    SERVICE_ROLE: discord_publisher
    NEWSWIRE_ENABLED: "false"
    DISCORD_PUBLISHER_ENABLED: "true"
  command: hyperliquid-trading-agent-runtime discord_publisher
  ports: []

agent:
  <<: *app-common
  environment:
    SERVICE_ROLE: agent
    NEWSWIRE_ENABLED: "false"
  command: hyperliquid-trading-agent-runtime agent
  ports: []

discord-bot:
  profiles: ["discord-bot"]
  <<: *app-common
  environment:
    SERVICE_ROLE: discord_bot
    DISCORD_BOT_ENABLED: "true"
    NEWSWIRE_ENABLED: "false"
  command: hyperliquid-trading-agent-runtime discord_bot
  ports: []

liquidations:
  profiles: ["liquidations"]
  <<: *app-common
  environment:
    SERVICE_ROLE: liquidations
    LIQUIDATIONS_ENABLED: "true"
  command: hyperliquid-trading-agent-runtime liquidations
  ports: []

scheduler:
  profiles: ["scheduler"]
  <<: *app-common
  environment:
    SERVICE_ROLE: scheduler
  command: hyperliquid-trading-agent-runtime scheduler
  ports: []
```

### Deprecated aliases

Keep briefly:

```yaml
bot:
  profiles: ["legacy"]
  extends/duplicates api config
  environment:
    SERVICE_ROLE: api
  ports:
    - "${DASHBOARD_BIND:-127.0.0.1}:${HOST_PORT:-8081}:8080"
```

```yaml
world-model-live:
  profiles: ["legacy-world-model-live"]
  environment:
    SERVICE_ROLE: world_model
    NEWSWIRE_ENABLED: "false"
  command: hyperliquid-trading-agent-runtime world_model
  ports: []
```

No legacy alias may expose `8091`.

Remove or deprecate:

```text
WORLD_MODEL_LIVE_HOST_PORT
DASHBOARD_HOST_PORT
```

Add:

```env
DASHBOARD_BIND=127.0.0.1
HOST_PORT=8081
SERVICE_ROLE=api
```

## Documentation Updates

Update:
- `README.md`
- `.env.example`
- `docs/world-model.md`
- `docs/vault.md`
- `docs/deploy/step-10-resumption-runbook.md`
- `docs/deploy/hyrule-cloud.md`

Document invariant:

```text
There is exactly one public HTTP service: api.

Workers do not expose ports.

Only SERVICE_ROLE=newswire may connect to external news providers.

Only SERVICE_ROLE=trader may submit/cancel orders.

The API service is passive: dashboards, DB reads, safe DB writes, and command-intent creation only.

Deployment composition is controlled by Docker Compose services.
Process behavior is controlled by SERVICE_ROLE.
RUNTIME_PROFILE must not start background workers.
```

## Tests

### Config tests

Add tests for:
- `SERVICE_ROLE=api` rejects worker flags.
- `SERVICE_ROLE=newswire` rejects trading flags.
- `SERVICE_ROLE=world_model` rejects `NEWSWIRE_ENABLED=true`.
- `SERVICE_ROLE=trader` rejects `NEWSWIRE_ENABLED=true`.
- `SERVICE_ROLE=discord_publisher` requires token/channel/enabled flag.
- legacy `RUNTIME_PROFILE=full/world_model_live/dashboard_only` does not control behavior.

### API tests

Update existing `create_app` tests:
- `create_app(Settings(service_role="api"))` succeeds.
- `create_app(Settings(service_role="newswire"))` raises.
- `/health`, `/ready`, `/runtime/status`, `/commands/{id}` work.
- `/newswire/events` reads repository rows, not in-memory service.
- `/world-model/snapshot` returns latest persisted snapshot.
- external-action endpoints return `202` command intent.

### Worker tests

Add unit tests:
- runtime CLI role mismatch exits.
- worker heartbeat writes status.
- command claim/complete/fail transitions.
- consumer offset advances after event processing.
- duplicate worker lock failure exits/degrades.
- `discord_publisher` uses `newswire_publish_ledger`.
- `newswire_worker` builds adapters only in newswire role.

### Compose/static tests

Add a test or script check:
- only `api` has `ports`.
- no service publishes `8091`.
- `world-model-live` has no ports.
- `NEWSWIRE_ENABLED=true` appears only on `newswire`.
- `SERVICE_ROLE` is set for every app service.

### Regression tests

Update existing tests:
- `tests/test_world_model.py`
- `tests/test_newswire.py`
- runtime profile readiness tests
- health config tests

## Verification Commands

After implementation:

```bash
docker compose config | rg -n "ports:|8091|WORLD_MODEL_LIVE_HOST_PORT|SERVICE_ROLE|NEWSWIRE_ENABLED"
```

Expected:
- only `api` has a port
- no `8091`
- only `newswire` has `NEWSWIRE_ENABLED=true`

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Command}}'
```

Expected:

```text
api                  127.0.0.1:8081->8080/tcp
newswire             no public ports
world-model          no public ports
trader               no public ports
discord-publisher    no public ports
agent                no public ports
```

```bash
ss -ltnp | rg '8081|8091'
```

Expected:
- `8081` only
- no `8091`

```bash
curl http://127.0.0.1:8081/runtime/status
curl http://127.0.0.1:8081/newswire/status
curl http://127.0.0.1:8081/world-model/dashboard/data
```

## Rollout Plan

1. Implement config validation and Compose stop-bleed first.
2. Make `api` the default public service on `127.0.0.1:8081`.
3. Move Newswire ingestion to `newswire`.
4. Move World Model streams and event consumption to `world_model`.
5. Move Discord publishing to `discord_publisher`.
6. Move LLM endpoints to command intents and `agent`.
7. Move trader/engine/HIP4/autonomy loops to `trader`.
8. Move liquidations to `liquidations`.
9. Enable worker heartbeats in dashboard.
10. Remove deprecated aliases in a later cleanup release after verifying no scripts use them.

## Acceptance Criteria

- `docker compose up -d --build` starts one public HTTP service: `api`.
- No container other than `api` exposes a host port.
- `world-model-live` no longer publishes `8091`.
- API startup never opens Alpaca, RSS, Polymarket, Discord, Hyperliquid WS, or trading loops.
- Only `newswire` connects to Alpaca/RSS/TradingEconomics/X.
- Only `world_model` connects to prediction-market streams.
- Only `discord_publisher` publishes curated news to Discord.
- Only `agent` executes LLM `/ask` and proposal work.
- Only `trader` owns engine/autonomy/HIP4/trading loops.
- Dashboard shows worker health from `service_heartbeats`.
- Provider connection-limit failures from duplicate Newswire ownership stop recurring.
- Existing live-execution safety validators remain intact.









<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add service-role configuration and hard validation. _(done)_
- [x] 2. Add runtime infrastructure tables and repository helpers. _(done)_
- [x] 3. Split FastAPI into a passive api process. _(done)_
- [x] 4. Add non-web worker entrypoint and role workers. _(done)_
- [x] 5. Convert external-action API endpoints to command intents. _(done)_
- [x] 6. Refactor Newswire/World Model/Discord flows onto persisted events and offsets. _(done)_
- [x] 7. Refactor Docker Compose so only api exposes a port. _(done)_
- [x] 8. Update docs, examples, and deployment runbooks. _(done)_
- [x] 9. Add tests and verification coverage. _(done)_
- [x] 10. Roll out with stop-bleed compatibility aliases and monitor heartbeats. _(done)_

<!-- pi-plan-progress:end -->
