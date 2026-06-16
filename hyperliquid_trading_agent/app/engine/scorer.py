from __future__ import annotations

import argparse
import hashlib
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, EVEstimate, RegimeVector

DETERMINISTIC_MODEL_VERSION = "deterministic_fallback_v1"


class DeterministicEVScorer:
    """Safe fallback scorer used until a human-approved sklearn artifact exists."""

    def score(self, candidate: AlphaCandidate, regime: RegimeVector | None = None) -> EVEstimate:
        rr = _risk_reward(candidate)
        p_target = min(0.68, max(0.2, 0.28 + candidate.confidence * 0.35 + max(0.0, rr - 1.0) * 0.05))
        p_stop = min(0.65, max(0.15, 0.42 - candidate.confidence * 0.15))
        p_timeout = max(0.0, 1.0 - p_target - p_stop)
        total = p_target + p_stop + p_timeout
        p_target, p_stop, p_timeout = p_target / total, p_stop / total, p_timeout / total
        target_bps = _target_bps(candidate)
        stop_bps = _stop_bps(candidate)
        fee_bps = 4.5
        spread_bps = float(candidate.metadata.get("spread_bps") or 1.5)
        slippage_bps = 2.0
        impact_bps = 0.5
        funding_bps = float(candidate.metadata.get("expected_funding_cost_bps") or 0.0)
        net_ev = p_target * target_bps - p_stop * stop_bps - fee_bps - spread_bps - slippage_bps - impact_bps - funding_bps
        tail = max(stop_bps * 1.25, 1.0)
        stability = regime.regime_stability_score if regime is not None else 0.35
        uncertainty = max(0.65, 1.0 - candidate.confidence * 0.4 - stability * 0.2)
        digest = hashlib.sha1(f"{candidate.candidate_id}:{DETERMINISTIC_MODEL_VERSION}".encode()).hexdigest()[:24]
        return EVEstimate(
            estimate_id="ev_" + digest,
            candidate_id=candidate.candidate_id,
            model_version_id=DETERMINISTIC_MODEL_VERSION,
            p_target=p_target,
            p_stop=p_stop,
            p_timeout=p_timeout,
            expected_favorable_bps=target_bps,
            expected_adverse_bps=stop_bps,
            expected_holding_ms=_horizon_ms(candidate.horizon),
            expected_fee_bps=fee_bps,
            expected_spread_cost_bps=spread_bps,
            expected_slippage_bps=slippage_bps,
            expected_market_impact_bps=impact_bps,
            expected_funding_cost_bps=funding_bps,
            tail_loss_bps=tail,
            net_ev_bps=net_ev,
            risk_adjusted_utility=net_ev / max(tail, 1.0),
            uncertainty=uncertainty,
            calibration_bucket=f"fallback:{candidate.strategy_id}:{candidate.asset_class}:{candidate.horizon}",
            created_at_ms=now_ms(),
        )


class EVScorerService:
    def __init__(self, repository: Any | None = None):
        self.repository = repository
        self.fallback = DeterministicEVScorer()

    async def score(self, candidate: AlphaCandidate, regime: RegimeVector | None = None) -> EVEstimate:
        estimate = self.fallback.score(candidate, regime=regime)
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_ev_estimate", None)
            if callable(record):
                await record(estimate.model_dump(mode="json"))
        return estimate


def _risk_reward(candidate: AlphaCandidate) -> float:
    stop_distance = abs(candidate.proposed_entry - candidate.stop)
    if stop_distance <= 0 or not candidate.targets:
        return 0.0
    reward = abs(candidate.targets[0] - candidate.proposed_entry)
    return reward / stop_distance


def _target_bps(candidate: AlphaCandidate) -> float:
    if not candidate.targets or candidate.proposed_entry <= 0:
        return 0.0
    return abs(candidate.targets[0] - candidate.proposed_entry) / candidate.proposed_entry * 10_000


def _stop_bps(candidate: AlphaCandidate) -> float:
    return abs(candidate.proposed_entry - candidate.stop) / candidate.proposed_entry * 10_000 if candidate.proposed_entry > 0 else 0.0


def _horizon_ms(horizon: str) -> int:
    text = horizon.lower().strip()
    if text.endswith("m"):
        return int(float(text[:-1]) * 60_000)
    if text.endswith("h"):
        return int(float(text[:-1]) * 3_600_000)
    if text.endswith("d"):
        return int(float(text[:-1]) * 86_400_000)
    return 30 * 60_000


def train_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train engine EV scorer artifacts (scaffold).")
    parser.add_argument("train", nargs="?")
    parser.add_argument("--start-ms", type=int, required=False)
    parser.add_argument("--end-ms", type=int, required=False)
    parser.add_argument("--output-dir", required=False)
    args = parser.parse_args(argv)
    raise SystemExit(f"offline sklearn training scaffold only; requested output_dir={args.output_dir!r}")


if __name__ == "__main__":  # pragma: no cover
    train_main()
