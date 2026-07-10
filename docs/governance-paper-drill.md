# Paper-Only Governance Review Drill

This drill proves the proposal → replay → shadow → review packet → portable review export path without applying configuration or placing an order. Run it against a disposable paper/shadow proposal with linked evidence.

## Preconditions

Export the local operator connection details without echoing the token:

```bash
export AGENT_BASE_URL="http://127.0.0.1:${HOST_PORT:-8081}"
export AGENT_API_BEARER_TOKEN="<operator-token>"
```

Confirm the database is at the repository migration head and the runtime is healthy:

```bash
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic heads
curl -fsS -H "Authorization: Bearer ${AGENT_API_BEARER_TOKEN}" "${AGENT_BASE_URL}/health"
curl -fsS -H "Authorization: Bearer ${AGENT_API_BEARER_TOKEN}" "${AGENT_BASE_URL}/ready"
python -m hyperliquid_trading_agent.app.governance.cli dashboard-url
```

Expected observations:

- Alembic current and heads match (at least governance migration `0010`; current repository head is `0028`).
- `/health` returns healthy and `/ready` has no database/runtime blocker.
- The governance dashboard reports `paper_only: true`. Stop if Hyperliquid or Alpaca exchange execution is enabled.

## Run the Drill

List proposals and choose a disposable candidate with evidence. Do not use a live or operator-critical proposal:

```bash
python -m hyperliquid_trading_agent.app.governance.cli list-proposals
export PROPOSAL_ID="tp_replace_with_disposable_proposal"
python -m hyperliquid_trading_agent.app.governance.cli show-proposal "${PROPOSAL_ID}"
```

Record the active runtime references before validation:

```bash
python -m hyperliquid_trading_agent.app.governance.cli show-active-config > /tmp/governance-active-before.json
```

Run replay and shadow validation, create the review packet, and export the complete review bundle:

```bash
python -m hyperliquid_trading_agent.app.governance.cli run-replay "${PROPOSAL_ID}"
python -m hyperliquid_trading_agent.app.governance.cli run-shadow "${PROPOSAL_ID}"
python -m hyperliquid_trading_agent.app.governance.cli create-review-packet "${PROPOSAL_ID}"
python -m hyperliquid_trading_agent.app.governance.cli export-review "${PROPOSAL_ID}" > /tmp/governance-review-bundle.json
```

Inspect the evidence and authority boundary:

```bash
python -m json.tool /tmp/governance-review-bundle.json
python -m hyperliquid_trading_agent.app.governance.cli list-replays --proposal-id "${PROPOSAL_ID}"
python -m hyperliquid_trading_agent.app.governance.cli list-shadows --proposal-id "${PROPOSAL_ID}"
python -m hyperliquid_trading_agent.app.governance.cli list-review-packets --proposal-id "${PROPOSAL_ID}"
```

The review bundle must contain:

- the candidate diff and compact evidence summaries, including unresolved evidence IDs;
- all proposal replay and shadow records and their caveats;
- the latest review packet, approval requirements, and promotion decisions;
- a complete rollback plan;
- active config, risk, prompt, model-route, and linked decision-context references;
- `execution_authority: false`, `config_mutation_authority: false`, `auto_apply_allowed: false`, `apply_performed: false`, and an empty `exchange_actions` list.

Reject the disposable proposal to finish the human-decision leg without changing runtime configuration:

```bash
python -m hyperliquid_trading_agent.app.governance.cli reject-proposal "${PROPOSAL_ID}" \
  --reviewer "paper-drill-operator" \
  --rationale "Paper-only governance drill; no runtime change requested"
python -m hyperliquid_trading_agent.app.governance.cli export-review "${PROPOSAL_ID}" > /tmp/governance-review-bundle-final.json
python -m hyperliquid_trading_agent.app.governance.cli show-active-config > /tmp/governance-active-after.json
diff -u /tmp/governance-active-before.json /tmp/governance-active-after.json
```

Expected observations:

- the proposal status becomes `rejected` and a promotion decision is present in the final export;
- active config/risk/prompt/model-route references are unchanged;
- no paper order, live order, exchange action, or config apply is created by export or rejection;
- the rollback plan remains available even though no change was applied.

## Database Audit Queries

Use read-only queries to retain drill evidence:

```bash
docker compose exec -T postgres psql -U "${POSTGRES_USER:-hlagent}" -d "${POSTGRES_DB:-hlagent}" -c \
  "select proposal_id,status,risk_direction,requires_human_approval,auto_apply_allowed from candidate_config_diffs where proposal_id='${PROPOSAL_ID}';"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-hlagent}" -d "${POSTGRES_DB:-hlagent}" -c \
  "select replay_id,status,created_at_ms from replay_results where proposal_id='${PROPOSAL_ID}' order by created_at_ms;"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-hlagent}" -d "${POSTGRES_DB:-hlagent}" -c \
  "select comparison_id,status,recommendation,created_at_ms from shadow_comparisons where proposal_id='${PROPOSAL_ID}' order by created_at_ms;"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-hlagent}" -d "${POSTGRES_DB:-hlagent}" -c \
  "select review_packet_id,rollback_plan_id,created_at_ms from review_packets where proposal_id='${PROPOSAL_ID}' order by created_at_ms;"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-hlagent}" -d "${POSTGRES_DB:-hlagent}" -c \
  "select decision_id,decision,reviewer,change_control_id,created_at_ms from promotion_decisions where proposal_id='${PROPOSAL_ID}' order by created_at_ms;"
```

Attach the two exported bundles, command output, and query output to the issue/PR as the durable paper-only drill artifact. The export itself never writes audit rows; only the explicit replay, shadow, review-packet, and human decision commands do.
