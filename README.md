# Hyperliquid Trading Agent

Standalone Hyperliquid trading support desk and Discord bot.

The MVP is read-mostly and paper-trading only: it enriches Discord mention
answers with official Hyperliquid API data, market/news context, Hyperliquid
docs grounding, and local risk math. Mainnet exchange actions are disabled by
design.

## What is implemented

- FastAPI operational surface:
  - `GET /health`
  - `GET /ready`
  - `GET /health/config`
  - `POST /ask`
  - `GET /metrics`
- Discord mention bot with guild/channel/role allowlists and threaded answers.
- LiteLLM model gateway with ordered fallback for:
  - OpenRouter
  - OpenAI
  - Anthropic
  - Kimi/Moonshot through OpenAI-compatible API settings
- Hyperliquid official `/info` client with TTL cache and conservative process-local rate guard.
- Official docs grounding through GitBook markdown/`ask=` support plus static safety notes.
- RSS news, optional Tavily/SerpAPI/NewsAPI/Perplexity search, optional X recent search.
- Semantic tool gathering for market snapshots, funding, candles, account public state, fills, docs, news, and paper trades.
- PostgreSQL persistence for audit events, tool calls, conversations, cache, news, and paper trades.
- Alembic initial migration.
- Dockerfile and Docker Compose with Postgres.

## Quick start

```bash
uv sync --extra dev
cp .env.example .env
uv run pytest -q
uv run hyperliquid-trading-agent
```

Docker Compose:

```bash
cp .env.example .env
# Fill Discord + at least one model provider key for full LLM answers.
docker compose up -d --build
curl http://127.0.0.1:8080/health
```

`docker compose config` works without `.env`; the env file is optional for static
validation and expected for real deployments.

## Configuration

Required for Discord runtime:

```env
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_GUILD_IDS=
DISCORD_ALLOWED_CHANNEL_IDS=
```

Model chain:

```env
AGENT_MODEL_CHAIN=openrouter:anthropic/claude-sonnet-4.6,openrouter:deepseek/deepseek-v4-pro
OPENROUTER_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.ai/v1
```

Hyperliquid:

```env
HYPERLIQUID_NETWORK=mainnet
HYPERLIQUID_WS_ENABLED=false
HYPERLIQUID_EXCHANGE_ENABLED=false
```

`HYPERLIQUID_EXCHANGE_ENABLED=true` is rejected by config validation in this MVP.

## Hyperliquid ground truth

Official API docs: <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api>

Official Python SDK: <https://github.com/hyperliquid-dex/hyperliquid-python-sdk>

MVP uses these official `/info` endpoints before any custom exchange logic:
`allMids`, `meta`, `metaAndAssetCtxs`, `spotMeta`, `spotMetaAndAssetCtxs`,
`clearinghouseState`, `spotClearinghouseState`, `frontendOpenOrders`,
`openOrders`, `userFills`, `userFillsByTime`, `historicalOrders`,
`userFunding`, `fundingHistory`, `predictedFundings`, `l2Book`,
`candleSnapshot`, and `userRateLimit`.

Important docs-backed rules embedded in the agent:

- Query account data with the actual master/subaccount address, not an API wallet address.
- Perp coins use `meta.universe[].name`.
- Spot pairs use `PURR/USDC` for PURR or `@{index}` from `spotMeta.universe`.
- Future exchange asset IDs: perps use `meta.universe` index; spot uses `10000 + spotMeta.universe index`.
- Price/size validation follows Hyperliquid tick/lot size docs.

## Safety stance

- No private keys, seed phrases, passwords, API keys, or signing secrets in Discord.
- No mainnet trading in the MVP.
- Local paper simulation only.
- Direct trade coaching is allowed, but every answer should include risk,
  assumptions, invalidation, and caveats.

## Testing

See [TESTING.md](TESTING.md).

Current local validation:

```text
11 passed
ruff: all checks passed
mypy: success
alembic offline SQL: generated
compose config: valid
live Hyperliquid allMids: BTC returned
```

## Deployment

See:

- [docs/deploy/hyrule-cloud.md](docs/deploy/hyrule-cloud.md)
- [docs/deploy/step-10-resumption-runbook.md](docs/deploy/step-10-resumption-runbook.md)
