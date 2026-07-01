---
created: 2026-07-01T18:05:04.867Z
source: pi-plan-mode
status: accepted-for-execution
---

# Wire Persisted Newswire Into the Institutional Trading Engine

## Summary
Implement the missing service-role runtime path:

`newswire_events` table → `trader` worker persisted pump → `EngineNewsConsumer` → `InstitutionalEngineService.ledger` → engine feature store/regime inputs.

Keep `NEWSWIRE_ENABLED=false` on `trader`; the trader must **consume persisted rows only**, not own external news providers. Keep all behavior paper/shadow-only and avoid historical backlog pollution.

## Current Facts
- Configured engine gate: `ENGINE_NEWS_MIN_IMPORTANCE=35`.
- Discord feed gate was lowered to `NEWSWIRE_NEWS_MIN_IMPORTANCE=25`.
- Current service-role runtime has only:
  - `world_model:newswire`
  - `discord_publisher:newswire`
- `normalized_events where event_type='newswire' = 0`, so the Institutional Engine currently receives no Newswire evidence.
- Existing `EngineNewsConsumer` only works from an in-process bus and currently requires `settings.newswire_enabled`, which conflicts with service-role `trader` boundary.
- `trader` correctly has `NEWSWIRE_ENABLED=false`.

## Implementation Steps
1. Harden the generic persisted Newswire pump.
2. Decouple `EngineNewsConsumer` from process-owned Newswire ingestion.
3. Add a trader-owned persisted Newswire engine pump.
4. Expose engine-newsfeed runtime status through heartbeats/API.
5. Add regression and integration tests.
6. Update docs/runbooks.
7. Roll out safely with no Newswire backlog replay.

## Detailed Design

### 1. Harden `StoredNewswirePump`
File: `hyperliquid_trading_agent/app/workers/stored_newswire_pump.py`

Add optional constructor args:

```python
bootstrap_from_latest: bool = False
bootstrap_metadata: dict[str, Any] | None = None
```

Behavior:
- Existing consumers keep default `bootstrap_from_latest=False`.
- For the engine consumer, use `bootstrap_from_latest=True`.
- If no offset exists and `bootstrap_from_latest=True`:
  - Query `repository.list_newswire_events(limit=1)`.
  - If a latest event exists, write the consumer offset to that event.
  - Return `0` without delivering historical rows.
  - Metadata should include:
    - `bootstrap_from_latest: True`
    - `reason: "avoid_historical_news_regime_pollution"`
    - latest headline/source.

Also harden bad rows:
- Move `NewswireEvent.model_validate(row)` inside per-row error handling.
- If row validation fails:
  - Increment pump error counters.
  - Log `stored_newswire_pump_invalid_row`.
  - Advance offset past that invalid row so one poison row cannot stall all consumers.
- If a callback fails:
  - Preserve current behavior: do **not** advance offset past the failed event.

### 2. Update `EngineNewsConsumer`
File: `hyperliquid_trading_agent/app/engine/newswire_bridge.py`

Change `effective_enabled` from:

```python
settings.newswire_enabled and settings.engine_enabled and settings.engine_newsfeed_enabled
```

to:

```python
settings.engine_enabled and settings.engine_newsfeed_enabled and engine_service is not None
```

Reason:
- In service-role runtime, `NEWSWIRE_ENABLED` means “this process owns external Newswire ingestion.”
- `trader` must not own ingestion, but it should consume persisted Newswire rows.

Add a public handler:

```python
async def handle_event(self, event: NewswireEvent) -> None:
    ...
```

Then subscribe using `self.handle_event` instead of private `_on_event`.

Keep existing gates:
- Bus filter: `min_importance=ENGINE_NEWS_MIN_IMPORTANCE`
- Symbol mapping:
  - direct only if event symbols intersect `autonomy_core_symbols`
  - macro proxies if `asset_class="macro"` and importance `>= ENGINE_NEWS_MACRO_MIN_IMPORTANCE`
- Feature derivation only if `source_score >= ENGINE_NEWS_MIN_SOURCE_SCORE`.

