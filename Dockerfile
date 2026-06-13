FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY hyperliquid_trading_agent ./hyperliquid_trading_agent

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && hyperliquid-trading-agent"]
