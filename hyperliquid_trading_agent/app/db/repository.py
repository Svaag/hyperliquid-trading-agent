from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.db.models import (
    AllocationDecisionRecord,
    AllocationDiversityEventRecord,
    AlphaCandidateRecord,
    AlphaEventEvaluationMarkRecord,
    AlphaEventEvaluationRecord,
    AuditEvent,
    AutonomyEvent,
    AutonomyNewsEvent,
    BanditPolicySnapshotRecord,
    BanditRecommendationRecord,
    CacheItem,
    CandidateBookSnapshotRecord,
    CandidateConfigDiffRecord,
    CandidateEvidenceLinkRecord,
    CandidateLessonRecord,
    CandidateOutcomeAttributionRecord,
    CandidateTradePacketRecord,
    ConfigVersionRecord,
    ConversationMessage,
    ConversationThread,
    CouncilReviewRecord,
    CouncilVoteRecord,
    DailyReportRecord,
    DebateDecisionRecord,
    DecisionContextRecord,
    DecisionRoleOutput,
    DecisionRun,
    DecisionStateSnapshot,
    EquityOptionsFlowEventRecord,
    EquityPaperFillRecord,
    EquityPaperOrderRecord,
    EquityPaperPortfolioRecord,
    EquityPaperPositionRecord,
    EquityPortfolioSnapshotRecord,
    EVEstimateRecord,
    EvidencePackRecord,
    ExecutionReportRecord,
    FeatureRollupRecord,
    FeatureSchemaVersionRecord,
    FeatureValueRecord,
    Hip4CapabilityProbeRecord,
    Hip4EdgeCandidateRecord,
    Hip4MarketSnapshotRecord,
    Hip4OutcomeSpecRecord,
    Hip4PaperActionRecord,
    Hip4PaperFillRecord,
    Hip4PaperPortfolioRecord,
    Hip4PaperPositionRecord,
    Hip4QuestionSpecRecord,
    Hip4RawPayloadRecord,
    Hip4ReconciliationRunRecord,
    Hip4SettlementRecord,
    KillSwitchEventRecord,
    MarketAssetRecord,
    MarketBeliefRecord,
    MarketLevelRecord,
    MarketObservation,
    MemoryInjectionEventRecord,
    MemoryObservationRecord,
    ModelTrainingRunRecord,
    ModelVersionRecord,
    NarrativeClusterRecord,
    NewsItem,
    NewswireEventRow,
    NewswirePublishLedgerRow,
    NormalizedEventRecord,
    OperatorFeedbackRecord,
    OperatorOutputLessonRecord,
    OrderIntentRecord,
    PaperFillRecord,
    PaperOrderRecord,
    PaperPortfolioRecord,
    PaperPositionRecord,
    PaperTradeIdea,
    PaperTradeSnapshot,
    PnLAttributionRecord,
    PortfolioConcentrationEventRecord,
    PortfolioSnapshotRecord,
    PositionThesisRecord,
    PositionTracker,
    PredictionMarketCalibrationRecord,
    PredictionMarketSignalRecord,
    PromotionDecisionRecord,
    PromptVersionRecord,
    ReconciliationRunRecord,
    RegimeSnapshotRecord,
    ReplayResultLinkRecord,
    ReplayResultRecord,
    RetentionRunRecord,
    ReviewPacketRecord,
    RiskGatewayDecisionRecord,
    RoleLessonRecord,
    RollbackPlanRecord,
    ShadowComparisonRecord,
    ShadowRoleLessonRecord,
    SignalEvaluationMarkRecord,
    SignalEvaluationRecord,
    SourceCredibilityRecord,
    StrategyRegimePerformanceRecord,
    StrategySpecRecord,
    TokenCapitalSnapshotRecord,
    ToolCall,
    TrackedLevel,
    TrackingEvent,
    TradeProposalRecord,
    TradeSignalRecord,
    TuningProposalRecord,
    WeeklyReportRecord,
    WorldEventRecord,
    WorldMemoryAtomRecord,
    WorldModelAnnotationRecord,
    WorldModelOutcomeRecord,
    WorldModelSnapshotRecord,
)
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.security import redact_secrets
from hyperliquid_trading_agent.app.tracking.schemas import PositionTrackingPlan

log = get_logger(__name__)