### 3. Wire Trader Worker
File: `hyperliquid_trading_agent/app/workers/trader_worker.py`

Add trader-owned fields:
```python
self._engine_service: InstitutionalEngineService | None
self._engine_news_bus: InProcessNewswireBus | None
self._engine_news_consumer: EngineNewsConsumer | None
self._engine_news_pump: StoredNewswirePump | None
```

Add helper:

```python
async def _start_engine_newsfeed(self) -> None:
```

Behavior:
- If `not settings.engine_enabled` or `not settings.engine_newsfeed_enabled`, do nothing.
- Instantiate:
  - `InProcessNewswireBus`
  - `RiskGateway(settings=settings, repository=self.repository)`
  - `InstitutionalEngineService(...)`
    - `hyperliquid=None` is acceptable for this path because the news bridge only uses `ledger` and `feature_store`.
    - Do not start engine loops.
    - Do not place or enqueue orders.
  - `EngineNewsConsumer(settings=settings, bus=bus, engine_service=engine_service)`
  - `StoredNewswirePump(...)`
    - `consumer_name="trader:engine_newswire"`
    - `callbacks=[bus.publish]`
    - `bootstrap_from_latest=True`
    - same poll/batch settings as other consumers.

Restructure `TraderWorker.run()`:
- Start engine-newsfeed pump if enabled.
- Run command loop as a task.
- Run engine Newswire pump as a task when configured.
- Wait until stopped.
- On shutdown:
  - stop pump
  - stop engine consumer
  - cancel tasks cleanly.

Heartbeat metadata should include:

```json
{
  "trader": {
    "command_count": 0,
    "last_command_type": null,
    "execution_authority": "paper-only/settings-gated"
  },
  "engine_newsfeed": {
    "enabled": true,
    "consumer_name": "trader:engine_newswire",
    "consumer": { "... EngineNewsConsumer.status()" },
    "pump": { "... StoredNewswirePump.status()" },
    "bus": { "... InProcessNewswireBus.status()" },
    "thresholds": {
      "min_importance": 35,
      "min_source_score": 0.4,
      "macro_min_importance": 60,
      "catalyst_threshold": 0.35
    }
  }
}
```

### 4. Config Warning Cleanup
File: `hyperliquid_trading_agent/app/config.py`

Remove or revise this warning:

```python
ENGINE_NEWSFEED_ENABLED requires NEWSWIRE_ENABLED=true
```

Replace with:
- No warning for service-role runtime.
- Add validation warning if `ENGINE_NEWS_MIN_IMPORTANCE` is outside `[0, 100]`.

Keep `SERVICE_ROLE=trader` rejecting `NEWSWIRE_ENABLED=true`.

### 5. API / Observability
Keep `/runtime/heartbeats` as the source of worker truth.

Optionally update `/engine/status`:
- If local API `app.state.engine_news_consumer` is not running, include latest trader heartbeat `metadata.engine_newsfeed` under:

```json
"newsfeed_runtime": { ... }
```

This avoids the API showing only the passive in-process consumer.

### 6. Tests

Add/extend tests in:

#### `tests/test_newswire_world_model_resume.py`
Cover:
- `bootstrap_from_latest=True` with no offset:
  - existing rows are skipped
  - offset is set to latest
  - callback is not called
- invalid persisted row:
  - pump logs/counts error
  - offset advances past invalid row
  - subsequent valid rows are processed
- callback failure:
  - offset does not advance past failed row

#### `tests/test_engine_newsfeed.py`
Cover:
- `EngineNewsConsumer` works when `newswire_enabled=False` but:
  - `engine_enabled=True`
  - `engine_newsfeed_enabled=True`
- importance `<35` is filtered out.
- importance `35` with `BTC` records:
  - normalized event `evt_{newswire_event_id}`
  - `catalyst_pressure`
  - `event_risk_pressure`
  - `source_consensus_score`
- source score below minimum:
  - normalized event is recorded
  - features are skipped
  - skip reason is `source_score_below_minimum`
- macro event with importance `>=60` and no symbols maps to `BTC,ETH,HYPE`.

