---
created: 2026-06-21T19:32:59.391Z
source: pi-plan-mode
status: accepted-for-execution
---

# HIP-4 Implementation Audit and Remediation Plan

## Summary

I audited the current `app/hip4` implementation against the planning-session recommendations. Overall, the repo now follows the intended macro-architecture: HIP-4 is isolated, disabled by default, read-only/shadow/paper only, has capability probes, registry parsing, WebSocket management, Decimal scanner math, paper ledger, routes, persistence, tests, and a runbook.

However, several high-value hardening gaps remain before I would call it production-quality:

- `hip4_mode` is not enforced strongly enough.
- Paper/manual paths can be attempted with missing or incomplete capabilities.
- Book staleness can be masked because candidates use scan time, not book time.
- `quoteToken` is still partly assumed as `USDC`.
- WebSocket worker startup does not include HIP-4 as a reason to start the worker.
- `outcomeMetaUpdates`, size/tick metadata, docs testnet-only status, and native action schema stability are not fully probed.
- Discord digest formatter exists but is not integrated into a reporting loop.
- Some repository/migration pieces exist but are not fully exercised by upgrade/downgrade or market snapshot persistence tests.
- Manual-ticket route registration is config-gated but not truly capability-gated at registration time.

This plan keeps the current safe posture and adds a hardening pass. It does **not** add live execution.

## Implementation Steps

1. Enforce HIP-4 mode and route capability gates.
2. Harden capability probes and split abstract paper mechanics from exchange action JSON stability.
3. Fix market-data freshness semantics and WebSocket lifecycle integration.
4. Remove remaining `USDC` assumptions and validate quote-token consistency.
5. Tighten scanner, risk, and paper-ledger invariants.
6. Complete persistence coverage and migration validation.
7. Wire operator-visible Discord reporting.
8. Add missing tests and static safety checks.
9. Update runbook and final acceptance checklist.

## Current Compliance Matrix

| Recommendation | Current state | Verdict |
|---|---:|---|
| Dedicated `app/hip4` subsystem | Present | Pass |
| No signing/private keys/`/exchange`/SDK `Exchange` | No live imports found in `app/hip4`; safety tests exist | Pass |
| Config flags disabled by default | Present | Pass |
| Runtime capability probe | Present but incomplete | Partial |
| Graceful degradation on missing `outcomeMeta` | Present | Pass |
| `questions` vs outcomes-only handling | Present | Pass |
| `quoteToken` detection | Present but downstream still partly assumes USDC | Partial |
| Size/tick metadata probe | Flags exist but always `unknown/false` | Partial |
| `outcomeMetaUpdates` probe | Worker identifier exists, real probe absent | Partial |
| Docs/testnet-only status probe | Field exists, always `unknown` | Gap |
| Raw payload persistence | Present | Pass |
| Centralized WS manager | Present | Pass |
| WS worker lifecycle starts for HIP-4 | Missing in `main.py` condition | Gap |
| Decimal-only scanner/paper | Present with static test | Pass |
| Executable depth scanner | Present | Pass |
| Candidate stale-data enforcement | Risk uses candidate scan time, not min book time | Gap |
| Separate stale thresholds | Present | Pass |
| Edge threshold `both` default | Scanner correct; risk `either` semantics incomplete | Partial |
| Paper ledger no negative inventory | Present | Pass |
| RiskGateway integration | Present | Pass |
| Manual ticket non-executable | Present | Pass |
| Manual route config/capability gating | Config-gated; not registration-gated by capability | Partial |
| Discord reporting | Formatter exists; no service integration | Gap |
| Migration reversible | Migration has downgrade | Pass, but needs actual migration test |
| Tests/replay fixtures | Present; missing several hardening cases | Partial |

## Detailed Findings and Required Fixes

### 1. `hip4_mode` must be enforced everywhere

Current issue:
- `hip4_mode` supports `read_only`, `shadow`, and `paper_shadow`, but routes/risk mostly rely on individual booleans.
- A bad config like `HIP4_MODE=read_only` plus `HIP4_PAPER_EXECUTION_ENABLED=true` can still attempt paper execution.

Required behavior:
- `read_only`: registry/books/status only. Reject scan, paper, reconciliation, manual ticket.
- `shadow`: allow registry/books/scan only. Reject paper execution and manual ticket.
- `paper_shadow`: allow scan and paper if corresponding flags are enabled.
- Manual ticket remains separately disabled unless explicit config plus capability allow it.

Implementation details:
- Add helper methods in `Settings` or `app/hip4/risk.py`:
  - `hip4_mode_allows_scan`
  - `hip4_mode_allows_paper`
  - `hip4_mode_allows_manual_ticket`
- Apply these in:
  - `routes.py`
  - `service.py`
  - `risk.py`
