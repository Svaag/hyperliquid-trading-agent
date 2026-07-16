from __future__ import annotations

import hashlib
from typing import Any, Protocol

from hyperliquid_trading_agent.app.engine.event_ledger import now_ms
from hyperliquid_trading_agent.app.engine.schemas import (
    AlphaCandidate,
    ExecutionCostQuote,
    ExecutionReport,
    OrderIntent,
)


class ExecutionAdapter(Protocol):
    async def submit(self, intent: OrderIntent, quote: ExecutionCostQuote | None = None) -> ExecutionReport: ...


class ExecutionCostService:
    """Venue-, fee-tier-, size-, and order-book-aware fill simulation."""

    simulation_model_version = "depth_walk_v1"

    def __init__(self, *, settings: Any, repository: Any | None = None, hyperliquid: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self.max_book_age_ms = int(getattr(settings, "engine_execution_book_max_age_ms", 15_000))
        self.fee_cache_ttl_ms = max(
            0,
            int(getattr(settings, "engine_execution_fee_cache_ttl_ms", 300_000)),
        )
        self.latency_slippage_bps = max(0.0, float(getattr(settings, "engine_execution_latency_slippage_bps", 0.0)))
        self._hyperliquid_fee_cache: tuple[float, str, int] | None = None

    async def quote_candidate(
        self,
        candidate: AlphaCandidate,
        *,
        requested_size: float,
        requested_notional_usd: float,
        order_book: dict[str, Any] | Any | None,
        created_at_ms: int | None = None,
    ) -> ExecutionCostQuote:
        timestamp = int(created_at_ms or now_ms())
        side = "buy" if candidate.side == "long" else "sell"
        venue_id = str(candidate.venue_id or candidate.venue).lower()
        levels, book_snapshot_id, book_as_of_ms, depth_reason = _book_levels(order_book, side=side)
        fee_bps, fee_schedule_id, fee_measured = await self._fee_schedule(
            venue_id,
            order_book,
            timestamp_ms=timestamp,
        )
        reason_codes: list[str] = []
        if depth_reason:
            reason_codes.append(depth_reason)
        if fee_bps is None:
            reason_codes.append("fee_schedule_unavailable")
        supported = venue_id.startswith("hyperliquid") or venue_id.startswith("lighter")
        if not supported:
            reason_codes.append("venue_depth_simulation_unsupported")
        if book_as_of_ms is None:
            reason_codes.append("order_book_timestamp_unavailable")
        if book_snapshot_id is None:
            reason_codes.append("order_book_snapshot_id_unavailable")
        stale = book_as_of_ms is not None and timestamp - book_as_of_ms > self.max_book_age_ms
        if stale:
            reason_codes.append("order_book_stale")

        reference = float(candidate.proposed_entry)
        best_bid, best_ask = _best_prices(order_book)
        if best_bid is None or best_ask is None:
            reason_codes.append("two_sided_order_book_unavailable")
        if best_bid and best_ask:
            reference = (best_bid + best_ask) / 2.0
        elif side == "buy" and best_ask:
            reference = best_ask
        elif side == "sell" and best_bid:
            reference = best_bid

        filled_size, avg_fill_px = _walk_depth(levels, requested_size)
        full_depth = filled_size >= requested_size - 1e-12 and requested_size > 0
        if levels and not full_depth:
            reason_codes.append("insufficient_visible_depth")
        if not levels:
            filled_size = 0.0
            avg_fill_px = None

        spread_cost = 0.0
        impact = 0.0
        if best_bid and best_ask and reference > 0:
            spread_cost = max(0.0, (best_ask - best_bid) / (2.0 * reference) * 10_000.0)
        best_touch = best_ask if side == "buy" else best_bid
        if avg_fill_px and best_touch and best_touch > 0:
            direction = 1.0 if side == "buy" else -1.0
            impact = max(0.0, ((avg_fill_px / best_touch) - 1.0) * 10_000.0 * direction)
        slippage = spread_cost + impact

        if (
            not supported
            or not levels
            or fee_bps is None
            or book_as_of_ms is None
            or book_snapshot_id is None
            or best_bid is None
            or best_ask is None
        ):
            quality = "unavailable"
        elif stale:
            quality = "stale"
        elif len(levels) == 1:
            quality = "top_of_book_only"
        elif fee_measured and full_depth:
            quality = "measured"
        else:
            quality = "configured_ceiling"
        total_cost = (fee_bps or 0.0) + spread_cost + impact + self.latency_slippage_bps
        digest_input = (
            f"{candidate.candidate_id}:{venue_id}:{side}:{requested_size:.12g}:"
            f"{book_snapshot_id}:{book_as_of_ms}:{self.simulation_model_version}"
        )
        quote = ExecutionCostQuote(
            quote_id="ecq_" + hashlib.sha1(digest_input.encode()).hexdigest()[:24],
            candidate_id=candidate.candidate_id,
            venue_id=venue_id,
            instrument_id=candidate.instrument_id,
            side=side,  # type: ignore[arg-type]
            requested_size=requested_size,
            requested_notional_usd=requested_notional_usd,
            reference_price=reference,
            simulated_fill_size=filled_size,
            simulated_avg_fill_px=avg_fill_px,
            fee_bps=fee_bps or 0.0,
            spread_cost_bps=spread_cost,
            slippage_bps=slippage,
            market_impact_bps=impact,
            latency_slippage_bps=self.latency_slippage_bps,
            total_execution_cost_bps=total_cost,
            cost_quality=quality,  # type: ignore[arg-type]
            book_snapshot_id=book_snapshot_id,
            fee_schedule_id=fee_schedule_id,
            simulation_model_version=self.simulation_model_version,
            book_as_of_ms=book_as_of_ms,
            created_at_ms=timestamp,
            reason_codes=reason_codes,
            metadata={
                "visible_level_count": len(levels),
                "full_visible_fill": full_depth,
                "fee_tier_measured": fee_measured,
                "book_max_age_ms": self.max_book_age_ms,
                "promotion_eligible_cost": quality == "measured",
            },
        )
        if self.repository is not None and getattr(self.repository, "enabled", False):
            persist = getattr(self.repository, "record_execution_cost_quote", None)
            if callable(persist):
                await persist(quote.model_dump(mode="json"))
        return quote

    async def _fee_schedule(
        self,
        venue_id: str,
        order_book: Any | None,
        *,
        timestamp_ms: int,
    ) -> tuple[float | None, str | None, bool]:
        if venue_id.startswith("hyperliquid"):
            if self._hyperliquid_fee_cache is not None:
                cached_bps, cached_schedule, cached_at_ms = self._hyperliquid_fee_cache
                if 0 <= timestamp_ms - cached_at_ms <= self.fee_cache_ttl_ms:
                    return cached_bps, cached_schedule, True
            address = str(getattr(self.settings, "engine_execution_fee_account_address", "") or "").strip()
            fetch = getattr(self.hyperliquid, "user_fees", None)
            if address and callable(fetch):
                try:
                    payload = await fetch(address)
                    fee_rate = _first_numeric(payload, "userCrossRate", "user_cross_rate", "takerRate", "taker_rate")
                    if fee_rate is not None and fee_rate >= 0:
                        bps = _rate_to_bps(fee_rate)
                        schedule = "hyperliquid:user_fees:" + hashlib.sha256(address.lower().encode()).hexdigest()[:12]
                        self._hyperliquid_fee_cache = (bps, schedule, timestamp_ms)
                        return bps, schedule, True
                except Exception:
                    pass
            configured = float(getattr(self.settings, "engine_execution_hyperliquid_taker_fee_bps", -1.0))
            if configured >= 0:
                return configured, "hyperliquid:configured_account_tier", False
            ceiling = float(getattr(self.settings, "autonomy_paper_taker_fee_bps", -1.0))
            return (ceiling, "hyperliquid:configured_ceiling", False) if ceiling >= 0 else (None, None, False)
        if venue_id.startswith("lighter"):
            dump = getattr(order_book, "model_dump", None)
            payload = dump(mode="json") if callable(dump) else order_book if isinstance(order_book, dict) else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            taker_rate = _first_numeric(metadata, "taker_fee", "takerFee", "taker_rate")
            if taker_rate is not None and taker_rate >= 0:
                return _rate_to_bps(taker_rate), "lighter:instrument_market_tier", True
            configured = float(getattr(self.settings, "engine_execution_lighter_taker_fee_bps", -1.0))
            if configured >= 0:
                return configured, "lighter:configured_market_tier", False
        return None, None, False


class PaperAdapter:
    def __init__(self, *, taker_fee_bps: float = 4.5, default_slippage_bps: float = 2.0):
        self.taker_fee_bps = taker_fee_bps
        self.default_slippage_bps = default_slippage_bps

    async def submit(self, intent: OrderIntent, quote: ExecutionCostQuote | None = None) -> ExecutionReport:
        if quote is None:
            return self._legacy_report(intent)
        return _report_from_quote(intent, quote, execution_mode="paper")

    def _legacy_report(self, intent: OrderIntent) -> ExecutionReport:
        slippage = min(intent.max_slippage_bps, self.default_slippage_bps)
        fill_px = intent.price_limit or (intent.target_notional_usd / intent.target_size)
        fill_px *= 1 + slippage / 10_000 if intent.side == "buy" else 1 - slippage / 10_000
        fees = intent.target_notional_usd * self.taker_fee_bps / 10_000
        digest = hashlib.sha1(f"paper:{intent.intent_id}".encode()).hexdigest()[:24]
        return ExecutionReport(
            report_id="er_" + digest,
            intent_id=intent.intent_id,
            execution_mode="paper",
            status="filled",
            requested_size=intent.target_size,
            filled_size=intent.target_size,
            avg_fill_px=fill_px,
            fees_usd=fees,
            slippage_bps=slippage,
            market_impact_bps=0.0,
            fee_bps=self.taker_fee_bps,
            total_execution_cost_bps=self.taker_fee_bps + slippage,
            cost_quality="configured_ceiling",
            adapter="paper",
            assumptions={"fill_model": "legacy_instant_marketable_limit", "live_exchange_actions": False},
            created_at_ms=now_ms(),
        )


class ShadowAdapter:
    async def submit(self, intent: OrderIntent, quote: ExecutionCostQuote | None = None) -> ExecutionReport:
        if quote is not None:
            return _report_from_quote(intent, quote, execution_mode="shadow")
        digest = hashlib.sha1(f"shadow:{intent.intent_id}".encode()).hexdigest()[:24]
        return ExecutionReport(
            report_id="er_" + digest,
            intent_id=intent.intent_id,
            execution_mode="shadow",
            status="accepted",
            requested_size=intent.target_size,
            filled_size=0.0,
            avg_fill_px=None,
            fees_usd=0.0,
            slippage_bps=0.0,
            market_impact_bps=None,
            adapter="shadow",
            assumptions={
                "shadow_only": True,
                "would_submit": intent.model_dump(mode="json"),
                "live_exchange_actions": False,
            },
            created_at_ms=now_ms(),
        )


class ExecutionGateway:
    def __init__(
        self, *, repository=None, paper_adapter: PaperAdapter | None = None, shadow_adapter: ShadowAdapter | None = None
    ):
        self.repository = repository
        self.paper_adapter = paper_adapter or PaperAdapter()
        self.shadow_adapter = shadow_adapter or ShadowAdapter()

    async def submit(self, intent: OrderIntent, quote: ExecutionCostQuote | None = None) -> ExecutionReport:
        adapter: ExecutionAdapter = self.paper_adapter if intent.execution_mode == "paper" else self.shadow_adapter
        report = await adapter.submit(intent, quote)
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record_intent = getattr(self.repository, "record_order_intent", None)
            record_report = getattr(self.repository, "record_execution_report", None)
            if callable(record_intent):
                await record_intent(intent.model_dump(mode="json"))
            if callable(record_report):
                await record_report(report.model_dump(mode="json"))
        return report


def _report_from_quote(intent: OrderIntent, quote: ExecutionCostQuote, *, execution_mode: str) -> ExecutionReport:
    if quote.cost_quality in {"unavailable", "stale"} or quote.simulated_fill_size <= 0:
        status = "rejected"
        filled_size = 0.0
        avg_fill_px = None
    elif quote.simulated_fill_size >= intent.target_size - 1e-12:
        status = "filled"
        filled_size = intent.target_size
        avg_fill_px = quote.simulated_avg_fill_px
    else:
        status = "partial"
        filled_size = quote.simulated_fill_size
        avg_fill_px = quote.simulated_avg_fill_px
    filled_notional = filled_size * (avg_fill_px or quote.reference_price)
    digest = hashlib.sha1(f"{execution_mode}:{intent.intent_id}:{quote.quote_id}".encode()).hexdigest()[:24]
    return ExecutionReport(
        report_id="er_" + digest,
        intent_id=intent.intent_id,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        requested_size=intent.target_size,
        filled_size=filled_size,
        avg_fill_px=avg_fill_px,
        fees_usd=filled_notional * quote.fee_bps / 10_000.0,
        slippage_bps=quote.slippage_bps,
        market_impact_bps=quote.market_impact_bps,
        execution_cost_quote_id=quote.quote_id,
        fee_bps=quote.fee_bps,
        spread_cost_bps=quote.spread_cost_bps,
        latency_slippage_bps=quote.latency_slippage_bps,
        total_execution_cost_bps=quote.total_execution_cost_bps,
        book_snapshot_id=quote.book_snapshot_id,
        fee_schedule_id=quote.fee_schedule_id,
        simulation_model_version=quote.simulation_model_version,
        cost_quality=quote.cost_quality,
        adapter=execution_mode,  # type: ignore[arg-type]
        assumptions={
            "fill_model": quote.simulation_model_version,
            "hypothetical": execution_mode == "shadow",
            "live_exchange_actions": False,
            "reason_codes": quote.reason_codes,
        },
        created_at_ms=now_ms(),
        metadata={"candidate_id": quote.candidate_id, "promotion_eligible_cost": quote.cost_quality == "measured"},
    )


def _book_levels(
    order_book: Any | None, *, side: str
) -> tuple[list[tuple[float, float]], str | None, int | None, str | None]:
    if order_book is None:
        return [], None, None, "order_book_unavailable"
    if hasattr(order_book, "model_dump"):
        row = order_book.model_dump(mode="json")
    elif isinstance(order_book, dict):
        row = order_book
    else:
        return [], None, None, "order_book_invalid"
    snapshot_id = str(row.get("snapshot_id")) if row.get("snapshot_id") else None
    book_as_of_ms = _int_or_none(row.get("exchange_ts_ms") or row.get("received_ts_ms") or row.get("time"))
    depth = row.get("depth_bands") if isinstance(row.get("depth_bands"), dict) else row
    raw_levels: Any = depth.get("asks" if side == "buy" else "bids") if isinstance(depth, dict) else None
    if raw_levels is None and isinstance(row.get("levels"), list) and len(row["levels"]) >= 2:
        raw_levels = row["levels"][1 if side == "buy" else 0]
    levels: list[tuple[float, float]] = []
    for item in raw_levels or []:
        if isinstance(item, dict):
            px = _float(item.get("px") or item.get("price"))
            size = _float(item.get("size") or item.get("sz") or item.get("remaining_base_amount"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            px, size = _float(item[0]), _float(item[1])
        else:
            continue
        if px and size and px > 0 and size > 0:
            levels.append((px, size))
    levels.sort(key=lambda item: item[0], reverse=side == "sell")
    return levels, snapshot_id, book_as_of_ms, None if levels else "order_book_depth_unavailable"


def _best_prices(order_book: Any | None) -> tuple[float | None, float | None]:
    bids, _, _, _ = _book_levels(order_book, side="sell")
    asks, _, _, _ = _book_levels(order_book, side="buy")
    return (bids[0][0] if bids else None, asks[0][0] if asks else None)


def _walk_depth(levels: list[tuple[float, float]], requested_size: float) -> tuple[float, float | None]:
    remaining = max(0.0, float(requested_size))
    filled = 0.0
    notional = 0.0
    for px, available in levels:
        take = min(remaining, available)
        filled += take
        notional += take * px
        remaining -= take
        if remaining <= 1e-12:
            break
    return filled, notional / filled if filled > 0 else None


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _first_numeric(payload: Any, *keys: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = _float(payload.get(key))
        if value is not None:
            return value
    for value in payload.values():
        if isinstance(value, dict):
            nested = _first_numeric(value, *keys)
            if nested is not None:
                return nested
    return None


def _rate_to_bps(value: float) -> float:
    return value * 10_000.0 if value <= 0.1 else value
