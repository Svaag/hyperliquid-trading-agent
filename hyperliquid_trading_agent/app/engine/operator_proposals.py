from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ProposalEvaluation:
    candidate_id: str
    candidate: dict[str, Any]
    ev: dict[str, Any]
    allocation: dict[str, Any]
    packet: dict[str, Any]
    council: dict[str, Any]
    debate: dict[str, Any]
    hard_blockers: list[str] = field(default_factory=list)
    soft_blockers: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.hard_blockers and not self.soft_blockers

    @property
    def rank(self) -> tuple[float, float, float, float]:
        return (
            float(self.ev.get("net_ev_bps") or 0),
            float(self.ev.get("risk_adjusted_utility") or 0),
            float(self.candidate.get("confidence") or 0),
            float(self.candidate.get("raw_alpha_score") or 0),
        )


class EngineOperatorProposalService:
    """Project institutional-engine decisions into shadow-only operator artifacts.

    This service never generates alpha, writes legacy ``trade_signals``, or submits
    orders. Acknowledgment is evidence that an operator reviewed a proposal; it is
    deliberately not paper execution authority.
    """

    def __init__(self, *, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.processed_books = 0
        self.evaluated_candidates = 0
        self.proposals_created = 0
        self.proposals_suppressed = 0
        self.digest_count = 0
        self.last_book_id: str | None = None
        self.last_proposal_id: str | None = None
        self.last_run_at_ms: int | None = None
        self.last_error: str | None = None
        self.last_blocker_counts: dict[str, int] = {}
        self._near_misses: dict[str, tuple[int, ProposalEvaluation]] = {}
        self._last_digest_bucket: int | None = None

    async def process_candidate_book(self, candidate_book_id: str | None) -> dict[str, Any]:
        now = _now_ms()
        self.last_run_at_ms = now
        if not self.settings.engine_operator_proposals_enabled:
            return {"enabled": False, "created": 0, "evaluated": 0}
        await self.repository.expire_engine_operator_proposals(now_ms=now)
        book = await self.repository.latest_candidate_book_snapshot()
        if not book or (candidate_book_id and str(book.get("candidate_book_id")) != str(candidate_book_id)):
            self.last_error = "candidate_book_unavailable"
            return {"enabled": True, "created": 0, "evaluated": 0, "error": self.last_error}
        self.last_book_id = str(book.get("candidate_book_id") or candidate_book_id or "") or None

        evaluations: list[ProposalEvaluation] = []
        blocker_counts: dict[str, int] = {}
        for candidate_id in list(book.get("candidate_ids") or []):
            evaluation = await self._evaluate_candidate(str(candidate_id), now_ms=now)
            if evaluation is None:
                continue
            evaluations.append(evaluation)
            for blocker in [*evaluation.hard_blockers, *evaluation.soft_blockers]:
                blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
            if self._digest_eligible(evaluation):
                self._near_misses[evaluation.candidate_id] = (now, evaluation)

        created: list[dict[str, Any]] = []
        eligible = sorted((item for item in evaluations if item.eligible), key=lambda item: item.rank, reverse=True)
        recent = await self.repository.list_engine_operator_proposals(
            since_ms=now - 24 * 60 * 60 * 1000,
            limit=max(100, self.settings.engine_operator_max_proposals_per_day * 5),
        )
        daily_remaining = max(0, self.settings.engine_operator_max_proposals_per_day - len(recent))
        loop_remaining = min(max(0, self.settings.engine_operator_max_proposals_per_loop), daily_remaining)
        cooldown_ms = max(1, self.settings.engine_operator_symbol_cooldown_minutes) * 60_000
        recent_assets = {
            str(item.get("asset") or "").upper()
            for item in recent
            if int(item.get("created_at_ms") or 0) >= now - cooldown_ms
        }

        for evaluation in eligible:
            if len(created) >= loop_remaining:
                self.proposals_suppressed += 1
                continue
            asset = str(evaluation.candidate.get("asset") or "").upper()
            if asset in recent_assets:
                self.proposals_suppressed += 1
                blocker_counts["symbol_cooldown"] = blocker_counts.get("symbol_cooldown", 0) + 1
                continue
            existing = await self.repository.get_engine_operator_proposal_by_candidate(evaluation.candidate_id)
            if existing is not None:
                continue
            proposal = self._build_proposal(evaluation, now_ms=now)
            proposal_id = await self.repository.upsert_engine_operator_proposal(proposal)
            if not proposal_id:
                continue
            proposal["proposal_id"] = proposal_id
            created.append(proposal)
            recent_assets.add(asset)
            self.proposals_created += 1
            self.last_proposal_id = proposal_id
            await self._enqueue_proposal_notification(proposal)

        await self._maybe_enqueue_shadow_digest(now_ms=now)
        self.processed_books += 1
        self.evaluated_candidates += len(evaluations)
        self.last_blocker_counts = blocker_counts
        self.last_error = None
        return {
            "enabled": True,
            "candidate_book_id": self.last_book_id,
            "evaluated": len(evaluations),
            "eligible": len(eligible),
            "created": len(created),
            "proposal_ids": [str(item["proposal_id"]) for item in created],
            "blockers": blocker_counts,
            "daily_remaining": max(0, daily_remaining - len(created)),
        }

    async def acknowledge(self, proposal_id: str, *, actor: str) -> dict[str, Any] | None:
        now = _now_ms()
        await self.repository.expire_engine_operator_proposals(now_ms=now)
        return await self.repository.update_engine_operator_proposal_status(
            proposal_id,
            status="acknowledged",
            actor=actor,
            now_ms=now,
        )

    async def reject(self, proposal_id: str, *, actor: str, reason: str = "") -> dict[str, Any] | None:
        now = _now_ms()
        await self.repository.expire_engine_operator_proposals(now_ms=now)
        return await self.repository.update_engine_operator_proposal_status(
            proposal_id,
            status="rejected",
            actor=actor,
            reason=reason,
            now_ms=now,
        )

    async def expire(self, proposal_id: str, *, actor: str) -> dict[str, Any] | None:
        return await self.repository.update_engine_operator_proposal_status(
            proposal_id,
            status="expired",
            actor=actor,
            now_ms=_now_ms(),
        )

    async def _evaluate_candidate(self, candidate_id: str, *, now_ms: int) -> ProposalEvaluation | None:
        candidate_record = await self.repository.get_alpha_candidate(candidate_id)
        packets = await self.repository.list_candidate_trade_packets(candidate_id=candidate_id, limit=1)
        packet_row = packets[0] if packets else {}
        packet = dict(packet_row.get("packet") or {})
        candidate = dict(packet.get("candidate") or candidate_record or {})
        if not candidate:
            return None
        estimates = await self.repository.list_ev_estimates(candidate_id=candidate_id, limit=1)
        allocations = await self.repository.list_allocation_decisions(candidate_id=candidate_id, limit=1)
        councils = await self.repository.list_council_reviews(candidate_id=candidate_id, limit=1)
        debates = await self.repository.list_debate_decisions(candidate_id=candidate_id, limit=1)
        ev = dict((estimates[0] if estimates else None) or packet.get("ev_estimate") or {})
        allocation = dict((allocations[0] if allocations else None) or packet.get("allocation") or {})
        council = dict(councils[0] if councils else {})
        debate = dict(debates[0] if debates else {})
        evaluation = ProposalEvaluation(
            candidate_id=candidate_id,
            candidate=candidate,
            ev=ev,
            allocation=allocation,
            packet=packet,
            council=council,
            debate=debate,
        )
        self._apply_eligibility(evaluation, now_ms=now_ms)
        return evaluation

    def _apply_eligibility(self, evaluation: ProposalEvaluation, *, now_ms: int) -> None:
        candidate = evaluation.candidate
        ev = evaluation.ev
        allocation = evaluation.allocation
        packet = evaluation.packet
        council = evaluation.council
        debate = evaluation.debate
        strategy_id = str(candidate.get("strategy_id") or "")
        side = str(candidate.get("side") or "flat")
        source_value = candidate.get("source_integrity")
        source: dict[str, Any] = dict(source_value) if isinstance(source_value, dict) else {}

        if side not in {"long", "short"}:
            evaluation.hard_blockers.append("not_directional")
        if strategy_id == "legacy_signal_adapter_v1":
            evaluation.hard_blockers.append("legacy_signal_adapter")
        activation_scope = str(source.get("activation_scope") or "paper_shadow")
        if activation_scope == "shadow_only":
            evaluation.soft_blockers.append("shadow_only_strategy")
        if not bool(source.get("paper_eligible", False)):
            evaluation.soft_blockers.append("not_paper_eligible")
        if not bool(candidate.get("counts_for_breadth", False)):
            evaluation.soft_blockers.append("not_alpha_breadth")

        min_ev = max(12.0, self.settings.engine_min_net_ev_bps, self.settings.engine_operator_min_net_ev_bps)
        min_utility = max(
            0.35,
            self.settings.engine_min_risk_adjusted_utility,
            self.settings.engine_operator_min_risk_adjusted_utility,
        )
        if float(ev.get("net_ev_bps") or 0) < min_ev:
            evaluation.hard_blockers.append("net_ev_below_operator_minimum")
        if float(ev.get("risk_adjusted_utility") or 0) < min_utility:
            evaluation.hard_blockers.append("utility_below_operator_minimum")
        if float(candidate.get("confidence") or 0) < max(0.55, self.settings.engine_operator_min_confidence):
            evaluation.hard_blockers.append("confidence_below_operator_minimum")
        if float(candidate.get("feature_coverage_pct") or 0) < max(
            80.0, self.settings.engine_operator_min_feature_coverage_pct
        ):
            evaluation.hard_blockers.append("feature_coverage_below_operator_minimum")
        if int(candidate.get("expires_at_ms") or 0) <= now_ms:
            evaluation.hard_blockers.append("candidate_expired")

        allocation_status = str(allocation.get("status") or "missing")
        allocation_positive = float(allocation.get("allocated_size") or 0) > 0 and float(
            allocation.get("allocated_notional_usd") or 0
        ) > 0
        if allocation_status not in {"allocate", "reduce"} or not allocation_positive:
            reasons = [str(reason) for reason in allocation.get("reason_codes") or []]
            if any("risk" in reason for reason in reasons) or allocation_status == "risk_rejected":
                evaluation.hard_blockers.append("allocation_risk_rejected")
            else:
                evaluation.soft_blockers.append("allocation_not_approved")
            if any("throttle" in reason for reason in reasons):
                evaluation.soft_blockers.append("strategy_throttle")
            if any("concentration" in reason or "share" in reason for reason in reasons):
                evaluation.soft_blockers.append("concentration_limit")

        risk_value = packet.get("risk_decision")
        risk: dict[str, Any] = dict(risk_value) if isinstance(risk_value, dict) else {}
        risk_allowed = risk.get("allowed")
        if risk_allowed is None:
            risk_allowed = str(risk.get("decision") or "reject") in {"allow", "allowed", "not_applicable"}
        if not bool(risk_allowed):
            evaluation.hard_blockers.append("risk_gateway_rejected")

        council_decision = str(council.get("decision") or "missing")
        vetoes = [str(veto) for veto in council.get("vetoes") or []]
        soft_council_vetoes = {"allocation_not_approved", "concentration_cap_breach"}
        hard_vetoes = [veto for veto in vetoes if veto not in soft_council_vetoes]
        if hard_vetoes or council_decision in {"missing", "needs_more_evidence"}:
            evaluation.hard_blockers.append("council_not_allowed")
        elif council_decision == "reject" and not vetoes:
            evaluation.hard_blockers.append("council_not_allowed")
        elif council_decision == "reject":
            evaluation.soft_blockers.append("council_allocation_block")

        if str(debate.get("decision") or "") in {"block", "require_more_data"}:
            evaluation.hard_blockers.append("debate_not_allowed")

        evaluation.hard_blockers = sorted(set(evaluation.hard_blockers))
        evaluation.soft_blockers = sorted(set(evaluation.soft_blockers))

    def _build_proposal(self, evaluation: ProposalEvaluation, *, now_ms: int) -> dict[str, Any]:
        candidate = evaluation.candidate
        ev = evaluation.ev
        allocation = evaluation.allocation
        council = evaluation.council
        proposal_id = "sig_eng_" + hashlib.sha1(evaluation.candidate_id.encode()).hexdigest()[:24]
        candidate_expiry = int(candidate.get("expires_at_ms") or now_ms)
        expires_at_ms = min(
            candidate_expiry,
            now_ms + max(1, self.settings.engine_operator_proposal_ttl_minutes) * 60_000,
        )
        targets = list(candidate.get("targets") or [])
        payload = {
            "signal": {
                "id": proposal_id,
                "symbol": str(candidate.get("asset") or "").upper(),
                "side": str(candidate.get("side") or "flat"),
                "signal_type": f"engine:{candidate.get('strategy_id') or 'unknown'}",
                "status": "posted",
                "score": float(candidate.get("raw_alpha_score") or 0),
                "confidence": float(candidate.get("confidence") or 0),
                "created_at_ms": now_ms,
                "expires_at_ms": expires_at_ms,
                "entry": float(candidate.get("proposed_entry") or 0),
                "stop": float(candidate.get("stop") or 0),
                "take_profit": float(targets[0]) if targets else None,
                "invalidation": "; ".join(str(item) for item in candidate.get("invalidation_conditions") or []),
                "thesis": str(candidate.get("thesis") or ""),
                "evidence": [
                    {
                        "category": "expected_value",
                        "label": "Net EV",
                        "value": float(ev.get("net_ev_bps") or 0),
                        "source": "model",
                        "kind": "bps",
                    },
                    {
                        "category": "risk",
                        "label": "Risk-adjusted utility",
                        "value": float(ev.get("risk_adjusted_utility") or 0),
                        "source": "risk",
                        "kind": "ratio",
                    },
                    {
                        "category": "data_quality",
                        "label": "Feature coverage",
                        "value": float(candidate.get("feature_coverage_pct") or 0),
                        "source": "market_structure",
                        "kind": "pct",
                    },
                ],
                "feature_snapshot": {
                    "feature_snapshot_id": candidate.get("feature_snapshot_id"),
                    "regime_snapshot_id": candidate.get("regime_snapshot_id"),
                    "feature_coverage_pct": candidate.get("feature_coverage_pct"),
                },
                "risk_plan": {
                    "allocated_notional_usd": allocation.get("allocated_notional_usd"),
                    "allocated_size": allocation.get("allocated_size"),
                    "risk_usd": allocation.get("risk_usd"),
                    "net_ev_bps": ev.get("net_ev_bps"),
                    "risk_adjusted_utility": ev.get("risk_adjusted_utility"),
                },
                "model_insight": None,
                "discord_channel_id": self.settings.autonomy_alert_channel_id or None,
                "discord_message_id": None,
                "metadata": {
                    "source": "institutional_engine",
                    "execution_authority": "none",
                    "acknowledgment_only": True,
                    "candidate_id": evaluation.candidate_id,
                    "packet_id": evaluation.packet.get("packet_id"),
                    "council_review_id": council.get("review_id"),
                    "council_decision": council.get("decision"),
                    "strategy_id": candidate.get("strategy_id"),
                    "strategy_version": candidate.get("strategy_version"),
                    "asset_class": candidate.get("asset_class"),
                },
            },
            "candidate": candidate,
            "ev_estimate": ev,
            "allocation": allocation,
            "risk_decision": evaluation.packet.get("risk_decision") or {},
            "council": council,
            "debate": evaluation.debate,
        }
        return {
            "proposal_id": proposal_id,
            "candidate_id": evaluation.candidate_id,
            "packet_id": evaluation.packet.get("packet_id"),
            "council_review_id": council.get("review_id"),
            "strategy_id": str(candidate.get("strategy_id") or "unknown"),
            "asset": str(candidate.get("asset") or "").upper(),
            "side": str(candidate.get("side") or "flat"),
            "score": float(candidate.get("raw_alpha_score") or 0),
            "confidence": float(candidate.get("confidence") or 0),
            "net_ev_bps": float(ev.get("net_ev_bps") or 0),
            "risk_adjusted_utility": float(ev.get("risk_adjusted_utility") or 0),
            "feature_coverage_pct": float(candidate.get("feature_coverage_pct") or 0),
            "allocated_notional_usd": float(allocation.get("allocated_notional_usd") or 0),
            "created_at_ms": now_ms,
            "expires_at_ms": expires_at_ms,
            "payload": payload,
            "metadata": {
                "candidate_book_id": self.last_book_id,
                "acknowledgment_only": True,
                "paper_execution_allowed": False,
                "live_execution_allowed": False,
            },
            "updated_at_ms": now_ms,
        }

    async def _enqueue_proposal_notification(self, proposal: dict[str, Any]) -> None:
        channel_id = str(self.settings.autonomy_alert_channel_id or "").strip()
        if not channel_id:
            return
        signal = dict((proposal.get("payload") or {}).get("signal") or {})
        take_profit = signal.get("take_profit")
        target_text = f"{float(take_profit):.8g}" if isinstance(take_profit, (int, float, str)) else "n/a"
        content = (
            f"📐 **Institutional engine proposal — {proposal['asset']} {str(proposal['side']).upper()}**\n"
            f"ID `{proposal['proposal_id']}` | strategy `{proposal['strategy_id']}`\n"
            f"Score **{proposal['score']:.1f}** | confidence **{proposal['confidence']:.2f}** | "
            f"net EV **{proposal['net_ev_bps']:.2f} bps** | utility **{proposal['risk_adjusted_utility']:.3f}**\n"
            f"Feature coverage **{proposal['feature_coverage_pct']:.1f}%** | shadow allocation "
            f"**${proposal['allocated_notional_usd']:,.2f}**\n"
            f"Entry `{signal.get('entry')}` | stop `{signal.get('stop')}` | target `{target_text}`\n\n"
            f"{signal.get('thesis') or 'No thesis recorded.'}\n\n"
            "**Shadow safety:** acknowledgment records operator review only. It does not create a paper or live order."
        )
        await self.repository.enqueue_operational_notification(
            dedupe_key=f"engine-proposal:{proposal['proposal_id']}",
            category="engine_operator_proposal",
            severity="info",
            source_type="engine_candidate",
            source_id=str(proposal["candidate_id"]),
            channel_id=channel_id,
            payload={
                "content": content,
                "components": [
                    {
                        "custom_id": f"engine_proposal_ack:{proposal['proposal_id']}",
                        "label": "Acknowledge",
                        "style": "primary",
                    },
                    {
                        "custom_id": f"engine_proposal_reject:{proposal['proposal_id']}",
                        "label": "Reject",
                        "style": "danger",
                    },
                ],
            },
        )

    def _digest_eligible(self, evaluation: ProposalEvaluation) -> bool:
        if evaluation.eligible or evaluation.hard_blockers:
            return False
        allowed = {
            "shadow_only_strategy",
            "not_paper_eligible",
            "not_alpha_breadth",
            "allocation_not_approved",
            "strategy_throttle",
            "concentration_limit",
            "council_allocation_block",
        }
        required = [str(item) for item in evaluation.council.get("required_evidence") or []]
        replay_blocked = any("replay" in item for item in required)
        return not replay_blocked and set(evaluation.soft_blockers) <= allowed

    @staticmethod
    def _display_soft_blockers(blockers: list[str]) -> list[str]:
        """Collapse research-governance details for the operator-facing digest.

        The raw blocker codes remain on ``ProposalEvaluation`` for diagnostics and
        auditability. To an operator, however, ``shadow_only_strategy`` and
        ``not_paper_eligible`` describe the same actionable state: research only.
        """

        research_codes = {"shadow_only_strategy", "not_paper_eligible"}
        display = [item for item in blockers if item not in research_codes]
        if research_codes.intersection(blockers):
            display.insert(0, "research_only")
        return display

    async def _maybe_enqueue_shadow_digest(self, *, now_ms: int) -> None:
        if not self.settings.engine_operator_shadow_digest_enabled:
            return
        channel_id = str(self.settings.autonomy_alert_channel_id or "").strip()
        if not channel_id:
            return
        interval_ms = max(60, self.settings.engine_operator_shadow_digest_interval_seconds) * 1000
        bucket = now_ms // interval_ms
        if self._last_digest_bucket == bucket:
            return
        cutoff = now_ms - interval_ms
        self._near_misses = {
            candidate_id: item
            for candidate_id, item in self._near_misses.items()
            if item[0] >= cutoff
        }
        ranked = sorted((item[1] for item in self._near_misses.values()), key=lambda item: item.rank, reverse=True)[:5]
        self._last_digest_bucket = bucket
        if not ranked:
            return
        lines = ["🔬 **Institutional engine shadow digest — research candidates**"]
        for item in ranked:
            display_blockers = self._display_soft_blockers(item.soft_blockers)
            lines.append(
                f"- `{item.candidate.get('asset')}` {str(item.candidate.get('side')).upper()} / "
                f"`{item.candidate.get('strategy_id')}` — EV `{float(item.ev.get('net_ev_bps') or 0):.2f}` bps, "
                f"utility `{float(item.ev.get('risk_adjusted_utility') or 0):.3f}`, blocked by "
                f"`{','.join(display_blockers)}`"
            )
        lines.extend(
            [
                "",
                "Research-only digest. Risk, council, debate, and replay-blocked candidates are excluded.",
            ]
        )
        await self.repository.enqueue_operational_notification(
            dedupe_key=f"engine-shadow-digest:{bucket}",
            category="engine_shadow_digest",
            severity="info",
            source_type="engine_candidate_book",
            source_id=self.last_book_id,
            channel_id=channel_id,
            payload={"content": "\n".join(lines)},
        )
        self.digest_count += 1

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_operator_proposals_enabled,
            "execution_authority": "none",
            "acknowledgment_only": True,
            "processed_books": self.processed_books,
            "evaluated_candidates": self.evaluated_candidates,
            "proposals_created": self.proposals_created,
            "proposals_suppressed": self.proposals_suppressed,
            "digest_count": self.digest_count,
            "last_book_id": self.last_book_id,
            "last_proposal_id": self.last_proposal_id,
            "last_run_at_ms": self.last_run_at_ms,
            "last_blocker_counts": self.last_blocker_counts,
            "last_error": self.last_error,
        }


def project_operator_proposal_to_trade_signal(proposal: dict[str, Any]) -> dict[str, Any]:
    signal = dict((proposal.get("payload") or {}).get("signal") or {})
    status_map = {
        "proposed": "posted",
        "acknowledged": "approved",
        "rejected": "rejected",
        "expired": "expired",
    }
    signal["status"] = status_map.get(str(proposal.get("status") or "proposed"), "posted")
    metadata = dict(signal.get("metadata") or {})
    metadata.update(
        {
            "operator_proposal_status": proposal.get("status"),
            "acknowledged_by": proposal.get("acknowledged_by"),
            "acknowledged_at_ms": proposal.get("acknowledged_at_ms"),
            "rejected_by": proposal.get("rejected_by"),
            "rejected_at_ms": proposal.get("rejected_at_ms"),
            "rejection_reason": proposal.get("rejection_reason"),
            "acknowledgment_only": True,
            "paper_execution_created": False,
        }
    )
    signal["metadata"] = metadata
    return signal
