from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, CandidateBookSnapshot, EVEstimate


class CandidateBook:
    def __init__(self, repository: Any | None = None):
        self.repository = repository
        self.candidates: dict[str, AlphaCandidate] = {}
        self.latest_snapshot: CandidateBookSnapshot | None = None

    async def add_many(self, candidates: list[AlphaCandidate]) -> list[AlphaCandidate]:
        for candidate in candidates:
            self.candidates[candidate.candidate_id] = candidate
            if self.repository is not None and getattr(self.repository, "enabled", False):
                record = getattr(self.repository, "record_alpha_candidate", None)
                if callable(record):
                    await record(candidate.model_dump(mode="json"))
        return candidates

    async def snapshot(self, estimates: dict[str, EVEstimate] | None = None, *, as_of_ms: int | None = None) -> CandidateBookSnapshot:
        ts = as_of_ms or now_ms()
        active = [item for item in self.candidates.values() if item.expires_at_ms > ts and item.status not in {"expired", "cancelled"}]
        estimates = estimates or {}
        ranked = sorted(
            active,
            key=lambda item: (estimates.get(item.candidate_id).net_ev_bps if estimates.get(item.candidate_id) else item.raw_alpha_score),
            reverse=True,
        )
        rejected = [item.candidate_id for item in self.candidates.values() if item.status in {"risk_rejected", "debate_blocked", "expired", "cancelled"}]
        digest = hashlib.sha1(f"{ts}:{[item.candidate_id for item in ranked]}".encode()).hexdigest()[:24]
        snapshot = CandidateBookSnapshot(
            candidate_book_id="book_" + digest,
            created_at_ms=now_ms(),
            as_of_ms=ts,
            candidate_ids=[item.candidate_id for item in active],
            ranked_candidate_ids=[item.candidate_id for item in ranked],
            rejected_candidate_ids=rejected,
        )
        self.latest_snapshot = snapshot
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_candidate_book_snapshot", None)
            if callable(record):
                await record(snapshot.model_dump(mode="json"))
        return snapshot
