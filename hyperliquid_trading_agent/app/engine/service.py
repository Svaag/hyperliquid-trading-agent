from __future__ import annotations

import hashlib
import inspect
import time
from collections import deque
from typing import Any

from hyperliquid_trading_agent.app.engine.alpha.base import StrategyGenerationTrace, evaluate_strategy
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
from hyperliquid_trading_agent.app.engine.evidence_admission import ShadowEvidenceAdmissionController
from hyperliquid_trading_agent.app.engine.execution import ExecutionGateway
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.portfolio_allocator import PortfolioAllocator
from hyperliquid_trading_agent.app.engine.position_manager import PositionManager
from hyperliquid_trading_agent.app.engine.regime import RegimeEngine
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
from hyperliquid_trading_agent.app.engine.schemas import AlphaCandidate, AssetClass, OrderIntent
from hyperliquid_trading_agent.app.engine.scorer import EVScorerService
from hyperliquid_trading_agent.app.engine.strategy_registry import create_default_strategy_registry
from hyperliquid_trading_agent.app.engine.strategy_selector import ConservativeStrategySelector
from hyperliquid_trading_agent.app.engine.throttles import StrategyThrottleController
from hyperliquid_trading_agent.app.governance.risk_gateway import RiskGateway
from hyperliquid_trading_agent.app.governance.schemas import RiskGatewayDecision


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
        self.feature_store = FeatureStore(
            repository,
            cross_venue_dexes=getattr(settings, "engine_cross_venue_dex_list", []),
            max_age_seconds=int(getattr(settings, "engine_feature_store_max_age_seconds", 7200)),
            funding_max_age_seconds=int(getattr(settings, "engine_feature_store_funding_max_age_seconds", 90000)),
            max_points_per_series=int(getattr(settings, "engine_feature_store_max_points_per_series", 4096)),
            full_universe_enabled=bool(getattr(settings, "engine_feature_full_universe_enabled", False)),
        )
        self.regime_engine = RegimeEngine(
            news_catalyst_threshold=getattr(settings, "engine_news_catalyst_threshold", 0.35),
            news_catalyst_ttl_ms=int(getattr(settings, "engine_news_catalyst_ttl_seconds", 3600)) * 1000,
            news_risk_overlay_mode=getattr(settings, "engine_news_risk_overlay_mode", "shadow"),
        )
        self.candidate_book = CandidateBook(repository)
        self.scorer = EVScorerService(repository)
        self.allocator = PortfolioAllocator(
            min_net_ev_bps=settings.engine_min_net_ev_bps,
            min_risk_adjusted_utility=settings.engine_min_risk_adjusted_utility,
            max_single_name_exposure_pct=settings.autonomy_paper_max_single_name_exposure_pct,
            risk_pct_per_trade=settings.autonomy_paper_risk_pct_per_trade,
            news_risk_overlay_mode=getattr(settings, "engine_news_risk_overlay_mode", "shadow"),
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
        self.evidence_admission = ShadowEvidenceAdmissionController(settings)
        self.strategy_selector = ConservativeStrategySelector()
        self.strategy_registry = create_default_strategy_registry(
            catalog_mode=getattr(settings, "engine_alpha_catalog_mode", "wave1a_locked"),
            enable_wave_1c=bool(getattr(settings, "engine_wave1c_enabled", False)),
            news_event_alpha_mode=getattr(settings, "engine_news_alpha_mode", "off"),
        )
        self.strategies = self.strategy_registry.strategies(enabled_only=True)
        for strategy in self.strategies:
            configure = getattr(strategy, "configure", None)
            if callable(configure):
                configure(settings)
        self.last_run_at_ms: int | None = None
        self.last_run_id: str | None = None
        self.last_run_completed_at_ms: int | None = None
        self.last_successful_run_completed_at_ms: int | None = None
        self.run_in_progress = False
        self.current_run_id: str | None = None
        self.current_run_started_at_ms: int | None = None
        self.last_error: str | None = None
        self.last_run_duration_ms: float | None = None
        self.last_stage_ms: dict[str, float] = {}
        self._recent_run_durations_ms: deque[float] = deque(maxlen=100)
        self.run_count = 0
        self.candidates_created = 0
        self.order_intents_created = 0
        self.execution_reports_created = 0
        self.debate_count_today = 0
        self.council_reviews_created = 0
        self.last_throttle_summary: dict[str, Any] = {}
        self.last_diversity_summary: dict[str, Any] = {}
        self.last_strategy_selection_summary: dict[str, Any] = {}
        self.strategy_specs_persisted = False
        self.strategy_specs_persisted_count = 0

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.engine_enabled,
            "mode": self.settings.engine_mode,
            "last_run_at_ms": self.last_run_at_ms,
            "last_run_id": self.last_run_id,
            "last_run_completed_at_ms": self.last_run_completed_at_ms,
            "last_successful_run_completed_at_ms": self.last_successful_run_completed_at_ms,
            "run_in_progress": self.run_in_progress,
            "current_run_id": self.current_run_id,
            "current_run_started_at_ms": self.current_run_started_at_ms,
            "last_error": self.last_error,
            "last_run_duration_ms": self.last_run_duration_ms,
            "last_stage_ms": self.last_stage_ms,
            "latency_window": _latency_window(list(self._recent_run_durations_ms)),
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
            "shadow_evidence_admission": self.evidence_admission.status(),
            "strategy_registry": self.strategy_registry.metadata(),
            "strategy_catalog": self.strategy_registry.catalog_summary(),
            "strategy_specs_persisted": self.strategy_specs_persisted,
            "strategy_specs_persisted_count": self.strategy_specs_persisted_count,
            "wave_policy": {
                "wave1c_enabled": bool(getattr(self.settings, "engine_wave1c_enabled", False)),
                "wave2_enabled": bool(getattr(self.settings, "engine_wave2_enabled", False)),
                "wave2_status": (
                    "early_shadow_only"
                    if self.strategy_registry.catalog_mode in {"wave2_early_shadow", "shadow_full_catalog"}
                    else "deferred_until_operator_enablement"
                ),
            },
        }

    async def run_once(self, *, symbols: list[str] | None = None) -> dict[str, Any]:
        ts = now_ms()
        symbols = [symbol.upper() for symbol in (symbols or self.settings.autonomy_core_symbols or ["BTC", "ETH", "HYPE"])]
        engine_run_id = "erun_" + hashlib.sha1(
            f"{ts}:{','.join(symbols)}:{self.run_count}".encode()
        ).hexdigest()[:24]
        run_started = time.perf_counter()
        stage_ms: dict[str, float] = {}
        self.run_in_progress = True
        self.current_run_id = engine_run_id
        self.current_run_started_at_ms = ts

        def _stage_done(name: str, started: float) -> None:
            stage_ms[name] = round(stage_ms.get(name, 0.0) + (time.perf_counter() - started) * 1000, 3)

        try:
            await self.persist_strategy_specs()
            stage_started = time.perf_counter()
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
            _stage_done("all_mids", stage_started)
            stage_started = time.perf_counter()
            await self._record_meta_and_asset_ctx_features(symbols=symbols, received_ts_ms=ts)
            _stage_done("meta_asset_ctxs", stage_started)
            stage_started = time.perf_counter()
            await self._record_persisted_venue_market_features(symbols=symbols, received_ts_ms=ts)
            _stage_done("venue_market", stage_started)
            stage_started = time.perf_counter()
            await self._record_persisted_cross_venue_features(symbols=symbols, received_ts_ms=ts)
            _stage_done("cross_venue", stage_started)
            # Add L2 for a bounded hot set to produce execution/microstructure features.
            stage_started = time.perf_counter()
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
            _stage_done("l2_books", stage_started)

            all_candidates: list[AlphaCandidate] = []
            estimates = {}
            allocations = {}
            executed = []
            current_loop_allocations = []
            throttle_events = []
            diversity_decisions = []
            risk_market_snapshot_cache: dict[str, dict[str, Any]] = {}
            strategy_selection_summaries: dict[str, Any] = {}
            stage_started = time.perf_counter()
            allocation_history: list[dict[str, Any]] = []
            shadow_intent_history: list[dict[str, Any]] = []
            if self.repository is not None and getattr(self.repository, "enabled", False):
                list_allocations = getattr(self.repository, "list_allocation_decisions", None)
                if callable(list_allocations):
                    try:
                        allocation_history = list(await list_allocations(limit=5000))
                    except Exception:
                        allocation_history = []
                list_intents = getattr(self.repository, "list_order_intents", None)
                if callable(list_intents):
                    evidence_lookback = max(10, int(getattr(self.settings, "engine_shadow_evidence_lookback_intents", 100)))
                    try:
                        shadow_intent_history = list(await list_intents(execution_mode="shadow", limit=evidence_lookback))
                    except TypeError:
                        try:
                            shadow_intent_history = [
                                item
                                for item in await list_intents(limit=evidence_lookback)
                                if item.get("execution_mode") == "shadow"
                            ]
                        except Exception:
                            shadow_intent_history = []
                    except Exception:
                        shadow_intent_history = []
            self.throttles.prime_allocation_history(allocation_history, timestamp_ms=ts)
            replay_context = await self._latest_replay_context()
            _stage_done("candidate_context", stage_started)
            for symbol in symbols:
                stage_started = time.perf_counter()
                await self._record_world_model_features(symbol)
                _stage_done("world_model", stage_started)
                stage_started = time.perf_counter()
                await self._record_liquidation_features(symbol)
                _stage_done("liquidations", stage_started)
                stage_started = time.perf_counter()
                features = await self.feature_store.latest(asset=symbol, limit=200)
                if not features:
                    await self._record_no_asset_strategy_evaluations(
                        engine_run_id=engine_run_id,
                        asset=symbol,
                        timestamp_ms=ts,
                    )
                    _stage_done("regime", stage_started)
                    continue
                regime = self.regime_engine.compute(features, primary_asset=symbol)
                await self._persist_regime(regime)
                snapshot = self.feature_store.snapshot(asset=symbol)
                _stage_done("regime", stage_started)
                stage_started = time.perf_counter()
                symbol_candidates = []
                selection = self.strategy_selector.select(
                    self.strategies,
                    regime,
                    asset=snapshot.asset,
                    venue=snapshot.venue_id,
                )
                strategy_selection_summaries[symbol] = selection.summary()
                selected_ids = {strategy.spec.strategy_id for strategy in selection.strategies}
                skipped_by_id = {
                    str(item.get("strategy_id") or ""): item for item in selection.skipped
                }
                for strategy in self.strategies:
                    trace: StrategyGenerationTrace | None = None
                    if strategy.spec.strategy_id in selected_ids:
                        await self._prepare_strategy(strategy, timestamp_ms=ts)
                        trace = evaluate_strategy(strategy, snapshot, regime, timestamp_ms=ts)
                        symbol_candidates.extend(trace.candidates)
                    await self._record_strategy_evaluation(
                        engine_run_id=engine_run_id,
                        asset=symbol,
                        timestamp_ms=ts,
                        strategy=strategy,
                        snapshot=snapshot,
                        regime=regime,
                        trace=trace,
                        skipped=skipped_by_id.get(strategy.spec.strategy_id),
                        news_risk_tier=selection.news_risk_tier,
                    )
                evidence_epoch_id = str(getattr(self.settings, "engine_evidence_epoch_id", "") or "runtime")
                symbol_candidates = [
                    candidate.model_copy(
                        update={
                            "instrument_id": snapshot.instrument_id,
                            "underlying_id": snapshot.underlying_id,
                            "venue_id": snapshot.venue_id,
                            "provider_symbol": snapshot.provider_symbol,
                            "venue": snapshot.venue_id,
                            "evidence_epoch_id": evidence_epoch_id,
                            "metadata": {
                                **candidate.metadata,
                                "evidence_epoch_id": evidence_epoch_id,
                                "instrument_id": snapshot.instrument_id,
                                "underlying_id": snapshot.underlying_id,
                                "venue_id": snapshot.venue_id,
                            },
                        }
                    )
                    for candidate in symbol_candidates
                ]
                symbol_candidates = symbol_candidates[: self.settings.engine_max_candidates_per_loop]
                symbol_candidates, symbol_throttle_events = await self.throttles.filter_candidates(symbol_candidates, repository=self.repository, timestamp_ms=ts)
                throttle_events.extend(symbol_throttle_events)
                all_candidates.extend(symbol_candidates)
                await self.candidate_book.add_many(symbol_candidates)
                _stage_done("strategy_generate", stage_started)
                stage_started = time.perf_counter()
                directional_strategy_ids = {item.strategy_id for item in symbol_candidates if item.side != "flat"}
                directional_family_ids = {item.strategy_family for item in symbol_candidates if item.side != "flat"}
                for candidate in symbol_candidates:
                    candidate_step_started = time.perf_counter()
                    ev = await self.scorer.score(candidate, regime)
                    estimates[candidate.candidate_id] = ev
                    allocation = await self.allocator.allocate(candidate, ev, regime=regime, portfolio_state=self._portfolio_snapshot())
                    allocation = self._enrich_allocation_metadata(allocation, candidate)
                    _stage_done("candidate_score_allocate", candidate_step_started)
                    candidate_step_started = time.perf_counter()
                    candidate_risk = await self._candidate_risk_precheck(
                        candidate,
                        ev,
                        snapshot_features=snapshot.features,
                        timestamp_ms=ts,
                        market_snapshot_cache=risk_market_snapshot_cache,
                    )
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
                    _stage_done("candidate_risk", candidate_step_started)
                    candidate_step_started = time.perf_counter()
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
                    allocation = await self.diversity.apply(
                        candidate,
                        allocation,
                        current_loop_allocations=current_loop_allocations,
                        repository=self.repository,
                        timestamp_ms=ts,
                        historical_allocations=allocation_history,
                    )
                    diversity_decisions.append(allocation.metadata.get("diversity", {}))
                    allocations[candidate.candidate_id] = allocation
                    await self._persist_allocation(allocation)
                    _stage_done("candidate_controls", candidate_step_started)

                    candidate_step_started = time.perf_counter()
                    intent = None
                    order_risk = None
                    if allocation.status in {"allocate", "reduce", "require_debate"}:
                        order_ts = now_ms()
                        intent = self._order_intent(candidate, ev.model_version_id, allocation.allocation_id, allocation.allocated_size, allocation.allocated_notional_usd, order_ts)
                        market_snapshot = await self._risk_market_snapshot(
                            candidate,
                            snapshot_features=snapshot.features,
                            cache=risk_market_snapshot_cache,
                        )
                        order_risk = await self.risk_gateway.check_order_intent(
                            intent,
                            market_snapshot=market_snapshot,
                            portfolio_snapshot=self._portfolio_snapshot(),
                            strategy_snapshot={"net_ev_bps": ev.net_ev_bps, "regime_permission": True},
                            operator_context={"kill_switch_active": False, "config_approved": True, "model_approved": ev.model_version_id == "deterministic_fallback_v1" or bool(self.settings.engine_approved_scorer_model_id)},
                        )
                        if not order_risk.allowed:
                            allocation = allocation.model_copy(update={"status": "risk_rejected", "allocated_size": 0.0, "allocated_notional_usd": 0.0, "risk_usd": 0.0, "reason_codes": [*allocation.reason_codes, "risk_gateway_reject"]})
                            allocation = self._enrich_allocation_metadata(allocation, candidate)
                            allocations[candidate.candidate_id] = allocation
                            await self._persist_allocation(allocation)
                    if (
                        allocation.status in {"allocate", "reduce", "require_debate"}
                        and intent is not None
                        and intent.execution_mode == "shadow"
                        and (order_risk is None or order_risk.allowed)
                    ):
                        admitted, admission = await self.evidence_admission.admit(
                            candidate,
                            repository=self.repository,
                            timestamp_ms=ts,
                            alternative_strategy_available=len(directional_strategy_ids - {candidate.strategy_id}) > 0,
                            alternative_family_available=len(directional_family_ids - {candidate.strategy_family}) > 0,
                            history_rows=shadow_intent_history,
                        )
                        if not admitted:
                            allocation = allocation.model_copy(
                                update={
                                    "status": "skip",
                                    "allocated_size": 0.0,
                                    "allocated_notional_usd": 0.0,
                                    "risk_usd": 0.0,
                                    "reason_codes": [*allocation.reason_codes, "shadow_evidence_deferred"],
                                    "metadata": {**allocation.metadata, "evidence_admission": admission},
                                }
                            )
                            allocation = self._enrich_allocation_metadata(allocation, candidate)
                            allocations[candidate.candidate_id] = allocation
                            await self._persist_allocation(allocation)
                    _stage_done("candidate_admission", candidate_step_started)
                    candidate_step_started = time.perf_counter()
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
                    _stage_done("candidate_evidence", candidate_step_started)
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
                        allocation = allocation.model_copy(
                            update={
                                "status": "skip",
                                "allocated_size": 0.0,
                                "allocated_notional_usd": 0.0,
                                "risk_usd": 0.0,
                                "reason_codes": [
                                    *allocation.reason_codes,
                                    f"council_{council_review.decision}",
                                ],
                                "metadata": {
                                    **allocation.metadata,
                                    "pre_council_status": allocation.status,
                                    "terminal_stage": "council",
                                    "council_review_id": council_review.review_id,
                                    "council_decision": council_review.decision,
                                },
                            }
                        )
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
                        intent = self._order_intent(candidate, ev.model_version_id, allocation.allocation_id, allocation.allocated_size, allocation.allocated_notional_usd, now_ms())
                    candidate_step_started = time.perf_counter()
                    report = await self.execution.submit(intent)
                    self.order_intents_created += 1
                    self.execution_reports_created += 1
                    executed.append(report)
                    await self.positions.open_from_execution(candidate, report)
                    if intent.execution_mode == "shadow":
                        shadow_intent_history.insert(0, intent.model_dump(mode="json"))
                        shadow_intent_history = shadow_intent_history[: max(10, int(getattr(self.settings, "engine_shadow_evidence_lookback_intents", 100)))]
                    _stage_done("candidate_execution", candidate_step_started)
                _stage_done("candidate_pipeline", stage_started)
            stage_started = time.perf_counter()
            book = await self.candidate_book.snapshot(estimates, as_of_ms=ts)
            _stage_done("book_snapshot", stage_started)
            self.candidates_created += len(all_candidates)
            self.last_throttle_summary = self._throttle_summary(throttle_events=throttle_events, timestamp_ms=ts)
            self.last_diversity_summary = self._diversity_summary(diversity_decisions=diversity_decisions, timestamp_ms=ts)
            self.last_strategy_selection_summary = {"timestamp_ms": ts, "symbols": strategy_selection_summaries}
            self.last_run_at_ms = ts
            self.last_run_id = engine_run_id
            self.run_count += 1
            duration_ms = round((time.perf_counter() - run_started) * 1000, 3)
            completed_at_ms = now_ms()
            self.last_run_duration_ms = duration_ms
            self.last_stage_ms = stage_ms
            self.last_run_completed_at_ms = completed_at_ms
            self.last_successful_run_completed_at_ms = completed_at_ms
            self.last_error = None
            self._recent_run_durations_ms.append(duration_ms)
            return {
                "candidate_book_id": book.candidate_book_id,
                "engine_run_id": engine_run_id,
                "candidates": len(all_candidates),
                "executed": len(executed),
                "throttle_events": len(throttle_events),
                "throttle_summary": self.last_throttle_summary,
                "diversity_summary": self.last_diversity_summary,
                "duration_ms": duration_ms,
                "stage_ms": stage_ms,
            }
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.last_run_duration_ms = round((time.perf_counter() - run_started) * 1000, 3)
            self.last_stage_ms = stage_ms
            self.last_run_completed_at_ms = now_ms()
            raise
        finally:
            self.run_in_progress = False
            self.current_run_id = None
            self.current_run_started_at_ms = None

    async def _record_no_asset_strategy_evaluations(
        self,
        *,
        engine_run_id: str,
        asset: str,
        timestamp_ms: int,
    ) -> None:
        for strategy in self.strategies:
            await self._persist_strategy_evaluation(
                self._strategy_evaluation_payload(
                    engine_run_id=engine_run_id,
                    asset=asset,
                    timestamp_ms=timestamp_ms,
                    strategy=strategy,
                    selection_status="no_asset_data",
                    selection_reason="no_asset_features",
                    generation_outcome="data_unavailable",
                    generation_attempted=False,
                    reason_codes=["no_asset_features"],
                )
            )

    async def _record_strategy_evaluation(
        self,
        *,
        engine_run_id: str,
        asset: str,
        timestamp_ms: int,
        strategy: Any,
        snapshot: Any,
        regime: Any,
        trace: StrategyGenerationTrace | None,
        skipped: dict[str, Any] | None,
        news_risk_tier: str,
    ) -> None:
        required = list(strategy.spec.required_features or [])
        ages_value = snapshot.metadata.get("feature_ages_ms") if isinstance(snapshot.metadata, dict) else {}
        ages = dict(ages_value) if isinstance(ages_value, dict) else {}
        missing = [name for name in required if snapshot.features.get(name) is None]
        stale_threshold_ms = max(
            1,
            int(getattr(self.settings, "engine_validation_missing_data_seconds", 300)),
        ) * 1000
        stale = [
            name
            for name in required
            if name not in missing and int(ages.get(name) or 0) > stale_threshold_ms
        ]
        present_count = max(0, len(required) - len(missing))
        fresh_count = max(0, present_count - len(stale))
        if trace is None:
            selection_reason = str((skipped or {}).get("reason") or "not_selected")
            selection_status = {
                "regime_mismatch": "regime_gated",
                "news_event_risk_suppression": "news_gated",
                "news_event_shock_suppression": "news_gated",
                "strategy_disabled": "disabled",
            }.get(selection_reason, "not_selected")
            outcome = "not_attempted"
            attempted = False
            candidate_ids: list[str] = []
            reasons = [selection_reason]
            diagnostics: dict[str, Any] = {}
        else:
            selection_reason = "selected"
            selection_status = "selected"
            outcome = trace.outcome
            attempted = True
            candidate_ids = [item.candidate_id for item in trace.candidates]
            reasons = list(trace.reason_codes)
            diagnostics = dict(trace.diagnostics)
        if missing and "missing_required_features" not in reasons:
            reasons.extend(["missing_required_features", *[f"missing_feature:{name}" for name in missing]])
        if stale:
            reasons.extend(["stale_required_features", *[f"stale_feature:{name}" for name in stale]])
        payload = self._strategy_evaluation_payload(
            engine_run_id=engine_run_id,
            asset=asset,
            timestamp_ms=timestamp_ms,
            strategy=strategy,
            selection_status=selection_status,
            selection_reason=selection_reason,
            generation_outcome=outcome,
            generation_attempted=attempted,
            reason_codes=reasons,
            regime_snapshot_id=regime.regime_snapshot_id,
            regime_label=regime.regime_label,
            news_risk_tier=news_risk_tier,
            required_feature_count=len(required),
            present_feature_count=present_count,
            fresh_feature_count=fresh_count,
            feature_coverage_pct=(present_count / len(required) * 100.0) if required else 100.0,
            fresh_feature_coverage_pct=(fresh_count / len(required) * 100.0) if required else 100.0,
            missing_features=missing,
            stale_features=stale,
            feature_ages_ms={name: ages.get(name) for name in required if name in ages},
            candidate_ids=candidate_ids,
            diagnostics={
                **diagnostics,
                "stale_threshold_ms": stale_threshold_ms,
                "report_only_freshness": True,
            },
        )
        payload.update(
            {
                "instrument_id": getattr(snapshot, "instrument_id", None),
                "underlying_id": getattr(snapshot, "underlying_id", None),
                "venue_id": getattr(snapshot, "venue_id", None),
                "provider_symbol": getattr(snapshot, "provider_symbol", None),
            }
        )
        await self._persist_strategy_evaluation(payload)

    def _strategy_evaluation_payload(
        self,
        *,
        engine_run_id: str,
        asset: str,
        timestamp_ms: int,
        strategy: Any,
        selection_status: str,
        selection_reason: str,
        generation_outcome: str,
        generation_attempted: bool,
        reason_codes: list[str],
        regime_snapshot_id: str | None = None,
        regime_label: str = "unknown",
        news_risk_tier: str = "no_event",
        required_feature_count: int = 0,
        present_feature_count: int = 0,
        fresh_feature_count: int = 0,
        feature_coverage_pct: float = 0.0,
        fresh_feature_coverage_pct: float = 0.0,
        missing_features: list[str] | None = None,
        stale_features: list[str] | None = None,
        feature_ages_ms: dict[str, Any] | None = None,
        candidate_ids: list[str] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = strategy.spec
        activation_scope = str(spec.metadata.get("activation_scope") or "paper_shadow")
        paper_eligible = bool(spec.metadata.get("paper_eligible", True)) and activation_scope != "shadow_only"
        evaluation_id = "seval_" + hashlib.sha1(
            f"{engine_run_id}:{asset}:{spec.strategy_id}".encode()
        ).hexdigest()[:24]
        ids = list(candidate_ids or [])
        return {
            "evaluation_id": evaluation_id,
            "engine_run_id": engine_run_id,
            "evaluated_at_ms": timestamp_ms,
            "asset": asset,
            "venue": "hyperliquid",
            "strategy_id": spec.strategy_id,
            "strategy_version": spec.version,
            "strategy_family": spec.family,
            "catalog_mode": self.strategy_registry.catalog_mode,
            "activation_scope": activation_scope,
            "paper_eligible": paper_eligible,
            "counts_for_breadth": bool(spec.counts_for_breadth),
            "selection_status": selection_status,
            "selection_reason": selection_reason,
            "regime_snapshot_id": regime_snapshot_id,
            "regime_label": regime_label,
            "news_risk_tier": news_risk_tier,
            "required_feature_count": required_feature_count,
            "present_feature_count": present_feature_count,
            "fresh_feature_count": fresh_feature_count,
            "feature_coverage_pct": round(feature_coverage_pct, 4),
            "fresh_feature_coverage_pct": round(fresh_feature_coverage_pct, 4),
            "missing_features": list(missing_features or []),
            "stale_features": list(stale_features or []),
            "feature_ages_ms": dict(feature_ages_ms or {}),
            "generation_attempted": generation_attempted,
            "generation_outcome": generation_outcome,
            "trigger_fired": bool(ids),
            "candidate_count": len(ids),
            "candidate_ids": ids,
            "reason_codes": sorted(set(reason_codes)),
            "diagnostics": dict(diagnostics or {}),
            "metadata": {
                "schema_version": 1,
                "artifact_type": "engine_strategy_evaluation",
                "freshness_is_report_only": True,
            },
        }

    async def _persist_strategy_evaluation(self, evaluation: dict[str, Any]) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_engine_strategy_evaluation", None)
        if callable(record):
            await record(evaluation)

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

    async def persist_strategy_specs(self) -> int:
        """Persist all registry specs once so reports see unobserved/gated strategies."""

        if self.strategy_specs_persisted:
            return self.strategy_specs_persisted_count
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return 0
        record = getattr(self.repository, "upsert_strategy_spec", None)
        if not callable(record):
            return 0
        count = 0
        for spec in self.strategy_registry.specs(enabled_only=False):
            await record(spec.model_dump(mode="json"))
            count += 1
        self.strategy_specs_persisted = True
        self.strategy_specs_persisted_count = count
        return count

    async def _candidate_risk_precheck(
        self,
        candidate: AlphaCandidate,
        ev: Any,
        *,
        snapshot_features: dict[str, Any],
        timestamp_ms: int,
        market_snapshot_cache: dict[str, dict[str, Any]] | None = None,
    ) -> Any | None:
        decision_ts = now_ms()
        if candidate.side == "flat":
            decision = RiskGatewayDecision(
                decision_id="rgd_no_trade_" + hashlib.sha1(candidate.candidate_id.encode()).hexdigest()[:24],
                intent_id=_no_trade_intent_id(candidate.candidate_id),
                mode="shadow" if self.settings.engine_shadow_enabled and not self.settings.engine_paper_enabled else "paper",
                decision="allow",
                violations=[],
                limits_snapshot={"asset_class": candidate.asset_class, "no_trade": True},
                market_snapshot={"last_price_at_ms": decision_ts, "spread_bps": snapshot_features.get("spread_bps")},
                portfolio_snapshot=self._portfolio_snapshot(),
                config_version_id=None,
                created_at_ms=decision_ts,
                metadata={
                    "candidate_id": candidate.candidate_id,
                    "strategy_id": candidate.strategy_id,
                    "asset": candidate.asset,
                    "venue": candidate.venue,
                    "candidate_level_no_trade": True,
                    "execution_authority": "none",
                    "exchange_actions": [],
                },
            )
            await self.risk_gateway.record(decision)
            return decision
        notional = max(1.0, min(10.0, float(candidate.proposed_entry) * 0.001))
        size = notional / max(float(candidate.proposed_entry), 1e-9)
        intent = self._order_intent(candidate, ev.model_version_id, f"precheck_{candidate.candidate_id}", size, notional, decision_ts)
        intent = intent.model_copy(update={"intent_id": f"precheck_{candidate.candidate_id}"})
        market_snapshot = await self._risk_market_snapshot(
            candidate,
            snapshot_features=snapshot_features,
            cache=market_snapshot_cache,
        )
        return await self.risk_gateway.check_order_intent(
            intent,
            market_snapshot=market_snapshot,
            portfolio_snapshot=self._portfolio_snapshot(),
            strategy_snapshot={"net_ev_bps": ev.net_ev_bps, "regime_permission": True, "candidate_level_precheck": True},
            operator_context={"kill_switch_active": False, "config_approved": True, "model_approved": ev.model_version_id == "deterministic_fallback_v1" or bool(self.settings.engine_approved_scorer_model_id)},
        )

    async def _risk_market_snapshot(
        self,
        candidate: AlphaCandidate,
        *,
        snapshot_features: dict[str, Any],
        cache: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        asset = candidate.asset.upper()
        now = now_ms()
        if cache is not None:
            cached = cache.get(asset)
            cached_at = _int_or_none((cached or {}).get("last_orderbook_at_ms"))
            if cached is not None and cached_at is not None and now - cached_at <= 15_000:
                return dict(cached)

        market_snapshot = {
            "spread_bps": snapshot_features.get("spread_bps"),
            "freshness_source": "feature_snapshot",
        }
        fetch_l2 = getattr(self.hyperliquid, "l2_book", None)
        if not callable(fetch_l2):
            return market_snapshot
        try:
            book = await fetch_l2(asset)
        except Exception:
            return market_snapshot

        received_ts = now_ms()
        spread_bps = _spread_bps_from_l2_book(book)
        if spread_bps is not None:
            market_snapshot["spread_bps"] = spread_bps
        market_snapshot.update(
            {
                "last_price_at_ms": received_ts,
                "last_market_data_at_ms": received_ts,
                "last_orderbook_at_ms": received_ts,
                "freshness_source": "hyperliquid_l2_book",
            }
        )
        if cache is not None:
            cache[asset] = dict(market_snapshot)
        return market_snapshot

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

    async def _record_persisted_cross_venue_features(self, *, symbols: list[str], received_ts_ms: int) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        method = getattr(self.repository, "list_cross_venue_feature_snapshots", None)
        if not callable(method):
            return
        rows = await method(since_ms=received_ts_ms - 5 * 60 * 1000, limit=1000)
        wanted = {symbol.upper() for symbol in symbols}
        latest_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            underlying_id = str(row.get("underlying_id") or "")
            display = underlying_id.split(":", 1)[-1].upper()
            if display not in wanted:
                continue
            key = (display, str(row.get("comparison_instrument_id") or ""))
            if key not in latest_by_pair or int(row.get("as_of_ms") or 0) > int(latest_by_pair[key].get("as_of_ms") or 0):
                latest_by_pair[key] = row
        for (symbol, _), row in latest_by_pair.items():
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            underlying_id = str(row.get("underlying_id") or f"UNKNOWN:{symbol}")
            asset_class = _asset_class_from_underlying(underlying_id)
            reference_identity = {
                "instrument_id": str(row.get("reference_instrument_id") or ""),
                "underlying_id": underlying_id,
                "venue_id": str(row.get("reference_venue_id") or "hyperliquid:main"),
                "provider_symbol": str(metadata.get("reference_provider_symbol") or symbol),
            }
            payload = {
                "symbol": symbol,
                "underlying_id": underlying_id,
                "reference_instrument_id": row.get("reference_instrument_id"),
                "comparison_instrument_id": row.get("comparison_instrument_id"),
                "reference_venue_id": row.get("reference_venue_id"),
                "comparison_venue_id": row.get("comparison_venue_id"),
                "price_delta_bps": row.get("price_delta_bps"),
                "volume_imbalance": row.get("volume_imbalance"),
                "liquidation_divergence": row.get("liquidation_divergence"),
                "max_clock_skew_ms": row.get("max_clock_skew_ms"),
                "quality_flags": row.get("quality_flags") or [],
                "pairwise_not_averaged": True,
                **metadata,
            }
            event = await self.ledger.normalize_and_record(
                event_type="cross_venue_market",
                source="canonical_market_universe",
                provider=str(row.get("comparison_venue_id") or "unknown"),
                payload=payload,
                asset_class=asset_class,
                symbols=[symbol],
                event_ts_ms=int(row.get("as_of_ms") or received_ts_ms),
                received_ts_ms=received_ts_ms,
                metadata={
                    "read_only": True,
                    "pairwise_not_averaged": True,
                    "asset_class": asset_class,
                    "instrument_identity": reference_identity,
                },
            )
            await self.feature_store.features_for_event(event)

    async def _record_persisted_venue_market_features(
        self,
        *,
        symbols: list[str],
        received_ts_ms: int,
    ) -> None:
        """Ingest the latest canonical Hyperliquid reference for each underlying."""

        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        method = getattr(self.repository, "list_venue_market_snapshots", None)
        if not callable(method):
            return
        rows = await method(since_ms=received_ts_ms - 5 * 60 * 1000, limit=5000)
        wanted = {symbol.upper() for symbol in symbols}
        priority = {"hyperliquid:main": 0, "hyperliquid:xyz": 1, "lighter": 2, "alpaca:paper": 3}
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            underlying_id = str(row.get("underlying_id") or "")
            display = underlying_id.split(":", 1)[-1].upper()
            venue_id = str(row.get("venue_id") or "")
            if display not in wanted or venue_id not in priority:
                continue
            current = selected.get(display)
            row_key = (priority[venue_id], -int(row.get("received_ts_ms") or 0))
            current_key = (
                priority.get(str((current or {}).get("venue_id") or ""), 99),
                -int((current or {}).get("received_ts_ms") or 0),
            )
            if current is None or row_key < current_key:
                selected[display] = row
        for symbol, row in selected.items():
            underlying_id = str(row.get("underlying_id") or f"UNKNOWN:{symbol}")
            asset_class = _asset_class_from_underlying(underlying_id)
            provider_symbol = str(row.get("provider_symbol") or symbol)
            identity = {
                "instrument_id": str(row.get("instrument_id") or ""),
                "underlying_id": underlying_id,
                "venue_id": str(row.get("venue_id") or "hyperliquid:main"),
                "provider_symbol": provider_symbol,
            }
            event = await self.ledger.normalize_and_record(
                event_type="venue_market_snapshot",
                source="canonical_market_universe",
                provider=identity["venue_id"],
                payload={
                    "display_symbol": symbol,
                    "provider_symbol": provider_symbol,
                    "bid_px": row.get("bid_px"),
                    "ask_px": row.get("ask_px"),
                    "mid_px": row.get("mid_px"),
                    "mark_px": row.get("mark_px"),
                    "index_px": row.get("index_px"),
                    "last_trade_px": row.get("last_trade_px"),
                    "volume_24h": row.get("volume_24h"),
                    "open_interest": row.get("open_interest"),
                    "funding_rate": row.get("funding_rate"),
                    "depth_bands": row.get("depth_bands") or {},
                    "source_integrity": row.get("source_integrity"),
                },
                asset_class=asset_class,
                symbols=[symbol],
                event_ts_ms=int(row.get("exchange_ts_ms") or row.get("received_ts_ms") or received_ts_ms),
                received_ts_ms=received_ts_ms,
                metadata={
                    "read_only": True,
                    "asset_class": asset_class,
                    "instrument_identity": identity,
                    "venue_snapshot_id": row.get("snapshot_id"),
                },
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
            if inspect.isawaitable(payload):
                payload = await payload
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
        if candidate.strategy_id == "news_event_alpha_v2":
            # A Newswire strategy configured for shadow evaluation must stay shadow even
            # when the wider engine is paper-enabled.  "paper" remains subject to the
            # engine's global paper switch; no Newswire path has live authority.
            news_mode = str(candidate.metadata.get("news_alpha_mode") or "shadow")
            mode = "paper" if news_mode == "paper" and self.settings.engine_paper_enabled else "shadow"
        return OrderIntent(
            intent_id="intent_" + candidate.candidate_id.removeprefix("cand_"),
            parent_candidate_id=candidate.candidate_id,
            portfolio_decision_id=allocation_id,
            asset=candidate.asset,
            asset_class=candidate.asset_class,
            venue=candidate.venue,
            instrument_id=candidate.instrument_id,
            underlying_id=candidate.underlying_id,
            venue_id=candidate.venue_id,
            provider_symbol=candidate.provider_symbol,
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
            metadata={
                "evidence_epoch_id": candidate.evidence_epoch_id,
                "strategy_family": candidate.strategy_family,
                "instrument_id": candidate.instrument_id,
                "underlying_id": candidate.underlying_id,
                "venue_id": candidate.venue_id,
            },
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


def _no_trade_intent_id(candidate_id: str) -> str:
    value = f"no_trade_{candidate_id}"
    if len(value) <= 96:
        return value
    return "no_trade_" + hashlib.sha1(candidate_id.encode()).hexdigest()[:24]


def _latency_window(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"sample_count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "sample_count": len(values),
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = min(1.0, max(0.0, quantile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


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


def _asset_class_from_underlying(underlying_id: str) -> AssetClass:
    prefix = underlying_id.split(":", 1)[0].upper()
    return {
        "CRYPTO": "crypto",
        "EQUITY": "equity",
        "ETF": "equity",
        "INDEX": "macro",
        "FX": "fx",
        "COMMODITY": "commodity",
        "SYNTHETIC": "unknown",
    }.get(prefix, "unknown")  # type: ignore[return-value]


def _spread_bps_from_l2_book(book: Any) -> float | None:
    if isinstance(book, dict):
        levels = book.get("levels") or []
    else:
        levels = book or []
    if not isinstance(levels, (list, tuple)) or len(levels) < 2:
        return None
    bids = levels[0] if isinstance(levels[0], (list, tuple)) else []
    asks = levels[1] if isinstance(levels[1], (list, tuple)) else []
    if not bids or not asks:
        return None
    bid_px, _ = _level_px_sz(bids[0])
    ask_px, _ = _level_px_sz(asks[0])
    if bid_px is None or ask_px is None or bid_px <= 0 or ask_px <= 0:
        return None
    mid = (bid_px + ask_px) / 2.0
    if mid <= 0 or ask_px < bid_px:
        return None
    return (ask_px - bid_px) / mid * 10_000.0


def _level_px_sz(level: Any) -> tuple[float | None, float | None]:
    if isinstance(level, dict):
        return _float_or_none(level.get("px") or level.get("price")), _float_or_none(level.get("sz") or level.get("size"))
    if isinstance(level, (list, tuple)):
        px = level[0] if len(level) > 0 else None
        sz = level[1] if len(level) > 1 else None
        return _float_or_none(px), _float_or_none(sz)
    return None, None


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
