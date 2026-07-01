---
created: 2026-07-01T01:29:00.445Z
source: pi-plan-mode
status: accepted-for-execution
---

# Newsfeed → Engine Regime → Conservative Strategy Gating

## Summary

Current code has partial support for news-driven regimes, but live newswire events do **not** directly feed the institutional engine regime today. Newswire currently feeds Discord, the World Model, and legacy autonomy news state. The engine can derive `catalyst_pressure`, but nothing wires `NewswireEvent` into the engine `EventLedger`/`FeatureStore`.

Implement direct newsfeed-to-engine wiring with:

- Engine as canonical strategy decision layer.
- Legacy autonomy/world model still informed for dashboards/evaluation.
- Conservative gate-only rollout: news can flip regime and suppress unsafe strategy families, but does **not** auto-enable new alpha or paper/live execution.
- Macro news proxies to all core symbols.
- News catalyst decay after 60 minutes.

## Implementation Steps

1. Add engine newsfeed configuration.
2. Add a newswire-to-engine bridge/consumer.
3. Convert qualifying `NewswireEvent`s into engine normalized events and features.
4. Update regime computation to use recent directional and event-risk news pressure with 60-minute decay.
5. Add conservative strategy selection/gating based on news risk tier.
6. Wire lifecycle/status/metrics into app startup and engine status.
7. Add unit/integration tests and documentation.

## Key Implementation Details

### Current behavior to preserve/clarify

- `NewswireService` normalizes and publishes `NewswireEvent`s.
- `AgentNewsConsumer` already feeds:
  - legacy autonomy reducer news state,
  - world model,
  - event evaluation.
- `FeatureStore._news_features()` can derive `catalyst_pressure`, but engine runtime never receives newswire events directly.
- Engine strategies are currently run from `InstitutionalEngineService.run_once()` using market data, L2, funding/OI, liquidations, and world-model features.

### New settings

Add to `Settings` and `.env.example`:

```env
ENGINE_NEWSFEED_ENABLED=true
ENGINE_NEWS_MIN_IMPORTANCE=35
ENGINE_NEWS_MIN_SOURCE_SCORE=0.4
ENGINE_NEWS_CATALYST_THRESHOLD=0.35
ENGINE_NEWS_CATALYST_TTL_SECONDS=3600
ENGINE_NEWS_MACRO_MIN_IMPORTANCE=60
ENGINE_NEWS_MACRO_PROXY_SYMBOLS=
```

Behavior:

- Empty `ENGINE_NEWS_MACRO_PROXY_SYMBOLS` means use all `AUTONOMY_CORE_UNIVERSE` symbols.
- With defaults, macro news proxies to BTC, ETH, HYPE.

### New engine news bridge

Add `hyperliquid_trading_agent/app/engine/newswire_bridge.py`.

Responsibilities:

- Subscribe to `newswire_service.bus` when:
  - `NEWSWIRE_ENABLED=true`
  - `ENGINE_ENABLED=true`
  - `ENGINE_NEWSFEED_ENABLED=true`
- Filter by `ENGINE_NEWS_MIN_IMPORTANCE`.
- Convert eligible `NewswireEvent` into an engine `NormalizedEvent` with stable ID:

```text
evt_<newswire_event_id>
```

- Record through `engine_service.ledger.record(...)`.
- Derive features through `engine_service.feature_store.features_for_event(...)`.

Symbol mapping:

1. Use explicit event symbols that are in `settings.autonomy_core_symbols`.
2. If `event.asset_class == "macro"` and importance >= `ENGINE_NEWS_MACRO_MIN_IMPORTANCE`, add all macro proxy symbols.
3. Ignore events with no resulting engine symbols.

Skip rules:

- Skip feature derivation for `action="removed"`.
- Record but do not derive features when source score is below `ENGINE_NEWS_MIN_SOURCE_SCORE`.
- Unknown/mixed sentiment may still create event-risk pressure but not directional catalyst pressure.

### Feature changes

Update `FeatureStore._news_features()` to emit:

- `catalyst_pressure`: signed directional pressure.
- `event_risk_pressure`: unsigned event-risk magnitude.

Pressure calculation:

```text
weighted = importance_score / 100 * confidence * source_score
direction = +1 bullish, -1 bearish, 0 unknown/mixed
catalyst_pressure = direction * weighted
event_risk_pressure = weighted
```

Feature metadata should include:

- `newswire_event_id`
- `headline`
- `event_type`
- `urgency`
- `importance_score`
- `sentiment`
- `confidence`
- `source_score`

### Regime changes

Update `RegimeEngine` to accept configurable:

- `news_catalyst_threshold`
- `news_catalyst_ttl_ms`

Regime computation should only consider news features whose `computed_ts_ms >= as_of_ms - ttl`.

Compute:

