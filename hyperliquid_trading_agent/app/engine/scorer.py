from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, EVEstimate, ExecutionCostQuote, RegimeVector
from hyperliquid_trading_agent.app.engine.time_block_stats import BLOCK_MS_BY_HORIZON

DETERMINISTIC_MODEL_VERSION = "deterministic_fallback_v1"
NO_EDGE_MODEL_VERSION = "conservative_no_edge_v1"
# Retained for import compatibility.  The contribution is intentionally zero.
STRATEGY_EDGE_PRIOR_CAP_BPS = 0.0
EMPIRICAL_ARTIFACT_VERSION = "hierarchical_empirical_ev_v1"
UNMEASURED_COST_PENALTY_BPS = 25.0


class DeterministicEVScorer:
    """Compatibility name for the conservative no-edge fallback.

    Strategy-supplied ``expected_edge_bps`` is retained only in audit metadata and
    contributes exactly zero to the estimate.
    """

    model_version_id = DETERMINISTIC_MODEL_VERSION

    def score(
        self,
        candidate: AlphaCandidate,
        regime: RegimeVector | None = None,
        cost_quote: ExecutionCostQuote | None = None,
    ) -> EVEstimate:
        del regime
        target_bps = _target_bps(candidate)
        stop_bps = _stop_bps(candidate)
        costs = _cost_components(cost_quote)
        funding_bps = float(candidate.metadata.get("expected_funding_cost_bps") or 0.0)
        gross_ev = 0.0
        net_ev = gross_ev - costs["total"] - funding_bps
        tail = max(stop_bps * 1.25, 1.0)
        digest = hashlib.sha1(
            f"{candidate.candidate_id}:{self.model_version_id}:{getattr(cost_quote, 'quote_id', '')}".encode()
        ).hexdigest()[:24]
        return EVEstimate(
            estimate_id="ev_" + digest,
            candidate_id=candidate.candidate_id,
            model_version_id=self.model_version_id,
            p_target=0.0,
            p_stop=0.0,
            p_timeout=1.0,
            expected_favorable_bps=target_bps,
            expected_adverse_bps=stop_bps,
            expected_holding_ms=_horizon_ms(candidate.horizon),
            expected_fee_bps=costs["fee"],
            expected_spread_cost_bps=costs["spread"],
            expected_slippage_bps=costs["slippage"],
            expected_market_impact_bps=costs["impact"],
            expected_funding_cost_bps=funding_bps,
            tail_loss_bps=tail,
            gross_ev_bps=gross_ev,
            net_ev_bps=net_ev,
            risk_adjusted_utility=net_ev / tail,
            uncertainty=1.0,
            calibration_bucket=f"no_edge:{candidate.strategy_id}:{candidate.asset_class}:{candidate.horizon}",
            execution_cost_quote_id=cost_quote.quote_id if cost_quote else None,
            created_at_ms=now_ms(),
            metadata={
                "base_net_ev_bps": net_ev,
                "strategy_edge_prior_bps": 0.0,
                "strategy_edge_prior_cap_bps": 0.0,
                "strategy_edge_prior_source": "candidate.expected_edge_bps",
                "strategy_supplied_edge_bps_audit_only": float(candidate.expected_edge_bps or 0.0),
                "strategy_supplied_edge_contribution_bps": 0.0,
                "scorer_kind": "conservative_no_edge",
                "cost_quality": cost_quote.cost_quality if cost_quote else "unavailable",
                "risk_gateway_required": True,
                "paper_approved": False,
            },
        )


class ConservativeNoEdgeScorer(DeterministicEVScorer):
    model_version_id = NO_EDGE_MODEL_VERSION


@dataclass(frozen=True)
class EmpiricalBucket:
    count: int
    mean_gross_return_bps: float
    positive_rate: float


