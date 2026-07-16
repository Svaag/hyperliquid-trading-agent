from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, ExecutionReport, PositionThesis


class PositionManager:
    def __init__(self, repository: Any | None = None):
        self.repository = repository
        self.positions: dict[str, PositionThesis] = {}

    async def open_from_execution(self, candidate: AlphaCandidate, report: ExecutionReport) -> PositionThesis | None:
        # Shadow reports contain hypothetical depth-walk fills for measurement;
        # they must never create portfolio state.
        if report.execution_mode == "shadow":
            return None
        if report.status not in {"filled", "partial", "accepted"}:
            return None
        side = "long" if candidate.side == "long" else "short" if candidate.side == "short" else None
        if side is None:
            return None
        digest = hashlib.sha1(f"{candidate.candidate_id}:{report.report_id}".encode()).hexdigest()[:24]
        ts = now_ms()
        thesis = PositionThesis(
            position_id="pos_" + digest,
            entry_candidate_id=candidate.candidate_id,
            strategy_id=candidate.strategy_id,
            asset=candidate.asset,
            asset_class=candidate.asset_class,
            venue=candidate.venue,
            side=side,  # type: ignore[arg-type]
            entry_reason=candidate.thesis,
            expected_horizon=candidate.horizon,
            stop=candidate.stop,
            targets=candidate.targets,
            invalidation_rules=candidate.invalidation_conditions,
            thesis_features_at_entry=candidate.metadata,
            current_thesis_score=candidate.confidence,
            position_state="open" if report.status in {"filled", "partial"} else "approved",
            execution_report_ids=[report.report_id],
            opened_at_ms=ts if report.status in {"filled", "partial"} else None,
            updated_at_ms=ts,
        )
        self.positions[thesis.position_id] = thesis
        await self._persist(thesis)
        return thesis

    async def mark_degraded(
        self, position_id: str, reason: str, *, score_delta: float = -0.15
    ) -> PositionThesis | None:
        thesis = self.positions.get(position_id)
        if thesis is None:
            return None
        updated = thesis.model_copy(
            update={
                "current_thesis_score": max(0.0, thesis.current_thesis_score + score_delta),
                "degradation_reasons": [*thesis.degradation_reasons, reason],
                "position_state": "de_risking"
                if thesis.current_thesis_score + score_delta < 0.35
                else thesis.position_state,
                "updated_at_ms": now_ms(),
            }
        )
        self.positions[position_id] = updated
        await self._persist(updated)
        return updated

    async def close(self, position_id: str, reason: str = "closed") -> PositionThesis | None:
        thesis = self.positions.get(position_id)
        if thesis is None:
            return None
        ts = now_ms()
        updated = thesis.model_copy(
            update={
                "position_state": "closed",
                "closed_at_ms": ts,
                "updated_at_ms": ts,
                "degradation_reasons": [*thesis.degradation_reasons, reason],
            }
        )
        self.positions[position_id] = updated
        await self._persist(updated)
        return updated

    async def _persist(self, thesis: PositionThesis) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_position_thesis", None)
            if callable(record):
                await record(thesis.model_dump(mode="json"))
