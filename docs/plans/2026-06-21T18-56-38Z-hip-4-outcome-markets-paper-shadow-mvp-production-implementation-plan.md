---
created: 2026-06-21T18:56:38.540Z
source: pi-plan-mode
status: accepted-for-execution
---

# HIP-4 Outcome Markets Paper/Shadow MVP — Production Implementation Plan

## 1. Executive Decision

Implement HIP-4 as a new isolated bounded subsystem at:

```text
hyperliquid_trading_agent/app/hip4/
```

This pass is **read-only, shadow, and paper only**.

Hard prohibitions:
- No signing.
- No private keys.
- No `/exchange` mutation.
- No live orders.
- No SDK `Exchange` instantiation.
- No LLM-controlled execution.
- No promotion into autonomy or the perps/institutional engine.

HIP-4 may integrate only through:
- `Settings`
- repository/persistence
- FastAPI routes
- metrics
- existing Hyperliquid WebSocket worker
- Discord reporting
- deterministic `RiskGateway`

The MVP ships in small gated milestones: capability probes → read-only registry → normalized market data → scanner math → paper ledger → risk integration → persistence/routes/reporting/tests.

---

## 2. What Changes From The Original Plan

Critical corrections:

1. **No hardcoded migration name**
   - Replace `0015_hip4_outcomes.py` with “next Alembic revision after inspecting current head.”
   - Current observed head appears to be `0014_model_registry_retention`, but implementation must re-check.

2. **Capability probes before relying on HIP-4 API shape**
   - The original plan assumed `questions`, `outcomeMetaUpdates`, `quoteToken`, and question mechanics are stable. These must be probed and capability-gated.

3. **Separate freshness thresholds**
   - Replace one global book staleness threshold with separate registry, scan, paper-execution, and manual-ticket thresholds.

4. **Manual ticket export is non-executable**
   - No exchange-ready JSON, nonce, signature fields, or directly signable payloads.
   - Route is disabled and not registered by default.

5. **Scanner is narrower and safer**
   - Risk-free candidates must prove zero residual inventory using a balance-vector simulation.
   - Inventory-carrying and market-making are shadow-only unless explicitly enabled later.

6. **No implicit lot/tick-size assumptions**
   - If HIP-4 size/tick metadata is unavailable, executable paper simulation degrades or disables depending on config.

7. **Raw API payload persistence is mandatory**
   - Every metadata parse must preserve raw source JSON with schema hash/version for replay/debugging.

8. **Route-level protections are explicit**
   - Paper/reconcile/manual routes require bearer auth, `HIP4_ENABLED=true`, and route-specific enablement flags.

9. **RiskGateway integration is explicit**
   - Add a deterministic HIP-4 risk check path; do not route HIP-4 through LLM/autonomy/perps execution.

10. **Market maker is not part of this MVP execution**
   - This pass may prepare schemas and shadow signals, but no automated quoting or order placement.

---

## 3. Verified Facts, Assumptions, and Capability Probes

### Verified repo facts from inspection

Observed current integration points:
- `Settings` lives in `hyperliquid_trading_agent/app/config.py`.
- FastAPI app assembly is in `hyperliquid_trading_agent/app/main.py`.
- Read-only Hyperliquid REST client is `app/hyperliquid/client.py`.
- Existing dynamic WebSocket fan-out worker is `app/hyperliquid/ws_worker.py`.
- Metrics live in `app/metrics.py` with names like `hyperliquid_trading_agent_*`.
- DB models live in `app/db/models.py`; repository facade lives in `app/db/repository.py`.
- Alembic versions live under `alembic/versions/`; current observed latest file is `0014_model_registry_retention.py`.
- `RiskGateway` lives in `app/governance/risk_gateway.py`.
- Tests use `pytest`, `Settings(environment="test")`, `TestClient`, and fake repositories.

### `Hip4CapabilityProbe`

Create schema:

