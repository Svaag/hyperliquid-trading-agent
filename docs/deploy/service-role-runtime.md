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

Default Compose services start `api`, `newswire`, `world-model`, `trader`, and `agent` after migrations. Only `api` publishes a host port:

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
docker compose --profile scheduler up -d scheduler
```

## Command intents

HTTP endpoints that perform external work enqueue `worker_commands` rows and return an accepted response with `command_id` and `status_url`.

Examples include:

- `POST /ask` and `POST /trade/proposals` -> `agent`
- engine run/refresh endpoints -> `trader`
- HIP4 run/scan/paper/reconcile endpoints -> `trader`
- autonomy pause/resume/evaluation/report/approval endpoints -> `trader`
- `POST /newswire/discord/test` -> `discord_publisher`
- World Model adapter poll/dev seed -> `world_model`

Poll command completion through the API:

```bash
curl http://127.0.0.1:${HOST_PORT:-8081}/commands/<command_id>
```

## Compatibility aliases

Legacy Compose profiles are fail-safe aliases only:

- `bot` under `--profile legacy` runs a passive API-role process with worker flags disabled and no published host port.
- `world-model-live` under `--profile legacy-world-model-live` runs the `world_model` worker and publishes no host port.

Do not expose workers directly. `8091` is retired; `WORLD_MODEL_LIVE_HOST_PORT` is ignored.

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
