# Engine Shadow Observation and Paper-Promotion Package

This package is intentionally operator-executed. The repository now defaults to shadow-only; do not enable paper until the readiness gate passes.

## Shadow deployment env

```env
ENGINE_ENABLED=true
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
ENGINE_READINESS_CLEAN_WINDOW_START_MS=<deployment_ms>
```

Enable read-only data feeds only:

- Hyperliquid mids/L2/meta contexts
- liquidation read-only adapters, if available
- no signed exchange adapters
- no private keys

## Observation run

1. Deploy the current build with the env above.
2. Record `<deployment_ms>` and set `ENGINE_READINESS_CLEAN_WINDOW_START_MS`.
3. Let the engine observe for at least 24h.
4. During observation, periodically inspect:

```http
GET /engine/status
GET /engine/validation-report
GET /engine/readiness
GET /engine/council-reviews
GET /engine/diversity-events
GET /engine/strategy-regime-performance
```

## End-of-window evidence refresh

```http
POST /engine/strategy-regime-performance/refresh
POST /engine/replay-comparisons/run
POST /engine/bandit-recommendations/run
GET /engine/replay-comparisons/latest
GET /engine/readiness
```

Bandit recommendations are report-only. Confirm every recommendation has `auto_apply_allowed=false`.

## Promotion condition

Paper promotion remains blocked unless:

```json
{
  "ready_for_paper": true,
  "grade": "pass",
  "hard_blocks": []
}
```

Only after that human review may apply:

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=true
ENGINE_EXECUTION_MODES=paper,shadow
ENGINE_LIVE_ENABLED=false
```

Live execution remains out of scope and must stay disabled.
