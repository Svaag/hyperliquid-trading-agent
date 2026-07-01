# Service-Role Runtime

This deployment has one public FastAPI process and dedicated non-web workers for side effects.

## Roles

`SERVICE_ROLE` is the only process-behavior switch. `RUNTIME_PROFILE` is an environment/profile label only and must not start work by itself.

| Role | Owns | Must not own |
| --- | --- | --- |
| `api` | HTTP dashboard/API, command enqueueing, persisted-state reads | news providers, prediction streams, Discord sessions, trading loops |
| `newswire` | RSS/Alpaca/TradingEconomics/X ingestion into `newswire_events` | dashboard port, Discord sessions, trading loops |
| `world_model` | persisted news consumption, world-model updates, prediction-market streams | dashboard port, external news providers |
| `trader` | engine/autonomy/HIP4/tracking loops under existing safety flags | dashboard port, news providers, Discord sessions |
| `agent` | LLM command execution for `/ask` and `/trade/proposals` | dashboard port, ingestion loops, order execution |
| `discord_publisher` | send-only publishing from persisted Newswire events | dashboard port, news providers, trading loops |
| `discord_bot` | optional command/control Discord session | dashboard port, ingestion/trading loops |
| `liquidations` | liquidation-feed adapters when enabled | dashboard port, trading loops |
| `scheduler` | lightweight periodic command scheduling | dashboard port |

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
3. Newswire -> World Model offsets advance and resume after restarts without skipped events.
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

At runtime, `/runtime/status` and `/runtime/heartbeats` include `heartbeat_age_ms`, `stale`, and stale-count fields. Stale or missing worker heartbeats indicate a stopped worker, not an API failure. Restart the role-specific service rather than starting another dashboard process.
