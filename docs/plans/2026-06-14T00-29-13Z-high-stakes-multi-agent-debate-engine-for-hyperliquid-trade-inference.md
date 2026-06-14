---
created: 2026-06-14T00:29:13.971Z
source: pi-plan-mode
status: accepted-for-execution
---

# High-Stakes Multi-Agent Debate Engine for Hyperliquid Trade Inference

## Summary

Build a **LangGraph-powered high-stakes inference path** alongside the existing single-agent runner. It will run only for risk-routed trade/proposal/account-risk situations, use configurable per-role model chains, persist full structured audit trails, and produce **paper/autonomous trade proposals only**. It will not place signed Hyperliquid orders or enable mainnet execution.

## Locked Decisions

- Execution scope: **advisory + paper/autonomous proposal only**
- Orchestration: **LangGraph**
- Activation: **risk-routed**
- Models: **configurable per-role defaults**
- Interfaces: **extend `/ask` + add `/trade/proposals`**
- Account context: **prompt-supplied address + optional env allowlist**
- Audit: **full structured audit**
- Budget caps: **balanced: max 3 rounds, 90s timeout**

## Current Codebase Grounding

Existing repo state:

- `TradingAgentRunner` currently does heuristic tool gathering + one LLM call.
- `ModelGateway` already supports LiteLLM model fallback for OpenRouter/OpenAI/Anthropic/Kimi.
- Hyperliquid support is currently **read-only `/info` only**.
- `HYPERLIQUID_EXCHANGE_ENABLED=true` is rejected by config validation.
- Existing tools cover:
  - market snapshot
  - L2 book
  - candles
  - funding
  - public account state
  - fills
  - docs
  - news
  - paper trade simulation
- Persistence exists for audit events, tool calls, conversations, cache, news, and paper trades.
- No LangGraph dependency exists yet.

## Implementation Steps

1. Add high-stakes configuration, role model chains, and LangGraph dependency.
2. Extend `ModelGateway` for role-specific and structured JSON model calls.
3. Add high-stakes schemas, prompts, routing, and context-building modules.
4. Implement deterministic market/account feature builders.
5. Implement the LangGraph debate graph and role nodes.
6. Persist full decision runs, role outputs, state snapshots, and trade proposals.
7. Add `/trade/proposals` API endpoints and extend `/ask` response metadata.
8. Integrate the high-stakes path into `TradingAgentRunner`.
9. Add metrics, health/config visibility, and documentation.
10. Add unit/integration tests with fake models/tools and migration coverage.

## Detailed Design

### New package layout

Create:

```text
hyperliquid_trading_agent/app/agent/high_stakes/
  __init__.py
  schemas.py
  routing.py
  prompts.py
  context.py
  features.py
  json_io.py
  roles.py
  graph.py
  formatting.py
```

Add:

```text
hyperliquid_trading_agent/app/hyperliquid/validation.py
```

### Config additions

Add to `Settings`:

```env
HIGH_STAKES_DEBATE_ENABLED=false
HIGH_STAKES_ACTIVATION_POLICY=risk_routed
HIGH_STAKES_MAX_ROUNDS=3
HIGH_STAKES_TIMEOUT_SECONDS=90
HIGH_STAKES_MAX_COINS=3
HIGH_STAKES_REQUIRE_ACCOUNT_FOR_AUTONOMOUS=false

ACCOUNT_ADDRESS_ALLOWLIST=

AGENT_API_BEARER_TOKEN=

DEBATE_ANALYST_MODEL_CHAIN=
DEBATE_QUANT_MODEL_CHAIN=
DEBATE_RESEARCH_MODEL_CHAIN=
DEBATE_ADVERSARY_MODEL_CHAIN=
DEBATE_RISK_MODEL_CHAIN=
DEBATE_TREASURY_MODEL_CHAIN=
DEBATE_EXECUTION_MODEL_CHAIN=
DEBATE_JUDGE_MODEL_CHAIN=
```

Behavior:

- Empty role model chain falls back to `AGENT_MODEL_CHAIN`.
- `/trade/proposals` requires `AGENT_API_BEARER_TOKEN` in production.
- Existing `/ask` remains backward-compatible.
- Mainnet exchange execution remains disabled.

### High-stakes routing

Add `routing.py` with deterministic classification.

Trigger debate when prompt contains trade/proposal risk intent, including:

- `long`, `short`, `entry`, `stop`, `take profit`, `tp`, `sl`
- `leverage`, `liquidation`, `position size`, `risk`
- `execute`, `autonomous`, `place order`, `proposal`
- account address plus portfolio/risk language
- explicit `debate this trade` / `high stakes`

Do **not** trigger for ordinary docs/news/general market questions unless the prompt asks for a trade decision.

### Data/context gathering

Use existing `AgentTools`, but add richer high-stakes context:

- `get_market_snapshot(..., include_l2=True)`
- `get_candles` for inferred timeframe
- `get_funding_context`
- `search_market_news` when macro/news/catalyst/swing context is relevant
- `search_hyperliquid_docs` for margin, funding, tick/lot, order semantics
- `get_public_user_state` if an account address is present and allowed

Address rules:

- If `ACCOUNT_ADDRESS_ALLOWLIST` is empty, prompt-supplied public addresses are allowed.
- If allowlist is non-empty, non-allowlisted addresses are ignored for account-aware sizing.
- Private keys/API secrets remain blocked by existing guardrails.

### Deterministic feature builders

Add `features.py` for non-LLM summaries:

- candle trend, range, volatility/ATR approximation
- order-book spread, top-depth, imbalance, rough slippage estimate
- funding current/predicted/48h summary
- account equity/open positions/open orders when public account state exists
- paper sizing using existing `PaperTradeSimulator`
- risk/reward ratio and max-loss summary

