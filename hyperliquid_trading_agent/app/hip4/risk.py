from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.schemas import (
    Hip4Candidate,
    Hip4CapabilityProbe,
    Hip4RiskDecision,
    QuestionSpec,
)

FORBIDDEN_PAYLOAD_KEYS = {"signature", "nonce", "privateKey", "private_key", "exchange_actions", "action", "userOutcome"}


class Hip4RiskChecker:
    def __init__(self, *, settings: Settings, risk_gateway: Any | None = None):
        self.settings = settings
        self.risk_gateway = risk_gateway

    async def check_candidate(
        self,
        candidate: Hip4Candidate,
        *,
        capabilities: Hip4CapabilityProbe | None = None,
        question: QuestionSpec | None = None,
        registry_last_refresh_at_ms: int | None = None,
        now_ms: int | None = None,
        manual_ticket: bool = False,
    ) -> Hip4RiskDecision:
        now = now_ms or int(time.time() * 1000)
        violations: list[dict[str, Any]] = []
        if not self.settings.hip4_enabled:
            violations.append(_violation("hip4_disabled", "HIP-4 subsystem is disabled."))
        if candidate.mode not in {"shadow", "paper", "manual_ticket"}:
            violations.append(_violation("unsupported_mode", "HIP-4 candidate mode is unsupported."))
        if candidate.mode == "shadow" and not self.settings.hip4_mode_allows_scan:
            violations.append(_violation("mode_disallows_scan", "HIP-4 mode does not allow shadow scanning."))
        if candidate.mode == "paper" and not self.settings.hip4_mode_allows_paper:
            violations.append(_violation("mode_disallows_paper", "HIP-4 mode does not allow paper execution."))
        if (candidate.mode == "manual_ticket" or manual_ticket) and not self.settings.hip4_mode_allows_manual_ticket:
            violations.append(_violation("mode_disallows_manual_ticket", "HIP-4 mode does not allow manual tickets."))
        if candidate.mode == "manual_ticket" or manual_ticket:
            if not self.settings.hip4_manual_ticket_export_enabled:
                violations.append(_violation("manual_ticket_disabled", "HIP-4 manual ticket export is disabled."))
            if now - candidate.as_of_ms > self.settings.hip4_manual_ticket_max_book_staleness_ms:
                violations.append(_violation("manual_ticket_stale", "Manual ticket data is stale."))
        else:
            threshold = self.settings.hip4_paper_execution_max_book_staleness_ms if candidate.mode == "paper" else self.settings.hip4_scan_max_book_staleness_ms
            if now - candidate.as_of_ms > threshold:
                violations.append(_violation("stale_candidate", "HIP-4 candidate market data is stale."))
            stale_books = _stale_books_from_proof(candidate.proof, now_ms=now, max_staleness_ms=threshold)
            if stale_books:
                violations.append(_violation("stale_candidate_books", f"HIP-4 candidate contains stale book legs: {', '.join(stale_books)}"))
        if registry_last_refresh_at_ms is None or now - registry_last_refresh_at_ms > self.settings.hip4_registry_max_staleness_ms:
            violations.append(_violation("stale_registry", "HIP-4 registry metadata is stale."))
        if capabilities is None and (candidate.mode == "paper" or candidate.mode == "manual_ticket" or manual_ticket):
            violations.append(_violation("capability_probe_missing", "HIP-4 capability probe is required for paper/manual actions."))
        if capabilities is not None:
            if not capabilities.outcome_meta_available or not capabilities.supports_outcomes:
                violations.append(_violation("capability_unavailable", "HIP-4 outcome metadata capabilities are unavailable."))
            if candidate.mode == "paper" and not capabilities.supports_abstract_native_mechanics:
                violations.append(_violation("abstract_mechanics_disabled", "Capability probe disabled abstract HIP-4 mechanics."))
            if candidate.strategy_type.startswith("question_") and not capabilities.supports_question_mechanics:
                violations.append(_violation("question_mechanics_disabled", "HIP-4 question mechanics are capability-disabled."))
            if candidate.mode == "paper" and not self.settings.hip4_allow_inferred_lot_size_for_paper:
                if not capabilities.supports_authoritative_size_metadata:
                    violations.append(_violation("size_metadata_unavailable", "Authoritative HIP-4 size metadata is unavailable."))
                if not capabilities.supports_authoritative_tick_metadata:
                    violations.append(_violation("tick_metadata_unavailable", "Authoritative HIP-4 tick metadata is unavailable."))
            if (candidate.mode == "manual_ticket" or manual_ticket) and not capabilities.supports_manual_ticket_export:
                violations.append(_violation("manual_ticket_capability_disabled", "Capability probe disabled HIP-4 manual ticket export."))
        if question is not None and question.status != "open" and not self.settings.hip4_include_partially_settled:
            violations.append(_violation("settled_or_partial_question", "Settled/partially-settled question is not allowed."))
        bps_ok = candidate.expected_net_edge_bps >= self.settings.hip4_min_edge_bps
        usd_ok = candidate.expected_net_edge_usd >= self.settings.hip4_min_edge_usd
        if self.settings.hip4_edge_threshold_mode == "both" and not (bps_ok and usd_ok):
            violations.append(_violation("edge_below_minimum", "HIP-4 candidate does not meet both edge thresholds."))
        if self.settings.hip4_edge_threshold_mode == "either" and not (bps_ok or usd_ok):
            violations.append(_violation("edge_below_minimum", "HIP-4 candidate does not meet either edge threshold."))
        if candidate.gross_cost_or_proceeds > self.settings.hip4_max_paper_notional_per_candidate_usd:
            violations.append(_violation("candidate_notional_limit", "HIP-4 candidate exceeds per-candidate notional cap."))
        if candidate.residual_inventory and not self.settings.hip4_allow_inventory_carry:
            violations.append(_violation("residual_inventory", "Risk-free HIP-4 candidate leaves residual inventory."))
        payload = candidate.model_dump(mode="json")
        found_forbidden = sorted(_find_forbidden_keys(payload))
        if found_forbidden:
            violations.append(_violation("live_payload_material", f"Candidate contains forbidden live payload keys: {', '.join(found_forbidden)}"))

        gateway_decision: dict[str, Any] | None = None
        if not violations and self.risk_gateway is not None and callable(getattr(self.risk_gateway, "check_order_intent", None)) and candidate.mode in {"paper", "shadow"}:
            intent = SimpleNamespace(
                intent_id=candidate.candidate_id,
                execution_mode=candidate.mode,
                target_size=candidate.size,
                target_notional_usd=candidate.gross_cost_or_proceeds,
                price_limit=1,
                max_slippage_bps=0,
                deadline_ts_ms=now + 60_000,
                created_at_ms=candidate.as_of_ms,
                asset_class="crypto",
                asset="HIP4",
                venue="hyperliquid_outcome_paper",
                strategy_id=candidate.strategy_type,
            )
            decision = await self.risk_gateway.check_order_intent(
                intent,
                market_snapshot={"last_market_data_at_ms": candidate.as_of_ms, "last_orderbook_at_ms": candidate.as_of_ms, "spread_bps": 0, "latency_ms": 0},
                portfolio_snapshot={"equity_usd": str(self.settings.hip4_paper_initial_equity_usd)},
                strategy_snapshot={"net_ev_bps": str(candidate.expected_net_edge_bps)},
                operator_context={"kill_switch_active": False, "config_approved": True, "model_approved": True},
            )
            gateway_decision = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else dict(decision)
            if not getattr(decision, "allowed", False):
                violations.append(_violation("risk_gateway_reject", "Existing deterministic RiskGateway rejected HIP-4 candidate."))

        return Hip4RiskDecision(allowed=not violations, decision="allow" if not violations else "reject", violations=violations, risk_gateway_decision=gateway_decision)


def _stale_books_from_proof(proof: dict[str, Any], *, now_ms: int, max_staleness_ms: int) -> list[str]:
    raw = proof.get("book_as_of_ms_by_coin") if isinstance(proof, dict) else None
    if not isinstance(raw, dict):
        return []
    stale: list[str] = []
    for coin, as_of in raw.items():
        try:
            if now_ms - int(as_of) > max_staleness_ms:
                stale.append(str(coin))
        except (TypeError, ValueError):
            stale.append(str(coin))
    return stale


def _find_forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in FORBIDDEN_PAYLOAD_KEYS:
                found.add(str(key))
            found.update(_find_forbidden_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_forbidden_keys(item))
    return found


def _violation(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}
