from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent.app.newswire.policy import NewsPolicyVersion


def train_contextual_bandit_policy(*, decisions: list[dict[str, Any]], rewards: list[dict[str, Any]], min_rows: int = 50) -> NewsPolicyVersion:
    """Build a deterministic candidate policy from logged rewards.

    V1 is an offline contextual-bandit parameter learner: contexts are source and
    event-type buckets, actions are the logged Newswire/engine actions, and rewards
    update bucket-level action values. The output is params, not executable code.
    """

    reward_by_decision = {str(item.get("decision_id") or ""): float(item.get("total_reward") or 0.0) for item in rewards if item.get("decision_id")}
    rows = [item for item in decisions if str(item.get("decision_id") or "") in reward_by_decision]
    now = _now_ms()
    policy_version = "newswire_bandit_" + hashlib.sha1(f"{now}:{len(rows)}".encode()).hexdigest()[:12]
    source_values: dict[str, list[float]] = defaultdict(list)
    event_type_values: dict[str, list[float]] = defaultdict(list)
    action_values: dict[str, list[float]] = defaultdict(list)
    for decision in rows:
        reward = reward_by_decision[str(decision.get("decision_id"))]
        source_values[str(decision.get("source") or "unknown")].append(reward)
        event_type_values[str(decision.get("event_type") or "unknown")].append(reward)
        action_values[f"{decision.get('newswire_action')}:{decision.get('engine_action')}"].append(reward)

    params = {
        "learner": "contextual_bandit_v1",
        "min_rows": min_rows,
        "row_count": len(rows),
        "ready": len(rows) >= min_rows,
        "source_reputation": {key: _reward_to_reputation(values) for key, values in source_values.items()},
        "event_type_value": {key: _avg(values) for key, values in event_type_values.items()},
        "action_value": {key: _avg(values) for key, values in action_values.items()},
    }
    return NewsPolicyVersion(
        policy_version=policy_version,
        policy_type="bandit",
        status="candidate",
        params=params,
        replay_metrics={
            "row_count": len(rows),
            "avg_reward": _avg(list(reward_by_decision.values())),
            "ready": len(rows) >= min_rows,
        },
        created_at_ms=now,
        metadata={"offline_only": True, "manual_promotion_required": True},
    )


def _reward_to_reputation(values: list[float]) -> float:
    avg = _avg(values)
    normalized = max(-1.0, min(1.0, avg / 8.0))
    return round(max(0.0, min(1.0, 0.5 + 0.5 * normalized)), 4)


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)
