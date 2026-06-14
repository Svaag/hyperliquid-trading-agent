from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import DISCORD_MESSAGES
from hyperliquid_trading_agent.app.tracking.commands import parse_tracking_command
from hyperliquid_trading_agent.app.tracking.service import PositionTrackingService

discord: Any
try:  # pragma: no cover - import availability depends on runtime extras
    import discord
except Exception:  # pragma: no cover
    discord = None

log = get_logger(__name__)
MENTION_RE = re.compile(r"<@!?\d+>")


@dataclass(frozen=True)
class DiscordContext:
    guild_id: int | None
    channel_id: int | None
    author_id: int | None


class DiscordTradingBot:
    """Mention-driven Discord support desk bot."""

    def __init__(self, settings: Settings, runner: TradingAgentRunner | None = None, tracking_service: PositionTrackingService | None = None):
        self.settings = settings
        self.runner = runner
        self.tracking_service = tracking_service
        self.client = None
        if discord is not None:
            intents = discord.Intents.default()
            intents.message_content = True
            self.client = discord.Client(intents=intents)
            self._register_handlers()

    async def start(self) -> None:
        if not self.settings.discord_bot_token:
            return
        if self.client is None:
            raise RuntimeError("discord.py is not installed")
        await self.client.start(self.settings.discord_bot_token)

    async def stop(self) -> None:
        if self.client is not None and not self.client.is_closed():
            await self.client.close()

    def is_authorized(self, context: DiscordContext, role_ids: set[int] | None = None) -> bool:
        if self.settings.allowed_guild_ids and context.guild_id not in self.settings.allowed_guild_ids:
            return False
        allowed_channels = self.settings.allowed_channel_ids
        if allowed_channels and context.channel_id not in allowed_channels:
            return False
        if self.settings.allowed_role_ids:
            return bool((role_ids or set()) & self.settings.allowed_role_ids)
        return True

    def _register_handlers(self) -> None:
        assert self.client is not None

        @self.client.event
        async def on_ready():
            log.info("discord_bot_ready", user=str(self.client.user), guild_count=len(self.client.guilds))

        @self.client.event
        async def on_message(message):
            if message.author.bot or self.client.user is None:
                return
            mentioned = self.client.user in message.mentions
            thread_continuation = _is_bot_thread(getattr(message, "channel", None), self.client.user)
            if not mentioned and not thread_continuation:
                return
            role_ids = {int(getattr(role, "id", 0)) for role in getattr(message.author, "roles", [])}
            channel_id = _authorized_channel_id(message)
            context = DiscordContext(
                guild_id=getattr(getattr(message, "guild", None), "id", None),
                channel_id=channel_id,
                author_id=getattr(getattr(message, "author", None), "id", None),
            )
            if not self.is_authorized(context, role_ids=role_ids):
                DISCORD_MESSAGES.labels(result="unauthorized").inc()
                await message.reply("Not authorized for this bot/channel.", mention_author=False)
                return
            if self.runner is None:
                DISCORD_MESSAGES.labels(result="no_runner").inc()
                await message.reply("Trading agent runtime is not ready yet.", mention_author=False)
                return
            prompt = _message_prompt_without_mentions(message.content)
            if not prompt:
                await message.reply("Mention me with a trading, Hyperliquid, market, macro, or news question.", mention_author=False)
                return
            tracking_command = parse_tracking_command(prompt)
            try:
                async with message.channel.typing():
                    thread = await _ensure_thread(message, prompt)
                    if tracking_command is not None and self.tracking_service is not None:
                        content = await self.tracking_service.handle_thread_command(tracking_command, _maybe_str(getattr(thread, "id", None)) or "")
                        for chunk in _chunk(content, self.settings.discord_max_response_chars):
                            await thread.send(chunk)
                        DISCORD_MESSAGES.labels(result="tracking_command").inc()
                        return
                    agent_context = AgentContext(
                        source="discord",
                        discord_guild_id=_maybe_str(getattr(getattr(message, "guild", None), "id", None)),
                        discord_channel_id=_maybe_str(channel_id),
                        discord_thread_id=_maybe_str(getattr(thread, "id", None)),
                        discord_user_id=_maybe_str(getattr(message.author, "id", None)),
                    )
                    response = await self.runner.answer(prompt, context=agent_context)
                    for chunk in _chunk(response.content, self.settings.discord_max_response_chars):
                        await thread.send(chunk)
                DISCORD_MESSAGES.labels(result="ok").inc()
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                DISCORD_MESSAGES.labels(result="error").inc()
                log.exception("discord_message_failed", error=type(exc).__name__)
                await message.reply("I hit an internal error while answering. No trade was placed.", mention_author=False)


def _message_prompt_without_mentions(content: str) -> str:
    return " ".join(MENTION_RE.sub(" ", content).split())


def _authorized_channel_id(message) -> int | None:
    channel = getattr(message, "channel", None)
    parent = getattr(channel, "parent", None)
    return getattr(parent, "id", None) or getattr(channel, "id", None)


async def _ensure_thread(message, prompt: str):
    if _is_thread_channel(getattr(message, "channel", None)):
        return message.channel
    if callable(getattr(message, "create_thread", None)):
        name = _thread_name(prompt)
        try:
            return await message.create_thread(name=name)
        except TypeError:
            try:
                return await message.create_thread(name)
            except Exception as exc:  # pragma: no cover - Discord permission/runtime behavior
                log.warning("discord_thread_create_failed", error=type(exc).__name__)
        except Exception as exc:  # pragma: no cover - Discord permission/runtime behavior
            log.warning("discord_thread_create_failed", error=type(exc).__name__)
    return message.channel


def _is_thread_channel(channel) -> bool:
    if channel is None:
        return False
    if discord is not None and hasattr(discord, "Thread") and isinstance(channel, discord.Thread):
        return True
    channel_type = getattr(getattr(channel, "type", None), "name", "")
    if channel_type in {"public_thread", "private_thread", "news_thread"}:
        return True
    return getattr(channel, "owner_id", None) is not None and getattr(channel, "parent", None) is not None


def _is_bot_thread(channel, bot_user) -> bool:
    if not _is_thread_channel(channel):
        return False
    bot_id = getattr(bot_user, "id", None)
    if bot_id is None:
        return False
    return getattr(channel, "owner_id", None) == bot_id


def _thread_name(prompt: str) -> str:
    cleaned = re.sub(r"\s+", " ", prompt).strip()
    return (cleaned[:80] or "Hyperliquid support")


def _chunk(content: str, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]
    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks


def _maybe_str(value) -> str | None:
    return None if value is None else str(value)
