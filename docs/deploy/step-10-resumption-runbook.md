# Step 10 Resumption Runbook — Provision Hyrule Cloud VM, Deploy, Validate

This file documents the remaining accepted-plan step for later resumption.

## Current local state

Project path:

```text
/home/svag/Dev/hyperliquid-trading-agent
```

Implemented through step 9:

- Python service package
- FastAPI `/health`, `/ready`, `/health/config`, `/ask`, `/metrics`
- Discord mention bot with allowlists and threaded responses
- LiteLLM model fallback for OpenRouter, OpenAI, Anthropic, and Kimi/Moonshot
- Hyperliquid official `/info` REST data layer with TTL cache and process-local rate guard
- Optional disabled-by-default Hyperliquid WebSocket cache worker
- Hyperliquid GitBook docs grounding
- RSS plus optional Tavily/SerpAPI/NewsAPI/Perplexity/X integrations
- PostgreSQL persistence with Alembic migration
- Local paper-trade simulator
- Dockerfile and Docker Compose

Last local validation:

```bash
uv run pytest -q                         # 11 passed
uv run ruff check .                      # all checks passed
uv run mypy hyperliquid_trading_agent    # success
uv run alembic upgrade head --sql        # generated migration SQL
docker compose config                    # valid
```

Live Hyperliquid smoke:

```text
live-allMids-ok True
```

Local Docker build was not run because the local Docker daemon was unavailable:

```text
Cannot connect to the Docker daemon at unix:///var/run/docker.sock
```

## Goal of remaining step

Provision a fresh Hyrule Cloud Customer VM, deploy this service with Docker Compose, configure secrets, and validate Discord end-to-end.

## Target VM

Recommended Hyrule Cloud spec:

- OS: `debian-13`
- Size: `md`
- Duration: 30 days
- Domain mode: `auto`
- Hostname: generated under `*.deploy.hyrule.host`
- Open inbound ports: 22, 80, 443
- Runtime app port: 8080 internally; optionally reverse proxy 80/443 to 8080 later

## Prerequisites to resume

Local machine:

- GitHub repo exists and is pushed.
- SSH key available for Hyrule VM provisioning.
- Hyrule Cloud payment flow available.

Secrets to prepare for `.env` on the VM:

```env
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_GUILD_IDS=
DISCORD_ALLOWED_CHANNEL_IDS=
DISCORD_ALLOWED_ROLE_IDS=
DISCORD_ADMIN_USER_IDS=

AGENT_MODEL_CHAIN=openrouter:anthropic/claude-sonnet-4.6,openrouter:deepseek/deepseek-v4-pro
OPENROUTER_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.ai/v1

TAVILY_API_KEY=
SERPAPI_API_KEY=
NEWSAPI_API_KEY=
PERPLEXITY_API_KEY=
X_BEARER_TOKEN=

METRICS_BEARER_TOKEN=
```

Minimum viable secrets:

- `DISCORD_BOT_TOKEN`
- `DISCORD_ALLOWED_GUILD_IDS`
- `DISCORD_ALLOWED_CHANNEL_IDS`
- at least one model-provider API key, preferably `OPENROUTER_API_KEY`

Never paste private keys, seed phrases, or exchange signing secrets into this service. Mainnet exchange actions are disabled by config validation.

## Provision VM through Hyrule Cloud

Use `https://cloud.hyrule.host` or the Hyrule Cloud client/API.

Request shape:

```json
{
  "duration_days": 30,
  "size": "md",
  "os": "debian-13",
  "ssh_pubkey": "ssh-ed25519 ...",
  "domain_mode": "auto",
  "open_ports": [80, 443],
  "setup_script": "apt-get update && apt-get install -y ca-certificates curl git docker.io docker-compose-plugin && systemctl enable --now docker"
}
```

Expected result after paid x402 flow:

```text
status_url=https://cloud.hyrule.host/v1/vm/<vm_id>
ssh=ssh root@<auto>.deploy.hyrule.host
```

Poll until `status` is `ready`.

## Deploy on VM

SSH in:

```bash
ssh root@<auto>.deploy.hyrule.host
```

Install runtime if the setup script did not already finish it:

```bash
apt-get update
apt-get install -y ca-certificates curl git docker.io docker-compose-plugin
systemctl enable --now docker
```

Clone and configure:

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/Svaag/hyperliquid-trading-agent.git
cd hyperliquid-trading-agent
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Use the Compose defaults for Postgres unless changing credentials:

```env
POSTGRES_USER=hlagent
POSTGRES_PASSWORD=<generate-a-new-password>
POSTGRES_DB=hlagent
DATABASE_URL=postgresql+asyncpg://hlagent:<same-password>@postgres:5432/hlagent
```

Start:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

## Validate service

On VM:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/ready
curl -fsS http://127.0.0.1:8080/health/config | python3 -m json.tool
```

Expected:

- `/health` status `ok`
- `/ready` status `ready`; Hyperliquid check should be `ok` or temporarily `degraded:<error>` during network issues
- `/health/config` confirms:
  - `hyperliquid_exchange_enabled: false`
  - `hyperliquid_ws_enabled: false` unless intentionally enabled
  - at least one configured model has `missing: null`

Protected metrics check:

```bash
curl -fsS -H "Authorization: Bearer $METRICS_BEARER_TOKEN" http://127.0.0.1:8080/metrics | head
```

## Validate Discord

In an allowlisted channel, mention the bot:

```text
@bot what is your BTC market read?
@bot compare ETH and SOL funding
@bot plan a paper long BTC entry 65000 stop 63500 tp 69000 equity 10000 risk 1
@bot what happened in macro/crypto news today?
@bot what is the weather?   # should refuse as off-topic
```

Expected behavior:

- Bot creates or uses a thread.
- Bot answers with trading-support format.
- BTC/ETH/SOL market questions use live Hyperliquid data.
- Paper trade question returns size/notional/risk and stores audit/paper-trade records.
- Off-topic question is refused.
- No mainnet trade execution is possible.

## Optional Caddy reverse proxy

If exposing HTTP(S), put Caddy or another reverse proxy in front of port 8080.
Keep `/metrics` protected by `METRICS_BEARER_TOKEN` even if the service is not public.

Example Caddyfile sketch:

```caddyfile
<auto>.deploy.hyrule.host {
  reverse_proxy 127.0.0.1:8080
}
```

## Operational commands

```bash
cd /opt/hyperliquid-trading-agent

docker compose ps
docker compose logs -f bot
docker compose restart bot
docker compose pull && docker compose up -d --build

docker compose exec postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"
docker compose exec bot alembic current
```

## Rollback / stop

```bash
cd /opt/hyperliquid-trading-agent
docker compose down
```

Data persists in the `postgres_data` Docker volume. To destroy all state:

```bash
docker compose down -v
```

## Completion checklist

- [ ] Fresh Hyrule Cloud VM provisioned and reachable by SSH.
- [ ] Repo cloned from GitHub.
- [ ] `.env` created with Discord/model/news secrets.
- [ ] `docker compose up -d --build` succeeds.
- [ ] Alembic migration ran on container startup.
- [ ] `/health` returns ok.
- [ ] `/ready` returns ready.
- [ ] `/health/config` shows at least one model provider available.
- [ ] Discord bot is online.
- [ ] Mention test answers live Hyperliquid BTC data.
- [ ] Paper trade simulation works and persists.
- [ ] Off-topic refusal works.
- [ ] Metrics access is protected or internal-only.
