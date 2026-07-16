from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import (
    AllocationScope,
    AlphaCandidate,
    StrategySpec,
    StrategyVersionPolicy,
)


class StrategyPromotionPolicyService:
    """Exact-version promotion policy with fail-closed defaults.

    The first catalog observed in a fresh store is frozen.  Any implementation
    version introduced after that bootstrap is research-only until a separate,
    audited promotion process explicitly changes its persisted policy.
    """

    def __init__(self, repository: Any | None = None):
        self.repository = repository
        self._policies: dict[str, StrategyVersionPolicy] = {}
        self._initialized = False

    async def ensure_registry(self, specs: Iterable[StrategySpec]) -> list[StrategyVersionPolicy]:
        specs = list(specs)
        persisted: list[dict[str, Any]] = []
        if self.repository is not None and getattr(self.repository, "enabled", False):
            method = getattr(self.repository, "list_strategy_version_policies", None)
            if callable(method):
                persisted = list(await method(limit=10_000))
        for row in persisted:
            policy = StrategyVersionPolicy.model_validate(row)
            self._policies[policy.strategy_version_key] = policy

        bootstrap = not self._policies
        timestamp = now_ms()
        for spec in specs:
            key = self.key(spec.strategy_id, spec.version)
            if key in self._policies:
                continue
            policy = StrategyVersionPolicy(
                strategy_version_key=key,
                strategy_id=spec.strategy_id,
                strategy_version=spec.version,
                state="frozen" if bootstrap else "research_only",
                reason_codes=(
                    [
                        "current_strategy_version_frozen",
                        "negative_strict_cohort_review_2026_07_16",
                    ]
                    if bootstrap
                    else ["new_strategy_version_requires_promotion_evidence"]
                ),
                effective_from_ms=timestamp,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
                metadata={"bootstrap_catalog": bootstrap, "mutation_authority": "external_governance_only"},
            )
            self._policies[key] = policy
            if self.repository is not None and getattr(self.repository, "enabled", False):
                persist = getattr(self.repository, "upsert_strategy_version_policy", None)
                if callable(persist):
                    await persist(policy.model_dump(mode="json"))
        self._initialized = True
        return self.list()

    @staticmethod
    def key(strategy_id: str, strategy_version: str) -> str:
        return f"{strategy_id}@{strategy_version}"

    def get(self, strategy_id: str, strategy_version: str) -> StrategyVersionPolicy:
        key = self.key(strategy_id, strategy_version)
        policy = self._policies.get(key)
        if policy is not None:
            timestamp = now_ms()
            if timestamp < policy.effective_from_ms:
                return policy.model_copy(
                    update={
                        "state": "research_only",
                        "reason_codes": [*policy.reason_codes, "strategy_version_policy_not_yet_effective"],
                        "metadata": {**policy.metadata, "ephemeral_fail_closed_default": True},
                    }
                )
            if policy.effective_until_ms is not None and timestamp >= policy.effective_until_ms:
                return policy.model_copy(
                    update={
                        "state": "research_only",
                        "reason_codes": [*policy.reason_codes, "strategy_version_policy_expired"],
                        "metadata": {**policy.metadata, "ephemeral_fail_closed_default": True},
                    }
                )
            return policy
        timestamp = now_ms()
        return StrategyVersionPolicy(
            strategy_version_key=key,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            state="research_only",
            reason_codes=["missing_exact_version_policy"],
            effective_from_ms=timestamp,
            created_at_ms=timestamp,
            updated_at_ms=timestamp,
            metadata={"ephemeral_fail_closed_default": True},
        )

    def apply(self, candidate: AlphaCandidate) -> AlphaCandidate:
        policy = self.get(candidate.strategy_id, candidate.strategy_version)
        paper_eligible = policy.state == "paper_approved"
        return candidate.model_copy(
            update={
                "source_integrity": {
                    **candidate.source_integrity,
                    "strategy_version_key": policy.strategy_version_key,
                    "promotion_state": policy.state,
                    "paper_eligible": paper_eligible,
                },
                "metadata": {
                    **candidate.metadata,
                    "strategy_version_key": policy.strategy_version_key,
                    "promotion_state": policy.state,
                    "promotion_reason_codes": policy.reason_codes,
                    "paper_eligible": paper_eligible,
                },
            }
        )

    def allocation_scope(self, candidate: AlphaCandidate) -> AllocationScope:
        if candidate.side == "flat":
            return "defensive"
        return (
            "paper_eligible"
            if self.get(candidate.strategy_id, candidate.strategy_version).state == "paper_approved"
            else "research"
        )

    def paper_eligible(self, candidate: AlphaCandidate) -> bool:
        return self.get(candidate.strategy_id, candidate.strategy_version).state == "paper_approved"

    def list(self) -> list[StrategyVersionPolicy]:
        return sorted(self._policies.values(), key=lambda item: item.strategy_version_key)

    def status(self) -> dict[str, Any]:
        counts = Counter(policy.state for policy in self._policies.values())
        return {
            "initialized": self._initialized,
            "exact_version_policy_count": len(self._policies),
            "state_counts": dict(sorted(counts.items())),
            "missing_version_default": "research_only",
            "paper_requires": "paper_approved",
        }
