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
    outcome_rows = await _list_outcomes(repository, limit=limit)
    outcome_rows = [
        item
        for item in outcome_rows
        if str(item.get("terminal_state") or "") == "matured"
        and window_start_ms
        <= int(item.get("window_end_ms") or item.get("created_at_ms") or 0)
        <= window_end_ms
    ]
    if outcome_rows:
        return await _refresh_from_candidate_outcomes(repository, outcome_rows, window_start_ms=window_start_ms, window_end_ms=window_end_ms, limit=limit)
    return await _refresh_from_legacy_ledgers(repository, window_start_ms=window_start_ms, window_end_ms=window_end_ms, limit=limit)


async def _refresh_from_candidate_outcomes(
    repository: Any,
    outcomes: list[dict[str, Any]],
    *,
    window_start_ms: int,
    window_end_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    regime_ids = sorted({str(item.get("regime_snapshot_id") or "") for item in outcomes if item.get("regime_snapshot_id")})
    list_regimes = getattr(repository, "list_regime_snapshots_by_ids", None)
    regime_rows = await list_regimes(regime_ids) if callable(list_regimes) else []
    regimes = {str(item.get("regime_snapshot_id") or ""): item for item in regime_rows}
    concentration_events = await _list_concentration_events(repository, limit=limit)
    concentration_events = [item for item in concentration_events if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
    concentration_by_group: dict[tuple[str, str, str], int] = defaultdict(int)
    for event in concentration_events:
        concentration_by_group[(str(event.get("strategy_id") or "unknown"), str(event.get("asset") or "GLOBAL").upper(), str(event.get("venue") or "unknown"))] += 1

    groups: dict[tuple[str, str, str, str, str, str, str], dict[str, Any]] = {}
    for outcome in outcomes:
        metadata = outcome.get("metadata") if isinstance(outcome.get("metadata"), dict) else {}
        strategy_id = str(outcome.get("strategy_id") or "unknown")
        strategy_version = str(outcome.get("strategy_version") or "unknown")
        strategy_family = str(outcome.get("strategy_family") or "unknown")
        regime_row = regimes.get(str(outcome.get("regime_snapshot_id") or "")) or {}
        regime_vector = regime_row.get("vector") if isinstance(regime_row.get("vector"), dict) else regime_row
        regime_label = str(
            regime_vector.get("regime_label")
            or metadata.get("regime_label")
            or outcome.get("regime_label")
            or outcome.get("regime_snapshot_id")
            or "unknown"
        )
        asset = str(outcome.get("asset") or "GLOBAL").upper()
        venue = str(outcome.get("venue") or "unknown")
        outcome_window = str(outcome.get("outcome_window") or "unknown")
        key = (strategy_id, strategy_version, strategy_family, regime_label, asset, venue, outcome_window)
        group = groups.setdefault(
            key,
            {
                "candidate_ids": set(),
                "allocation_ids": set(),
                "net_returns": [],
                "realized_rs": [],
                "drawdowns": [],
                "fees": [],
                "slippage": [],
                "pnl_values": [],
                "risk_reject_count": 0,
                "council_veto_count": 0,
            },
        )
        cid = str(outcome.get("candidate_id") or "")
        if cid:
            group["candidate_ids"].add(cid)
        allocation_id = str(outcome.get("allocation_id") or "")
        allocation_status = str(outcome.get("allocation_status") or "")
        if allocation_id and allocation_status in ALLOCATING_STATUSES:
            group["allocation_ids"].add(allocation_id)
        risk_decision = str(outcome.get("risk_decision") or "unknown")
        if risk_decision in {"reject", "halt", "tighten"} or allocation_status == "risk_rejected":
            group["risk_reject_count"] += 1
        council_decision = str(outcome.get("council_decision") or "unknown")
        if council_decision in {"reject", "needs_more_evidence"}:
            group["council_veto_count"] += 1
        net_return = _f(outcome.get("net_return_bps"))
        group["net_returns"].append(net_return)
        group["realized_rs"].append(_f(outcome.get("realized_r")))
        group["drawdowns"].append(abs(min(0.0, _f(outcome.get("mae_bps")))))
        group["fees"].append(_f(outcome.get("fees_bps")))
        group["slippage"].append(_f(outcome.get("slippage_bps")))
        allocated_notional = _f(metadata.get("allocated_notional_usd"))
        if allocated_notional > 0:
            group["pnl_values"].append(net_return * allocated_notional / 10_000.0)

    rows: list[dict[str, Any]] = []
    ts = now_ms()
    for key, group in groups.items():
        strategy_id, strategy_version, strategy_family, regime_label, asset, venue, outcome_window = key
        candidate_count = len(group["candidate_ids"])
        allocation_count = len(group["allocation_ids"])
        net_returns = group["net_returns"]
        avg_net_return = _avg(net_returns)
        avg_realized_r = _avg(group["realized_rs"])
        avg_drawdown = _avg(group["drawdowns"])
        avg_fees = _avg(group["fees"])
        avg_slippage = _avg(group["slippage"])
        win_rate = sum(1 for value in net_returns if value > 0) / len(net_returns) * 100.0 if net_returns else 0.0
        pnl_total = sum(group["pnl_values"])
        concentration_count = concentration_by_group.get((strategy_id, asset, venue), 0)
        risk_reject_count = int(group["risk_reject_count"])
        council_veto_count = int(group["council_veto_count"])
        score = _outcome_score(
            candidate_count=candidate_count,
            allocation_count=allocation_count,
            avg_net_return=avg_net_return,
            avg_realized_r=avg_realized_r,
            win_rate=win_rate,
            avg_drawdown=avg_drawdown,
            risk_reject_count=risk_reject_count,
            council_veto_count=council_veto_count,
            concentration_count=concentration_count,
        )
        performance_id = "srp_" + hashlib.sha1(f"{strategy_id}:{strategy_version}:{regime_label}:{asset}:{venue}:{outcome_window}:{window_start_ms}:{window_end_ms}".encode()).hexdigest()[:24]
        row = {
            "performance_id": performance_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "strategy_family": strategy_family,
            "regime_label": regime_label,
            "asset": asset,
            "venue": venue,
            "outcome_window": outcome_window,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "candidate_count": candidate_count,
            "allocation_count": allocation_count,
            "risk_reject_count": risk_reject_count,
            "council_veto_count": council_veto_count,
            "concentration_event_count": concentration_count,
            "win_rate_pct": round(win_rate, 2),
            "avg_net_ev_bps": 0.0,
            "avg_net_return_bps": round(avg_net_return, 4),
            "avg_realized_r": round(avg_realized_r, 4),
            "avg_drawdown_bps": round(avg_drawdown, 4),
            "avg_fees_bps": round(avg_fees, 4),
            "avg_slippage_bps": round(avg_slippage, 4),
            "realized_pnl_usd": round(pnl_total, 4),
            "score": round(score, 2),
            "created_at_ms": ts,
            "metadata": {
                "candidate_ids": sorted(group["candidate_ids"]),
                "allocation_ids": sorted(group["allocation_ids"]),
                "sample_quality": "outcome_attributed",
                "grouping": "strategy_regime_asset_venue_outcome_window",
            },
        }
        await repository.upsert_strategy_regime_performance(row)
        rows.append(row)
    return sorted(rows, key=lambda item: (item["score"], item["candidate_count"]), reverse=True)


async def _refresh_from_legacy_ledgers(
    repository: Any,
    *,
    window_start_ms: int,
    window_end_ms: int,
    limit: int,
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

    groups: dict[tuple[str, str, str, str, str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        strategy_id = str(candidate.get("strategy_id") or "unknown")
        strategy_version = str(metadata.get("strategy_version") or candidate.get("strategy_version") or "unknown")
        strategy_family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        regime_label = str(metadata.get("regime_label") or candidate.get("regime_label") or candidate.get("regime_snapshot_id") or "unknown")
        asset = str(candidate.get("asset") or "GLOBAL").upper()
        venue = str(candidate.get("venue") or metadata.get("venue") or "unknown")
        outcome_window = str(candidate.get("horizon") or metadata.get("horizon") or "unknown")
        key = (strategy_id, strategy_version, strategy_family, regime_label, asset, venue, outcome_window)
        group = groups.setdefault(
            key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "strategy_family": strategy_family,
                "regime_label": regime_label,
                "asset": asset,
                "venue": venue,
                "outcome_window": outcome_window,
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
        strategy_id, strategy_version, strategy_family, regime_label, asset, venue, outcome_window = key
        pnl_values = pnl_by_strategy_asset.get((strategy_id, asset), [])
        group["pnl_values"].extend(pnl_values)
        candidate_count = len(set(group["candidate_ids"]))
        allocation_count = len(set(group["allocation_ids"]))
        ev_values = group["ev_values"]
        avg_ev = sum(ev_values) / len(ev_values) if ev_values else 0.0
        pnl_total = sum(group["pnl_values"])
        win_rate = sum(1 for value in group["pnl_values"] if value > 0) / len(group["pnl_values"]) * 100.0 if group["pnl_values"] else 0.0
        score = _legacy_score(candidate_count=candidate_count, allocation_count=allocation_count, avg_ev=avg_ev, win_rate=win_rate, pnl_total=pnl_total)
        performance_id = "srp_" + hashlib.sha1(f"{strategy_id}:{strategy_version}:{regime_label}:{asset}:{venue}:{outcome_window}:{window_start_ms}:{window_end_ms}".encode()).hexdigest()[:24]
        row = {
            "performance_id": performance_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "strategy_family": strategy_family,
            "regime_label": regime_label,
            "asset": asset,
            "venue": venue,
            "outcome_window": outcome_window,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "candidate_count": candidate_count,
            "allocation_count": allocation_count,
            "risk_reject_count": 0,
            "council_veto_count": 0,
            "concentration_event_count": 0,
            "win_rate_pct": round(win_rate, 2),
            "avg_net_ev_bps": round(avg_ev, 4),
            "avg_net_return_bps": 0.0,
            "avg_realized_r": 0.0,
            "avg_drawdown_bps": 0.0,
            "avg_fees_bps": 0.0,
            "avg_slippage_bps": 0.0,
            "realized_pnl_usd": round(pnl_total, 4),
            "score": round(score, 2),
            "created_at_ms": ts,
            "metadata": {
                "candidate_ids": sorted(set(group["candidate_ids"])),
                "allocation_ids": sorted(set(group["allocation_ids"])),
                "sample_quality": "legacy_candidate_ledger_fallback" if allocation_count else "candidate_only",
            },
        }
        await repository.upsert_strategy_regime_performance(row)
        rows.append(row)
    return sorted(rows, key=lambda item: (item["score"], item["candidate_count"]), reverse=True)


async def _list_outcomes(repository: Any, *, limit: int) -> list[dict[str, Any]]:
    method = getattr(repository, "list_candidate_outcome_attributions", None)
    if not callable(method):
        return []
    try:
        return await method(limit=limit)
    except TypeError:
        return await method()


async def _list_concentration_events(repository: Any, *, limit: int) -> list[dict[str, Any]]:
    method = getattr(repository, "list_portfolio_concentration_events", None)
    if not callable(method):
        return []
    try:
        return await method(limit=limit)
    except TypeError:
        return await method()


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


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _outcome_score(
    *,
    candidate_count: int,
    allocation_count: int,
    avg_net_return: float,
    avg_realized_r: float,
    win_rate: float,
    avg_drawdown: float,
    risk_reject_count: int,
    council_veto_count: int,
    concentration_count: int,
) -> float:
    sample_score = min(25.0, candidate_count * 2.0 + allocation_count * 3.0)
    return_score = max(-15.0, min(30.0, avg_net_return / 2.0))
    r_score = max(-10.0, min(20.0, avg_realized_r * 10.0))
    win_score = max(0.0, min(20.0, win_rate / 5.0))
    drawdown_penalty = min(15.0, avg_drawdown / 10.0)
    reject_penalty = min(15.0, risk_reject_count * 2.0 + council_veto_count * 2.0 + concentration_count)
    return max(0.0, min(100.0, 35.0 + sample_score + return_score + r_score + win_score - drawdown_penalty - reject_penalty))


def _legacy_score(*, candidate_count: int, allocation_count: int, avg_ev: float, win_rate: float, pnl_total: float) -> float:
    sample_score = min(30.0, candidate_count * 3.0 + allocation_count * 4.0)
    ev_score = max(0.0, min(30.0, avg_ev))
    win_score = max(0.0, min(25.0, win_rate / 4.0))
    pnl_score = max(-10.0, min(15.0, pnl_total / 10.0))
    return max(0.0, min(100.0, 20.0 + sample_score + ev_score + win_score + pnl_score))