Add `hyperliquid/validation.py` for:

- asset kind/id resolution
- size rounding using `szDecimals`
- price significant-figure/tick validation from Hyperliquid docs
- max leverage awareness from metadata

### Agent roles

Use these roles, dynamically no-op when irrelevant:

| Role | Purpose |
|---|---|
| Analyst / Proposer | Creates initial trade thesis/proposal. |
| Quant Agent | Evaluates price action, candles, volatility, funding, order book. |
| Research Agent | Evaluates macro/news/catalyst context. |
| Risk Manager | Checks sizing, invalidation, downside, leverage, liquidation concerns. |
| Treasury Agent | Checks account exposure/margin/open positions if address exists. |
| Execution Strategist | Produces non-executing Hyperliquid order checklist and tick/lot validation. |
| Adversary / Red Team | Attacks the setup and searches for hidden failure modes. |
| Judge | Synthesizes, accepts/rejects critiques, decides convergence/final status. |

### LangGraph topology

Implement `HighStakesDebateGraph`.

Flow:

```text
START
  -> triage
  -> gather_context
  -> proposer
  -> quant_review
  -> research_review
  -> risk_review
  -> treasury_review
  -> execution_review
  -> adversary_review
  -> judge
       -> proposer, if revision needed and rounds < 3
       -> finalize, otherwise
END
```

Inactive role nodes return a structured `abstain` output.

Convergence rules:

- Judge must explicitly classify every critical critique as accepted/rejected/deferred.
- No “average compromise” allowed.
- If unresolved critical risk remains after max rounds, final status becomes `manual_review_required` or `no_trade`.
- If user asks for live/autonomous execution, final proposal must set:

```json
{
  "autonomous_execution_allowed": false,
  "exchange_actions": []
}
```

### Structured schemas

Create Pydantic models:

- `HighStakesRoute`
- `HighStakesDecisionState`
- `MarketContextBundle`
- `TradeSetupDraft`
- `RoleOpinion`
- `RiskAssessment`
- `JudgeDecision`
- `TradeProposal`
- `TradeProposalRequest`
- `TradeProposalResponse`

Proposal status enum:

```text
paper_ready
manual_review_required
no_trade
needs_more_data
rejected_by_guardrails
not_executable
error
```

### ModelGateway changes

Add:

```python
complete_with_chain(...)
complete_structured(...)
```

Structured behavior:

1. Prompt role to return JSON only.
2. Parse into target Pydantic schema.
3. If parsing fails, do one JSON repair call.
4. If still invalid, record role error and continue to Judge.

### Persistence

Add Alembic migration `0002_high_stakes_decisions.py`.

New tables:

```text
decision_runs
decision_role_outputs
decision_state_snapshots
trade_proposals
```

Repository methods:

```python
create_decision_run(...)
record_decision_role_output(...)
record_decision_state_snapshot(...)
complete_decision_run(...)
record_trade_proposal(...)
get_trade_proposal(...)
```

Persist:

- redacted prompt
- route decision
- selected roles
- model/provider per role
- role JSON outputs
- judge decisions
- final proposal
- tool/context snapshot
- latency/round count/status

### API changes

Extend `/ask` response with optional fields:

```json
{
  "decision_run_id": null,
  "proposal_id": null,
  "high_stakes": false
}
```

Add:

```http
POST /trade/proposals
GET  /trade/proposals/{proposal_id}
```

`POST /trade/proposals` always forces the high-stakes graph if enabled.

Request:

```json
{
  "prompt": "Evaluate autonomous BTC long...",
  "account_address": "0x...",
  "account_equity_usd": 10000,
  "risk_pct": 1.0,
  "dry_run": true
}
```

Response:

```json
{
  "run_id": "...",
  "proposal_id": "...",
  "status": "paper_ready",
  "content": "...human-readable summary...",
  "proposal": {},
  "judge_decision": {},
  "rounds": 2,
  "role_count": 7,
  "warnings": []
}
```

### Safety stance

Implementation must preserve:

- no private keys
- no API secrets
- no signing
- no `/exchange` actions
- no mainnet execution
- no claim that a trade was placed

Execution Strategist may produce an order checklist, but not executable orders.

## Testing Plan

Add tests for:

- high-stakes route detection
- normal `/ask` path unchanged when debate disabled
- debate path triggered when enabled
- role activation/no-op behavior
- fake structured model outputs
- malformed JSON repair/failure handling
- Judge convergence and max-round stop
- no live execution even for “autonomous” prompts
- address allowlist behavior
- Hyperliquid tick/lot validation helpers
- proposal API auth behavior
- DB repository methods with mocked session or migration-level coverage

Validation commands after implementation:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy hyperliquid_trading_agent
uv run alembic upgrade head --sql >/tmp/hla_migration.sql
docker compose config
```

## Acceptance Criteria

- Existing tests continue passing.
- Existing `/ask` behavior remains backward-compatible.
- High-stakes debate can be enabled by env.
- Trade setup prompts produce audited multi-agent proposals.
- Judge never approves live execution.
- Full role outputs and final proposal are persisted.
- `/health/config` exposes high-stakes status and role model readiness.
- Hyperliquid API usage remains limited to official `/info` endpoints.
- No signed exchange code is introduced.

## Rollout

1. Ship disabled by default.
2. Enable in local/dev with fake models.
3. Enable in Discord for explicit high-stakes prompts.
4. Enable risk-routed activation after cost/latency observation.
5. Keep execution scope proposal/paper-only until a separate testnet execution plan is approved.
