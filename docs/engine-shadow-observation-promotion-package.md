# Engine Shadow Observation and Paper-Promotion Package

This package is intentionally operator-executed. The repository now defaults to shadow-only; do not enable paper until the readiness gate passes.

## Shadow deployment env

```env
ENGINE_ENABLED=true
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
ENGINE_ALPHA_CATALOG_MODE=wave1a_locked
ENGINE_CROSS_VENUE_DEXES=
ENGINE_READINESS_CLEAN_WINDOW_START_MS=<deployment_ms>
```

To collect broad model evidence without granting paper eligibility, switch only the catalog mode:

```env
ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
```

This activates the full shadow alpha catalog and marks research candidates `paper_eligible=false`; `/engine/readiness` reports their shadow breadth separately from paper-eligible breadth.

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

## Full shadow alpha catalog observation

When `ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog` is active, verify:

- `GET /engine/strategy-catalog` shows the expanded catalog and `shadow_only` runtime strategies.
- `GET /engine/readiness` shows `active_shadow_strategy_count` / `active_shadow_family_count` separately from `paper_eligible_active_*`.
- Paper/live settings remain disabled; RiskGateway and Council coverage remain mandatory.
- No paper promotion is inferred from shadow-only research breadth.

## Wave 1C canary condition

Wave 1C canary remains blocked until the same shadow window proves the Wave 1B spine is reliable:

- `GET /engine/readiness` has no evidence-spine hard blocks.
- Candidate evidence link coverage is 100%.
- Candidate-level RiskGateway coverage is 100% for non-flat candidates.
- Flat/no-trade candidates have explicit no-exposure RiskGateway evidence.
- Matured outcome attribution coverage is at least 95%.
- Council packet coverage is at least 95%.
- Latest replay is `passed` or `advisory_pass`.
- Paper/live flags remain disabled during the canary.

Only after those checks pass may an operator canary Wave 1C by changing:

```env
ENGINE_WAVE1C_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_WAVE2_ENABLED=false
```

If readiness hard-blocks, replay fails, or concentration breaches during canary, roll back with:

```env
ENGINE_WAVE1C_ENABLED=false
```

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
