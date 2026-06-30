from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _id() -> str:
    return uuid4().hex


class ConversationThread(TimestampMixin, Base):
    __tablename__ = "conversation_threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    discord_guild_id: Mapped[str | None] = mapped_column(String(32))
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    discord_thread_id: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255), default="")
    messages: Mapped[list[ConversationMessage]] = relationship(back_populates="thread")


class ConversationMessage(TimestampMixin, Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    thread_id: Mapped[str] = mapped_column(ForeignKey("conversation_threads.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    discord_user_id: Mapped[str | None] = mapped_column(String(32))
    thread: Mapped[ConversationThread] = relationship(back_populates="messages")


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ToolCall(TimestampMixin, Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class CacheItem(TimestampMixin, Base):
    __tablename__ = "cache_items"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsItem(TimestampMixin, Base):
    __tablename__ = "news_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperTradeIdea(TimestampMixin, Base):
    __tablename__ = "paper_trade_ideas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    discord_user_id: Mapped[str | None] = mapped_column(String(32))
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    thesis: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperTradeSnapshot(TimestampMixin, Base):
    __tablename__ = "paper_trade_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    idea_id: Mapped[str] = mapped_column(ForeignKey("paper_trade_ideas.id"), nullable=False)
    market_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DecisionRun(TimestampMixin, Base):
    __tablename__ = "decision_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    actor: Mapped[str] = mapped_column(String(128), default="")
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(64), default="started")
    round_count: Mapped[int] = mapped_column(Integer, default=0)
    final_summary: Mapped[str] = mapped_column(Text, default="")
    proposal_id: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionRoleOutput(TimestampMixin, Base):
    __tablename__ = "decision_role_outputs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("decision_runs.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), default="ok")
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_content: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class DecisionStateSnapshot(TimestampMixin, Base):
    __tablename__ = "decision_state_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("decision_runs.id"), nullable=False)
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    node: Mapped[str] = mapped_column(String(128), nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ConfigVersionRecord(TimestampMixin, Base):
    __tablename__ = "config_versions"
    __table_args__ = (
        Index("ix_config_versions_scope_active", "scope", "active"),
        Index("ix_config_versions_hash", "version_hash"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    version_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PromptVersionRecord(TimestampMixin, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index("ix_prompt_versions_name_active", "prompt_name", "active"),
        Index("ix_prompt_versions_hash", "version_hash"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    prompt_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DecisionContextRecord(TimestampMixin, Base):
    __tablename__ = "decision_contexts"
    __table_args__ = (
        Index("ix_decision_contexts_source", "source_type", "source_id"),
        Index("ix_decision_contexts_run_id", "run_id"),
        Index("ix_decision_contexts_created_at_ms", "created_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    source_id: Mapped[str | None] = mapped_column(String(96))
    run_id: Mapped[str | None] = mapped_column(String(64))
    config_version_id: Mapped[str] = mapped_column(String(96), nullable=False)
    risk_config_version_id: Mapped[str] = mapped_column(String(96), nullable=False)
    model_route_version_id: Mapped[str | None] = mapped_column(String(96))
    prompt_version_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    injected_memory_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    market_snapshot_refs_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    data_freshness_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RiskGatewayDecisionRecord(TimestampMixin, Base):
    __tablename__ = "risk_gateway_decisions"
    __table_args__ = (
        Index("ix_risk_gateway_decisions_intent", "intent_id"),
        Index("ix_risk_gateway_decisions_created_at_ms", "created_at_ms"),
    )

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(96), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    violations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    limits_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    market_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    portfolio_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_version_id: Mapped[str | None] = mapped_column(String(96))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemoryInjectionEventRecord(TimestampMixin, Base):
    __tablename__ = "memory_injection_events"
    __table_args__ = (
        Index("ix_memory_injection_events_run_role", "run_id", "role"),
        Index("ix_memory_injection_events_created_at_ms", "created_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    context_type: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    blocked_memory_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_decision_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WorldEventRecord(TimestampMixin, Base):
    __tablename__ = "world_events"
    __table_args__ = (
        Index("ix_world_events_source_type", "source_type"),
        Index("ix_world_events_received", "received_ts_ms"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    provider: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    asset_class: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    topics_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    event_ts_ms: Mapped[int | None] = mapped_column(BigInteger)
    received_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sentiment: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    staleness_ms: Mapped[int | None] = mapped_column(BigInteger)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MarketBeliefRecord(TimestampMixin, Base):
    __tablename__ = "market_beliefs"
    __table_args__ = (
        Index("ix_market_beliefs_kind_status", "kind", "status"),
        Index("ix_market_beliefs_subject", "subject"),
        Index("ix_market_beliefs_updated", "updated_at_ms"),
    )

    belief_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    topics_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    direction: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    probability: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    contradicts_belief_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NarrativeClusterRecord(TimestampMixin, Base):
    __tablename__ = "narrative_clusters"
    __table_args__ = (
        Index("ix_narrative_clusters_updated", "updated_at_ms"),
        Index("ix_narrative_clusters_pressure", "pressure_score"),
    )

    cluster_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    topics_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    belief_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    pressure_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    consensus_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    conflict_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PredictionMarketSignalRecord(TimestampMixin, Base):
    __tablename__ = "prediction_market_signals"
    __table_args__ = (
        Index("ix_prediction_market_signals_venue_market", "venue", "market_id"),
        Index("ix_prediction_market_signals_as_of", "as_of_ms"),
        Index("ix_prediction_market_signals_status", "status"),
    )

    signal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_id: Mapped[str | None] = mapped_column(String(128))
    outcome_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    topics_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    implied_probability: Mapped[float | None] = mapped_column(Float)
    probability_delta: Mapped[float | None] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_usd: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    source_event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    staleness_ms: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SourceCredibilityRecord(TimestampMixin, Base):
    __tablename__ = "source_credibility"

    source_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contradictions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WorldMemoryAtomRecord(TimestampMixin, Base):
    __tablename__ = "world_memory_atoms"
    __table_args__ = (
        Index("ix_world_memory_atoms_type", "memory_type"),
        Index("ix_world_memory_atoms_subject", "subject"),
        Index("ix_world_memory_atoms_reinforced", "last_reinforced_at_ms"),
    )

    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    topics_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_belief_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_reinforced_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WorldModelSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "world_model_snapshots"
    __table_args__ = (Index("ix_world_model_snapshots_as_of", "as_of_ms"),)

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    topics_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    top_beliefs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    narrative_clusters_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    prediction_market_signals_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_credibility_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    memory_atoms_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    quality_flags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WorldModelAnnotationRecord(TimestampMixin, Base):
    __tablename__ = "world_model_annotations"
    __table_args__ = (
        Index("ix_world_model_annotations_target", "target_type", "target_id"),
        Index("ix_world_model_annotations_created", "created_at_ms"),
    )

    annotation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WorldModelOutcomeRecord(TimestampMixin, Base):
    __tablename__ = "world_model_outcomes"
    __table_args__ = (
        Index("ix_world_model_outcomes_target", "target_type", "target_id"),
        Index("ix_world_model_outcomes_created", "created_at_ms"),
    )

    outcome_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(64))
    horizon: Mapped[str | None] = mapped_column(String(64))
    realized_value: Mapped[float | None] = mapped_column(Float)
    confidence_delta: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PredictionMarketCalibrationRecord(TimestampMixin, Base):
    __tablename__ = "prediction_market_calibrations"
    __table_args__ = (
        Index("ix_prediction_market_calibrations_signal", "signal_id"),
        Index("ix_prediction_market_calibrations_venue_market", "venue", "market_id"),
    )

    calibration_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    implied_probability: Mapped[float | None] = mapped_column(Float)
    realized_outcome: Mapped[float | None] = mapped_column(Float)
    brier_score: Mapped[float | None] = mapped_column(Float)
    settled_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TradeProposalRecord(TimestampMixin, Base):
    __tablename__ = "trade_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("decision_runs.id"))
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    coin: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str | None] = mapped_column(String(16))
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content: Mapped[str] = mapped_column(Text, default="")


class PositionTracker(TimestampMixin, Base):
    __tablename__ = "position_trackers"
    __table_args__ = (
        Index("ix_position_trackers_status_coin", "status", "coin"),
        Index("ix_position_trackers_discord_thread", "discord_thread_id"),
        Index("ix_position_trackers_proposal_id", "proposal_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("trade_proposals.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("decision_runs.id"))
    source: Mapped[str] = mapped_column(String(64), default="auto_high_stakes", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_px: Mapped[float] = mapped_column(Float, nullable=False)
    stop_px: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    current_px: Mapped[float | None] = mapped_column(Float)
    last_px: Mapped[float | None] = mapped_column(Float)
    last_price_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    price_source: Mapped[str] = mapped_column(String(32), default="allMids", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discord_guild_id: Mapped[str | None] = mapped_column(String(32))
    discord_channel_id: Mapped[str | None] = mapped_column(String(32))
    discord_thread_id: Mapped[str | None] = mapped_column(String(32))
    discord_user_id: Mapped[str | None] = mapped_column(String(32))
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    levels: Mapped[list[TrackedLevel]] = relationship(back_populates="tracker")
    events: Mapped[list[TrackingEvent]] = relationship(back_populates="tracker")


class TrackedLevel(TimestampMixin, Base):
    __tablename__ = "tracked_levels"
    __table_args__ = (
        Index("ix_tracked_levels_tracker_id", "tracker_id"),
        Index("ix_tracked_levels_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tracker_id: Mapped[str] = mapped_column(ForeignKey("position_trackers.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rearm_band_bps: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tracker: Mapped[PositionTracker] = relationship(back_populates="levels")
    events: Mapped[list[TrackingEvent]] = relationship(back_populates="level")


class TrackingEvent(TimestampMixin, Base):
    __tablename__ = "tracking_events"
    __table_args__ = (
        Index("ix_tracking_events_tracker_id_created_at", "tracker_id", "created_at"),
        Index("ix_tracking_events_event_type", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    tracker_id: Mapped[str] = mapped_column(ForeignKey("position_trackers.id"), nullable=False)
    level_id: Mapped[str | None] = mapped_column(ForeignKey("tracked_levels.id"))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    alert_destination: Mapped[str | None] = mapped_column(String(64))
    alert_status: Mapped[str | None] = mapped_column(String(32))
    tracker: Mapped[PositionTracker] = relationship(back_populates="events")
    level: Mapped[TrackedLevel | None] = relationship(back_populates="events")


class AutonomyEvent(TimestampMixin, Base):
    __tablename__ = "autonomy_events"
    __table_args__ = (
        Index("ix_autonomy_events_event_type_created_at", "event_type", "created_at"),
        Index("ix_autonomy_events_symbol_created_at", "symbol", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), default="")
    symbol: Mapped[str | None] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MarketAssetRecord(TimestampMixin, Base):
    __tablename__ = "market_assets"

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    dex: Mapped[str | None] = mapped_column(String(64))
    sz_decimals: Mapped[int | None] = mapped_column(Integer)
    max_leverage: Mapped[int | None] = mapped_column(Integer)
    day_volume_usd: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketObservation(TimestampMixin, Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        Index("ix_market_observations_symbol_timestamp", "symbol", "timestamp_ms"),
        Index("ix_market_observations_timestamp_ms", "timestamp_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mid: Mapped[float | None] = mapped_column(Float)
    mark: Mapped[float | None] = mapped_column(Float)
    oracle: Mapped[float | None] = mapped_column(Float)
    funding_hourly: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)
    day_volume_usd: Mapped[float | None] = mapped_column(Float)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MarketLevelRecord(TimestampMixin, Base):
    __tablename__ = "market_levels"
    __table_args__ = (
        Index("ix_market_levels_symbol", "symbol"),
        Index("ix_market_levels_kind", "kind"),
        Index("ix_market_levels_symbol_kind_price", "symbol", "kind", "price"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    first_seen_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AutonomyNewsEvent(TimestampMixin, Base):
    __tablename__ = "news_events"
    __table_args__ = (
        Index("ix_news_events_observed_at_ms", "observed_at_ms"),
        Index("ix_news_events_provider", "provider"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str | None] = mapped_column(String(64))
    created_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment: Mapped[str] = mapped_column(String(32), nullable=False)
    assets_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NewswireEventRow(TimestampMixin, Base):
    __tablename__ = "newswire_events"
    __table_args__ = (
        Index("ix_newswire_events_received_at_ms", "received_at_ms"),
        Index("ix_newswire_events_source", "source"),
        Index("ix_newswire_events_event_type", "event_type"),
        Index("ix_newswire_events_asset_class", "asset_class"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(128))
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, default="headline")
    urgency: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    importance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    freshness: Mapped[str] = mapped_column(String(16), nullable=False, default="fresh")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tradability_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enrichment_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NormalizedEventRecord(TimestampMixin, Base):
    __tablename__ = "normalized_events"
    __table_args__ = (
        Index("ix_normalized_events_received_at_ms", "received_ts_ms"),
        Index("ix_normalized_events_event_type_received", "event_type", "received_ts_ms"),
        Index("ix_normalized_events_asset_class_received", "asset_class", "received_ts_ms"),
    )

    event_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(96), nullable=False)
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    event_ts_ms: Mapped[int | None] = mapped_column(BigInteger)
    received_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    staleness_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FeatureValueRecord(TimestampMixin, Base):
    __tablename__ = "feature_values"
    __table_args__ = (
        Index("ix_feature_values_asset_feature_computed", "asset", "feature_name", "computed_ts_ms"),
        Index("ix_feature_values_source_event", "source_event_id"),
        Index("ix_feature_values_group_computed", "feature_group", "computed_ts_ms"),
    )

    feature_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_group: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scalar_value: Mapped[float | None] = mapped_column(Float)
    event_ts_ms: Mapped[int | None] = mapped_column(BigInteger)
    received_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(96))
    source: Mapped[str] = mapped_column(String(96), nullable=False)
    version: Mapped[str] = mapped_column(String(96), nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    staleness_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FeatureRollupRecord(TimestampMixin, Base):
    __tablename__ = "feature_rollups"
    __table_args__ = (
        Index("ix_feature_rollups_asset_feature_window", "asset", "feature_name", "window_start_ms"),
        Index("ix_feature_rollups_interval_window", "interval", "window_start_ms"),
    )

    rollup_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_group: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_name: Mapped[str] = mapped_column(String(128), nullable=False)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    avg_value: Mapped[float | None] = mapped_column(Float)
    last_value: Mapped[float | None] = mapped_column(Float)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_avg: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RegimeSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "regime_snapshots"
    __table_args__ = (
        Index("ix_regime_snapshots_created_at_ms", "created_at_ms"),
        Index("ix_regime_snapshots_primary_asset_created", "primary_asset", "created_at_ms"),
    )

    regime_snapshot_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    primary_asset: Mapped[str] = mapped_column(String(64), nullable=False, default="GLOBAL")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vector_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    feature_refs_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    quality_flags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class StrategySpecRecord(TimestampMixin, Base):
    __tablename__ = "strategy_specs"
    __table_args__ = (
        Index("ix_strategy_specs_family", "family"),
        Index("ix_strategy_specs_enabled", "enabled"),
    )

    strategy_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    family: Mapped[str] = mapped_column(String(96), nullable=False)
    supported_assets_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    supported_venues_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    supported_horizons_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_features_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    valid_regimes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_candidates_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_allocation_share_pct: Mapped[float] = mapped_column(Float, nullable=False, default=45.0)
    cooldown_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    min_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    min_ev_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    risk_tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    counts_for_breadth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class StrategyRegimePerformanceRecord(TimestampMixin, Base):
    __tablename__ = "strategy_regime_performance"
    __table_args__ = (
        Index("ix_strategy_regime_performance_strategy", "strategy_id"),
        Index("ix_strategy_regime_performance_regime", "regime_label"),
        Index("ix_strategy_regime_performance_window", "window_end_ms"),
    )

    performance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    strategy_family: Mapped[str] = mapped_column(String(96), nullable=False, default="unknown")
    regime_label: Mapped[str] = mapped_column(String(255), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False, default="GLOBAL")
    window_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allocation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_net_ev_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AlphaCandidateRecord(TimestampMixin, Base):
    __tablename__ = "alpha_candidates"
    __table_args__ = (
        Index("ix_alpha_candidates_status_created", "status", "created_at_ms"),
        Index("ix_alpha_candidates_asset_status", "asset", "status"),
        Index("ix_alpha_candidates_strategy_created", "strategy_id", "created_at_ms"),
    )

    candidate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="crypto")
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    proposed_entry: Mapped[float] = mapped_column(Float, nullable=False)
    stop: Mapped[float] = mapped_column(Float, nullable=False)
    targets_json: Mapped[list[float]] = mapped_column(JSON, default=list)
    thesis: Mapped[str] = mapped_column(Text, default="", nullable=False)
    invalidation_conditions_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    feature_snapshot_id: Mapped[str] = mapped_column(String(96), nullable=False)
    regime_snapshot_id: Mapped[str] = mapped_column(String(96), nullable=False)
    source_event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_alpha_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CandidateBookSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "candidate_book_snapshots"
    __table_args__ = (Index("ix_candidate_book_snapshots_created", "created_at_ms"),)

    candidate_book_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    ranked_candidate_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rejected_candidate_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    portfolio_state_ref: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EVEstimateRecord(TimestampMixin, Base):
    __tablename__ = "ev_estimates"
    __table_args__ = (
        Index("ix_ev_estimates_candidate", "candidate_id"),
        Index("ix_ev_estimates_created_at_ms", "created_at_ms"),
    )

    estimate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    p_target: Mapped[float] = mapped_column(Float, nullable=False)
    p_stop: Mapped[float] = mapped_column(Float, nullable=False)
    p_timeout: Mapped[float] = mapped_column(Float, nullable=False)
    expected_favorable_bps: Mapped[float] = mapped_column(Float, nullable=False)
    expected_adverse_bps: Mapped[float] = mapped_column(Float, nullable=False)
    expected_holding_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_fee_bps: Mapped[float] = mapped_column(Float, nullable=False)
    expected_spread_cost_bps: Mapped[float] = mapped_column(Float, nullable=False)
    expected_slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    expected_market_impact_bps: Mapped[float] = mapped_column(Float, nullable=False)
    expected_funding_cost_bps: Mapped[float] = mapped_column(Float, nullable=False)
    tail_loss_bps: Mapped[float] = mapped_column(Float, nullable=False)
    net_ev_bps: Mapped[float] = mapped_column(Float, nullable=False)
    risk_adjusted_utility: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainty: Mapped[float] = mapped_column(Float, nullable=False)
    calibration_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AllocationDecisionRecord(TimestampMixin, Base):
    __tablename__ = "allocation_decisions"
    __table_args__ = (
        Index("ix_allocation_decisions_candidate", "candidate_id"),
        Index("ix_allocation_decisions_created", "created_at_ms"),
    )

    allocation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    candidate_book_id: Mapped[str | None] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    allocated_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    allocated_notional_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    risk_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_size_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    opportunity_cost_rank: Mapped[int | None] = mapped_column(Integer)
    constraints_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AllocationDiversityEventRecord(TimestampMixin, Base):
    __tablename__ = "allocation_diversity_events"
    __table_args__ = (
        Index("ix_allocation_diversity_events_candidate", "candidate_id"),
        Index("ix_allocation_diversity_events_strategy", "strategy_id"),
        Index("ix_allocation_diversity_events_created", "created_at_ms"),
        Index("ix_allocation_diversity_events_decision", "decision"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    allocation_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    strategy_family: Mapped[str] = mapped_column(String(96), nullable=False, default="unknown")
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CandidateTradePacketRecord(TimestampMixin, Base):
    __tablename__ = "candidate_trade_packets"
    __table_args__ = (
        Index("ix_candidate_trade_packets_candidate", "candidate_id"),
        Index("ix_candidate_trade_packets_strategy", "strategy_id"),
        Index("ix_candidate_trade_packets_created", "created_at_ms"),
    )

    packet_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    strategy_family: Mapped[str] = mapped_column(String(96), nullable=False, default="unknown")
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(96), nullable=False)
    regime_snapshot_id: Mapped[str] = mapped_column(String(96), nullable=False)
    packet_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CouncilReviewRecord(TimestampMixin, Base):
    __tablename__ = "council_reviews"
    __table_args__ = (
        Index("ix_council_reviews_candidate", "candidate_id"),
        Index("ix_council_reviews_strategy", "strategy_id"),
        Index("ix_council_reviews_decision", "decision"),
        Index("ix_council_reviews_created", "created_at_ms"),
    )

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    packet_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    vetoes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_evidence_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    regime_fit_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strategy_regime_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    portfolio_impact_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CouncilVoteRecord(TimestampMixin, Base):
    __tablename__ = "council_votes"
    __table_args__ = (
        Index("ix_council_votes_review", "review_id"),
        Index("ix_council_votes_role", "role"),
        Index("ix_council_votes_created", "created_at_ms"),
    )

    vote_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    review_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    vetoes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_evidence_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BanditPolicySnapshotRecord(TimestampMixin, Base):
    __tablename__ = "bandit_policy_snapshots"
    __table_args__ = (
        Index("ix_bandit_policy_snapshots_status", "status"),
        Index("ix_bandit_policy_snapshots_created", "created_at_ms"),
    )

    policy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="report_only")
    trained_window_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trained_window_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_features_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    arms_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BanditRecommendationRecord(TimestampMixin, Base):
    __tablename__ = "bandit_recommendations"
    __table_args__ = (
        Index("ix_bandit_recommendations_policy", "policy_id"),
        Index("ix_bandit_recommendations_strategy", "strategy_id"),
        Index("ix_bandit_recommendations_created", "created_at_ms"),
    )

    recommendation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False, default="GLOBAL")
    regime_label: Mapped[str] = mapped_column(String(255), nullable=False, default="unknown")
    recommendation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    expected_score_delta: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    auto_apply_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvidencePackRecord(TimestampMixin, Base):
    __tablename__ = "evidence_packs"
    __table_args__ = (Index("ix_evidence_packs_candidate", "candidate_id"),)

    evidence_pack_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_snapshot_id: Mapped[str] = mapped_column(String(96), nullable=False)
    pack_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DebateDecisionRecord(TimestampMixin, Base):
    __tablename__ = "debate_decisions"
    __table_args__ = (
        Index("ix_debate_decisions_candidate", "candidate_id"),
        Index("ix_debate_decisions_evidence_pack", "evidence_pack_id"),
        Index("ix_debate_decisions_created", "created_at_ms"),
    )

    debate_decision_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    evidence_pack_id: Mapped[str] = mapped_column(String(96), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_adjustment: Mapped[float] = mapped_column(Float, nullable=False)
    max_size_multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_invalidation_checks_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    audit_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    role_outputs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    judge_model: Mapped[str | None] = mapped_column(String(255))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OrderIntentRecord(TimestampMixin, Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        Index("ix_order_intents_candidate", "parent_candidate_id"),
        Index("ix_order_intents_created", "created_at_ms"),
        Index("ix_order_intents_mode", "execution_mode"),
    )

    intent_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    parent_candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    portfolio_decision_id: Mapped[str] = mapped_column(String(96), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="crypto")
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(32), nullable=False)
    target_size: Mapped[float] = mapped_column(Float, nullable=False)
    target_notional_usd: Mapped[float] = mapped_column(Float, nullable=False)
    max_slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    price_limit: Mapped[float | None] = mapped_column(Float)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    post_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deadline_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    model_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_budget_id: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ExecutionReportRecord(TimestampMixin, Base):
    __tablename__ = "execution_reports"
    __table_args__ = (
        Index("ix_execution_reports_intent", "intent_id"),
        Index("ix_execution_reports_created", "created_at_ms"),
        Index("ix_execution_reports_mode_status", "execution_mode", "status"),
    )

    report_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(96), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_size: Mapped[float] = mapped_column(Float, nullable=False)
    filled_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_fill_px: Mapped[float | None] = mapped_column(Float)
    fees_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    market_impact_bps: Mapped[float | None] = mapped_column(Float)
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    assumptions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PositionThesisRecord(TimestampMixin, Base):
    __tablename__ = "position_theses"
    __table_args__ = (
        Index("ix_position_theses_asset_state", "asset", "position_state"),
        Index("ix_position_theses_candidate", "entry_candidate_id"),
    )

    position_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    entry_candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="crypto")
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expected_horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    stop: Mapped[float] = mapped_column(Float, nullable=False)
    targets_json: Mapped[list[float]] = mapped_column(JSON, default=list)
    invalidation_rules_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    thesis_features_at_entry_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_thesis_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    degradation_reasons_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    position_state: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    execution_report_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    opened_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    closed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReconciliationRunRecord(TimestampMixin, Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (Index("ix_reconciliation_runs_started", "started_at_ms"),)

    reconciliation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_positions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    observed_positions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    mismatches_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PnLAttributionRecord(TimestampMixin, Base):
    __tablename__ = "pnl_attribution_records"
    __table_args__ = (
        Index("ix_pnl_attribution_asset_window", "asset", "window_start_ms", "window_end_ms"),
        Index("ix_pnl_attribution_strategy", "strategy_id"),
    )

    attribution_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    position_id: Mapped[str | None] = mapped_column(String(96))
    candidate_id: Mapped[str | None] = mapped_column(String(96))
    strategy_id: Mapped[str] = mapped_column(String(96), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    alpha_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    timing_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    execution_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fees_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    funding_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    residual_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class KillSwitchEventRecord(TimestampMixin, Base):
    __tablename__ = "kill_switch_events"
    __table_args__ = (
        Index("ix_kill_switch_events_scope_action", "scope", "action"),
        Index("ix_kill_switch_events_created", "created_at_ms"),
    )

    event_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    affected_assets_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_strategies_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    block_new_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cancel_open_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    freeze_config_changes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModelVersionRecord(TimestampMixin, Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        Index("ix_model_versions_status", "status"),
        Index("ix_model_versions_model_type", "model_type"),
    )

    model_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_type: Mapped[str] = mapped_column(String(96), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False)
    training_data_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModelTrainingRunRecord(TimestampMixin, Base):
    __tablename__ = "model_training_runs"
    __table_args__ = (Index("ix_model_training_runs_created", "created_at_ms"),)

    training_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_version_id: Mapped[str | None] = mapped_column(String(128))
    model_type: Mapped[str] = mapped_column(String(96), nullable=False)
    dataset_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_data_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FeatureSchemaVersionRecord(TimestampMixin, Base):
    __tablename__ = "feature_schema_versions"

    feature_schema_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    feature_definitions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RetentionRunRecord(TimestampMixin, Base):
    __tablename__ = "retention_runs"
    __table_args__ = (Index("ix_retention_runs_started", "started_at_ms"),)

    retention_run_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    deleted_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    rollup_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    caveats_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TradeSignalRecord(TimestampMixin, Base):
    __tablename__ = "trade_signals"
    __table_args__ = (
        Index("ix_trade_signals_symbol", "symbol"),
        Index("ix_trade_signals_status", "status"),
        Index("ix_trade_signals_created_at_ms", "created_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_px: Mapped[float] = mapped_column(Float, nullable=False)
    stop_px: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    thesis: Mapped[str] = mapped_column(Text, default="")
    invalidation: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    feature_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_insight_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    discord_channel_id: Mapped[str | None] = mapped_column(String(64))
    discord_message_id: Mapped[str | None] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(32), default="crypto", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approved_by_discord_user_id: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by_discord_user_id: Mapped[str | None] = mapped_column(String(64))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperPortfolioRecord(TimestampMixin, Base):
    __tablename__ = "paper_portfolios"
    __table_args__ = (Index("uq_paper_portfolios_name", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperOrderRecord(TimestampMixin, Base):
    __tablename__ = "paper_orders"
    __table_args__ = (
        Index("ix_paper_orders_symbol", "symbol"),
        Index("ix_paper_orders_status", "status"),
        Index("ix_paper_orders_signal_id", "signal_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id"), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("trade_signals.id"))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    requested_px: Mapped[float | None] = mapped_column(Float)
    filled_px: Mapped[float | None] = mapped_column(Float)
    stop_px: Mapped[float | None] = mapped_column(Float)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    fee_bps: Mapped[float] = mapped_column(Float, nullable=False)
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperFillRecord(TimestampMixin, Base):
    __tablename__ = "paper_fills"
    __table_args__ = (Index("ix_paper_fills_symbol", "symbol"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("paper_orders.id"), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    fee_usd: Mapped[float] = mapped_column(Float, nullable=False)
    slippage_usd: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperPositionRecord(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        Index("ix_paper_positions_symbol", "symbol"),
        Index("ix_paper_positions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id"), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("trade_signals.id"))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    avg_entry_px: Mapped[float] = mapped_column(Float, nullable=False)
    mark_px: Mapped[float | None] = mapped_column(Float)
    stop_px: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PortfolioSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_portfolio_snapshots_portfolio_timestamp", "portfolio_id", "timestamp_ms"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id"), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    gross_exposure_usd: Mapped[float] = mapped_column(Float, nullable=False)
    net_exposure_usd: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    total_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    sharpe: Mapped[float | None] = mapped_column(Float)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SignalEvaluationRecord(TimestampMixin, Base):
    __tablename__ = "signal_evaluations"
    __table_args__ = (
        Index("uq_signal_evaluations_signal_id", "signal_id", unique=True),
        Index("ix_signal_evaluations_status_symbol", "status", "symbol"),
        Index("ix_signal_evaluations_symbol_created_at_ms", "symbol", "created_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("trade_signals.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    entry_px: Mapped[float] = mapped_column(Float, nullable=False)
    stop_px: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    signal_score: Mapped[float] = mapped_column(Float, nullable=False)
    signal_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    signal_status_at_eval_start: Mapped[str] = mapped_column(String(32), nullable=False)
    first_price: Mapped[float | None] = mapped_column(Float)
    latest_price: Mapped[float | None] = mapped_column(Float)
    latest_price_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    max_favorable_price: Mapped[float | None] = mapped_column(Float)
    max_adverse_price: Mapped[float | None] = mapped_column(Float)
    max_favorable_bps: Mapped[float | None] = mapped_column(Float)
    max_adverse_bps: Mapped[float | None] = mapped_column(Float)
    max_favorable_r: Mapped[float | None] = mapped_column(Float)
    max_adverse_r: Mapped[float | None] = mapped_column(Float)
    stop_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stop_hit_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    take_profit_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    take_profit_hit_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    terminal_outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    realized_or_marked_r: Mapped[float | None] = mapped_column(Float)
    opportunity_cost_r: Mapped[float | None] = mapped_column(Float)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paper_ordered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paper_position_id: Mapped[str | None] = mapped_column(String(64))
    feature_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_snapshot_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    market_regime: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignalEvaluationMarkRecord(TimestampMixin, Base):
    __tablename__ = "signal_evaluation_marks"
    __table_args__ = (
        Index("uq_signal_evaluation_marks_signal_horizon", "signal_id", "horizon", unique=True),
        Index("ix_signal_evaluation_marks_eval", "evaluation_id"),
        Index("ix_signal_evaluation_marks_due_status", "status", "due_at_ms"),
        Index("ix_signal_evaluation_marks_symbol_due", "symbol", "due_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("signal_evaluations.id"), nullable=False)
    signal_id: Mapped[str] = mapped_column(ForeignKey("trade_signals.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    due_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    marked_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    price: Mapped[float | None] = mapped_column(Float)
    direction_adjusted_return_bps: Mapped[float | None] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    mfe_bps_until_mark: Mapped[float | None] = mapped_column(Float)
    mae_bps_until_mark: Mapped[float | None] = mapped_column(Float)
    mfe_r_until_mark: Mapped[float | None] = mapped_column(Float)
    mae_r_until_mark: Mapped[float | None] = mapped_column(Float)
    stop_hit_before_mark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    take_profit_hit_before_mark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AlphaEventEvaluationRecord(TimestampMixin, Base):
    __tablename__ = "alpha_event_evaluations"
    __table_args__ = (
        Index("uq_alpha_event_evaluations_event_symbol", "event_id", "symbol", unique=True),
        Index("ix_alpha_event_evaluations_status_symbol", "status", "symbol"),
        Index("ix_alpha_event_evaluations_source_type", "event_source", "event_type"),
        Index("ix_alpha_event_evaluations_received_at_ms", "received_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_source: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, default="headline")
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    sentiment: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    terminal_outcome: Mapped[str] = mapped_column(String(64), nullable=False, default="open")
    received_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    headline: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    urgency: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    freshness: Mapped[str] = mapped_column(String(32), nullable=False, default="fresh")
    market_regime: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    reference_price: Mapped[float | None] = mapped_column(Float)
    reference_price_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    latest_price: Mapped[float | None] = mapped_column(Float)
    latest_price_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    max_favorable_price: Mapped[float | None] = mapped_column(Float)
    max_adverse_price: Mapped[float | None] = mapped_column(Float)
    max_favorable_bps: Mapped[float | None] = mapped_column(Float)
    max_adverse_bps: Mapped[float | None] = mapped_column(Float)
    max_abs_move_bps: Mapped[float | None] = mapped_column(Float)
    realized_or_marked_bps: Mapped[float | None] = mapped_column(Float)
    linked_signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlphaEventEvaluationMarkRecord(TimestampMixin, Base):
    __tablename__ = "alpha_event_evaluation_marks"
    __table_args__ = (
        Index("uq_alpha_event_eval_marks_event_symbol_horizon", "event_id", "symbol", "horizon", unique=True),
        Index("ix_alpha_event_eval_marks_eval", "evaluation_id"),
        Index("ix_alpha_event_eval_marks_due_status", "status", "due_at_ms"),
        Index("ix_alpha_event_eval_marks_symbol_due", "symbol", "due_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("alpha_event_evaluations.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    due_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    marked_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    price: Mapped[float | None] = mapped_column(Float)
    direction_adjusted_return_bps: Mapped[float | None] = mapped_column(Float)
    abs_move_bps: Mapped[float | None] = mapped_column(Float)
    max_favorable_bps_until_mark: Mapped[float | None] = mapped_column(Float)
    max_adverse_bps_until_mark: Mapped[float | None] = mapped_column(Float)
    max_abs_move_bps_until_mark: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemoryObservationRecord(TimestampMixin, Base):
    __tablename__ = "memory_observations"
    __table_args__ = (
        Index("ix_memory_observations_source", "source_type", "source_id"),
        Index("ix_memory_observations_role_symbol", "role", "symbol"),
        Index("ix_memory_observations_created_at_ms", "created_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str | None] = mapped_column(String(64))
    signal_type: Mapped[str | None] = mapped_column(String(64))
    market_regime: Mapped[str | None] = mapped_column(String(64))
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CandidateLessonRecord(TimestampMixin, Base):
    __tablename__ = "candidate_lessons"
    __table_args__ = (
        Index("ix_candidate_lessons_status_expires", "status", "expires_at_ms"),
        Index("ix_candidate_lessons_role_type", "role", "lesson_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lesson_type: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str | None] = mapped_column(String(64))
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_observation_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_run_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    counterexamples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    expected_future_behavior_change: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capital_allocation_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ShadowRoleLessonRecord(TimestampMixin, Base):
    __tablename__ = "shadow_role_lessons"
    __table_args__ = (
        Index("ix_shadow_role_lessons_role_status", "role", "validation_status"),
        Index("ix_shadow_role_lessons_expires", "expires_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    lesson_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_candidate_id: Mapped[str | None] = mapped_column(String(64))
    source_run_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    counterexamples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capital_allocation_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_revalidated_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    memory_status: Mapped[str] = mapped_column(String(64), nullable=False, default="validated_advisory")
    allowed_contexts_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    forbidden_contexts_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    promotion_history_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rollback_target: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RoleLessonRecord(TimestampMixin, Base):
    __tablename__ = "role_lessons"
    __table_args__ = (
        Index("ix_role_lessons_role_status", "role", "validation_status"),
        Index("ix_role_lessons_expires", "expires_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    lesson_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_candidate_id: Mapped[str | None] = mapped_column(String(64))
    source_run_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    counterexamples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    capital_allocation_affecting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_revalidated_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    memory_status: Mapped[str] = mapped_column(String(64), nullable=False, default="validated_advisory")
    allowed_contexts_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    forbidden_contexts_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    promotion_history_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rollback_target: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OperatorOutputLessonRecord(TimestampMixin, Base):
    __tablename__ = "operator_output_lessons"
    __table_args__ = (
        Index("ix_operator_output_lessons_status", "validation_status"),
        Index("ix_operator_output_lessons_expires", "expires_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    issue_or_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_behavior: Mapped[str] = mapped_column(Text, nullable=False)
    bad_examples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    good_examples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OperatorFeedbackRecord(TimestampMixin, Base):
    __tablename__ = "operator_feedback"
    __table_args__ = (
        Index("ix_operator_feedback_target", "target_type", "target_id"),
        Index("ix_operator_feedback_created_at_ms", "created_at_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rating: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --- TradFi / Equity paper trading tables (0007_tradfi) -------------------------


class EquityPaperPortfolioRecord(TimestampMixin, Base):
    __tablename__ = "equity_paper_portfolios"
    __table_args__ = (Index("uq_equity_portfolios_name", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EquityPaperOrderRecord(TimestampMixin, Base):
    __tablename__ = "equity_paper_orders"
    __table_args__ = (
        Index("ix_equity_orders_symbol", "symbol"),
        Index("ix_equity_orders_status", "status"),
        Index("ix_equity_orders_signal_id", "signal_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("equity_paper_portfolios.id"), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    requested_px: Mapped[float | None] = mapped_column(Float)
    filled_px: Mapped[float | None] = mapped_column(Float)
    stop_px: Mapped[float | None] = mapped_column(Float)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    fee_bps: Mapped[float] = mapped_column(Float, nullable=False)
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EquityPaperFillRecord(TimestampMixin, Base):
    __tablename__ = "equity_paper_fills"
    __table_args__ = (Index("ix_equity_fills_symbol", "symbol"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("equity_paper_orders.id"), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("equity_paper_portfolios.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    fee_usd: Mapped[float] = mapped_column(Float, nullable=False)
    slippage_usd: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EquityPaperPositionRecord(Base):
    __tablename__ = "equity_paper_positions"
    __table_args__ = (
        Index("ix_equity_positions_symbol", "symbol"),
        Index("ix_equity_positions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("equity_paper_portfolios.id"), nullable=False)
    signal_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    avg_entry_px: Mapped[float] = mapped_column(Float, nullable=False)
    mark_px: Mapped[float | None] = mapped_column(Float)
    stop_px: Mapped[float | None] = mapped_column(Float)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EquityPortfolioSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "equity_portfolio_snapshots"
    __table_args__ = (Index("ix_equity_snapshots_portfolio_ts", "portfolio_id", "timestamp_ms"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("equity_paper_portfolios.id"), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    gross_exposure_usd: Mapped[float] = mapped_column(Float, nullable=False)
    net_exposure_usd: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    total_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EquityOptionsFlowEventRecord(TimestampMixin, Base):
    __tablename__ = "equity_options_flow_events"
    __table_args__ = (Index("ix_equity_flow_symbol_detected", "symbol", "detected_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    flow_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    volume_oi_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    premium_estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_sweep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cluster_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    urgency_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    contract_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enrichment_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class RollbackPlanRecord(TimestampMixin, Base):
    __tablename__ = "rollback_plans"

    rollback_plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(96), nullable=False)
    previous_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rollback_steps_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    verification_steps_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ReviewPacketRecord(TimestampMixin, Base):
    __tablename__ = "review_packets"
    __table_args__ = (Index("ix_review_packets_proposal", "proposal_id"),)

    review_packet_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_links_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_strategies_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_venues_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_direction: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_effect: Mapped[str] = mapped_column(Text, default="", nullable=False)
    known_risks_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    replay_results_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    shadow_results_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reviewer_findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    approval_requirements_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rollback_plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PromotionDecisionRecord(TimestampMixin, Base):
    __tablename__ = "promotion_decisions"
    __table_args__ = (Index("ix_promotion_decisions_proposal", "proposal_id"),)

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_reviewed_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    tests_reviewed_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    proposer_actor: Mapped[str] = mapped_column(String(128), nullable=False)
    approver_actor: Mapped[str] = mapped_column(String(128), nullable=False)
    change_control_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_contexts_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rollback_plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ReplayResultRecord(TimestampMixin, Base):
    __tablename__ = "replay_results"
    __table_args__ = (
        Index("ix_replay_results_proposal", "proposal_id"),
        Index("ix_replay_results_decision", "decision_id"),
        Index("ix_replay_results_created_at_ms", "created_at_ms"),
    )

    replay_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str | None] = mapped_column(String(64))
    decision_id: Mapped[str | None] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diffs_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    caveats_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ShadowComparisonRecord(TimestampMixin, Base):
    __tablename__ = "shadow_comparisons"
    __table_args__ = (
        Index("ix_shadow_comparisons_proposal", "proposal_id"),
        Index("ix_shadow_comparisons_created_at_ms", "created_at_ms"),
    )

    comparison_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metric_deltas_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommendation: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CandidateConfigDiffRecord(TimestampMixin, Base):
    __tablename__ = "candidate_config_diffs"
    __table_args__ = (
        Index("ix_candidate_config_diffs_status_created", "status", "created_at_ms"),
        Index("ix_candidate_config_diffs_strategy", "strategy_id"),
    )

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    current_value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proposed_value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    expected_effect: Mapped[str] = mapped_column(Text, default="", nullable=False)
    known_risks_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    validation_required_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_direction: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_apply_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TuningProposalRecord(TimestampMixin, Base):
    __tablename__ = "tuning_proposals"
    __table_args__ = (
        Index("ix_tuning_proposals_status_expires", "status", "expires_at_ms"),
        Index("ix_tuning_proposals_type", "proposal_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    affected_scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_behavior_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proposed_diff_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_lesson_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, default="autonomy_v1")
    change_type: Mapped[str] = mapped_column(String(64), nullable=False, default="proposal")
    risk_direction: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    validation_required_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    known_risks_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_packet_id: Mapped[str | None] = mapped_column(String(64))
    candidate_diff_status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    expected_impact: Mapped[str] = mapped_column(Text, default="", nullable=False)
    risk_assessment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    blast_radius: Mapped[str] = mapped_column(String(32), nullable=False)
    rollback_plan: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evaluation_window: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TokenCapitalSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "token_capital_snapshots"
    __table_args__ = (Index("ix_token_capital_snapshots_window_timestamp", "window", "timestamp_ms"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window: Mapped[str] = mapped_column(String(32), nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    risk_adjusted_performance_score: Mapped[float] = mapped_column(Float, nullable=False)
    signal_quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    memory_compounding_score: Mapped[float] = mapped_column(Float, nullable=False)
    risk_discipline_score: Mapped[float] = mapped_column(Float, nullable=False)
    operator_communication_score: Mapped[float] = mapped_column(Float, nullable=False)
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False)
    hard_gate_penalties_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    component_details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_from_report_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DailyReportRecord(TimestampMixin, Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        Index("uq_daily_reports_report_date", "report_date", unique=True),
        Index("ix_daily_reports_period", "period_start_ms", "period_end_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_date: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_capital_score: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discord_channel_id: Mapped[str | None] = mapped_column(String(64))
    discord_message_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WeeklyReportRecord(TimestampMixin, Base):
    __tablename__ = "weekly_reports"
    __table_args__ = (
        Index("uq_weekly_reports_week_key", "week_key", unique=True),
        Index("ix_weekly_reports_period", "period_start_ms", "period_end_ms"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    week_key: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_capital_score: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discord_channel_id: Mapped[str | None] = mapped_column(String(64))
    discord_message_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Hip4CapabilityProbeRecord(TimestampMixin, Base):
    __tablename__ = "hip4_capability_probes"
    __table_args__ = (
        Index("ix_hip4_capability_probes_network_created", "network", "probed_at_ms"),
        Index("ix_hip4_capability_probes_schema_hash", "outcome_meta_schema_hash"),
    )

    probe_id: Mapped[str] = mapped_column(String(96), primary_key=True, default=_id)
    network: Mapped[str] = mapped_column(String(32), nullable=False)
    probed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome_meta_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outcome_meta_error: Mapped[str | None] = mapped_column(String(128))
    outcome_meta_schema_hash: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    degraded_reasons_json: Mapped[list[str]] = mapped_column(JSON, default=list)


class Hip4RawPayloadRecord(TimestampMixin, Base):
    __tablename__ = "hip4_raw_payloads"
    __table_args__ = (
        Index("ix_hip4_raw_payloads_source_network", "source", "network"),
        Index("ix_hip4_raw_payloads_observed", "observed_at_ms"),
        Index("ix_hip4_raw_payloads_schema_hash", "schema_hash"),
    )

    payload_id: Mapped[str] = mapped_column(String(96), primary_key=True, default=_id)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    network: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schema_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4OutcomeSpecRecord(TimestampMixin, Base):
    __tablename__ = "hip4_outcome_specs"
    __table_args__ = (
        Index("ix_hip4_outcome_specs_outcome", "outcome_id"),
        Index("ix_hip4_outcome_specs_status", "status"),
        Index("ix_hip4_outcome_specs_as_of", "as_of_ms"),
    )

    outcome_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    quote_token: Mapped[str | None] = mapped_column(String(64))
    side0_name: Mapped[str] = mapped_column(String(64), default="YES", nullable=False)
    side1_name: Mapped[str] = mapped_column(String(64), default="NO", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    settle_fraction: Mapped[str | None] = mapped_column(String(96))
    settlement_details: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4QuestionSpecRecord(TimestampMixin, Base):
    __tablename__ = "hip4_question_specs"
    __table_args__ = (
        Index("ix_hip4_question_specs_question", "question_id"),
        Index("ix_hip4_question_specs_status", "status"),
        Index("ix_hip4_question_specs_as_of", "as_of_ms"),
    )

    question_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fallback_outcome_id: Mapped[int | None] = mapped_column(Integer)
    named_outcome_ids_json: Mapped[list[int]] = mapped_column(JSON, default=list)
    settled_named_outcome_ids_json: Mapped[list[int]] = mapped_column(JSON, default=list)
    outcome_ids_json: Mapped[list[int]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4MarketSnapshotRecord(TimestampMixin, Base):
    __tablename__ = "hip4_market_snapshots"
    __table_args__ = (
        Index("ix_hip4_market_snapshots_question", "question_id"),
        Index("ix_hip4_market_snapshots_outcome", "outcome_id"),
        Index("ix_hip4_market_snapshots_as_of", "as_of_ms"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(96), primary_key=True, default=_id)
    question_id: Mapped[int | None] = mapped_column(Integer)
    outcome_id: Mapped[int | None] = mapped_column(Integer)
    coin: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[int] = mapped_column(Integer, nullable=False)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    best_bid: Mapped[str | None] = mapped_column(String(96))
    best_ask: Mapped[str | None] = mapped_column(String(96))
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Hip4EdgeCandidateRecord(TimestampMixin, Base):
    __tablename__ = "hip4_edge_candidates"
    __table_args__ = (
        Index("ix_hip4_edge_candidates_candidate", "candidate_id"),
        Index("ix_hip4_edge_candidates_question", "question_id"),
        Index("ix_hip4_edge_candidates_status", "status"),
        Index("ix_hip4_edge_candidates_as_of", "as_of_ms"),
    )

    candidate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    question_id: Mapped[int | None] = mapped_column(Integer)
    outcome_ids_json: Mapped[list[int]] = mapped_column(JSON, default=list)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[str] = mapped_column(String(96), nullable=False)
    gross_cost_or_proceeds: Mapped[str] = mapped_column(String(96), nullable=False)
    expected_net_edge_usd: Mapped[str] = mapped_column(String(96), nullable=False)
    expected_net_edge_bps: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Hip4PaperPortfolioRecord(TimestampMixin, Base):
    __tablename__ = "hip4_paper_portfolios"

    portfolio_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quote_token: Mapped[str] = mapped_column(String(64), nullable=False)
    cash: Mapped[str] = mapped_column(String(96), nullable=False)
    realized_pnl: Mapped[str] = mapped_column(String(96), nullable=False)
    unrealized_pnl: Mapped[str] = mapped_column(String(96), nullable=False)
    settlement_pnl: Mapped[str] = mapped_column(String(96), nullable=False)
    modeled_fees: Mapped[str] = mapped_column(String(96), nullable=False)
    daily_notional: Mapped[str] = mapped_column(String(96), nullable=False)
    balances_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4PaperPositionRecord(TimestampMixin, Base):
    __tablename__ = "hip4_paper_positions"
    __table_args__ = (Index("ix_hip4_paper_positions_token", "token"),)

    position_id: Mapped[str] = mapped_column(String(96), primary_key=True, default=_id)
    portfolio_id: Mapped[str] = mapped_column(String(96), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    balance: Mapped[str] = mapped_column(String(96), nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4PaperActionRecord(TimestampMixin, Base):
    __tablename__ = "hip4_paper_actions"
    __table_args__ = (
        Index("ix_hip4_paper_actions_candidate", "candidate_id"),
        Index("ix_hip4_paper_actions_action_type", "action_type"),
        Index("ix_hip4_paper_actions_created", "created_at_ms"),
    )

    action_id: Mapped[str] = mapped_column(String(96), primary_key=True, default=_id)
    candidate_id: Mapped[str | None] = mapped_column(String(96))
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[str] = mapped_column(String(96), nullable=False)
    price: Mapped[str | None] = mapped_column(String(96))
    action_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4PaperFillRecord(TimestampMixin, Base):
    __tablename__ = "hip4_paper_fills"
    __table_args__ = (
        Index("ix_hip4_paper_fills_candidate", "candidate_id"),
        Index("ix_hip4_paper_fills_created", "created_at_ms"),
    )

    fill_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    coin: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    size: Mapped[str] = mapped_column(String(96), nullable=False)
    price: Mapped[str] = mapped_column(String(96), nullable=False)
    notional: Mapped[str] = mapped_column(String(96), nullable=False)
    fee: Mapped[str] = mapped_column(String(96), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4ReconciliationRunRecord(TimestampMixin, Base):
    __tablename__ = "hip4_reconciliation_runs"
    __table_args__ = (
        Index("ix_hip4_reconciliation_runs_status", "status"),
        Index("ix_hip4_reconciliation_runs_created", "created_at_ms"),
    )

    run_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    discrepancies_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Hip4SettlementRecord(TimestampMixin, Base):
    __tablename__ = "hip4_settlements"
    __table_args__ = (
        Index("ix_hip4_settlements_outcome", "outcome_id"),
        Index("ix_hip4_settlements_as_of", "as_of_ms"),
    )

    settlement_id: Mapped[str] = mapped_column(String(96), primary_key=True, default=_id)
    outcome_id: Mapped[int] = mapped_column(Integer, nullable=False)
    settle_fraction: Mapped[str | None] = mapped_column(String(96))
    details: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    as_of_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LiquidationEventRecord(TimestampMixin, Base):
    """Append-only normalized liquidation row (liquidations subsystem).

    Numeric fields are Float to match the codebase convention; exact decimals
    survive in the contract layer and in ``raw_json`` for audit/replay.
    """

    __tablename__ = "liquidation_events"
    __table_args__ = (
        Index("ix_liquidation_events_ts", "timestamp_ms"),
        Index("ix_liquidation_events_venue_symbol_ts", "venue", "symbol", "timestamp_ms"),
        Index("ix_liquidation_events_integrity_ts", "source_integrity", "timestamp_ms"),
    )

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_integrity: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_market_id: Mapped[str | None] = mapped_column(String(64))
    liquidated_side: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    raw_side: Mapped[str | None] = mapped_column(String(32))
    price: Mapped[float | None] = mapped_column(Float)
    avg_price: Mapped[float | None] = mapped_column(Float)
    mark_price: Mapped[float | None] = mapped_column(Float)
    bankruptcy_price: Mapped[float | None] = mapped_column(Float)
    size_base: Mapped[float | None] = mapped_column(Float)
    notional_usd: Mapped[float | None] = mapped_column(Float)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_height: Mapped[int | None] = mapped_column(BigInteger)
    tx_hash: Mapped[str | None] = mapped_column(String(128))
    log_index: Mapped[int | None] = mapped_column(Integer)
    trade_id: Mapped[str | None] = mapped_column(String(128))
    liquidation_id: Mapped[str | None] = mapped_column(String(128))
    liquidated_user: Mapped[str | None] = mapped_column(String(128))
    liquidator: Mapped[str | None] = mapped_column(String(128))
    method: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LiquidationAdapterStateRecord(TimestampMixin, Base):
    """Per-adapter checkpoint / health row (liquidations subsystem)."""

    __tablename__ = "liquidation_adapter_state"

    adapter_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_cursor: Mapped[str | None] = mapped_column(String(255))
    last_event_ms: Mapped[int | None] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="init")
    error: Mapped[str | None] = mapped_column(Text)
