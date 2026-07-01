# Paper-Signoff Canary — 2026-07-01

Scope: local Docker Compose, no live exchange, requested symbols `BTC,ETH,HYPE`.

## Commands run

```bash
VAULT_ENABLED=false docker compose config > /tmp/hta-compose.yml
VAULT_ENABLED=false docker compose up -d --build api newswire world-model trader agent scheduler
curl http://127.0.0.1:8081/health
curl http://127.0.0.1:8081/ready
curl http://127.0.0.1:8081/runtime/status
curl 'http://127.0.0.1:8081/engine/paper-signoff/preflight?symbols=BTC,ETH,HYPE&window_hours=24&limit=1000'
```

## Result

The local canary preflight executed and **did not proceed to paper signoff**.

Observed runtime:

- `api` public on `127.0.0.1:8081`.
- Workers running: `agent`, `newswire`, `scheduler`, `trader`, `world_model`.
- Runtime heartbeats after cleanup of old stopped instances: required running roles `agent`, `newswire`, `scheduler`, `trader`, `world_model`; `stale_worker_count=0`.
- Commands at check time: `0` pending/failed/stale.
- Preflight: `ready_for_paper_signoff=false`.
- Live exchange blocks: none.
- Requested symbol evidence existed for all requested symbols:
  - `BTC`: shadow candidates present.
  - `ETH`: shadow candidates present.
  - `HYPE`: shadow candidates present.

Blocking readiness codes included:

- `insufficient_shadow_observation`
- `missing_core_data`
- `candidate_evidence_link_coverage_low`
- `candidate_risk_gateway_coverage_low`
- `flat_no_trade_risk_evidence_coverage_low`
- `matured_outcome_attribution_coverage_low`
- `risk_gateway_coverage_low`
- `insufficient_active_strategy_count`
- `insufficient_active_strategy_family_count`
- strategy/family/symbol allocation dominance blocks
- `strategy_regime_score_low`
- `replay_comparison_failed`

## Operator interpretation

This is the expected safe outcome: the canary verified that the command/runtime split and preflight path are available, while the readiness gate prevented paper promotion because evidence quality is not yet sufficient.

Do not enable live exchange execution. Continue local shadow collection and fix the readiness blockers before any paper-signoff rerun.

## Rerun checklist

1. Confirm Compose shape:
   - only `api` publishes a host port;
   - `scheduler` starts by default;
   - `VAULT_ENABLED=false` unless deliberately testing Vault.
2. Confirm `/runtime/status` has no stale workers.
3. Confirm `/commands` has no failed/stale claimed commands.
4. Run engine evidence maintenance commands as needed:
   - `POST /engine/strategy-regime-performance/refresh`
   - `POST /engine/bandit-recommendations/run`
   - `POST /engine/replay-comparisons/run`
5. Run preflight:

```bash
curl 'http://127.0.0.1:8081/engine/paper-signoff/preflight?symbols=BTC,ETH,HYPE&window_hours=24&limit=1000'
```

Proceed only when `ready_for_paper_signoff=true`, `live_exchange_blocks=[]`, and the human operator signs off.
