from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import RetentionRun


class RetentionService:
    """Audit-only retention scaffold.

    Actual delete/rollup jobs are intentionally explicit future work; this records
    retention intent/results without silently deleting research-critical history.
    """

    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def record_run(self, *, status: str = "completed", deleted_counts: dict[str, int] | None = None, rollup_counts: dict[str, int] | None = None, caveats: list[str] | None = None) -> RetentionRun:
        ts = now_ms()
        digest = hashlib.sha1(f"{ts}:{deleted_counts}:{rollup_counts}".encode()).hexdigest()[:24]
        run = RetentionRun(
            retention_run_id="ret_" + digest,
            status=status,  # type: ignore[arg-type]
            started_at_ms=ts,
            completed_at_ms=now_ms(),
            deleted_counts=deleted_counts or {},
            rollup_counts=rollup_counts or {},
            caveats=caveats or ["audit_only_no_deletes_performed"],
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_retention_run", None)
            if callable(record):
                await record(run.model_dump(mode="json"))
        return run
