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

## Safe Local Bootstrap

The repository includes an idempotent, local-address-only bootstrap command. It initializes a fresh Vault, unseals it, enables KV v2, seeds an allowlisted set of secret values from `.env`, creates a read-only policy/token, and prints only key names and counts:

```bash
python -m hyperliquid_trading_agent.app.vault_admin status
python -m hyperliquid_trading_agent.app.vault_admin bootstrap-local
```

The bootstrap stores local development recovery material and the application token separately:

```text
.local/vault/admin/dev-credentials.json  # unseal key and root token; never mounted into apps
.local/vault/app/token                   # read-only token; mounted at /run/secrets/vault/token
```

Both files are ignored by Git and written atomically with mode `0600`. Only `.gitkeep` placeholders are tracked. This is a development convenience, not a production secret-management design. Back up recovery material outside the repository if the local Vault data matters.

The default bootstrap secret is `kv/hyperliquid-trading-agent/local`. Configure the app to read the same path:

```env
VAULT_ENABLED=true
VAULT_ADDR=http://vault:8200
VAULT_KV_MOUNT=kv
VAULT_KV_VERSION=2
VAULT_SECRET_PATH=hyperliquid-trading-agent/local
VAULT_ENV_OVERRIDE=false
VAULT_TOKEN=
VAULT_TOKEN_FILE=/run/secrets/vault/token
```

Then recreate the migration and application services so the read-only token mount and Vault settings take effect:

```bash
docker compose up -d --build api newswire world-model trader agent scheduler
```

Verify without exposing credentials:

```bash
python -m hyperliquid_trading_agent.app.vault_admin status
docker compose ps
curl -fsS http://127.0.0.1:${HOST_PORT:-8081}/health
curl -fsS http://127.0.0.1:${HOST_PORT:-8081}/ready
```

Application startup now reports a specific sealed/uninitialized diagnostic instead of a generic Vault HTTP error.

## Lost Local Unseal Key

An initialized 1-of-1 Vault cannot be unsealed without its key. `bootstrap-local` detects an initialized Vault with missing local recovery credentials and stops. It never resets a volume.

Inspect the proposed recovery sequence with:

```bash
python -m hyperliquid_trading_agent.app.vault_admin reset-plan
```

The output names the one affected `vault_data` volume and marks its removal as destructive. Obtain separate explicit approval before running that volume-removal command, and first confirm there is no recoverable unseal key or snapshot. Stop dependent services before any approved reset, bootstrap the replacement Vault, verify reads with the narrow token, and only then re-enable applications.

## Initialize And Unseal

The safe bootstrap above is preferred for local development. The following manual procedure is retained for operators who manage recovery material outside the repository.

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
VAULT_SECRET_PATH=hyperliquid-trading-agent/local
VAULT_ENV_OVERRIDE=false
VAULT_TOKEN=
VAULT_TOKEN_FILE=/run/secrets/vault/token
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

Prefer the Compose-mounted token created by `bootstrap-local`. For a separately managed token file, set:

```env
VAULT_TOKEN_FILE=/run/secrets/vault/token
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
