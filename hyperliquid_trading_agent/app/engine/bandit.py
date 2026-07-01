from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import BanditPolicySnapshot, BanditRecommendation

CONTEXT_FEATURES = ["strategy_id", "strategy_family", "regime_label", "asset", "candidate_count", "allocation_count", "score"]
WAVE2_POLICY_ACTION_SPACE = [
    "strategy_weight_bucket",
    "candidate_quota_bucket",
    "min_confidence_threshold",
    "min_ev_threshold",
    "cooldown_bucket",
    "no_trade",
    "shadow_only_experiment",
]
WAVE2_REWARD_TERMS = [
    "net_pnl",
    "realized_r",
    "slippage",
    "fees",
    "drawdown_penalty",
    "risk_reject_penalty",
    "Council_veto_penalty",
    "concentration_penalty",
    "replay_failure_penalty",
]
WAVE2_FORBIDDEN_ACTIONS = [
    "place_orders",
    "raise_leverage",
    "bypass_RiskGateway",
    "bypass_Council",
    "auto_apply_production_config",
]


class OfflineContextualBanditReporter:
    """Report-only offline contextual-bandit recommender.

    This component writes policy snapshots and recommendations only. It never mutates
    runtime config, risk limits, strategy weights, or orders.
    """

    def __init__(self, repository: Any):
        self.repository = repository

    async def run(self, *, window_start_ms: int, window_end_ms: int, limit: int = 1000) -> dict[str, Any]:
        rows = await self.repository.list_strategy_regime_performance(limit=limit)
        rows = [row for row in rows if window_start_ms <= int(row.get("window_end_ms") or row.get("created_at_ms") or 0) <= window_end_ms]
        specs = await _list_specs(self.repository)
        arms = sorted({str(spec.get("strategy_id")) for spec in specs if spec.get("enabled", True) and spec.get("counts_for_breadth", True)})
        ts = now_ms()
        policy_id = "bandit_policy_" + hashlib.sha1(f"{window_start_ms}:{window_end_ms}:{len(rows)}".encode()).hexdigest()[:24]
        policy = BanditPolicySnapshot(
            policy_id=policy_id,
            policy_version="offline_report_only_v1",
            status="report_only",
            trained_window_start_ms=window_start_ms,
            trained_window_end_ms=window_end_ms,
            context_features=CONTEXT_FEATURES,
            arms=arms,
            policy_json=_policy_summary(rows),
            created_at_ms=ts,
            metadata={
                "auto_apply_allowed": False,
                "row_count": len(rows),
                "wave2_policy_action_space": WAVE2_POLICY_ACTION_SPACE,
                "wave2_reward_terms": WAVE2_REWARD_TERMS,
                "forbidden_actions": WAVE2_FORBIDDEN_ACTIONS,
            },
        )
        await self.repository.upsert_bandit_policy_snapshot(policy.model_dump(mode="json"))
        recommendations = self._recommendations(policy, rows=rows, specs=specs, created_at_ms=ts)
        for recommendation in recommendations:
            await self.repository.record_bandit_recommendation(recommendation.model_dump(mode="json"))
        return {
            "policy": policy.model_dump(mode="json"),
            "recommendations": [item.model_dump(mode="json") for item in recommendations],
            "recommendation_count": len(recommendations),
            "report_only": True,
            "auto_apply_allowed": False,
        }

    def _recommendations(self, policy: BanditPolicySnapshot, *, rows: list[dict[str, Any]], specs: list[dict[str, Any]], created_at_ms: int) -> list[BanditRecommendation]:
        by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_strategy[str(row.get("strategy_id") or "unknown")].append(row)
        out: list[BanditRecommendation] = []
        for strategy_id, strategy_rows in sorted(by_strategy.items()):
            best = max(strategy_rows, key=lambda item: float(item.get("score") or 0.0))
            avg_score = sum(float(item.get("score") or 0.0) for item in strategy_rows) / len(strategy_rows)
            if avg_score >= 70:
                recommendation = "increase_shadow_observation_priority"
                delta = min(10.0, (avg_score - 70.0) / 3.0)
            elif avg_score < 45:
                recommendation = "reduce_paper_readiness_weight_until_more_evidence"
                delta = -min(15.0, (45.0 - avg_score) / 2.0)
            else:
                recommendation = "maintain_shadow_weight_collect_more_samples"
                delta = 0.0
            out.append(_recommendation(policy.policy_id, strategy_id, best, recommendation, avg_score=avg_score, delta=delta, created_at_ms=created_at_ms))
        observed = set(by_strategy)
        for spec in specs:
            strategy_id = str(spec.get("strategy_id") or "unknown")
            if strategy_id in observed or not spec.get("enabled", True) or not spec.get("counts_for_breadth", True):
                continue
            row = {"strategy_id": strategy_id, "asset": "GLOBAL", "regime_label": "insufficient_evidence", "score": 0, "strategy_family": spec.get("family")}
            out.append(_recommendation(policy.policy_id, strategy_id, row, "collect_shadow_evidence_before_weighting", avg_score=0.0, delta=0.0, created_at_ms=created_at_ms))
        return out


def _recommendation(policy_id: str, strategy_id: str, row: dict[str, Any], recommendation: str, *, avg_score: float, delta: float, created_at_ms: int) -> BanditRecommendation:
    digest = hashlib.sha1(f"{policy_id}:{strategy_id}:{row.get('regime_label')}:{recommendation}".encode()).hexdigest()[:24]
    confidence = max(0.0, min(1.0, float(row.get("candidate_count") or 0) / 20.0))
    return BanditRecommendation(
        recommendation_id="bandit_rec_" + digest,
        policy_id=policy_id,
        strategy_id=strategy_id,
        asset=str(row.get("asset") or "GLOBAL").upper(),
        regime_label=str(row.get("regime_label") or "unknown"),
        recommendation=recommendation,
        confidence=round(confidence, 4),
        expected_score_delta=round(delta, 4),
        auto_apply_allowed=False,
        created_at_ms=created_at_ms,
        metadata={
            "report_only": True,
            "avg_score": round(avg_score, 4),
            "strategy_family": row.get("strategy_family"),
            "source": "offline_contextual_bandit_v1",
            "config_mutation": False,
            "order_mutation": False,
            "allowed_action_space": WAVE2_POLICY_ACTION_SPACE,
            "forbidden_actions": WAVE2_FORBIDDEN_ACTIONS,
        },
    )


def _policy_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_strategy[str(row.get("strategy_id") or "unknown")].append(float(row.get("score") or 0.0))
    return {
        "algorithm": "deterministic_ucb_style_report_v1",
        "auto_apply_allowed": False,
        "strategy_scores": {strategy: round(sum(values) / len(values), 4) for strategy, values in sorted(by_strategy.items()) if values},
    }


async def _list_specs(repository: Any) -> list[dict[str, Any]]:
    method = getattr(repository, "list_strategy_specs", None)
    if not callable(method):
        return []
    return await method(limit=500)