```python
Hip4CapabilityProbe:
  network: "mainnet" | "testnet"
  probed_at_ms: int
  outcome_meta_available: bool
  outcome_meta_error: str | None
  outcome_meta_top_level_keys: list[str]
  outcome_meta_schema_hash: str | None

  supports_outcomes: bool
  supports_questions: bool
  supports_question_fields: bool
  question_fields_seen: list[str]
  missing_question_fields: list[str]

  supports_outcome_meta_ws: bool
  outcome_meta_ws_status: "confirmed" | "unconfirmed" | "unsupported" | "disabled"

  supports_quote_token: bool
  quote_tokens_seen: list[str]

  supports_authoritative_size_metadata: bool
  size_metadata_source: "meta" | "spotMeta" | "outcomeMeta" | "inferred" | "unknown"
  supports_authoritative_tick_metadata: bool
  tick_metadata_source: "meta" | "spotMeta" | "outcomeMeta" | "inferred" | "unknown"

  supports_native_action_modeling: bool
  supports_question_mechanics: bool
  supports_manual_ticket_export: bool

  docs_scope_status: "verified_not_testnet_only" | "testnet_only" | "unknown"
  undocumented_fields: dict[str, list[str]]
  network_dependent_fields: list[str]
  degraded_reasons: list[str]
```

### Required probes and graceful degradation

| Probe | Capability flag | If false / unstable |
|---|---|---|
| `outcomeMeta` available on selected network | `outcome_meta_available` | HIP-4 service starts degraded; no registry refresh, scanner, paper, or manual routes beyond status. |
| `outcomeMeta` contains `outcomes` | `supports_outcomes` | Disable all HIP-4 features except degraded status. |
| `outcomeMeta` contains `questions` | `supports_questions` | Binary outcome-only mode. Disable complete-set question arbitrage. |
| `question`, `fallbackOutcome`, `namedOutcomes`, `settledNamedOutcomes` stable | `supports_question_fields` | Disable question mechanics and question routes that require complete sets. |
| WS `outcomeMetaUpdates` confirmed | `supports_outcome_meta_ws` | Use REST polling only. |
| `quoteToken` present | `supports_quote_token` | Registry can display metadata; scanner and paper PnL disabled. |
| Lot/tick metadata authoritative | `supports_authoritative_size_metadata`, `supports_authoritative_tick_metadata` | Shadow-only unless `HIP4_ALLOW_INFERRED_LOT_SIZE_FOR_PAPER=true`; manual ticket disabled. |
| Native action schemas stable enough to model | `supports_native_action_modeling` | Disable paper conversion actions and scanner execution candidates. |
| Official docs not testnet-only | `docs_scope_status` | If `testnet_only` on mainnet, disable mainnet HIP-4 features. |
| Undocumented/network-dependent fields found | `undocumented_fields`, `network_dependent_fields` | Preserve raw JSON, warn in status/Discord, avoid relying on those fields. |

### Assumptions to Re-Verify Before Coding

- Installed `hyperliquid-python-sdk` still lacks first-class HIP-4 helpers.
- `outcomeMeta` remains a documented `/info` request.
- `settledOutcome` remains a documented read-only `/info` request.
- `l2Book` works for `#<encoding>` HIP-4 symbols on selected network.
- `outcomeMetaUpdates` WebSocket support is not assumed until confirmed.
- `quoteToken` usually names USDC, but code must not assume USDC.
- HIP-4 action JSON must not be represented as an exchange-ready payload in this MVP.
- Alembic head must be inspected immediately before adding migration.

---

## 4. Repo Integration Points To Inspect First

Before writing code, inspect these exact files and conventions:

1. `hyperliquid_trading_agent/app/config.py`
   - Settings naming, validators, CSV parsing helpers, safe exchange-disabled validators.

2. `hyperliquid_trading_agent/app/main.py`
   - `create_app`, lifespan service construction, route registration, app state patterns.

3. `hyperliquid_trading_agent/app/hyperliquid/client.py`
   - `/info` allowlist, weight limiter, cache TTL, metrics labels.