class Repository:
    """Async persistence facade for audit, cache, conversations, and paper trades."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None):
        self.sessionmaker = sessionmaker

    @property
    def enabled(self) -> bool:
        return self.sessionmaker is not None

    async def record_audit_event(self, event_type: str, actor: str = "", payload: dict[str, Any] | None = None) -> None:
        if self.sessionmaker is None:
            return
        payload = redact_secrets(payload or {})
        try:
            async with self.sessionmaker() as session:
                session.add(AuditEvent(event_type=event_type, actor=actor, payload=payload))
                await session.commit()
        except Exception as exc:  # pragma: no cover - best-effort audit logging must not break bot answers
            log.warning("audit_event_record_failed", event_type=event_type, error=type(exc).__name__)

    async def record_tool_call(
        self,
        tool_name: str,
        status: str,
        input_json: dict[str, Any] | None = None,
        output_json: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(
                    ToolCall(
                        tool_name=tool_name,
                        status=status,
                        input_json=redact_secrets(input_json or {}),
                        output_json=redact_secrets(output_json or {}),
                        latency_ms=latency_ms,
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("tool_call_record_failed", tool_name=tool_name, error=type(exc).__name__)

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(CacheItem, key)
                if item is None:
                    return None
                if item.expires_at and item.expires_at <= datetime.now(UTC):
                    await session.delete(item)
                    await session.commit()
                    return None
                return item.value
        except Exception as exc:  # pragma: no cover
            log.warning("cache_get_failed", key=key, error=type(exc).__name__)
            return None

    async def cache_set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if self.sessionmaker is None:
            return
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        try:
            async with self.sessionmaker() as session:
                existing = await session.get(CacheItem, key)
                if existing is None:
                    session.add(CacheItem(key=key, value=value, expires_at=expires_at))
                else:
                    existing.value = value
                    existing.expires_at = expires_at
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("cache_set_failed", key=key, error=type(exc).__name__)

    async def upsert_discord_thread(
        self,
        discord_guild_id: str | None,
        discord_channel_id: str | None,
        discord_thread_id: str | None,
        title: str,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(ConversationThread).where(ConversationThread.discord_thread_id == discord_thread_id)
            )
            thread = result.scalar_one_or_none()
            if thread is None:
                thread = ConversationThread(
                    discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                    discord_thread_id=discord_thread_id,
                    title=title[:255],
                )
                session.add(thread)
                await session.flush()
            await session.commit()
            return thread.id

    async def add_message(
        self,
        thread_id: str | None,
        role: str,
        content: str,
        discord_user_id: str | None = None,
    ) -> None:
        if self.sessionmaker is None or thread_id is None:
            return
        async with self.sessionmaker() as session:
            session.add(
                ConversationMessage(
                    thread_id=thread_id,
                    role=role,
                    content=content,
                    discord_user_id=discord_user_id,
                )
            )
            await session.commit()

    async def get_recent_messages(self, thread_id: str | None, limit: int = 8) -> list[dict[str, Any]]:
        if self.sessionmaker is None or thread_id is None:
            return []
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.thread_id == thread_id)
                    .order_by(ConversationMessage.created_at.desc())
                    .limit(limit)
                )
                messages = list(reversed(result.scalars().all()))
                return [
                    {
                        "role": item.role,
                        "content": item.content,
                        "discord_user_id": item.discord_user_id,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    }
                    for item in messages
                ]
        except Exception as exc:  # pragma: no cover
            log.warning("recent_messages_get_failed", thread_id=thread_id, error=type(exc).__name__)
            return []

    async def record_news_item(self, source: str, title: str, url: str, summary: str = "") -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(NewsItem(source=source, title=title, url=url, summary=summary, published_at=None))
                await session.commit()
        except Exception:  # pragma: no cover - duplicate/unavailable news persistence should not break answers
            return

    async def record_paper_trade(
        self,
        discord_user_id: str | None,
        coin: str,
        side: str,
        thesis: str,
        plan: dict[str, Any],
        market_snapshot: dict[str, Any] | None = None,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            idea = PaperTradeIdea(discord_user_id=discord_user_id, coin=coin, side=side, thesis=thesis, plan=plan)
            session.add(idea)
            await session.flush()
            session.add(PaperTradeSnapshot(idea_id=idea.id, market_snapshot=market_snapshot or {}))
            await session.commit()
            return idea.id

    async def upsert_config_version(self, version: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                version_id = str(version["id"])
                scope = str(version.get("scope") or "runtime_settings")
                await session.execute(
                    update(ConfigVersionRecord)
                    .where(ConfigVersionRecord.scope == scope, ConfigVersionRecord.id != version_id)
                    .values(active=False)
                )
                item = await session.get(ConfigVersionRecord, version_id)
                if item is None:
                    item = ConfigVersionRecord(
                        id=version_id,
                        scope=scope,
                        version_hash=str(version.get("version_hash") or ""),
                        created_at_ms=int(version.get("created_at_ms") or 0),
                    )
                    session.add(item)
                item.scope = scope
                item.version_hash = str(version.get("version_hash") or item.version_hash)
                item.payload_json = redact_secrets(dict(version.get("payload") or {}))
                item.code_version = version.get("code_version")
                item.created_at_ms = int(version.get("created_at_ms") or item.created_at_ms)
                item.active = bool(version.get("active", True))
                item.metadata_json = redact_secrets(dict(version.get("metadata") or {}))
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover - governance audit must not block runtime
            log.warning("config_version_upsert_failed", error=type(exc).__name__)
            return None

    async def upsert_prompt_version(self, version: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                version_id = str(version["id"])
                prompt_name = str(version.get("prompt_name") or "unknown")
                await session.execute(
                    update(PromptVersionRecord)
                    .where(PromptVersionRecord.prompt_name == prompt_name, PromptVersionRecord.id != version_id)
                    .values(active=False)
                )
                item = await session.get(PromptVersionRecord, version_id)
                if item is None:
                    item = PromptVersionRecord(
                        id=version_id,
                        prompt_name=prompt_name,
                        version_hash=str(version.get("version_hash") or ""),
                        content_hash=str(version.get("content_hash") or ""),
                        created_at_ms=int(version.get("created_at_ms") or 0),
                    )
                    session.add(item)
                item.prompt_name = prompt_name
                item.version_hash = str(version.get("version_hash") or item.version_hash)
                item.content_hash = str(version.get("content_hash") or item.content_hash)
                item.payload_json = redact_secrets(dict(version.get("payload") or {}))
                item.code_version = version.get("code_version")
                item.created_at_ms = int(version.get("created_at_ms") or item.created_at_ms)
                item.active = bool(version.get("active", True))
                item.metadata_json = redact_secrets(dict(version.get("metadata") or {}))
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("prompt_version_upsert_failed", error=type(exc).__name__)
            return None

    async def record_decision_context(
        self,
        context: dict[str, Any],
        *,
        source_type: str = "unknown",
        source_id: str | None = None,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                context_id = str(context["decision_id"])
                item = await session.get(DecisionContextRecord, context_id)
                model_route = dict(context.get("model_route") or {})
                metadata = dict(context.get("metadata") or {})
                if item is None:
                    item = DecisionContextRecord(
                        id=context_id,
                        source_type=source_type,
                        source_id=source_id or metadata.get("source_id"),
                        run_id=context.get("run_id"),
                        config_version_id=str(context.get("config_version_id") or ""),
                        risk_config_version_id=str(context.get("risk_config_version_id") or ""),
                        created_at_ms=int(context.get("created_at_ms") or 0),
                    )
                    session.add(item)
                item.source_type = source_type or str(metadata.get("source_type") or "unknown")
                item.source_id = source_id or metadata.get("source_id")
                item.run_id = context.get("run_id")
                item.config_version_id = str(context.get("config_version_id") or item.config_version_id)
                item.risk_config_version_id = str(context.get("risk_config_version_id") or item.risk_config_version_id)
                item.model_route_version_id = model_route.get("version_id")
                item.prompt_version_ids_json = list(context.get("prompt_version_ids") or [])
                item.injected_memory_ids_json = list(context.get("injected_memory_ids") or [])
                item.market_snapshot_refs_json = list(context.get("market_snapshot_refs") or [])
                item.data_freshness_json = redact_secrets(dict(context.get("data_freshness") or {}))
                item.code_version = context.get("code_version")
                item.created_at_ms = int(context.get("created_at_ms") or item.created_at_ms)
                item.context_json = redact_secrets(dict(context))
                item.metadata_json = redact_secrets(metadata)
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("decision_context_record_failed", source_type=source_type, error=type(exc).__name__)
            return None

    async def get_decision_context(self, decision_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(DecisionContextRecord, decision_id)
                return _decision_context_to_dict(item) if item is not None else None
        except Exception as exc:  # pragma: no cover
            log.warning("decision_context_get_failed", decision_id=decision_id, error=type(exc).__name__)
            return None

    async def record_risk_gateway_decision(self, decision: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = RiskGatewayDecisionRecord(
                    decision_id=str(decision["decision_id"]),
                    intent_id=str(decision.get("intent_id") or ""),
                    mode=str(decision.get("mode") or "paper"),
                    decision=str(decision.get("decision") or "reject"),
                    violations_json=redact_secrets(list(decision.get("violations") or [])),
                    limits_snapshot_json=redact_secrets(dict(decision.get("limits_snapshot") or {})),
                    market_snapshot_json=redact_secrets(dict(decision.get("market_snapshot") or {})),
                    portfolio_snapshot_json=redact_secrets(dict(decision.get("portfolio_snapshot") or {})),
                    config_version_id=decision.get("config_version_id"),
                    created_at_ms=int(decision.get("created_at_ms") or 0),
                    metadata_json=redact_secrets(dict(decision.get("metadata") or {})),
                )
                session.add(item)
                await session.commit()
                return item.decision_id
        except Exception as exc:  # pragma: no cover
            log.warning("risk_gateway_decision_record_failed", intent_id=decision.get("intent_id"), error=type(exc).__name__)
            return None

    async def list_risk_gateway_decisions(self, limit: int = 100, decision: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                stmt = select(RiskGatewayDecisionRecord).order_by(RiskGatewayDecisionRecord.created_at_ms.desc()).limit(limit)
                if decision:
                    stmt = stmt.where(RiskGatewayDecisionRecord.decision == decision)
                result = await session.execute(stmt)
                return [_risk_gateway_decision_to_dict(item) for item in result.scalars().all()]
        except Exception as exc:  # pragma: no cover
            log.warning("risk_gateway_decisions_list_failed", error=type(exc).__name__)
            return []

    async def record_hip4_capability_probe(self, probe: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = Hip4CapabilityProbeRecord(
                    network=str(probe.get("network") or "unknown"),
                    probed_at_ms=int(probe.get("probed_at_ms") or 0),
                    outcome_meta_available=bool(probe.get("outcome_meta_available")),
                    outcome_meta_error=probe.get("outcome_meta_error"),
                    outcome_meta_schema_hash=probe.get("outcome_meta_schema_hash"),
                    payload_json=redact_secrets(dict(probe)),
                    degraded_reasons_json=list(probe.get("degraded_reasons") or []),
                )
                session.add(item)
                await session.commit()
                return item.probe_id
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_capability_probe_record_failed", error=type(exc).__name__)
            return None

    async def record_hip4_raw_payload(self, payload: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = Hip4RawPayloadRecord(
                    source=str(payload.get("source") or "unknown"),
                    network=str(payload.get("network") or "unknown"),
                    payload_json=redact_secrets(dict(payload.get("payload_json") or {})),
                    schema_hash=str(payload.get("schema_hash") or ""),
                    schema_version=int(payload.get("schema_version") or 1),
                    observed_at_ms=int(payload.get("observed_at_ms") or 0),
                )
                session.add(item)
                await session.commit()
                return item.payload_id
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_raw_payload_record_failed", error=type(exc).__name__)
            return None

    async def upsert_hip4_outcome_specs(self, outcomes: list[dict[str, Any]], *, as_of_ms: int) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                for data in outcomes:
                    item = await session.get(Hip4OutcomeSpecRecord, int(data["outcome_id"]))
                    if item is None:
                        item = Hip4OutcomeSpecRecord(outcome_id=int(data["outcome_id"]), name="", as_of_ms=as_of_ms)
                        session.add(item)
                    item.name = str(data.get("name") or item.name)
                    item.description = str(data.get("description") or "")
                    item.quote_token = data.get("quote_token")
                    item.side0_name = str(data.get("side0_name") or "YES")
                    item.side1_name = str(data.get("side1_name") or "NO")
                    item.status = "settled" if data.get("settled") else "open"
                    item.settle_fraction = str(data.get("settle_fraction")) if data.get("settle_fraction") is not None else None
                    item.settlement_details = data.get("settlement_details")
                    item.raw_json = redact_secrets(dict(data.get("raw") or {}))
                    item.as_of_ms = as_of_ms
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_outcome_specs_upsert_failed", error=type(exc).__name__)

    async def upsert_hip4_question_specs(self, questions: list[dict[str, Any]], *, as_of_ms: int) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                for data in questions:
                    item = await session.get(Hip4QuestionSpecRecord, int(data["question_id"]))
                    if item is None:
                        item = Hip4QuestionSpecRecord(question_id=int(data["question_id"]), name="", as_of_ms=as_of_ms)
                        session.add(item)
                    item.name = str(data.get("name") or item.name)
                    item.description = str(data.get("description") or "")
                    item.fallback_outcome_id = data.get("fallback_outcome_id")
                    item.named_outcome_ids_json = list(data.get("named_outcome_ids") or [])
                    item.settled_named_outcome_ids_json = list(data.get("settled_named_outcome_ids") or [])
                    item.outcome_ids_json = list(data.get("outcome_ids") or [])
                    item.status = str(data.get("status") or "open")
                    item.raw_json = redact_secrets(dict(data.get("raw") or {}))
                    item.as_of_ms = as_of_ms
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_question_specs_upsert_failed", error=type(exc).__name__)

    async def record_hip4_edge_candidates(self, candidates: list[dict[str, Any]]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                for data in candidates:
                    item = Hip4EdgeCandidateRecord(
                        candidate_id=str(data["candidate_id"]),
                        strategy_type=str(data.get("strategy_type") or "unknown"),
                        mode=str(data.get("mode") or "shadow"),
                        question_id=data.get("question_id"),
                        outcome_ids_json=list(data.get("outcome_ids") or []),
                        as_of_ms=int(data.get("as_of_ms") or 0),
                        size=str(data.get("size") or "0"),
                        gross_cost_or_proceeds=str(data.get("gross_cost_or_proceeds") or "0"),
                        expected_net_edge_usd=str(data.get("expected_net_edge_usd") or "0"),
                        expected_net_edge_bps=str(data.get("expected_net_edge_bps") or "0"),
                        status=str(data.get("status") or "candidate"),
                        candidate_json=redact_secrets(dict(data)),
                    )
                    await session.merge(item)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_edge_candidates_record_failed", error=type(exc).__name__)

    async def record_hip4_market_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                bids = list(snapshot.get("bids") or [])
                asks = list(snapshot.get("asks") or [])
                item = Hip4MarketSnapshotRecord(
                    question_id=snapshot.get("question_id"),
                    outcome_id=snapshot.get("outcome_id"),
                    coin=str(snapshot.get("coin") or ""),
                    side=int(snapshot.get("side") or 0),
                    as_of_ms=int(snapshot.get("as_of_ms") or 0),
                    best_bid=str((bids[0] or {}).get("px")) if bids else None,
                    best_ask=str((asks[0] or {}).get("px")) if asks else None,
                    raw_json=redact_secrets(dict(snapshot)),
                )
                session.add(item)
                await session.commit()
                return item.snapshot_id
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_market_snapshot_record_failed", error=type(exc).__name__)
            return None

    async def record_hip4_settlement(self, settlement: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = Hip4SettlementRecord(
                    outcome_id=int(settlement.get("outcome_id") or settlement.get("outcome") or 0),
                    settle_fraction=str(settlement.get("settle_fraction") or settlement.get("settleFraction")) if settlement.get("settle_fraction") is not None or settlement.get("settleFraction") is not None else None,
                    details=settlement.get("details") or settlement.get("settlement_details"),
                    raw_json=redact_secrets(dict(settlement.get("raw") or settlement)),
                    as_of_ms=int(settlement.get("as_of_ms") or 0),
                )
                session.add(item)
                await session.commit()
                return item.settlement_id
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_settlement_record_failed", error=type(exc).__name__)
            return None

    async def record_hip4_paper_execution(self, execution: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                portfolio = dict(execution.get("portfolio") or {})
                await session.merge(
                    Hip4PaperPortfolioRecord(
                        portfolio_id=str(portfolio.get("portfolio_id") or "hip4_default"),
                        quote_token=str(portfolio.get("quote_token") or "USDC"),
                        cash=str(portfolio.get("cash") or "0"),
                        realized_pnl=str(portfolio.get("realized_pnl") or "0"),
                        unrealized_pnl=str(portfolio.get("unrealized_pnl") or "0"),
                        settlement_pnl=str(portfolio.get("settlement_pnl") or "0"),
                        modeled_fees=str(portfolio.get("modeled_fees") or "0"),
                        daily_notional=str(portfolio.get("daily_notional") or "0"),
                        balances_json=redact_secrets(dict(portfolio.get("balances") or {})),
                        updated_at_ms=int(portfolio.get("updated_at_ms") or execution.get("created_at_ms") or 0),
                    )
                )
                candidate_id = str((execution.get("candidate") or {}).get("candidate_id") or "")
                for token, balance in dict(portfolio.get("balances") or {}).items():
                    session.add(Hip4PaperPositionRecord(portfolio_id=str(portfolio.get("portfolio_id") or "hip4_default"), token=str(token), balance=str(balance), updated_at_ms=int(portfolio.get("updated_at_ms") or 0)))
                for action in execution.get("actions") or []:
                    session.add(Hip4PaperActionRecord(candidate_id=candidate_id, action_type=str(action.get("action_type") or "unknown"), amount=str(action.get("amount") or "0"), price=str(action.get("price")) if action.get("price") is not None else None, action_json=redact_secrets(dict(action)), created_at_ms=int(execution.get("created_at_ms") or 0)))
                for fill in execution.get("fills") or []:
                    session.add(Hip4PaperFillRecord(fill_id=str(fill["fill_id"]), candidate_id=str(fill.get("candidate_id") or candidate_id), coin=str(fill.get("coin") or ""), side=str(fill.get("side") or ""), size=str(fill.get("size") or "0"), price=str(fill.get("price") or "0"), notional=str(fill.get("notional") or "0"), fee=str(fill.get("fee") or "0"), created_at_ms=int(fill.get("created_at_ms") or 0)))
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_paper_execution_record_failed", error=type(exc).__name__)

    async def record_hip4_reconciliation_run(self, result: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = Hip4ReconciliationRunRecord(
                    run_id=str(result.get("run_id") or ""),
                    status=str(result.get("status") or "unknown"),
                    discrepancies_json=redact_secrets(list(result.get("discrepancies") or [])),
                    result_json=redact_secrets(dict(result)),
                    created_at_ms=int(result.get("created_at_ms") or 0),
                )
                session.add(item)
                await session.commit()
                return item.run_id
        except Exception as exc:  # pragma: no cover
            log.warning("hip4_reconciliation_record_failed", error=type(exc).__name__)
            return None

    async def record_normalized_event(self, event: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            NormalizedEventRecord(
                event_id=str(event["event_id"]),
                schema_version=int(event.get("schema_version") or 1),
                event_type=str(event.get("event_type") or "unknown"),
                asset_class=str(event.get("asset_class") or "unknown"),
                symbols_json=list(event.get("symbols") or []),
                source=str(event.get("source") or "unknown"),
                provider=str(event.get("provider") or "unknown"),
                event_ts_ms=event.get("event_ts_ms"),
                received_ts_ms=int(event.get("received_ts_ms") or 0),
                computed_ts_ms=int(event.get("computed_ts_ms") or 0),
                payload_json=redact_secrets(dict(event.get("payload") or {})),
                quality_score=float(event.get("quality_score") if event.get("quality_score") is not None else 1.0),
                staleness_ms=event.get("staleness_ms"),
                metadata_json=redact_secrets(dict(event.get("metadata") or {})),
            ),
            "event_id",
        )

    async def get_normalized_event(self, event_id: str) -> dict[str, Any] | None:
        return await self._get_engine_record(NormalizedEventRecord, event_id)

    async def list_normalized_events(self, *, limit: int = 100, event_type: str | None = None, asset_class: str | None = None) -> list[dict[str, Any]]:
        filters = []
        if event_type:
            filters.append(NormalizedEventRecord.event_type == event_type)
        if asset_class:
            filters.append(NormalizedEventRecord.asset_class == asset_class)
        return await self._list_engine_records(NormalizedEventRecord, order_by=NormalizedEventRecord.received_ts_ms, limit=limit, filters=filters)

    async def record_feature_value(self, feature: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            FeatureValueRecord(
                feature_id=str(feature["feature_id"]),
                asset=str(feature.get("asset") or "").upper(),
                feature_group=str(feature.get("feature_group") or "unknown"),
                feature_name=str(feature.get("feature_name") or "unknown"),
                value_json=redact_secrets(dict(feature.get("value") or {})),
                scalar_value=feature.get("scalar_value"),
                event_ts_ms=feature.get("event_ts_ms"),
                received_ts_ms=int(feature.get("received_ts_ms") or 0),
                computed_ts_ms=int(feature.get("computed_ts_ms") or 0),
                source_event_id=feature.get("source_event_id"),
                source=str(feature.get("source") or "unknown"),
                version=str(feature.get("version") or "v0"),
                quality_score=float(feature.get("quality_score") if feature.get("quality_score") is not None else 1.0),
                staleness_ms=feature.get("staleness_ms"),
                metadata_json=redact_secrets(dict(feature.get("metadata") or {})),
            ),
            "feature_id",
        )

    async def list_feature_values(self, *, asset: str | None = None, feature_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if asset:
            filters.append(FeatureValueRecord.asset == asset.upper())
        if feature_name:
            filters.append(FeatureValueRecord.feature_name == feature_name)
        return await self._list_engine_records(FeatureValueRecord, order_by=FeatureValueRecord.computed_ts_ms, limit=limit, filters=filters)

    async def record_feature_rollup(self, rollup: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            FeatureRollupRecord(
                rollup_id=str(rollup["rollup_id"]),
                asset=str(rollup.get("asset") or "").upper(),
                feature_group=str(rollup.get("feature_group") or "unknown"),
                feature_name=str(rollup.get("feature_name") or "unknown"),
                interval=str(rollup.get("interval") or "1m"),
                window_start_ms=int(rollup.get("window_start_ms") or 0),
                window_end_ms=int(rollup.get("window_end_ms") or 0),
                min_value=rollup.get("min_value"),
                max_value=rollup.get("max_value"),
                avg_value=rollup.get("avg_value"),
                last_value=rollup.get("last_value"),
                count=int(rollup.get("count") or 0),
                quality_avg=rollup.get("quality_avg"),
                metadata_json=redact_secrets(dict(rollup.get("metadata") or {})),
            ),
            "rollup_id",
        )

    async def upsert_strategy_spec(self, spec: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            StrategySpecRecord(
                strategy_id=str(spec["strategy_id"]),
                version=str(spec.get("version") or "unknown"),
                family=str(spec.get("family") or "unknown"),
                supported_assets_json=list(spec.get("supported_assets") or []),
                supported_venues_json=list(spec.get("supported_venues") or []),
                supported_horizons_json=list(spec.get("supported_horizons") or []),
                required_features_json=list(spec.get("required_features") or []),
                valid_regimes_json=list(spec.get("valid_regimes") or []),
                max_candidates_per_run=int(spec.get("max_candidates_per_run") or 0),
                max_allocation_share_pct=float(spec.get("max_allocation_share_pct") or 0),
                cooldown_ms=int(spec.get("cooldown_ms") or 0),
                min_confidence=float(spec.get("min_confidence") or 0),
                min_ev_bps=float(spec.get("min_ev_bps") or 0),
                risk_tags_json=list(spec.get("risk_tags") or []),
                counts_for_breadth=bool(spec.get("counts_for_breadth", True)),
                enabled=bool(spec.get("enabled", True)),
                metadata_json=redact_secrets(dict(spec.get("metadata") or {})),
            ),
            "strategy_id",
        )

    async def list_strategy_specs(self, *, family: str | None = None, enabled: bool | None = None, limit: int = 500) -> list[dict[str, Any]]:
        filters = []
        if family:
            filters.append(StrategySpecRecord.family == family)
        if enabled is not None:
            filters.append(StrategySpecRecord.enabled == enabled)
        return await self._list_engine_records(StrategySpecRecord, order_by=StrategySpecRecord.strategy_id, limit=limit, filters=filters)

    async def get_strategy_spec(self, strategy_id: str) -> dict[str, Any] | None:
        return await self._get_engine_record(StrategySpecRecord, strategy_id)

    async def record_regime_snapshot(self, regime: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            RegimeSnapshotRecord(
                regime_snapshot_id=str(regime["regime_snapshot_id"]),
                primary_asset=str(regime.get("primary_asset") or "GLOBAL").upper(),
                created_at_ms=int(regime.get("created_at_ms") or 0),
                as_of_ms=int(regime.get("as_of_ms") or regime.get("created_at_ms") or 0),
                vector_json=redact_secrets(dict(regime)),
                permissions_json=redact_secrets(dict(regime.get("permissions") or {})),
                feature_refs_json=list(regime.get("feature_refs") or []),
                quality_flags_json=list(regime.get("quality_flags") or []),
                metadata_json=redact_secrets(dict(regime.get("metadata") or {})),
            ),
            "regime_snapshot_id",
        )

    async def latest_regime_snapshot(self, primary_asset: str | None = None) -> dict[str, Any] | None:
        items = await self._list_engine_records(
            RegimeSnapshotRecord,
            order_by=RegimeSnapshotRecord.created_at_ms,
            limit=1,
            filters=[RegimeSnapshotRecord.primary_asset == primary_asset.upper()] if primary_asset else [],
        )
        return items[0] if items else None

    async def record_alpha_candidate(self, candidate: dict[str, Any]) -> str | None:
        metadata = {
            **dict(candidate.get("metadata") or {}),
            "strategy_version": candidate.get("strategy_version", "unknown"),
            "strategy_family": candidate.get("strategy_family", "unknown"),
            "valid_regimes": list(candidate.get("valid_regimes") or []),
            "required_features": list(candidate.get("required_features") or []),
            "feature_coverage_pct": candidate.get("feature_coverage_pct", 0.0),
            "expected_edge_bps": candidate.get("expected_edge_bps", 0.0),
            "risk_tags": list(candidate.get("risk_tags") or []),
            "counts_for_breadth": candidate.get("counts_for_breadth", True),
        }
        return await self._merge_engine_record(
            AlphaCandidateRecord(
                candidate_id=str(candidate["candidate_id"]),
                strategy_id=str(candidate.get("strategy_id") or "unknown"),
                asset=str(candidate.get("asset") or "").upper(),
                asset_class=str(candidate.get("asset_class") or "unknown"),
                venue=str(candidate.get("venue") or "unknown"),
                side=str(candidate.get("side") or "flat"),
                horizon=str(candidate.get("horizon") or "unknown"),
                proposed_entry=float(candidate.get("proposed_entry") or 0),
                stop=float(candidate.get("stop") or 0),
                targets_json=list(candidate.get("targets") or []),
                thesis=str(candidate.get("thesis") or ""),
                invalidation_conditions_json=list(candidate.get("invalidation_conditions") or []),
                feature_snapshot_id=str(candidate.get("feature_snapshot_id") or ""),
                regime_snapshot_id=str(candidate.get("regime_snapshot_id") or ""),
                source_event_ids_json=list(candidate.get("source_event_ids") or []),
                raw_alpha_score=float(candidate.get("raw_alpha_score") or 0),
                confidence=float(candidate.get("confidence") or 0),
                status=str(candidate.get("status") or "new"),
                created_at_ms=int(candidate.get("created_at_ms") or 0),
                expires_at_ms=int(candidate.get("expires_at_ms") or 0),
                metadata_json=redact_secrets(metadata),
            ),
            "candidate_id",
        )

    async def get_alpha_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        return await self._get_engine_record(AlphaCandidateRecord, candidate_id)

    async def list_alpha_candidates(self, *, status: str | None = None, asset: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if status:
            filters.append(AlphaCandidateRecord.status == status)
        if asset:
            filters.append(AlphaCandidateRecord.asset == asset.upper())
        return await self._list_engine_records(AlphaCandidateRecord, order_by=AlphaCandidateRecord.created_at_ms, limit=limit, filters=filters)

    async def record_candidate_book_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            CandidateBookSnapshotRecord(
                candidate_book_id=str(snapshot["candidate_book_id"]),
                created_at_ms=int(snapshot.get("created_at_ms") or 0),
                as_of_ms=int(snapshot.get("as_of_ms") or snapshot.get("created_at_ms") or 0),
                candidate_ids_json=list(snapshot.get("candidate_ids") or []),
                ranked_candidate_ids_json=list(snapshot.get("ranked_candidate_ids") or []),
                rejected_candidate_ids_json=list(snapshot.get("rejected_candidate_ids") or []),
                portfolio_state_ref=snapshot.get("portfolio_state_ref"),
                metadata_json=redact_secrets(dict(snapshot.get("metadata") or {})),
            ),
            "candidate_book_id",
        )

    async def latest_candidate_book_snapshot(self) -> dict[str, Any] | None:
        items = await self._list_engine_records(CandidateBookSnapshotRecord, order_by=CandidateBookSnapshotRecord.created_at_ms, limit=1)
        return items[0] if items else None

    async def record_ev_estimate(self, estimate: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            EVEstimateRecord(
                estimate_id=str(estimate["estimate_id"]),
                candidate_id=str(estimate.get("candidate_id") or ""),
                model_version_id=str(estimate.get("model_version_id") or "unknown"),
                p_target=float(estimate.get("p_target") or 0),
                p_stop=float(estimate.get("p_stop") or 0),
                p_timeout=float(estimate.get("p_timeout") or 0),
                expected_favorable_bps=float(estimate.get("expected_favorable_bps") or 0),
                expected_adverse_bps=float(estimate.get("expected_adverse_bps") or 0),
                expected_holding_ms=int(estimate.get("expected_holding_ms") or 0),
                expected_fee_bps=float(estimate.get("expected_fee_bps") or 0),
                expected_spread_cost_bps=float(estimate.get("expected_spread_cost_bps") or 0),
                expected_slippage_bps=float(estimate.get("expected_slippage_bps") or 0),
                expected_market_impact_bps=float(estimate.get("expected_market_impact_bps") or 0),
                expected_funding_cost_bps=float(estimate.get("expected_funding_cost_bps") or 0),
                tail_loss_bps=float(estimate.get("tail_loss_bps") or 0),
                net_ev_bps=float(estimate.get("net_ev_bps") or 0),
                risk_adjusted_utility=float(estimate.get("risk_adjusted_utility") or 0),
                uncertainty=float(estimate.get("uncertainty") or 0),
                calibration_bucket=str(estimate.get("calibration_bucket") or "unknown"),
                created_at_ms=int(estimate.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(estimate.get("metadata") or {})),
            ),
            "estimate_id",
        )

    async def list_ev_estimates(self, *, candidate_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = [EVEstimateRecord.candidate_id == candidate_id] if candidate_id else []
        return await self._list_engine_records(EVEstimateRecord, order_by=EVEstimateRecord.created_at_ms, limit=limit, filters=filters)

    async def record_allocation_decision(self, allocation: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            AllocationDecisionRecord(
                allocation_id=str(allocation["allocation_id"]),
                candidate_id=str(allocation.get("candidate_id") or ""),
                candidate_book_id=allocation.get("candidate_book_id"),
                status=str(allocation.get("status") or "skip"),
                allocated_size=float(allocation.get("allocated_size") or 0),
                allocated_notional_usd=float(allocation.get("allocated_notional_usd") or 0),
                risk_usd=float(allocation.get("risk_usd") or 0),
                max_size_multiplier=float(allocation.get("max_size_multiplier") if allocation.get("max_size_multiplier") is not None else 1.0),
                opportunity_cost_rank=allocation.get("opportunity_cost_rank"),
                constraints_json=redact_secrets(dict(allocation.get("constraints") or {})),
                reason_codes_json=list(allocation.get("reason_codes") or []),
                created_at_ms=int(allocation.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(allocation.get("metadata") or {})),
            ),
            "allocation_id",
        )

    async def list_allocation_decisions(self, *, candidate_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = [AllocationDecisionRecord.candidate_id == candidate_id] if candidate_id else []
        return await self._list_engine_records(AllocationDecisionRecord, order_by=AllocationDecisionRecord.created_at_ms, limit=limit, filters=filters)

    async def record_allocation_diversity_event(self, event: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            AllocationDiversityEventRecord(
                event_id=str(event["event_id"]),
                candidate_id=str(event.get("candidate_id") or ""),
                allocation_id=str(event.get("allocation_id") or ""),
                strategy_id=str(event.get("strategy_id") or "unknown"),
                strategy_version=str(event.get("strategy_version") or "unknown"),
                strategy_family=str(event.get("strategy_family") or "unknown"),
                asset=str(event.get("asset") or "").upper(),
                venue=str(event.get("venue") or "unknown"),
                decision=str(event.get("decision") or "allow"),
                reason_codes_json=list(event.get("reason_codes") or []),
                created_at_ms=int(event.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(event.get("metadata") or {})),
            ),
            "event_id",
        )

    async def list_allocation_diversity_events(self, *, strategy_id: str | None = None, decision: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if strategy_id:
            filters.append(AllocationDiversityEventRecord.strategy_id == strategy_id)
        if decision:
            filters.append(AllocationDiversityEventRecord.decision == decision)
        return await self._list_engine_records(AllocationDiversityEventRecord, order_by=AllocationDiversityEventRecord.created_at_ms, limit=limit, filters=filters)

    async def record_portfolio_concentration_event(self, event: dict[str, Any]) -> str | None:
        projected = event.get("metadata", {}).get("projected", {}) if isinstance(event.get("metadata"), dict) else {}
        return await self._merge_engine_record(
            PortfolioConcentrationEventRecord(
                event_id=str(event["event_id"]),
                candidate_id=str(event.get("candidate_id") or ""),
                allocation_id=event.get("allocation_id"),
                strategy_id=str(event.get("strategy_id") or "unknown"),
                strategy_version=str(event.get("strategy_version") or "unknown"),
                strategy_family=str(event.get("strategy_family") or "unknown"),
                asset=str(event.get("asset") or "").upper(),
                venue=str(event.get("venue") or "unknown"),
                decision=str(event.get("decision") or "allow"),
                reason_codes_json=list(event.get("reason_codes") or []),
                strategy_share_pct=float(event.get("strategy_share_pct") or projected.get("strategy_share_pct") or 0),
                family_share_pct=float(event.get("family_share_pct") or projected.get("family_share_pct") or 0),
                symbol_strategy_share_pct=float(event.get("symbol_strategy_share_pct") or projected.get("symbol_strategy_share_pct") or 0),
                created_at_ms=int(event.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(event.get("metadata") or {})),
            ),
            "event_id",
        )

    async def list_portfolio_concentration_events(self, *, strategy_id: str | None = None, decision: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if strategy_id:
            filters.append(PortfolioConcentrationEventRecord.strategy_id == strategy_id)
        if decision:
            filters.append(PortfolioConcentrationEventRecord.decision == decision)
        return await self._list_engine_records(PortfolioConcentrationEventRecord, order_by=PortfolioConcentrationEventRecord.created_at_ms, limit=limit, filters=filters)

    async def upsert_candidate_evidence_link(self, link: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            CandidateEvidenceLinkRecord(
                link_id=str(link["link_id"]),
                candidate_id=str(link.get("candidate_id") or ""),
                strategy_id=str(link.get("strategy_id") or "unknown"),
                strategy_version=str(link.get("strategy_version") or "unknown"),
                strategy_family=str(link.get("strategy_family") or "unknown"),
                asset=str(link.get("asset") or "").upper(),
                venue=str(link.get("venue") or "unknown"),
                horizon=str(link.get("horizon") or ""),
                regime_snapshot_id=str(link.get("regime_snapshot_id") or ""),
                feature_snapshot_id=str(link.get("feature_snapshot_id") or ""),
                risk_decision_id=link.get("risk_decision_id"),
                council_review_id=link.get("council_review_id"),
                replay_context_id=link.get("replay_context_id"),
                allocation_id=link.get("allocation_id"),
                packet_id=link.get("packet_id"),
                outcome_window_ids_json=list(link.get("outcome_window_ids") or []),
                created_at_ms=int(link.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(link.get("metadata") or {})),
            ),
            "link_id",
        )

    async def list_candidate_evidence_links(self, *, candidate_id: str | None = None, strategy_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if candidate_id:
            filters.append(CandidateEvidenceLinkRecord.candidate_id == candidate_id)
        if strategy_id:
            filters.append(CandidateEvidenceLinkRecord.strategy_id == strategy_id)
        return await self._list_engine_records(CandidateEvidenceLinkRecord, order_by=CandidateEvidenceLinkRecord.created_at_ms, limit=limit, filters=filters)

    async def upsert_candidate_outcome_attribution(self, item: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            CandidateOutcomeAttributionRecord(
                attribution_id=str(item["attribution_id"]),
                candidate_id=str(item.get("candidate_id") or ""),
                strategy_id=str(item.get("strategy_id") or "unknown"),
                strategy_version=str(item.get("strategy_version") or "unknown"),
                strategy_family=str(item.get("strategy_family") or "unknown"),
                asset=str(item.get("asset") or "").upper(),
                venue=str(item.get("venue") or "unknown"),
                side=str(item.get("side") or ""),
                candidate_horizon=str(item.get("candidate_horizon") or item.get("horizon") or ""),
                regime_snapshot_id=str(item.get("regime_snapshot_id") or ""),
                feature_snapshot_id=str(item.get("feature_snapshot_id") or ""),
                risk_decision_id=item.get("risk_decision_id"),
                council_review_id=item.get("council_review_id"),
                replay_context_id=item.get("replay_context_id"),
                allocation_id=item.get("allocation_id"),
                outcome_window=str(item.get("outcome_window") or "unknown"),
                window_start_ms=int(item.get("window_start_ms") or 0),
                window_end_ms=int(item.get("window_end_ms") or 0),
                entry_px=float(item.get("entry_px") or 0),
                mark_px=item.get("mark_px"),
                gross_return_bps=float(item.get("gross_return_bps") or 0),
                fees_bps=float(item.get("fees_bps") or 0),
                slippage_bps=float(item.get("slippage_bps") or 0),
                funding_bps=float(item.get("funding_bps") or 0),
                net_return_bps=float(item.get("net_return_bps") or 0),
                realized_r=float(item.get("realized_r") or 0),
                mfe_bps=float(item.get("mfe_bps") or 0),
                mae_bps=float(item.get("mae_bps") or 0),
                risk_decision=str(item.get("risk_decision") or "unknown"),
                council_decision=str(item.get("council_decision") or "unknown"),
                allocation_status=str(item.get("allocation_status") or "unknown"),
                terminal_state=str(item.get("terminal_state") or "pending"),
                quality_flags_json=list(item.get("quality_flags") or []),
                created_at_ms=int(item.get("created_at_ms") or 0),
                updated_at_ms=int(item.get("updated_at_ms") or item.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(item.get("metadata") or {})),
            ),
            "attribution_id",
        )

    async def list_candidate_outcome_attributions(
        self,
        *,
        candidate_id: str | None = None,
        strategy_id: str | None = None,
        outcome_window: str | None = None,
        terminal_state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = []
        if candidate_id:
            filters.append(CandidateOutcomeAttributionRecord.candidate_id == candidate_id)
        if strategy_id:
            filters.append(CandidateOutcomeAttributionRecord.strategy_id == strategy_id)
        if outcome_window:
            filters.append(CandidateOutcomeAttributionRecord.outcome_window == outcome_window)
        if terminal_state:
            filters.append(CandidateOutcomeAttributionRecord.terminal_state == terminal_state)
        return await self._list_engine_records(CandidateOutcomeAttributionRecord, order_by=CandidateOutcomeAttributionRecord.window_end_ms, limit=limit, filters=filters)

    async def record_replay_result_link(self, link: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            ReplayResultLinkRecord(
                link_id=str(link["link_id"]),
                replay_id=str(link.get("replay_id") or ""),
                candidate_id=link.get("candidate_id"),
                strategy_id=str(link.get("strategy_id") or "unknown"),
                strategy_version=str(link.get("strategy_version") or "unknown"),
                strategy_family=str(link.get("strategy_family") or "unknown"),
                asset=str(link.get("asset") or "GLOBAL").upper(),
                venue=str(link.get("venue") or "unknown"),
                regime_snapshot_id=link.get("regime_snapshot_id"),
                horizon=str(link.get("horizon") or "unknown"),
                outcome_window=str(link.get("outcome_window") or "unknown"),
                created_at_ms=int(link.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(link.get("metadata") or {})),
            ),
            "link_id",
        )

    async def list_replay_result_links(self, *, replay_id: str | None = None, candidate_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if replay_id:
            filters.append(ReplayResultLinkRecord.replay_id == replay_id)
        if candidate_id:
            filters.append(ReplayResultLinkRecord.candidate_id == candidate_id)
        return await self._list_engine_records(ReplayResultLinkRecord, order_by=ReplayResultLinkRecord.created_at_ms, limit=limit, filters=filters)

    async def record_candidate_trade_packet(self, packet: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            CandidateTradePacketRecord(
                packet_id=str(packet["packet_id"]),
                candidate_id=str(packet.get("candidate_id") or ""),
                strategy_id=str(packet.get("strategy_id") or "unknown"),
                strategy_version=str(packet.get("strategy_version") or "unknown"),
                strategy_family=str(packet.get("strategy_family") or "unknown"),
                asset=str(packet.get("asset") or "").upper(),
                side=str(packet.get("side") or ""),
                horizon=str(packet.get("horizon") or ""),
                feature_snapshot_id=str(packet.get("feature_snapshot_id") or ""),
                regime_snapshot_id=str(packet.get("regime_snapshot_id") or ""),
                packet_json=redact_secrets(dict(packet)),
                created_at_ms=int(packet.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(packet.get("metadata") or {})),
            ),
            "packet_id",
        )

    async def list_candidate_trade_packets(self, *, candidate_id: str | None = None, strategy_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if candidate_id:
            filters.append(CandidateTradePacketRecord.candidate_id == candidate_id)
        if strategy_id:
            filters.append(CandidateTradePacketRecord.strategy_id == strategy_id)
        return await self._list_engine_records(CandidateTradePacketRecord, order_by=CandidateTradePacketRecord.created_at_ms, limit=limit, filters=filters)

    async def record_council_review(self, review: dict[str, Any]) -> str | None:
        review_id = await self._merge_engine_record(
            CouncilReviewRecord(
                review_id=str(review["review_id"]),
                packet_id=str(review.get("packet_id") or ""),
                candidate_id=str(review.get("candidate_id") or ""),
                strategy_id=str(review.get("strategy_id") or "unknown"),
                decision=str(review.get("decision") or "needs_more_evidence"),
                vetoes_json=list(review.get("vetoes") or []),
                warnings_json=list(review.get("warnings") or []),
                required_evidence_json=list(review.get("required_evidence") or []),
                regime_fit_score=float(review.get("regime_fit_score") or 0),
                strategy_regime_score=float(review.get("strategy_regime_score") or 0),
                portfolio_impact_score=float(review.get("portfolio_impact_score") or 0),
                created_at_ms=int(review.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(review.get("metadata") or {})),
            ),
            "review_id",
        )
        for vote in review.get("votes") or []:
            await self.record_council_vote(vote)
        return review_id

    async def list_council_reviews(self, *, candidate_id: str | None = None, strategy_id: str | None = None, decision: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if candidate_id:
            filters.append(CouncilReviewRecord.candidate_id == candidate_id)
        if strategy_id:
            filters.append(CouncilReviewRecord.strategy_id == strategy_id)
        if decision:
            filters.append(CouncilReviewRecord.decision == decision)
        return await self._list_engine_records(CouncilReviewRecord, order_by=CouncilReviewRecord.created_at_ms, limit=limit, filters=filters)

    async def record_council_vote(self, vote: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            CouncilVoteRecord(
                vote_id=str(vote["vote_id"]),
                review_id=str(vote.get("review_id") or ""),
                role=str(vote.get("role") or "unknown"),
                decision=str(vote.get("decision") or "allow"),
                rationale=str(vote.get("rationale") or ""),
                vetoes_json=list(vote.get("vetoes") or []),
                warnings_json=list(vote.get("warnings") or []),
                required_evidence_json=list(vote.get("required_evidence") or []),
                scores_json=redact_secrets(dict(vote.get("scores") or {})),
                created_at_ms=int(vote.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(vote.get("metadata") or {})),
            ),
            "vote_id",
        )

    async def list_council_votes(self, *, review_id: str | None = None, role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if review_id:
            filters.append(CouncilVoteRecord.review_id == review_id)
        if role:
            filters.append(CouncilVoteRecord.role == role)
        return await self._list_engine_records(CouncilVoteRecord, order_by=CouncilVoteRecord.created_at_ms, limit=limit, filters=filters)

    async def upsert_strategy_regime_performance(self, item: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            StrategyRegimePerformanceRecord(
                performance_id=str(item["performance_id"]),
                strategy_id=str(item.get("strategy_id") or "unknown"),
                strategy_version=str(item.get("strategy_version") or "unknown"),
                strategy_family=str(item.get("strategy_family") or "unknown"),
                regime_label=str(item.get("regime_label") or "unknown"),
                asset=str(item.get("asset") or "GLOBAL").upper(),
                venue=str(item.get("venue") or "unknown"),
                outcome_window=str(item.get("outcome_window") or "unknown"),
                window_start_ms=int(item.get("window_start_ms") or 0),
                window_end_ms=int(item.get("window_end_ms") or 0),
                candidate_count=int(item.get("candidate_count") or 0),
                allocation_count=int(item.get("allocation_count") or 0),
                risk_reject_count=int(item.get("risk_reject_count") or 0),
                council_veto_count=int(item.get("council_veto_count") or 0),
                concentration_event_count=int(item.get("concentration_event_count") or 0),
                win_rate_pct=float(item.get("win_rate_pct") or 0),
                avg_net_ev_bps=float(item.get("avg_net_ev_bps") or 0),
                avg_net_return_bps=float(item.get("avg_net_return_bps") or 0),
                avg_realized_r=float(item.get("avg_realized_r") or 0),
                avg_drawdown_bps=float(item.get("avg_drawdown_bps") or 0),
                avg_fees_bps=float(item.get("avg_fees_bps") or 0),
                avg_slippage_bps=float(item.get("avg_slippage_bps") or 0),
                realized_pnl_usd=float(item.get("realized_pnl_usd") or 0),
                score=float(item.get("score") or 0),
                created_at_ms=int(item.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(item.get("metadata") or {})),
            ),
            "performance_id",
        )

    async def list_strategy_regime_performance(self, *, strategy_id: str | None = None, regime_label: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if strategy_id:
            filters.append(StrategyRegimePerformanceRecord.strategy_id == strategy_id)
        if regime_label:
            filters.append(StrategyRegimePerformanceRecord.regime_label == regime_label)
        return await self._list_engine_records(StrategyRegimePerformanceRecord, order_by=StrategyRegimePerformanceRecord.window_end_ms, limit=limit, filters=filters)

    async def upsert_bandit_policy_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            BanditPolicySnapshotRecord(
                policy_id=str(snapshot["policy_id"]),
                policy_version=str(snapshot.get("policy_version") or "unknown"),
                status=str(snapshot.get("status") or "report_only"),
                trained_window_start_ms=int(snapshot.get("trained_window_start_ms") or 0),
                trained_window_end_ms=int(snapshot.get("trained_window_end_ms") or 0),
                context_features_json=list(snapshot.get("context_features") or []),
                arms_json=list(snapshot.get("arms") or []),
                policy_json=redact_secrets(dict(snapshot.get("policy_json") or {})),
                created_at_ms=int(snapshot.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(snapshot.get("metadata") or {})),
            ),
            "policy_id",
        )

    async def record_bandit_recommendation(self, recommendation: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            BanditRecommendationRecord(
                recommendation_id=str(recommendation["recommendation_id"]),
                policy_id=str(recommendation.get("policy_id") or "unknown"),
                strategy_id=str(recommendation.get("strategy_id") or "unknown"),
                asset=str(recommendation.get("asset") or "GLOBAL").upper(),
                regime_label=str(recommendation.get("regime_label") or "unknown"),
                recommendation=str(recommendation.get("recommendation") or ""),
                confidence=float(recommendation.get("confidence") or 0),
                expected_score_delta=float(recommendation.get("expected_score_delta") or 0),
                auto_apply_allowed=bool(recommendation.get("auto_apply_allowed", False)),
                created_at_ms=int(recommendation.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(recommendation.get("metadata") or {})),
            ),
            "recommendation_id",
        )

    async def list_bandit_recommendations(self, *, strategy_id: str | None = None, policy_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if strategy_id:
            filters.append(BanditRecommendationRecord.strategy_id == strategy_id)
        if policy_id:
            filters.append(BanditRecommendationRecord.policy_id == policy_id)
        return await self._list_engine_records(BanditRecommendationRecord, order_by=BanditRecommendationRecord.created_at_ms, limit=limit, filters=filters)

    async def record_evidence_pack(self, pack: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            EvidencePackRecord(
                evidence_pack_id=str(pack["evidence_pack_id"]),
                candidate_id=str(pack.get("candidate_id") or ""),
                strategy_id=str(pack.get("strategy_id") or ""),
                asset=str(pack.get("asset") or "").upper(),
                side=str(pack.get("side") or ""),
                horizon=str(pack.get("horizon") or ""),
                feature_snapshot_id=str(pack.get("feature_snapshot_id") or ""),
                pack_json=redact_secrets(dict(pack)),
                created_at_ms=int(pack.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(pack.get("metadata") or {})),
            ),
            "evidence_pack_id",
        )

    async def get_evidence_pack(self, evidence_pack_id: str) -> dict[str, Any] | None:
        return await self._get_engine_record(EvidencePackRecord, evidence_pack_id)

    async def record_debate_decision(self, decision: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            DebateDecisionRecord(
                debate_decision_id=str(decision["debate_decision_id"]),
                evidence_pack_id=str(decision.get("evidence_pack_id") or ""),
                candidate_id=str(decision.get("candidate_id") or ""),
                decision=str(decision.get("decision") or "require_more_data"),
                confidence_adjustment=float(decision.get("confidence_adjustment") or 0),
                max_size_multiplier=float(decision.get("max_size_multiplier") if decision.get("max_size_multiplier") is not None else 1.0),
                reason_codes_json=list(decision.get("reason_codes") or []),
                required_invalidation_checks_json=list(decision.get("required_invalidation_checks") or []),
                audit_summary=str(decision.get("audit_summary") or ""),
                role_outputs_json=redact_secrets(list(decision.get("role_outputs") or [])),
                judge_model=decision.get("judge_model"),
                created_at_ms=int(decision.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(decision.get("metadata") or {})),
            ),
            "debate_decision_id",
        )

    async def list_debate_decisions(self, *, candidate_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = [DebateDecisionRecord.candidate_id == candidate_id] if candidate_id else []
        return await self._list_engine_records(DebateDecisionRecord, order_by=DebateDecisionRecord.created_at_ms, limit=limit, filters=filters)

    async def record_order_intent(self, intent: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            OrderIntentRecord(
                intent_id=str(intent["intent_id"]),
                parent_candidate_id=str(intent.get("parent_candidate_id") or ""),
                portfolio_decision_id=str(intent.get("portfolio_decision_id") or ""),
                asset=str(intent.get("asset") or "").upper(),
                asset_class=str(intent.get("asset_class") or "unknown"),
                venue=str(intent.get("venue") or "unknown"),
                side=str(intent.get("side") or "buy"),
                order_type=str(intent.get("order_type") or "marketable_limit"),
                time_in_force=str(intent.get("time_in_force") or "ioc"),
                target_size=float(intent.get("target_size") or 0),
                target_notional_usd=float(intent.get("target_notional_usd") or 0),
                max_slippage_bps=float(intent.get("max_slippage_bps") or 0),
                price_limit=intent.get("price_limit"),
                reduce_only=bool(intent.get("reduce_only", False)),
                post_only=bool(intent.get("post_only", False)),
                deadline_ts_ms=int(intent.get("deadline_ts_ms") or 0),
                strategy_id=str(intent.get("strategy_id") or "unknown"),
                model_version_id=str(intent.get("model_version_id") or "unknown"),
                config_version_id=str(intent.get("config_version_id") or "unknown"),
                risk_budget_id=str(intent.get("risk_budget_id") or "unknown"),
                execution_mode=str(intent.get("execution_mode") or "paper"),
                created_at_ms=int(intent.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(intent.get("metadata") or {})),
            ),
            "intent_id",
        )

    async def list_order_intents(self, *, execution_mode: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = [OrderIntentRecord.execution_mode == execution_mode] if execution_mode else []
        return await self._list_engine_records(OrderIntentRecord, order_by=OrderIntentRecord.created_at_ms, limit=limit, filters=filters)

    async def record_execution_report(self, report: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            ExecutionReportRecord(
                report_id=str(report["report_id"]),
                intent_id=str(report.get("intent_id") or ""),
                execution_mode=str(report.get("execution_mode") or "paper"),
                status=str(report.get("status") or "accepted"),
                requested_size=float(report.get("requested_size") or 0),
                filled_size=float(report.get("filled_size") or 0),
                avg_fill_px=report.get("avg_fill_px"),
                fees_usd=float(report.get("fees_usd") or 0),
                slippage_bps=float(report.get("slippage_bps") or 0),
                market_impact_bps=report.get("market_impact_bps"),
                adapter=str(report.get("adapter") or report.get("execution_mode") or "paper"),
                assumptions_json=redact_secrets(dict(report.get("assumptions") or {})),
                created_at_ms=int(report.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(report.get("metadata") or {})),
            ),
            "report_id",
        )

    async def list_execution_reports(self, *, intent_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = [ExecutionReportRecord.intent_id == intent_id] if intent_id else []
        return await self._list_engine_records(ExecutionReportRecord, order_by=ExecutionReportRecord.created_at_ms, limit=limit, filters=filters)

    async def record_position_thesis(self, thesis: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            PositionThesisRecord(
                position_id=str(thesis["position_id"]),
                entry_candidate_id=str(thesis.get("entry_candidate_id") or ""),
                strategy_id=str(thesis.get("strategy_id") or ""),
                asset=str(thesis.get("asset") or "").upper(),
                asset_class=str(thesis.get("asset_class") or "unknown"),
                venue=str(thesis.get("venue") or "unknown"),
                side=str(thesis.get("side") or "long"),
                entry_reason=str(thesis.get("entry_reason") or ""),
                expected_horizon=str(thesis.get("expected_horizon") or ""),
                stop=float(thesis.get("stop") or 0),
                targets_json=list(thesis.get("targets") or []),
                invalidation_rules_json=list(thesis.get("invalidation_rules") or []),
                thesis_features_at_entry_json=redact_secrets(dict(thesis.get("thesis_features_at_entry") or {})),
                current_thesis_score=float(thesis.get("current_thesis_score") if thesis.get("current_thesis_score") is not None else 1.0),
                degradation_reasons_json=list(thesis.get("degradation_reasons") or []),
                position_state=str(thesis.get("position_state") or "proposed"),
                execution_report_ids_json=list(thesis.get("execution_report_ids") or []),
                opened_at_ms=thesis.get("opened_at_ms"),
                updated_at_ms=int(thesis.get("updated_at_ms") or 0),
                closed_at_ms=thesis.get("closed_at_ms"),
                metadata_json=redact_secrets(dict(thesis.get("metadata") or {})),
            ),
            "position_id",
        )

    async def list_position_theses(self, *, state: str | None = None, asset: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if state:
            filters.append(PositionThesisRecord.position_state == state)
        if asset:
            filters.append(PositionThesisRecord.asset == asset.upper())
        return await self._list_engine_records(PositionThesisRecord, order_by=PositionThesisRecord.updated_at_ms, limit=limit, filters=filters)

    async def record_reconciliation_run(self, run: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            ReconciliationRunRecord(
                reconciliation_id=str(run["reconciliation_id"]),
                execution_mode=str(run.get("execution_mode") or "paper"),
                status=str(run.get("status") or "ok"),
                expected_positions_json=redact_secrets(list(run.get("expected_positions") or [])),
                observed_positions_json=redact_secrets(list(run.get("observed_positions") or [])),
                mismatches_json=redact_secrets(list(run.get("mismatches") or [])),
                started_at_ms=int(run.get("started_at_ms") or 0),
                completed_at_ms=run.get("completed_at_ms"),
                metadata_json=redact_secrets(dict(run.get("metadata") or {})),
            ),
            "reconciliation_id",
        )

    async def list_reconciliation_runs(self, *, execution_mode: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = [ReconciliationRunRecord.execution_mode == execution_mode] if execution_mode else []
        return await self._list_engine_records(ReconciliationRunRecord, order_by=ReconciliationRunRecord.started_at_ms, limit=limit, filters=filters)

    async def record_pnl_attribution(self, item: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            PnLAttributionRecord(
                attribution_id=str(item["attribution_id"]),
                position_id=item.get("position_id"),
                candidate_id=item.get("candidate_id"),
                strategy_id=str(item.get("strategy_id") or "unknown"),
                asset=str(item.get("asset") or "").upper(),
                window_start_ms=int(item.get("window_start_ms") or 0),
                window_end_ms=int(item.get("window_end_ms") or 0),
                alpha_pnl_usd=float(item.get("alpha_pnl_usd") or 0),
                timing_pnl_usd=float(item.get("timing_pnl_usd") or 0),
                execution_pnl_usd=float(item.get("execution_pnl_usd") or 0),
                fees_usd=float(item.get("fees_usd") or 0),
                funding_usd=float(item.get("funding_usd") or 0),
                residual_pnl_usd=float(item.get("residual_pnl_usd") or 0),
                total_pnl_usd=float(item.get("total_pnl_usd") or 0),
                metrics_json=redact_secrets(dict(item.get("metrics") or {})),
                metadata_json=redact_secrets(dict(item.get("metadata") or {})),
            ),
            "attribution_id",
        )

    async def list_pnl_attribution(self, *, strategy_id: str | None = None, asset: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if strategy_id:
            filters.append(PnLAttributionRecord.strategy_id == strategy_id)
        if asset:
            filters.append(PnLAttributionRecord.asset == asset.upper())
        return await self._list_engine_records(PnLAttributionRecord, order_by=PnLAttributionRecord.window_end_ms, limit=limit, filters=filters)

    async def record_kill_switch_event(self, event: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            KillSwitchEventRecord(
                event_id=str(event["event_id"]),
                scope=str(event.get("scope") or "global"),
                action=str(event.get("action") or "triggered"),
                triggered_by=str(event.get("triggered_by") or "unknown"),
                reason=str(event.get("reason") or ""),
                affected_assets_json=list(event.get("affected_assets") or []),
                affected_strategies_json=list(event.get("affected_strategies") or []),
                block_new_orders=bool(event.get("block_new_orders", True)),
                cancel_open_orders=bool(event.get("cancel_open_orders", False)),
                freeze_config_changes=bool(event.get("freeze_config_changes", True)),
                created_at_ms=int(event.get("created_at_ms") or 0),
                expires_at_ms=event.get("expires_at_ms"),
                metadata_json=redact_secrets(dict(event.get("metadata") or {})),
            ),
            "event_id",
        )

    async def record_model_version(self, version: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            ModelVersionRecord(
                model_version_id=str(version["model_version_id"]),
                model_type=str(version.get("model_type") or "unknown"),
                artifact_uri=str(version.get("artifact_uri") or ""),
                training_data_hash=str(version.get("training_data_hash") or ""),
                feature_schema_hash=str(version.get("feature_schema_hash") or ""),
                metrics_json=redact_secrets(dict(version.get("metrics") or {})),
                status=str(version.get("status") or "candidate"),
                approved_by=version.get("approved_by"),
                approved_at_ms=version.get("approved_at_ms"),
                created_at_ms=int(version.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(version.get("metadata") or {})),
            ),
            "model_version_id",
        )

    async def list_model_versions(self, *, status: str | None = None, model_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = []
        if status:
            filters.append(ModelVersionRecord.status == status)
        if model_type:
            filters.append(ModelVersionRecord.model_type == model_type)
        return await self._list_engine_records(ModelVersionRecord, order_by=ModelVersionRecord.created_at_ms, limit=limit, filters=filters)

    async def record_model_training_run(self, run: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            ModelTrainingRunRecord(
                training_run_id=str(run["training_run_id"]),
                model_version_id=run.get("model_version_id"),
                model_type=str(run.get("model_type") or "unknown"),
                dataset_start_ms=int(run.get("dataset_start_ms") or 0),
                dataset_end_ms=int(run.get("dataset_end_ms") or 0),
                training_data_hash=str(run.get("training_data_hash") or ""),
                feature_schema_hash=str(run.get("feature_schema_hash") or ""),
                code_version=run.get("code_version"),
                metrics_json=redact_secrets(dict(run.get("metrics") or {})),
                artifact_uri=run.get("artifact_uri"),
                status=str(run.get("status") or "started"),
                created_at_ms=int(run.get("created_at_ms") or 0),
                completed_at_ms=run.get("completed_at_ms"),
                metadata_json=redact_secrets(dict(run.get("metadata") or {})),
            ),
            "training_run_id",
        )

    async def record_feature_schema_version(self, version: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            FeatureSchemaVersionRecord(
                feature_schema_version_id=str(version["feature_schema_version_id"]),
                schema_hash=str(version.get("schema_hash") or ""),
                feature_names_json=list(version.get("feature_names") or []),
                feature_definitions_json=redact_secrets(dict(version.get("feature_definitions") or {})),
                created_at_ms=int(version.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(version.get("metadata") or {})),
            ),
            "feature_schema_version_id",
        )

    async def record_retention_run(self, run: dict[str, Any]) -> str | None:
        return await self._merge_engine_record(
            RetentionRunRecord(
                retention_run_id=str(run["retention_run_id"]),
                status=str(run.get("status") or "started"),
                started_at_ms=int(run.get("started_at_ms") or 0),
                completed_at_ms=run.get("completed_at_ms"),
                deleted_counts_json=dict(run.get("deleted_counts") or {}),
                rollup_counts_json=dict(run.get("rollup_counts") or {}),
                caveats_json=list(run.get("caveats") or []),
                metadata_json=redact_secrets(dict(run.get("metadata") or {})),
            ),
            "retention_run_id",
        )

    async def list_retention_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self._list_engine_records(RetentionRunRecord, order_by=RetentionRunRecord.started_at_ms, limit=limit)

    async def _merge_engine_record(self, item: Any, primary_key_attr: str) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                merged = await session.merge(item)
                await session.commit()
                return str(getattr(merged, primary_key_attr))
        except Exception as exc:  # pragma: no cover - engine persistence should not break runtime loop
            log.warning("engine_record_merge_failed", table=getattr(item, "__tablename__", "unknown"), error=type(exc).__name__)
            return None

    async def _get_engine_record(self, record_cls: Any, primary_key: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(record_cls, primary_key)
                return _engine_record_to_dict(item) if item is not None else None
        except Exception as exc:  # pragma: no cover
            log.warning("engine_record_get_failed", table=getattr(record_cls, "__tablename__", "unknown"), error=type(exc).__name__)
            return None

    async def _list_engine_records(self, record_cls: Any, *, order_by: Any, limit: int = 100, filters: list[Any] | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                stmt = select(record_cls)
                for condition in filters or []:
                    stmt = stmt.where(condition)
                stmt = stmt.order_by(order_by.desc()).limit(limit)
                result = await session.execute(stmt)
                return [_engine_record_to_dict(item) for item in result.scalars().all()]
        except Exception as exc:  # pragma: no cover
            log.warning("engine_record_list_failed", table=getattr(record_cls, "__tablename__", "unknown"), error=type(exc).__name__)
            return []

    async def record_memory_injection_event(self, event: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = MemoryInjectionEventRecord(
                    run_id=event.get("run_id"),
                    role=str(event.get("role") or "unknown"),
                    context_type=str(event.get("context_type") or "unknown"),
                    memory_ids_json=list(event.get("memory_ids") or []),
                    blocked_memory_ids_json=list(event.get("blocked_memory_ids") or []),
                    policy_decision_json=redact_secrets(dict(event.get("policy_decision") or {})),
                    created_at_ms=int(event.get("created_at_ms") or 0),
                    metadata_json=redact_secrets(dict(event.get("metadata") or {})),
                )
                session.add(item)
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("memory_injection_event_record_failed", role=event.get("role"), error=type(exc).__name__)
            return None

    async def list_memory_injection_events(self, limit: int = 100, role: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                stmt = select(MemoryInjectionEventRecord).order_by(MemoryInjectionEventRecord.created_at_ms.desc()).limit(limit)
                if role:
                    stmt = stmt.where(MemoryInjectionEventRecord.role == role)
                result = await session.execute(stmt)
                return [_memory_injection_event_to_dict(item) for item in result.scalars().all()]
        except Exception as exc:  # pragma: no cover
            log.warning("memory_injection_events_list_failed", error=type(exc).__name__)
            return []

    async def create_decision_run(
        self,
        prompt: str,
        route: dict[str, Any],
        selected_roles: list[str],
        actor: str = "",
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                run = DecisionRun(
                    actor=actor,
                    prompt=str(redact_secrets(prompt)),
                    route=redact_secrets(route),
                    selected_roles=selected_roles,
                    status="started",
                )
                session.add(run)
                await session.flush()
                await session.commit()
                return run.id
        except Exception as exc:  # pragma: no cover
            log.warning("decision_run_create_failed", error=type(exc).__name__)
            return None

    async def update_decision_run_context(self, run_id: str | None, context_snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                run = await session.get(DecisionRun, run_id)
                if run is not None:
                    run.context_snapshot = redact_secrets(context_snapshot)
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_run_context_update_failed", run_id=run_id, error=type(exc).__name__)

    async def record_decision_role_output(
        self,
        run_id: str | None,
        role: str,
        round_index: int,
        model: str | None,
        provider: str | None,
        status: str,
        output_json: dict[str, Any],
        raw_content: str = "",
        latency_ms: int | None = None,
    ) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(
                    DecisionRoleOutput(
                        run_id=run_id,
                        role=role,
                        round_index=round_index,
                        model=model,
                        provider=provider,
                        status=status,
                        output_json=redact_secrets(output_json),
                        raw_content=str(redact_secrets(raw_content)),
                        latency_ms=latency_ms,
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_role_output_record_failed", role=role, error=type(exc).__name__)

    async def record_decision_state_snapshot(
        self,
        run_id: str | None,
        round_index: int,
        node: str,
        state_json: dict[str, Any],
    ) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                session.add(
                    DecisionStateSnapshot(
                        run_id=run_id,
                        round_index=round_index,
                        node=node,
                        state_json=redact_secrets(state_json),
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_state_snapshot_record_failed", node=node, error=type(exc).__name__)

    async def complete_decision_run(
        self,
        run_id: str | None,
        status: str,
        round_count: int,
        final_summary: str = "",
        proposal_id: str | None = None,
    ) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        try:
            async with self.sessionmaker() as session:
                run = await session.get(DecisionRun, run_id)
                if run is not None:
                    run.status = status
                    run.round_count = round_count
                    run.final_summary = final_summary
                    run.proposal_id = proposal_id
                    run.completed_at = datetime.now(UTC)
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("decision_run_complete_failed", run_id=run_id, error=type(exc).__name__)

    async def record_trade_proposal(
        self,
        run_id: str | None,
        status: str,
        coin: str | None,
        side: str | None,
        proposal: dict[str, Any],
        content: str = "",
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = TradeProposalRecord(
                    run_id=run_id,
                    status=status,
                    coin=coin,
                    side=side,
                    proposal_json=redact_secrets(proposal),
                    content=str(redact_secrets(content)),
                )
                session.add(item)
                await session.flush()
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("trade_proposal_record_failed", error=type(exc).__name__)
            return None

    async def update_trade_proposal(self, proposal_id: str, proposal: dict[str, Any], content: str = "") -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(TradeProposalRecord, proposal_id)
                if item is not None:
                    item.proposal_json = redact_secrets(proposal)
                    if content:
                        item.content = str(redact_secrets(content))
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("trade_proposal_update_failed", proposal_id=proposal_id, error=type(exc).__name__)

    async def get_trade_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(TradeProposalRecord, proposal_id)
                if item is None:
                    return None
                return {
                    "id": item.id,
                    "run_id": item.run_id,
                    "status": item.status,
                    "coin": item.coin,
                    "side": item.side,
                    "proposal": item.proposal_json,
                    "content": item.content,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
        except Exception as exc:  # pragma: no cover
            log.warning("trade_proposal_get_failed", proposal_id=proposal_id, error=type(exc).__name__)
            return None

    async def create_position_tracker(self, plan: PositionTrackingPlan, proposal_id: str | None = None, run_id: str | None = None) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            plan_json = redact_secrets(plan.model_dump(mode="json"))
            async with self.sessionmaker() as session:
                tracker = PositionTracker(
                    id=plan.id,
                    proposal_id=proposal_id or plan.proposal_id,
                    run_id=run_id or plan.run_id,
                    source=str(plan.metadata.get("source") or "auto_high_stakes"),
                    status=plan.status,
                    coin=plan.coin.upper(),
                    side=plan.side,
                    entry_px=plan.entry,
                    stop_px=plan.stop,
                    take_profit_px=plan.take_profit,
                    current_px=plan.current_price_at_arm,
                    last_px=None,
                    last_price_at_ms=None,
                    price_source=plan.price_source,
                    expires_at=_datetime_from_ms(plan.expires_at_ms),
                    discord_guild_id=plan.discord_guild_id,
                    discord_channel_id=plan.discord_channel_id,
                    discord_thread_id=plan.discord_thread_id,
                    discord_user_id=plan.discord_user_id,
                    plan_json=plan_json,
                    metadata_json=redact_secrets(plan.metadata),
                    updated_at=datetime.now(UTC),
                )
                session.add(tracker)
                for level in plan.levels:
                    session.add(
                        TrackedLevel(
                            id=level.id,
                            tracker_id=plan.id,
                            kind=level.kind,
                            label=level.label,
                            price=level.price,
                            direction=level.direction,
                            terminal=level.terminal,
                            severity=level.severity,
                            armed=level.armed,
                            hit_count=level.hit_count,
                            rearm_band_bps=level.rearm_band_bps,
                            metadata_json=redact_secrets(level.metadata),
                        )
                    )
                await session.commit()
                return tracker.id
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_create_failed", coin=plan.coin, error=type(exc).__name__)
            return None

    async def get_active_position_trackers(self) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(select(PositionTracker).where(PositionTracker.status.in_(["pending", "active"])))
                trackers = list(result.scalars().all())
                return [await self._tracker_to_dict(session, item) for item in trackers]
        except Exception as exc:  # pragma: no cover
            log.warning("active_position_trackers_get_failed", error=type(exc).__name__)
            return []

    async def get_position_tracker(self, tracker_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = await session.get(PositionTracker, tracker_id)
                return await self._tracker_to_dict(session, item) if item is not None else None
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_get_failed", tracker_id=tracker_id, error=type(exc).__name__)
            return None

    async def list_position_trackers(
        self,
        status: str | None = None,
        coin: str | None = None,
        discord_thread_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                stmt = select(PositionTracker).order_by(PositionTracker.created_at.desc()).limit(limit)
                if status:
                    stmt = stmt.where(PositionTracker.status == status)
                if coin:
                    stmt = stmt.where(PositionTracker.coin == coin.upper())
                if discord_thread_id:
                    stmt = stmt.where(PositionTracker.discord_thread_id == discord_thread_id)
                result = await session.execute(stmt)
                trackers = list(result.scalars().all())
                return [await self._tracker_to_dict(session, item) for item in trackers]
        except Exception as exc:  # pragma: no cover
            log.warning("position_trackers_list_failed", error=type(exc).__name__)
            return []

    async def update_position_tracker_price(self, tracker_id: str, current_px: float, previous_px: float | None, timestamp_ms: int) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                tracker = await session.get(PositionTracker, tracker_id)
                if tracker is not None:
                    tracker.last_px = previous_px
                    tracker.current_px = current_px
                    tracker.last_price_at_ms = timestamp_ms
                    tracker.updated_at = datetime.now(UTC)
                    if tracker.status == "pending":
                        tracker.status = "active"
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_price_update_failed", tracker_id=tracker_id, error=type(exc).__name__)

    async def update_tracked_level_state(self, level_id: str, armed: bool, hit_count: int, last_triggered_at: datetime | None = None) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                level = await session.get(TrackedLevel, level_id)
                if level is not None:
                    level.armed = armed
                    level.hit_count = hit_count
                    level.last_triggered_at = last_triggered_at
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("tracked_level_update_failed", level_id=level_id, error=type(exc).__name__)

    async def set_position_tracker_expiry(self, tracker_id: str, expires_at_ms: int) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                tracker = await session.get(PositionTracker, tracker_id)
                if tracker is not None:
                    tracker.expires_at = _datetime_from_ms(expires_at_ms)
                    plan = dict(tracker.plan_json or {})
                    plan["expires_at_ms"] = expires_at_ms
                    tracker.plan_json = plan
                    tracker.updated_at = datetime.now(UTC)
                    await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_expiry_update_failed", tracker_id=tracker_id, error=type(exc).__name__)

    async def set_position_tracker_status(self, tracker_id: str, status: str, reason: str = "") -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                tracker = await session.get(PositionTracker, tracker_id)
                if tracker is not None:
                    tracker.status = status
                    tracker.updated_at = datetime.now(UTC)
                    if status in {"completed", "expired", "stopped", "error"}:
                        tracker.completed_at = datetime.now(UTC)
                    await session.commit()
            if reason:
                await self.record_tracking_event(tracker_id=tracker_id, event_type=f"tracker_{status}", coin="", payload={"reason": reason})
        except Exception as exc:  # pragma: no cover
            log.warning("position_tracker_status_update_failed", tracker_id=tracker_id, status=status, error=type(exc).__name__)

    async def record_tracking_event(
        self,
        tracker_id: str,
        event_type: str,
        coin: str,
        price: float | None = None,
        level_id: str | None = None,
        payload: dict[str, Any] | None = None,
        alert_destination: str | None = None,
        alert_status: str | None = None,
    ) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                event = TrackingEvent(
                    tracker_id=tracker_id,
                    level_id=level_id,
                    event_type=event_type,
                    coin=coin.upper() if coin else "",
                    price=price,
                    payload_json=redact_secrets(payload or {}),
                    alert_destination=alert_destination,
                    alert_status=alert_status,
                )
                session.add(event)
                await session.flush()
                await session.commit()
                return event.id
        except Exception as exc:  # pragma: no cover
            log.warning("tracking_event_record_failed", tracker_id=tracker_id, event_type=event_type, error=type(exc).__name__)
            return None

    async def list_tracking_events(self, tracker_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(select(TrackingEvent).where(TrackingEvent.tracker_id == tracker_id).order_by(TrackingEvent.created_at.desc()).limit(limit))
                return [_event_to_dict(item) for item in result.scalars().all()]
        except Exception as exc:  # pragma: no cover
            log.warning("tracking_events_list_failed", tracker_id=tracker_id, error=type(exc).__name__)
            return []

    async def record_autonomy_event(self, event_type: str, actor: str = "", symbol: str | None = None, payload: dict[str, Any] | None = None) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                event = AutonomyEvent(event_type=event_type, actor=actor, symbol=symbol.upper() if symbol else None, payload_json=redact_secrets(payload or {}))
                session.add(event)
                await session.flush()
                await session.commit()
                return event.id
        except Exception as exc:  # pragma: no cover
            log.warning("autonomy_event_record_failed", event_type=event_type, error=type(exc).__name__)
            return None

    async def upsert_market_asset(self, asset: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            symbol = str(asset["symbol"]).upper()
            item = await session.get(MarketAssetRecord, symbol)
            if item is None:
                item = MarketAssetRecord(symbol=symbol, display_name=str(asset.get("display_name") or symbol), kind=str(asset.get("kind") or "perp"), source=str(asset.get("source") or "top_volume"))
                session.add(item)
            item.display_name = str(asset.get("display_name") or symbol)
            item.kind = str(asset.get("kind") or "perp")
            item.source = str(asset.get("source") or "top_volume")
            item.dex = asset.get("dex")
            item.sz_decimals = asset.get("sz_decimals")
            item.max_leverage = asset.get("max_leverage")
            item.day_volume_usd = asset.get("day_volume_usd")
            item.metadata_json = redact_secrets(dict(asset.get("metadata") or {}))
            item.updated_at = datetime.now(UTC)
            await session.commit()

    async def list_market_assets(self) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(MarketAssetRecord).order_by(MarketAssetRecord.symbol.asc()))
            return [_market_asset_to_dict(item) for item in result.scalars().all()]

    async def record_market_observation(self, state: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                item = MarketObservation(
                    symbol=str(state.get("symbol") or "").upper(),
                    timestamp_ms=int(state.get("timestamp_ms") or 0),
                    mid=state.get("mid"),
                    mark=state.get("mark"),
                    oracle=state.get("oracle"),
                    funding_hourly=state.get("funding_hourly"),
                    open_interest=state.get("open_interest"),
                    day_volume_usd=state.get("day_volume_usd"),
                    features_json=redact_secrets(state),
                )
                session.add(item)
                await session.flush()
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("market_observation_record_failed", symbol=state.get("symbol"), error=type(exc).__name__)
            return None

    async def upsert_market_levels(self, levels: list[dict[str, Any]]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            for level in levels:
                item = await session.get(MarketLevelRecord, str(level["id"]))
                if item is None:
                    item = MarketLevelRecord(id=str(level["id"]), symbol=str(level.get("symbol") or "").upper(), kind=str(level.get("kind") or "support"), price=float(level.get("price") or 0), strength=float(level.get("strength") or 0), timeframe=str(level.get("timeframe") or ""), source=str(level.get("source") or "inferred"), first_seen_ms=int(level.get("first_seen_ms") or 0), last_seen_ms=int(level.get("last_seen_ms") or 0))
                    session.add(item)
                item.symbol = str(level.get("symbol") or item.symbol).upper()
                item.kind = str(level.get("kind") or item.kind)
                item.price = float(level.get("price") or item.price)
                item.strength = float(level.get("strength") or item.strength)
                item.timeframe = str(level.get("timeframe") or item.timeframe)
                item.source = str(level.get("source") or item.source)
                item.first_seen_ms = int(level.get("first_seen_ms") or item.first_seen_ms)
                item.last_seen_ms = int(level.get("last_seen_ms") or item.last_seen_ms)
                item.expires_at_ms = level.get("expires_at_ms")
                item.metadata_json = redact_secrets(dict(level.get("metadata") or {}))
            await session.commit()

    async def record_news_event(self, event: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                existing = await session.get(AutonomyNewsEvent, str(event["id"]))
                if existing is not None:
                    return existing.id
                item = AutonomyNewsEvent(
                    id=str(event["id"]),
                    provider=str(event.get("provider") or "unknown"),
                    source=str(event.get("source") or "unknown"),
                    title=str(event.get("title") or ""),
                    text=str(event.get("text") or ""),
                    url=event.get("url"),
                    author_id=event.get("author_id"),
                    created_at_ms=event.get("created_at_ms"),
                    observed_at_ms=int(event.get("observed_at_ms") or 0),
                    importance_score=float(event.get("importance_score") or 0),
                    sentiment=str(event.get("sentiment") or "unknown"),
                    assets_json=list(event.get("assets") or []),
                    metadata_json=redact_secrets(dict(event.get("metadata") or {})),
                )
                session.add(item)
                await session.commit()
                return item.id
        except Exception as exc:  # pragma: no cover
            log.warning("autonomy_news_event_record_failed", error=type(exc).__name__)
            return None

    async def list_news_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(AutonomyNewsEvent).order_by(AutonomyNewsEvent.observed_at_ms.desc()).limit(limit))
            return [_news_event_to_dict(item) for item in result.scalars().all()]

    async def record_newswire_event(self, event: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        try:
            async with self.sessionmaker() as session:
                event_id = str(event["event_id"])
                existing = await session.get(NewswireEventRow, event_id)
                fields = _newswire_kwargs(event)
                if existing is None:
                    session.add(NewswireEventRow(event_id=event_id, **fields))
                elif event.get("action") in {"updated", "removed"}:
                    for key, value in fields.items():
                        setattr(existing, key, value)
                else:
                    return existing.event_id
                await session.commit()
                return event_id
        except Exception as exc:  # pragma: no cover - duplicate/unavailable persistence should not break ingestion
            log.warning("newswire_event_record_failed", error=type(exc).__name__)
            return None

    async def list_newswire_events(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(NewswireEventRow).order_by(NewswireEventRow.received_at_ms.desc()).limit(limit))
            return [_newswire_event_to_dict(item) for item in result.scalars().all()]

    async def claim_newswire_publish(
        self,
        event_id: str,
        channel_id: str,
        mode: str,
        now_ms: int,
        *,
        destination: str = "discord",
        stale_after_ms: int = 10 * 60 * 1000,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Claim a Newswire event for one destination/channel.

        Returns False when the event was already posted or has a fresh pending
        claim. Failed or stale pending claims may be retried.
        """
        if self.sessionmaker is None:
            return True
        publish_id = _newswire_publish_id(destination, channel_id, event_id)
        try:
            async with self.sessionmaker() as session:
                item = await session.get(NewswirePublishLedgerRow, publish_id)
                if item is None:
                    session.add(
                        NewswirePublishLedgerRow(
                            publish_id=publish_id,
                            event_id=event_id,
                            destination=destination,
                            channel_id=str(channel_id),
                            mode=mode,
                            status="pending",
                            attempt_count=1,
                            first_attempt_ms=now_ms,
                            last_attempt_ms=now_ms,
                            metadata_json=redact_secrets(metadata or {}),
                        )
                    )
                    await session.commit()
                    return True
                if item.status == "posted":
                    return False
                if item.status == "pending" and now_ms - int(item.last_attempt_ms or 0) < stale_after_ms:
                    return False
                item.mode = mode
                item.status = "pending"
                item.attempt_count = int(item.attempt_count or 0) + 1
                item.last_attempt_ms = now_ms
                item.last_error = None
                if metadata:
                    item.metadata_json = redact_secrets(metadata)
                await session.commit()
                return True
        except IntegrityError:
            return False
        except Exception as exc:  # pragma: no cover - publishing should degrade rather than drop critical news
            log.warning("newswire_publish_claim_failed", error=type(exc).__name__)
            return True

    async def mark_newswire_publish_posted(
        self,
        event_ids: list[str],
        channel_id: str,
        message_id: str | None,
        now_ms: int,
        *,
        destination: str = "discord",
    ) -> None:
        if self.sessionmaker is None or not event_ids:
            return
        try:
            async with self.sessionmaker() as session:
                ids = [_newswire_publish_id(destination, channel_id, event_id) for event_id in event_ids]
                await session.execute(
                    update(NewswirePublishLedgerRow)
                    .where(NewswirePublishLedgerRow.publish_id.in_(ids))
                    .values(status="posted", discord_message_id=message_id, posted_at_ms=now_ms, last_attempt_ms=now_ms, last_error=None)
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("newswire_publish_mark_posted_failed", error=type(exc).__name__)

    async def mark_newswire_publish_failed(
        self,
        event_ids: list[str],
        channel_id: str,
        error: str,
        now_ms: int,
        *,
        destination: str = "discord",
    ) -> None:
        if self.sessionmaker is None or not event_ids:
            return
        try:
            async with self.sessionmaker() as session:
                ids = [_newswire_publish_id(destination, channel_id, event_id) for event_id in event_ids]
                await session.execute(
                    update(NewswirePublishLedgerRow)
                    .where(NewswirePublishLedgerRow.publish_id.in_(ids))
                    .values(status="failed", last_error=error[:1000], last_attempt_ms=now_ms)
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("newswire_publish_mark_failed_failed", error=type(exc).__name__)

    async def newswire_publish_status(self, channel_id: str, *, destination: str = "discord") -> dict[str, Any]:
        if self.sessionmaker is None:
            return {"enabled": False, "counts": {}}
        try:
            async with self.sessionmaker() as session:
                result = await session.execute(
                    select(NewswirePublishLedgerRow.status, func.count())
                    .where(NewswirePublishLedgerRow.destination == destination, NewswirePublishLedgerRow.channel_id == str(channel_id))
                    .group_by(NewswirePublishLedgerRow.status)
                )
                counts = {str(status): int(count) for status, count in result.all()}
                last_result = await session.execute(
                    select(NewswirePublishLedgerRow)
                    .where(NewswirePublishLedgerRow.destination == destination, NewswirePublishLedgerRow.channel_id == str(channel_id))
                    .order_by(NewswirePublishLedgerRow.last_attempt_ms.desc())
                    .limit(1)
                )
                last = last_result.scalars().first()
                return {
                    "enabled": True,
                    "counts": counts,
                    "last_event_id": last.event_id if last is not None else None,
                    "last_status": last.status if last is not None else None,
                    "last_attempt_ms": last.last_attempt_ms if last is not None else None,
                    "last_posted_at_ms": last.posted_at_ms if last is not None else None,
                    "last_error": last.last_error if last is not None else None,
                }
        except Exception as exc:  # pragma: no cover
            log.warning("newswire_publish_status_failed", error=type(exc).__name__)
            return {"enabled": True, "error": type(exc).__name__, "counts": {}}

    async def upsert_world_event(self, event: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(WorldEventRecord, str(event["event_id"]))
            if item is None:
                item = WorldEventRecord(
                    event_id=str(event["event_id"]),
                    received_ts_ms=int(event.get("received_ts_ms") or 0),
                    computed_ts_ms=int(event.get("computed_ts_ms") or 0),
                )
                session.add(item)
            _apply_world_event(item, event)
            await session.commit()
            return item.event_id

    async def list_world_events(self, limit: int = 100, source_type: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(WorldEventRecord).order_by(WorldEventRecord.computed_ts_ms.desc()).limit(max(limit, limit * 3 if symbol else limit))
            if source_type:
                stmt = stmt.where(WorldEventRecord.source_type == source_type)
            result = await session.execute(stmt)
            items = [_world_event_to_dict(item) for item in result.scalars().all()]
            if symbol:
                wanted = symbol.upper()
                items = [item for item in items if wanted in item.get("symbols", [])]
            return items[:limit]

    async def upsert_market_belief(self, belief: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(MarketBeliefRecord, str(belief["belief_id"]))
            if item is None:
                item = MarketBeliefRecord(
                    belief_id=str(belief["belief_id"]),
                    kind=str(belief.get("kind") or "fact"),
                    subject=str(belief.get("subject") or ""),
                    statement=str(belief.get("statement") or ""),
                    created_at_ms=int(belief.get("created_at_ms") or 0),
                    updated_at_ms=int(belief.get("updated_at_ms") or 0),
                )
                session.add(item)
            _apply_market_belief(item, belief)
            await session.commit()
            return item.belief_id

    async def list_market_beliefs(self, limit: int = 100, symbol: str | None = None, kind: str | None = None, status: str | None = "active") -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(MarketBeliefRecord).order_by(MarketBeliefRecord.updated_at_ms.desc()).limit(max(limit, limit * 3 if symbol else limit))
            if kind:
                stmt = stmt.where(MarketBeliefRecord.kind == kind)
            if status:
                stmt = stmt.where(MarketBeliefRecord.status == status)
            result = await session.execute(stmt)
            items = [_market_belief_to_dict(item) for item in result.scalars().all()]
            if symbol:
                wanted = symbol.upper()
                items = [item for item in items if wanted in item.get("symbols", [])]
            return items[:limit]

    async def upsert_narrative_cluster(self, cluster: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(NarrativeClusterRecord, str(cluster["cluster_id"]))
            if item is None:
                item = NarrativeClusterRecord(
                    cluster_id=str(cluster["cluster_id"]),
                    title=str(cluster.get("title") or ""),
                    created_at_ms=int(cluster.get("created_at_ms") or 0),
                    updated_at_ms=int(cluster.get("updated_at_ms") or 0),
                )
                session.add(item)
            _apply_narrative_cluster(item, cluster)
            await session.commit()
            return item.cluster_id

    async def list_narrative_clusters(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(NarrativeClusterRecord).order_by(NarrativeClusterRecord.updated_at_ms.desc()).limit(max(limit, limit * 3 if symbol else limit)))
            items = [_narrative_cluster_to_dict(item) for item in result.scalars().all()]
            if symbol:
                wanted = symbol.upper()
                items = [item for item in items if wanted in item.get("symbols", [])]
            return items[:limit]

    async def upsert_prediction_market_signal(self, signal: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(PredictionMarketSignalRecord, str(signal["signal_id"]))
            if item is None:
                item = PredictionMarketSignalRecord(
                    signal_id=str(signal["signal_id"]),
                    venue=str(signal.get("venue") or "unknown"),
                    market_id=str(signal.get("market_id") or ""),
                    question=str(signal.get("question") or ""),
                    as_of_ms=int(signal.get("as_of_ms") or 0),
                )
                session.add(item)
            _apply_prediction_market_signal(item, signal)
            await session.commit()
            return item.signal_id

    async def list_prediction_market_signals(self, limit: int = 100, venue: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(PredictionMarketSignalRecord).order_by(PredictionMarketSignalRecord.as_of_ms.desc()).limit(max(limit, limit * 3 if symbol else limit))
            if venue:
                stmt = stmt.where(PredictionMarketSignalRecord.venue == venue)
            result = await session.execute(stmt)
            items = [_prediction_market_signal_to_dict(item) for item in result.scalars().all()]
            if symbol:
                wanted = symbol.upper()
                items = [item for item in items if wanted in item.get("symbols", [])]
            return items[:limit]

    async def upsert_source_credibility(self, source: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(SourceCredibilityRecord, str(source["source_key"]))
            if item is None:
                item = SourceCredibilityRecord(
                    source_key=str(source["source_key"]),
                    source=str(source.get("source") or "unknown"),
                    last_updated_at_ms=int(source.get("last_updated_at_ms") or 0),
                )
                session.add(item)
            _apply_source_credibility(item, source)
            await session.commit()
            return item.source_key

    async def upsert_world_memory_atom(self, memory: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(WorldMemoryAtomRecord, str(memory["memory_id"]))
            if item is None:
                item = WorldMemoryAtomRecord(
                    memory_id=str(memory["memory_id"]),
                    memory_type=str(memory.get("memory_type") or "working"),
                    subject=str(memory.get("subject") or ""),
                    content=str(memory.get("content") or ""),
                    created_at_ms=int(memory.get("created_at_ms") or 0),
                )
                session.add(item)
            _apply_world_memory_atom(item, memory)
            await session.commit()
            return item.memory_id

    async def list_world_memory_atoms(self, limit: int = 100, symbol: str | None = None, memory_type: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(WorldMemoryAtomRecord).order_by(WorldMemoryAtomRecord.last_reinforced_at_ms.desc().nullslast(), WorldMemoryAtomRecord.created_at_ms.desc()).limit(max(limit, limit * 3 if symbol else limit))
            if memory_type:
                stmt = stmt.where(WorldMemoryAtomRecord.memory_type == memory_type)
            result = await session.execute(stmt)
            items = [_world_memory_atom_to_dict(item) for item in result.scalars().all()]
            if symbol:
                wanted = symbol.upper()
                items = [item for item in items if wanted in item.get("symbols", [])]
            return items[:limit]

    async def upsert_world_model_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(WorldModelSnapshotRecord, str(snapshot["snapshot_id"]))
            if item is None:
                item = WorldModelSnapshotRecord(snapshot_id=str(snapshot["snapshot_id"]), as_of_ms=int(snapshot.get("as_of_ms") or 0))
                session.add(item)
            _apply_world_model_snapshot(item, snapshot)
            await session.commit()
            return item.snapshot_id

    async def latest_world_model_snapshot(self) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            result = await session.execute(select(WorldModelSnapshotRecord).order_by(WorldModelSnapshotRecord.as_of_ms.desc()).limit(1))
            item = result.scalar_one_or_none()
            return _world_model_snapshot_to_dict(item) if item is not None else None

    async def get_world_model_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(WorldModelSnapshotRecord, snapshot_id)
            return _world_model_snapshot_to_dict(item) if item is not None else None

    async def list_world_model_snapshots(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        topic: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(WorldModelSnapshotRecord).order_by(WorldModelSnapshotRecord.as_of_ms.desc()).limit(max(limit, limit * 3 if symbol or topic else limit))
            if start_ms is not None:
                stmt = stmt.where(WorldModelSnapshotRecord.as_of_ms >= start_ms)
            if end_ms is not None:
                stmt = stmt.where(WorldModelSnapshotRecord.as_of_ms <= end_ms)
            result = await session.execute(stmt)
            items = [_world_model_snapshot_to_dict(item) for item in result.scalars().all()]
            if symbol:
                wanted = symbol.upper()
                items = [item for item in items if wanted in item.get("symbols", []) or any(wanted in belief.get("symbols", []) for belief in item.get("top_beliefs", []))]
            if topic:
                wanted_topic = topic.lower()
                items = [item for item in items if wanted_topic in item.get("topics", [])]
            return items[:limit]

    async def nearest_world_model_snapshot(self, as_of_ms: int, *, symbol: str | None = None, topic: str | None = None) -> dict[str, Any] | None:
        items = await self.list_world_model_snapshots(limit=50, symbol=symbol, topic=topic, end_ms=as_of_ms)
        return items[0] if items else None

    async def upsert_world_model_annotation(self, annotation: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(WorldModelAnnotationRecord, str(annotation["annotation_id"]))
            if item is None:
                item = WorldModelAnnotationRecord(
                    annotation_id=str(annotation["annotation_id"]),
                    target_type=str(annotation.get("target_type") or ""),
                    target_id=str(annotation.get("target_id") or ""),
                    action=str(annotation.get("action") or ""),
                    created_at_ms=int(annotation.get("created_at_ms") or 0),
                )
                session.add(item)
            _apply_world_model_annotation(item, annotation)
            await session.commit()
            return item.annotation_id

    async def list_world_model_annotations(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(WorldModelAnnotationRecord).order_by(WorldModelAnnotationRecord.created_at_ms.desc()).limit(limit)
            if target_type:
                stmt = stmt.where(WorldModelAnnotationRecord.target_type == target_type)
            if target_id:
                stmt = stmt.where(WorldModelAnnotationRecord.target_id == target_id)
            if action:
                stmt = stmt.where(WorldModelAnnotationRecord.action == action)
            result = await session.execute(stmt)
            return [_world_model_annotation_to_dict(item) for item in result.scalars().all()]

    async def upsert_world_model_outcome(self, outcome: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(WorldModelOutcomeRecord, str(outcome["outcome_id"]))
            if item is None:
                item = WorldModelOutcomeRecord(
                    outcome_id=str(outcome["outcome_id"]),
                    target_type=str(outcome.get("target_type") or ""),
                    target_id=str(outcome.get("target_id") or ""),
                    outcome=str(outcome.get("outcome") or ""),
                    created_at_ms=int(outcome.get("created_at_ms") or 0),
                )
                session.add(item)
            _apply_world_model_outcome(item, outcome)
            await session.commit()
            return item.outcome_id

    async def list_world_model_outcomes(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(WorldModelOutcomeRecord).order_by(WorldModelOutcomeRecord.created_at_ms.desc()).limit(limit)
            if target_type:
                stmt = stmt.where(WorldModelOutcomeRecord.target_type == target_type)
            if target_id:
                stmt = stmt.where(WorldModelOutcomeRecord.target_id == target_id)
            result = await session.execute(stmt)
            return [_world_model_outcome_to_dict(item) for item in result.scalars().all()]

    async def upsert_prediction_market_calibration(self, calibration: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(PredictionMarketCalibrationRecord, str(calibration["calibration_id"]))
            if item is None:
                item = PredictionMarketCalibrationRecord(
                    calibration_id=str(calibration["calibration_id"]),
                    signal_id=str(calibration.get("signal_id") or ""),
                    venue=str(calibration.get("venue") or "unknown"),
                    market_id=str(calibration.get("market_id") or ""),
                    created_at_ms=int(calibration.get("created_at_ms") or 0),
                )
                session.add(item)
            _apply_prediction_market_calibration(item, calibration)
            await session.commit()
            return item.calibration_id

    async def list_prediction_market_calibrations(
        self,
        *,
        signal_id: str | None = None,
        venue: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(PredictionMarketCalibrationRecord).order_by(PredictionMarketCalibrationRecord.created_at_ms.desc()).limit(limit)
            if signal_id:
                stmt = stmt.where(PredictionMarketCalibrationRecord.signal_id == signal_id)
            if venue:
                stmt = stmt.where(PredictionMarketCalibrationRecord.venue == venue)
            result = await session.execute(stmt)
            return [_prediction_market_calibration_to_dict(item) for item in result.scalars().all()]

    async def ping(self) -> dict[str, Any]:
        if self.sessionmaker is None:
            return {"ok": False, "error": "repository_disabled"}
        try:
            async with self.sessionmaker() as session:
                await session.execute(select(1))
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    async def create_or_update_trade_signal(self, signal: dict[str, Any], approved_by: str | None = None, rejected_by: str | None = None) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TradeSignalRecord, str(signal["id"]))
            if item is None:
                item = TradeSignalRecord(id=str(signal["id"]), symbol=str(signal.get("symbol") or "").upper(), side=str(signal.get("side") or "long"), signal_type=str(signal.get("signal_type") or "unknown"), status=str(signal.get("status") or "candidate"), score=float(signal.get("score") or 0), confidence=float(signal.get("confidence") or 0), created_at_ms=int(signal.get("created_at_ms") or 0), expires_at_ms=int(signal.get("expires_at_ms") or 0), entry_px=float(signal.get("entry") or 0), stop_px=float(signal.get("stop") or 0))
                session.add(item)
            item.symbol = str(signal.get("symbol") or item.symbol).upper()
            item.side = str(signal.get("side") or item.side)
            item.signal_type = str(signal.get("signal_type") or item.signal_type)
            item.status = str(signal.get("status") or item.status)
            item.score = float(signal.get("score") or item.score)
            item.confidence = float(signal.get("confidence") or item.confidence)
            item.created_at_ms = int(signal.get("created_at_ms") or item.created_at_ms)
            item.expires_at_ms = int(signal.get("expires_at_ms") or item.expires_at_ms)
            item.entry_px = float(signal.get("entry") or item.entry_px)
            item.stop_px = float(signal.get("stop") or item.stop_px)
            item.take_profit_px = signal.get("take_profit")
            item.thesis = str(signal.get("thesis") or "")
            item.invalidation = str(signal.get("invalidation") or "")
            item.evidence_json = list(signal.get("evidence") or [])
            item.feature_snapshot_json = redact_secrets(dict(signal.get("feature_snapshot") or {}))
            item.risk_plan_json = redact_secrets(dict(signal.get("risk_plan") or {}))
            item.model_insight_json = redact_secrets(signal.get("model_insight")) if signal.get("model_insight") is not None else None
            item.discord_channel_id = signal.get("discord_channel_id")
            item.discord_message_id = signal.get("discord_message_id")
            item.asset_class = str((signal.get("metadata") or {}).get("asset_class") or signal.get("asset_class") or item.asset_class or "crypto")
            item.metadata_json = redact_secrets(dict(signal.get("metadata") or {}))
            if approved_by:
                item.approved_by_discord_user_id = approved_by
                item.approved_at = datetime.now(UTC)
            if rejected_by:
                item.rejected_by_discord_user_id = rejected_by
                item.rejected_at = datetime.now(UTC)
            await session.commit()

    async def get_autonomy_trade_signal(self, signal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(TradeSignalRecord, signal_id)
            return _trade_signal_to_dict(item) if item is not None else None

    async def list_autonomy_trade_signals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(TradeSignalRecord).order_by(TradeSignalRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(TradeSignalRecord.status == status)
            result = await session.execute(stmt)
            return [_trade_signal_to_dict(item) for item in result.scalars().all()]

    async def upsert_signal_evaluation(self, evaluation: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(SignalEvaluationRecord, str(evaluation["id"]))
                if item is None:
                    result = await session.execute(select(SignalEvaluationRecord).where(SignalEvaluationRecord.signal_id == str(evaluation["signal_id"])))
                    item = result.scalar_one_or_none()
                if item is None:
                    item = SignalEvaluationRecord(id=str(evaluation["id"]), signal_id=str(evaluation["signal_id"]), symbol=str(evaluation.get("symbol") or "").upper(), side=str(evaluation.get("side") or "long"), signal_type=str(evaluation.get("signal_type") or "unknown"), status=str(evaluation.get("status") or "open"), created_at_ms=int(evaluation.get("created_at_ms") or 0), entry_px=float(evaluation.get("entry") or 0), stop_px=float(evaluation.get("stop") or 0), signal_score=float(evaluation.get("signal_score") or 0), signal_confidence=float(evaluation.get("signal_confidence") or 0), signal_status_at_eval_start=str(evaluation.get("signal_status_at_eval_start") or "unknown"), terminal_outcome=str(evaluation.get("terminal_outcome") or "open"))
                    session.add(item)
                _apply_signal_evaluation(item, evaluation)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("signal_evaluation_upsert_failed", signal_id=evaluation.get("signal_id"), error=type(exc).__name__)

    async def upsert_signal_evaluation_mark(self, mark: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(SignalEvaluationMarkRecord, str(mark["id"]))
                if item is None:
                    result = await session.execute(select(SignalEvaluationMarkRecord).where(SignalEvaluationMarkRecord.signal_id == str(mark["signal_id"]), SignalEvaluationMarkRecord.horizon == str(mark["horizon"])))
                    item = result.scalar_one_or_none()
                if item is None:
                    item = SignalEvaluationMarkRecord(id=str(mark["id"]), evaluation_id=str(mark["evaluation_id"]), signal_id=str(mark["signal_id"]), symbol=str(mark.get("symbol") or "").upper(), horizon=str(mark.get("horizon") or ""), due_at_ms=int(mark.get("due_at_ms") or 0), status=str(mark.get("status") or "pending"))
                    session.add(item)
                _apply_signal_evaluation_mark(item, mark)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("signal_evaluation_mark_upsert_failed", signal_id=mark.get("signal_id"), horizon=mark.get("horizon"), error=type(exc).__name__)

    async def upsert_signal_evaluation_marks(self, marks: list[dict[str, Any]]) -> None:
        for mark in marks:
            await self.upsert_signal_evaluation_mark(mark)

    async def get_signal_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(SignalEvaluationRecord, evaluation_id)
            if item is None:
                return None
            return await self._signal_evaluation_to_dict(session, item)

    async def get_signal_evaluation_by_signal_id(self, signal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            result = await session.execute(select(SignalEvaluationRecord).where(SignalEvaluationRecord.signal_id == signal_id))
            item = result.scalar_one_or_none()
            if item is None:
                return None
            return await self._signal_evaluation_to_dict(session, item)

    async def list_signal_evaluations(self, status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(SignalEvaluationRecord).order_by(SignalEvaluationRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(SignalEvaluationRecord.status == status)
            if symbol:
                stmt = stmt.where(SignalEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._signal_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_open_signal_evaluations(self, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(SignalEvaluationRecord).where(SignalEvaluationRecord.status.in_(["open", "partial"])).order_by(SignalEvaluationRecord.created_at_ms.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(SignalEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._signal_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_due_signal_evaluation_marks(self, now_ms: int, limit: int = 500) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(SignalEvaluationMarkRecord).where(SignalEvaluationMarkRecord.status == "pending", SignalEvaluationMarkRecord.due_at_ms <= now_ms).order_by(SignalEvaluationMarkRecord.due_at_ms.asc()).limit(limit))
            return [_signal_evaluation_mark_to_dict(item) for item in result.scalars().all()]

    async def list_signal_evaluation_marks(self, evaluation_id: str | None = None, signal_id: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(SignalEvaluationMarkRecord).order_by(SignalEvaluationMarkRecord.due_at_ms.asc())
            if evaluation_id:
                stmt = stmt.where(SignalEvaluationMarkRecord.evaluation_id == evaluation_id)
            if signal_id:
                stmt = stmt.where(SignalEvaluationMarkRecord.signal_id == signal_id)
            result = await session.execute(stmt)
            return [_signal_evaluation_mark_to_dict(item) for item in result.scalars().all()]

    async def upsert_alpha_event_evaluation(self, evaluation: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(AlphaEventEvaluationRecord, str(evaluation["id"]))
                if item is None:
                    result = await session.execute(select(AlphaEventEvaluationRecord).where(AlphaEventEvaluationRecord.event_id == str(evaluation["event_id"]), AlphaEventEvaluationRecord.symbol == str(evaluation.get("symbol") or "").upper()))
                    item = result.scalar_one_or_none()
                if item is None:
                    item = AlphaEventEvaluationRecord(id=str(evaluation["id"]), event_id=str(evaluation["event_id"]), symbol=str(evaluation.get("symbol") or "").upper(), received_at_ms=int(evaluation.get("received_at_ms") or 0))
                    session.add(item)
                _apply_alpha_event_evaluation(item, evaluation)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("alpha_event_evaluation_upsert_failed", event_id=evaluation.get("event_id"), symbol=evaluation.get("symbol"), error=type(exc).__name__)

    async def upsert_alpha_event_evaluation_mark(self, mark: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        try:
            async with self.sessionmaker() as session:
                item = await session.get(AlphaEventEvaluationMarkRecord, str(mark["id"]))
                if item is None:
                    result = await session.execute(select(AlphaEventEvaluationMarkRecord).where(AlphaEventEvaluationMarkRecord.event_id == str(mark["event_id"]), AlphaEventEvaluationMarkRecord.symbol == str(mark.get("symbol") or "").upper(), AlphaEventEvaluationMarkRecord.horizon == str(mark.get("horizon") or "")))
                    item = result.scalar_one_or_none()
                if item is None:
                    item = AlphaEventEvaluationMarkRecord(id=str(mark["id"]), evaluation_id=str(mark["evaluation_id"]), event_id=str(mark["event_id"]), symbol=str(mark.get("symbol") or "").upper(), horizon=str(mark.get("horizon") or ""), due_at_ms=int(mark.get("due_at_ms") or 0), status=str(mark.get("status") or "pending"))
                    session.add(item)
                _apply_alpha_event_evaluation_mark(item, mark)
                await session.commit()
        except Exception as exc:  # pragma: no cover
            log.warning("alpha_event_evaluation_mark_upsert_failed", event_id=mark.get("event_id"), horizon=mark.get("horizon"), error=type(exc).__name__)

    async def upsert_alpha_event_evaluation_marks(self, marks: list[dict[str, Any]]) -> None:
        for mark in marks:
            await self.upsert_alpha_event_evaluation_mark(mark)

    async def get_alpha_event_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(AlphaEventEvaluationRecord, evaluation_id)
            if item is None:
                return None
            return await self._alpha_event_evaluation_to_dict(session, item)

    async def get_alpha_event_evaluation_by_event_id(self, event_id: str, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(AlphaEventEvaluationRecord).where(AlphaEventEvaluationRecord.event_id == event_id).order_by(AlphaEventEvaluationRecord.symbol.asc())
            if symbol:
                stmt = stmt.where(AlphaEventEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._alpha_event_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_alpha_event_evaluations(self, status: str | None = None, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(AlphaEventEvaluationRecord).order_by(AlphaEventEvaluationRecord.received_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(AlphaEventEvaluationRecord.status == status)
            if symbol:
                stmt = stmt.where(AlphaEventEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._alpha_event_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_open_alpha_event_evaluations(self, symbol: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(AlphaEventEvaluationRecord).where(AlphaEventEvaluationRecord.status.in_(["open", "partial"])).order_by(AlphaEventEvaluationRecord.received_at_ms.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(AlphaEventEvaluationRecord.symbol == symbol.upper())
            result = await session.execute(stmt)
            return [await self._alpha_event_evaluation_to_dict(session, item) for item in result.scalars().all()]

    async def list_due_alpha_event_evaluation_marks(self, now_ms: int, limit: int = 500) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(AlphaEventEvaluationMarkRecord).where(AlphaEventEvaluationMarkRecord.status == "pending", AlphaEventEvaluationMarkRecord.due_at_ms <= now_ms).order_by(AlphaEventEvaluationMarkRecord.due_at_ms.asc()).limit(limit))
            return [_alpha_event_evaluation_mark_to_dict(item) for item in result.scalars().all()]

    async def record_memory_observation(self, observation: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = MemoryObservationRecord(id=str(observation["id"]), source_type=str(observation.get("source_type") or "signal_evaluation"), source_id=str(observation.get("source_id") or ""), role=observation.get("role"), symbol=str(observation["symbol"]).upper() if observation.get("symbol") else None, signal_type=observation.get("signal_type"), market_regime=observation.get("market_regime"), observation=str(observation.get("observation") or ""), evidence_json=redact_secrets(list(observation.get("evidence") or [])), severity=str(observation.get("severity") or "info"), created_at_ms=int(observation.get("created_at_ms") or 0), metadata_json=redact_secrets(dict(observation.get("metadata") or {})))
            session.add(item)
            await session.commit()
            return item.id

    async def list_memory_observations(self, source_type: str | None = None, role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(MemoryObservationRecord).order_by(MemoryObservationRecord.created_at_ms.desc()).limit(limit)
            if source_type:
                stmt = stmt.where(MemoryObservationRecord.source_type == source_type)
            if role:
                stmt = stmt.where(MemoryObservationRecord.role == role)
            result = await session.execute(stmt)
            return [_memory_observation_to_dict(item) for item in result.scalars().all()]

    async def upsert_candidate_lesson(self, lesson: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(CandidateLessonRecord, str(lesson["id"]))
            if item is None:
                item = CandidateLessonRecord(id=str(lesson["id"]), lesson_type=str(lesson.get("lesson_type") or "role_behavior"), role=lesson.get("role"), scope_json={}, claim=str(lesson.get("claim") or ""), sample_size=int(lesson.get("sample_size") or 0), confidence=float(lesson.get("confidence") or 0), expected_future_behavior_change=str(lesson.get("expected_future_behavior_change") or ""), status=str(lesson.get("status") or "candidate"), created_at_ms=int(lesson.get("created_at_ms") or 0), expires_at_ms=int(lesson.get("expires_at_ms") or 0))
                session.add(item)
            _apply_candidate_lesson(item, lesson)
            await session.commit()

    async def list_candidate_lessons(self, status: str | None = None, role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(CandidateLessonRecord).order_by(CandidateLessonRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(CandidateLessonRecord.status == status)
            if role:
                stmt = stmt.where(CandidateLessonRecord.role == role)
            result = await session.execute(stmt)
            return [_candidate_lesson_to_dict(item) for item in result.scalars().all()]

    async def get_candidate_lesson(self, candidate_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(CandidateLessonRecord, candidate_id)
            return _candidate_lesson_to_dict(item) if item is not None else None

    async def set_candidate_lesson_status(self, candidate_id: str, status: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(CandidateLessonRecord, candidate_id)
            if item is not None:
                item.status = status
                await session.commit()

    async def upsert_shadow_role_lesson(self, lesson: dict[str, Any]) -> None:
        await self._upsert_role_lesson_record(ShadowRoleLessonRecord, lesson)

    async def upsert_role_lesson(self, lesson: dict[str, Any]) -> None:
        await self._upsert_role_lesson_record(RoleLessonRecord, lesson)

    async def _upsert_role_lesson_record(self, model: type[ShadowRoleLessonRecord] | type[RoleLessonRecord], lesson: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = cast(ShadowRoleLessonRecord | RoleLessonRecord | None, await session.get(model, str(lesson["id"])))
            if item is None:
                item = model(id=str(lesson["id"]), role=str(lesson.get("role") or "analyst"), lesson_type=str(lesson.get("lesson_type") or "role_behavior"), claim=str(lesson.get("claim") or ""), instruction=str(lesson.get("instruction") or ""), confidence=float(lesson.get("confidence") or 0), sample_size=int(lesson.get("sample_size") or 0), validation_status=str(lesson.get("validation_status") or "shadow"), created_at_ms=int(lesson.get("created_at_ms") or 0), expires_at_ms=int(lesson.get("expires_at_ms") or 0))
                session.add(item)
            _apply_role_lesson(item, lesson)
            await session.commit()

    async def list_role_lessons(self, role: str | None = None, status: str | None = "active", include_shadow: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            model = ShadowRoleLessonRecord if include_shadow else RoleLessonRecord
            stmt = select(model).order_by(model.created_at_ms.desc()).limit(limit)
            if role:
                stmt = stmt.where(model.role == role)
            if status:
                stmt = stmt.where(model.validation_status == status)
            result = await session.execute(stmt)
            return [_role_lesson_to_dict(cast(ShadowRoleLessonRecord | RoleLessonRecord, item)) for item in result.scalars().all()]

    async def get_role_lesson(self, lesson_id: str, include_shadow: bool = False) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            role_item = await session.get(RoleLessonRecord, lesson_id)
            if role_item is not None:
                return _role_lesson_to_dict(role_item)
            if include_shadow:
                shadow_item = await session.get(ShadowRoleLessonRecord, lesson_id)
                return _role_lesson_to_dict(shadow_item) if shadow_item is not None else None
            return None

    async def archive_role_lesson(self, lesson_id: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(RoleLessonRecord, lesson_id)
            if item is not None:
                item.validation_status = "archived"
                await session.commit()

    async def upsert_operator_output_lesson(self, lesson: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(OperatorOutputLessonRecord, str(lesson["id"]))
            if item is None:
                item = OperatorOutputLessonRecord(id=str(lesson["id"]), issue_or_pattern=str(lesson.get("issue_or_pattern") or ""), preferred_behavior=str(lesson.get("preferred_behavior") or ""), confidence=float(lesson.get("confidence") or 0), sample_size=int(lesson.get("sample_size") or 0), validation_status=str(lesson.get("validation_status") or "active"), created_at_ms=int(lesson.get("created_at_ms") or 0), expires_at_ms=int(lesson.get("expires_at_ms") or 0))
                session.add(item)
            _apply_operator_lesson(item, lesson)
            await session.commit()

    async def list_operator_output_lessons(self, status: str | None = "active", limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(OperatorOutputLessonRecord).order_by(OperatorOutputLessonRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(OperatorOutputLessonRecord.validation_status == status)
            result = await session.execute(stmt)
            return [_operator_lesson_to_dict(item) for item in result.scalars().all()]

    async def record_operator_feedback(self, feedback: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = OperatorFeedbackRecord(id=str(feedback["id"]), source=str(feedback.get("source") or "api"), actor_id=feedback.get("actor_id"), target_type=str(feedback.get("target_type") or "signal"), target_id=str(feedback.get("target_id") or ""), rating=str(feedback.get("rating") or "unclear"), note=str(feedback.get("note") or ""), created_at_ms=int(feedback.get("created_at_ms") or 0), metadata_json=redact_secrets(dict(feedback.get("metadata") or {})))
            session.add(item)
            await session.commit()
            return item.id

    async def list_operator_feedback(self, target_type: str | None = None, target_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(OperatorFeedbackRecord).order_by(OperatorFeedbackRecord.created_at_ms.desc()).limit(limit)
            if target_type:
                stmt = stmt.where(OperatorFeedbackRecord.target_type == target_type)
            if target_id:
                stmt = stmt.where(OperatorFeedbackRecord.target_id == target_id)
            result = await session.execute(stmt)
            return [_operator_feedback_to_dict(item) for item in result.scalars().all()]

    async def upsert_rollback_plan(self, plan: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(RollbackPlanRecord, str(plan["rollback_plan_id"]))
            if item is None:
                item = RollbackPlanRecord(
                    rollback_plan_id=str(plan["rollback_plan_id"]),
                    target_type=str(plan.get("target_type") or "config"),
                    target_id=str(plan.get("target_id") or ""),
                    previous_version_id=str(plan.get("previous_version_id") or ""),
                    owner=str(plan.get("owner") or ""),
                    created_at_ms=int(plan.get("created_at_ms") or 0),
                )
                session.add(item)
            item.rollback_steps_json = [str(item) for item in plan.get("rollback_steps") or []]
            item.verification_steps_json = [str(item) for item in plan.get("verification_steps") or []]
            await session.commit()
            return item.rollback_plan_id

    async def upsert_review_packet(self, packet: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(ReviewPacketRecord, str(packet["review_packet_id"]))
            if item is None:
                item = ReviewPacketRecord(
                    review_packet_id=str(packet["review_packet_id"]),
                    proposal_id=str(packet.get("proposal_id") or ""),
                    risk_direction=str(packet.get("risk_direction") or "unknown"),
                    rollback_plan_id=str(packet.get("rollback_plan_id") or ""),
                    created_at_ms=int(packet.get("created_at_ms") or 0),
                )
                session.add(item)
            item.evidence_links_json = [str(item) for item in packet.get("evidence_links") or []]
            item.affected_strategies_json = [str(item) for item in packet.get("affected_strategies") or []]
            item.affected_symbols_json = [str(item) for item in packet.get("affected_symbols") or []]
            item.affected_venues_json = [str(item) for item in packet.get("affected_venues") or []]
            item.expected_effect = str(packet.get("expected_effect") or "")
            item.known_risks_json = [str(item) for item in packet.get("known_risks") or []]
            item.replay_results_json = redact_secrets(packet.get("replay_results")) if packet.get("replay_results") is not None else None
            item.shadow_results_json = redact_secrets(packet.get("shadow_results")) if packet.get("shadow_results") is not None else None
            item.reviewer_findings_json = redact_secrets(list(packet.get("reviewer_findings") or []))
            item.approval_requirements_json = [str(item) for item in packet.get("approval_requirements") or []]
            await session.commit()
            return item.review_packet_id

    async def list_review_packets(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(ReviewPacketRecord).order_by(ReviewPacketRecord.created_at_ms.desc()).limit(limit)
            if proposal_id:
                stmt = stmt.where(ReviewPacketRecord.proposal_id == proposal_id)
            result = await session.execute(stmt)
            return [_review_packet_to_dict(item) for item in result.scalars().all()]

    async def upsert_promotion_decision(self, decision: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = PromotionDecisionRecord(
                decision_id=str(decision["decision_id"]),
                proposal_id=str(decision.get("proposal_id") or ""),
                reviewer=str(decision.get("reviewer") or ""),
                decision=str(decision.get("decision") or "needs_more_evidence"),
                rationale=str(decision.get("rationale") or ""),
                evidence_reviewed_json=[str(item) for item in decision.get("evidence_reviewed") or []],
                tests_reviewed_json=[str(item) for item in decision.get("tests_reviewed") or []],
                proposer_actor=str(decision.get("proposer_actor") or ""),
                approver_actor=str(decision.get("approver_actor") or ""),
                change_control_id=str(decision.get("change_control_id") or ""),
                approved_contexts_json=[str(item) for item in decision.get("approved_contexts") or []],
                rollback_plan_id=str(decision.get("rollback_plan_id") or ""),
                created_at_ms=int(decision.get("created_at_ms") or 0),
            )
            session.add(item)
            await session.commit()
            return item.decision_id

    async def record_replay_result(self, result: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = ReplayResultRecord(
                replay_id=str(result["replay_id"]),
                proposal_id=result.get("proposal_id"),
                decision_id=result.get("decision_id"),
                status=str(result.get("status") or "audit_only"),
                baseline_metrics_json=redact_secrets(dict(result.get("baseline_metrics") or {})),
                candidate_metrics_json=redact_secrets(dict(result.get("candidate_metrics") or {})),
                diffs_json=redact_secrets(dict(result.get("diffs") or {})),
                caveats_json=[str(item) for item in result.get("caveats") or []],
                created_at_ms=int(result.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(result.get("metadata") or {})),
            )
            session.add(item)
            await session.commit()
            return item.replay_id

    async def list_replay_results(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(ReplayResultRecord).order_by(ReplayResultRecord.created_at_ms.desc()).limit(limit)
            if proposal_id:
                stmt = stmt.where(ReplayResultRecord.proposal_id == proposal_id)
            result = await session.execute(stmt)
            return [_replay_result_to_dict(item) for item in result.scalars().all()]

    async def record_engine_replay_comparison(self, result: dict[str, Any]) -> str | None:
        return await self.record_replay_result(result)

    async def list_engine_replay_comparisons(self, limit: int = 100) -> list[dict[str, Any]]:
        items = await self.list_replay_results(limit=limit)
        return [item for item in items if str(item.get("proposal_id") or "").startswith("engine:") or (item.get("metadata") or {}).get("artifact_type") == "engine_shadow_comparison"]

    async def latest_engine_replay_comparison(self) -> dict[str, Any] | None:
        items = await self.list_engine_replay_comparisons(limit=1)
        return items[0] if items else None

    async def list_shadow_comparisons(self, proposal_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(ShadowComparisonRecord).order_by(ShadowComparisonRecord.created_at_ms.desc()).limit(limit)
            if proposal_id:
                stmt = stmt.where(ShadowComparisonRecord.proposal_id == proposal_id)
            result = await session.execute(stmt)
            return [_shadow_comparison_to_dict(item) for item in result.scalars().all()]

    async def record_shadow_comparison(self, result: dict[str, Any]) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = ShadowComparisonRecord(
                comparison_id=str(result["comparison_id"]),
                proposal_id=str(result.get("proposal_id") or ""),
                status=str(result.get("status") or "audit_only"),
                baseline_metrics_json=redact_secrets(dict(result.get("baseline_metrics") or {})),
                candidate_metrics_json=redact_secrets(dict(result.get("candidate_metrics") or {})),
                metric_deltas_json=redact_secrets(dict(result.get("metric_deltas") or {})),
                recommendation=str(result.get("recommendation") or "audit_only"),
                created_at_ms=int(result.get("created_at_ms") or 0),
                metadata_json=redact_secrets(dict(result.get("metadata") or {})),
            )
            session.add(item)
            await session.commit()
            return item.comparison_id

    async def upsert_candidate_config_diff(self, diff: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(CandidateConfigDiffRecord, str(diff["proposal_id"]))
            if item is None:
                item = CandidateConfigDiffRecord(
                    proposal_id=str(diff["proposal_id"]),
                    strategy_id=str(diff.get("strategy_id") or "autonomy_v1"),
                    change_type=str(diff.get("change_type") or "proposal"),
                    rationale=str(diff.get("rationale") or ""),
                    risk_direction=str(diff.get("risk_direction") or "unknown"),
                    created_by=str(diff.get("created_by") or "unknown"),
                    created_at_ms=int(diff.get("created_at_ms") or 0),
                    status=str(diff.get("status") or "proposed"),
                )
                session.add(item)
            _apply_candidate_config_diff(item, diff)
            await session.commit()

    async def list_candidate_config_diffs(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(CandidateConfigDiffRecord).order_by(CandidateConfigDiffRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(CandidateConfigDiffRecord.status == status)
            result = await session.execute(stmt)
            return [_candidate_config_diff_to_dict(item) for item in result.scalars().all()]

    async def get_candidate_config_diff(self, proposal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(CandidateConfigDiffRecord, proposal_id)
            return _candidate_config_diff_to_dict(item) if item is not None else None

    async def set_candidate_config_diff_status(self, proposal_id: str, status: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(CandidateConfigDiffRecord, proposal_id)
            if item is not None:
                item.status = status
            proposal = await session.get(TuningProposalRecord, proposal_id)
            if proposal is not None:
                proposal.candidate_diff_status = status
            await session.commit()

    async def upsert_tuning_proposal(self, proposal: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TuningProposalRecord, str(proposal["id"]))
            if item is None:
                item = TuningProposalRecord(id=str(proposal["id"]), proposal_type=str(proposal.get("proposal_type") or "threshold_change"), status=str(proposal.get("status") or "draft"), title=str(proposal.get("title") or ""), summary=str(proposal.get("summary") or ""), expected_impact=str(proposal.get("expected_impact") or ""), risk_assessment=str(proposal.get("risk_assessment") or ""), blast_radius=str(proposal.get("blast_radius") or "low"), rollback_plan=str(proposal.get("rollback_plan") or ""), confidence=float(proposal.get("confidence") or 0), sample_size=int(proposal.get("sample_size") or 0), created_at_ms=int(proposal.get("created_at_ms") or 0), expires_at_ms=int(proposal.get("expires_at_ms") or 0), evaluation_window=str(proposal.get("evaluation_window") or "7d"))
                session.add(item)
            _apply_tuning_proposal(item, proposal)
            await session.commit()

    async def list_tuning_proposals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(TuningProposalRecord).order_by(TuningProposalRecord.created_at_ms.desc()).limit(limit)
            if status:
                stmt = stmt.where(TuningProposalRecord.status == status)
            result = await session.execute(stmt)
            return [_tuning_proposal_to_dict(item) for item in result.scalars().all()]

    async def get_tuning_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            item = await session.get(TuningProposalRecord, proposal_id)
            return _tuning_proposal_to_dict(item) if item is not None else None

    async def set_tuning_proposal_status(self, proposal_id: str, status: str) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TuningProposalRecord, proposal_id)
            if item is not None:
                item.status = status
                await session.commit()

    async def record_token_capital_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(TokenCapitalSnapshotRecord, str(snapshot["id"]))
            if item is None:
                item = TokenCapitalSnapshotRecord(id=str(snapshot["id"]), timestamp_ms=int(snapshot.get("timestamp_ms") or 0), window=str(snapshot.get("window") or "daily"), total_score=float(snapshot.get("total_score") or 0), risk_adjusted_performance_score=float(snapshot.get("risk_adjusted_performance_score") or 0), signal_quality_score=float(snapshot.get("signal_quality_score") or 0), memory_compounding_score=float(snapshot.get("memory_compounding_score") or 0), risk_discipline_score=float(snapshot.get("risk_discipline_score") or 0), operator_communication_score=float(snapshot.get("operator_communication_score") or 0), reliability_score=float(snapshot.get("reliability_score") or 0))
                session.add(item)
            item.hard_gate_penalties_json = redact_secrets(list(snapshot.get("hard_gate_penalties") or []))
            item.component_details_json = redact_secrets(dict(snapshot.get("component_details") or {}))
            item.created_from_report_id = snapshot.get("created_from_report_id")
            item.metadata_json = redact_secrets(dict(snapshot.get("metadata") or {}))
            await session.commit()

    async def list_token_capital_snapshots(self, window: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(TokenCapitalSnapshotRecord).order_by(TokenCapitalSnapshotRecord.timestamp_ms.desc()).limit(limit)
            if window:
                stmt = stmt.where(TokenCapitalSnapshotRecord.window == window)
            result = await session.execute(stmt)
            return [_token_capital_snapshot_to_dict(item) for item in result.scalars().all()]

    async def upsert_autonomy_report(self, report: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        report_type = str(report.get("report_type") or "daily")
        model = WeeklyReportRecord if report_type == "weekly" else DailyReportRecord
        key_field = "week_key" if report_type == "weekly" else "report_date"
        key_value = str(report.get("key") or "")
        async with self.sessionmaker() as session:
            stmt = select(model).where(getattr(model, key_field) == key_value)
            result = await session.execute(stmt)
            item = cast(DailyReportRecord | WeeklyReportRecord | None, result.scalar_one_or_none())
            if item is None:
                common = dict(id=str(report["id"]), period_start_ms=int(report.get("period_start_ms") or 0), period_end_ms=int(report.get("period_end_ms") or 0), generated_at_ms=int(report.get("generated_at_ms") or 0), summary=str(report.get("summary") or ""))
                item = model(**common, **{key_field: key_value})
                session.add(item)
            item.period_start_ms = int(report.get("period_start_ms") or item.period_start_ms)
            item.period_end_ms = int(report.get("period_end_ms") or item.period_end_ms)
            item.generated_at_ms = int(report.get("generated_at_ms") or item.generated_at_ms)
            token_capital = dict(report.get("token_capital") or {})
            item.token_capital_score = token_capital.get("total_score")
            item.summary = str(report.get("summary") or item.summary)
            item.report_json = redact_secrets(dict(report.get("report") or {}))
            item.discord_channel_id = report.get("discord_channel_id")
            item.discord_message_id = report.get("discord_message_id")
            item.metadata_json = redact_secrets(dict(report.get("metadata") or {}))
            await session.commit()

    async def list_autonomy_reports(self, report_type: str = "daily", limit: int = 30) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        model = WeeklyReportRecord if report_type == "weekly" else DailyReportRecord
        async with self.sessionmaker() as session:
            result = await session.execute(select(model).order_by(model.generated_at_ms.desc()).limit(limit))
            return [_autonomy_report_to_dict(cast(DailyReportRecord | WeeklyReportRecord, item), report_type) for item in result.scalars().all()]

    async def get_autonomy_report(self, report_type: str, key: str) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        model = WeeklyReportRecord if report_type == "weekly" else DailyReportRecord
        key_field = "week_key" if report_type == "weekly" else "report_date"
        async with self.sessionmaker() as session:
            result = await session.execute(select(model).where(getattr(model, key_field) == key))
            item = cast(DailyReportRecord | WeeklyReportRecord | None, result.scalar_one_or_none())
            return _autonomy_report_to_dict(item, report_type) if item is not None else None

    async def create_or_get_paper_portfolio(self, name: str, initial_equity_usd: float, mode: str = "paper_signoff") -> dict[str, Any]:
        if self.sessionmaker is None:
            raise RuntimeError("repository disabled")
        async with self.sessionmaker() as session:
            result = await session.execute(select(PaperPortfolioRecord).where(PaperPortfolioRecord.name == name))
            item = result.scalar_one_or_none()
            if item is None:
                item = PaperPortfolioRecord(name=name, status="active", initial_equity_usd=initial_equity_usd, cash_usd=initial_equity_usd, realized_pnl_usd=0.0, metadata_json={"mode": mode}, updated_at=datetime.now(UTC))
                session.add(item)
                await session.flush()
            await session.commit()
            return _paper_portfolio_to_dict(item)

    async def create_paper_order(self, order: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperOrderRecord, str(order["id"]))
            if item is None:
                item = PaperOrderRecord(id=str(order["id"]), portfolio_id=str(order["portfolio_id"]), signal_id=order.get("signal_id"), symbol=str(order.get("symbol") or "").upper(), side=str(order.get("side") or "long"), order_type=str(order.get("order_type") or "market"), status=str(order.get("status") or "new"), quantity=float(order.get("quantity") or 0), fee_bps=float(order.get("fee_bps") or 0), slippage_bps=float(order.get("slippage_bps") or 0))
                session.add(item)
            item.status = str(order.get("status") or item.status)
            item.requested_px = order.get("requested_px")
            item.filled_px = order.get("filled_px")
            item.stop_px = order.get("stop_px")
            item.take_profit_px = order.get("take_profit_px")
            item.filled_at = _datetime_from_optional_ms(order.get("filled_at_ms"))
            item.cancelled_at = _datetime_from_optional_ms(order.get("cancelled_at_ms"))
            item.metadata_json = redact_secrets(dict(order.get("metadata") or {}))
            await session.commit()

    async def mark_paper_order_filled(self, order_id: str, filled_px: float, timestamp_ms: int) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperOrderRecord, order_id)
            if item is not None:
                item.status = "filled"
                item.filled_px = filled_px
                item.filled_at = _datetime_from_ms(timestamp_ms)
                await session.commit()

    async def record_paper_fill(self, fill: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(PaperFillRecord, str(fill["id"]))
            if existing is None:
                fee_usd = float(fill.get("fee_usd") or 0)
                session.add(PaperFillRecord(id=str(fill["id"]), order_id=str(fill["order_id"]), portfolio_id=str(fill["portfolio_id"]), symbol=str(fill.get("symbol") or "").upper(), side=str(fill.get("side") or "long"), quantity=float(fill.get("quantity") or 0), price=float(fill.get("price") or 0), fee_usd=fee_usd, slippage_usd=float(fill.get("slippage_usd") or 0), metadata_json=redact_secrets(dict(fill.get("metadata") or {}))))
                portfolio = await session.get(PaperPortfolioRecord, str(fill["portfolio_id"]))
                if portfolio is not None:
                    portfolio.cash_usd -= fee_usd
                    portfolio.updated_at = datetime.now(UTC)
                await session.commit()

    async def upsert_paper_position(self, position: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperPositionRecord, str(position["id"]))
            if item is None:
                item = PaperPositionRecord(id=str(position["id"]), portfolio_id=str(position["portfolio_id"]), signal_id=position.get("signal_id"), symbol=str(position.get("symbol") or "").upper(), side=str(position.get("side") or "long"), status=str(position.get("status") or "open"), quantity=float(position.get("quantity") or 0), avg_entry_px=float(position.get("avg_entry_px") or 0), stop_px=float(position.get("stop_px") or 0), opened_at=_datetime_from_ms(int(position.get("opened_at_ms") or 0)))
                session.add(item)
            item.status = str(position.get("status") or item.status)
            item.mark_px = position.get("mark_px")
            item.take_profit_px = position.get("take_profit_px")
            item.realized_pnl_usd = float(position.get("realized_pnl_usd") or 0)
            item.unrealized_pnl_usd = float(position.get("unrealized_pnl_usd") or 0)
            item.closed_at = _datetime_from_optional_ms(position.get("closed_at_ms"))
            item.metadata_json = redact_secrets(dict(position.get("metadata") or {}))
            await session.commit()

    async def close_paper_position(self, position_id: str, close_px: float, realized_pnl_usd: float, reason: str, timestamp_ms: int) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(PaperPositionRecord, position_id)
            if item is not None:
                item.status = "closed"
                item.mark_px = close_px
                item.realized_pnl_usd = realized_pnl_usd
                item.unrealized_pnl_usd = 0.0
                item.closed_at = _datetime_from_ms(timestamp_ms)
                item.metadata_json = {**(item.metadata_json or {}), "close_reason": reason}
                portfolio = await session.get(PaperPortfolioRecord, item.portfolio_id)
                if portfolio is not None:
                    portfolio.cash_usd += realized_pnl_usd
                    portfolio.realized_pnl_usd += realized_pnl_usd
                    portfolio.updated_at = datetime.now(UTC)
                await session.commit()

    async def list_paper_positions(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(PaperPositionRecord).limit(limit)
            if status:
                stmt = stmt.where(PaperPositionRecord.status == status)
            result = await session.execute(stmt)
            return [_paper_position_to_dict(item) for item in result.scalars().all()]

    async def list_paper_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(PaperOrderRecord).order_by(PaperOrderRecord.created_at.desc()).limit(limit))
            return [_paper_order_to_dict(item) for item in result.scalars().all()]

    async def list_paper_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(PaperFillRecord).order_by(PaperFillRecord.created_at.desc()).limit(limit))
            return [_paper_fill_to_dict(item) for item in result.scalars().all()]

    async def record_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(PortfolioSnapshotRecord, str(snapshot["id"]))
            if existing is None:
                session.add(PortfolioSnapshotRecord(id=str(snapshot["id"]), portfolio_id=str(snapshot["portfolio_id"]), timestamp_ms=int(snapshot.get("timestamp_ms") or 0), cash_usd=float(snapshot.get("cash_usd") or 0), equity_usd=float(snapshot.get("equity_usd") or 0), gross_exposure_usd=float(snapshot.get("gross_exposure_usd") or 0), net_exposure_usd=float(snapshot.get("net_exposure_usd") or 0), realized_pnl_usd=float(snapshot.get("realized_pnl_usd") or 0), unrealized_pnl_usd=float(snapshot.get("unrealized_pnl_usd") or 0), total_pnl_usd=float(snapshot.get("total_pnl_usd") or 0), drawdown_pct=float(snapshot.get("drawdown_pct") or 0), sharpe=snapshot.get("sharpe"), metrics_json=redact_secrets(dict(snapshot.get("metrics") or {}))))
                await session.commit()

    async def get_latest_portfolio_snapshot(self, portfolio_id: str | None = None) -> dict[str, Any] | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as session:
            stmt = select(PortfolioSnapshotRecord).order_by(PortfolioSnapshotRecord.timestamp_ms.desc()).limit(1)
            if portfolio_id:
                stmt = stmt.where(PortfolioSnapshotRecord.portfolio_id == portfolio_id)
            result = await session.execute(stmt)
            item = result.scalar_one_or_none()
            return _portfolio_snapshot_to_dict(item) if item is not None else None

    async def list_portfolio_snapshots(self, portfolio_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(PortfolioSnapshotRecord).order_by(PortfolioSnapshotRecord.timestamp_ms.desc()).limit(limit)
            if portfolio_id:
                stmt = stmt.where(PortfolioSnapshotRecord.portfolio_id == portfolio_id)
            result = await session.execute(stmt)
            return [_portfolio_snapshot_to_dict(item) for item in result.scalars().all()]

    # --- TradFi / equity paper portfolio --------------------------------------

    async def create_or_get_equity_paper_portfolio(self, name: str, initial_equity_usd: float, mode: str = "equity_paper") -> dict[str, Any]:
        if self.sessionmaker is None:
            raise RuntimeError("repository disabled")
        async with self.sessionmaker() as session:
            result = await session.execute(select(EquityPaperPortfolioRecord).where(EquityPaperPortfolioRecord.name == name))
            item = result.scalar_one_or_none()
            if item is None:
                item = EquityPaperPortfolioRecord(
                    name=name,
                    status="active",
                    initial_equity_usd=initial_equity_usd,
                    cash_usd=initial_equity_usd,
                    realized_pnl_usd=0.0,
                    metadata_json={"mode": mode, "asset_class": "equity"},
                    updated_at=datetime.now(UTC),
                )
                session.add(item)
                await session.flush()
            await session.commit()
            return _equity_paper_portfolio_to_dict(item)

    async def upsert_equity_paper_portfolio(self, portfolio: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(EquityPaperPortfolioRecord, str(portfolio["id"]))
            if item is None:
                item = EquityPaperPortfolioRecord(
                    id=str(portfolio["id"]),
                    name=str(portfolio.get("name") or "equity_paper"),
                    status=str(portfolio.get("status") or "active"),
                    initial_equity_usd=float(portfolio.get("initial_equity_usd") or 0),
                    cash_usd=float(portfolio.get("cash_usd") or 0),
                    realized_pnl_usd=float(portfolio.get("realized_pnl_usd") or 0),
                    metadata_json=redact_secrets(dict(portfolio.get("metadata") or {})),
                    updated_at=datetime.now(UTC),
                )
                session.add(item)
            else:
                item.status = str(portfolio.get("status") or item.status)
                item.cash_usd = float(portfolio.get("cash_usd") or item.cash_usd)
                item.realized_pnl_usd = float(portfolio.get("realized_pnl_usd") or item.realized_pnl_usd)
                item.metadata_json = redact_secrets(dict(portfolio.get("metadata") or item.metadata_json or {}))
                item.updated_at = datetime.now(UTC)
            await session.commit()

    async def create_equity_paper_order(self, order: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(EquityPaperOrderRecord, str(order["id"]))
            if item is None:
                item = EquityPaperOrderRecord(
                    id=str(order["id"]),
                    portfolio_id=str(order["portfolio_id"]),
                    signal_id=order.get("signal_id"),
                    symbol=str(order.get("symbol") or "").upper(),
                    side=str(order.get("side") or "long"),
                    order_type=str(order.get("order_type") or "market"),
                    status=str(order.get("status") or "pending"),
                    quantity=float(order.get("quantity") or 0),
                    fee_bps=float(order.get("fee_bps") or 0),
                    slippage_bps=float(order.get("slippage_bps") or 0),
                )
                session.add(item)
            item.status = str(order.get("status") or item.status)
            item.requested_px = order.get("requested_px")
            item.filled_px = order.get("filled_px")
            item.stop_px = order.get("stop_px")
            item.take_profit_px = order.get("take_profit_px")
            item.filled_at = _datetime_from_optional_iso(order.get("filled_at"))
            item.cancelled_at = _datetime_from_optional_iso(order.get("cancelled_at"))
            item.metadata_json = redact_secrets(dict(order.get("metadata") or {}))
            await session.commit()

    async def record_equity_paper_fill(self, fill: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(EquityPaperFillRecord, str(fill["id"]))
            if existing is None:
                session.add(
                    EquityPaperFillRecord(
                        id=str(fill["id"]),
                        order_id=str(fill["order_id"]),
                        portfolio_id=str(fill["portfolio_id"]),
                        symbol=str(fill.get("symbol") or "").upper(),
                        side=str(fill.get("side") or "long"),
                        quantity=float(fill.get("quantity") or 0),
                        price=float(fill.get("price") or 0),
                        fee_usd=float(fill.get("fee_usd") or 0),
                        slippage_usd=float(fill.get("slippage_usd") or 0),
                        metadata_json=redact_secrets(dict(fill.get("metadata") or {})),
                    )
                )
                await session.commit()

    async def upsert_equity_paper_position(self, position: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            item = await session.get(EquityPaperPositionRecord, str(position["id"]))
            if item is None:
                item = EquityPaperPositionRecord(
                    id=str(position["id"]),
                    portfolio_id=str(position["portfolio_id"]),
                    signal_id=position.get("signal_id"),
                    symbol=str(position.get("symbol") or "").upper(),
                    side=str(position.get("side") or "long"),
                    status=str(position.get("status") or "open"),
                    quantity=float(position.get("quantity") or 0),
                    avg_entry_px=float(position.get("avg_entry_px") or 0),
                    opened_at=_datetime_from_optional_iso(position.get("opened_at")) or datetime.now(UTC),
                )
                session.add(item)
            item.status = str(position.get("status") or item.status)
            item.mark_px = position.get("mark_px")
            item.stop_px = position.get("stop_px")
            item.take_profit_px = position.get("take_profit_px")
            item.realized_pnl_usd = float(position.get("realized_pnl_usd") or 0)
            item.unrealized_pnl_usd = float(position.get("unrealized_pnl_usd") or 0)
            item.closed_at = _datetime_from_optional_iso(position.get("closed_at"))
            item.metadata_json = redact_secrets(dict(position.get("metadata") or {}))
            await session.commit()

    async def record_equity_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(EquityPortfolioSnapshotRecord, str(snapshot["id"]))
            if existing is None:
                session.add(
                    EquityPortfolioSnapshotRecord(
                        id=str(snapshot["id"]),
                        portfolio_id=str(snapshot["portfolio_id"]),
                        timestamp_ms=int(snapshot.get("timestamp_ms") or 0),
                        cash_usd=float(snapshot.get("cash_usd") or 0),
                        equity_usd=float(snapshot.get("equity_usd") or 0),
                        gross_exposure_usd=float(snapshot.get("gross_exposure_usd") or 0),
                        net_exposure_usd=float(snapshot.get("net_exposure_usd") or 0),
                        realized_pnl_usd=float(snapshot.get("realized_pnl_usd") or 0),
                        unrealized_pnl_usd=float(snapshot.get("unrealized_pnl_usd") or 0),
                        total_pnl_usd=float(snapshot.get("total_pnl_usd") or 0),
                        metrics_json=redact_secrets(dict(snapshot.get("metrics") or {})),
                    )
                )
                await session.commit()

    async def list_equity_paper_positions(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            stmt = select(EquityPaperPositionRecord).limit(limit)
            if status:
                stmt = stmt.where(EquityPaperPositionRecord.status == status)
            result = await session.execute(stmt)
            return [_equity_paper_position_to_dict(item) for item in result.scalars().all()]

    async def list_equity_paper_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(EquityPaperOrderRecord).order_by(EquityPaperOrderRecord.created_at.desc()).limit(limit))
            return [_equity_paper_order_to_dict(item) for item in result.scalars().all()]

    async def list_equity_paper_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as session:
            result = await session.execute(select(EquityPaperFillRecord).order_by(EquityPaperFillRecord.created_at.desc()).limit(limit))
            return [_equity_paper_fill_to_dict(item) for item in result.scalars().all()]

    async def record_equity_options_flow_event(self, event: dict[str, Any]) -> None:
        if self.sessionmaker is None:
            return
        async with self.sessionmaker() as session:
            existing = await session.get(EquityOptionsFlowEventRecord, str(event["id"]))
            if existing is None:
                session.add(
                    EquityOptionsFlowEventRecord(
                        id=str(event["id"]),
                        symbol=str(event.get("symbol") or "").upper(),
                        detected_at=_datetime_from_optional_iso(event.get("detected_at")) or datetime.now(UTC),
                        flow_type=str(event.get("flow_type") or "unknown"),
                        volume_oi_ratio=float(event.get("volume_oi_ratio") or 0),
                        premium_estimate=float(event.get("premium_estimate") or 0),
                        is_sweep=bool(event.get("is_sweep") or False),
                        cluster_score=float(event.get("cluster_score") or 0),
                        urgency_score=float(event.get("urgency_score") or 0),
                        contract_json=redact_secrets(dict(event.get("contract") or {})),
                        enrichment_json=redact_secrets(dict(event.get("enrichment") or {})),
                    )
                )
                await session.commit()

    async def _tracker_to_dict(self, session: AsyncSession, tracker: PositionTracker) -> dict[str, Any]:
        levels_result = await session.execute(select(TrackedLevel).where(TrackedLevel.tracker_id == tracker.id).order_by(TrackedLevel.created_at.asc()))
        events_result = await session.execute(select(TrackingEvent).where(TrackingEvent.tracker_id == tracker.id).order_by(TrackingEvent.created_at.desc()).limit(20))
        return {
            "id": tracker.id,
            "proposal_id": tracker.proposal_id,
            "run_id": tracker.run_id,
            "source": tracker.source,
            "status": tracker.status,
            "coin": tracker.coin,
            "side": tracker.side,
            "entry": tracker.entry_px,
            "stop": tracker.stop_px,
            "take_profit": tracker.take_profit_px,
            "current_price": tracker.current_px,
            "last_price": tracker.last_px,
            "last_price_at_ms": tracker.last_price_at_ms,
            "price_source": tracker.price_source,
            "expires_at": tracker.expires_at.isoformat() if tracker.expires_at else None,
            "completed_at": tracker.completed_at.isoformat() if tracker.completed_at else None,
            "discord_guild_id": tracker.discord_guild_id,
            "discord_channel_id": tracker.discord_channel_id,
            "discord_thread_id": tracker.discord_thread_id,
            "discord_user_id": tracker.discord_user_id,
            "plan": tracker.plan_json,
            "metadata": tracker.metadata_json,
            "levels": [_level_to_dict(item) for item in levels_result.scalars().all()],
            "recent_events": [_event_to_dict(item) for item in events_result.scalars().all()],
            "created_at": tracker.created_at.isoformat() if tracker.created_at else None,
            "updated_at": tracker.updated_at.isoformat() if tracker.updated_at else None,
        }

    async def _signal_evaluation_to_dict(self, session: AsyncSession, item: SignalEvaluationRecord) -> dict[str, Any]:
        result = await session.execute(select(SignalEvaluationMarkRecord).where(SignalEvaluationMarkRecord.evaluation_id == item.id).order_by(SignalEvaluationMarkRecord.due_at_ms.asc()))
        data = _signal_evaluation_to_dict(item)
        data["marks"] = [_signal_evaluation_mark_to_dict(mark) for mark in result.scalars().all()]
        return data

    async def _alpha_event_evaluation_to_dict(self, session: AsyncSession, item: AlphaEventEvaluationRecord) -> dict[str, Any]:
        result = await session.execute(select(AlphaEventEvaluationMarkRecord).where(AlphaEventEvaluationMarkRecord.evaluation_id == item.id).order_by(AlphaEventEvaluationMarkRecord.due_at_ms.asc()))
        data = _alpha_event_evaluation_to_dict(item)
        data["marks"] = [_alpha_event_evaluation_mark_to_dict(mark) for mark in result.scalars().all()]
        return data


def _apply_signal_evaluation(item: SignalEvaluationRecord, data: dict[str, Any]) -> None:
    item.symbol = str(data.get("symbol") or item.symbol).upper()
    item.side = str(data.get("side") or item.side)
    item.signal_type = str(data.get("signal_type") or item.signal_type)
    item.status = str(data.get("status") or item.status)
    item.completed_at_ms = data.get("completed_at_ms")
    item.entry_px = _float_value(data.get("entry"), item.entry_px)
    item.stop_px = _float_value(data.get("stop"), item.stop_px)
    item.take_profit_px = data.get("take_profit")
    item.signal_score = _float_value(data.get("signal_score"), item.signal_score)
    item.signal_confidence = _float_value(data.get("signal_confidence"), item.signal_confidence)
    item.signal_status_at_eval_start = str(data.get("signal_status_at_eval_start") or item.signal_status_at_eval_start)
    item.first_price = data.get("first_price")
    item.latest_price = data.get("latest_price")
    item.latest_price_at_ms = data.get("latest_price_at_ms")
    item.max_favorable_price = data.get("max_favorable_price")
    item.max_adverse_price = data.get("max_adverse_price")
    item.max_favorable_bps = data.get("max_favorable_bps")
    item.max_adverse_bps = data.get("max_adverse_bps")
    item.max_favorable_r = data.get("max_favorable_r")
    item.max_adverse_r = data.get("max_adverse_r")
    item.stop_hit = bool(data.get("stop_hit"))
    item.stop_hit_at_ms = data.get("stop_hit_at_ms")
    item.take_profit_hit = bool(data.get("take_profit_hit"))
    item.take_profit_hit_at_ms = data.get("take_profit_hit_at_ms")
    item.terminal_outcome = str(data.get("terminal_outcome") or item.terminal_outcome)
    item.realized_or_marked_r = data.get("realized_or_marked_r")
    item.opportunity_cost_r = data.get("opportunity_cost_r")
    item.approved = bool(data.get("approved"))
    item.rejected = bool(data.get("rejected"))
    item.paper_ordered = bool(data.get("paper_ordered"))
    item.paper_position_id = data.get("paper_position_id")
    item.feature_snapshot_json = redact_secrets(dict(data.get("feature_snapshot") or {}))
    item.evidence_snapshot_json = redact_secrets(list(data.get("evidence_snapshot") or []))
    item.market_regime = str(data.get("market_regime") or "unknown")
    item.error = str(data.get("error") or "")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))
    item.updated_at = datetime.now(UTC)


def _float_value(value: Any, fallback: float) -> float:
    try:
        return float(fallback if value is None else value)
    except (TypeError, ValueError):
        return float(fallback)


def _apply_signal_evaluation_mark(item: SignalEvaluationMarkRecord, data: dict[str, Any]) -> None:
    item.evaluation_id = str(data.get("evaluation_id") or item.evaluation_id)
    item.signal_id = str(data.get("signal_id") or item.signal_id)
    item.symbol = str(data.get("symbol") or item.symbol).upper()
    item.horizon = str(data.get("horizon") or item.horizon)
    item.due_at_ms = int(data.get("due_at_ms") or item.due_at_ms)
    item.marked_at_ms = data.get("marked_at_ms")
    item.price = data.get("price")
    item.direction_adjusted_return_bps = data.get("direction_adjusted_return_bps")
    item.r_multiple = data.get("r_multiple")
    item.mfe_bps_until_mark = data.get("mfe_bps_until_mark")
    item.mae_bps_until_mark = data.get("mae_bps_until_mark")
    item.mfe_r_until_mark = data.get("mfe_r_until_mark")
    item.mae_r_until_mark = data.get("mae_r_until_mark")
    item.stop_hit_before_mark = bool(data.get("stop_hit_before_mark"))
    item.take_profit_hit_before_mark = bool(data.get("take_profit_hit_before_mark"))
    item.status = str(data.get("status") or item.status)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_alpha_event_evaluation(item: AlphaEventEvaluationRecord, data: dict[str, Any]) -> None:
    item.event_id = str(data.get("event_id") or item.event_id)
    item.event_source = str(data.get("event_source") or item.event_source)
    item.provider = str(data.get("provider") or item.provider)
    item.event_type = str(data.get("event_type") or item.event_type)
    item.asset_class = str(data.get("asset_class") or item.asset_class)
    item.symbol = str(data.get("symbol") or item.symbol).upper()
    item.direction = str(data.get("direction") or item.direction)
    item.sentiment = str(data.get("sentiment") or item.sentiment)
    item.status = str(data.get("status") or item.status)
    item.terminal_outcome = str(data.get("terminal_outcome") or item.terminal_outcome)
    item.received_at_ms = int(data.get("received_at_ms") or item.received_at_ms)
    item.completed_at_ms = data.get("completed_at_ms")
    item.headline = str(data.get("headline") or item.headline)
    item.url = data.get("url")
    item.importance_score = _float_value(data.get("importance_score"), item.importance_score)
    item.source_score = _float_value(data.get("source_score"), item.source_score)
    item.urgency = str(data.get("urgency") or item.urgency)
    item.freshness = str(data.get("freshness") or item.freshness)
    item.market_regime = str(data.get("market_regime") or item.market_regime)
    item.reference_price = data.get("reference_price")
    item.reference_price_at_ms = data.get("reference_price_at_ms")
    item.latest_price = data.get("latest_price")
    item.latest_price_at_ms = data.get("latest_price_at_ms")
    item.max_favorable_price = data.get("max_favorable_price")
    item.max_adverse_price = data.get("max_adverse_price")
    item.max_favorable_bps = data.get("max_favorable_bps")
    item.max_adverse_bps = data.get("max_adverse_bps")
    item.max_abs_move_bps = data.get("max_abs_move_bps")
    item.realized_or_marked_bps = data.get("realized_or_marked_bps")
    item.linked_signal_ids_json = list(data.get("linked_signal_ids") or [])
    item.error = str(data.get("error") or "")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))
    item.updated_at = datetime.now(UTC)


def _apply_alpha_event_evaluation_mark(item: AlphaEventEvaluationMarkRecord, data: dict[str, Any]) -> None:
    item.evaluation_id = str(data.get("evaluation_id") or item.evaluation_id)
    item.event_id = str(data.get("event_id") or item.event_id)
    item.symbol = str(data.get("symbol") or item.symbol).upper()
    item.asset_class = str(data.get("asset_class") or item.asset_class)
    item.horizon = str(data.get("horizon") or item.horizon)
    item.due_at_ms = int(data.get("due_at_ms") or item.due_at_ms)
    item.marked_at_ms = data.get("marked_at_ms")
    item.price = data.get("price")
    item.direction_adjusted_return_bps = data.get("direction_adjusted_return_bps")
    item.abs_move_bps = data.get("abs_move_bps")
    item.max_favorable_bps_until_mark = data.get("max_favorable_bps_until_mark")
    item.max_adverse_bps_until_mark = data.get("max_adverse_bps_until_mark")
    item.max_abs_move_bps_until_mark = data.get("max_abs_move_bps_until_mark")
    item.status = str(data.get("status") or item.status)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_candidate_lesson(item: CandidateLessonRecord, data: dict[str, Any]) -> None:
    item.lesson_type = str(data.get("lesson_type") or item.lesson_type)
    item.role = data.get("role")
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.claim = str(data.get("claim") or item.claim)
    item.evidence_json = redact_secrets(list(data.get("evidence") or []))
    item.source_observation_ids_json = list(data.get("source_observation_ids") or [])
    item.source_run_ids_json = list(data.get("source_run_ids") or [])
    item.source_signal_ids_json = list(data.get("source_signal_ids") or [])
    item.sample_size = int(data.get("sample_size") or 0)
    item.counterexamples_json = redact_secrets(list(data.get("counterexamples") or []))
    item.confidence = float(data.get("confidence") or 0)
    item.expected_future_behavior_change = str(data.get("expected_future_behavior_change") or "")
    item.strategy_affecting = bool(data.get("strategy_affecting"))
    item.risk_affecting = bool(data.get("risk_affecting"))
    item.execution_affecting = bool(data.get("execution_affecting"))
    item.capital_allocation_affecting = bool(data.get("capital_allocation_affecting"))
    item.status = str(data.get("status") or item.status)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_role_lesson(item: ShadowRoleLessonRecord | RoleLessonRecord, data: dict[str, Any]) -> None:
    item.role = str(data.get("role") or item.role)
    item.lesson_type = str(data.get("lesson_type") or item.lesson_type)
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.claim = str(data.get("claim") or item.claim)
    item.instruction = str(data.get("instruction") or item.instruction)
    item.evidence_json = redact_secrets(list(data.get("evidence") or []))
    item.source_candidate_id = data.get("source_candidate_id")
    item.source_run_ids_json = list(data.get("source_run_ids") or [])
    item.source_signal_ids_json = list(data.get("source_signal_ids") or [])
    item.confidence = float(data.get("confidence") or 0)
    item.sample_size = int(data.get("sample_size") or 0)
    item.counterexamples_json = redact_secrets(list(data.get("counterexamples") or []))
    item.validation_status = str(data.get("validation_status") or item.validation_status)
    item.strategy_affecting = bool(data.get("strategy_affecting"))
    item.risk_affecting = bool(data.get("risk_affecting"))
    item.execution_affecting = bool(data.get("execution_affecting"))
    item.capital_allocation_affecting = bool(data.get("capital_allocation_affecting"))
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.activated_at_ms = data.get("activated_at_ms")
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.last_revalidated_at_ms = data.get("last_revalidated_at_ms")
    item.memory_status = str(data.get("memory_status") or data.get("metadata", {}).get("memory_status") or item.memory_status)
    item.allowed_contexts_json = list(data.get("allowed_contexts") or [])
    item.forbidden_contexts_json = list(data.get("forbidden_contexts") or [])
    item.promotion_history_json = redact_secrets(list(data.get("promotion_history") or []))
    item.rollback_target = data.get("rollback_target")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_operator_lesson(item: OperatorOutputLessonRecord, data: dict[str, Any]) -> None:
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.issue_or_pattern = str(data.get("issue_or_pattern") or item.issue_or_pattern)
    item.preferred_behavior = str(data.get("preferred_behavior") or item.preferred_behavior)
    item.bad_examples_json = redact_secrets(list(data.get("bad_examples") or []))
    item.good_examples_json = redact_secrets(list(data.get("good_examples") or []))
    item.confidence = float(data.get("confidence") or 0)
    item.sample_size = int(data.get("sample_size") or 0)
    item.validation_status = str(data.get("validation_status") or item.validation_status)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_candidate_config_diff(item: CandidateConfigDiffRecord, data: dict[str, Any]) -> None:
    item.strategy_id = str(data.get("strategy_id") or item.strategy_id)
    item.scope_json = redact_secrets(dict(data.get("scope") or {}))
    item.change_type = str(data.get("change_type") or item.change_type)
    item.current_value_json = redact_secrets(dict(data.get("current_value") or {}))
    item.proposed_value_json = redact_secrets(dict(data.get("proposed_value") or {}))
    item.rationale = str(data.get("rationale") or item.rationale)
    item.evidence_json = [str(item) for item in data.get("evidence") or []]
    item.expected_effect = str(data.get("expected_effect") or "")
    item.known_risks_json = [str(item) for item in data.get("known_risks") or []]
    item.validation_required_json = [str(item) for item in data.get("validation_required") or []]
    item.risk_direction = str(data.get("risk_direction") or item.risk_direction)
    item.requires_human_approval = bool(data.get("requires_human_approval", True))
    item.auto_apply_allowed = bool(data.get("auto_apply_allowed", False))
    item.created_by = str(data.get("created_by") or item.created_by)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.status = str(data.get("status") or item.status)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_tuning_proposal(item: TuningProposalRecord, data: dict[str, Any]) -> None:
    item.proposal_type = str(data.get("proposal_type") or item.proposal_type)
    item.status = str(data.get("status") or item.status)
    item.title = str(data.get("title") or item.title)
    item.summary = str(data.get("summary") or item.summary)
    item.affected_scope_json = redact_secrets(dict(data.get("affected_scope") or {}))
    item.current_behavior_json = redact_secrets(dict(data.get("current_behavior") or {}))
    item.proposed_diff_json = redact_secrets(dict(data.get("proposed_diff") or {}))
    item.evidence_json = redact_secrets(list(data.get("evidence") or []))
    item.source_lesson_ids_json = list(data.get("source_lesson_ids") or [])
    item.source_signal_ids_json = list(data.get("source_signal_ids") or [])
    item.strategy_id = str(data.get("strategy_id") or item.strategy_id)
    item.change_type = str(data.get("change_type") or item.change_type)
    item.risk_direction = str(data.get("risk_direction") or item.risk_direction)
    item.requires_human_approval = bool(data.get("requires_human_approval", True))
    item.validation_required_json = [str(item) for item in data.get("validation_required") or []]
    item.known_risks_json = [str(item) for item in data.get("known_risks") or []]
    item.review_packet_id = data.get("review_packet_id")
    item.candidate_diff_status = str(data.get("candidate_diff_status") or item.candidate_diff_status)
    item.expected_impact = str(data.get("expected_impact") or "")
    item.risk_assessment = str(data.get("risk_assessment") or "")
    item.blast_radius = str(data.get("blast_radius") or "low")
    item.rollback_plan = str(data.get("rollback_plan") or "")
    item.confidence = float(data.get("confidence") or 0)
    item.sample_size = int(data.get("sample_size") or 0)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms)
    item.expires_at_ms = int(data.get("expires_at_ms") or item.expires_at_ms)
    item.evaluation_window = str(data.get("evaluation_window") or item.evaluation_window)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _datetime_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _datetime_from_optional_ms(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return _datetime_from_ms(int(value))
    except (TypeError, ValueError, OSError):
        return None


def _datetime_from_optional_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _ms_from_datetime(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value is not None else None


def _engine_record_to_dict(item: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for column in item.__table__.columns:
        key = column.name
        value = getattr(item, key)
        if isinstance(value, datetime):
            value = value.isoformat()
        out_key = key[:-5] if key.endswith("_json") else key
        data[out_key] = value
    return data


def _market_asset_to_dict(item: MarketAssetRecord) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "display_name": item.display_name,
        "kind": item.kind,
        "source": item.source,
        "dex": item.dex,
        "sz_decimals": item.sz_decimals,
        "max_leverage": item.max_leverage,
        "day_volume_usd": item.day_volume_usd,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _news_event_to_dict(item: AutonomyNewsEvent) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "provider": item.provider,
        "title": item.title,
        "text": item.text,
        "url": item.url,
        "author_id": item.author_id,
        "created_at_ms": item.created_at_ms,
        "observed_at_ms": item.observed_at_ms,
        "assets": item.assets_json,
        "importance_score": item.importance_score,
        "sentiment": item.sentiment,
        "freshness": str((item.metadata_json or {}).get("freshness") or "fresh"),
        "metadata": item.metadata_json,
    }


def _newswire_publish_id(destination: str, channel_id: str, event_id: str) -> str:
    key = f"{destination}:{channel_id}:{event_id}"
    return "nwpub_" + hashlib.sha1(key.encode()).hexdigest()[:32]


def _newswire_kwargs(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": int(event.get("schema_version") or 1),
        "source": str(event.get("source") or "unknown"),
        "provider": str(event.get("provider") or "unknown"),
        "transport": str(event.get("transport") or "rss"),
        "received_at_ms": int(event.get("received_at_ms") or 0),
        "published_at_ms": event.get("published_at_ms"),
        "updated_at_ms": event.get("updated_at_ms"),
        "action": str(event.get("action") or "created"),
        "headline": str(event.get("headline") or ""),
        "body": str(event.get("body") or ""),
        "url": event.get("url"),
        "author": event.get("author"),
        "symbols_json": list(event.get("symbols") or []),
        "asset_class": str(event.get("asset_class") or "unknown"),
        "event_type": str(event.get("event_type") or "headline"),
        "urgency": str(event.get("urgency") or "normal"),
        "importance_score": float(event.get("importance_score") or 0),
        "sentiment": str(event.get("sentiment") or "unknown"),
        "freshness": str(event.get("freshness") or "fresh"),
        "confidence": float(event.get("confidence") or 0),
        "source_score": float(event.get("source_score") or 0),
        "tradability_json": dict(event.get("tradability") or {}),
        "enrichment_json": event.get("enrichment"),
        "metadata_json": redact_secrets(dict(event.get("metadata") or {})),
    }


def _newswire_event_to_dict(item: NewswireEventRow) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "schema_version": item.schema_version,
        "source": item.source,
        "provider": item.provider,
        "transport": item.transport,
        "received_at_ms": item.received_at_ms,
        "published_at_ms": item.published_at_ms,
        "updated_at_ms": item.updated_at_ms,
        "action": item.action,
        "headline": item.headline,
        "body": item.body,
        "url": item.url,
        "author": item.author,
        "symbols": item.symbols_json,
        "asset_class": item.asset_class,
        "event_type": item.event_type,
        "urgency": item.urgency,
        "importance_score": item.importance_score,
        "sentiment": item.sentiment,
        "freshness": item.freshness,
        "confidence": item.confidence,
        "source_score": item.source_score,
        "tradability": item.tradability_json,
        "enrichment": item.enrichment_json,
        "metadata": item.metadata_json,
    }


def _world_event_to_dict(item: WorldEventRecord) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "source_type": item.source_type,
        "source": item.source,
        "provider": item.provider,
        "event_type": item.event_type,
        "asset_class": item.asset_class,
        "symbols": item.symbols_json,
        "topics": item.topics_json,
        "title": item.title,
        "body": item.body,
        "url": item.url,
        "event_ts_ms": item.event_ts_ms,
        "received_ts_ms": item.received_ts_ms,
        "computed_ts_ms": item.computed_ts_ms,
        "importance_score": item.importance_score,
        "sentiment": item.sentiment,
        "confidence": item.confidence,
        "source_score": item.source_score,
        "quality_score": item.quality_score,
        "staleness_ms": item.staleness_ms,
        "payload": item.payload_json,
        "metadata": item.metadata_json,
    }


def _apply_world_event(item: WorldEventRecord, data: dict[str, Any]) -> None:
    item.source_type = str(data.get("source_type") or "unknown")
    item.source = str(data.get("source") or "unknown")
    item.provider = str(data.get("provider") or "unknown")
    item.event_type = str(data.get("event_type") or "unknown")
    item.asset_class = str(data.get("asset_class") or "unknown")
    item.symbols_json = [str(symbol).upper() for symbol in data.get("symbols") or []]
    item.topics_json = [str(topic).lower() for topic in data.get("topics") or []]
    item.title = str(data.get("title") or "")
    item.body = str(data.get("body") or "")
    item.url = data.get("url")
    item.event_ts_ms = data.get("event_ts_ms")
    item.received_ts_ms = int(data.get("received_ts_ms") or 0)
    item.computed_ts_ms = int(data.get("computed_ts_ms") or item.received_ts_ms)
    item.importance_score = float(data.get("importance_score") or 0)
    item.sentiment = str(data.get("sentiment") or "unknown")
    item.confidence = float(data.get("confidence") or 0)
    item.source_score = float(data.get("source_score") or 0)
    item.quality_score = float(data.get("quality_score") or 1)
    item.staleness_ms = data.get("staleness_ms")
    item.payload_json = redact_secrets(dict(data.get("payload") or {}))
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _market_belief_to_dict(item: MarketBeliefRecord) -> dict[str, Any]:
    return {
        "belief_id": item.belief_id,
        "kind": item.kind,
        "subject": item.subject,
        "statement": item.statement,
        "symbols": item.symbols_json,
        "topics": item.topics_json,
        "direction": item.direction,
        "probability": item.probability,
        "confidence": item.confidence,
        "salience": item.salience,
        "evidence_event_ids": item.evidence_event_ids_json,
        "contradicts_belief_ids": item.contradicts_belief_ids_json,
        "status": item.status,
        "created_at_ms": item.created_at_ms,
        "updated_at_ms": item.updated_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "metadata": item.metadata_json,
    }


def _apply_market_belief(item: MarketBeliefRecord, data: dict[str, Any]) -> None:
    item.kind = str(data.get("kind") or "fact")
    item.subject = str(data.get("subject") or "")
    item.statement = str(data.get("statement") or "")
    item.symbols_json = [str(symbol).upper() for symbol in data.get("symbols") or []]
    item.topics_json = [str(topic).lower() for topic in data.get("topics") or []]
    item.direction = str(data.get("direction") or "unknown")
    item.probability = data.get("probability")
    item.confidence = float(data.get("confidence") or 0)
    item.salience = float(data.get("salience") or 0)
    item.evidence_event_ids_json = [str(event_id) for event_id in data.get("evidence_event_ids") or []]
    item.contradicts_belief_ids_json = [str(belief_id) for belief_id in data.get("contradicts_belief_ids") or []]
    item.status = str(data.get("status") or "active")
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms or 0)
    item.updated_at_ms = int(data.get("updated_at_ms") or item.updated_at_ms or item.created_at_ms)
    item.expires_at_ms = data.get("expires_at_ms")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _narrative_cluster_to_dict(item: NarrativeClusterRecord) -> dict[str, Any]:
    return {
        "cluster_id": item.cluster_id,
        "title": item.title,
        "summary": item.summary,
        "symbols": item.symbols_json,
        "topics": item.topics_json,
        "belief_ids": item.belief_ids_json,
        "event_ids": item.event_ids_json,
        "pressure_score": item.pressure_score,
        "consensus_score": item.consensus_score,
        "conflict_score": item.conflict_score,
        "created_at_ms": item.created_at_ms,
        "updated_at_ms": item.updated_at_ms,
        "metadata": item.metadata_json,
    }


def _apply_narrative_cluster(item: NarrativeClusterRecord, data: dict[str, Any]) -> None:
    item.title = str(data.get("title") or "")
    item.summary = str(data.get("summary") or "")
    item.symbols_json = [str(symbol).upper() for symbol in data.get("symbols") or []]
    item.topics_json = [str(topic).lower() for topic in data.get("topics") or []]
    item.belief_ids_json = [str(belief_id) for belief_id in data.get("belief_ids") or []]
    item.event_ids_json = [str(event_id) for event_id in data.get("event_ids") or []]
    item.pressure_score = float(data.get("pressure_score") or 0)
    item.consensus_score = float(data.get("consensus_score") or 0)
    item.conflict_score = float(data.get("conflict_score") or 0)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms or 0)
    item.updated_at_ms = int(data.get("updated_at_ms") or item.updated_at_ms or item.created_at_ms)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _prediction_market_signal_to_dict(item: PredictionMarketSignalRecord) -> dict[str, Any]:
    return {
        "signal_id": item.signal_id,
        "venue": item.venue,
        "market_id": item.market_id,
        "question": item.question,
        "outcome_id": item.outcome_id,
        "outcome_name": item.outcome_name,
        "symbols": item.symbols_json,
        "topics": item.topics_json,
        "implied_probability": item.implied_probability,
        "probability_delta": item.probability_delta,
        "best_bid": item.best_bid,
        "best_ask": item.best_ask,
        "liquidity_usd": item.liquidity_usd,
        "volume_usd": item.volume_usd,
        "status": item.status,
        "source_event_ids": item.source_event_ids_json,
        "as_of_ms": item.as_of_ms,
        "staleness_ms": item.staleness_ms,
        "confidence": item.confidence,
        "metadata": item.metadata_json,
    }


def _apply_prediction_market_signal(item: PredictionMarketSignalRecord, data: dict[str, Any]) -> None:
    item.venue = str(data.get("venue") or "unknown")
    item.market_id = str(data.get("market_id") or "")
    item.question = str(data.get("question") or "")
    item.outcome_id = data.get("outcome_id")
    item.outcome_name = str(data.get("outcome_name") or "")
    item.symbols_json = [str(symbol).upper() for symbol in data.get("symbols") or []]
    item.topics_json = [str(topic).lower() for topic in data.get("topics") or []]
    item.implied_probability = data.get("implied_probability")
    item.probability_delta = data.get("probability_delta")
    item.best_bid = data.get("best_bid")
    item.best_ask = data.get("best_ask")
    item.liquidity_usd = data.get("liquidity_usd")
    item.volume_usd = data.get("volume_usd")
    item.status = str(data.get("status") or "unknown")
    item.source_event_ids_json = [str(event_id) for event_id in data.get("source_event_ids") or []]
    item.as_of_ms = int(data.get("as_of_ms") or item.as_of_ms or 0)
    item.staleness_ms = data.get("staleness_ms")
    item.confidence = float(data.get("confidence") or 0)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _apply_source_credibility(item: SourceCredibilityRecord, data: dict[str, Any]) -> None:
    item.source = str(data.get("source") or "unknown")
    item.provider = str(data.get("provider") or "unknown")
    item.score = float(data.get("score") or 0.5)
    item.observations = int(data.get("observations") or 0)
    item.confirmations = int(data.get("confirmations") or 0)
    item.contradictions = int(data.get("contradictions") or 0)
    item.last_updated_at_ms = int(data.get("last_updated_at_ms") or item.last_updated_at_ms or 0)
    item.notes_json = [str(note) for note in data.get("notes") or []]
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _world_memory_atom_to_dict(item: WorldMemoryAtomRecord) -> dict[str, Any]:
    return {
        "memory_id": item.memory_id,
        "memory_type": item.memory_type,
        "subject": item.subject,
        "content": item.content,
        "symbols": item.symbols_json,
        "topics": item.topics_json,
        "source_event_ids": item.source_event_ids_json,
        "source_belief_ids": item.source_belief_ids_json,
        "confidence": item.confidence,
        "salience": item.salience,
        "created_at_ms": item.created_at_ms,
        "last_reinforced_at_ms": item.last_reinforced_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "metadata": item.metadata_json,
    }


def _apply_world_memory_atom(item: WorldMemoryAtomRecord, data: dict[str, Any]) -> None:
    item.memory_type = str(data.get("memory_type") or "working")
    item.subject = str(data.get("subject") or "")
    item.content = str(data.get("content") or "")
    item.symbols_json = [str(symbol).upper() for symbol in data.get("symbols") or []]
    item.topics_json = [str(topic).lower() for topic in data.get("topics") or []]
    item.source_event_ids_json = [str(event_id) for event_id in data.get("source_event_ids") or []]
    item.source_belief_ids_json = [str(belief_id) for belief_id in data.get("source_belief_ids") or []]
    item.confidence = float(data.get("confidence") or 0)
    item.salience = float(data.get("salience") or 0)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms or 0)
    item.last_reinforced_at_ms = data.get("last_reinforced_at_ms")
    item.expires_at_ms = data.get("expires_at_ms")
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _world_model_snapshot_to_dict(item: WorldModelSnapshotRecord) -> dict[str, Any]:
    return {
        "snapshot_id": item.snapshot_id,
        "as_of_ms": item.as_of_ms,
        "symbols": item.symbols_json,
        "topics": item.topics_json,
        "summary": item.summary,
        "top_beliefs": item.top_beliefs_json,
        "narrative_clusters": item.narrative_clusters_json,
        "prediction_market_signals": item.prediction_market_signals_json,
        "source_credibility": item.source_credibility_json,
        "memory_atoms": item.memory_atoms_json,
        "quality_flags": item.quality_flags_json,
        "metadata": item.metadata_json,
    }


def _apply_world_model_snapshot(item: WorldModelSnapshotRecord, data: dict[str, Any]) -> None:
    item.as_of_ms = int(data.get("as_of_ms") or 0)
    item.symbols_json = [str(symbol).upper() for symbol in data.get("symbols") or []]
    item.topics_json = [str(topic).lower() for topic in data.get("topics") or []]
    item.summary = str(data.get("summary") or "")
    item.top_beliefs_json = redact_secrets(list(data.get("top_beliefs") or []))
    item.narrative_clusters_json = redact_secrets(list(data.get("narrative_clusters") or []))
    item.prediction_market_signals_json = redact_secrets(list(data.get("prediction_market_signals") or []))
    item.source_credibility_json = redact_secrets(list(data.get("source_credibility") or []))
    item.memory_atoms_json = redact_secrets(list(data.get("memory_atoms") or []))
    item.quality_flags_json = [str(flag) for flag in data.get("quality_flags") or []]
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _world_model_annotation_to_dict(item: WorldModelAnnotationRecord) -> dict[str, Any]:
    return {
        "annotation_id": item.annotation_id,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "action": item.action,
        "note": item.note,
        "actor_id": item.actor_id,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _apply_world_model_annotation(item: WorldModelAnnotationRecord, data: dict[str, Any]) -> None:
    item.target_type = str(data.get("target_type") or "")
    item.target_id = str(data.get("target_id") or "")
    item.action = str(data.get("action") or "")
    item.note = str(data.get("note") or "")
    item.actor_id = data.get("actor_id")
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms or 0)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _world_model_outcome_to_dict(item: WorldModelOutcomeRecord) -> dict[str, Any]:
    return {
        "outcome_id": item.outcome_id,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "outcome": item.outcome,
        "symbol": item.symbol,
        "horizon": item.horizon,
        "realized_value": item.realized_value,
        "confidence_delta": item.confidence_delta,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _apply_world_model_outcome(item: WorldModelOutcomeRecord, data: dict[str, Any]) -> None:
    item.target_type = str(data.get("target_type") or "")
    item.target_id = str(data.get("target_id") or "")
    item.outcome = str(data.get("outcome") or "")
    item.symbol = str(data.get("symbol")).upper() if data.get("symbol") else None
    item.horizon = data.get("horizon")
    item.realized_value = data.get("realized_value")
    item.confidence_delta = float(data.get("confidence_delta") if data.get("confidence_delta") is not None else 0.05)
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms or 0)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _prediction_market_calibration_to_dict(item: PredictionMarketCalibrationRecord) -> dict[str, Any]:
    return {
        "calibration_id": item.calibration_id,
        "signal_id": item.signal_id,
        "venue": item.venue,
        "market_id": item.market_id,
        "implied_probability": item.implied_probability,
        "realized_outcome": item.realized_outcome,
        "brier_score": item.brier_score,
        "settled_at_ms": item.settled_at_ms,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _apply_prediction_market_calibration(item: PredictionMarketCalibrationRecord, data: dict[str, Any]) -> None:
    item.signal_id = str(data.get("signal_id") or "")
    item.venue = str(data.get("venue") or "unknown")
    item.market_id = str(data.get("market_id") or "")
    item.implied_probability = data.get("implied_probability")
    item.realized_outcome = data.get("realized_outcome")
    item.brier_score = data.get("brier_score")
    item.settled_at_ms = data.get("settled_at_ms")
    item.created_at_ms = int(data.get("created_at_ms") or item.created_at_ms or 0)
    item.metadata_json = redact_secrets(dict(data.get("metadata") or {}))


def _risk_gateway_decision_to_dict(item: RiskGatewayDecisionRecord) -> dict[str, Any]:
    return {
        "decision_id": item.decision_id,
        "intent_id": item.intent_id,
        "mode": item.mode,
        "decision": item.decision,
        "violations": item.violations_json,
        "limits_snapshot": item.limits_snapshot_json,
        "market_snapshot": item.market_snapshot_json,
        "portfolio_snapshot": item.portfolio_snapshot_json,
        "config_version_id": item.config_version_id,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _memory_injection_event_to_dict(item: MemoryInjectionEventRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "role": item.role,
        "context_type": item.context_type,
        "memory_ids": item.memory_ids_json,
        "blocked_memory_ids": item.blocked_memory_ids_json,
        "policy_decision": item.policy_decision_json,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _decision_context_to_dict(item: DecisionContextRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "run_id": item.run_id,
        "config_version_id": item.config_version_id,
        "risk_config_version_id": item.risk_config_version_id,
        "model_route_version_id": item.model_route_version_id,
        "prompt_version_ids": item.prompt_version_ids_json,
        "injected_memory_ids": item.injected_memory_ids_json,
        "market_snapshot_refs": item.market_snapshot_refs_json,
        "data_freshness": item.data_freshness_json,
        "code_version": item.code_version,
        "created_at_ms": item.created_at_ms,
        "context": item.context_json,
        "metadata": item.metadata_json,
    }


def _trade_signal_to_dict(item: TradeSignalRecord) -> dict[str, Any]:
    metadata = dict(item.metadata_json or {})
    metadata.setdefault("asset_class", item.asset_class)
    return {
        "id": item.id,
        "symbol": item.symbol,
        "side": item.side,
        "signal_type": item.signal_type,
        "status": item.status,
        "score": item.score,
        "confidence": item.confidence,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "entry": item.entry_px,
        "stop": item.stop_px,
        "take_profit": item.take_profit_px,
        "thesis": item.thesis,
        "invalidation": item.invalidation,
        "evidence": item.evidence_json,
        "feature_snapshot": item.feature_snapshot_json,
        "risk_plan": item.risk_plan_json,
        "model_insight": item.model_insight_json,
        "discord_channel_id": item.discord_channel_id,
        "discord_message_id": item.discord_message_id,
        "metadata": metadata,
    }


def _paper_portfolio_to_dict(item: PaperPortfolioRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "initial_equity_usd": item.initial_equity_usd,
        "cash_usd": item.cash_usd,
        "realized_pnl_usd": item.realized_pnl_usd,
        "metadata": item.metadata_json,
        "created_at_ms": _ms_from_datetime(item.created_at) or int(datetime.now(UTC).timestamp() * 1000),
        "updated_at_ms": _ms_from_datetime(item.updated_at) or _ms_from_datetime(item.created_at) or int(datetime.now(UTC).timestamp() * 1000),
    }


def _paper_order_to_dict(item: PaperOrderRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "order_type": item.order_type,
        "status": item.status,
        "quantity": item.quantity,
        "requested_px": item.requested_px,
        "filled_px": item.filled_px,
        "stop_px": item.stop_px,
        "take_profit_px": item.take_profit_px,
        "fee_bps": item.fee_bps,
        "slippage_bps": item.slippage_bps,
        "created_at_ms": _ms_from_datetime(item.created_at),
        "filled_at_ms": _ms_from_datetime(item.filled_at),
        "cancelled_at_ms": _ms_from_datetime(item.cancelled_at),
        "metadata": item.metadata_json,
    }


def _paper_fill_to_dict(item: PaperFillRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "order_id": item.order_id,
        "portfolio_id": item.portfolio_id,
        "symbol": item.symbol,
        "side": item.side,
        "quantity": item.quantity,
        "price": item.price,
        "fee_usd": item.fee_usd,
        "slippage_usd": item.slippage_usd,
        "created_at_ms": _ms_from_datetime(item.created_at),
        "metadata": item.metadata_json,
    }


def _paper_position_to_dict(item: PaperPositionRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "status": item.status,
        "quantity": item.quantity,
        "avg_entry_px": item.avg_entry_px,
        "mark_px": item.mark_px,
        "stop_px": item.stop_px,
        "take_profit_px": item.take_profit_px,
        "realized_pnl_usd": item.realized_pnl_usd,
        "unrealized_pnl_usd": item.unrealized_pnl_usd,
        "opened_at_ms": _ms_from_datetime(item.opened_at),
        "closed_at_ms": _ms_from_datetime(item.closed_at),
        "metadata": item.metadata_json,
    }


def _portfolio_snapshot_to_dict(item: PortfolioSnapshotRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "timestamp_ms": item.timestamp_ms,
        "cash_usd": item.cash_usd,
        "equity_usd": item.equity_usd,
        "gross_exposure_usd": item.gross_exposure_usd,
        "net_exposure_usd": item.net_exposure_usd,
        "realized_pnl_usd": item.realized_pnl_usd,
        "unrealized_pnl_usd": item.unrealized_pnl_usd,
        "total_pnl_usd": item.total_pnl_usd,
        "drawdown_pct": item.drawdown_pct,
        "sharpe": item.sharpe,
        "metrics": item.metrics_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _equity_paper_portfolio_to_dict(item: EquityPaperPortfolioRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "initial_equity_usd": item.initial_equity_usd,
        "cash_usd": item.cash_usd,
        "realized_pnl_usd": item.realized_pnl_usd,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _equity_paper_order_to_dict(item: EquityPaperOrderRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "order_type": item.order_type,
        "status": item.status,
        "quantity": item.quantity,
        "requested_px": item.requested_px,
        "filled_px": item.filled_px,
        "stop_px": item.stop_px,
        "take_profit_px": item.take_profit_px,
        "fee_bps": item.fee_bps,
        "slippage_bps": item.slippage_bps,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "filled_at": item.filled_at.isoformat() if item.filled_at else None,
        "cancelled_at": item.cancelled_at.isoformat() if item.cancelled_at else None,
        "metadata": item.metadata_json,
    }


def _equity_paper_fill_to_dict(item: EquityPaperFillRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "order_id": item.order_id,
        "portfolio_id": item.portfolio_id,
        "symbol": item.symbol,
        "side": item.side,
        "quantity": item.quantity,
        "price": item.price,
        "fee_usd": item.fee_usd,
        "slippage_usd": item.slippage_usd,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "metadata": item.metadata_json,
    }


def _equity_paper_position_to_dict(item: EquityPaperPositionRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "portfolio_id": item.portfolio_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "status": item.status,
        "quantity": item.quantity,
        "avg_entry_px": item.avg_entry_px,
        "mark_px": item.mark_px,
        "stop_px": item.stop_px,
        "take_profit_px": item.take_profit_px,
        "realized_pnl_usd": item.realized_pnl_usd,
        "unrealized_pnl_usd": item.unrealized_pnl_usd,
        "opened_at": item.opened_at.isoformat() if item.opened_at else None,
        "closed_at": item.closed_at.isoformat() if item.closed_at else None,
        "metadata": item.metadata_json,
    }


def _signal_evaluation_to_dict(item: SignalEvaluationRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "side": item.side,
        "signal_type": item.signal_type,
        "status": item.status,
        "created_at_ms": item.created_at_ms,
        "completed_at_ms": item.completed_at_ms,
        "entry": item.entry_px,
        "stop": item.stop_px,
        "take_profit": item.take_profit_px,
        "signal_score": item.signal_score,
        "signal_confidence": item.signal_confidence,
        "signal_status_at_eval_start": item.signal_status_at_eval_start,
        "first_price": item.first_price,
        "latest_price": item.latest_price,
        "latest_price_at_ms": item.latest_price_at_ms,
        "max_favorable_price": item.max_favorable_price,
        "max_adverse_price": item.max_adverse_price,
        "max_favorable_bps": item.max_favorable_bps,
        "max_adverse_bps": item.max_adverse_bps,
        "max_favorable_r": item.max_favorable_r,
        "max_adverse_r": item.max_adverse_r,
        "stop_hit": item.stop_hit,
        "stop_hit_at_ms": item.stop_hit_at_ms,
        "take_profit_hit": item.take_profit_hit,
        "take_profit_hit_at_ms": item.take_profit_hit_at_ms,
        "terminal_outcome": item.terminal_outcome,
        "realized_or_marked_r": item.realized_or_marked_r,
        "opportunity_cost_r": item.opportunity_cost_r,
        "approved": item.approved,
        "rejected": item.rejected,
        "paper_ordered": item.paper_ordered,
        "paper_position_id": item.paper_position_id,
        "feature_snapshot": item.feature_snapshot_json,
        "evidence_snapshot": item.evidence_snapshot_json,
        "market_regime": item.market_regime,
        "error": item.error,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _signal_evaluation_mark_to_dict(item: SignalEvaluationMarkRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "evaluation_id": item.evaluation_id,
        "signal_id": item.signal_id,
        "symbol": item.symbol,
        "horizon": item.horizon,
        "due_at_ms": item.due_at_ms,
        "marked_at_ms": item.marked_at_ms,
        "price": item.price,
        "direction_adjusted_return_bps": item.direction_adjusted_return_bps,
        "r_multiple": item.r_multiple,
        "mfe_bps_until_mark": item.mfe_bps_until_mark,
        "mae_bps_until_mark": item.mae_bps_until_mark,
        "mfe_r_until_mark": item.mfe_r_until_mark,
        "mae_r_until_mark": item.mae_r_until_mark,
        "stop_hit_before_mark": item.stop_hit_before_mark,
        "take_profit_hit_before_mark": item.take_profit_hit_before_mark,
        "status": item.status,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _alpha_event_evaluation_to_dict(item: AlphaEventEvaluationRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "event_id": item.event_id,
        "event_source": item.event_source,
        "provider": item.provider,
        "event_type": item.event_type,
        "asset_class": item.asset_class,
        "symbol": item.symbol,
        "direction": item.direction,
        "sentiment": item.sentiment,
        "status": item.status,
        "terminal_outcome": item.terminal_outcome,
        "received_at_ms": item.received_at_ms,
        "completed_at_ms": item.completed_at_ms,
        "headline": item.headline,
        "url": item.url,
        "importance_score": item.importance_score,
        "source_score": item.source_score,
        "urgency": item.urgency,
        "freshness": item.freshness,
        "market_regime": item.market_regime,
        "reference_price": item.reference_price,
        "reference_price_at_ms": item.reference_price_at_ms,
        "latest_price": item.latest_price,
        "latest_price_at_ms": item.latest_price_at_ms,
        "max_favorable_price": item.max_favorable_price,
        "max_adverse_price": item.max_adverse_price,
        "max_favorable_bps": item.max_favorable_bps,
        "max_adverse_bps": item.max_adverse_bps,
        "max_abs_move_bps": item.max_abs_move_bps,
        "realized_or_marked_bps": item.realized_or_marked_bps,
        "linked_signal_ids": item.linked_signal_ids_json,
        "error": item.error,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _alpha_event_evaluation_mark_to_dict(item: AlphaEventEvaluationMarkRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "evaluation_id": item.evaluation_id,
        "event_id": item.event_id,
        "symbol": item.symbol,
        "asset_class": item.asset_class,
        "horizon": item.horizon,
        "due_at_ms": item.due_at_ms,
        "marked_at_ms": item.marked_at_ms,
        "price": item.price,
        "direction_adjusted_return_bps": item.direction_adjusted_return_bps,
        "abs_move_bps": item.abs_move_bps,
        "max_favorable_bps_until_mark": item.max_favorable_bps_until_mark,
        "max_adverse_bps_until_mark": item.max_adverse_bps_until_mark,
        "max_abs_move_bps_until_mark": item.max_abs_move_bps_until_mark,
        "status": item.status,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _memory_observation_to_dict(item: MemoryObservationRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "role": item.role,
        "symbol": item.symbol,
        "signal_type": item.signal_type,
        "market_regime": item.market_regime,
        "observation": item.observation,
        "evidence": item.evidence_json,
        "severity": item.severity,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _candidate_lesson_to_dict(item: CandidateLessonRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "lesson_type": item.lesson_type,
        "role": item.role,
        "scope": item.scope_json,
        "claim": item.claim,
        "evidence": item.evidence_json,
        "source_observation_ids": item.source_observation_ids_json,
        "source_run_ids": item.source_run_ids_json,
        "source_signal_ids": item.source_signal_ids_json,
        "sample_size": item.sample_size,
        "counterexamples": item.counterexamples_json,
        "confidence": item.confidence,
        "expected_future_behavior_change": item.expected_future_behavior_change,
        "strategy_affecting": item.strategy_affecting,
        "risk_affecting": item.risk_affecting,
        "execution_affecting": item.execution_affecting,
        "capital_allocation_affecting": item.capital_allocation_affecting,
        "status": item.status,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "metadata": item.metadata_json,
    }


def _role_lesson_to_dict(item: ShadowRoleLessonRecord | RoleLessonRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "role": item.role,
        "lesson_type": item.lesson_type,
        "scope": item.scope_json,
        "claim": item.claim,
        "instruction": item.instruction,
        "evidence": item.evidence_json,
        "source_candidate_id": item.source_candidate_id,
        "source_run_ids": item.source_run_ids_json,
        "source_signal_ids": item.source_signal_ids_json,
        "confidence": item.confidence,
        "sample_size": item.sample_size,
        "counterexamples": item.counterexamples_json,
        "validation_status": item.validation_status,
        "strategy_affecting": item.strategy_affecting,
        "risk_affecting": item.risk_affecting,
        "execution_affecting": item.execution_affecting,
        "capital_allocation_affecting": item.capital_allocation_affecting,
        "created_at_ms": item.created_at_ms,
        "activated_at_ms": item.activated_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "last_revalidated_at_ms": item.last_revalidated_at_ms,
        "memory_status": item.memory_status,
        "allowed_contexts": item.allowed_contexts_json,
        "forbidden_contexts": item.forbidden_contexts_json,
        "promotion_history": item.promotion_history_json,
        "rollback_target": item.rollback_target,
        "metadata": item.metadata_json,
    }


def _operator_lesson_to_dict(item: OperatorOutputLessonRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "scope": item.scope_json,
        "issue_or_pattern": item.issue_or_pattern,
        "preferred_behavior": item.preferred_behavior,
        "bad_examples": item.bad_examples_json,
        "good_examples": item.good_examples_json,
        "confidence": item.confidence,
        "sample_size": item.sample_size,
        "validation_status": item.validation_status,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "metadata": item.metadata_json,
    }


def _operator_feedback_to_dict(item: OperatorFeedbackRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "actor_id": item.actor_id,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "rating": item.rating,
        "note": item.note,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _review_packet_to_dict(item: ReviewPacketRecord) -> dict[str, Any]:
    return {
        "review_packet_id": item.review_packet_id,
        "proposal_id": item.proposal_id,
        "evidence_links": item.evidence_links_json,
        "affected_strategies": item.affected_strategies_json,
        "affected_symbols": item.affected_symbols_json,
        "affected_venues": item.affected_venues_json,
        "risk_direction": item.risk_direction,
        "expected_effect": item.expected_effect,
        "known_risks": item.known_risks_json,
        "replay_results": item.replay_results_json,
        "shadow_results": item.shadow_results_json,
        "reviewer_findings": item.reviewer_findings_json,
        "approval_requirements": item.approval_requirements_json,
        "rollback_plan_id": item.rollback_plan_id,
        "created_at_ms": item.created_at_ms,
    }


def _replay_result_to_dict(item: ReplayResultRecord) -> dict[str, Any]:
    return {
        "replay_id": item.replay_id,
        "proposal_id": item.proposal_id,
        "decision_id": item.decision_id,
        "status": item.status,
        "baseline_metrics": item.baseline_metrics_json,
        "candidate_metrics": item.candidate_metrics_json,
        "diffs": item.diffs_json,
        "caveats": item.caveats_json,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _shadow_comparison_to_dict(item: ShadowComparisonRecord) -> dict[str, Any]:
    return {
        "comparison_id": item.comparison_id,
        "proposal_id": item.proposal_id,
        "status": item.status,
        "baseline_metrics": item.baseline_metrics_json,
        "candidate_metrics": item.candidate_metrics_json,
        "metric_deltas": item.metric_deltas_json,
        "recommendation": item.recommendation,
        "created_at_ms": item.created_at_ms,
        "metadata": item.metadata_json,
    }


def _candidate_config_diff_to_dict(item: CandidateConfigDiffRecord) -> dict[str, Any]:
    return {
        "proposal_id": item.proposal_id,
        "strategy_id": item.strategy_id,
        "scope": item.scope_json,
        "change_type": item.change_type,
        "current_value": item.current_value_json,
        "proposed_value": item.proposed_value_json,
        "rationale": item.rationale,
        "evidence": item.evidence_json,
        "expected_effect": item.expected_effect,
        "known_risks": item.known_risks_json,
        "validation_required": item.validation_required_json,
        "risk_direction": item.risk_direction,
        "requires_human_approval": item.requires_human_approval,
        "auto_apply_allowed": item.auto_apply_allowed,
        "created_by": item.created_by,
        "created_at_ms": item.created_at_ms,
        "status": item.status,
        "metadata": item.metadata_json,
    }


def _tuning_proposal_to_dict(item: TuningProposalRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "proposal_type": item.proposal_type,
        "status": item.status,
        "title": item.title,
        "summary": item.summary,
        "affected_scope": item.affected_scope_json,
        "current_behavior": item.current_behavior_json,
        "proposed_diff": item.proposed_diff_json,
        "evidence": item.evidence_json,
        "source_lesson_ids": item.source_lesson_ids_json,
        "source_signal_ids": item.source_signal_ids_json,
        "strategy_id": item.strategy_id,
        "change_type": item.change_type,
        "risk_direction": item.risk_direction,
        "requires_human_approval": item.requires_human_approval,
        "validation_required": item.validation_required_json,
        "known_risks": item.known_risks_json,
        "review_packet_id": item.review_packet_id,
        "candidate_diff_status": item.candidate_diff_status,
        "expected_impact": item.expected_impact,
        "risk_assessment": item.risk_assessment,
        "blast_radius": item.blast_radius,
        "rollback_plan": item.rollback_plan,
        "confidence": item.confidence,
        "sample_size": item.sample_size,
        "created_at_ms": item.created_at_ms,
        "expires_at_ms": item.expires_at_ms,
        "evaluation_window": item.evaluation_window,
        "metadata": item.metadata_json,
    }


def _token_capital_snapshot_to_dict(item: TokenCapitalSnapshotRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "timestamp_ms": item.timestamp_ms,
        "window": item.window,
        "total_score": item.total_score,
        "risk_adjusted_performance_score": item.risk_adjusted_performance_score,
        "signal_quality_score": item.signal_quality_score,
        "memory_compounding_score": item.memory_compounding_score,
        "risk_discipline_score": item.risk_discipline_score,
        "operator_communication_score": item.operator_communication_score,
        "reliability_score": item.reliability_score,
        "hard_gate_penalties": item.hard_gate_penalties_json,
        "component_details": item.component_details_json,
        "created_from_report_id": item.created_from_report_id,
        "metadata": item.metadata_json,
    }


def _autonomy_report_to_dict(item: DailyReportRecord | WeeklyReportRecord, report_type: str) -> dict[str, Any]:
    key = item.week_key if isinstance(item, WeeklyReportRecord) else item.report_date
    return {
        "id": item.id,
        "report_type": report_type,
        "key": key,
        "period_start_ms": item.period_start_ms,
        "period_end_ms": item.period_end_ms,
        "generated_at_ms": item.generated_at_ms,
        "token_capital_score": item.token_capital_score,
        "summary": item.summary,
        "report": item.report_json,
        "discord_channel_id": item.discord_channel_id,
        "discord_message_id": item.discord_message_id,
        "metadata": item.metadata_json,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _level_to_dict(level: TrackedLevel) -> dict[str, Any]:
    return {
        "id": level.id,
        "tracker_id": level.tracker_id,
        "kind": level.kind,
        "label": level.label,
        "price": level.price,
        "direction": level.direction,
        "terminal": level.terminal,
        "severity": level.severity,
        "armed": level.armed,
        "hit_count": level.hit_count,
        "rearm_band_bps": level.rearm_band_bps,
        "last_triggered_at": level.last_triggered_at.isoformat() if level.last_triggered_at else None,
        "metadata": level.metadata_json,
        "created_at": level.created_at.isoformat() if level.created_at else None,
    }


def _event_to_dict(event: TrackingEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tracker_id": event.tracker_id,
        "level_id": event.level_id,
        "event_type": event.event_type,
        "coin": event.coin,
        "price": event.price,
        "payload": event.payload_json,
        "alert_destination": event.alert_destination,
        "alert_status": event.alert_status,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
