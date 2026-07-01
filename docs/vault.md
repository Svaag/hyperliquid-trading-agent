# HashiCorp Vault

This repository includes an opt-in local HashiCorp Vault service for credentials. It is meant to replace ad hoc `.env` secret storage over time while keeping the agent's current safety model intact.

Vault can store sensitive material for all subsystems, including Discord tokens, LLM provider keys, Alpaca and newswire keys, Prediction Market credentials, database passwords, operator API tokens, and future Hyperliquid signing material such as `HYPERLIQUID_PRIVATE_KEY`.

The app still rejects live execution flags such as `HYPERLIQUID_EXCHANGE_ENABLED=true`, `ALPACA_TRADING_ENABLED=true`, and `ENGINE_LIVE_ENABLED=true`. Storing a private key in Vault does not grant the agent order-placement authority.

## Start Vault

```bash
docker compose --profile vault up -d vault
docker compose exec vault vault status
```

The Compose Vault listener uses HTTP and `tls_disable=true` for local development only. Do not expose this port publicly.

## Initialize And Unseal

Run this once per fresh `vault_data` volume:

```bash
docker compose exec vault vault operator init -key-shares=1 -key-threshold=1
```

Store the unseal key and initial root token outside the repository.

Unseal after initialization or container restart:

```bash
docker compose exec vault vault operator unseal <UNSEAL_KEY>
docker compose exec vault vault login <INITIAL_ROOT_TOKEN>
```

Enable KV v2:

```bash
docker compose exec vault vault secrets enable -path=kv kv-v2
```

## Credential Path

Use this canonical path for the agent:

```text
kv/hyperliquid-trading-agent/prod
```

For staging or local sandboxes, use a different leaf:

```text
kv/hyperliquid-trading-agent/local
kv/hyperliquid-trading-agent/staging
```

## Credential Inventory

Recommended keys for `kv/hyperliquid-trading-agent/prod`:

```text
DISCORD_BOT_TOKEN
AGENT_API_BEARER_TOKEN
METRICS_BEARER_TOKEN
POSTGRES_PASSWORD
DATABASE_URL
OPENROUTER_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY
KIMI_API_KEY
ALPACA_API_KEY
ALPACA_API_SECRET
TRADING_ECONOMICS_API_KEY
TAVILY_API_KEY
SERPAPI_API_KEY
NEWSAPI_API_KEY
PERPLEXITY_API_KEY
X_BEARER_TOKEN
KALSHI_API_KEY_ID
KALSHI_PRIVATE_KEY_PEM
POLYMARKET_API_KEY
POLYMARKET_SECRET
POLYMARKET_PASSPHRASE
POLYMARKET_PRIVATE_KEY
HYPERLIQUID_ACCOUNT_ADDRESS
HYPERLIQUID_VAULT_ADDRESS
HYPERLIQUID_API_WALLET_ADDRESS
HYPERLIQUID_PRIVATE_KEY
HYPERLIQUID_PRIVATE_KEY_TESTNET
```

IDs such as Discord guild/channel IDs are not secrets, but it is fine to keep deployment-specific values in the same KV object if you want one source of truth.

## Write Secrets

The safest local path is to use the Vault UI after logging in:

```text
http://127.0.0.1:8200
```

For command-line writes, avoid putting real private keys into shell history. Placeholder example:

```bash
docker compose exec vault vault kv put kv/hyperliquid-trading-agent/prod \
  DISCORD_BOT_TOKEN='<discord-token>' \
  OPENROUTER_API_KEY='<openrouter-key>' \
  ALPACA_API_KEY='<alpaca-key>' \
  ALPACA_API_SECRET='<alpaca-secret>' \
  HYPERLIQUID_ACCOUNT_ADDRESS='<0x-public-address>' \
  HYPERLIQUID_PRIVATE_KEY='<0x-private-key>'
```

Read back metadata and keys:

```bash
docker compose exec vault vault kv get kv/hyperliquid-trading-agent/prod
```

## Let The App Hydrate Env From Vault

The app remains environment-variable driven. When `VAULT_ENABLED=true`, startup reads one KV object and inserts valid uppercase keys into the process environment before `Settings` is built.

In `.env` for Compose:

```env
VAULT_ENABLED=true
VAULT_ADDR=http://vault:8200
VAULT_KV_MOUNT=kv
VAULT_KV_VERSION=2
VAULT_SECRET_PATH=hyperliquid-trading-agent/prod
VAULT_ENV_OVERRIDE=false
VAULT_TOKEN=<read-only-agent-token>
```

`VAULT_ENV_OVERRIDE=false` means already-present environment values win. Empty values can still be filled from Vault. Set `VAULT_ENV_OVERRIDE=true` only when you explicitly want Vault to replace non-empty local values.

Start Vault and the API/worker services together:

```bash
docker compose --profile vault up -d vault api newswire world-model
```

Only `api` exposes the dashboard/API port. Vault and service-role workers expose no app dashboard ports.

## Read-Only App Token

Do not run the app with the initial root token. Create a narrow policy:

```bash
docker compose exec -T vault vault policy write hyperliquid-trading-agent - <<'HCL'
path "kv/data/hyperliquid-trading-agent/prod" {
  capabilities = ["read"]
}
HCL
```

Create a renewable token for the app:

```bash
docker compose exec vault vault token create -policy=hyperliquid-trading-agent -period=24h
```

Put that token in `.env` as `VAULT_TOKEN`, or mount it as a file and set:

```env
VAULT_TOKEN_FILE=/run/secrets/vault_token
```

## Production Notes

For production, replace the local file-storage setup with a hardened Vault deployment:

- TLS enabled and enforced.
- Auto-unseal through KMS/HSM.
- Audit devices enabled.
- No app usage of root tokens.
- Separate policies per environment and subsystem.
- Regular rotation for model, newswire, exchange, and operator tokens.
- A separate live-execution security review before any private key can sign orders.