4. `hyperliquid_trading_agent/app/hyperliquid/ws_worker.py`
   - `SubscriptionSpec`, callback lifecycle, reconnect behavior, identifier routing.

5. `hyperliquid_trading_agent/app/db/models.py`
   - SQLAlchemy model style, indexes, JSON naming, timestamp conventions.

6. `hyperliquid_trading_agent/app/db/repository.py`
   - Async session patterns, `repository.enabled`, best-effort writes.

7. `alembic/versions/*`
   - Determine current head dynamically. Do not hardcode `0015`.

8. `hyperliquid_trading_agent/app/metrics.py`
   - Metric naming and label style.

9. `hyperliquid_trading_agent/app/governance/risk_gateway.py`
   - Add/route deterministic HIP-4 risk checks without relaxing existing gates.

10. Discord style:
    - `app/autonomy/discord.py`
    - `app/newswire/consumers/discord_news.py`
    - `app/discord_bot.py`

11. Tests:
    - `tests/test_engine_routes.py`
    - `tests/test_runtime_components.py`
    - existing fake repository patterns.

---

## 5. Revised Package/File Layout

Add:

```text
hyperliquid_trading_agent/app/hip4/
  __init__.py
  schemas.py
  ids.py
  mechanics.py
  capabilities.py
  client.py
  registry.py
  orderbook.py
  ws.py
  scanner.py
  paper.py
  risk.py
  routes.py
  discord.py
  service.py
```

Responsibilities:

- `schemas.py`: Pydantic models, Decimal-safe types, status DTOs.
- `ids.py`: encoding helpers for `#N`, `+N`, exchange asset IDs.
- `mechanics.py`: abstract paper-only split/merge/negate/mergeQuestion semantics.
- `capabilities.py`: `Hip4CapabilityProbe` and probe service.
- `client.py`: read-only HIP-4 `/info` wrapper around existing `HyperliquidClient`.
- `registry.py`: metadata refresh, raw payload hashing, normalized specs.
- `orderbook.py`: Decimal book parser, executable depth model.
- `ws.py`: centralized `Hip4WsSubscriptionManager`.
- `scanner.py`: risk-free scanner and shadow-only inventory candidates.
- `paper.py`: paper ledger and reconciliation.
- `risk.py`: HIP-4 deterministic pre-risk wrapper.
- `routes.py`: FastAPI route registration.
- `discord.py`: digest formatting.
- `service.py`: lifecycle orchestration.

Minimal shared-boundary changes:
- `config.py`: add HIP-4 settings.
- `metrics.py`: add HIP-4 metrics.
- `hyperliquid/client.py`: add read-only `outcomeMeta` / `settledOutcome` info types and helper methods.
- `hyperliquid/ws_worker.py`: only extend generic subscription/message identifiers if needed.
- `main.py`: construct `Hip4Service`, store in `app.state.hip4_service`, register routes.
- `db/models.py`, `db/repository.py`, Alembic migration: persistence.
- `governance/risk_gateway.py`: add deterministic HIP-4 check method or clearly scoped wrapper call.

---

## 6. Config and Feature Flags

Add settings with safe defaults:

```python
hip4_enabled: bool = False
hip4_mode: Literal["read_only", "shadow", "paper_shadow"] = "paper_shadow"

hip4_scan_enabled: bool = False
hip4_paper_execution_enabled: bool = False
hip4_manual_ticket_export_enabled: bool = False

hip4_question_allowlist: str = ""
hip4_max_questions: int = 25
hip4_max_hot_questions: int = 10
hip4_max_hot_outcome_sides: int = 120
hip4_include_partially_settled: bool = False

hip4_outcome_meta_refresh_seconds: int = 60
hip4_settlement_refresh_seconds: int = 300

hip4_registry_max_staleness_ms: int = 300_000
hip4_scan_max_book_staleness_ms: int = 10_000
hip4_paper_execution_max_book_staleness_ms: int = 5_000
hip4_manual_ticket_max_book_staleness_ms: int = 3_000

hip4_ws_enabled: bool = True
hip4_probe_outcome_meta_ws: bool = False
hip4_ws_max_subscriptions: int = 150
hip4_ws_resnapshot_on_reconnect: bool = True

hip4_min_edge_bps: Decimal = Decimal("25")
hip4_min_edge_usd: Decimal = Decimal("10")
hip4_edge_threshold_mode: Literal["both", "either"] = "both"

hip4_min_depth_usd: Decimal = Decimal("250")
hip4_max_paper_notional_per_candidate_usd: Decimal = Decimal("10000")
hip4_max_paper_daily_notional_usd: Decimal = Decimal("100000")
hip4_paper_initial_equity_usd: Decimal = Decimal("100000")

hip4_outcome_taker_fee_bps: Decimal = Decimal("0")
hip4_outcome_maker_fee_bps: Decimal = Decimal("0")
hip4_fee_stress_bps: Decimal = Decimal("10")

hip4_allow_inventory_carry: bool = False
hip4_allow_inferred_lot_size_for_paper: bool = False

hip4_discord_digest_enabled: bool = True
hip4_discord_digest_interval_seconds: int = 300
hip4_alert_channel_id: str = ""
```

Add helper properties:
- `hip4_question_allowlist_ids: set[int]`
- `hip4_alert_channel_configured: bool`

Safety validators:
- Reject any future HIP-4 setting that enables signing/exchange mutation.
- Do not add private-key settings.
- Do not add `/exchange` endpoint settings.

---

## 7. Core Schemas and Persistence Model

### Core schemas

Use `Decimal` only for price, size, notional, PnL, and fees.

Key models:
- `OutcomeAssetId`
- `OutcomeSpec`
- `QuestionSpec`
- `Hip4CapabilityProbe`
- `NormalizedOutcomeBook`
- `ExecutableLeg`
- `Hip4Candidate`
- `OutcomeOrderIntent`
- `PaperNativeAction`
- `Hip4PaperFill`
- `Hip4PaperPortfolio`
- `Hip4RiskDecision`

### Asset ID utilities

Tests must verify:

```text
outcome_id=172, side=0
encoding = 1720
coin = "#1720"
balance_token = "+1720"
asset_id = 100001720
```

### Persistence

Add the next Alembic migration after inspecting current head.

Tables:
1. `hip4_capability_probes`
2. `hip4_raw_payloads`
3. `hip4_outcome_specs`
4. `hip4_question_specs`
5. `hip4_market_snapshots`
6. `hip4_edge_candidates`
7. `hip4_paper_portfolios`
8. `hip4_paper_positions`
9. `hip4_paper_actions`
10. `hip4_paper_fills`
11. `hip4_reconciliation_runs`
12. `hip4_settlements`

Requirements:
- Store raw API payloads with:
  - `source`
  - `network`
  - `payload_json`
  - `schema_hash`
  - `schema_version`
  - `observed_at_ms`
- Use `Numeric` or stringified Decimal columns only.
- No `Float` columns for HIP-4 price, size, edge, fees, or PnL.
- Add indexes for:
  - `question_id`
  - `outcome_id`
  - `as_of_ms`
  - `status`
  - `candidate_id`
  - `schema_hash`
- Migration must have reversible downgrade.
- Existing tables must be unaffected.

---

## 8. Market Data and WebSocket Design

### Centralized manager

Add `Hip4WsSubscriptionManager`.

It must:
- Reuse existing `HyperliquidWebSocketWorker`.
- Deduplicate subscriptions.
- Enforce `hip4_ws_max_subscriptions`.
- Avoid one-connection-per-market.
- Prioritize top-N hot questions/outcomes.
- Resnapshot via REST after reconnect.
- Track freshness per coin and per question.
- Mark stale books unusable for scanner/paper execution.

### Subscription priority

