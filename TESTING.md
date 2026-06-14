# Testing

Local validation commands:

```bash
uv sync --extra dev
uv run pytest -q                         # 22 passed locally
uv run ruff check .                      # all checks passed
uv run mypy hyperliquid_trading_agent    # success
uv run alembic upgrade head --sql >/tmp/hla_migration.sql

docker compose config
```

Live smoke tests that do not require secrets:

```bash
uv run python - <<'PY'
import asyncio
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
async def main():
    client = HyperliquidClient(Settings())
    try:
        mids = await client.all_mids()
        print('BTC', mids.get('BTC'))
    finally:
        await client.close()
asyncio.run(main())
PY
```

High-stakes proposal path is disabled by default. Enable it with `HIGH_STAKES_DEBATE_ENABLED=true`; `/trade/proposals` is token-gated outside dev/test/local and produces paper/manual proposals only. `HIGH_STAKES_PROMPT_STYLE=standard|aggressive`, `HIGH_STAKES_INFO_PROVIDER=sdk_preferred|rest_only|sdk_only`, and `HIGH_STAKES_MAX_DATA_ESCALATIONS=1` control the institutional prompt/data-desk behavior.

API fallback smoke test without LLM keys or Postgres:

```bash
uv run python - <<'PY'
from fastapi.testclient import TestClient
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.main import create_app
app = create_app(Settings(discord_bot_token='', database_url='postgresql+asyncpg://bad:bad@127.0.0.1:1/bad'))
with TestClient(app) as client:
    r = client.post('/ask', json={'prompt': 'What is your BTC market read?'})
    print(r.status_code, r.json()['tool_count'], r.json()['fallback_used'])
PY
```

Docker daemon note: `docker compose build` requires a running Docker daemon and
buildx plugin. In this environment, `docker compose config` is the available
static validation.
