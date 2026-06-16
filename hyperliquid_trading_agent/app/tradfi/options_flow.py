"""Unusual options flow detection — deterministic pre-filter + LLM enrichment contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tradfi.schemas import (
    FLOW_PREMIUM_THRESHOLDS,
    FLOW_VOLUME_OI_THRESHOLDS,
    OptionContract,
    OptionsChain,
    OptionsFlowEvent,
)

log = get_logger(__name__)


class OptionsFlowDetector:
    """Deterministic pre-filter for unusual options activity.

    Flags contracts with:
    - Elevated volume/OI ratio
    - High premium (suggests institutional flow)
    - Sweep-like patterns (multiple same-side orders)
    - Unusual strike/expiry clustering

    LLM enrichment is a separate second pass (see :class:`FlowEnricher`).
    """

    def __init__(
        self,
        *,
        min_volume_oi_ratio: float = 3.0,
        min_premium: float = 1_000_000.0,
        cluster_window_pct: float = 5.0,
    ):
        self.min_volume_oi_ratio = min_volume_oi_ratio
        self.min_premium = min_premium
        self.cluster_window_pct = cluster_window_pct  # strike window as % of underlying for clustering

    def detect(
        self,
        chain: OptionsChain,
        *,
        volume_oi_ratio: float | None = None,
        min_premium: float | None = None,
    ) -> list[OptionsFlowEvent]:
        """Scan an options chain for unusual activity. Returns scored events.

        Args:
            chain: The options chain to scan
            volume_oi_ratio: Override default threshold
            min_premium: Override default premium threshold
        """
        events: list[OptionsFlowEvent] = []
        now = datetime.now(UTC)

        vol_oi_thresh = volume_oi_ratio or self.min_volume_oi_ratio
        premium_thresh = min_premium or self.min_premium

        # Compute cluster scores by expiry
        cluster_scores = self._compute_cluster_scores(chain)

        for contract in chain.contracts:
            if contract.volume is None or contract.open_interest is None:
                continue
            if contract.open_interest == 0:
                continue
            if contract.last_price is None:
                continue

            vol_oi = contract.volume / contract.open_interest
            premium = contract.volume * contract.last_price * 100

            if vol_oi < vol_oi_thresh and premium < premium_thresh:
                continue

            # Classify flow type
            flow_type = self._classify_flow(contract, chain)
            cluster_score = cluster_scores.get(contract.strike_price, 0.0)
            urgency = self._urgency_score(vol_oi, premium, cluster_score)

            events.append(
                OptionsFlowEvent(
                    symbol=chain.underlying,
                    detected_at=now,
                    contract=contract,
                    volume_oi_ratio=round(vol_oi, 2),
                    premium_estimate=round(premium, 2),
                    is_sweep=flow_type in {"call_buy", "put_buy"} and vol_oi > FLOW_VOLUME_OI_THRESHOLDS["unusual"],
                    cluster_score=round(cluster_score, 1),
                    flow_type=flow_type,
                    urgency_score=round(urgency, 1),
                )
            )

        return sorted(events, key=lambda e: e.urgency_score, reverse=True)

    def _classify_flow(self, contract: OptionContract, chain: OptionsChain) -> Literal["call_buy", "call_sell", "put_buy", "put_sell", "multi_leg", "unknown"]:
        """Heuristic flow classifier based on option type and where the trade price sits vs bid/ask."""
        if contract.option_type == "call":
            if contract.last_price and contract.ask and contract.last_price >= contract.ask:
                return "call_buy"
            if contract.last_price and contract.bid and contract.last_price <= contract.bid:
                return "call_sell"
            return "call_buy"  # default for elevated volume
        else:
            if contract.last_price and contract.ask and contract.last_price >= contract.ask:
                return "put_buy"
            if contract.last_price and contract.bid and contract.last_price <= contract.bid:
                return "put_sell"
            return "put_buy"

    def _compute_cluster_scores(self, chain: OptionsChain) -> dict[float, float]:
        """Score each strike by how much volume clusters around it vs neighbors."""
        if not chain.contracts or chain.underlying_price is None or chain.underlying_price == 0:
            return {}
        under = chain.underlying_price
        # Group volume by strike
        vol_by_strike: dict[float, float] = {}
        for c in chain.contracts:
            if c.volume:
                vol_by_strike[c.strike_price] = vol_by_strike.get(c.strike_price, 0.0) + float(c.volume)
        if not vol_by_strike:
            return {}
        strikes = sorted(vol_by_strike.keys())
        total_vol = sum(vol_by_strike.values())
        if total_vol == 0:
            return {}
        scores: dict[float, float] = {}
        for i, strike in enumerate(strikes):
            cluster_vol = vol_by_strike[strike]
            neighbor_vol = 0.0
            window = under * self.cluster_window_pct / 100.0
            for j, other in enumerate(strikes):
                if i != j and abs(other - strike) <= window:
                    neighbor_vol += vol_by_strike[other]
            concentration = cluster_vol / total_vol * 100
            neighbor_ratio = cluster_vol / (neighbor_vol + 0.001) if neighbor_vol < cluster_vol else 1.0
            scores[strike] = min(100.0, concentration * min(5.0, neighbor_ratio))
        return scores

    def _urgency_score(self, vol_oi: float, premium: float, cluster_score: float) -> float:
        score = 0.0
        # vol/OI component
        if vol_oi >= FLOW_VOLUME_OI_THRESHOLDS["extreme"]:
            score += 40.0
        elif vol_oi >= FLOW_VOLUME_OI_THRESHOLDS["unusual"]:
            score += 25.0
        elif vol_oi >= FLOW_VOLUME_OI_THRESHOLDS["elevated"]:
            score += 10.0
        # premium component
        if premium >= FLOW_PREMIUM_THRESHOLDS["extreme"]:
            score += 40.0
        elif premium >= FLOW_PREMIUM_THRESHOLDS["unusual"]:
            score += 25.0
        elif premium >= FLOW_PREMIUM_THRESHOLDS["elevated"]:
            score += 10.0
        # cluster component
        score += min(20.0, cluster_score / 5.0)
        return max(0.0, min(100.0, score))


class FlowEnricher:
    """LLM second-pass enrichment for options flow events.

    Mirrors the ``Enricher`` pattern from ``app/newswire/enrich.py``:
    deterministic-first, LLM-second, never gates tradability.
    """

    def __init__(self, *, model_gateway: Any | None = None, max_calls_per_hour: int = 20):
        self.model_gateway = model_gateway
        self.max_calls_per_hour = max_calls_per_hour
        self._call_times: list[float] = []

    @property
    def enabled(self) -> bool:
        return self.model_gateway is not None

    def _within_budget(self) -> bool:
        import time
        now = time.time()
        self._call_times = [ts for ts in self._call_times if now - ts < 3600]
        return len(self._call_times) < max(1, self.max_calls_per_hour)

    async def maybe_enrich(self, event: OptionsFlowEvent) -> dict[str, Any] | None:
        """Add LLM context to a flagged flow event. Returns enrichment dict or None."""
        gateway = self.model_gateway
        if gateway is None or not self._within_budget():
            return None
        if event.contract is None:
            return None
        import time
        self._call_times.append(time.time())
        c = event.contract
        prompt = (
            f"Unusual options flow detected:\n"
            f"Underlying: {event.symbol} | Strike: {c.strike_price} | Expiry: {c.expiration_date}\n"
            f"Type: {event.flow_type} | Volume/OI: {event.volume_oi_ratio:.1f}x | "
            f"Premium est: ${event.premium_estimate:,.0f}\n"
            f"Cluster score: {event.cluster_score:.0f}/100 | Urgency: {event.urgency_score:.0f}/100\n\n"
            f"Provide a one-line assessment: is this likely directional, hedging, or noise? "
            f"NO trade advice, price targets, or buy/sell calls."
        )
        try:
            from pydantic import BaseModel

            class FlowAssessment(BaseModel):
                assessment: str = ""

            response = await gateway.complete_structured(prompt, "You assess options flow. Be concise and factual.", FlowAssessment, max_tokens=150)
            return {"assessment": response.parsed.assessment, "enriched_at": time.time()}
        except Exception as exc:
            log.warning("flow_enrich_failed", symbol=event.symbol, error=type(exc).__name__)
            return None
