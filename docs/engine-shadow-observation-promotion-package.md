# Engine Shadow Observation and Paper-Promotion Package

This package is intentionally operator-executed. The repository now defaults to shadow-only; do not enable paper until the readiness gate passes.

## Shadow deployment env

```env
ENGINE_ENABLED=true
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
ENGINE_ALPHA_CATALOG_MODE=integrated
ENGINE_WAVE1C_ENABLED=true
ENGINE_WAVE2_ENABLED=true
ENGINE_CROSS_VENUE_DEXES=lighter,xyz,alpaca:paper
ENGINE_READINESS_CLEAN_WINDOW_START_MS=<deployment_ms>
```

The integrated catalog evaluates Wave 1 and all Wave 2A/2B/2C strategies through one readiness and reporting path while global execution remains shadow-only.

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
GET /engine/strategy-catalog
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

## Unified catalog observation condition

Paper promotion remains blocked until the integrated Wave 1/Wave 2 shadow window proves the evidence spine is reliable:

- `GET /engine/readiness` has no evidence-spine hard blocks.
- Candidate evidence link coverage is 100%.
- Candidate-level RiskGateway coverage is 100% for non-flat candidates.
- Flat/no-trade candidates have explicit no-exposure RiskGateway evidence.
- Matured outcome attribution coverage is at least 95%.
- Council packet coverage is at least 95%.
- Latest replay is `passed` or `advisory_pass`.
- Paper/live flags remain disabled during observation.
- `GET /engine/strategy-catalog` shows every Wave 2A/2B/2C strategy as `paper_shadow`, `paper_eligible=true`, and first-class.
- The normal digest and readiness reports include Wave 2 alongside Wave 1 rather than in a separate research population.

If readiness hard-blocks, replay fails, or concentration breaches, keep execution shadow-only and investigate the failing strategies or data feeds.

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
