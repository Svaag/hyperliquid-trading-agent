---
created: 2026-06-16T13:33:46.262Z
source: pi-plan-mode
status: accepted-for-execution
---

# Persistent Alpha Memory + News/Event/Signal Evaluation Loop

## Summary

Build on the repo’s existing autonomy foundation and make the learning loop truly persistent across **crypto signals, equity signals, and high-signal newswire catalysts**.

The implementation remains **paper/signoff + observe-only/shadow learning**:
- No live execution.
- No automatic threshold/weight/sizing/risk/model-routing changes.
- Tuning proposals are review packets only.
- Active advisory memories are injected only into `analyst`, `quant`, `research`, `adversary`, and `judge` contexts by default.
- `risk`, `execution`, and `treasury` memory injection requires explicit versioned change control.

## Current Repo Grounding

Existing pieces to extend:
- Signal evaluation: `hyperliquid_trading_agent/app/autonomy/evaluation.py`
- Memory pipeline: `hyperliquid_trading_agent/app/autonomy/memory.py`
- Token Capital reports: `hyperliquid_trading_agent/app/autonomy/reports.py`
- Observe-only tuning: `hyperliquid_trading_agent/app/autonomy/tuning.py`
- Autonomy loop wiring: `hyperliquid_trading_agent/app/autonomy/service.py`
- Newswire bridge: `hyperliquid_trading_agent/app/newswire/consumers/agent_feed.py`
- Equity signal generation: `hyperliquid_trading_agent/app/autonomy/equity_features.py`
- API routes: `hyperliquid_trading_agent/app/main.py`
- Persistence: `hyperliquid_trading_agent/app/db/models.py`, `hyperliquid_trading_agent/app/db/repository.py`
- Existing migrations through `alembic/versions/0007_tradfi.py`

Main gaps:
- News/events are only evidence inside signals; they are not first-class evaluated alpha subjects.
- Equity signals are generated but not persisted/evaluated through the signal evaluation loop.
- Token Capital does not yet score event/catalyst hit rate or equity paper outcomes.
- Memory injection is currently broader than the requested risk/execution/treasury boundary.

## Implementation Steps

1. Add first-class alpha event evaluation schemas and configuration.
2. Add persistence and Alembic migration for event evaluations and signal metadata.
3. Implement the event/catalyst evaluation service.
4. Wire crypto, equity, and newswire flows into signal/event evaluation.
5. Enforce the observe-only memory injection and change-control policy.
6. Extend Token Capital, reports, tuning proposals, API, and Discord commands.
7. Update docs, `.env.example`, readiness/config health, and full-start rollout settings.
8. Add/extend tests and validation commands.

## Key Implementation Details

### 1. New event evaluation contract

Add to `hyperliquid_trading_agent/app/autonomy/schemas.py`:

- `AlphaEventEvaluation`
- `AlphaEventEvaluationMark`
- literals for:
  - status: `open|partial|complete|expired_no_data|skipped|error`
  - side: `long|short|neutral`
  - outcome: `worked|failed|mixed|volatility_only|insufficient_data|open`

Capture policy:
- Evaluate Newswire events where:
  - `importance_score >= 50`
  - `source_score >= 0.4`
  - event has at least one symbol, or is macro/regulatory/exchange-status and can map to macro proxies.
- Default horizons:
  - `15m,1h,4h,24h,72h`
- Default macro proxies:
  - `BTC,ETH,SPY,QQQ`
- Max symbols per event:
  - `5`
- Direction:
  - bullish → `long`
  - bearish → `short`
  - mixed/unknown → `neutral`, evaluated as volatility-only.

Default outcome thresholds:
- directional worked: `max_favorable_bps >= +50`
- directional failed: `max_adverse_bps <= -35` and adverse move dominates favorable move
- neutral volatility-only: `max_abs_move_bps >= 75`

### 2. Config additions

Add to `Settings` in `config.py`:

```env
AUTONOMY_EVENT_EVALUATION_ENABLED=true
AUTONOMY_EVENT_EVAL_HORIZONS=15m,1h,4h,24h,72h
AUTONOMY_EVENT_EVAL_MIN_IMPORTANCE=50
AUTONOMY_EVENT_EVAL_MIN_SOURCE_SCORE=0.4
AUTONOMY_EVENT_EVAL_MAX_OPEN_EVENTS=1000
AUTONOMY_EVENT_EVAL_SYMBOLS_PER_EVENT=5
AUTONOMY_EVENT_EVAL_MACRO_PROXIES=BTC,ETH,SPY,QQQ
AUTONOMY_EVENT_EVAL_WORKED_BPS=50
AUTONOMY_EVENT_EVAL_FAILED_BPS=-35
AUTONOMY_EVENT_EVAL_VOLATILITY_BPS=75

AUTONOMY_MEMORY_PROMPT_ROLES=analyst,quant,research,adversary,judge
AUTONOMY_MEMORY_REQUIRE_CHANGE_CONTROL_FOR_RISK_EXECUTION=true
```