Priority order:
1. Allowlisted questions.
2. Questions with active candidates.
3. Highest observed liquidity/depth.
4. Highest recent update/activity.
5. Remaining markets until cap.

### REST fallback

If `supports_outcome_meta_ws=false`:
- Use REST polling for metadata.
- Continue using REST `l2Book` snapshots for low-priority markets.
- Scanner may run only on books with fresh snapshots.

### Book normalization

Maintain:
- Raw book per `#<encoding>`.
- Canonical side0 display book.
- Direct side0/side1 executable books for strategy simulation.

Do not rely on midpoint for executable candidates. Use actual bid/ask depth.

---

## 9. Scanner Strategy and Math Invariants

### Decimal-only invariant

HIP-4 scanner and paper ledger must not use float for:
- prices
- sizes
- notionals
- PnL
- fees
- edge calculations

Tests must fail if scanner/paper code converts these values through float.

### Edge threshold semantics

Default acceptance requires:

```text
edge_bps >= HIP4_MIN_EDGE_BPS
AND
edge_usd >= HIP4_MIN_EDGE_USD
```

Only if `hip4_edge_threshold_mode="either"` may OR semantics be used. Default is `both`.

### Executable depth model

For every candidate:
- Consume actual orderbook depth.
- Compute weighted average execution price.
- Require equal-leg size for complete-set strategies.
- Reject partial-depth opportunities.
- Quantize size only with authoritative size metadata, unless inferred sizing is explicitly enabled for paper.

### Risk-free strategies allowed for paper

1. **Binary split-sell**
   - Split quote into side0 + side1.
   - Sell equal size into direct side0 and side1 bids.
   - Proof: final token inventory vector is zero.

2. **Binary buy-merge**
   - Buy equal side0 and side1 from asks.
   - Merge into quote.
   - Proof: final token inventory vector is zero.

3. **Question complete-set sell**
   - Split one seed outcome.
   - Negate side1 into side0 shares of all other outcomes.
   - Sell equal side0 size across every outcome.
   - Requires `supports_questions=true` and `supports_question_mechanics=true`.
   - Proof: final token inventory vector is zero.

4. **Question complete-set buy-merge**
   - Buy equal side0 shares across all outcomes.
   - Merge question into quote.
   - Requires `supports_questions=true` and `supports_question_mechanics=true`.
   - Proof: final token inventory vector is zero.

### Shadow-only strategies

Inventory-carrying opportunities:
- May be reported.
- May not be paper-executed unless `hip4_allow_inventory_carry=true`.
- Must be labeled `shadow_inventory_carry`, not `risk_free`.

Market making:
- Not implemented in this MVP.
- No quote placement simulation beyond future design placeholders.

---

## 10. Paper Ledger and Reconciliation

### Ledger actions

Paper-only action types:
- `BUY_SIDE_TOKEN`
- `SELL_SIDE_TOKEN`
- `SPLIT_OUTCOME`
- `MERGE_OUTCOME`
- `NEGATE_OUTCOME`
- `MERGE_QUESTION`
- `SETTLE_OUTCOME`
- `MARK_TO_BOOK`

These are abstract paper actions, not exchange payloads.

### Ledger invariants

Paper execution must:
- Never create negative quote balance.
- Never create negative token balance.
- Never create balances from nowhere.
- Validate every conversion against available balances.
- Preserve complete-set roundtrips minus modeled fees/costs.
- Reject residual inventory for risk-free candidates.
- Store every action and fill with input/output balance deltas.

### PnL

Track separately:
- realized PnL
- unrealized mark-to-book PnL
- settlement PnL
- modeled fees
- fee stress
- residual inventory value

Settlement-aware PnL must not be conflated with mark-to-mid PnL.

### Reconciliation

Implement paper reconciliation only:
- Rebuild balances from action ledger.
- Compare rebuilt balances against stored positions.
- Record discrepancies in `hip4_reconciliation_runs`.
- Do not query or infer live account balances.

---

## 11. Deterministic Risk Rules

