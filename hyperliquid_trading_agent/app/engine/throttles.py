from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate


class StrategyThrottleController:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cooldowns: dict[str, int] = defaultdict(int)
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.reason_counts: Counter[str] = Counter()
        self.strategy_reason_counts: Counter[str] = Counter()
        self.last_recent_share_pct: dict[str, float] = {}
        self.last_decision_at_ms: int | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_strategy_throttles_enabled,
            "cooldowns": dict(self.cooldowns),
            "reason_counts": dict(self.reason_counts),
            "strategy_reason_counts": dict(self.strategy_reason_counts),
            "last_recent_share_pct": dict(self.last_recent_share_pct),
            "last_decision_at_ms": self.last_decision_at_ms,
            "recent_events": list(self.events)[-20:],
            "settings": {
                "max_candidates_per_loop": self.settings.engine_strategy_max_candidates_per_loop,
                "max_allocations_per_loop": self.settings.engine_strategy_max_allocations_per_loop,
                "max_allocation_share_pct": self.settings.engine_strategy_max_allocation_share_pct,
                "lookback_hours": self.settings.engine_strategy_throttle_lookback_hours,
                "cooldown_loops": self.settings.engine_strategy_throttle_cooldown_loops,
            },
        }

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
                event = {"type": "candidate_throttled", "strategy_id": strategy_id, "candidate_id": candidate.candidate_id, "reason": "max_candidates_per_loop", "timestamp_ms": timestamp_ms}
                events.append(event)
                self._record_event(event)
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
        if candidate.side == "flat" or not bool(candidate.portfolio_concentration_impact.get("opens_position", True)):
            metadata = {"throttle_reason": "defensive_no_trade_control", "allocation_expected": False}
            self._record_event(
                {
                    "type": "allocation_observed",
                    "strategy_id": strategy_id,
                    "reason": "defensive_no_trade_control",
                    "timestamp_ms": timestamp_ms,
                    **metadata,
                }
            )
            return True, [], metadata
        shadow_observation = bool(self.settings.engine_shadow_enabled and not self.settings.engine_paper_enabled)
        current_count = sum(1 for item in current_loop_allocations if item.status in {"allocate", "reduce", "require_debate"} and _candidate_strategy_hint(item) == strategy_id)
        self.last_decision_at_ms = timestamp_ms
        if current_count >= self.settings.engine_strategy_max_allocations_per_loop:
            metadata = {"throttle_reason": "max_allocations_per_loop", "current_loop_allocations": current_count}
            self._record_event({"type": "allocation_throttled", "strategy_id": strategy_id, "reason": "max_allocations_per_loop", "timestamp_ms": timestamp_ms, **metadata})
            return False, ["strategy_throttle"], metadata
        recent = await self._recent_allocation_share(repository, strategy_id, now_ms=timestamp_ms)
        recent_share = recent["share_pct"]
        self.last_recent_share_pct[strategy_id] = recent_share
        metadata = {
            "recent_allocation_share_pct": recent_share,
            "recent_allocation_count": recent["strategy_count"],
            "recent_total_allocations": recent["total_count"],
            "recent_lookback_hours": self.settings.engine_strategy_throttle_lookback_hours,
        }
        min_samples = max(2, self.settings.engine_strategy_max_allocations_per_loop * 2)
        if recent["total_count"] >= min_samples and recent_share > self.settings.engine_strategy_max_allocation_share_pct:
            if shadow_observation:
                allowed = {**metadata, "throttle_reason": "recent_allocation_share_report_only", "shadow_observation_report_only": True}
                self._record_event({"type": "allocation_allowed", "strategy_id": strategy_id, "reason": "recent_allocation_share_report_only", "timestamp_ms": timestamp_ms, **allowed})
                return True, [], allowed
            self.cooldowns[strategy_id] = max(self.cooldowns[strategy_id], self.settings.engine_strategy_throttle_cooldown_loops)
            blocked = {**metadata, "throttle_reason": "recent_allocation_share"}
            self._record_event({"type": "allocation_throttled", "strategy_id": strategy_id, "reason": "recent_allocation_share", "timestamp_ms": timestamp_ms, **blocked})
            return False, ["strategy_throttle"], blocked
        if self.cooldowns.get(strategy_id, 0) > 0:
            if shadow_observation:
                allowed = {**metadata, "throttle_reason": "cooldown_report_only", "remaining_loops": self.cooldowns[strategy_id], "shadow_observation_report_only": True}
                self._record_event({"type": "allocation_allowed", "strategy_id": strategy_id, "reason": "cooldown_report_only", "timestamp_ms": timestamp_ms, **allowed})
                return True, [], allowed
            self.cooldowns[strategy_id] -= 1
            blocked = {**metadata, "throttle_reason": "cooldown", "remaining_loops": self.cooldowns[strategy_id]}
            self._record_event({"type": "allocation_throttled", "strategy_id": strategy_id, "reason": "cooldown", "timestamp_ms": timestamp_ms, **blocked})
            return False, ["strategy_throttle"], blocked
        self._record_event({"type": "allocation_allowed", "strategy_id": strategy_id, "reason": "allowed", "timestamp_ms": timestamp_ms, **metadata})
        return True, [], metadata

    async def _recent_allocation_share(self, repository: Any, strategy_id: str, *, now_ms: int) -> dict[str, Any]:
        if repository is None or not getattr(repository, "enabled", False):
            return {"share_pct": 0.0, "strategy_count": 0, "total_count": 0}
        lookback_ms = max(1, self.settings.engine_strategy_throttle_lookback_hours) * 60 * 60 * 1000
        start_ms = now_ms - lookback_ms
        allocations = await repository.list_allocation_decisions(limit=5000)
        counts: Counter[str] = Counter()
        for allocation in allocations:
            if int(allocation.get("created_at_ms") or 0) < start_ms:
                continue
            if allocation.get("status") not in {"allocate", "reduce", "require_debate"}:
                continue
            strategy = _allocation_strategy(allocation)
            if strategy:
                counts[strategy] += 1
        total = sum(counts.values())
        strategy_count = counts[strategy_id]
        share = round(strategy_count / total * 100, 4) if total else 0.0
        return {"share_pct": share, "strategy_count": strategy_count, "total_count": total}

    def _record_event(self, event: dict[str, Any]) -> None:
        reason = str(event.get("reason") or "unknown")
        strategy = str(event.get("strategy_id") or "unknown")
        if event.get("type") in {"candidate_throttled", "allocation_throttled"}:
            self.reason_counts[reason] += 1
            self.strategy_reason_counts[f"{strategy}:{reason}"] += 1
        self.events.append(event)


def _candidate_strategy_hint(allocation: AllocationDecision) -> str:
    # AllocationDecision does not carry strategy_id as a top-level field today.
    # The orchestrator attaches it in metadata for current-loop throttling.
    return str(allocation.metadata.get("strategy_id") or "unknown")


def _allocation_strategy(allocation: dict[str, Any]) -> str:
    metadata = allocation.get("metadata") or allocation.get("metadata_json") or {}
    if isinstance(metadata, dict):
        strategy = metadata.get("strategy_id")
        if strategy:
            return str(strategy)
    return ""
