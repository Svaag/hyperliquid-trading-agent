# Service-Role Runtime

This deployment has one public FastAPI process and dedicated non-web workers for side effects.

## Roles

`SERVICE_ROLE` is the only process-behavior switch. `RUNTIME_PROFILE` is an environment/profile label only and must not start work by itself.

| Role | Owns | Must not own |
| --- | --- | --- |
| `api` | HTTP dashboard/API, command enqueueing, persisted-state reads | news providers, prediction streams, Discord sessions, trading loops |
| `newswire` | RSS/Alpaca/TradingEconomics/X ingestion into `newswire_events` | dashboard port, Discord sessions, trading loops |
| `world_model` | persisted news consumption, world-model updates, prediction-market streams | dashboard port, external news providers |
| `trader` | engine/autonomy/HIP4/tracking loops under existing safety flags; persisted Newswire consumption for engine features | dashboard port, news providers, Discord sessions |
| `agent` | LLM command execution for `/ask` and `/trade/proposals` | dashboard port, ingestion loops, order execution |
| `discord_publisher` | send-only publishing from persisted Newswire events | dashboard port, news providers, trading loops |
| `discord_bot` | optional command/control Discord session | dashboard port, ingestion/trading loops |
| `liquidations` | liquidation-feed adapters when enabled | dashboard port, trading loops |
| `scheduler` | periodic commands and the single Wave Supervisor loop | dashboard port, engine execution |

## Local compose layout

```bash
cp .env.example .env
docker compose up -d --build
```

Default Compose services start `api`, `newswire`, `world-model`, `trader`, `agent`, and `scheduler` after migrations. Only `api` publishes a host port:

```bash
curl http://127.0.0.1:${HOST_PORT:-8081}/health
curl http://127.0.0.1:${HOST_PORT:-8081}/runtime/status
curl http://127.0.0.1:${HOST_PORT:-8081}/runtime/heartbeats
curl http://127.0.0.1:${HOST_PORT:-8081}/runtime/heartbeats/history
```

Optional workers are profile-gated:

```bash
docker compose --profile discord-publisher up -d discord-publisher
docker compose --profile discord-bot up -d discord-bot
docker compose --profile liquidations up -d liquidations
```

## Command intents

HTTP endpoints that perform external work enqueue `worker_commands` rows and return an accepted response with `command_id` and `status_url`.

Examples include:

- `POST /ask` and `POST /trade/proposals` -> `agent`
- engine run/refresh endpoints -> `trader`
- HIP4 run/scan/paper/reconcile/manual-ticket endpoints -> `trader`
- autonomy pause/resume/evaluation/report/approval endpoints -> `trader`
- tracking pause/resume/stop endpoints -> `trader`
- `POST /newswire/discord/test` -> `discord_publisher`
- `POST /orchestration/wave/run-once` -> `scheduler`
- World Model adapter poll/dev seed -> `world_model`

Poll, retry, or cancel command completion through the API:

```bash
curl http://127.0.0.1:${HOST_PORT:-8081}/commands/<command_id>
curl -X POST http://127.0.0.1:${HOST_PORT:-8081}/commands/<command_id>/retry
curl -X POST http://127.0.0.1:${HOST_PORT:-8081}/commands/<command_id>/cancel
```

Operator surfaces:

```bash
curl http://127.0.0.1:${HOST_PORT:-8081}/runtime/command-registry
curl http://127.0.0.1:${HOST_PORT:-8081}/runtime/command-health
curl http://127.0.0.1:${HOST_PORT:-8081}/runtime/offsets
open http://127.0.0.1:${HOST_PORT:-8081}/runtime/dashboard
```

## Newswire consumers

Persisted canonical story revisions fan out through independent `consumer_offsets` entries:

- `world_model:newswire` updates world-model events, beliefs, narratives, and memories.
- `discord_publisher:newswire` routes V2 `standard|high|breaking` assessments through a durable outbox. High/breaking stories release immediately; standard stories release on schedule as individual rich posts with feedback controls.
- `trader:engine_newswire` feeds the Institutional Engine ledger, features, and news-risk state from explicit engine actions without enabling news provider connections in the trader process.

`SERVICE_ROLE=trader` must keep `NEWSWIRE_ENABLED=false`; the trader consumes stored revisions only. Consumer offsets now identify `source_table=newswire_story_revisions`. Use a dedicated replay/backfill tool if historical engine news features are desired.

Paper-signoff preflight remains read-only and never allows live execution:

```bash
curl 'http://127.0.0.1:${HOST_PORT:-8081}/engine/paper-signoff/preflight?symbols=BTC,ETH,HYPE&window_hours=24&limit=1000'
```

## Compatibility aliases

Legacy Compose profiles are fail-safe aliases only:

- `bot` under `--profile legacy` runs a passive API-role process with worker flags disabled and no published host port.
- `world-model-live` under `--profile legacy-world-model-live` runs the `world_model` worker and publishes no host port.

Do not expose workers directly. `8091` is retired; `WORLD_MODEL_LIVE_HOST_PORT` is ignored.

Remove the compatibility aliases after a clean post-soak window with all of the following true:

1. At least one full local soak has no stale required workers, failed commands, or duplicate host app ports.
2. `/runtime/command-health` reports no missing default roles.
3. Newswire -> World Model and Newswire -> Engine offsets advance and resume after restarts without skipped valid events.
4. Operators have switched to service-role names (`api`, `world-model`, `trader`, etc.) in runbooks and automation.
5. No deployment scripts reference `bot`, `world-model-live`, `8091`, or `WORLD_MODEL_LIVE_HOST_PORT`.

## Verification

```bash
VAULT_ENABLED=false docker compose config >/tmp/compose.yml
python3 - <<'PY'
from pathlib import Path
lines = Path('/tmp/compose.yml').read_text().splitlines()
ports = []
for i, line in enumerate(lines):
    if line.strip() == 'ports:':
        service = '?'
        for prev in range(i - 1, -1, -1):
            if lines[prev].startswith('  ') and not lines[prev].startswith('    ') and lines[prev].strip().endswith(':'):
                service = lines[prev].strip().rstrip(':')
                break
        ports.append(service)
print(ports)
assert ports == ['api']
assert '8091' not in '\n'.join(lines)
PY
```

At runtime, `/runtime/status` and `/runtime/heartbeats` show one current active instance per role and include `heartbeat_age_ms`, `stale`, and stale-count fields. Superseded, stopped, and failed instances remain available for one hour through `/runtime/heartbeats/history`. Stale or missing worker heartbeats indicate a stopped worker, not an API failure. Restart the role-specific service rather than starting another dashboard process.

For the interactive Discord command path, follow [the mention-path smoke test](../discord-mention-path-runbook.md). Gateway readiness alone is not proof that mentions reach the agent worker and receive a reply.
