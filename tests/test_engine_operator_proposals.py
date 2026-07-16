from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.operator_proposals import EngineOperatorProposalService


class _ProposalRepository:
    def __init__(self, context: dict[str, Any]):
        self.context = context
        self.proposals: dict[str, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []

    async def expire_engine_operator_proposals(self, **kwargs) -> int:
        return 0

    async def latest_candidate_book_snapshot(self) -> dict[str, Any]:
        return {"candidate_book_id": "book_1", "candidate_ids": [self.context["candidate"]["candidate_id"]]}

    async def get_alpha_candidate(self, candidate_id: str) -> dict[str, Any]:
        return self.context["candidate"]

    async def list_candidate_trade_packets(self, **kwargs) -> list[dict[str, Any]]:
        return [{"packet": self.context["packet"]}]

    async def list_ev_estimates(self, **kwargs) -> list[dict[str, Any]]:
        return [self.context["ev"]]

    async def list_allocation_decisions(self, **kwargs) -> list[dict[str, Any]]:
        return [self.context["allocation"]]

    async def list_council_reviews(self, **kwargs) -> list[dict[str, Any]]:
        return [self.context["council"]]

    async def list_debate_decisions(self, **kwargs) -> list[dict[str, Any]]:
        debate = self.context.get("debate")
        return [debate] if debate else []

    async def list_engine_operator_proposals(self, **kwargs) -> list[dict[str, Any]]:
        return list(self.proposals.values())

    async def get_engine_operator_proposal_by_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        return next((item for item in self.proposals.values() if item["candidate_id"] == candidate_id), None)

    async def upsert_engine_operator_proposal(self, proposal: dict[str, Any]) -> str:
        stored = {**deepcopy(proposal), "status": "proposed"}
        self.proposals[str(proposal["proposal_id"])] = stored
        return str(proposal["proposal_id"])

    async def enqueue_operational_notification(self, **kwargs) -> str:
        self.notifications.append(deepcopy(kwargs))
        return "opn_test"

    async def update_engine_operator_proposal_status(
        self,
        proposal_id: str,
        *,
        status: str,
        actor: str,
        now_ms: int,
        reason: str = "",
    ) -> dict[str, Any] | None:
        item = self.proposals.get(proposal_id)
        if item is None or item["status"] != "proposed":
            return item
        item["status"] = status
        if status == "acknowledged":
            item["acknowledged_by"] = actor
            item["acknowledged_at_ms"] = now_ms
        if status == "rejected":
            item["rejected_by"] = actor
            item["rejection_reason"] = reason
        return item


def _context(**candidate_updates: Any) -> dict[str, Any]:
    now = int(time.time() * 1000)
    candidate = {
        "candidate_id": "cand_eligible",
        "strategy_id": "funding_carry_v1",
        "strategy_version": "1.0.0",
        "asset": "BTC",
        "asset_class": "crypto",
        "side": "long",
        "proposed_entry": 100.0,
        "stop": 98.0,
        "targets": [104.0],
        "thesis": "Funding dislocation with positive institutional EV.",
        "invalidation_conditions": ["funding normalizes"],
        "feature_snapshot_id": "features_1",
        "regime_snapshot_id": "regime_1",
        "raw_alpha_score": 88.0,
        "confidence": 0.8,
        "feature_coverage_pct": 100.0,
        "counts_for_breadth": True,
        "expires_at_ms": now + 60 * 60_000,
        "source_integrity": {
            "activation_scope": "paper_shadow",
            "promotion_state": "paper_approved",
            "paper_eligible": True,
        },
    }
    candidate.update(candidate_updates)
    ev = {
        "candidate_id": candidate["candidate_id"],
        "net_ev_bps": 15.0,
        "risk_adjusted_utility": 0.5,
    }
    allocation = {
        "candidate_id": candidate["candidate_id"],
        "status": "allocate",
        "allocated_size": 100.0,
        "allocated_notional_usd": 10_000.0,
        "risk_usd": 250.0,
        "reason_codes": [],
    }
    packet = {
        "packet_id": "packet_1",
        "candidate": candidate,
        "ev_estimate": ev,
        "allocation": allocation,
        "risk_decision": {"decision": "allow", "allowed": True, "violations": []},
    }
    council = {
        "review_id": "council_1",
        "candidate_id": candidate["candidate_id"],
        "decision": "allow_shadow",
        "vetoes": [],
        "required_evidence": [],
    }
    return {
        "candidate": candidate,
        "ev": ev,
        "allocation": allocation,
        "packet": packet,
        "council": council,
    }


def _settings(**updates: Any) -> Settings:
    return Settings(
        _env_file=None,
        engine_enabled=True,
        engine_operator_proposals_enabled=True,
        engine_operator_shadow_digest_enabled=False,
        autonomy_alert_channel_id="alerts",
        **updates,
    )


def test_institutional_candidate_becomes_deduplicated_operator_proposal() -> None:
    async def run() -> tuple[dict[str, Any], dict[str, Any], _ProposalRepository]:
        repo = _ProposalRepository(_context())
        service = EngineOperatorProposalService(settings=_settings(), repository=repo)  # type: ignore[arg-type]
        first = await service.process_candidate_book("book_1")
        second = await service.process_candidate_book("book_1")
        return first, second, repo

    first, second, repo = anyio.run(run)

    assert first["created"] == 1
    assert second["created"] == 0
    proposal = next(iter(repo.proposals.values()))
    assert proposal["proposal_id"].startswith("eng_prop_")
    assert proposal["metadata"]["paper_execution_allowed"] is False
    assert proposal["payload"]["candidate"]["candidate_id"] == "cand_eligible"
    assert "signal" not in proposal["payload"]
    assert len(repo.notifications) == 1
    assert repo.notifications[0]["category"] == "engine_operator_proposal"
    assert "does not create a paper or live order" in repo.notifications[0]["payload"]["content"]


def test_operator_acknowledgment_changes_review_state_without_order_side_effect() -> None:
    async def run() -> dict[str, Any]:
        repo = _ProposalRepository(_context())
        service = EngineOperatorProposalService(settings=_settings(), repository=repo)  # type: ignore[arg-type]
        await service.process_candidate_book("book_1")
        proposal_id = next(iter(repo.proposals))
        acknowledged = await service.acknowledge(proposal_id, actor="operator-1")
        assert acknowledged is not None
        return acknowledged

    acknowledged = anyio.run(run)

    assert acknowledged["status"] == "acknowledged"
    assert "order" not in acknowledged
    assert acknowledged["metadata"]["acknowledgment_only"] is True
    assert acknowledged["metadata"]["paper_execution_allowed"] is False


def test_non_breadth_and_candidates_below_hard_floors_never_produce_proposals() -> None:
    async def run(context: dict[str, Any]) -> tuple[dict[str, Any], _ProposalRepository]:
        repo = _ProposalRepository(context)
        service = EngineOperatorProposalService(settings=_settings(), repository=repo)  # type: ignore[arg-type]
        return await service.process_candidate_book("book_1"), repo

    non_breadth_result, non_breadth_repo = anyio.run(
        run,
        _context(counts_for_breadth=False),
    )
    weak = _context(confidence=0.4, feature_coverage_pct=70.0)
    weak["ev"]["net_ev_bps"] = 11.0
    weak["ev"]["risk_adjusted_utility"] = 0.2
    weak["packet"]["ev_estimate"] = weak["ev"]
    weak_result, weak_repo = anyio.run(run, weak)

    assert non_breadth_result["created"] == 0
    assert "not_alpha_breadth" in non_breadth_result["blockers"]
    assert weak_result["created"] == 0
    assert {
        "net_ev_below_operator_minimum",
        "utility_below_operator_minimum",
        "confidence_below_operator_minimum",
        "feature_coverage_below_operator_minimum",
    } <= set(weak_result["blockers"])
    assert non_breadth_repo.proposals == weak_repo.proposals == {}


def test_shadow_digest_displays_actual_research_governance_blockers() -> None:
    async def run() -> _ProposalRepository:
        context = _context(
            source_integrity={
                "activation_scope": "shadow_only",
                "promotion_state": "research_only",
                "paper_eligible": False,
            }
        )
        repo = _ProposalRepository(context)
        settings = _settings().model_copy(
            update={
                "engine_operator_shadow_digest_enabled": True,
                "engine_operator_shadow_digest_interval_seconds": 60,
            }
        )
        service = EngineOperatorProposalService(settings=settings, repository=repo)  # type: ignore[arg-type]
        result = await service.process_candidate_book("book_1")
        assert result["blockers"]["shadow_only_strategy"] == 1
        assert result["blockers"]["strategy_version_research_only"] == 1
        assert result["blockers"]["not_paper_eligible"] == 1
        return repo

    repo = anyio.run(run)

    assert len(repo.notifications) == 1
    notification = repo.notifications[0]
    assert notification["category"] == "engine_shadow_digest"
    content = notification["payload"]["content"]
    assert "shadow_only_strategy" in content
    assert "strategy_version_research_only" in content
    assert "not_paper_eligible" in content
