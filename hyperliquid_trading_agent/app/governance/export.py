from __future__ import annotations

import time
from typing import Any

from hyperliquid_trading_agent.app.security import redact_secrets

REVIEW_EXPORT_STATUSES = {"review_ready", "approved", "rejected", "needs_more_evidence"}
PAPER_ONLY_CAVEATS = [
    "Replay and shadow results are historical or simulated evidence; they are not proof of live performance.",
    "Paper fills do not reproduce queue position, latency, market impact, partial fills, or venue failure modes.",
    "This bundle is read-only and cannot apply configuration, alter risk limits, place orders, or grant execution authority.",
]


class ReviewExportService:
    """Build a redacted, read-only review bundle for one governance proposal."""

    def __init__(self, *, repository: Any):
        self.repository = repository

    async def build(self, proposal_id: str, *, active_refs: dict[str, Any] | None = None) -> dict[str, Any]:
        proposal = await self.repository.get_candidate_config_diff(proposal_id)
        if proposal is None:
            raise KeyError("proposal not found")

        status = str(proposal.get("status") or "proposed")
        packets = await self.repository.list_review_packets(proposal_id=proposal_id, limit=100)
        if status not in REVIEW_EXPORT_STATUSES or not packets:
            raise PermissionError("proposal is not review-ready")

        replays = await self.repository.list_replay_results(proposal_id=proposal_id, limit=100)
        shadows = await self.repository.list_shadow_comparisons(proposal_id=proposal_id, limit=100)
        if not replays or not shadows:
            raise PermissionError("proposal review evidence is incomplete")
        latest_packet = max(packets, key=lambda item: int(item.get("created_at_ms") or 0))
        rollback_plan = await self._load_rollback_plan(str(latest_packet.get("rollback_plan_id") or ""))
        if rollback_plan is None:
            raise PermissionError("proposal rollback plan is unavailable")
        decisions = await self._list_promotion_decisions(proposal_id)
        evidence = await self._resolve_evidence([str(item) for item in proposal.get("evidence") or []])
        decision_contexts = await self._load_decision_contexts(proposal, replays)

        bundle = {
            "schema_version": 1,
            "export_type": "governance_review_bundle",
            "generated_at_ms": int(time.time() * 1000),
            "proposal_id": proposal_id,
            "candidate_diff": proposal,
            "evidence": evidence,
            "validation": {
                "replay_results": replays,
                "shadow_comparisons": shadows,
                "replay_count": len(replays),
                "shadow_count": len(shadows),
            },
            "review": {
                "latest_packet": latest_packet,
                "packets": packets,
                "promotion_decisions": decisions,
                "risk_direction": proposal.get("risk_direction") or "unknown",
                "requires_human_approval": bool(proposal.get("requires_human_approval", True)),
                "approval_requirements": list(latest_packet.get("approval_requirements") or []),
            },
            "rollback_plan": rollback_plan,
            "runtime_references": {
                "active": active_refs or {},
                "decision_contexts": decision_contexts,
            },
            "authority": {
                "mode": "review_export_only",
                "execution_authority": False,
                "config_mutation_authority": False,
                "auto_apply_allowed": False,
                "apply_performed": False,
                "exchange_actions": [],
            },
            "caveats": PAPER_ONLY_CAVEATS,
        }
        return redact_secrets(bundle)

    async def _load_rollback_plan(self, rollback_plan_id: str) -> dict[str, Any] | None:
        get_plan = getattr(self.repository, "get_rollback_plan", None)
        if not rollback_plan_id or not callable(get_plan):
            return None
        return await get_plan(rollback_plan_id)

    async def _list_promotion_decisions(self, proposal_id: str) -> list[dict[str, Any]]:
        list_decisions = getattr(self.repository, "list_promotion_decisions", None)
        if not callable(list_decisions):
            return []
        return await list_decisions(proposal_id=proposal_id, limit=100)

    async def _resolve_evidence(self, evidence_ids: list[str]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for evidence_id in evidence_ids:
            summary = await self._resolve_evidence_item(evidence_id)
            if summary is None:
                unresolved.append(evidence_id)
            else:
                items.append(summary)
        return {"requested_ids": evidence_ids, "items": items, "unresolved_ids": unresolved}

    async def _resolve_evidence_item(self, evidence_id: str) -> dict[str, Any] | None:
        lookups = (
            ("alpha_event_evaluation", "get_alpha_event_evaluation", _alpha_evaluation_summary),
            ("newswire_event", "get_newswire_event", _newswire_summary),
            ("normalized_event", "get_normalized_event", _normalized_event_summary),
        )
        for evidence_type, method_name, summarize in lookups:
            lookup = getattr(self.repository, method_name, None)
            if not callable(lookup):
                continue
            try:
                item = await lookup(evidence_id)
            except Exception:  # pragma: no cover - an unavailable evidence store must not break export
                continue
            if item is not None:
                return {"evidence_id": evidence_id, "evidence_type": evidence_type, "summary": summarize(item)}

        lookup_by_event = getattr(self.repository, "get_alpha_event_evaluation_by_event_id", None)
        if callable(lookup_by_event):
            try:
                matches = await lookup_by_event(evidence_id)
            except Exception:  # pragma: no cover
                matches = []
            if matches:
                return {
                    "evidence_id": evidence_id,
                    "evidence_type": "alpha_event_evaluation",
                    "summary": [_alpha_evaluation_summary(item) for item in matches],
                }
        return None

    async def _load_decision_contexts(self, proposal: dict[str, Any], replays: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decision_ids: set[str] = set()
        metadata = proposal.get("metadata") or {}
        for key in ("decision_id", "decision_context_id"):
            if metadata.get(key):
                decision_ids.add(str(metadata[key]))
        decision_ids.update(str(item) for item in metadata.get("decision_ids") or [] if item)
        for replay in replays:
            if replay.get("decision_id"):
                decision_ids.add(str(replay["decision_id"]))
            replay_metadata = replay.get("metadata") or {}
            if replay_metadata.get("decision_id"):
                decision_ids.add(str(replay_metadata["decision_id"]))

        get_context = getattr(self.repository, "get_decision_context", None)
        if not callable(get_context):
            return []
        contexts: list[dict[str, Any]] = []
        for decision_id in sorted(decision_ids):
            context = await get_context(decision_id)
            if context is not None:
                contexts.append(context)
        return contexts


def _pick(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: item[key] for key in keys if key in item}


def _alpha_evaluation_summary(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        item,
        "id",
        "event_id",
        "event_source",
        "provider",
        "event_type",
        "asset_class",
        "symbol",
        "direction",
        "status",
        "terminal_outcome",
        "importance_score",
        "realized_or_marked_bps",
        "received_at_ms",
        "completed_at_ms",
    )


def _newswire_summary(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        item,
        "event_id",
        "story_id",
        "source",
        "provider",
        "published_at_ms",
        "headline",
        "symbols",
        "asset_class",
        "event_type",
        "urgency",
        "importance_score",
        "sentiment",
        "freshness",
        "confidence",
    )


def _normalized_event_summary(item: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        item,
        "event_id",
        "event_type",
        "asset_class",
        "symbols",
        "source",
        "provider",
        "event_ts_ms",
        "received_ts_ms",
        "quality_score",
        "staleness_ms",
    )
