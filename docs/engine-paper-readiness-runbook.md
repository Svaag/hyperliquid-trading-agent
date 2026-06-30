# Engine Paper-Readiness Promotion and Rollback Runbook

## Safety posture

Live execution remains forbidden. Do not enable private keys, signed exchange adapters, live order routes, `HYPERLIQUID_EXCHANGE_ENABLED`, `ALPACA_TRADING_ENABLED`, or `ENGINE_LIVE_ENABLED`.

Repository defaults and `.env.example` are shadow-only. Paper mode is an explicit operator promotion step after `/engine/readiness` passes.

## Shadow-only baseline

```env
ENGINE_ENABLED=true
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
```

## Required checks before paper mode

Paper mode may be promoted only when all are true:

- `GET /engine/readiness` returns `ready_for_paper=true`.
- Score is `>=85` and `hard_blocks=[]`.
- Shadow observation is at least 24h.
- Candidate sample is at least 250 and shadow intents at least 50.
- Paper intent/report count during shadow-only is zero.
- Live flags and live intent/report counts are zero.
- Feature and regime coverage are at least 95% for core symbols.
- Risk reject rate is at most 25%.
- Allocation rate is between 5% and 60%.
- Strategy allocation share is at most 55%.
- Average simulated slippage is at most 8 bps.
- Latest engine replay comparison is not `candidate_worse`.
- Discord engine validation digest is operational.
- Unified `/dashboard` and `/engine/dashboard` are accessible.

## Required operator review

1. Open `/dashboard` and verify the Overview and Readiness tabs.
2. Open `/engine/readiness` and save the JSON artifact.
3. Open `/engine/validation-report` and save the JSON artifact.
4. Run or inspect a 24h replay comparison via `/engine/replay-comparisons/latest`.
5. Confirm the latest Discord digest has no critical alerts.
6. Confirm no live execution configuration is present.

## Promotion to paper/shadow

Only after the checklist above passes, update env:

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=true
ENGINE_EXECUTION_MODES=paper,shadow
ENGINE_LIVE_ENABLED=false
```

Then rebuild/reload the service and immediately verify:

```http
GET /engine/status
GET /engine/readiness
GET /engine/validation-report
```

## Rollback to shadow-only

Rollback env:

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=false
ENGINE_EXECUTION_MODES=shadow
ENGINE_LIVE_ENABLED=false
```

Reload Docker and verify `/engine/status` shows `paper_enabled=false` and `execution_modes=["shadow"]`.

## Immediate rollback triggers

Rollback immediately if any occur:

- `ENGINE_LIVE_ENABLED=true` or live intent/report appears.
- Readiness grade becomes `blocked` after promotion.
- Paper orders occur outside the paper adapter path.
- Paper intent/report appears while shadow-only is configured.
- Risk reject spike is dominated by stale or invalid data.
- PnL attribution loop is stale for more than two intervals while positions are active.
- Any strategy exceeds 70% allocation share.
- Average simulated/paper slippage exceeds 15 bps over the latest 20 reports.

## Incident response

1. Roll back to shadow-only.
2. Preserve `/engine/readiness`, `/engine/validation-report`, latest replay artifact, and Docker logs.
3. File an issue with timestamps, config hash, readiness hard blocks, and suspected root cause.
4. Do not re-promote until the readiness scorecard passes again and the incident has an explicit remediation note.