- Add config warning/status fields showing contradictory settings.

Acceptance tests:
- `HIP4_MODE=read_only` rejects `/hip4/scan/run`.
- `HIP4_MODE=shadow` rejects `/hip4/paper/execute/{id}` even if paper flag is true.
- `HIP4_MODE=paper_shadow` still requires `HIP4_PAPER_EXECUTION_ENABLED=true`.

### 2. Capability probes need to be stricter and more explicit

Current issue:
- `supports_native_action_modeling` is `true` whenever outcomes and quote token exist.
- Size/tick metadata always unknown.
- `docs_scope_status` always unknown.
- `supports_manual_ticket_export` always false, which is safe but makes config-enabled manual ticket mostly unusable.
- `outcomeMetaUpdates` is not actually probed.

Required changes:
- Split capabilities:
  - `supports_abstract_native_mechanics`
  - `supports_user_outcome_action_json`
  - `supports_manual_ticket_export`
- Paper ledger should require `supports_abstract_native_mechanics`, not stable exchange JSON.
- Manual ticket should not require exchange JSON because it is non-executable, but it should require:
  - `outcome_meta_available`
  - `supports_outcomes`
  - quote token known
  - no testnet-only mainnet violation
  - fresh registry/books
- Keep `supports_user_outcome_action_json=false` in this MVP.
- Add doc-scope probe as either:
  - static docs fixture/test updated manually, or
  - runtime optional docs check disabled by default.
- Add explicit `outcomeMetaUpdates` probe mode:
  - Subscribe with timeout only when `HIP4_PROBE_OUTCOME_META_WS=true`.
  - If no event arrives, set `supports_outcome_meta_ws=false`.
  - Do not fail startup.

Acceptance tests:
- Missing `quoteToken` disables scanner/paper.
- Missing questions enables binary-only mode.
- `supports_user_outcome_action_json` remains false.
- Manual ticket is enabled only for non-executable ticket capability, not exchange JSON capability.
- `outcomeMetaUpdates` timeout degrades to REST polling.

### 3. Fix market-data freshness semantics

Current issue:
- Scanner candidates set `as_of_ms=int(time.time()*1000)` at scan time.
- Risk checks candidate age, not the underlying book timestamps.
- Stale books can be masked if stale data is scanned “now.”

Required behavior:
- Candidate `as_of_ms` must be the **minimum** `as_of_ms` across all executable leg books.
- Candidate proof must include:
  - `book_as_of_ms_by_coin`
  - `min_book_as_of_ms`
  - `max_book_age_ms`
- Scanner must reject stale books before emitting executable candidates.
- Risk must check candidate proof and reject if any leg is stale.

Implementation details:
- In scanner, pass `now_ms` and `max_staleness_ms`.
- Before sizing, require `book_is_fresh(book, now_ms, scan_threshold)`.
- Set candidate `as_of_ms=min(book.as_of_ms for leg books)`.
- In `Hip4RiskChecker`, check both:
  - `now - candidate.as_of_ms`
  - each proof book timestamp if present.

Acceptance tests:
- A stale book fixture cannot produce a candidate.
- A candidate with fresh scan time but stale leg timestamp is rejected.
- Reconnect/resnapshot refreshes timestamps and clears stale flag.

### 4. Start WebSocket worker when HIP-4 needs it

Current issue:
- `main.py` starts the shared WebSocket worker only if:
  - `hyperliquid_ws_enabled`
  - or `position_tracking_enabled`
  - or `autonomy_enabled`
- HIP-4 can subscribe through `Hip4WsSubscriptionManager` while the worker task is not running if those other flags are false.

Required change:
- Update lifespan condition to include:
  - `settings.hip4_enabled and settings.hip4_ws_enabled`

Acceptance tests:
- With only HIP-4 enabled, `ws_worker.start()` task is scheduled.
- With HIP-4 disabled, default runtime behavior unchanged.

### 5. Remove remaining `USDC` assumptions

Current issue:
- Binary scanner uses `outcome.quote_token or "USDC"`.
- Question scanner hardcodes `"USDC"`.
- Paper ledger default quote token is `USDC`.
- Capability probe detects quote token but downstream does not fully enforce consistency.

Required behavior:
- For each candidate, compute quote token from involved outcomes.
- Reject if:
  - quote token missing,
  - quote tokens differ across outcomes in a question,
  - paper ledger quote token does not match candidate quote token.
- Candidate should include `quote_token`.
- Paper ledger should either:
  - maintain one portfolio per quote token, or
  - reject non-configured quote token.

Recommended MVP choice:
- Add `quote_token` to `Hip4Candidate`.
- Keep one paper ledger quote token at a time.
- Reject candidates whose quote token differs from ledger quote token.

