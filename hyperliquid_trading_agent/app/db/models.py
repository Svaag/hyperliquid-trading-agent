from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
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


class TradeProposalRecord(TimestampMixin, Base):
    __tablename__ = "trade_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("decision_runs.id"))
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    coin: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str | None] = mapped_column(String(16))
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content: Mapped[str] = mapped_column(Text, default="")
