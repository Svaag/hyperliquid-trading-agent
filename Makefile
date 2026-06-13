.PHONY: test lint run docker-up docker-down

test:
	pytest

lint:
	ruff check .

run:
	hyperliquid-trading-agent

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
