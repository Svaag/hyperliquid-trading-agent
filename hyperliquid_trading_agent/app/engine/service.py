from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.engine.attribution import CandidateOutcomeAttributionService
from hyperliquid_trading_agent.app.engine.candidate_book import CandidateBook
from hyperliquid_trading_agent.app.engine.council import (
    DeterministicCouncil,
    build_candidate_trade_packet,
    council_allows_execution,
)
from hyperliquid_trading_agent.app.engine.debate_adjudicator import (
    DebateAdjudicator,
    EvidencePackBuilder,
    debate_priority,
)
from hyperliquid_trading_agent.app.engine.diversity import PortfolioDiversityController
from hyperliquid_trading_agent.app.engine.event_ledger import EventLedger, now_ms
from hyperliquid_trading_agent.app.engine.execution import ExecutionGateway
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.portfolio_allocator import PortfolioAllocator
from hyperliquid_trading_agent.app.engine.position_manager import PositionManager
from hyperliquid_trading_agent.app.engine.regime import RegimeEngine
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, OrderIntent
from hyperliquid_trading_agent.app.engine.scorer import EVScorerService
from hyperliquid_trading_agent.app.engine.strategy_registry import create_default_strategy_registry
from hyperliquid_trading_agent.app.engine.strategy_selector import ConservativeStrategySelector
from hyperliquid_trading_agent.app.engine.throttles import StrategyThrottleController
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway


