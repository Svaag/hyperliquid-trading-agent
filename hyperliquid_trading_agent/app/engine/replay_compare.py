from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Any

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.config import Settings


def _now_ms() -> int:
    return int(time.time() * 1000)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def engine_config_hash(settings: Settings) -> str:
    keys = [
        "engine_min_net_ev_bps",
        "engine_min_risk_adjusted_utility",
        "engine_max_candidates_per_loop",
        "engine_max_approved_candidates_per_loop",
        "engine_strategy_throttles_enabled",
        "engine_strategy_max_candidates_per_loop",
        "engine_strategy_max_allocations_per_loop",
        "engine_strategy_max_allocation_share_pct",
    ]
    return stable_hash({key: getattr(settings, key, None) for key in keys})


def dataset_id_for_window(*, start_ms: int, end_ms: int, universe: list[str], config_hash: str) -> str:
    universe_hash = stable_hash(sorted(symbol.upper() for symbol in universe))
    return f"engine_dataset_{start_ms}_{end_ms}_{universe_hash}_{config_hash}"


class EngineReplayComparisonService:
    """Ledger-based shadow replay comparison using replay_results storage."""

    def __init__(self, *, repository: Any, settings: Settings):
        self.repository = repository
        self.settings = settings

    async def compare_variant(
        self,
        *,
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
        window_start_ms: int,
        window_end_ms: int,
        universe: list[str],
        dataset_id: str | None = None,
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        variant_id = variant_id or "engine_variant_" + stable_hash({"candidate_config": candidate_config, "window_end_ms": window_end_ms})
        baseline_config = self._normalize_config(baseline_config)
        candidate_config = self._normalize_config(candidate_config)
        universe = [symbol.upper() for symbol in universe]
        dataset_id = dataset_id or dataset_id_for_window(start_ms=window_start_ms, end_ms=window_end_ms, universe=universe, config_hash=stable_hash(baseline_config))
        # Window at the query layer so newest-first fetch limits cannot silently
        # truncate the comparison window; in-memory refiltering below stays for
        # repositories that ignore the window kwargs.
        window = {"since_ms": window_start_ms, "until_ms": window_end_ms}
        candidates = await _list_windowed(self.repository.list_alpha_candidates, limit=5000, **window)
        evs = await _list_windowed(self.repository.list_ev_estimates, limit=5000, **window)
        allocations = await _list_windowed(self.repository.list_allocation_decisions, limit=5000, **window)
        reports = await _list_windowed(self.repository.list_execution_reports, limit=5000, **window)
        risk_rejects = await _list_windowed(self.repository.list_risk_gateway_decisions, limit=5000, decision="reject", **window)
        pnl = await _list_windowed(self.repository.list_pnl_attribution, limit=5000, **window)
        outcomes = await _list_candidate_outcomes(self.repository, limit=5000, **window)
        candidates = [item for item in candidates if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms and (not universe or str(item.get("asset") or "").upper() in universe)]
        ev_by_candidate = {str(item.get("candidate_id")): item for item in evs if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms}
        allocations = [item for item in allocations if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
        reports = [item for item in reports if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
        risk_rejects = [item for item in risk_rejects if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
        pnl = [item for item in pnl if window_start_ms <= int(item.get("window_end_ms") or 0) <= window_end_ms]
        outcomes = [item for item in outcomes if window_start_ms <= int(item.get("window_end_ms") or item.get("created_at_ms") or 0) <= window_end_ms and (not universe or str(item.get("asset") or "").upper() in universe)]
        baseline_metrics = self._metrics_for_config(candidates, ev_by_candidate, allocations, reports, risk_rejects, pnl, outcomes, baseline_config)
        candidate_metrics = self._metrics_for_config(candidates, ev_by_candidate, allocations, reports, risk_rejects, pnl, outcomes, candidate_config)
        diffs = _diff_metrics(baseline_metrics, candidate_metrics)
        dominance_cap_pct = self.settings.engine_readiness_max_strategy_allocation_share_pct
        min_sample = max(1, int(getattr(self.settings, "engine_replay_min_sample_candidates", 50)))
        min_shadow_intents = max(1, int(getattr(self.settings, "engine_replay_min_shadow_intents", 50)))
        if (
            len(candidates) < min_sample
            or int(baseline_metrics.get("allocated_count") or 0) == 0
            or int(baseline_metrics.get("shadow_intent_count") or 0) < min_shadow_intents
        ):
            # An empty or thin window cannot fail nor pass a config; the old
            # behavior scored it "candidate_worse" and blocked readiness with a
            # misleading verdict.
            verdict = "insufficient_data"
            status = "insufficient_data"
        elif stable_hash(baseline_config) == stable_hash(candidate_config):
            if float(candidate_metrics.get("dominant_strategy_share_pct") or 0.0) <= dominance_cap_pct:
                verdict = "baseline_equivalence"
                status = "advisory_pass"
            else:
                verdict = "dominance_cap_breach"
                status = "failed"
        else:
            verdict = _verdict(baseline_metrics, candidate_metrics, diffs, dominance_cap_pct)
            status = "passed" if verdict == "candidate_better" else "failed" if verdict == "candidate_worse" else "advisory_pass"
        promotion_decision = "eligible_for_review" if status in {"passed", "advisory_pass"} else "do_not_promote"
        ts = _now_ms()
        replay_id = "ereplay_" + stable_hash({"variant_id": variant_id, "dataset_id": dataset_id, "ts": ts})
        artifact = {
            "replay_id": replay_id,
            "proposal_id": f"engine:{variant_id}",
            "decision_id": None,
            "status": status,
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "diffs": diffs,
            "caveats": [
                "ledger_replay_without_market_reconstruction_v1",
                "candidate_threshold_variants_reuse_recorded_allocation_decisions",
            ],
            "created_at_ms": ts,
            "metadata": {
                "schema_version": 1,
                "artifact_type": "engine_shadow_comparison",
                "baseline_engine_version": __version__,
                "candidate_engine_version": __version__,
                "baseline_config_hash": stable_hash(baseline_config),
                "candidate_config_hash": stable_hash(candidate_config),
                "scorer_variant": "deterministic_fallback_v1",
                "threshold_variant": candidate_config,
                "data_window": {"start_ms": window_start_ms, "end_ms": window_end_ms},
                "replay_dataset_id": dataset_id,
                "market_universe": universe,
                "verdict": verdict,
                "promotion_decision": promotion_decision,
                "variant_id": variant_id,
                "sample_requirements": {
                    "min_candidates": min_sample,
                    "min_shadow_intents": min_shadow_intents,
                    "requires_approved_allocation": True,
                },
                "notes": [],
                "exchange_actions": [],
            },
        }
        if getattr(self.repository, "enabled", False):
            await self.repository.record_replay_result(artifact)
            await self._record_replay_links(artifact, candidate_metrics)
        return artifact

    def _normalize_config(self, value: dict[str, Any]) -> dict[str, Any]:
        if not value or value.get("current") is True:
            return {
                "engine_min_net_ev_bps": self.settings.engine_min_net_ev_bps,
                "engine_min_risk_adjusted_utility": self.settings.engine_min_risk_adjusted_utility,
            }
        return {
            "engine_min_net_ev_bps": float(value.get("engine_min_net_ev_bps", self.settings.engine_min_net_ev_bps)),
            "engine_min_risk_adjusted_utility": float(value.get("engine_min_risk_adjusted_utility", self.settings.engine_min_risk_adjusted_utility)),
        }

    def _metrics_for_config(
        self,
        candidates: list[dict[str, Any]],
        ev_by_candidate: dict[str, dict[str, Any]],
        allocations: list[dict[str, Any]],
        reports: list[dict[str, Any]],
        risk_rejects: list[dict[str, Any]],
        pnl: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        eligible: list[dict[str, Any]] = []
        net_evs: list[float] = []
        utilities: list[float] = []
        candidates_by_id = {
            str(candidate.get("candidate_id") or ""): candidate
            for candidate in candidates
            if candidate.get("candidate_id")
        }
        active_alpha_strategies: set[str] = set()
        active_alpha_families: set[str] = set()
        min_ev = float(config.get("engine_min_net_ev_bps", 0))
        min_utility = float(config.get("engine_min_risk_adjusted_utility", 0))
        for candidate in candidates:
            ev = ev_by_candidate.get(str(candidate.get("candidate_id")))
            if not ev:
                continue
            net_ev = float(ev.get("net_ev_bps") or 0)
            utility = float(ev.get("risk_adjusted_utility") or 0)
            if net_ev >= min_ev and utility >= min_utility:
                eligible.append(candidate)
                net_evs.append(net_ev)
                utilities.append(utility)
        eligible_ids = {str(item.get("candidate_id") or "") for item in eligible}
        latest_allocation_by_candidate: dict[str, dict[str, Any]] = {}
        for allocation in allocations:
            candidate_id = str(allocation.get("candidate_id") or "")
            if candidate_id not in eligible_ids:
                continue
            current = latest_allocation_by_candidate.get(candidate_id)
            if current is None or int(allocation.get("created_at_ms") or 0) > int(current.get("created_at_ms") or 0):
                latest_allocation_by_candidate[candidate_id] = allocation
        approved_allocations = [
            allocation
            for allocation in latest_allocation_by_candidate.values()
            if str(allocation.get("status") or "") in {"allocate", "reduce"}
            and float(allocation.get("allocated_notional_usd") or 0) > 0
        ]
        strategy_notional: dict[str, float] = defaultdict(float)
        for allocation in approved_allocations:
            allocated_candidate: dict[str, Any] = candidates_by_id.get(str(allocation.get("candidate_id") or "")) or {}
            strategy_id = str(allocated_candidate.get("strategy_id") or "unknown")
            strategy_notional[strategy_id] += float(allocation.get("allocated_notional_usd") or 0)
            metadata_value = allocated_candidate.get("metadata")
            metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
            family = str(metadata.get("strategy_family") or allocated_candidate.get("strategy_family") or "unknown")
            counts_for_breadth = bool(metadata.get("counts_for_breadth", allocated_candidate.get("counts_for_breadth", True)))
            if counts_for_breadth and family not in {"legacy_bridge", "risk_off_defensive"} and allocated_candidate.get("side") != "flat":
                active_alpha_strategies.add(strategy_id)
                active_alpha_families.add(family)
        allocated_count = len(approved_allocations)
        approved_candidate_ids = {
            str(item.get("candidate_id") or "") for item in approved_allocations
        }
        allocated_outcomes = [
            item
            for item in outcomes
            if str(item.get("candidate_id") or "") in approved_candidate_ids
        ]
        outcome_groups = _outcome_groups(allocated_outcomes)
        outcome_candidate_ids = {
            str(item.get("candidate_id") or "")
            for item in allocated_outcomes
            if item.get("candidate_id")
        }
        risk_denominator = allocated_count + len(risk_rejects)
        total_allocated_notional = sum(strategy_notional.values())
        strategy_share = {
            strategy: round(notional / total_allocated_notional * 100, 4)
            for strategy, notional in strategy_notional.items()
        } if total_allocated_notional else {}
        return {
            "candidate_count": len(candidates),
            "ev_estimate_count": len(ev_by_candidate),
            "eligible_candidate_count": len(eligible),
            "allocated_count": allocated_count,
            "allocated_notional_usd": round(total_allocated_notional, 4),
            "allocation_rate_pct": round(allocated_count / len(eligible) * 100, 4) if eligible else 0.0,
            "shadow_intent_count": len([item for item in reports if item.get("execution_mode") == "shadow"]),
            "risk_reject_count": len(risk_rejects),
            "risk_reject_rate_pct": round(len(risk_rejects) / risk_denominator * 100, 4) if risk_denominator else 0.0,
            "avg_net_ev_bps": round(sum(net_evs) / len(net_evs), 4) if net_evs else 0.0,
            "avg_risk_adjusted_utility": round(sum(utilities) / len(utilities), 4) if utilities else 0.0,
            "avg_slippage_bps": round(sum(float(item.get("slippage_bps") or 0) for item in reports) / len(reports), 4) if reports else 0.0,
            "fees_usd": round(sum(float(item.get("fees_usd") or 0) for item in reports), 4),
            "total_pnl_usd": round(sum(float(item.get("total_pnl_usd") or 0) for item in pnl), 4),
            "strategy_allocation_share": strategy_share,
            "dominant_strategy_share_pct": max(strategy_share.values()) if strategy_share else 0.0,
            "active_alpha_strategy_count": len(active_alpha_strategies),
            "active_alpha_family_count": len(active_alpha_families),
            "active_alpha_strategies": sorted(active_alpha_strategies),
            "active_alpha_families": sorted(active_alpha_families),
            "outcome_attribution_count": len(allocated_outcomes),
            "outcome_attribution_coverage_pct": round(len(outcome_candidate_ids) / allocated_count * 100, 4) if allocated_count else 0.0,
            "strategy_regime_outcome_groups": outcome_groups,
        }

    async def _record_replay_links(self, artifact: dict[str, Any], candidate_metrics: dict[str, Any]) -> None:
        record = getattr(self.repository, "record_replay_result_link", None)
        if not callable(record):
            return
        replay_id = str(artifact.get("replay_id") or "")
        ts = int(artifact.get("created_at_ms") or _now_ms())
        for group_key, group in (candidate_metrics.get("strategy_regime_outcome_groups") or {}).items():
            for candidate_id in group.get("candidate_ids") or [None]:
                link = {
                    "link_id": "rrl_" + stable_hash({"replay_id": replay_id, "group_key": group_key, "candidate_id": candidate_id}),
                    "replay_id": replay_id,
                    "candidate_id": candidate_id,
                    "strategy_id": group.get("strategy_id") or "unknown",
                    "strategy_version": group.get("strategy_version") or "unknown",
                    "strategy_family": group.get("strategy_family") or "unknown",
                    "asset": group.get("asset") or "GLOBAL",
                    "venue": group.get("venue") or "unknown",
                    "regime_snapshot_id": group.get("regime_snapshot_id"),
                    "horizon": group.get("candidate_horizon") or "unknown",
                    "outcome_window": group.get("outcome_window") or "unknown",
                    "created_at_ms": ts,
                    "metadata": {"group_key": group_key, "replay_status": artifact.get("status"), "artifact_type": "engine_replay_result_link"},
                }
                await record(link)


async def list_engine_replay_comparisons(repository: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    list_replays = getattr(repository, "list_replay_results", None)
    if not callable(list_replays):
        return []
    items = await list_replays(limit=limit)
    return [item for item in items if str(item.get("proposal_id") or "").startswith("engine:") or (item.get("metadata") or {}).get("artifact_type") == "engine_shadow_comparison"]


async def latest_engine_replay_comparison(repository: Any) -> dict[str, Any] | None:
    items = await list_engine_replay_comparisons(repository, limit=1)
    return items[0] if items else None


async def _list_windowed(method: Any, *, limit: int, **kwargs: Any) -> list[dict[str, Any]]:
    try:
        return await method(limit=limit, **kwargs)
    except TypeError:
        kwargs.pop("since_ms", None)
        kwargs.pop("until_ms", None)
        return await method(limit=limit, **kwargs)


async def _list_candidate_outcomes(repository: Any, *, limit: int, **kwargs: Any) -> list[dict[str, Any]]:
    method = getattr(repository, "list_candidate_outcome_attributions", None)
    if not callable(method):
        return []
    try:
        return await _list_windowed(method, limit=limit, **kwargs)
    except TypeError:
        return await method()


def _outcome_groups(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        metadata_value = outcome.get("metadata")
        metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
        strategy_id = str(outcome.get("strategy_id") or "unknown")
        strategy_version = str(outcome.get("strategy_version") or "unknown")
        strategy_family = str(outcome.get("strategy_family") or "unknown")
        regime = str(metadata.get("regime_label") or outcome.get("regime_snapshot_id") or "unknown")
        asset = str(outcome.get("asset") or "GLOBAL").upper()
        venue = str(outcome.get("venue") or "unknown")
        outcome_window = str(outcome.get("outcome_window") or "unknown")
        key = "|".join([strategy_id, regime, asset, venue, outcome_window])
        group = groups.setdefault(
            key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "strategy_family": strategy_family,
                "regime_label": regime,
                "regime_snapshot_id": outcome.get("regime_snapshot_id"),
                "asset": asset,
                "venue": venue,
                "candidate_horizon": str(outcome.get("candidate_horizon") or "unknown"),
                "outcome_window": outcome_window,
                "candidate_ids": set(),
                "net_return_bps": [],
                "realized_r": [],
                "risk_reject_count": 0,
                "council_veto_count": 0,
            },
        )
        if outcome.get("candidate_id"):
            group["candidate_ids"].add(str(outcome["candidate_id"]))
        group["net_return_bps"].append(float(outcome.get("net_return_bps") or 0.0))
        group["realized_r"].append(float(outcome.get("realized_r") or 0.0))
        if str(outcome.get("risk_decision") or "") in {"reject", "halt", "tighten"}:
            group["risk_reject_count"] += 1
        if str(outcome.get("council_decision") or "") in {"reject", "needs_more_evidence"}:
            group["council_veto_count"] += 1
    out: dict[str, Any] = {}
    for key, group in groups.items():
        net_values = group.pop("net_return_bps")
        r_values = group.pop("realized_r")
        candidate_ids = sorted(group.pop("candidate_ids"))
        out[key] = {
            **group,
            "candidate_ids": candidate_ids,
            "candidate_count": len(candidate_ids),
            "avg_net_return_bps": round(sum(net_values) / len(net_values), 4) if net_values else 0.0,
            "avg_realized_r": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
            "win_rate_pct": round(sum(1 for value in net_values if value > 0) / len(net_values) * 100, 4) if net_values else 0.0,
        }
    return out


def _diff_metrics(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "allocated_count_delta": candidate.get("allocated_count", 0) - baseline.get("allocated_count", 0),
        "allocation_rate_delta_pct": round(candidate.get("allocation_rate_pct", 0) - baseline.get("allocation_rate_pct", 0), 4),
        "risk_reject_rate_delta_pct": round(candidate.get("risk_reject_rate_pct", 0) - baseline.get("risk_reject_rate_pct", 0), 4),
        "avg_net_ev_delta_bps": round(candidate.get("avg_net_ev_bps", 0) - baseline.get("avg_net_ev_bps", 0), 4),
        "avg_slippage_delta_bps": round(candidate.get("avg_slippage_bps", 0) - baseline.get("avg_slippage_bps", 0), 4),
        "dominance_delta_pct": round(candidate.get("dominant_strategy_share_pct", 0) - baseline.get("dominant_strategy_share_pct", 0), 4),
    }


def _verdict(baseline: dict[str, Any], candidate: dict[str, Any], diffs: dict[str, Any], dominance_cap_pct: float) -> str:
    baseline_allocated = max(1, int(baseline.get("allocated_count") or 0))
    if (
        diffs["risk_reject_rate_delta_pct"] > 10
        or candidate.get("allocated_count", 0) < baseline_allocated * 0.25
        or candidate.get("dominant_strategy_share_pct", 0) > dominance_cap_pct
        or diffs["avg_slippage_delta_bps"] > 5
    ):
        return "candidate_worse"
    if (
        diffs["risk_reject_rate_delta_pct"] <= 5
        and diffs["avg_slippage_delta_bps"] <= 2
        and candidate.get("dominant_strategy_share_pct", 0) <= dominance_cap_pct
        and candidate.get("allocated_count", 0) >= baseline_allocated * 0.5
        and diffs["avg_net_ev_delta_bps"] >= 1
    ):
        return "candidate_better"
    return "inconclusive"