Acceptance tests:
- Mixed quote-token question is rejected.
- Missing quote token is rejected.
- Non-USDC quote token is preserved in metadata and not silently converted.

### 6. Tighten risk edge-threshold semantics

Current issue:
- Scanner implements default `both` semantics correctly.
- Risk checker only rejects edge violations when mode is `both`; if mode is `either`, it currently does not explicitly reject when both fail.

Required behavior:
- If `hip4_edge_threshold_mode == "both"`:
  - reject unless `edge_bps >= min_bps AND edge_usd >= min_usd`.
- If `hip4_edge_threshold_mode == "either"`:
  - reject unless `edge_bps >= min_bps OR edge_usd >= min_usd`.

Acceptance tests:
- `either` mode accepts one passing threshold.
- `either` mode rejects when both fail.
- `both` mode rejects when either threshold fails.

### 7. Make capability absence fail closed for paper/manual

Current issue:
- `Hip4RiskChecker` only applies capability gates when `capabilities is not None`.
- Service usually passes capabilities, but a missing/failed probe should be a hard reject for paper/manual.

Required behavior:
- Paper execution and manual ticket require a successful capability probe.
- Shadow scan may degrade but should not paper-execute without capabilities.

Acceptance tests:
- `capabilities=None` rejects paper execution.
- `capabilities=None` rejects manual ticket.
- `capabilities=None` may still allow status/read-only degraded responses.

### 8. Complete persistence behavior

Current issue:
- Tables exist for market snapshots and settlements, but service does not persist market snapshots or settlement records.
- Migration downgrade exists but upgrade/downgrade is not tested.
- Paper portfolio is upserted, but positions are append-only; that may be acceptable as history, but current schema does not make that explicit.

Required behavior:
- Persist market snapshots when REST/WS books are normalized:
  - `coin`
  - `outcome_id`
  - side
  - best bid/ask as strings
  - raw book JSON
  - `as_of_ms`
- Persist settlements when `settledOutcome` is queried.
- Clarify paper position semantics:
  - either current balance table with upsert key `(portfolio_id, token)`,
  - or historical snapshots with `position_id`.
- Recommended MVP choice:
  - Current append-only positions are okay if renamed/treated as snapshots, but add index on `portfolio_id` and `updated_at_ms`.
  - If current-balance semantics are intended, use merge/upsert.

Acceptance tests:
- Market snapshot repository method stores string decimals only.
- Settlement repository method stores raw payload.
- Alembic upgrade/downgrade dry-run or SQLite-compatible structural test passes.
- HIP-4 model section contains no `Float`.

### 9. Wire Discord reporting, not just formatting

Current issue:
- `format_hip4_digest()` exists.
- No service loop or alert sink integration posts HIP-4 digest.

Required behavior:
- Add `Hip4DiscordReporter` or integrate with existing `DiscordAutonomyAlertSink`.
- It must:
  - only start when `HIP4_ENABLED=true`,
  - require `HIP4_DISCORD_DIGEST_ENABLED=true`,
  - require `HIP4_ALERT_CHANNEL_ID`,
  - include degraded status and rejects,
  - never post only profitable candidates.
- It should be best-effort and not block service startup.

Acceptance tests:
- Digest includes stale/degraded warnings.
- Digest includes reject reasons.
- Reporter does not start without channel ID.
- Reporter handles send failure without crashing HIP-4 service.

### 10. Route-level hardening

Current issue:
- `/hip4/reconcile/run` requires auth but has no dedicated config gate.
- Manual route is registered if config is true, regardless of dynamic capability.
- Read routes expose paper portfolio/actions without auth when HIP-4 is enabled.

Required behavior:
- Add `HIP4_RECONCILIATION_ENABLED=false` or gate reconciliation under paper execution.
- Recommended MVP:
  - Add `hip4_reconciliation_enabled: bool = False`.
  - Require auth and config for `/hip4/reconcile/run`.
- Decide read-route auth:
  - Keep public in local/test/dev if desired, but in prod require `AGENT_API_BEARER_TOKEN`.
  - Recommended: status public, all other HIP-4 routes require auth outside local/dev/test through existing `_require_agent_api`.

Manual route:
- FastAPI cannot practically register/unregister based on runtime probe after startup.
- Use config-based registration plus runtime capability rejection.
- Status must expose `manual_ticket_route_registered` and `manual_ticket_capability_allowed`.

Acceptance tests:
- Reconcile route rejects when disabled.
- Manual route is 404 when config disabled.
- Manual route is 403 when config enabled but capability false.
- Non-status HIP-4 routes require auth in prod.

### 11. Improve WebSocket/rate-limit budget behavior

Current issue:
- `Hip4WsSubscriptionManager` dedupes/caps subscriptions.
- Top-N prioritization is basic and does not use depth/activity.
- REST l2 snapshots loop over hot coins with no HIP-4-specific REST weight budget.