Add HIP-4-specific pre-risk checks in `app/hip4/risk.py`, then call the existing deterministic `RiskGateway`.

Reject if:
- `hip4_enabled=false`.
- Candidate mode is not `shadow` or `paper`.
- Any signed payload, nonce, private-key material, `/exchange` body, or `exchange_actions` is present.
- Registry is stale beyond `hip4_registry_max_staleness_ms`.
- Scan book is stale beyond `hip4_scan_max_book_staleness_ms`.
- Paper execution book is stale beyond `hip4_paper_execution_max_book_staleness_ms`.
- Manual ticket data is stale beyond `hip4_manual_ticket_max_book_staleness_ms`.
- Market is settled or partially settled and `hip4_include_partially_settled=false`.
- Required capability flag is false.
- Edge fails threshold semantics.
- Notional exceeds per-candidate or daily paper caps.
- Risk-free proof leaves residual inventory.
- Inventory-carrying execution requested while disabled.
- Manual ticket export requested while disabled.
- Any HIP-4 code attempts to mutate risk config.

Risk decisions must include structured reject reasons and be persisted through existing risk decision patterns.

---

## 12. API Routes and Discord Reporting

### Route registration

Register routes via:

```python
register_hip4_routes(app, settings, _require_agent_api)
```

Rules:
- `/hip4/status` may return disabled/degraded status.
- All other routes fail closed when `HIP4_ENABLED=false`.
- Paper/reconcile/manual routes require bearer auth and specific config enablement.
- Manual-ticket route is not registered unless both config and capability allow it.

### Routes

Read/status:
- `GET /hip4/status`
- `GET /hip4/capabilities`
- `GET /hip4/outcomes`
- `GET /hip4/questions`
- `GET /hip4/questions/{question_id}`
- `GET /hip4/books`
- `GET /hip4/edges`
- `GET /hip4/paper/portfolio`
- `GET /hip4/paper/actions`

Mutation-like paper/admin:
- `POST /hip4/scan/run`
- `POST /hip4/paper/execute/{candidate_id}`
- `POST /hip4/reconcile/run`

Manual ticket:
- `POST /hip4/manual-ticket/{candidate_id}`
- Disabled and unregistered by default.
- Returns human-readable instructions only.

### Non-executable manual ticket format

Allowed fields:
- candidate summary
- market/question names
- intended human operation in prose
- coin symbols for human reference
- size/price limits as text
- freshness timestamps
- risk reject/allow summary
- checklist for operator review

Forbidden fields:
- `signature`
- `nonce`
- private key references
- exact `/exchange` path
- exact `userOutcome` action JSON
- directly postable request body

### Discord digest

Include:
- capability/degraded status
- stale-data warnings
- top candidates
- rejected edge reasons
- paper PnL and inventory
- settlement warnings
- quote token warnings
- undocumented field warnings

Do not post only profitable candidates.

---

## 13. Tests and Replay Fixtures

Add tests:

```text
tests/test_hip4_ids.py
tests/test_hip4_capabilities.py
tests/test_hip4_registry.py
tests/test_hip4_orderbook.py
tests/test_hip4_scanner.py
tests/test_hip4_paper.py
tests/test_hip4_risk.py
tests/test_hip4_routes.py
tests/test_hip4_persistence.py
```

Fixtures:

```text
tests/fixtures/hip4/
  outcome_meta_with_questions.json
  outcome_meta_outcomes_only.json
  outcome_meta_missing_quote_token.json
  l2_book_side0.json
  l2_book_side1.json
  settled_outcome.json
  partial_depth_books.json
  stale_book_snapshot.json
```

Required coverage:
- unavailable `outcomeMeta`
- missing `questions`
- unstable/missing question fields
- missing WebSocket outcome updates
- missing `quoteToken`
- missing lot/tick metadata
- stale registry
- stale books
- partial depth
- partial settlement
- disabled manual-ticket route
- no negative paper inventory
- Decimal-only scanner and ledger math
- raw payload schema hash persistence
- risk-free candidate residual inventory proof
- route auth/config fail-closed behavior
- no exchange-ready manual-ticket payload
- static guard test: HIP-4 code does not import SDK `Exchange`, signing helpers, or private-key settings.