#### `tests/test_trader_worker.py`
Add trader worker lifecycle/unit coverage:
- When engine newsfeed enabled, `_start_engine_newsfeed()` creates:
  - pump
  - consumer
  - bus
  - engine service
- Heartbeat metadata includes `engine_newsfeed`.
- Consumer name is exactly `trader:engine_newswire`.
- `NEWSWIRE_ENABLED=false` remains valid for `SERVICE_ROLE=trader`.

### 7. Documentation
Update:
- `docs/deploy/service-role-runtime.md`
- `docs/newswire-smoke-test.md`
- `docs/world-model.md` or engine runbook if applicable.

Document:
- Discord threshold: `NEWSWIRE_NEWS_MIN_IMPORTANCE`
- Engine threshold: `ENGINE_NEWS_MIN_IMPORTANCE`
- Engine persisted consumer: `trader:engine_newswire`
- No automatic historical backfill on first start.
- Backfill is intentionally out of scope unless implemented as a separate replay tool that preserves original event timestamps.

## Rollout / Execution Plan

1. Merge implementation.
2. Run:
   ```bash
   uv run pytest -q tests/test_newswire_world_model_resume.py tests/test_engine_newsfeed.py tests/test_trader_worker.py tests/test_service_role_runtime.py
   uv run pytest -q
   uv run ruff check hyperliquid_trading_agent tests
   ```
3. Rebuild/restart only `trader` initially:
   ```bash
   VAULT_ENABLED=false docker compose up -d --build --force-recreate --no-deps trader
   ```
4. Verify runtime:
   ```bash
   curl http://127.0.0.1:8081/runtime/heartbeats?service_role=trader
   curl http://127.0.0.1:8081/runtime/offsets
   ```
5. Confirm new offset exists:
   - `consumer_name = trader:engine_newswire`
6. Inject a fresh valid smoke Newswire row:
   - `symbols=["BTC","ETH","HYPE"]`
   - `importance_score=40`
   - `source_score=0.9`
   - `confidence=0.9`
   - `sentiment="bullish"`
   - `freshness="fresh"`
7. Verify:
   - `normalized_events.event_type='newswire'` contains `evt_{smoke_event_id}`
   - feature rows exist for each core symbol:
     - `catalyst_pressure`
     - `event_risk_pressure`
     - `source_consensus_score`
   - no live exchange/order side effects occurred.
8. Restart full default stack if needed.
9. Verify final runtime roles:
   - `agent`
   - `discord_bot`
   - `discord_publisher`
   - `newswire`
   - `scheduler`
   - `trader`
   - `world_model`
10. Confirm `stale_worker_count=0`.

## Acceptance Criteria
- `trader:engine_newswire` appears in `/runtime/offsets`.
- `trader` heartbeat shows engine newsfeed pump running.
- Fresh BTC/ETH/HYPE Newswire event with importance `>=35` reaches engine ledger.
- Low-importance event `<35` does not reach engine ledger.
- Macro event `>=60` maps into core symbols.
- Invalid persisted Newswire rows no longer stall consumers.
- No live exchange execution path is introduced.
- `NEWSWIRE_ENABLED=false` remains enforced for `SERVICE_ROLE=trader`.

## Explicit Non-Goals
- Do not enable live trading.
- Do not make `trader` own RSS/Alpaca/X/TradingEconomics ingestion.
- Do not replay historical Newswire rows into current regime features on first start.
- Do not change Discord posting thresholds beyond the already-local runtime setting.








<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Harden the generic persisted Newswire pump _(done)_
- [x] 2. Decouple EngineNewsConsumer from process-owned Newswire ingestion _(done)_
- [x] 3. Add a trader-owned persisted Newswire engine pump _(done)_
- [x] 4. Expose engine-newsfeed runtime status through heartbeats/API _(done)_
- [x] 5. Add regression and integration tests _(done)_
- [x] 6. Update docs/runbooks _(done)_
- [x] 7. Roll out safely with no Newswire backlog replay _(done)_

<!-- pi-plan-progress:end -->
