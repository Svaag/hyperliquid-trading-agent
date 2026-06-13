from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hyperliquid_trading_agent.app.db.models import (
    AuditEvent,
    CacheItem,
    ConversationMessage,
    ConversationThread,
    NewsItem,
    PaperTradeIdea,
    PaperTradeSnapshot,
    ToolCall,
)
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.security import redact_secrets

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