class InstitutionalEngineService:
    """Canonical paper/shadow institutional engine orchestrator.

    This service is live-order inert. It can submit only to PaperAdapter/ShadowAdapter
    through ExecutionGateway and uses RiskGateway before every OrderIntent.
    """

    def __init__(
        self,
        *,
        settings: Any,
        repository: Any | None,
        hyperliquid: Any,
        risk_gateway: RiskGateway,
        portfolio_service: Any | None = None,
        world_model_service: Any | None = None,
        liquidation_bridge: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self.risk_gateway = risk_gateway
        self.portfolio_service = portfolio_service
        self.world_model_service = world_model_service
        self.liquidation_bridge = liquidation_bridge
        self.ledger = EventLedger(repository)
        self.feature_store = FeatureStore(repository)
        self.regime_engine = RegimeEngine(
            news_catalyst_threshold=getattr(settings, "engine_news_catalyst_threshold", 0.35),
            news_catalyst_ttl_ms=int(getattr(settings, "engine_news_catalyst_ttl_seconds", 3600)) * 1000,
        )
        self.candidate_book = CandidateBook(repository)
        self.scorer = EVScorerService(repository)
        self.allocator = PortfolioAllocator(
            min_net_ev_bps=settings.engine_min_net_ev_bps,
            min_risk_adjusted_utility=settings.engine_min_risk_adjusted_utility,
            max_single_name_exposure_pct=settings.autonomy_paper_max_single_name_exposure_pct,
            risk_pct_per_trade=settings.autonomy_paper_risk_pct_per_trade,
            repository=repository,
        )
        self.pack_builder = EvidencePackBuilder()
        self.council = DeterministicCouncil()
        self.debate = DebateAdjudicator(repository)
        self.execution = ExecutionGateway(repository=repository)
        self.positions = PositionManager(repository)
        self.candidate_outcomes = CandidateOutcomeAttributionService(repository)
        self.diversity = PortfolioDiversityController(settings)
        self.throttles = StrategyThrottleController(settings)
        self.strategy_selector = ConservativeStrategySelector()
        self.strategy_registry = create_default_strategy_registry(enable_wave_1c=bool(getattr(settings, "engine_wave1c_enabled", False)))
        self.strategies = self.strategy_registry.strategies(enabled_only=True)
        self.last_run_at_ms: int | None = None
        self.last_error: str | None = None
        self.run_count = 0
        self.candidates_created = 0
        self.order_intents_created = 0
        self.execution_reports_created = 0
        self.debate_count_today = 0
        self.council_reviews_created = 0
        self.last_throttle_summary: dict[str, Any] = {}
        self.last_diversity_summary: dict[str, Any] = {}
        self.last_strategy_selection_summary: dict[str, Any] = {}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_enabled,
            "mode": self.settings.engine_mode,
            "last_run_at_ms": self.last_run_at_ms,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "candidates_created": self.candidates_created,
            "order_intents_created": self.order_intents_created,
            "execution_reports_created": self.execution_reports_created,
            "debate_count_today": self.debate_count_today,
            "council_reviews_created": self.council_reviews_created,
            "last_throttle_summary": self.last_throttle_summary,
            "last_strategy_selection_summary": self.last_strategy_selection_summary,
            "throttles": self.throttles.status(),
            "diversity": {**self.diversity.status(), "last_summary": self.last_diversity_summary},
            "strategy_registry": self.strategy_registry.metadata(),
            "wave_policy": {
                "wave1c_enabled": bool(getattr(self.settings, "engine_wave1c_enabled", False)),
                "wave2_enabled": bool(getattr(self.settings, "engine_wave2_enabled", False)),
                "wave2_status": "deferred_until_wave1_evidence_replay_readiness",
            },
        }

    async def run_once(self, *, symbols: list[str] | None = None) -> dict[str, Any]:
        ts = now_ms()
        symbols = [symbol.upper() for symbol in (symbols or self.settings.autonomy_core_symbols or ["BTC", "ETH", "HYPE"])]
        try:
            mids = await self._safe_all_mids()
            selected_mids = {symbol: mids[symbol] for symbol in symbols if symbol in mids}
            if selected_mids:
                event = await self.ledger.normalize_and_record(
                    event_type="all_mids",
                    source="hyperliquid",
                    provider="rest_or_ws_cache",
                    payload=selected_mids,
                    asset_class="crypto",
                    symbols=list(selected_mids.keys()),
                    received_ts_ms=ts,
                )
                await self.feature_store.features_for_event(event)
            await self._record_meta_and_asset_ctx_features(symbols=symbols, received_ts_ms=ts)
            # Add L2 for a bounded hot set to produce execution/microstructure features.
            for symbol in symbols[: max(1, self.settings.autonomy_max_hot_l2_assets)]:
                try:
                    book = await self.hyperliquid.l2_book(symbol)
                except Exception:
                    continue
                event = await self.ledger.normalize_and_record(
                    event_type="l2_book",
                    source="hyperliquid",
                    provider="rest",
                    payload={"coin": symbol, **(book if isinstance(book, dict) else {"raw": book})},
                    asset_class="crypto",
                    symbols=[symbol],
                    received_ts_ms=now_ms(),
                )
                await self.feature_store.features_for_event(event)

            all_candidates: list[AlphaCandidate] = []
            estimates = {}
            allocations = {}
            executed = []
            current_loop_allocations = []
            throttle_events = []
            diversity_decisions = []
            strategy_selection_summaries: dict[str, Any] = {}
            for symbol in symbols:
                await self._record_world_model_features(symbol)
                await self._record_liquidation_features(symbol)
                features = await self.feature_store.latest(asset=symbol, limit=200)
                if not features:
                    continue
                regime = self.regime_engine.compute(features, primary_asset=symbol)
                await self._persist_regime(regime)
                snapshot = self.feature_store.snapshot(asset=symbol)
                symbol_candidates = []
                selection = self.strategy_selector.select(self.strategies, regime)
                strategy_selection_summaries[symbol] = selection.summary()
                for strategy in selection.strategies:
                    await self._prepare_strategy(strategy, timestamp_ms=ts)
                    symbol_candidates.extend(strategy.generate(snapshot, regime, timestamp_ms=ts))
                symbol_candidates = symbol_candidates[: self.settings.engine_max_candidates_per_loop]
                symbol_candidates, symbol_throttle_events = await self.throttles.filter_candidates(symbol_candidates, repository=self.repository, timestamp_ms=ts)
                throttle_events.extend(symbol_throttle_events)
                all_candidates.extend(symbol_candidates)
                await self.candidate_book.add_many(symbol_candidates)
                for candidate in symbol_candidates:
                    ev = await self.scorer.score(candidate, regime)
                    estimates[candidate.candidate_id] = ev
                    allocation = await self.allocator.allocate(candidate, ev, regime=regime, portfolio_state=self._portfolio_snapshot())
                    allocation = self._enrich_allocation_metadata(allocation, candidate)
                    candidate_risk = await self._candidate_risk_precheck(candidate, ev, snapshot_features=snapshot.features, timestamp_ms=ts)
                    if candidate_risk is not None and not candidate_risk.allowed:
                        allocation = allocation.model_copy(
                            update={
                                "status": "risk_rejected",
                                "allocated_size": 0.0,
                                "allocated_notional_usd": 0.0,
                                "risk_usd": 0.0,
                                "reason_codes": [*allocation.reason_codes, "candidate_risk_gateway_reject"],
                            }
                        )
                    allowed_by_throttle, throttle_reasons, throttle_metadata = await self.throttles.allow_allocation(candidate, current_loop_allocations=current_loop_allocations, repository=self.repository, timestamp_ms=ts)
                    if not allowed_by_throttle:
                        allocation = allocation.model_copy(
                            update={
                                "status": "skip",
                                "allocated_size": 0.0,
                                "allocated_notional_usd": 0.0,
                                "risk_usd": 0.0,
                                "reason_codes": [*allocation.reason_codes, *throttle_reasons],
                                "metadata": {**allocation.metadata, **throttle_metadata},
                            }
                        )
                    allocation = await self.diversity.apply(candidate, allocation, current_loop_allocations=current_loop_allocations, repository=self.repository, timestamp_ms=ts)
                    diversity_decisions.append(allocation.metadata.get("diversity", {}))
                    allocations[candidate.candidate_id] = allocation
                    await self._persist_allocation(allocation)

                    replay_context = await self._latest_replay_context()
                    intent = None
                    order_risk = None
                    if allocation.status in {"allocate", "reduce", "require_debate"}:
                        intent = self._order_intent(candidate, ev.model_version_id, allocation.allocation_id, allocation.allocated_size, allocation.allocated_notional_usd, ts)
                        order_risk = await self.risk_gateway.check_order_intent(
                            intent,
                            market_snapshot={"last_price_at_ms": ts, "last_orderbook_at_ms": ts, "spread_bps": snapshot.features.get("spread_bps")},
                            portfolio_snapshot=self._portfolio_snapshot(),
                            strategy_snapshot={"net_ev_bps": ev.net_ev_bps, "regime_permission": True},
                            operator_context={"kill_switch_active": False, "config_approved": True, "model_approved": ev.model_version_id == "deterministic_fallback_v1" or bool(self.settings.engine_approved_scorer_model_id)},
                        )
                        if not order_risk.allowed:
                            allocation = allocation.model_copy(update={"status": "risk_rejected", "allocated_size": 0.0, "allocated_notional_usd": 0.0, "risk_usd": 0.0, "reason_codes": [*allocation.reason_codes, "risk_gateway_reject"]})
                            allocation = self._enrich_allocation_metadata(allocation, candidate)
                            allocations[candidate.candidate_id] = allocation
                            await self._persist_allocation(allocation)
                    packet_risk = order_risk or candidate_risk or {"decision_id": None, "decision": "not_applicable", "allowed": True, "violations": []}
                    packet = build_candidate_trade_packet(candidate=candidate, ev=ev, allocation=allocation, order_intent=intent, risk_decision=packet_risk, replay_context=replay_context, created_at_ms=ts)
                    await self._persist_candidate_trade_packet(packet)
                    council_review = self.council.review(packet, regime)
                    self.council_reviews_created += 1
                    await self._persist_council_review(council_review)
                    await self.candidate_outcomes.record_candidate_evidence(
                        candidate=candidate,
                        allocation=allocation,
                        ev=ev,
                        risk_decision=candidate_risk or order_risk,
                        council_review=council_review,
                        packet=packet,
                        replay_context=replay_context,
                        created_at_ms=ts,
                    )
                    if allocation.status not in {"allocate", "reduce", "require_debate"}:
                        continue
                    current_loop_allocations.append(allocation)
                    risk = order_risk
                    if risk is None or not risk.allowed:
                        allocation = allocation.model_copy(update={"status": "risk_rejected", "allocated_size": 0.0, "allocated_notional_usd": 0.0, "risk_usd": 0.0, "reason_codes": [*allocation.reason_codes, "risk_gateway_reject"]})
                        allocation = self._enrich_allocation_metadata(allocation, candidate)
                        current_loop_allocations.pop()
                        await self._persist_allocation(allocation)
                        continue
                    if intent is None or not council_allows_execution(council_review, execution_mode=intent.execution_mode):
                        allocation = allocation.model_copy(update={"status": "skip", "allocated_size": 0.0, "allocated_notional_usd": 0.0, "risk_usd": 0.0, "reason_codes": [*allocation.reason_codes, f"council_{council_review.decision}"]})
                        allocation = self._enrich_allocation_metadata(allocation, candidate)
                        current_loop_allocations.pop()
                        await self._persist_allocation(allocation)
                        continue
                    priority = debate_priority(candidate, ev, allocation, regime, portfolio_equity=float(self._portfolio_snapshot().get("equity_usd") or 100_000))
                    if self.settings.engine_debate_enabled and self.debate_count_today < self.settings.engine_debate_max_per_day and priority >= self.settings.engine_debate_priority_min:
                        pack = self.pack_builder.build(candidate, ev, allocation, regime, feature_snapshot=snapshot.features)
                        await self._persist_evidence_pack(pack)
                        debate = await self.debate.adjudicate_fallback(pack)
                        self.debate_count_today += 1
                        if debate.decision == "block":
                            allocation = allocation.model_copy(
                                update={
                                    "status": "skip",
                                    "allocated_size": 0.0,
                                    "allocated_notional_usd": 0.0,
                                    "risk_usd": 0.0,
                                    "reason_codes": [*allocation.reason_codes, "debate_blocked"],
                                }
                            )
                            allocation = self._enrich_allocation_metadata(allocation, candidate)
                            current_loop_allocations.pop()
                            await self._persist_allocation(allocation)
                            continue
                        allocation = allocation.model_copy(update={"allocated_size": allocation.allocated_size * debate.max_size_multiplier, "allocated_notional_usd": allocation.allocated_notional_usd * debate.max_size_multiplier, "risk_usd": allocation.risk_usd * debate.max_size_multiplier})
                        allocation = self._enrich_allocation_metadata(allocation, candidate)
                        current_loop_allocations[-1] = allocation
                        await self._persist_allocation(allocation)
                        if allocation.allocated_size <= 0:
                            continue
                        intent = self._order_intent(candidate, ev.model_version_id, allocation.allocation_id, allocation.allocated_size, allocation.allocated_notional_usd, ts)
                    report = await self.execution.submit(intent)
                    self.order_intents_created += 1
                    self.execution_reports_created += 1
                    executed.append(report)
                    await self.positions.open_from_execution(candidate, report)
            book = await self.candidate_book.snapshot(estimates, as_of_ms=ts)
            self.candidates_created += len(all_candidates)
            self.last_throttle_summary = self._throttle_summary(throttle_events=throttle_events, timestamp_ms=ts)
            self.last_diversity_summary = self._diversity_summary(diversity_decisions=diversity_decisions, timestamp_ms=ts)
            self.last_strategy_selection_summary = {"timestamp_ms": ts, "symbols": strategy_selection_summaries}
            self.last_run_at_ms = ts
            self.run_count += 1
            return {"candidate_book_id": book.candidate_book_id, "candidates": len(all_candidates), "executed": len(executed), "throttle_events": len(throttle_events), "throttle_summary": self.last_throttle_summary, "diversity_summary": self.last_diversity_summary}
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise

    def _throttle_summary(self, *, throttle_events: list[dict[str, Any]], timestamp_ms: int) -> dict[str, Any]:
        by_reason: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        for event in throttle_events:
            reason = str(event.get("reason") or "unknown")
            strategy = str(event.get("strategy_id") or "unknown")
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        return {
            "timestamp_ms": timestamp_ms,
            "event_count": len(throttle_events),
            "by_reason": by_reason,
            "by_strategy": by_strategy,
            "controller": self.throttles.status(),
        }

    def _diversity_summary(self, *, diversity_decisions: list[dict[str, Any]], timestamp_ms: int) -> dict[str, Any]:
        by_decision: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        for item in diversity_decisions:
            decision = str(item.get("decision") or "unknown")
            by_decision[decision] = by_decision.get(decision, 0) + 1
            for reason in item.get("reason_codes") or []:
                by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
        return {
            "timestamp_ms": timestamp_ms,
            "decision_count": len(diversity_decisions),
            "by_decision": by_decision,
            "by_reason": by_reason,
            "controller": self.diversity.status(),
        }

    def _enrich_allocation_metadata(self, allocation, candidate: AlphaCandidate):
        return allocation.model_copy(
            update={
                "metadata": {
                    **allocation.metadata,
                    "strategy_id": candidate.strategy_id,
                    "strategy_version": candidate.strategy_version,
                    "strategy_family": candidate.strategy_family,
                    "asset": candidate.asset,
                    "venue": candidate.venue,
                    "counts_for_breadth": candidate.counts_for_breadth,
                    "regime_snapshot_id": candidate.regime_snapshot_id,
                    "feature_coverage_pct": candidate.feature_coverage_pct,
                }
            }
        )

    async def _safe_all_mids(self) -> dict[str, float]:
        try:
            raw = await self.hyperliquid.all_mids()
            out = {}
            for key, value in raw.items():
                try:
                    out[str(key).upper()] = float(value)
                except (TypeError, ValueError):
                    continue
            return out
        except Exception:
            return {}

    async def _prepare_strategy(self, strategy: Any, *, timestamp_ms: int) -> None:
        refresh = getattr(strategy, "refresh_from_repository", None)
        if callable(refresh):
            await refresh(self.repository, now_ms=timestamp_ms)

    async def _candidate_risk_precheck(self, candidate: AlphaCandidate, ev: Any, *, snapshot_features: dict[str, Any], timestamp_ms: int) -> Any | None:
        if candidate.side == "flat":
            return None
        notional = max(1.0, min(10.0, float(candidate.proposed_entry) * 0.001))
        size = notional / max(float(candidate.proposed_entry), 1e-9)
        intent = self._order_intent(candidate, ev.model_version_id, f"precheck_{candidate.candidate_id}", size, notional, timestamp_ms)
        intent = intent.model_copy(update={"intent_id": f"precheck_{candidate.candidate_id}"})
        return await self.risk_gateway.check_order_intent(
            intent,
            market_snapshot={"last_price_at_ms": timestamp_ms, "last_orderbook_at_ms": timestamp_ms, "spread_bps": snapshot_features.get("spread_bps")},
            portfolio_snapshot=self._portfolio_snapshot(),
            strategy_snapshot={"net_ev_bps": ev.net_ev_bps, "regime_permission": True, "candidate_level_precheck": True},
            operator_context={"kill_switch_active": False, "config_approved": True, "model_approved": ev.model_version_id == "deterministic_fallback_v1" or bool(self.settings.engine_approved_scorer_model_id)},
        )

    async def _record_meta_and_asset_ctx_features(self, *, symbols: list[str], received_ts_ms: int) -> None:
        fetch = getattr(self.hyperliquid, "meta_and_asset_ctxs", None)
        if not callable(fetch):
            return
        try:
            raw = await fetch()
        except Exception:
            return
        payload = _meta_and_asset_payload(raw)
        event = await self.ledger.normalize_and_record(
            event_type="meta_and_asset_ctxs",
            source="hyperliquid",
            provider="rest",
            payload=payload,
            asset_class="crypto",
            symbols=symbols,
            received_ts_ms=received_ts_ms,
            metadata={"read_only": True},
        )
        await self.feature_store.features_for_event(event)

    async def _record_liquidation_features(self, symbol: str) -> None:
        if self.liquidation_bridge is None:
            return
        named_signals = getattr(self.liquidation_bridge, "named_signals", None)
        if not callable(named_signals):
            return
        try:
            payload = named_signals(symbol)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        ts = now_ms()
        event = await self.ledger.normalize_and_record(
            event_type="liquidation_signal",
            source="liquidation_signal_bridge",
            provider=str(payload.get("venue") or "all"),
            payload=payload,
            asset_class="crypto",
            symbols=[symbol],
            event_ts_ms=_int_or_none(payload.get("as_of_ms")),
            received_ts_ms=ts,
            metadata={"read_only": True, "advisory_only": True},
        )
        await self.feature_store.features_for_event(event)

    def _portfolio_snapshot(self) -> dict[str, Any]:
        if self.portfolio_service is not None and callable(getattr(self.portfolio_service, "latest_snapshot", None)):
            snapshot = self.portfolio_service.latest_snapshot()
            if snapshot is not None:
                return snapshot.model_dump(mode="json")
        return {"equity_usd": self.settings.autonomy_paper_initial_equity_usd, "initial_equity_usd": self.settings.autonomy_paper_initial_equity_usd}

    async def _record_world_model_features(self, symbol: str) -> None:
        if self.world_model_service is None:
            return
        snapshot = getattr(self.world_model_service, "snapshot", None)
        if not callable(snapshot):
            return
        try:
            world_snapshot = snapshot(symbols=[symbol], max_beliefs=12)
            await self.feature_store.features_for_world_model_snapshot(asset=symbol, snapshot=world_snapshot)
        except Exception:
            return

    def _order_intent(self, candidate: AlphaCandidate, model_version_id: str, allocation_id: str, size: float, notional: float, ts: int) -> OrderIntent:
        side = "buy" if candidate.side == "long" else "sell"
        mode = "shadow" if self.settings.engine_shadow_enabled and not self.settings.engine_paper_enabled else "paper"
        return OrderIntent(
            intent_id="intent_" + candidate.candidate_id.removeprefix("cand_"),
            parent_candidate_id=candidate.candidate_id,
            portfolio_decision_id=allocation_id,
            asset=candidate.asset,
            asset_class=candidate.asset_class,
            venue=candidate.venue,
            side=side,  # type: ignore[arg-type]
            order_type="marketable_limit",
            time_in_force="ioc",
            target_size=size,
            target_notional_usd=notional,
            max_slippage_bps=10,
            price_limit=candidate.proposed_entry,
            reduce_only=False,
            post_only=False,
            deadline_ts_ms=ts + 60_000,
            strategy_id=candidate.strategy_id,
            model_version_id=model_version_id,
            config_version_id="runtime_settings",
            risk_budget_id="default",
            execution_mode=mode,  # type: ignore[arg-type]
            created_at_ms=ts,
        )

    async def _persist_regime(self, regime) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_regime_snapshot", None)
            if callable(record):
                await record(regime.model_dump(mode="json"))

    async def _persist_evidence_pack(self, pack) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_evidence_pack", None)
            if callable(record):
                await record(pack.model_dump(mode="json"))

    async def _persist_candidate_trade_packet(self, packet) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_candidate_trade_packet", None)
            if callable(record):
                await record(packet.model_dump(mode="json"))

    async def _persist_council_review(self, review) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_council_review", None)
            if callable(record):
                await record(review.model_dump(mode="json"))

    async def _latest_replay_context(self) -> dict[str, Any]:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            try:
                return dict(await latest_engine_replay_comparison(self.repository) or {})
            except Exception:
                return {}
        return {}

    async def _persist_allocation(self, allocation) -> None:
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_allocation_decision", None)
            if callable(record):
                await record(allocation.model_dump(mode="json"))


def _meta_and_asset_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and len(raw) >= 2:
        return {"meta": raw[0], "asset_ctxs": raw[1]}
    return {"raw": raw}


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
