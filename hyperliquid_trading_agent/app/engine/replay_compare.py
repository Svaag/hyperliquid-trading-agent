from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
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
        candidates = await self.repository.list_alpha_candidates(limit=5000)
        evs = await self.repository.list_ev_estimates(limit=5000)
        reports = await self.repository.list_execution_reports(limit=5000)
        risk_rejects = await self.repository.list_risk_gateway_decisions(limit=5000, decision="reject")
        pnl = await self.repository.list_pnl_attribution(limit=5000)
        candidates = [item for item in candidates if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms and (not universe or str(item.get("asset") or "").upper() in universe)]
        ev_by_candidate = {str(item.get("candidate_id")): item for item in evs if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms}
        reports = [item for item in reports if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
        risk_rejects = [item for item in risk_rejects if window_start_ms <= int(item.get("created_at_ms") or 0) <= window_end_ms]
        pnl = [item for item in pnl if window_start_ms <= int(item.get("window_end_ms") or 0) <= window_end_ms]
        baseline_metrics = self._metrics_for_config(candidates, ev_by_candidate, reports, risk_rejects, pnl, baseline_config)
        candidate_metrics = self._metrics_for_config(candidates, ev_by_candidate, reports, risk_rejects, pnl, candidate_config)
        diffs = _diff_metrics(baseline_metrics, candidate_metrics)
        verdict = _verdict(baseline_metrics, candidate_metrics, diffs, self.settings.engine_readiness_max_strategy_allocation_share_pct)
        promotion_decision = "eligible_for_review" if verdict == "candidate_better" else "do_not_promote"
        ts = _now_ms()
        replay_id = "ereplay_" + stable_hash({"variant_id": variant_id, "dataset_id": dataset_id, "ts": ts})
        artifact = {
            "replay_id": replay_id,
            "proposal_id": f"engine:{variant_id}",
            "decision_id": None,
            "status": "passed" if verdict == "candidate_better" else "failed" if verdict == "candidate_worse" else "audit_only",
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "diffs": diffs,
            "caveats": ["ledger_replay_without_market_reconstruction_v1"],
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
                "notes": [],
                "exchange_actions": [],
            },
        }
        if getattr(self.repository, "enabled", False):
            await self.repository.record_replay_result(artifact)
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
        reports: list[dict[str, Any]],
        risk_rejects: list[dict[str, Any]],
        pnl: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        eligible: list[dict[str, Any]] = []
        net_evs: list[float] = []
        utilities: list[float] = []
        strategy_counts: Counter[str] = Counter()
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
                strategy_counts[str(candidate.get("strategy_id") or "unknown")] += 1
        allocated_count = len(eligible)
        risk_denominator = allocated_count + len(risk_rejects)
        strategy_share = {strategy: round(count / allocated_count * 100, 4) for strategy, count in strategy_counts.items()} if allocated_count else {}
        return {
            "candidate_count": len(candidates),
            "ev_estimate_count": len(ev_by_candidate),
            "allocated_count": allocated_count,
            "allocation_rate_pct": round(allocated_count / len(candidates) * 100, 4) if candidates else 0.0,
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
        }


async def list_engine_replay_comparisons(repository: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    list_replays = getattr(repository, "list_replay_results", None)
    if not callable(list_replays):
        return []
    items = await list_replays(limit=limit)
    return [item for item in items if str(item.get("proposal_id") or "").startswith("engine:") or (item.get("metadata") or {}).get("artifact_type") == "engine_shadow_comparison"]


async def latest_engine_replay_comparison(repository: Any) -> dict[str, Any] | None:
    items = await list_engine_replay_comparisons(repository, limit=1)
    return items[0] if items else None


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
