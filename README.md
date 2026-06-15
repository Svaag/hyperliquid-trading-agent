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
  - `POST /trade/proposals`
  - `GET /trade/proposals/{proposal_id}`
  - `GET /tracking/positions`
  - `GET /tracking/positions/{tracker_id}`
  - `GET /tracking/positions/{tracker_id}/events`
  - `POST /tracking/positions/{tracker_id}/pause|resume|stop`
  - `GET /autonomy/status|universe|market-map|signals|portfolio|positions|orders|fills|news`
  - `GET /autonomy/evaluations/signals`, `/autonomy/token-capital`, `/autonomy/reports/daily|weekly`, `/autonomy/memory/*`, `/autonomy/tuning-proposals`
  - `POST /autonomy/pause|resume`
  - `POST /autonomy/signals/{signal_id}/approve|reject|expire`
  - `POST /autonomy/evaluations/run`, `/autonomy/reports/daily/run`, `/autonomy/reports/weekly/run`, `/autonomy/feedback`
  - `GET /metrics`
- Discord mention bot with guild/channel/role allowlists and threaded answers.
- Risk-routed high-stakes multi-agent debate engine for paper/manual trade proposals only, with institutional role rubrics, endpoint coverage, and optional official SDK `Info` data.
- Deterministic live position tracking for high-stakes position reviews: auto-arms canonical levels, monitors Hyperliquid WebSocket `allMids`, sends Discord thread alerts, and stores tracker/event history without LLM calls.
- LiteLLM model gateway with ordered fallback for:
  - OpenRouter
  - OpenAI
  - Anthropic
  - Kimi/Moonshot through OpenAI-compatible API settings
