# HIP-4 Outcome Markets Paper/Shadow Runbook

This subsystem lives under `hyperliquid_trading_agent/app/hip4` and is intentionally bounded to read-only, shadow, and paper behavior.

## Safety posture

Hard prohibitions for this MVP:

- No signing.
- No private keys.
- No Hyperliquid `/exchange` mutation calls.
- No live orders.
- No SDK `Exchange` client.
- No LLM-controlled execution.
- No auto-promotion into autonomy or the perps/institutional engine.

## Default state

All HIP-4 behavior is off by default:

```env
HIP4_ENABLED=false
HIP4_SCAN_ENABLED=false
HIP4_PAPER_EXECUTION_ENABLED=false
HIP4_MANUAL_TICKET_EXPORT_ENABLED=false
HIP4_PROACTIVE_LOOP_ENABLED=false
HIP4_PROACTIVE_PAPER_EXECUTION_ENABLED=false
HYPERLIQUID_EXCHANGE_ENABLED=false
```

`GET /hip4/status` remains available and returns disabled/degraded status. Other HIP-4 routes fail closed when `HIP4_ENABLED=false`.

## Safe local shadow startup

```env
HIP4_ENABLED=true
HIP4_MODE=shadow
HIP4_SCAN_ENABLED=true
HIP4_PAPER_EXECUTION_ENABLED=false
HIP4_MANUAL_TICKET_EXPORT_ENABLED=false
HIP4_PROACTIVE_LOOP_ENABLED=true
HIP4_PROACTIVE_PAPER_EXECUTION_ENABLED=false
HIP4_QUESTION_ALLOWLIST=32
```

Mode gates are enforced independently from feature flags:

- `HIP4_MODE=read_only`: status, registry, books only; scan/paper/reconcile/manual routes reject.
- `HIP4_MODE=shadow`: registry, books, scan only; paper/reconcile/manual routes reject.
- `HIP4_MODE=paper_shadow`: scan plus paper routes may run, but only when their specific feature flags and capability gates allow them.

Expected behavior:

1. Capability probe runs against `outcomeMeta`.
2. Raw metadata is persisted when repository is available.
3. Registry normalizes outcomes/questions if supported.
4. WebSocket subscriptions are capped and deduplicated; REST snapshots are used as fallback.
5. Scanner emits candidates only when capability and freshness checks allow.
6. Paper execution remains disabled unless explicitly enabled.

## Degraded modes

The service starts even when HIP-4 APIs are unavailable. `/hip4/status` reports degraded reasons such as:

- `outcome_meta_unavailable`
- `outcomes_missing`
- `questions_missing_binary_only`
- `question_fields_unstable`
- `quote_token_missing`
- `outcome_meta_ws_unconfirmed_rest_polling`
- `registry_stale`

Graceful degradation rules:

- Missing `outcomeMeta`: no registry/scanner/paper/manual features.
- Missing `questions`: binary outcome-only mode; question complete-set arbitrage disabled.
- Missing question fields: question mechanics disabled.
- Missing `outcomeMetaUpdates`: REST polling/snapshot fallback only.
- Missing `quoteToken`: scanner and paper PnL disabled through capability/risk checks.
- Mixed quote tokens in one question: question complete-set candidates reject.
- Missing authoritative size/tick metadata: paper execution rejects unless `HIP4_ALLOW_INFERRED_LOT_SIZE_FOR_PAPER=true`; manual tickets remain non-executable.
- Docs marked testnet-only on mainnet: abstract mechanics and manual tickets are disabled.

## Paper execution

Paper execution requires all of:

```env
HIP4_ENABLED=true
HIP4_SCAN_ENABLED=true
HIP4_PAPER_EXECUTION_ENABLED=true
```

Paper execution is still deterministic and risk-gated:

- stale registry/books reject execution;
- settled or partially settled questions reject unless explicitly allowed;
- residual inventory rejects risk-free strategies;
- daily and per-candidate notional caps apply;
- no live payload material is allowed in candidates.

## Manual ticket export

Disabled by default and conditionally registered only when:

```env
HIP4_MANUAL_TICKET_EXPORT_ENABLED=true
```

Manual tickets are non-executable human-readable instructions. They do not contain signatures, nonces, `/exchange` request bodies, or signable `userOutcome` JSON.

## Proactive shadow loop

The proactive loop is disabled by default and remains paper/shadow only. When enabled it repeatedly:

