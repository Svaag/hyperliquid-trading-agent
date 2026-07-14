# Catalyst Evaluation and Persistent Memory

The observation loop evaluates Newswire catalysts, stores evidence-gated memories,
and emits review-only tuning proposals. It does not generate, publish, approve, or
execute trade proposals. Institutional candidates and operator proposals belong to
the engine.

The loop is non-executing:

- no live orders or signed exchange actions
- no automatic strategy, risk, sizing, or cooldown changes
- no trade-signal generator or human-signoff workflow
- tuning proposals are recommendations only

## Runtime flow

```text
NewswireEvent / NewsEvent
  -> AlphaEventEvaluation + fixed-horizon marks
  -> crypto/equity/macro-proxy price updates
  -> worked/failed/mixed/volatility-only outcome
  -> MemoryObservation
  -> CandidateLesson
  -> ShadowRoleLessonMemory
  -> RoleLessonMemory or needs_human_review
  -> Daily/weekly report + Token Capital + TuningProposal
```

## Event evaluation

```env
AUTONOMY_EVENT_EVALUATION_ENABLED=true
AUTONOMY_EVENT_EVAL_HORIZONS=15m,1h,4h,24h,72h
AUTONOMY_EVENT_EVAL_MIN_IMPORTANCE=50
AUTONOMY_EVENT_EVAL_MIN_SOURCE_SCORE=0.4
AUTONOMY_EVENT_EVAL_MACRO_PROXIES=BTC,ETH,SPY,QQQ
```

Bullish events are evaluated long, bearish events short, and mixed/unknown events
as neutral volatility catalysts. Macro events without tagged symbols use the
configured proxy basket. These outcomes are observational evidence only.

## Memory and tuning

Routine role lessons require repeated evidence and confidence thresholds.
Strategy-, risk-, execution-, and capital-affecting lessons require human review
before durable use and cannot mutate runtime configuration automatically.

Discord commands supported by the observation layer include:

```text
daily report
weekly report
token capital
event outcome <event_id>
feedback bot <note>
memories [role]
memory <lesson_id>
tuning proposals
tuning proposal <id>
```

## API endpoints

Protected by `AGENT_API_BEARER_TOKEN` outside dev/test/local:

```http
GET  /autonomy/evaluations/events
GET  /autonomy/evaluations/events/{evaluation_id}
GET  /autonomy/evaluations/events/by-event/{event_id}
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
POST /autonomy/feedback
GET  /autonomy/tuning-proposals
```

Engine proposals are exposed separately at `GET /engine/operator-proposals`.
Acknowledging one records operator review only and creates no paper or live order.