- Hyperliquid official `/info` client with TTL cache and conservative process-local rate guard.
- Official docs grounding through GitBook markdown/`ask=` support plus static safety notes.
- RSS news, optional Tavily/SerpAPI/NewsAPI/Perplexity search, optional X recent search.
- Semantic tool gathering for market snapshots, funding, candles, account public state, fills, docs, news, and paper trades.
- PostgreSQL persistence for audit events, tool calls, conversations, cache, news, paper trades, debate runs, role outputs, state snapshots, trade proposals, autonomous market state, signals, paper orders/fills/positions, and portfolio snapshots.
- Alembic migrations through `0005_signal_evaluation_memory`.
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
AGENT_MODEL_CHAIN=openrouter:openai/gpt-oss-120b:free,openrouter:openai/gpt-oss-20b:free,openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:nvidia/nemotron-3-nano-30b-a3b:free
OPENROUTER_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.ai/v1
```

High-stakes proposal/debate path:

```env
HIGH_STAKES_DEBATE_ENABLED=false
HIGH_STAKES_ACTIVATION_POLICY=risk_routed
HIGH_STAKES_PROMPT_STYLE=standard
HIGH_STAKES_INFO_PROVIDER=sdk_preferred
HIGH_STAKES_MAX_ROUNDS=3
HIGH_STAKES_TIMEOUT_SECONDS=90
HIGH_STAKES_REVIEW_CONCURRENCY=3
HIGH_STAKES_MAX_COINS=3
HIGH_STAKES_MAX_DATA_ESCALATIONS=1
DEBATE_MODEL_DIVERSITY_POLICY=warn
ACCOUNT_ADDRESS_ALLOWLIST=
HIGH_STAKES_SMART_MONEY_ADDRESSES=
AGENT_API_BEARER_TOKEN=
DEBATE_ANALYST_MODEL_CHAIN=openrouter:qwen/qwen3-next-80b-a3b-instruct:free,openrouter:openai/gpt-oss-120b:free,openrouter:nex-agi/nex-n2-pro:free
DEBATE_QUANT_MODEL_CHAIN=openrouter:nvidia/nemotron-3-nano-30b-a3b:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free
DEBATE_RESEARCH_MODEL_CHAIN=openrouter:google/gemma-4-26b-a4b-it:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free
DEBATE_ADVERSARY_MODEL_CHAIN=openrouter:meta-llama/llama-3.3-70b-instruct:free,openrouter:openai/gpt-oss-120b:free,openrouter:nex-agi/nex-n2-pro:free
DEBATE_RISK_MODEL_CHAIN=openrouter:openai/gpt-oss-20b:free,openrouter:nvidia/nemotron-3-nano-30b-a3b:free,openrouter:nex-agi/nex-n2-pro:free
DEBATE_TREASURY_MODEL_CHAIN=openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free
DEBATE_EXECUTION_MODEL_CHAIN=openrouter:nex-agi/nex-n2-pro:free,openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:openai/gpt-oss-20b:free
DEBATE_JUDGE_MODEL_CHAIN=openrouter:openai/gpt-oss-120b:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free
```

The debate model contract is: role primary models should differ, adversarial reviewers should not share the proposer/quant primary, and the Judge should use a distinct strongest/main model. `DEBATE_MODEL_DIVERSITY_POLICY=warn|strict|off` controls diagnostics; `/health/config` reports the contract status. Development defaults use free OpenRouter models; in production, replace `DEBATE_JUDGE_MODEL_CHAIN` with the best available frontier/main model and keep other roles on varied open-source model families.

Role chains default to role-specific free-model chains when unset. `/trade/proposals` forces the high-stakes graph when enabled and requires `AGENT_API_BEARER_TOKEN` outside dev/test/local. `HIGH_STAKES_PROMPT_STYLE=aggressive` changes desk tone but does not relax vetoes or no-execution rules. `HIGH_STAKES_INFO_PROVIDER=sdk_preferred` uses the official Hyperliquid Python SDK `Info` client for read-only high-stakes data where available, with REST `/info` fallback for missing official endpoints. `HIGH_STAKES_REVIEW_CONCURRENCY` bounds concurrent independent reviewer calls after the analyst draft; adversary and judge still run after reviewer outputs.

Hyperliquid:

```env
HYPERLIQUID_NETWORK=mainnet
HYPERLIQUID_WS_ENABLED=false
HYPERLIQUID_EXCHANGE_ENABLED=false
```

Live position tracking:

```env
POSITION_TRACKING_ENABLED=true
POSITION_TRACKING_AUTO_ARM=true
POSITION_TRACKING_DEFAULT_TTL_HOURS=168
POSITION_TRACKING_PRICE_SOURCE=allMids
POSITION_TRACKING_REARM_BAND_BPS=10
POSITION_TRACKING_RELOAD_SECONDS=10
POSITION_TRACKING_MAX_ACTIVE=250
POSITION_TRACKING_ALERT_RETRY_COUNT=3
```

When enabled, high-stakes position reviews with coin/side/entry/stop auto-arm low-overhead WebSocket level alerts. Discord users can say `tracking status`, `tracking events`, `pause tracking`, `resume tracking`, `stop tracking`, or `track until 24h/7d` inside the bot-created thread.

Autonomous loop (disabled by default; paper + human signoff only):

```env
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

# Persistent Alpha Memory / signal evaluation loop (observe-and-recommend only)
AUTONOMY_EVALUATION_ENABLED=true
AUTONOMY_MEMORY_ENABLED=true
AUTONOMY_REPORTS_ENABLED=true
AUTONOMY_EVAL_HORIZONS=15m,1h,4h,24h,expiry
AUTONOMY_EVAL_MAX_OPEN_SIGNALS=500
AUTONOMY_EVAL_PRICE_SOURCE=allMids
AUTONOMY_DAILY_REPORT_ENABLED=true
AUTONOMY_DAILY_REPORT_UTC=00:05
AUTONOMY_WEEKLY_REPORT_ENABLED=true
AUTONOMY_WEEKLY_REPORT_DAY=MON
AUTONOMY_WEEKLY_REPORT_UTC=00:30
AUTONOMY_MEMORY_ROLE_MAX_ACTIVE=200
AUTONOMY_MEMORY_OPERATOR_MAX_ACTIVE=100
AUTONOMY_MEMORY_CANDIDATE_TTL_DAYS=30
AUTONOMY_MEMORY_SHADOW_TTL_DAYS=60
AUTONOMY_MEMORY_ROLE_TTL_DAYS=30
AUTONOMY_MEMORY_PROCESS_TTL_DAYS=90
AUTONOMY_MEMORY_INCIDENT_TTL_DAYS=14
AUTONOMY_ROLE_LESSON_MIN_SAMPLES=5
AUTONOMY_OPERATOR_LESSON_MIN_SAMPLES=3
AUTONOMY_SIGNAL_LESSON_MIN_SAMPLES=20
AUTONOMY_LESSON_MIN_CONFIDENCE=0.70
AUTONOMY_STRATEGY_LESSON_MIN_CONFIDENCE=0.75
AUTONOMY_TUNING_PROPOSALS_ENABLED=true
AUTONOMY_TUNING_PROPOSAL_TTL_DAYS=14

