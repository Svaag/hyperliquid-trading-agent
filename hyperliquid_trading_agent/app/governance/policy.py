from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.governance.schemas import MemoryStatus

VALIDATED_ADVISORY_CONTEXTS = {"research", "reviewer", "shadow", "report"}
FORBIDDEN_EXECUTION_CONTEXTS = {"execution", "execution_review", "risk_gateway", "live", "order_router"}
ROLE_CONTEXTS = {
    "analyst": "strategy",
    "quant": "reviewer",
    "research": "research",
    "risk": "risk",
    "treasury": "capital_review",
    "execution": "execution_review",
    "adversary": "reviewer",
    "judge": "reviewer",
}
SENSITIVE_ROLES = {"risk", "execution", "treasury"}


@dataclass(frozen=True)
class MemoryPolicyDecision:
    allowed: bool
    memory_id: str
    role: str
    context_type: str
    status: str
    reason: str

    def model_dump(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "memory_id": self.memory_id,
            "role": self.role,
            "context_type": self.context_type,
            "status": self.status,
            "reason": self.reason,
        }


class MemoryPolicyEngine:
    """Deterministic policy for memory prompt injection.

    Candidate/validated advisory memories are recommendation context, not policy.
    Approved-policy memories require explicit promotion metadata and contexts.
    """

    def context_for_role(self, role: str) -> str:
        return ROLE_CONTEXTS.get(_role_key(role), "reviewer")

    def can_inject(
        self,
        memory: Any,
        *,
        role: str,
        context_type: str | None = None,
        mode: str = "paper",
    ) -> MemoryPolicyDecision:
        role_key = _role_key(role)
        context = context_type or self.context_for_role(role_key)
        memory_id = str(getattr(memory, "id", None) or getattr(memory, "memory_id", "unknown"))
        status = self.memory_status(memory)
        allowed_contexts = _string_set(getattr(memory, "allowed_contexts", None))
        forbidden_contexts = _string_set(getattr(memory, "forbidden_contexts", None))
        metadata = dict(getattr(memory, "metadata", {}) or {})
        allowed_roles = _string_set(metadata.get("approved_for_role_injection_roles"))

        if mode == "live" and status != "approved_policy":
            return MemoryPolicyDecision(False, memory_id, role_key, context, status, "only approved_policy may enter live contexts")
        if context in forbidden_contexts or (context in FORBIDDEN_EXECUTION_CONTEXTS and status != "approved_policy"):
            return MemoryPolicyDecision(False, memory_id, role_key, context, status, "forbidden execution/risk context")
        if status in {"candidate", "deprecated", "reverted"}:
            return MemoryPolicyDecision(False, memory_id, role_key, context, status, f"status {status} is not injectable")
        if status == "validated_advisory":
            if context in allowed_contexts or (not allowed_contexts and context in VALIDATED_ADVISORY_CONTEXTS):
                return MemoryPolicyDecision(True, memory_id, role_key, context, status, "validated advisory context")
            return MemoryPolicyDecision(False, memory_id, role_key, context, status, "validated advisory not allowed for this context")
        if status == "approved_policy":
            if allowed_contexts and context not in allowed_contexts and role_key not in allowed_roles:
                return MemoryPolicyDecision(False, memory_id, role_key, context, status, "approved policy context not listed")
            if role_key in SENSITIVE_ROLES and not (metadata.get("change_control_id") and (role_key in allowed_roles or context in allowed_contexts)):
                return MemoryPolicyDecision(False, memory_id, role_key, context, status, "sensitive role requires change-control approval")
            return MemoryPolicyDecision(True, memory_id, role_key, context, status, "approved policy")
        return MemoryPolicyDecision(False, memory_id, role_key, context, status, "unknown memory status")

    def memory_status(self, memory: Any) -> MemoryStatus:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        metadata_status = metadata.get("memory_status")
        if metadata_status in {"candidate", "validated_advisory", "approved_policy", "deprecated", "reverted"}:
            return metadata_status  # type: ignore[return-value]
        explicit = getattr(memory, "memory_status", None)
        validation_status = str(getattr(memory, "validation_status", "") or "").lower()
        if explicit == "approved_policy":
            return "approved_policy"
        if validation_status == "active" and metadata.get("change_control_id"):
            return "approved_policy"
        if explicit in {"candidate", "validated_advisory", "deprecated", "reverted"}:
            return explicit  # type: ignore[return-value]
        if validation_status == "shadow":
            return "validated_advisory"
        if validation_status == "active":
            return "validated_advisory"
        if validation_status in {"archived", "expired", "rejected"}:
            return "deprecated"
        return "candidate"


def default_allowed_contexts(status: str, *, role: str | None = None, metadata: dict[str, Any] | None = None) -> list[str]:
    metadata = metadata or {}
    if status == "validated_advisory":
        return sorted(VALIDATED_ADVISORY_CONTEXTS)
    if status == "approved_policy":
        approved_roles = _string_set(metadata.get("approved_for_role_injection_roles"))
        contexts = {ROLE_CONTEXTS.get(role, "reviewer") for role in approved_roles}
        if role:
            contexts.add(ROLE_CONTEXTS.get(_role_key(role), "reviewer"))
        return sorted(contexts - {""})
    return []


def default_forbidden_contexts(status: str) -> list[str]:
    if status in {"candidate", "validated_advisory", "deprecated", "reverted"}:
        return sorted(FORBIDDEN_EXECUTION_CONTEXTS | {"risk", "capital_review", "strategy"})
    return []


def _role_key(role: str) -> str:
    return role.lower().strip().replace("-", "_")


def _string_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    try:
        return {str(item).lower().strip().replace("-", "_") for item in values if str(item).strip()}
    except TypeError:
        return set()
