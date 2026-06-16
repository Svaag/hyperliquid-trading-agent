# Institutional Engine Upgrade

This document tracks the new paper/shadow institutional trading engine path.

## Safety posture

The engine is **paper/shadow only**:

- no private-key handling
- no signed exchange adapter
- no route capable of submitting live orders
- `ENGINE_LIVE_ENABLED=true` is rejected by settings validation
- execution reports are produced by `PaperAdapter` or `ShadowAdapter` only

## Canonical loop

```text
NormalizedEvent
  -> FeatureValue / FeatureSnapshot
  -> RegimeVector
  -> AlphaCandidate
  -> EVEstimate
  -> AllocationDecision
  -> RiskGateway.check_order_intent
  -> EvidencePack / DebateDecision when review value is high
  -> OrderIntent
  -> ExecutionReport
  -> PositionThesis
  -> Reconciliation / PnL attribution / replay
```

## New package

```text
hyperliquid_trading_agent/app/engine/
```

Key modules:

- `schemas.py` — canonical contracts
- `event_ledger.py` — append-only normalized event facade
- `feature_store.py` — point-in-time feature values/snapshots
- `regime.py` — `RegimeVector` computation
- `alpha/` — initial alpha families
- `scorer.py` — deterministic EV fallback and offline training scaffold
- `portfolio_allocator.py` — portfolio-aware risk allocation
- `debate_adjudicator.py` — EvidencePack + DebateDecision authority boundary
- `execution.py` — PaperAdapter / ShadowAdapter
- `position_manager.py` — PositionThesis state lifecycle
- `routes.py` — read-only `/engine/*` endpoints

## New configuration

```env
ENGINE_ENABLED=false
ENGINE_MODE=paper_shadow
ENGINE_EXECUTION_MODES=paper,shadow
ENGINE_LIVE_ENABLED=false

NEWSWIRE_GATEWAY_ENABLED=true
AUTONOMY_LEGACY_NEWS_POLL_ENABLED=false
NEWS_SIGNAL_GENERATION_ENABLED=true
NEWS_EVENT_RISK_BLOCKS_ENABLED=true
```

## Read-only API

Protected by the existing agent API token outside dev/test/local:

```http
GET /engine/status
GET /engine/events
GET /engine/events/{event_id}
GET /engine/features?asset=BTC
GET /engine/regime/latest
GET /engine/candidates
GET /engine/candidates/{candidate_id}
GET /engine/candidate-book/latest
GET /engine/ev-estimates
GET /engine/allocations
GET /engine/evidence-packs/{evidence_pack_id}
GET /engine/debate-decisions
GET /engine/order-intents
GET /engine/execution-reports
GET /engine/positions
GET /engine/reconciliation
GET /engine/model-versions
GET /engine/retention
```

## Persistence

Alembic revisions:

- `0011_engine_event_feature_store`
- `0012_candidate_ev_allocation_debate`
- `0013_execution_position_reconciliation`
- `0014_model_registry_retention`

High-frequency event/feature data is intended for bounded retention and rollups; candidates, decisions, risk checks, evidence packs, execution reports, position theses, attribution, and governance records are durable audit artifacts.