Expose these in `/health/config`.

### 3. Persistence and migration

Create migration:

```text
alembic/versions/0008_alpha_event_evaluations.py
```

Add tables:
- `alpha_event_evaluations`
- `alpha_event_evaluation_marks`

Also add to `trade_signals`:
- `asset_class` default `crypto`
- `metadata_json` default `{}`

Update:
- `db/models.py`
- `db/repository.py`

Repository methods:
- `upsert_alpha_event_evaluation`
- `upsert_alpha_event_evaluation_mark(s)`
- `get_alpha_event_evaluation`
- `get_alpha_event_evaluation_by_event_id`
- `list_alpha_event_evaluations`
- `list_open_alpha_event_evaluations`
- `list_due_alpha_event_evaluation_marks`

### 4. Event evaluation service

Add:

```text
hyperliquid_trading_agent/app/autonomy/event_evaluation.py
```

Service name:

```python
AlphaEventEvaluationService
```

Responsibilities:
- `load_open()`
- `status()`
- `create_for_newswire_event(event, market_regime)`
- `create_for_news_event(event, market_regime)`
- `on_price(symbol, asset_class, price, timestamp_ms)`
- `mark_due(now_ms)`
- `expire_overdue_events(now_ms)`
- `list_evaluations(...)`
- `get(...)`
- `get_by_event_id(...)`
- `link_signal(event_id, signal_id, symbol)`

Completion:
- Complete after final horizon is marked.
- If no fresh price at all, mark `expired_no_data`.
- Never blocks the main autonomy loop.

### 5. Wiring

In `main.py`:
- Instantiate `AlphaEventEvaluationService`.
- Add to `app.state`.
- Pass into:
  - `AutonomousTradingLoopService`
  - `AgentNewsConsumer`
  - `AutonomyReportService`
  - `TuningProposalService`

In `AgentNewsConsumer._on_event`:
- After pushing event into reducer, create event evaluations.

In fallback `AutonomyNewswire` polling path:
- Create event evaluations for polled `NewsEvent`s.

In `AutonomousTradingLoopService`:
- Feed crypto prices from `allMids` into both:
  - `SignalEvaluationService.on_price`
  - `AlphaEventEvaluationService.on_price`
- Feed equity prices from TradFi snapshots into:
  - equity signal evaluations
  - event evaluations for equity symbols.
- Run both signal and event `mark_due`.
- Run both expiry checks.

For generated signals:
- Add `metadata.source_event_ids` from `state.news_state.latest_events`.
- Add `metadata.asset_class`.
- Persist equity signals through `trade_signals`.
- Create signal evaluations for equity signals too.
- Update signal evaluation status on equity post/approve/reject/expire.

### 6. Memory policy

Update `MemoryService` and high-stakes role wiring so default memory injection roles are exactly:

```text
analyst, quant, research, adversary, judge
```

Default excluded roles:

```text
risk, execution, treasury
```

Rules:
- Strategy/risk/execution/capital-affecting lessons may be persisted as candidates/shadow.
- They cannot be injected into excluded roles unless manually promoted with:
  - `human_review_confirmed=true`
  - `change_control_id`
  - approved target roles.
- `maybe_attach_model_insight` must not inject risk/execution memory by default.

Add API request fields for candidate promotion:
- `change_control_id`
- `approved_for_role_injection_roles`
- `reviewer`

### 7. Memory learning from events

Add:

```python
MemoryService.observe_event_evaluation(evaluation)
```

Candidate examples:
- Worked high-signal catalyst:
  - role: `research`
  - lesson type: `signal_quality` or `role_behavior`
  - claim: source/event_type/sentiment had follow-through for symbol/scope.
- Failed catalyst:
  - role: `adversary` or `research`
  - claim: event/source/type did not produce follow-through; require confirmation.
- Neutral volatility catalyst:
  - role: `research`
  - claim: event type produces volatility but weak direction.

All strategy-affecting candidates stay shadow/review-gated.

### 8. Token Capital extensions

Keep the existing `TokenCapitalSnapshot` schema and weights, but extend component details.

Update `TokenCapitalScorer.compute(...)` to accept:
- `event_evaluations`
- optional `equity_portfolio_snapshot`

Signal quality becomes:
- 70% existing signal outcome quality
- 30% catalyst/event hit quality when event evaluations exist
- falls back to existing signal-only behavior when no events exist.

Add to `component_details`:
- `event_evaluation_count`
- `completed_event_evaluation_count`
- `event_hit_rate`
- `event_failed_rate`
- `volatility_only_count`
- source/event-type breakdown
- crypto/equity paper attribution

Reports should include:
- event outcomes
- best/worst catalysts
- missed catalyst-linked signals
- linked event → signal attribution
- explicit observe-only safety block.

### 9. Tuning proposals

Extend `TuningProposalService` with event-based proposals.