```text
news_pressure = max(abs(catalyst_pressure), event_risk_pressure)
news_state = "catalyst" if news_pressure >= threshold else "no_event"
```

Add metadata/derived labels:

```json
{
  "news_risk_tier": "no_event|catalyst|event_risk|event_shock",
  "news_event_count_recent": 0,
  "news_source_event_ids": [],
  "news_direction": "bullish|bearish|mixed|unknown"
}
```

Tier thresholds:

- `< 0.35`: `no_event`
- `0.35–0.49`: `catalyst`
- `0.50–0.74`: `event_risk`
- `>= 0.75`: `event_shock`

Keep `news_state` binary-compatible as `no_event` / `catalyst`, but include `news_risk_tier` in `derived_labels`.

### Conservative strategy gating

Add `engine/strategy_selector.py`.

Use before `strategy.generate(...)` in `InstitutionalEngineService.run_once()`.

Rules:

- Do not auto-enable disabled strategies.
- Do not change `ENGINE_WAVE1C_ENABLED`.
- Do not change paper/live flags.
- Base eligibility still requires valid regime label match.

News-risk gating:

- `no_event` / `catalyst`: no extra suppression.
- `event_risk`: suppress mean-reversion/reversion/range strategies.
- `event_shock`: suppress mean-reversion, reversion, range, microstructure market-making/orderflow, and funding-basis strategies.
- Event-driven news strategies may run only if already enabled by existing wave flags.
- Defensive flat candidates may still be generated, but remain no-trade/flat.

Record selection summary in engine status:

```json
{
  "selected": [...],
  "skipped": [
    {"strategy_id": "...", "reason": "news_event_risk_suppression"}
  ],
  "news_risk_tier": "event_risk"
}
```

### Lifecycle wiring

In `main.py`:

- Instantiate `EngineNewsConsumer`.
- Start it before `newswire_service.start()`.
- Stop it during shutdown.
- Store on `app.state.engine_news_consumer`.

Existing `AgentNewsConsumer` remains unchanged so legacy autonomy/world model continue receiving news.

## Test Plan

Add/extend tests for:

1. `NewswireEvent` → engine normalized event → news features.
2. Bullish BTC news flips `RegimeVector.news_state` to `catalyst`.
3. Stale news older than 60 minutes no longer affects regime.
4. Macro Fed/CPI event proxies to all core symbols.
5. Unknown sentiment creates `event_risk_pressure` but no directional `catalyst_pressure`.
6. Conservative selector suppresses reversion strategies during `event_risk`.
7. `event_shock` suppresses microstructure/funding strategies.
8. Disabled Wave 1C news strategies remain disabled.
9. Legacy autonomy/world model news tests still pass.
10. No migration required; existing `normalized_events` and `feature_values` tables are reused.

Run targeted tests:

```bash
uv run pytest -q tests/test_newswire.py tests/test_engine_regime_features.py tests/test_engine_service.py tests/test_world_model.py
```

Then full suite:

```bash
uv run pytest -q
```

## Rollout / Verification

1. Deploy with:
   - `ENGINE_ENABLED=true`
   - `ENGINE_NEWSFEED_ENABLED=true`
   - `ENGINE_EXECUTION_MODES=shadow`
   - `ENGINE_PAPER_ENABLED=false`
   - `ENGINE_LIVE_ENABLED=false`
   - `ENGINE_WAVE1C_ENABLED=false`

2. Verify:
   - `/newswire/events` shows incoming events.
   - `/engine/events?event_type=newswire` shows bridged events.
   - `/engine/features?asset=BTC&feature_name=event_risk_pressure` shows news features.
   - `/engine/regime/latest?primary_asset=BTC` shows `news_state=catalyst` and expected `news_risk_tier`.

3. If too noisy:
   - raise `ENGINE_NEWS_MIN_IMPORTANCE`,
   - raise `ENGINE_NEWS_MIN_SOURCE_SCORE`,
   - or set `ENGINE_NEWSFEED_ENABLED=false`.

## Acceptance Criteria

- A qualifying newswire item affects the next engine regime snapshot.
- Macro news maps to all core symbols by default.
- News impact decays after 60 minutes.
- Strategy gating is visible in engine status.
- No new DB migration is required.
- No live execution path is introduced.
- No strategy or wave flag is auto-promoted.









<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add engine newsfeed configuration. _(done)_
- [x] 2. Add a newswire-to-engine bridge/consumer. _(done)_
- [x] 3. Convert qualifying NewswireEvents into engine normalized events and features. _(done)_
- [x] 4. Update regime computation to use recent directional and event-risk news pressure with 60-minute decay. _(done)_
- [x] 5. Add conservative strategy selection/gating based on news risk tier. _(done)_
- [x] 6. Wire lifecycle/status/metrics into app startup and engine status. _(done)_
- [x] 7. Add unit/integration tests and documentation. _(done)_

<!-- pi-plan-progress:end -->
