from __future__ import annotations

import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import ReconciliationRun


class ReconciliationService:
    def __init__(self, repository: Any | None = None):
        self.repository = repository

    async def reconcile(self, *, execution_mode: str, expected_positions: list[dict[str, Any]], observed_positions: list[dict[str, Any]] | None = None) -> ReconciliationRun:
        observed_positions = observed_positions or []
        mismatches = [] if execution_mode == "paper" and not observed_positions else _diff_positions(expected_positions, observed_positions)
        ts = now_ms()
        digest = hashlib.sha1(f"{execution_mode}:{ts}:{len(expected_positions)}:{len(mismatches)}".encode()).hexdigest()[:24]
        run = ReconciliationRun(
            reconciliation_id="recon_" + digest,
            execution_mode=execution_mode,  # type: ignore[arg-type]
            status="mismatch" if mismatches else "ok",
            expected_positions=expected_positions,
            observed_positions=observed_positions,
            mismatches=mismatches,
            started_at_ms=ts,
            completed_at_ms=now_ms(),
            metadata={"live_exchange_actions": False},
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_reconciliation_run", None)
            if callable(record):
                await record(run.model_dump(mode="json"))
        return run


def _diff_positions(expected: list[dict[str, Any]], observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(item.get("asset"), item.get("side")): item for item in observed}
    out = []
    for item in expected:
        key = (item.get("asset"), item.get("side"))
        other = by_key.get(key)
        if other is None:
            out.append({"type": "missing_observed_position", "expected": item})
        elif float(other.get("quantity") or 0) != float(item.get("quantity") or 0):
            out.append({"type": "quantity_mismatch", "expected": item, "observed": other})
    return out