Minimum event sample size:
- `8` completed event evaluations per `(asset_class, source, event_type, sentiment)` or `(symbol, event_type, side)` scope.

Proposal examples:
- Repeated failed catalysts:
  - `data_quality_gate`
  - proposed diff: require confirmation / lower source weight
- Repeated worked catalysts:
  - `weight_change`
  - proposed diff: review increasing evidence weight for that catalyst class

Every proposal:
- `status="proposed"`
- `auto_apply_enabled=false`
- `requires_change_control=true`
- includes rollback plan.

### 10. API and Discord

Add API routes:
- `GET /autonomy/evaluations/events`
- `GET /autonomy/evaluations/events/{evaluation_id}`
- `GET /autonomy/evaluations/events/by-event/{event_id}`
- `POST /autonomy/evaluations/events/backfill`

Update:
- `POST /autonomy/evaluations/run` returns both signal/event marks.
- `/autonomy/status`
- `/health/config`
- `/ready`
- `/autonomy/token-capital`
- report endpoints.

Add Discord commands:
- `event outcome <event_id>`
- `catalyst outcome <event_id>`

Responses must end with:
- no live trade placed
- no strategy setting changed.

## Full Configured Start

Use existing `.env`/deployment-specific values. Do not invent secrets or Discord IDs.

Set or document these toggles for the first full start:

```env
AUTONOMY_ENABLED=true
AUTONOMY_MODE=paper_signoff
AUTONOMY_REQUIRE_HUMAN_SIGNOFF=true
HYPERLIQUID_WS_ENABLED=true

AUTONOMY_UNIVERSE_TOP_N_PERPS=5
AUTONOMY_MAX_TRACKED_ASSETS=20
AUTONOMY_MAX_HOT_L2_ASSETS=5
AUTONOMY_MAX_SIGNALS_PER_DAY=3
AUTONOMY_MIN_SIGNAL_SCORE=75

AUTONOMY_EVALUATION_ENABLED=true
AUTONOMY_EVENT_EVALUATION_ENABLED=true
AUTONOMY_MEMORY_ENABLED=true
AUTONOMY_REPORTS_ENABLED=true
AUTONOMY_TUNING_PROPOSALS_ENABLED=true

NEWSWIRE_ENABLED=true
NEWSWIRE_AGENT_MIN_IMPORTANCE=50
```

Equity, if Alpaca credentials exist:

```env
TRADFI_ENABLED=true
AUTONOMY_EQUITY_ENABLED=true
AUTONOMY_EQUITY_UNIVERSE=SPY,QQQ,NVDA,AAPL,MSFT,TSLA,COIN,MSTR
AUTONOMY_EQUITY_MAX_SIGNALS_PER_DAY=3
```

If required runtime values are missing:
- `/ready` must degrade with a specific reason.
- The service must not silently relax safety.
- Paper/signoff and no-live-execution guarantees remain enforced.

## Tests

Add/update tests for:

- event evaluation lifecycle: bullish, bearish, neutral, no-price
- Newswire → event evaluation creation
- macro proxy mapping
- event marks and final outcomes
- equity signal persistence and evaluation
- crypto/equity price feeds into evaluations
- memory candidates from event outcomes
- memory injection role allowlist
- change-control-gated risk/execution/treasury memories
- Token Capital includes event/equity details
- event-based tuning proposals are observe-only
- API routes and Discord command parsing
- migration offline SQL generation

Validation commands:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy hyperliquid_trading_agent
uv run alembic upgrade head --sql >/tmp/hla_migration.sql
docker compose config
```

## Acceptance Criteria

- Every crypto and equity `TradeSignal` creates or reuses a persisted signal evaluation.
- Every high-signal Newswire catalyst creates event evaluations for eligible symbols/proxies.
- Event outcomes answer “would this catalyst have worked?” with horizon marks and final outcome.
- Token Capital reports include signal quality, catalyst hit rate, paper outcomes, memory compounding, and safety status.
- Memories are persisted and retrievable, but default injection excludes risk/execution/treasury.
- Tuning proposals are generated as review packets only and never auto-applied.
- Full configured start enables the loop in paper/signoff mode with readiness degradation for missing env values.
- `exchange_actions` remains `[]` everywhere in this phase.



<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add first-class alpha event evaluation schemas and config... _(done)_
- [x] 2. Add persistence and Alembic migration for event evaluatio... _(done)_
- [x] 3. Implement the event/catalyst evaluation service. _(done)_
- [x] 4. Wire crypto, equity, and newswire flows into signal/event... _(done)_
- [x] 5. Enforce the observe-only memory injection and change-cont... _(done)_
- [x] 6. Extend Token Capital, reports, tuning proposals, API, and... _(done)_
- [x] 7. Update docs, .env.example, readiness/config health, and f... _(done)_
- [x] 8. Add/extend tests and validation commands. _(done)_

<!-- pi-plan-progress:end -->
