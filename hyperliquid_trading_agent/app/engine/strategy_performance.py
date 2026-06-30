from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms

ALLOCATING_STATUSES = {"allocate", "reduce", "require_debate"}


async def refresh_strategy_regime_performance(
    repository: Any,
    *,
    window_start_ms: int,
    window_end_ms: int,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    candidates = [item for item in await repository.list_alpha_candidates(limit=limit) if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
    allocations = [item for item in await repository.list_allocation_decisions(limit=limit) if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
    estimates = await repository.list_ev_estimates(limit=limit)
    pnl_records = await _list_pnl(repository, limit=limit)

    ev_by_candidate: dict[str, list[float]] = defaultdict(list)
    for estimate in estimates:
        cid = str(estimate.get("candidate_id") or "")
        if cid:
            ev_by_candidate[cid].append(float(estimate.get("net_ev_bps") or 0.0))

    allocation_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for allocation in allocations:
        allocation_by_candidate[str(allocation.get("candidate_id") or "")].append(allocation)

    groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        strategy_id = str(candidate.get("strategy_id") or "unknown")
        strategy_version = str(metadata.get("strategy_version") or candidate.get("strategy_version") or "unknown")
        strategy_family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        regime_label = str(metadata.get("regime_label") or candidate.get("regime_label") or candidate.get("regime_snapshot_id") or "unknown")
        asset = str(candidate.get("asset") or "GLOBAL").upper()
        key = (strategy_id, strategy_version, strategy_family, regime_label, asset)
        group = groups.setdefault(
            key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "strategy_family": strategy_family,
                "regime_label": regime_label,
                "asset": asset,
                "candidate_ids": [],
                "allocation_ids": [],
                "ev_values": [],
                "pnl_values": [],
            },
        )
        cid = str(candidate.get("candidate_id") or "")
        group["candidate_ids"].append(cid)
        group["ev_values"].extend(ev_by_candidate.get(cid, []))
        for allocation in allocation_by_candidate.get(cid, []):
            if str(allocation.get("status") or "") in ALLOCATING_STATUSES:
                group["allocation_ids"].append(str(allocation.get("allocation_id") or ""))

    pnl_by_strategy_asset = _pnl_by_strategy_asset(pnl_records)
    rows: list[dict[str, Any]] = []
    ts = now_ms()
    for key, group in groups.items():
        strategy_id, strategy_version, strategy_family, regime_label, asset = key
        pnl_values = pnl_by_strategy_asset.get((strategy_id, asset), [])
        group["pnl_values"].extend(pnl_values)
        candidate_count = len(set(group["candidate_ids"]))
        allocation_count = len(set(group["allocation_ids"]))
        ev_values = group["ev_values"]
        avg_ev = sum(ev_values) / len(ev_values) if ev_values else 0.0
        pnl_total = sum(group["pnl_values"])
        win_rate = sum(1 for value in group["pnl_values"] if value > 0) / len(group["pnl_values"]) * 100.0 if group["pnl_values"] else 0.0
        score = _score(candidate_count=candidate_count, allocation_count=allocation_count, avg_ev=avg_ev, win_rate=win_rate, pnl_total=pnl_total)
        performance_id = "srp_" + hashlib.sha1(f"{strategy_id}:{strategy_version}:{regime_label}:{asset}:{window_start_ms}:{window_end_ms}".encode()).hexdigest()[:24]
        row = {
            "performance_id": performance_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "strategy_family": strategy_family,
            "regime_label": regime_label,
            "asset": asset,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "candidate_count": candidate_count,
            "allocation_count": allocation_count,
            "win_rate_pct": round(win_rate, 2),
            "avg_net_ev_bps": round(avg_ev, 4),
            "realized_pnl_usd": round(pnl_total, 4),
            "score": round(score, 2),
            "created_at_ms": ts,
            "metadata": {
                "candidate_ids": sorted(set(group["candidate_ids"])),
                "allocation_ids": sorted(set(group["allocation_ids"])),
                "sample_quality": "observed" if allocation_count else "candidate_only",
            },
        }
        await repository.upsert_strategy_regime_performance(row)
        rows.append(row)
    return sorted(rows, key=lambda item: (item["score"], item["candidate_count"]), reverse=True)


async def _list_pnl(repository: Any, *, limit: int) -> list[dict[str, Any]]:
    list_pnl = getattr(repository, "list_pnl_attribution", None)
    if not callable(list_pnl):
        return []
    try:
        return await list_pnl(limit=limit)
    except TypeError:
        return await list_pnl()


def _pnl_by_strategy_asset(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    for record in records:
        strategy_id = str(record.get("strategy_id") or "unknown")
        asset = str(record.get("asset") or record.get("symbol") or "GLOBAL").upper()
        value = float(record.get("realized_pnl_usd") or record.get("total_pnl_usd") or record.get("pnl_usd") or 0.0)
        out[(strategy_id, asset)].append(value)
    return out


def _score(*, candidate_count: int, allocation_count: int, avg_ev: float, win_rate: float, pnl_total: float) -> float:
    sample_score = min(30.0, candidate_count * 3.0 + allocation_count * 4.0)
    ev_score = max(0.0, min(30.0, avg_ev))
    win_score = max(0.0, min(25.0, win_rate / 4.0))
    pnl_score = max(-10.0, min(15.0, pnl_total / 10.0))
    return max(0.0, min(100.0, 20.0 + sample_score + ev_score + win_score + pnl_score))
