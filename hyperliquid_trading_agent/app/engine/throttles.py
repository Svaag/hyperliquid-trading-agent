from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate


class StrategyThrottleController:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cooldowns: dict[str, int] = defaultdict(int)

    async def filter_candidates(
        self,
        candidates: list[AlphaCandidate],
        *,
        repository: Any,
        timestamp_ms: int,
    ) -> tuple[list[AlphaCandidate], list[dict[str, Any]]]:
        if not self.settings.engine_strategy_throttles_enabled:
            return candidates, []
        kept: list[AlphaCandidate] = []
        events: list[dict[str, Any]] = []
        grouped: dict[str, list[AlphaCandidate]] = defaultdict(list)
        for candidate in candidates:
            grouped[candidate.strategy_id].append(candidate)
        for strategy_id, items in grouped.items():
            ranked = sorted(items, key=lambda item: (item.raw_alpha_score, item.confidence, item.created_at_ms), reverse=True)
            limit = max(1, self.settings.engine_strategy_max_candidates_per_loop)
            kept.extend(ranked[:limit])
            for candidate in ranked[limit:]:
                throttled = candidate.model_copy(update={"status": "throttled", "metadata": {**candidate.metadata, "throttle_reason": "max_candidates_per_loop", "exchange_actions": []}})
                events.append({"type": "candidate_throttled", "strategy_id": strategy_id, "candidate_id": candidate.candidate_id, "reason": "max_candidates_per_loop", "timestamp_ms": timestamp_ms})
                if repository is not None and getattr(repository, "enabled", False):
                    record = getattr(repository, "record_alpha_candidate", None)
                    if callable(record):
                        await record(throttled.model_dump(mode="json"))
        return kept, events

    async def allow_allocation(
        self,
        candidate: AlphaCandidate,
        *,
        current_loop_allocations: list[AllocationDecision],
        repository: Any,
        timestamp_ms: int,
    ) -> tuple[bool, list[str], dict[str, Any]]:
        if not self.settings.engine_strategy_throttles_enabled:
            return True, [], {}
        strategy_id = candidate.strategy_id
        current_count = sum(1 for item in current_loop_allocations if item.status in {"allocate", "reduce", "require_debate"} and _candidate_strategy_hint(item) == strategy_id)
        if current_count >= self.settings.engine_strategy_max_allocations_per_loop:
            return False, ["strategy_throttle"], {"throttle_reason": "max_allocations_per_loop", "current_loop_allocations": current_count}
        recent_share = await self._recent_allocation_share(repository, strategy_id)
        if recent_share > self.settings.engine_strategy_max_allocation_share_pct:
            self.cooldowns[strategy_id] = max(self.cooldowns[strategy_id], self.settings.engine_strategy_throttle_cooldown_loops)
            return False, ["strategy_throttle"], {"throttle_reason": "recent_allocation_share", "recent_allocation_share_pct": recent_share}
        if self.cooldowns.get(strategy_id, 0) > 0:
            self.cooldowns[strategy_id] -= 1
            return False, ["strategy_throttle"], {"throttle_reason": "cooldown", "remaining_loops": self.cooldowns[strategy_id]}
        return True, [], {"recent_allocation_share_pct": recent_share}

    async def _recent_allocation_share(self, repository: Any, strategy_id: str) -> float:
        if repository is None or not getattr(repository, "enabled", False):
            return 0.0
        allocations = await repository.list_allocation_decisions(limit=1000)
        candidates = await repository.list_alpha_candidates(limit=1000)
        candidate_strategy = {str(item.get("candidate_id")): str(item.get("strategy_id") or "unknown") for item in candidates}
        counts: Counter[str] = Counter()
        for allocation in allocations:
            if allocation.get("status") in {"allocate", "reduce", "require_debate"}:
                counts[candidate_strategy.get(str(allocation.get("candidate_id")), "unknown")] += 1
        total = sum(counts.values())
        return round(counts[strategy_id] / total * 100, 4) if total else 0.0


def _candidate_strategy_hint(allocation: AllocationDecision) -> str:
    # AllocationDecision does not carry strategy_id today. The orchestrator attaches
    # it in metadata for current-loop throttling.
    return str(allocation.metadata.get("strategy_id") or "unknown")
