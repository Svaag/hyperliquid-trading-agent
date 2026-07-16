# Engine Paper-Readiness Promotion and Rollback Runbook

## Safety posture

Live execution remains forbidden. Do not enable private keys, signed exchange adapters, live order routes, `HYPERLIQUID_EXCHANGE_ENABLED`, `ALPACA_TRADING_ENABLED`, or `ENGINE_LIVE_ENABLED`.

Repository defaults and `.env.example` are shadow-only. Migration `0034` freezes every exact strategy version present at upgrade time, and unseen versions default to `research_only`. There is no runtime promotion endpoint. Paper mode requires both a passing `/engine/readiness` artifact and a separately reviewed exact-version policy change; environment flags alone grant no strategy paper authority. Use `docs/engine-shadow-observation-promotion-package.md` to collect the required clean-window evidence.

## Shadow-only baseline

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
```

This baseline evaluates Wave 1 and every Wave 2A/2B/2C strategy as one research portfolio. Their candidates share the same evidence path, but current exact versions remain frozen and allocations are reported under the research scope.

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
- Strategy allocation share is at most 55%, family share at most 60%, and symbol+strategy share at most 35%.
- Candidate strategy metadata coverage is 100%.
- RiskGateway coverage is 100% and Council review coverage is at least 95% for allocated candidates.
- Strategy-regime evidence coverage is at least 95% with minimum score/sample thresholds.
- Strategy breadth checks use **paper-eligible** active strategies/families across the unified Wave 1/Wave 2 portfolio; intentionally shadow-only sources do not satisfy paper promotion.
- Every strategy is identified by exact `strategy_id@version` and has an externally reviewed `paper_approved` policy; missing policies fail closed.
- The approved scorer artifact is trained on strict native-horizon outcomes with expanding walk-forward validation; strategy-supplied edge contributes zero.
- Each strategy/horizon has at least 30 effective non-overlapping time blocks, with positive 95% lower bounds for realized R and measured execution-adjusted return.
- Execution evidence uses fresh multi-level venue books and verified account/market fee tiers. Configured ceilings, top-of-book-only, stale, and unavailable quotes are not promotion evidence.
- Average measured simulated slippage is at most 8 bps.
- Latest engine replay comparison is `passed` or `advisory_pass` for a >=24h, >=50-candidate window.
- Discord engine validation digest is operational.
- Unified `/dashboard` and `/engine/dashboard` are accessible.

## Required operator review

1. Open `/dashboard` and verify the Overview and Readiness tabs.
2. Open `/engine/readiness` and save the JSON artifact.
3. Open `/engine/validation-report` and save the JSON artifact.
4. Run or inspect a 24h replay comparison via `/engine/replay-comparisons/latest`.
5. Inspect `/engine/strategy-catalog`, `/engine/strategy-version-policies`, `/engine/execution-cost-quotes`, `/engine/strategy-regime-performance`, `/engine/council-reviews`, and `/engine/diversity-events`.
6. Inspect `/engine/signal-quality` and `/engine/strategy-research`; confirm raw candidate counts are not being treated as independent sample counts.
7. Optionally run `/engine/bandit-recommendations/run`; confirm all recommendations are report-only with `auto_apply_allowed=false`.
8. Confirm the latest Discord digest has no critical alerts and displays the actual hard-block codes.
9. Confirm no live execution configuration is present.

## Promotion to paper/shadow

The current frozen versions must not be promoted. For a separately developed version, only after the checklist above passes and an audited governance change records that exact version as `paper_approved`, update env:

```env
ENGINE_SHADOW_ENABLED=true
ENGINE_PAPER_ENABLED=true
ENGINE_EXECUTION_MODES=paper,shadow
ENGINE_LIVE_ENABLED=false
ENGINE_ALPHA_CATALOG_MODE=integrated
```

Then rebuild/reload the service and immediately verify:

```http
GET /engine/status
GET /engine/readiness
GET /engine/validation-report
GET /engine/strategy-regime-performance
GET /engine/council-reviews
GET /engine/replay-comparisons/latest
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