Required behavior:
- Add budget metadata:
  - max REST l2 snapshots per scan,
  - max scan interval,
  - subscription cap,
  - skipped coins reason.
- Prioritize:
  1. allowlisted questions,
  2. active candidates,
  3. highest liquidity/depth,
  4. recent activity,
  5. remaining until cap.
- Current implementation can keep placeholders for liquidity/activity, but should expose why a market was selected.

Acceptance tests:
- Subscription cap respected.
- Duplicate coins deduped.
- REST fallback skips beyond max snapshot budget.
- Reconnect calls resnapshot and records timestamp.

### 12. Clarify "HIP-3" vs HIP-4

The request said “HIP-3,” but the supplied context and implementation are HIP-4. For this remediation, do not touch HIP-3. Add a note in the runbook or implementation issue:

- HIP-4 outcome-market implementation is separate from HIP-3 perps/deployers.
- HIP-3 should be audited in a separate plan if needed.

## New/Updated Tests Required

Add or update:

1. `tests/test_hip4_modes.py`
   - mode matrix for read-only/shadow/paper-shadow.

2. `tests/test_hip4_capabilities.py`
   - docs testnet-only fixture,
   - `outcomeMetaUpdates` timeout fixture/fake,
   - size/tick metadata source fake,
   - native action JSON remains false.

3. `tests/test_hip4_staleness.py`
   - stale book cannot produce candidate,
   - candidate `as_of_ms` equals min leg book timestamp,
   - risk rejects stale leg timestamps.

4. `tests/test_hip4_quote_tokens.py`
   - non-USDC token preserved,
   - mixed quote-token question rejected,
   - missing quote-token rejects scanner/paper.

5. `tests/test_hip4_risk.py`
   - capability `None` rejects paper/manual,
   - edge threshold `either` semantics,
   - size/tick metadata reject unless inferred-size flag enabled.

6. `tests/test_hip4_routes.py`
   - reconcile config gate,
   - manual route config/capability behavior,
   - prod auth for non-status routes.

7. `tests/test_hip4_persistence.py`
   - market snapshot persistence,
   - settlement persistence,
   - migration upgrade/downgrade structural test.

8. `tests/test_hip4_discord.py`
   - digest contains degraded state and reject reasons,
   - reporter no-op without configured channel,
   - send failure is contained.

9. `tests/test_hip4_ws.py`
   - HIP-4 starts shared WS worker when needed,
   - reconnect resnapshot behavior,
   - REST fallback budget.

## Acceptance Criteria

The remediation is complete only when:

- `HIP4_MODE` is enforced consistently.
- HIP-4 paper/manual actions fail closed without successful capabilities.
- Scanner uses book timestamps, not scan time, for candidate freshness.
- Stale books cannot produce executable candidates.
- Quote token is never silently assumed as USDC for candidate math.
- WebSocket worker starts when HIP-4 is the only WS consumer.
- Capability probe reports docs scope, outcome meta WS support, size/tick metadata, abstract mechanics support, and exchange JSON support separately.
- Manual ticket remains non-executable and route behavior is both config-gated and capability-gated at runtime.
- Discord reporting is actually wired.
- Market snapshots and settlements are persisted or explicitly marked out-of-scope with tests proving no route depends on them.
- Full HIP-4 tests pass.
- Full repo tests pass.
- No `app/hip4` imports signing, SDK `Exchange`, private keys, or `/exchange` mutation helpers.

## Recommended Execution Order

1. Mode/risk fail-closed fixes.
2. Staleness and quote-token fixes.
3. Capability probe expansion.
4. WS lifecycle and rate-budget fixes.
5. Persistence completion.
6. Discord reporter wiring.
7. Tests and runbook update.

## Non-Goals

Do not add:
- live orders,
- signed actions,
- private keys,
- SDK `Exchange`,
- `/exchange` calls,
- executable `userOutcome` JSON,
- LLM-controlled execution,
- autonomy/perps-engine promotion,
- HIP-3 changes.




<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Enforce HIP-4 mode and route capability gates. _(done)_
- [x] 2. Harden capability probes and split abstract paper mechani... _(done)_
- [x] 3. Fix market-data freshness semantics and WebSocket lifecyc... _(done)_
- [x] 4. Remove remaining USDC assumptions and validate quote-toke... _(done)_
- [x] 5. Tighten scanner, risk, and paper-ledger invariants. _(done)_
- [x] 6. Complete persistence coverage and migration validation. _(done)_
- [x] 7. Wire operator-visible Discord reporting. _(done)_
- [x] 8. Add missing tests and static safety checks. _(done)_
- [x] 9. Update runbook and final acceptance checklist. _(done)_

<!-- pi-plan-progress:end -->