---

## 14. Rollout, Rollback, and Kill Switches

### Milestone 0: Repo audit and capability probe design

Deliverables:
- Exact integration file list.
- Dynamic Alembic head detection procedure.
- `Hip4CapabilityProbe` schema.
- Fail-closed behavior matrix.

Acceptance:
- No code assumes HIP-4 API availability.
- No implementation starts from hardcoded migration name.
- API unavailable means degraded status, not startup failure.

### Milestone 1: Read-only registry

Deliverables:
- Raw `outcomeMeta` fetch.
- Raw JSON persisted with schema hash/version.
- Normalized `OutcomeSpec` and `QuestionSpec`.
- Asset ID utility tests.

Acceptance:
- Service starts with `HIP4_ENABLED=false`.
- Service starts with `HIP4_ENABLED=true` even if HIP-4 API is unavailable.
- Unavailable API produces degraded status, not startup failure.
- Parsed metadata preserves raw source JSON.
- No scanner or paper execution yet.

### Milestone 2: Market-data normalization

Deliverables:
- `Hip4WsSubscriptionManager`.
- REST snapshot fallback.
- Subscription dedupe.
- Reconnect and resnapshot behavior.
- Per-market freshness tracking.
- Top-N subscription budget.

Acceptance:
- No one-connection-per-market design.
- Subscriptions capped by config.
- Reconnect produces clean resync.
- Stale books are marked unusable for execution simulation.

### Milestone 3: Scanner math and executable depth model

Deliverables:
- Decimal-only math.
- Executable depth sizing.
- Equal-leg sizing invariant.
- Residual-inventory invariant.
- Fee-stress model.
- Risk-free vs inventory-carrying split.

Acceptance:
- Risk-free candidate proves zero residual inventory.
- Partial-depth opportunities rejected.
- Edge uses executable depth, not midpoint.
- Default edge threshold requires both bps and USD minimums.

### Milestone 4: Paper ledger

Deliverables:
- Action ledger.
- Deterministic book-simulated fills.
- No negative inventory.
- Settlement-aware PnL.
- Reconciliation records.
- Daily/per-candidate notional counters.

Acceptance:
- Paper execution cannot create balances from nowhere.
- Complete-set roundtrips preserve quote minus modeled costs.
- Risk-free residual inventory is impossible.
- Inventory-carrying remains shadow-only unless explicitly enabled.

### Milestone 5: RiskGateway integration

Deliverables:
- HIP-4 pre-risk checks.
- Existing deterministic RiskGateway call.
- Structured reject reasons.
- Separate freshness thresholds.

Acceptance:
- Stale data rejects paper execution simulation.
- Settled/partial markets reject unless explicitly allowed.
- Manual ticket export rejects unless separately enabled.
- HIP-4 cannot mutate risk config.

### Milestone 6: Persistence and migrations

Deliverables:
- Next Alembic migration after current head.
- Reversible migration.
- HIP-4 tables and indexes.
- Numeric/string Decimal storage only.

Acceptance:
- Upgrade/downgrade passes.
- Existing tables unaffected.
- No HIP-4 float columns.
- Raw API payloads stored for replay/debugging.

### Milestone 7: API routes and Discord reporting

Deliverables:
- Safe read routes.
- Auth/config-gated paper/reconcile/manual routes.
- Non-executable manual ticket.
- Discord digest with stale warnings and rejects.

Acceptance:
- Routes fail closed when disabled.
- Paper actions require auth.
- Manual ticket export disabled by default.
- Discord includes rejects and degraded status.

### Milestone 8: Tests, replay, and runbook

Deliverables:
- Unit tests.
- Integration tests.
- Replay fixtures.
- Failure-mode tests.
- Degraded-mode tests.
- Operator runbook.