@dataclass(frozen=True)
class EmpiricalEVArtifact:
    model_version_id: str
    shrinkage_strength: float
    global_bucket: EmpiricalBucket
    buckets: dict[str, EmpiricalBucket]
    metrics: dict[str, Any]
    training_data_hash: str
    created_at_ms: int

    @classmethod
    def load(cls, artifact_uri: str) -> EmpiricalEVArtifact:
        path = Path(artifact_uri.removeprefix("file://"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("artifact_version") != EMPIRICAL_ARTIFACT_VERSION:
            raise ValueError("unsupported empirical EV artifact version")
        return cls(
            model_version_id=str(payload["model_version_id"]),
            shrinkage_strength=float(payload.get("shrinkage_strength") or 20.0),
            global_bucket=EmpiricalBucket(**payload["global_bucket"]),
            buckets={key: EmpiricalBucket(**value) for key, value in dict(payload.get("buckets") or {}).items()},
            metrics=dict(payload.get("metrics") or {}),
            training_data_hash=str(payload.get("training_data_hash") or ""),
            created_at_ms=int(payload.get("created_at_ms") or 0),
        )

    def prediction(
        self, candidate: AlphaCandidate, regime: RegimeVector | None
    ) -> tuple[float, float, float, str, list[dict[str, Any]]]:
        mean = self.global_bucket.mean_gross_return_bps
        positive_rate = self.global_bucket.positive_rate
        effective_n = float(self.global_bucket.count)
        trace: list[dict[str, Any]] = []
        selected = "global"
        for key in _candidate_bucket_keys(candidate, regime):
            bucket = self.buckets.get(key)
            if bucket is None or bucket.count <= 0:
                continue
            weight = bucket.count / (bucket.count + self.shrinkage_strength)
            mean = weight * bucket.mean_gross_return_bps + (1.0 - weight) * mean
            positive_rate = weight * bucket.positive_rate + (1.0 - weight) * positive_rate
            # Buckets are nested views of the same observations. Never add their
            # counts as though each hierarchy level were independent evidence.
            effective_n = min(effective_n, float(bucket.count))
            selected = key
            trace.append({"key": key, "count": bucket.count, "weight": weight})
        uncertainty = max(0.05, min(1.0, 1.0 / math.sqrt(max(1.0, effective_n))))
        return mean, positive_rate, uncertainty, selected, trace


class EmpiricalEVScorer:
    def __init__(self, artifact: EmpiricalEVArtifact):
        self.artifact = artifact

    def score(
        self,
        candidate: AlphaCandidate,
        regime: RegimeVector | None = None,
        cost_quote: ExecutionCostQuote | None = None,
    ) -> EVEstimate:
        gross_ev, positive_rate, uncertainty, bucket, trace = self.artifact.prediction(candidate, regime)
        costs = _cost_components(cost_quote)
        funding_bps = float(candidate.metadata.get("expected_funding_cost_bps") or 0.0)
        net_ev = gross_ev - costs["total"] - funding_bps
        stop_bps = _stop_bps(candidate)
        tail = max(stop_bps * 1.25, abs(gross_ev) * 1.5, 1.0)
        p_target = min(0.95, max(0.0, positive_rate))
        p_stop = min(1.0 - p_target, max(0.0, 1.0 - positive_rate))
        p_timeout = max(0.0, 1.0 - p_target - p_stop)
        digest = hashlib.sha1(
            f"{candidate.candidate_id}:{self.artifact.model_version_id}:{getattr(cost_quote, 'quote_id', '')}".encode()
        ).hexdigest()[:24]
        return EVEstimate(
            estimate_id="ev_" + digest,
            candidate_id=candidate.candidate_id,
            model_version_id=self.artifact.model_version_id,
            p_target=p_target,
            p_stop=p_stop,
            p_timeout=p_timeout,
            expected_favorable_bps=_target_bps(candidate),
            expected_adverse_bps=stop_bps,
            expected_holding_ms=_horizon_ms(candidate.horizon),
            expected_fee_bps=costs["fee"],
            expected_spread_cost_bps=costs["spread"],
            expected_slippage_bps=costs["slippage"],
            expected_market_impact_bps=costs["impact"],
            expected_funding_cost_bps=funding_bps,
            tail_loss_bps=tail,
            gross_ev_bps=gross_ev,
            net_ev_bps=net_ev,
            risk_adjusted_utility=net_ev / tail,
            uncertainty=uncertainty,
            calibration_bucket=bucket,
            execution_cost_quote_id=cost_quote.quote_id if cost_quote else None,
            created_at_ms=now_ms(),
            metadata={
                "scorer_kind": "hierarchical_empirical_shrinkage",
                "bucket_trace": trace,
                "training_data_hash": self.artifact.training_data_hash,
                "walk_forward_metrics": self.artifact.metrics,
                "strategy_supplied_edge_bps_audit_only": float(candidate.expected_edge_bps or 0.0),
                "strategy_supplied_edge_contribution_bps": 0.0,
                "cost_quality": cost_quote.cost_quality if cost_quote else "unavailable",
                "paper_approved": True,
            },
        )


class EVScorerService:
    def __init__(self, repository: Any | None = None, settings: Any | None = None):
        self.repository = repository
        self.settings = settings
        self.fallback = ConservativeNoEdgeScorer() if settings is not None else DeterministicEVScorer()
        self.approved: EmpiricalEVScorer | None = None
        self.load_error: str | None = None
        self._load_attempted = False

    @property
    def model_approved(self) -> bool:
        return self.approved is not None

    async def score(
        self,
        candidate: AlphaCandidate,
        regime: RegimeVector | None = None,
        cost_quote: ExecutionCostQuote | None = None,
    ) -> EVEstimate:
        await self._load_approved_model()
        scorer = self.approved or self.fallback
        estimate = scorer.score(candidate, regime=regime, cost_quote=cost_quote)
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_ev_estimate", None)
            if callable(record):
                await record(estimate.model_dump(mode="json"))
        return estimate

    async def _load_approved_model(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        model_id = str(getattr(self.settings, "engine_approved_scorer_model_id", "") or "")
        if not model_id:
            self.load_error = "approved_model_id_not_configured"
            return
        if self.repository is None or not getattr(self.repository, "enabled", False):
            self.load_error = "model_registry_unavailable"
            return
        get_model = getattr(self.repository, "get_model_version", None)
        if not callable(get_model):
            self.load_error = "model_registry_lookup_unavailable"
            return
        row = await get_model(model_id)
        if not row or row.get("status") != "approved":
            self.load_error = "configured_model_not_approved"
            return
        if row.get("model_type") != "engine_empirical_ev_v1":
            self.load_error = "configured_model_type_invalid"
            return
        try:
            artifact = EmpiricalEVArtifact.load(str(row.get("artifact_uri") or ""))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self.load_error = f"artifact_load_failed:{type(exc).__name__}"
            return
        if artifact.model_version_id != model_id:
            self.load_error = "artifact_model_id_mismatch"
            return
        self.approved = EmpiricalEVScorer(artifact)
        self.load_error = None

    def status(self) -> dict[str, Any]:
        return {
            "model_approved": self.model_approved,
            "active_model_version_id": (
                self.approved.artifact.model_version_id if self.approved else self.fallback.model_version_id
            ),
            "fallback": "conservative_no_edge",
            "strategy_edge_prior_contribution_bps": 0.0,
            "load_error": self.load_error,
        }


def train_empirical_artifact(
    rows: list[dict[str, Any]],
    *,
    model_version_id: str,
    shrinkage_strength: float = 20.0,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    strict_raw = _strict_native_rows(rows)
    strict = _independent_training_rows(strict_raw)
    if not strict:
        raise ValueError("no strict native-horizon outcomes available")
    strict.sort(key=lambda row: (int(row.get("window_end_ms") or 0), str(row.get("candidate_id") or "")))
    global_bucket = _bucket(strict)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in strict:
        for key in _row_bucket_keys(row):
            grouped.setdefault(key, []).append(row)
    buckets = {key: _bucket(values) for key, values in sorted(grouped.items())}
    metrics = _walk_forward_metrics(strict, shrinkage_strength=shrinkage_strength)
    canonical = json.dumps(strict, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "artifact_version": EMPIRICAL_ARTIFACT_VERSION,
        "model_version_id": model_version_id,
        "shrinkage_strength": float(shrinkage_strength),
        "global_bucket": global_bucket,
        "buckets": buckets,
        "metrics": metrics,
        "training_data_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "created_at_ms": int(created_at_ms or now_ms()),
        "training_semantics": {
            "target": "strict_native_horizon_gross_return_bps",
            "validation": "expanding_window_walk_forward",
            "strategy_supplied_edge_priors": "excluded",
            "independence_unit": "purged_time_block_after_equal_instrument_weighting",
            "raw_strict_outcome_count": len(strict_raw),
            "effective_training_block_count": len(strict),
        },
    }


def _walk_forward_metrics(rows: list[dict[str, Any]], *, shrinkage_strength: float) -> dict[str, Any]:
    if len(rows) < 4:
        return {"folds": 0, "samples": 0, "mae_bps": None, "mean_error_bps": None, "reason": "insufficient_rows"}
    errors: list[float] = []
    fold_count = 0
    boundaries = sorted({int(row.get("window_end_ms") or 0) for row in rows})
    fold_boundaries = sorted(
        {
            boundaries[min(len(boundaries) - 1, int((len(boundaries) - 1) * quantile))]
            for quantile in (0.4, 0.6, 0.8)
        }
    )
    for index, boundary in enumerate(fold_boundaries):
        next_boundary = fold_boundaries[index + 1] if index + 1 < len(fold_boundaries) else None
        train = [row for row in rows if int(row.get("window_end_ms") or 0) < boundary]
        test = [
            row
            for row in rows
            if int(row.get("window_end_ms") or 0) >= boundary
            and (next_boundary is None or int(row.get("window_end_ms") or 0) < next_boundary)
        ]
        if not train or not test:
            continue
        fold_count += 1
        global_mean = statistics.fmean(float(row.get("gross_return_bps") or 0.0) for row in train)
        by_key: dict[str, list[float]] = {}
        for row in train:
            for key in _row_bucket_keys(row):
                by_key.setdefault(key, []).append(float(row.get("gross_return_bps") or 0.0))
        for row in test:
            prediction = global_mean
            for key in _row_bucket_keys(row):
                values = by_key.get(key)
                if not values:
                    continue
                weight = len(values) / (len(values) + shrinkage_strength)
                prediction = weight * statistics.fmean(values) + (1.0 - weight) * prediction
            errors.append(prediction - float(row.get("gross_return_bps") or 0.0))
    return {
        "folds": fold_count,
        "samples": len(errors),
        "mae_bps": statistics.fmean(abs(value) for value in errors) if errors else None,
        "mean_error_bps": statistics.fmean(errors) if errors else None,
    }


def _strict_native_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded_flags = {
        "feature_store_mark_missing",
        "latest_mark_fallback",
        "late_mark",
        "future_mark",
        "missing_mark_px",
        "mark_missing",
        "mark_stale",
        "future_mark_used",
        "non_strict_mark_source",
    }
    strict: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("terminal_state") or "") != "matured":
            continue
        if str(row.get("candidate_horizon") or "") != str(row.get("outcome_window") or ""):
            continue
        if str(row.get("side") or "flat") not in {"long", "short"}:
            continue
        metadata_value = row.get("metadata")
        metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
        if str(metadata.get("mark_source") or "") != "feature_store_mid":
            continue
        try:
            float(row["gross_return_bps"])
            int(row["window_start_ms"])
            int(row["window_end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        flags = set(row.get("quality_flags") or [])
        if flags & excluded_flags:
            continue
        strict.append(dict(row))
    return strict


def _independent_training_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    instrument_groups: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    templates: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        horizon = str(row.get("candidate_horizon") or row.get("outcome_window") or "")
        block_ms = BLOCK_MS_BY_HORIZON.get(horizon)
        if block_ms is None:
            continue
        start_ms = int(row.get("window_start_ms") or 0)
        end_ms = int(row.get("window_end_ms") or 0)
        block_start = (start_ms // block_ms) * block_ms
        block_end = block_start + block_ms
        if end_ms <= start_ms or end_ms > block_end:
            continue
        metadata_value = row.get("metadata")
        metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
        instrument = str(
            row.get("instrument_id")
            or row.get("underlying_id")
            or f"{row.get('venue_id') or row.get('venue') or 'unknown'}:{row.get('asset') or 'UNKNOWN'}"
        )
        base_key = (
            str(row.get("strategy_id") or "unknown"),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("strategy_family") or "unknown"),
            str(row.get("asset_class") or metadata.get("asset_class") or "unknown"),
            str(row.get("regime_label") or metadata.get("regime_label") or "unknown"),
            horizon,
            block_start,
            block_end,
        )
        key = (*base_key, instrument)
        instrument_groups[key].append(float(row.get("gross_return_bps") or 0.0))
        templates[base_key] = row
    block_groups: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    for key, values in instrument_groups.items():
        block_groups[key[:-1]].append(statistics.fmean(values))
    independent: list[dict[str, Any]] = []
    for key, values in sorted(block_groups.items(), key=lambda item: (item[0][-1], item[0][0])):
        template = templates[key]
        independent.append(
            {
                **template,
                "candidate_id": f"block:{key[0]}:{key[-2]}:{key[-1]}",
                "window_start_ms": key[-2],
                "window_end_ms": key[-1],
                "gross_return_bps": statistics.fmean(values),
                "effective_instrument_count": len(values),
            }
        )
    return independent


def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("gross_return_bps") or 0.0) for row in rows]
    return {
        "count": len(values),
        "mean_gross_return_bps": statistics.fmean(values),
        "positive_rate": sum(value > 0 for value in values) / len(values),
    }


def _candidate_bucket_keys(candidate: AlphaCandidate, regime: RegimeVector | None) -> list[str]:
    version = candidate.strategy_version or "unknown"
    regime_label = regime.regime_label if regime is not None else "unknown"
    return [
        f"horizon:{candidate.horizon}",
        f"family:{candidate.strategy_family}|horizon:{candidate.horizon}",
        f"strategy:{candidate.strategy_id}|horizon:{candidate.horizon}",
        f"strategy_version:{candidate.strategy_id}@{version}|horizon:{candidate.horizon}",
        (
            f"exact:{candidate.strategy_id}@{version}|asset_class:{candidate.asset_class}|"
            f"horizon:{candidate.horizon}|regime:{regime_label}"
        ),
    ]


def _row_bucket_keys(row: dict[str, Any]) -> list[str]:
    strategy = str(row.get("strategy_id") or "unknown")
    version = str(row.get("strategy_version") or "unknown")
    family = str(row.get("strategy_family") or "unknown")
    horizon = str(row.get("candidate_horizon") or row.get("outcome_window") or "unknown")
    asset_class = str(row.get("asset_class") or (row.get("metadata") or {}).get("asset_class") or "unknown")
    regime = str(row.get("regime_label") or (row.get("metadata") or {}).get("regime_label") or "unknown")
    return [
        f"horizon:{horizon}",
        f"family:{family}|horizon:{horizon}",
        f"strategy:{strategy}|horizon:{horizon}",
        f"strategy_version:{strategy}@{version}|horizon:{horizon}",
        f"exact:{strategy}@{version}|asset_class:{asset_class}|horizon:{horizon}|regime:{regime}",
    ]


def _cost_components(cost_quote: ExecutionCostQuote | None) -> dict[str, float]:
    if cost_quote is None:
        return {
            "fee": 0.0,
            "spread": 0.0,
            # Carry the fail-closed penalty in an explicit EV component so
            # delayed outcome rows can reconstruct the modeled net return.
            "slippage": UNMEASURED_COST_PENALTY_BPS,
            "impact": 0.0,
            "total": UNMEASURED_COST_PENALTY_BPS,
        }
    total = float(cost_quote.total_execution_cost_bps)
    if cost_quote.cost_quality in {"unavailable", "stale"}:
        total = max(total, UNMEASURED_COST_PENALTY_BPS)
    fee = float(cost_quote.fee_bps)
    spread = float(cost_quote.spread_cost_bps)
    impact = float(cost_quote.market_impact_bps)
    # ``ExecutionCostQuote.slippage_bps`` is the observable touch-to-fill move
    # (spread + impact). EVEstimate stores mutually exclusive components, so its
    # slippage field carries latency and any fail-closed residual only.
    residual = max(0.0, total - fee - spread - impact)
    return {
        "fee": fee,
        "spread": spread,
        "slippage": residual,
        "impact": impact,
        "total": total,
    }


def _target_bps(candidate: AlphaCandidate) -> float:
    if not candidate.targets or candidate.proposed_entry <= 0:
        return 0.0
    return abs(candidate.targets[0] - candidate.proposed_entry) / candidate.proposed_entry * 10_000


def _stop_bps(candidate: AlphaCandidate) -> float:
    return (
        abs(candidate.proposed_entry - candidate.stop) / candidate.proposed_entry * 10_000
        if candidate.proposed_entry > 0
        else 0.0
    )


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
    parser = argparse.ArgumentParser(description="Train a hierarchical empirical engine EV artifact.")
    parser.add_argument("train", nargs="?")
    parser.add_argument("--input-json", required=True, help="JSON array of candidate outcome attributions")
    parser.add_argument("--model-version-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--shrinkage-strength", type=float, default=20.0)
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit("--input-json must contain a JSON array")
    artifact = train_empirical_artifact(
        payload,
        model_version_id=args.model_version_id,
        shrinkage_strength=args.shrinkage_strength,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(train_main())