1. refreshes hot HIP-4 books through WebSocket plus REST fallback,
2. scans native conversion-arbitrage paths,
3. risk-classifies candidates through deterministic HIP-4 risk checks and `RiskGateway`,
4. optionally paper-executes only when both proactive paper and normal paper execution flags are enabled,
5. reconciles the paper ledger on cadence,
6. updates learning statistics and conservative operator recommendations,
7. posts Discord digests for new/high-edge opportunities, executions, PnL, and inventory.

Relevant settings:

```env
HIP4_PROACTIVE_LOOP_ENABLED=true
HIP4_PROACTIVE_LOOP_INTERVAL_SECONDS=30
HIP4_PROACTIVE_ALERT_MIN_EDGE_USD=10
HIP4_PROACTIVE_ALERT_MIN_EDGE_BPS=25
HIP4_PROACTIVE_ALERT_DEDUPE_SECONDS=300
HIP4_PROACTIVE_RECONCILE_INTERVAL_SECONDS=300
HIP4_PROACTIVE_LEARNING_ENABLED=true

# Explicit paper-only replay/execution tracking:
HIP4_MODE=paper_shadow
HIP4_PAPER_EXECUTION_ENABLED=true
HIP4_PROACTIVE_PAPER_EXECUTION_ENABLED=true
HIP4_PROACTIVE_MAX_PAPER_EXECUTIONS_PER_CYCLE=1
```

The strategy follows the native Outcome mechanics from the original cluster write-up:

- `binary_split_sell`: split quote token into YES/NO and sell both when bids imply more than 1 quote token.
- `binary_buy_merge`: buy YES/NO below 1 quote token and merge back.
- `question_complete_set_sell`: split a seed outcome, negate the NO into YES legs across the question, and sell the complete YES set when bids imply more than 1 quote token.
- `question_complete_set_buy`: buy one YES leg of every outcome and `mergeQuestion` back to quote token when asks imply less than 1 quote token.

Learning is observe/rank/recommend only. It does not mutate risk thresholds, sign orders, create `/exchange` actions, or promote HIP-4 into live autonomy.

## Operational routes

- `GET /hip4/status`
- `GET /hip4/capabilities`
- `GET /hip4/outcomes`
- `GET /hip4/questions`
- `GET /hip4/books`
- `POST /hip4/scan/run`
- `GET /hip4/edges`
- `GET /hip4/loop/status`
- `POST /hip4/loop/run-once`
- `GET /hip4/learning`
- `GET /hip4/paper/portfolio`
- `GET /hip4/paper/actions`
- `GET /hip4/paper/fills`
- `POST /hip4/paper/execute/{candidate_id}`
- `POST /hip4/reconcile/run`

## Discord reporting

Set `HIP4_ALERT_CHANNEL_ID` and keep `HIP4_DISCORD_DIGEST_ENABLED=true` to send status, candidates, rejects, proactive loop state, learning notes, paper executions, PnL, and inventory to the configured Discord channel.

## Rollback / kill switches

Immediate kill switch:

```env
HIP4_ENABLED=false
```

More granular stops:

```env
HIP4_SCAN_ENABLED=false
HIP4_PAPER_EXECUTION_ENABLED=false
HIP4_PROACTIVE_LOOP_ENABLED=false
HIP4_PROACTIVE_PAPER_EXECUTION_ENABLED=false
HIP4_MANUAL_TICKET_EXPORT_ENABLED=false
```

Rollback behavior:

- background subscriptions stop;
- routes return disabled/degraded;
- persisted HIP-4 tables remain for replay/debugging;
- autonomy, tracking, and institutional engine are unaffected.

## Final acceptance checklist

Before enabling beyond local shadow mode, confirm:

- `GET /hip4/status` shows expected mode permissions and no unexpected degraded reasons.
- Capability probe succeeded for `outcomeMeta`, `quoteToken`, and required question fields.
- `supports_user_outcome_action_json` remains false in this MVP.
- Scanner candidates use book timestamps in `proof.book_as_of_ms_by_coin`.
- Stale books do not produce executable candidates.
- Paper/manual routes reject without a successful capability probe.
- Manual tickets are non-executable and contain no nonce/signature/request body.
- `GET /hip4/loop/status` reports the expected enabled/running/cycle state.
- `GET /hip4/learning` shows observe/rank/recommend-only learning policy.
- `GET /hip4/paper/portfolio`, `/paper/actions`, and `/paper/fills` reflect paper-only executions and inventory.
- Discord digests include degraded status, reject reasons, proactive loop state, PnL, and learning notes.

## Validation commands

```bash
.venv/bin/pytest -q tests/test_hip4_*.py
.venv/bin/ruff check hyperliquid_trading_agent/app/hip4 tests/test_hip4_*.py
```
