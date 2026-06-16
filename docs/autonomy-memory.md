# Persistent Alpha Memory and Signal Evaluation Loop

The Persistent Alpha Memory loop evaluates autonomous paper/signoff signals, scores the bot's improvement quality, stores evidence-gated memories, and emits observe-only tuning proposals.

The loop is non-executing:

- no live orders
- no private keys
- no signed exchange actions
- no automatic strategy/risk/sizing/cooldown changes
- tuning proposals are recommendations only

## Runtime flow

```text
TradeSignal
  -> SignalEvaluation + horizon marks
  -> allMids/equity price path updates
  -> MFE/MAE/R-multiple + TP/stop/expiry outcome
  -> MemoryObservation
  -> CandidateLesson
  -> ShadowRoleLessonMemory
  -> RoleLessonMemory or needs_human_review
  -> Daily/weekly report + Token Capital + TuningProposal

NewswireEvent / NewsEvent
  -> AlphaEventEvaluation + horizon marks
  -> crypto/equity/macro-proxy price path updates
  -> worked/failed/mixed/volatility-only outcome
  -> MemoryObservation
  -> CandidateLesson
  -> ShadowRoleLessonMemory
  -> RoleLessonMemory or needs_human_review
  -> Daily/weekly report + Token Capital + TuningProposal
```

## Evaluation horizons

Default horizons:

```env
AUTONOMY_EVAL_HORIZONS=15m,1h,4h,24h,expiry
```

Every signal is evaluated even if rejected or expired. Rejected signals can produce opportunity-cost attribution when they would have reached +1R/TP before stop.

Tracked outcome fields include:

- latest price
- MFE/MAE in bps
- MFE/MAE in R
- stop hit / TP hit timestamps
- terminal outcome
- horizon marks
- rejected-signal opportunity cost

## Event/catalyst evaluation

High-signal events are evaluated as first-class alpha subjects when:

```env
AUTONOMY_EVENT_EVALUATION_ENABLED=true
AUTONOMY_EVENT_EVAL_HORIZONS=15m,1h,4h,24h,72h
AUTONOMY_EVENT_EVAL_MIN_IMPORTANCE=50
AUTONOMY_EVENT_EVAL_MIN_SOURCE_SCORE=0.4
AUTONOMY_EVENT_EVAL_MACRO_PROXIES=BTC,ETH,SPY,QQQ
```

Bullish events are evaluated long, bearish events short, and mixed/unknown events as neutral volatility catalysts. Macro events without tagged symbols use the configured proxy basket. Event outcomes are observe-only and can link to later signals through `metadata.source_event_ids`.

## Token Capital

Token Capital is a hybrid score, not raw PnL.

Components:

```text
risk_adjusted_performance_score   30%
signal_quality_score              20%
memory_compounding_score          20%
risk_discipline_score             15%
operator_communication_score      10%
reliability_score                  5%
```

Hard gates can cap or penalize the total score for invalid market facts, schema failures, hallucinated orders/fills, stale data, or live-execution claims.

## Memory promotion

V1 memory types:

- `SignalOutcomeMemory` through signal evaluations
- `RoleLessonMemory` for Analyst, Quant, Research, Risk, Treasury, Execution, Adversary, Judge
- `OperatorOutputLessonMemory` for Discord/report clarity and actionability

Promotion path:

```text
MemoryObservation -> CandidateLesson -> ShadowRoleLessonMemory -> RoleLessonMemory
```

Routine role lessons require repeated evidence and confidence thresholds.

Strategy/risk/execution/capital-affecting memories require human review before active durable use and still cannot mutate runtime strategy in V1.

## Role contracts

Contracts live in:

```text
hyperliquid_trading_agent/app/autonomy/role_contracts.py
```

Each role has:

- purpose
- allowed inputs
- forbidden claims
- daily checklist
- output schema
- scoring criteria
- memory types
- escalation conditions

Role contracts and active scoped memories are injected into high-stakes role prompts as advisory context only.

## Tuning proposals

Tuning proposals include:

- proposal type
- affected scope
- current behavior
- exact proposed diff
- evidence
- expected impact
- risk/blast radius
- rollback plan
- confidence
- sample size
- expiry/evaluation window

They are never auto-applied.

## Discord commands

In the autonomy alert channel:

```text
daily report
weekly report
token capital
signal outcome <signal_id>
signal eval <signal_id>
mark signal <signal_id> good|bad|unclear|too_noisy|useful|wrong
feedback signal <signal_id> <note>
feedback bot <note>
memories
memories risk
memory <lesson_id>
tuning proposals
tuning proposal <id>
apply tuning proposal <id>   # denied; observe-only phase
```

## API endpoints

Protected by `AGENT_API_BEARER_TOKEN` outside dev/test/local.

```http
GET  /autonomy/evaluations/signals
GET  /autonomy/evaluations/signals/{signal_id}
GET  /autonomy/evaluations/events
GET  /autonomy/evaluations/events/{evaluation_id}
GET  /autonomy/evaluations/events/by-event/{event_id}
POST /autonomy/evaluations/run
POST /autonomy/evaluations/backfill
POST /autonomy/evaluations/events/backfill

GET  /autonomy/reports/daily
GET  /autonomy/reports/daily/{date}
POST /autonomy/reports/daily/run
GET  /autonomy/reports/weekly
GET  /autonomy/reports/weekly/{week_key}
POST /autonomy/reports/weekly/run

GET  /autonomy/token-capital
GET  /autonomy/token-capital/history

GET  /autonomy/memory/observations
GET  /autonomy/memory/candidates
GET  /autonomy/memory/shadow
GET  /autonomy/memory/lessons
GET  /autonomy/memory/lessons/{lesson_id}
POST /autonomy/memory/lessons/{lesson_id}/archive
POST /autonomy/memory/candidates/{candidate_id}/reject
POST /autonomy/memory/candidates/{candidate_id}/promote-shadow
POST /autonomy/memory/candidates/{candidate_id}/promote-active

POST /autonomy/feedback
GET  /autonomy/feedback

GET  /autonomy/tuning-proposals
GET  /autonomy/tuning-proposals/{proposal_id}
POST /autonomy/tuning-proposals/{proposal_id}/mark-reviewed
POST /autonomy/tuning-proposals/{proposal_id}/reject
POST /autonomy/tuning-proposals/{proposal_id}/expire
```

## Safe rollout / full configured start

Use existing deployment-specific `.env` values for tokens and channel IDs; do not invent secrets. For the first full configured start, keep the conservative canary while the scoring loop validates:

```env
AUTONOMY_ENABLED=true
AUTONOMY_MODE=paper_signoff
AUTONOMY_REQUIRE_HUMAN_SIGNOFF=true
HYPERLIQUID_WS_ENABLED=true
AUTONOMY_UNIVERSE_TOP_N_PERPS=5
AUTONOMY_MAX_TRACKED_ASSETS=20
AUTONOMY_MAX_HOT_L2_ASSETS=5
AUTONOMY_MAX_SIGNALS_PER_DAY=3
AUTONOMY_EVALUATION_ENABLED=true
AUTONOMY_EVENT_EVALUATION_ENABLED=true
AUTONOMY_MEMORY_ENABLED=true
AUTONOMY_REPORTS_ENABLED=true
AUTONOMY_TUNING_PROPOSALS_ENABLED=true
AUTONOMY_MEMORY_PROMPT_ROLES=analyst,quant,research,adversary,judge
AUTONOMY_MEMORY_REQUIRE_CHANGE_CONTROL_FOR_RISK_EXECUTION=true
NEWSWIRE_ENABLED=true
NEWSWIRE_AGENT_MIN_IMPORTANCE=50
```

If Alpaca data credentials are present, enable the liquid equity canary:

```env
TRADFI_ENABLED=true
AUTONOMY_EQUITY_ENABLED=true
AUTONOMY_EQUITY_UNIVERSE=SPY,QQQ,NVDA,AAPL,MSFT,TSLA,COIN,MSTR
AUTONOMY_EQUITY_MAX_SIGNALS_PER_DAY=3
```

Verify:

```bash
curl http://127.0.0.1:8081/ready
curl http://127.0.0.1:8081/health/config
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" http://127.0.0.1:8081/autonomy/token-capital
curl -H "Authorization: Bearer $AGENT_API_BEARER_TOKEN" http://127.0.0.1:8081/autonomy/evaluations/signals
```

Expected safety flags:

```json
{
  "strategy_mutation_enabled": false,
  "risk_limit_mutation_enabled": false,
  "tuning_auto_apply_enabled": false,
  "paper_only": true
}
```
