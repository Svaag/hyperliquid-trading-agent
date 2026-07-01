from __future__ import annotations

from collections import Counter
from typing import Any


def _node_id(node_type: str, value: Any) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return f"{node_type}:{text}"


def _edge_id(source: str, target: str, edge_type: str) -> str:
    return f"{edge_type}:{source}->{target}"


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("metadata") if isinstance(item.get("metadata"), dict) else {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _maybe_list(repository: Any, method_name: str, **kwargs) -> list[dict[str, Any]]:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return []
    try:
        return await method(**kwargs)
    except TypeError:
        return await method()


class AlphaGraphBuilder:
    """Build the read-only Strategy-Regime Alpha Graph from existing evidence tables."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.node_counts: Counter[str] = Counter()
        self.edge_counts: Counter[str] = Counter()

    def node(self, node_type: str, value: Any, *, label: str | None = None, attributes: dict[str, Any] | None = None) -> str:
        nid = _node_id(node_type, value)
        existing = self.nodes.get(nid, {})
        merged = {**existing.get("attributes", {}), **(attributes or {})}
        self.nodes[nid] = {"id": nid, "type": node_type, "label": label or str(value or "unknown"), "attributes": merged}
        self.node_counts[node_type] += 1
        return nid

    def edge(self, source: str, target: str, edge_type: str, *, attributes: dict[str, Any] | None = None) -> str:
        eid = _edge_id(source, target, edge_type)
        existing = self.edges.get(eid, {})
        merged = {**existing.get("attributes", {}), **(attributes or {})}
        count = int(existing.get("count") or 0) + 1
        self.edges[eid] = {"id": eid, "source": source, "target": target, "type": edge_type, "count": count, "attributes": merged}
        self.edge_counts[edge_type] += 1
        return eid


def _performance_edge_type(row: dict[str, Any]) -> str:
    candidate_count = _i(row.get("candidate_count"))
    score = _f(row.get("score"))
    avg_return = _f(row.get("avg_net_return_bps"))
    if candidate_count < 5:
        return "needs_more_evidence_in"
    if score >= 45 and avg_return >= 0:
        return "worked_in"
    return "failed_in"


def _regime_label(item: dict[str, Any]) -> str:
    metadata = _metadata(item)
    return str(metadata.get("regime_label") or item.get("regime_label") or item.get("regime_snapshot_id") or "unknown")


async def build_strategy_regime_alpha_graph(repository: Any, *, limit: int = 1000) -> dict[str, Any]:
    """Return a governed Strategy-Regime Alpha Graph projection.

    This is read-only and does not mutate strategy config, risk limits, or order state.
    It projects existing evidence rows into graph nodes/edges so Council and operators
    can inspect what worked, failed, needs evidence, was risk rejected, vetoed, or replay-failed.
    """

    builder = AlphaGraphBuilder()
    specs = await _maybe_list(repository, "list_strategy_specs", limit=limit)
    candidates = await _maybe_list(repository, "list_alpha_candidates", limit=limit)
    outcomes = await _maybe_list(repository, "list_candidate_outcome_attributions", limit=limit)
    performance = await _maybe_list(repository, "list_strategy_regime_performance", limit=limit)
    council_reviews = await _maybe_list(repository, "list_council_reviews", limit=limit)
    risk_decisions = await _maybe_list(repository, "list_risk_gateway_decisions", limit=limit)
    replay_links = await _maybe_list(repository, "list_replay_result_links", limit=limit)
    regimes = await _maybe_list(repository, "list_regime_snapshots", limit=limit)

    for spec in specs:
        strategy = str(spec.get("strategy_id") or "unknown")
        family = str(spec.get("family") or spec.get("strategy_family") or "unknown")
        strategy_node = builder.node(
            "strategy",
            strategy,
            attributes={
                "version": spec.get("version"),
                "family": family,
                "enabled": bool(spec.get("enabled", True)),
                "counts_for_breadth": bool(spec.get("counts_for_breadth", True)),
            },
        )
        family_node = builder.node("strategy_family", family)
        builder.edge(strategy_node, family_node, "belongs_to")

    for regime in regimes:
        vector = regime.get("vector") if isinstance(regime.get("vector"), dict) else regime.get("vector_json") if isinstance(regime.get("vector_json"), dict) else regime
        label = str(vector.get("regime_label") or regime.get("regime_label") or regime.get("regime_snapshot_id") or "unknown")
        builder.node("market_regime", label, attributes={"regime_snapshot_id": regime.get("regime_snapshot_id"), "primary_asset": regime.get("primary_asset")})

    for candidate in candidates:
        strategy = str(candidate.get("strategy_id") or "unknown")
        metadata = _metadata(candidate)
        family = str(metadata.get("strategy_family") or candidate.get("strategy_family") or "unknown")
        regime = _regime_label(candidate)
        asset = str(candidate.get("asset") or "GLOBAL").upper()
        venue = str(candidate.get("venue") or metadata.get("venue") or "unknown")
        horizon = str(candidate.get("horizon") or "unknown")
        strategy_node = builder.node("strategy", strategy, attributes={"family": family})
        family_node = builder.node("strategy_family", family)
        regime_node = builder.node("market_regime", regime, attributes={"regime_snapshot_id": candidate.get("regime_snapshot_id")})
        asset_node = builder.node("asset", asset)
        venue_node = builder.node("venue", venue)
        horizon_node = builder.node("horizon", horizon)
        feature_node = builder.node("feature_set", candidate.get("feature_snapshot_id") or "unknown", attributes={"coverage_pct": metadata.get("feature_coverage_pct")})
        builder.edge(strategy_node, family_node, "belongs_to")
        builder.edge(strategy_node, regime_node, "fired_in", attributes={"candidate_id": candidate.get("candidate_id"), "side": candidate.get("side")})
        builder.edge(strategy_node, asset_node, "traded_asset")
        builder.edge(strategy_node, venue_node, "observed_on")
        builder.edge(strategy_node, horizon_node, "evaluated_at")
        builder.edge(strategy_node, feature_node, "used_feature_set")

    for row in performance:
        strategy = str(row.get("strategy_id") or "unknown")
        family = str(row.get("strategy_family") or "unknown")
        regime = str(row.get("regime_label") or "unknown")
        asset = str(row.get("asset") or "GLOBAL").upper()
        venue = str(row.get("venue") or "unknown")
        horizon = str(row.get("outcome_window") or "unknown")
        strategy_node = builder.node("strategy", strategy, attributes={"family": family, "version": row.get("strategy_version")})
        regime_node = builder.node("market_regime", regime)
        builder.node("asset", asset)
        builder.node("venue", venue)
        builder.node("horizon", horizon)
        builder.edge(
            strategy_node,
            regime_node,
            _performance_edge_type(row),
            attributes={
                "performance_id": row.get("performance_id"),
                "asset": asset,
                "venue": venue,
                "outcome_window": horizon,
                "candidate_count": _i(row.get("candidate_count")),
                "allocation_count": _i(row.get("allocation_count")),
                "score": _f(row.get("score")),
                "avg_net_return_bps": _f(row.get("avg_net_return_bps")),
                "avg_realized_r": _f(row.get("avg_realized_r")),
                "risk_reject_count": _i(row.get("risk_reject_count")),
                "council_veto_count": _i(row.get("council_veto_count")),
            },
        )

    for outcome in outcomes:
        strategy = str(outcome.get("strategy_id") or "unknown")
        regime = _regime_label(outcome)
        strategy_node = builder.node("strategy", strategy, attributes={"family": outcome.get("strategy_family"), "version": outcome.get("strategy_version")})
        outcome_node = builder.node(
            "outcome",
            outcome.get("attribution_id") or outcome.get("candidate_id") or "unknown",
            attributes={
                "candidate_id": outcome.get("candidate_id"),
                "outcome_window": outcome.get("outcome_window"),
                "terminal_state": outcome.get("terminal_state"),
                "net_return_bps": _f(outcome.get("net_return_bps")),
                "realized_r": _f(outcome.get("realized_r")),
            },
        )
        regime_node = builder.node("market_regime", regime, attributes={"regime_snapshot_id": outcome.get("regime_snapshot_id")})
        builder.edge(strategy_node, outcome_node, "produced_outcome")
        if str(outcome.get("risk_decision") or "") in {"reject", "halt", "tighten"} or str(outcome.get("allocation_status") or "") == "risk_rejected":
            risk_node = builder.node("risk_state", str(outcome.get("risk_decision") or outcome.get("allocation_status") or "risk_rejected"))
            builder.edge(strategy_node, risk_node, "risk_rejected_in", attributes={"candidate_id": outcome.get("candidate_id"), "regime_label": regime})
        council_decision = str(outcome.get("council_decision") or "")
        if council_decision in {"reject", "needs_more_evidence"}:
            council_node = builder.node("council_decision", council_decision)
            builder.edge(strategy_node, council_node, "council_vetoed_in" if council_decision == "reject" else "needs_more_evidence_in", attributes={"candidate_id": outcome.get("candidate_id"), "regime_label": regime})
        if _f(outcome.get("net_return_bps")) >= 0 and str(outcome.get("terminal_state") or "") == "matured":
            builder.edge(strategy_node, regime_node, "worked_in", attributes={"candidate_id": outcome.get("candidate_id"), "outcome_window": outcome.get("outcome_window")})
        elif str(outcome.get("terminal_state") or "") == "matured":
            builder.edge(strategy_node, regime_node, "failed_in", attributes={"candidate_id": outcome.get("candidate_id"), "outcome_window": outcome.get("outcome_window")})

    for review in council_reviews:
        decision = str(review.get("decision") or "unknown")
        strategy = str(review.get("strategy_id") or "unknown")
        strategy_node = builder.node("strategy", strategy)
        council_node = builder.node("council_decision", decision)
        if decision == "reject":
            edge_type = "council_vetoed_in"
        elif decision == "needs_more_evidence":
            edge_type = "needs_more_evidence_in"
        else:
            edge_type = "council_reviewed"
        builder.edge(strategy_node, council_node, edge_type, attributes={"review_id": review.get("review_id"), "candidate_id": review.get("candidate_id")})

    for risk in risk_decisions:
        metadata = _metadata(risk)
        strategy = str(metadata.get("strategy_id") or "unknown")
        if strategy == "unknown":
            continue
        decision = str(risk.get("decision") or "unknown")
        strategy_node = builder.node("strategy", strategy)
        risk_node = builder.node("risk_state", decision)
        builder.edge(strategy_node, risk_node, "risk_rejected_in" if decision in {"reject", "halt", "tighten"} else "risk_reviewed", attributes={"decision_id": risk.get("decision_id"), "intent_id": risk.get("intent_id")})

    for link in replay_links:
        strategy = str(link.get("strategy_id") or "unknown")
        metadata = _metadata(link)
        replay_status = str(metadata.get("replay_status") or link.get("status") or "unknown")
        regime = str(metadata.get("regime_label") or link.get("regime_snapshot_id") or "unknown")
        strategy_node = builder.node("strategy", strategy)
        regime_node = builder.node("market_regime", regime, attributes={"regime_snapshot_id": link.get("regime_snapshot_id")})
        edge_type = "replay_failed_in" if replay_status == "failed" else "replay_linked"
        builder.edge(strategy_node, regime_node, edge_type, attributes={"replay_id": link.get("replay_id"), "candidate_id": link.get("candidate_id"), "replay_status": replay_status})

    overfit_candidates = [row for row in performance if _i(row.get("candidate_count")) < 5 and _f(row.get("score")) >= 80]
    for row in overfit_candidates:
        strategy_node = builder.node("strategy", row.get("strategy_id") or "unknown")
        regime_node = builder.node("market_regime", row.get("regime_label") or "unknown")
        builder.edge(strategy_node, regime_node, "overfit_warning_in", attributes={"performance_id": row.get("performance_id"), "candidate_count": _i(row.get("candidate_count")), "score": _f(row.get("score"))})

    nodes = sorted(builder.nodes.values(), key=lambda item: (item["type"], item["id"]))
    edges = sorted(builder.edges.values(), key=lambda item: (item["type"], item["id"]))
    return {
        "graph_id": "strategy_regime_alpha_graph_v1",
        "schema_version": 1,
        "read_only": True,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "node_types": dict(sorted(Counter(node["type"] for node in nodes).items())),
            "edge_types": dict(sorted(Counter(edge["type"] for edge in edges).items())),
            "source_tables": [
                "strategy_specs",
                "alpha_candidates",
                "regime_snapshots",
                "candidate_evidence_links",
                "candidate_outcome_attributions",
                "strategy_regime_performance",
                "council_reviews",
                "risk_gateway_decisions",
                "replay_result_links",
            ],
        },
        "safety": {"config_mutation": False, "order_mutation": False, "risk_bypass": False, "council_bypass": False},
    }
