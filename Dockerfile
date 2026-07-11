FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY alembic ./alembic
COPY hyperliquid_trading_agent ./hyperliquid_trading_agent

RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && hyperliquid-trading-agent"]
