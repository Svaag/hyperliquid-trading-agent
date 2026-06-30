from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.engine.schemas import AllocationDecision, AlphaCandidate

ALLOCATING_STATUSES = {"allocate", "reduce", "require_debate"}


@dataclass(frozen=True)
class DiversityDecision:
    allowed: bool
    reason_codes: list[str]
    metadata: dict[str, Any]
    event: dict[str, Any]


class PortfolioDiversityController:
    """Deterministic portfolio concentration controller for engine allocations."""

    def __init__(self, settings: Any):
        self.enabled = bool(getattr(settings, "engine_diversity_controller_enabled", True))
        self.lookback_ms = int(float(getattr(settings, "engine_diversity_lookback_hours", 24)) * 3_600_000)
        self.strategy_target_share_pct = float(getattr(settings, "engine_diversity_strategy_target_share_pct", 45.0))
        self.strategy_hard_share_pct = float(getattr(settings, "engine_diversity_strategy_hard_share_pct", 55.0))
        self.family_hard_share_pct = float(getattr(settings, "engine_diversity_family_hard_share_pct", 60.0))
        self.symbol_strategy_hard_share_pct = float(getattr(settings, "engine_diversity_symbol_strategy_hard_share_pct", 35.0))
        self.min_window_samples = int(getattr(settings, "engine_diversity_min_window_samples", 10))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "lookback_ms": self.lookback_ms,
            "strategy_target_share_pct": self.strategy_target_share_pct,
            "strategy_hard_share_pct": self.strategy_hard_share_pct,
            "family_hard_share_pct": self.family_hard_share_pct,
            "symbol_strategy_hard_share_pct": self.symbol_strategy_hard_share_pct,
            "min_window_samples": self.min_window_samples,
        }

    async def apply(
        self,
        candidate: AlphaCandidate,
        allocation: AllocationDecision,
        *,
        current_loop_allocations: list[AllocationDecision],
        repository: Any | None,
        timestamp_ms: int,
    ) -> AllocationDecision:
        decision = await self.evaluate(
            candidate,
            allocation,
            current_loop_allocations=current_loop_allocations,
            repository=repository,
            timestamp_ms=timestamp_ms,
        )
        await self._persist_event(decision.event, repository=repository)
        metadata = {**allocation.metadata, "diversity": decision.metadata}
        if decision.allowed:
            return allocation.model_copy(update={"metadata": metadata})
        return allocation.model_copy(
            update={
                "status": "skip",
                "allocated_size": 0.0,
                "allocated_notional_usd": 0.0,
                "risk_usd": 0.0,
                "reason_codes": [*allocation.reason_codes, *decision.reason_codes],
                "metadata": metadata,
            }
        )

    async def evaluate(
        self,
        candidate: AlphaCandidate,
        allocation: AllocationDecision,
        *,
        current_loop_allocations: list[AllocationDecision],
        repository: Any | None,
        timestamp_ms: int,
    ) -> DiversityDecision:
        if not self.enabled:
            return self._decision(True, [], candidate, allocation, timestamp_ms=timestamp_ms, window={}, projected={"controller_disabled": True})
        window_allocations = await self._window_allocations(repository=repository, current_loop_allocations=current_loop_allocations, timestamp_ms=timestamp_ms)
        window = _aggregate(window_allocations)
        if allocation.status not in ALLOCATING_STATUSES or allocation.allocated_notional_usd <= 0:
            return self._decision(True, [], candidate, allocation, timestamp_ms=timestamp_ms, window=window, projected={"passthrough_status": allocation.status})
        projected = _project(window, candidate=candidate, allocation=allocation)
        reasons: list[str] = []
        samples = int(window.get("sample_count") or 0)
        enforce_caps = samples >= self.min_window_samples
        if enforce_caps and projected["strategy_share_pct"] > self.strategy_hard_share_pct:
            reasons.append("strategy_hard_share_exceeded")
        if enforce_caps and projected["family_share_pct"] > self.family_hard_share_pct:
            reasons.append("family_hard_share_exceeded")
        if enforce_caps and projected["symbol_strategy_share_pct"] > self.symbol_strategy_hard_share_pct:
            reasons.append("symbol_strategy_hard_share_exceeded")
        current_strategy_share = float(window.get("strategy_notional", {}).get(candidate.strategy_id, 0.0)) / max(float(window.get("total_notional", 0.0)), 1.0) * 100.0
        if not reasons and samples >= self.min_window_samples and current_strategy_share >= self.strategy_target_share_pct:
            reasons.append("strategy_target_share_throttle")
        return self._decision(not reasons, reasons, candidate, allocation, timestamp_ms=timestamp_ms, window=window, projected=projected)

    async def _window_allocations(self, *, repository: Any | None, current_loop_allocations: list[AllocationDecision], timestamp_ms: int) -> list[dict[str, Any]]:
        cutoff = timestamp_ms - self.lookback_ms
        rows: list[dict[str, Any]] = []
        if repository is not None and getattr(repository, "enabled", False):
            list_allocations = getattr(repository, "list_allocation_decisions", None)
            if callable(list_allocations):
                try:
                    rows.extend(await list_allocations(limit=5000))
                except Exception:
                    rows = []
        for allocation in current_loop_allocations:
            rows.append(allocation.model_dump(mode="json"))
        return [row for row in rows if int(row.get("created_at_ms") or 0) >= cutoff and str(row.get("status") or "") in ALLOCATING_STATUSES and float(row.get("allocated_notional_usd") or 0.0) > 0]

    def _decision(
        self,
        allowed: bool,
        reasons: list[str],
        candidate: AlphaCandidate,
        allocation: AllocationDecision,
        *,
        timestamp_ms: int,
        window: dict[str, Any],
        projected: dict[str, Any],
    ) -> DiversityDecision:
        decision = "allow" if allowed else "throttle"
        metadata = {
            "decision": decision,
            "reason_codes": reasons,
            "lookback_ms": self.lookback_ms,
            "window": window,
            "projected": projected,
        }
        event_id = "div_" + hashlib.sha1(f"{candidate.candidate_id}:{allocation.allocation_id}:{timestamp_ms}:{decision}:{reasons}".encode()).hexdigest()[:24]
        event = {
            "event_id": event_id,
            "candidate_id": candidate.candidate_id,
            "allocation_id": allocation.allocation_id,
            "strategy_id": candidate.strategy_id,
            "strategy_version": candidate.strategy_version,
            "strategy_family": candidate.strategy_family,
            "asset": candidate.asset,
            "venue": candidate.venue,
            "decision": decision,
            "reason_codes": reasons,
            "strategy_share_pct": float(projected.get("strategy_share_pct") or 0.0),
            "family_share_pct": float(projected.get("family_share_pct") or 0.0),
            "symbol_strategy_share_pct": float(projected.get("symbol_strategy_share_pct") or 0.0),
            "created_at_ms": timestamp_ms,
            "metadata": metadata,
        }
        return DiversityDecision(allowed=allowed, reason_codes=reasons, metadata=metadata, event=event)

    async def _persist_event(self, event: dict[str, Any], *, repository: Any | None) -> None:
        if repository is not None and getattr(repository, "enabled", False):
            record = getattr(repository, "record_allocation_diversity_event", None)
            if callable(record):
                await record(event)
            record_concentration = getattr(repository, "record_portfolio_concentration_event", None)
            if callable(record_concentration):
                await record_concentration(event)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0.0
    strategy: dict[str, float] = {}
    family: dict[str, float] = {}
    symbol_strategy: dict[str, float] = {}
    for row in rows:
        notional = float(row.get("allocated_notional_usd") or 0.0)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        strategy_id = str(metadata.get("strategy_id") or row.get("strategy_id") or "unknown")
        strategy_family = str(metadata.get("strategy_family") or row.get("strategy_family") or "unknown")
        asset = str(metadata.get("asset") or row.get("asset") or "UNKNOWN").upper()
        total += notional
        strategy[strategy_id] = strategy.get(strategy_id, 0.0) + notional
        family[strategy_family] = family.get(strategy_family, 0.0) + notional
        key = f"{asset}:{strategy_id}"
        symbol_strategy[key] = symbol_strategy.get(key, 0.0) + notional
    return {
        "sample_count": len(rows),
        "total_notional": total,
        "strategy_notional": strategy,
        "family_notional": family,
        "symbol_strategy_notional": symbol_strategy,
    }


def _project(window: dict[str, Any], *, candidate: AlphaCandidate, allocation: AllocationDecision) -> dict[str, Any]:
    total = float(window.get("total_notional") or 0.0) + allocation.allocated_notional_usd
    strategy_notional = float(window.get("strategy_notional", {}).get(candidate.strategy_id, 0.0)) + allocation.allocated_notional_usd
    family_notional = float(window.get("family_notional", {}).get(candidate.strategy_family, 0.0)) + allocation.allocated_notional_usd
    symbol_strategy_key = f"{candidate.asset}:{candidate.strategy_id}"
    symbol_strategy_notional = float(window.get("symbol_strategy_notional", {}).get(symbol_strategy_key, 0.0)) + allocation.allocated_notional_usd
    denominator = max(total, 1.0)
    return {
        "projected_total_notional": total,
        "strategy_share_pct": strategy_notional / denominator * 100.0,
        "family_share_pct": family_notional / denominator * 100.0,
        "symbol_strategy_share_pct": symbol_strategy_notional / denominator * 100.0,
        "strategy_id": candidate.strategy_id,
        "strategy_family": candidate.strategy_family,
        "symbol_strategy_key": symbol_strategy_key,
    }