NEWSWIRE_ENABLED=true
NEWSWIRE_QUERIES=BTC,ETH,HYPE,Hyperliquid,Fed,CPI,FOMC,crypto liquidation
```

When `AUTONOMY_ENABLED=true`, the service watches the configured universe, builds a deterministic market mental map, generates scored signals, posts qualifying alerts to `AUTONOMY_ALERT_CHANNEL_ID`, and waits for human signoff. Discord alert-channel commands: `approve signal <id>`, `reject signal <id>`, `signal <id>`, `signals`, `portfolio`, `positions`, `orders`, `market map`, `pause autonomy`, `resume autonomy`, `daily report`, `weekly report`, `token capital`, `signal outcome <id>`, `mark signal <id> good|bad|unclear|too_noisy|useful|wrong`, `memories [role]`, and `tuning proposals`. Approvals create paper orders/fills/positions only; no live trade is placed. The persistent memory/evaluation knobs are shadow-safe: they evaluate and recommend, but never auto-apply strategy, risk, execution, or sizing changes. See [docs/autonomy-memory.md](docs/autonomy-memory.md).

`/ready` reports autonomy degraded when enabled without an alert channel, when market data is stale, or when persistence is unavailable.

`HYPERLIQUID_EXCHANGE_ENABLED=true` is rejected by config validation in this MVP.

## Hyperliquid ground truth

Official API docs: <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api>

Official Python SDK: <https://github.com/hyperliquid-dex/hyperliquid-python-sdk>

MVP uses these official `/info` endpoints before any custom exchange logic:
`allMids`, `meta`, `metaAndAssetCtxs`, `spotMeta`, `spotMetaAndAssetCtxs`,
`clearinghouseState`, `spotClearinghouseState`, `frontendOpenOrders`,
`openOrders`, `userFills`, `userFillsByTime`, `historicalOrders`,
`userFunding`, `fundingHistory`, `predictedFundings`, `l2Book`,
`candleSnapshot`, `userRateLimit`, `userFees`, `portfolio`,
`userNonFundingLedgerUpdates`, `userTwapSliceFills`, `userVaultEquities`,
`userRole`, `extraAgents`, `subAccounts`, and `referral`.
High-stakes runs use route-relevant endpoint coverage instead of sweeping every endpoint every time.

Important docs-backed rules embedded in the agent:

- Query account data with the actual master/subaccount address, not an API wallet address.
- Perp coins use `meta.universe[].name`.
- Spot pairs use `PURR/USDC` for PURR or `@{index}` from `spotMeta.universe`.
- Future exchange asset IDs: perps use `meta.universe` index; spot uses `10000 + spotMeta.universe index`.
- Price/size validation follows Hyperliquid tick/lot size docs.

## Safety stance

- No private keys, seed phrases, passwords, API keys, or signing secrets in Discord.
- No mainnet trading in the MVP.
- High-stakes debate produces manual/paper proposals only; `exchange_actions` is intentionally empty.
- Live tracking only emits alerts/events; it does not place orders and keeps `exchange_actions=[]` for future autonomous-trading hooks.
- Autonomous V1 is paper-signoff only: every signal requires human approval, and approval creates simulated paper orders/fills/positions only.
- Inferred stop/liquidation clusters are labeled inferred; only configured public-account liquidation prices are direct.
- Local paper simulation only.
- Direct trade coaching is allowed, but every answer should include risk,
  assumptions, invalidation, and caveats.

## Testing

See [TESTING.md](TESTING.md).

Current local validation:

```text
68 passed
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
