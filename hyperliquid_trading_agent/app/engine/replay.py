from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import ReplayResult


class ReplayLab:
    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def audit_only(self, *, mode: str, candidate_id: str | None = None, decision_id: str | None = None, caveats: list[str] | None = None) -> ReplayResult:
        ts = now_ms()
        digest = hashlib.sha1(f"{mode}:{candidate_id}:{decision_id}:{ts}".encode()).hexdigest()[:24]
        result = ReplayResult(
            replay_id="replay_" + digest,
            mode=mode,  # type: ignore[arg-type]
            candidate_id=candidate_id,
            decision_id=decision_id,
            status="audit_only",
            caveats=caveats or ["point_in_time_replay_engine_not_yet_connected"],
            created_at_ms=ts,
        )
        # Existing repository replay_results table is proposal/diff oriented; engine replay
        # persistence lands with the full replay migration extension.
        return result