Acceptance:
- All required scenarios in section 13 pass.
- Runbook explains enable/disable, degraded modes, and kill switches.
- No live-capable code is introduced.

### Rollback and kill switches

Rollback:
- Set `HIP4_ENABLED=false`.
- Stop HIP-4 background tasks.
- Leave persisted tables intact.
- Routes return disabled/degraded.
- No impact on autonomy/perps/engine.

Kill switches:
- `HIP4_ENABLED=false`
- `HIP4_SCAN_ENABLED=false`
- `HIP4_PAPER_EXECUTION_ENABLED=false`
- `HIP4_MANUAL_TICKET_EXPORT_ENABLED=false`
- Existing `HYPERLIQUID_EXCHANGE_ENABLED` must remain false.

---

## 15. Out of Scope

- Live orders.
- Signed actions.
- Private keys.
- `/exchange` calls.
- SDK `Exchange`.
- Directly executable `userOutcome` payload generation.
- Mainnet or testnet live trading.
- LLM execution control.
- Risk config mutation.
- Autonomy/perps engine promotion.
- Automated market making.
- Account balance reconciliation against live wallet state.

---

## 16. Open Questions Before Implementation

No product decisions remain open. These are verification tasks, not user decisions:

1. Re-check current Alembic head before migration.
2. Re-probe `outcomeMeta` on configured network.
3. Confirm whether `questions` and question fields are present.
4. Confirm whether `outcomeMetaUpdates` is available or must degrade to REST polling.
5. Confirm `quoteToken` names and whether non-USDC quote tokens exist.
6. Confirm authoritative size/tick metadata source.
7. Confirm official docs do not mark relevant HIP-4 endpoints testnet-only.
8. Confirm installed SDK still lacks HIP-4 helpers and avoid adding `Exchange`.
9. Confirm all undocumented API fields are stored raw but not relied upon.

---

## 17. Final Implementation Checklist

1. Perform repo audit and define `Hip4CapabilityProbe`.
2. Add HIP-4 settings, metrics, and read-only `/info` helpers.
3. Implement `app/hip4` schemas, IDs, mechanics, and capability probes.
4. Implement read-only registry with raw payload persistence.
5. Implement market-data normalization and `Hip4WsSubscriptionManager`.
6. Implement Decimal-only scanner with executable depth and invariants.
7. Implement paper ledger, action log, and reconciliation.
8. Integrate deterministic HIP-4 risk checks with `RiskGateway`.
9. Add persistence models, repository methods, and next Alembic migration.
10. Add guarded API routes and Discord digest.
11. Add tests, replay fixtures, degraded-mode cases, and runbook.
12. Verify no signing, private keys, `/exchange`, live orders, or LLM-controlled execution were introduced.





<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [ ] 1. Perform repo audit and define Hip4CapabilityProbe. _(pending)_
- [ ] 2. Add HIP-4 settings, metrics, and read-only /info helpers. _(pending)_
- [ ] 3. Implement app/hip4 schemas, IDs, mechanics, and capabilit... _(pending)_
- [ ] 4. Implement read-only registry with raw payload persistence. _(pending)_
- [ ] 5. Implement market-data normalization and Hip4WsSubscriptio... _(pending)_
- [ ] 6. Implement Decimal-only scanner with executable depth and ... _(pending)_
- [ ] 7. Implement paper ledger, action log, and reconciliation. _(pending)_
- [ ] 8. Integrate deterministic HIP-4 risk checks with RiskGateway. _(pending)_
- [ ] 9. Add persistence models, repository methods, and next Alem... _(pending)_
- [ ] 10. Add guarded API routes and Discord digest. _(pending)_
- [ ] 11. Add tests, replay fixtures, degraded-mode cases, and runb... _(pending)_
- [ ] 12. Verify no signing, private keys, /exchange, live orders, ... _(pending)_

<!-- pi-plan-progress:end -->
