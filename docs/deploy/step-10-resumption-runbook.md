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
- High-stakes multi-agent debate path for paper/manual trade proposals
- Dockerfile and Docker Compose

Last local validation:

```bash
uv run pytest -q                         # 22 passed
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

AGENT_MODEL_CHAIN=openrouter:openai/gpt-oss-120b:free,openrouter:openai/gpt-oss-20b:free,openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:nvidia/nemotron-3-nano-30b-a3b:free
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
AGENT_API_BEARER_TOKEN=
HIGH_STAKES_DEBATE_ENABLED=false
HIGH_STAKES_PROMPT_STYLE=standard
HIGH_STAKES_INFO_PROVIDER=sdk_preferred
HIGH_STAKES_MAX_DATA_ESCALATIONS=1
HIGH_STAKES_REVIEW_CONCURRENCY=3
HIGH_STAKES_SMART_MONEY_ADDRESSES=
DEBATE_MODEL_DIVERSITY_POLICY=warn
# Production: set DEBATE_JUDGE_MODEL_CHAIN to the strongest available frontier/main model.
# Development/free defaults are role-diverse and can be copied from .env.example.

POSITION_TRACKING_ENABLED=true
POSITION_TRACKING_AUTO_ARM=true
POSITION_TRACKING_DEFAULT_TTL_HOURS=168
POSITION_TRACKING_PRICE_SOURCE=allMids
POSITION_TRACKING_REARM_BAND_BPS=10
POSITION_TRACKING_RELOAD_SECONDS=10
POSITION_TRACKING_MAX_ACTIVE=250
POSITION_TRACKING_ALERT_RETRY_COUNT=3

AUTONOMY_ENABLED=false
AUTONOMY_MODE=paper_signoff
AUTONOMY_ALERT_CHANNEL_ID=
AUTONOMY_REQUIRE_HUMAN_SIGNOFF=true
AUTONOMY_CORE_UNIVERSE=BTC,ETH,HYPE
AUTONOMY_UNIVERSE_TOP_N_PERPS=20
AUTONOMY_MAX_TRACKED_ASSETS=40
AUTONOMY_MAX_HOT_L2_ASSETS=5
AUTONOMY_MAX_SIGNALS_PER_DAY=10
AUTONOMY_MIN_SIGNAL_SCORE=75
AUTONOMY_PAPER_INITIAL_EQUITY_USD=100000
AUTONOMY_PAPER_RISK_PCT_PER_TRADE=0.25
AUTONOMY_MODEL_INSIGHTS_ENABLED=true
NEWSWIRE_ENABLED=true
NEWSWIRE_QUERIES=BTC,ETH,HYPE,Hyperliquid,Fed,CPI,FOMC,crypto liquidation
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
  - `position_tracking.enabled: true` unless intentionally disabled
  - `high_stakes.model_contract.status` is `ok` or an intentional `warning`; Judge primary should not overlap reviewer primaries in production
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
- If `HIGH_STAKES_DEBATE_ENABLED=true`, explicit high-stakes trade setup prompts return audited manual/paper proposals with institutional prompt rubrics, route-relevant Hyperliquid endpoint coverage, optional official SDK `Info` data, bounded concurrent reviewer calls, and no live execution.
- Valid position reviews auto-arm live level tracking; inside the bot-created thread, `tracking status` should list the active tracker and `stop tracking` should stop it.
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

## Autonomous loop rollout

Autonomy is disabled by default. To run paper/signoff mode locally or on a VM:

```env
AUTONOMY_ENABLED=true
AUTONOMY_MODE=paper_signoff
AUTONOMY_ALERT_CHANNEL_ID=<discord-ai-bot-alerts-channel-id>
AUTONOMY_REQUIRE_HUMAN_SIGNOFF=true
AUTONOMY_CORE_UNIVERSE=BTC,ETH,HYPE
AUTONOMY_UNIVERSE_TOP_N_PERPS=5
AUTONOMY_MAX_SIGNALS_PER_DAY=3
```

Validate:

```bash
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" http://127.0.0.1:8080/autonomy/status
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" http://127.0.0.1:8080/autonomy/market-map
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" http://127.0.0.1:8080/autonomy/portfolio
```

Discord `#ai-bot-alerts` commands:

```text
approve signal <id>
reject signal <id>
signals
portfolio
positions
orders
market map
pause autonomy
resume autonomy
```

All approvals are paper-only. `HYPERLIQUID_EXCHANGE_ENABLED=true` remains rejected by config validation.

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
- [ ] If autonomy is enabled, `AUTONOMY_ALERT_CHANNEL_ID` points to `#ai-bot-alerts`.
- [ ] `/autonomy/status` shows `mode=paper_signoff` and no live execution path.
- [ ] A Discord signal approval creates a paper order/fill/position only.
