from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from hyperliquid_trading_agent.app.logging import get_logger

try:  # pragma: no cover - import availability depends on runtime extras
    import discord
except Exception:  # pragma: no cover
    discord = None  # type: ignore[assignment]

log = get_logger(__name__)


ComponentHandler = Callable[[str, str | None, str | None], Awaitable[str | None]]


class DiscordMessageSink(Protocol):
    async def send(
        self,
        channel_id: str,
        content: str,
        embeds: list[dict[str, Any]] | None = None,
        components: list[dict[str, Any]] | None = None,
    ) -> str | None: ...


class SendOnlyDiscordClient:
    """Minimal Discord publisher for restricted runtimes.

    This client intentionally registers no message handlers and requests only default
    intents. It can publish Newswire embeds from ``world_model_live`` without enabling
    the interactive trading bot, autonomy commands, or tracking commands.
    """

    def __init__(self, *, token: str):
        self.token = token
        self.client = None
        self.component_handler: ComponentHandler | None = None
        self._started = False
        self._ready_event = asyncio.Event()
        self._last_error: str | None = None
        if discord is not None:
            intents = discord.Intents.default()
            self.client = discord.Client(intents=intents)
            self._register_handlers()

    @property
    def available(self) -> bool:
        return self.client is not None and bool(self.token)

    @property
    def ready(self) -> bool:
        return bool(self.client is not None and (self.client.is_ready() or self._ready_event.is_set()))

    async def start(self) -> None:
        if not self.token:
            return
        if self.client is None:
            raise RuntimeError("discord.py is not installed")
        self._started = True
        self._ready_event.clear()
        try:
            await self.client.start(self.token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - Discord runtime behavior
            self._last_error = type(exc).__name__
            log.warning("discord_send_only_client_failed", error=type(exc).__name__)
            raise
        finally:
            self._started = False

    async def stop(self) -> None:
        if self.client is not None and not self.client.is_closed():
            await self.client.close()
        self._ready_event.clear()
        self._started = False

    async def wait_until_ready(self, timeout: float = 30.0) -> bool:
        if self.client is None:
            return False
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            self._last_error = "ready_timeout"
            return False

    async def send(self, channel_id: str, content: str, embeds: list[dict[str, Any]] | None = None, components: list[dict[str, Any]] | None = None) -> str | None:
        if self.client is None or not channel_id:
            return None
        channel = self.client.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if channel is None and callable(getattr(self.client, "fetch_channel", None)):
            try:
                channel = await self.client.fetch_channel(int(channel_id))
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self._last_error = type(exc).__name__
                log.warning("discord_send_only_channel_fetch_failed", channel_id=channel_id, error=type(exc).__name__)
                return None
        if channel is None or not callable(getattr(channel, "send", None)):
            self._last_error = "channel_unresolved"
            log.warning("discord_send_only_channel_unresolved", channel_id=channel_id)
            return None
        discord_embeds = _build_embeds(embeds)
        view = _build_view(components, self.component_handler)
        sent = await channel.send(content=content, embeds=discord_embeds, view=view)
        message_id = _maybe_str(getattr(sent, "id", None))
        log.info("discord_send_only_message_sent", channel_id=channel_id, message_id=message_id, preview=content[:200])
        return message_id

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "started": self._started,
            "ready": self.ready,
            "last_error": self._last_error,
        }

    def _register_handlers(self) -> None:
        assert self.client is not None

        @self.client.event
        async def on_ready():
            self._ready_event.set()
            log.info("discord_send_only_ready", user=str(self.client.user), guild_count=len(self.client.guilds))


class SendOnlyDiscordSink:
    def __init__(self, client: SendOnlyDiscordClient):
        self.client = client

    async def send(self, channel_id: str, content: str, embeds: list[dict[str, Any]] | None = None, components: list[dict[str, Any]] | None = None) -> str | None:
        return await self.client.send(channel_id, content, embeds=embeds, components=components)


def _build_embeds(items: list[dict[str, Any]] | None):
    if not items or discord is None:
        return None
    return [discord.Embed.from_dict(item) for item in items]


def _build_view(items: list[dict[str, Any]] | None, handler: ComponentHandler | None):
    if not items or discord is None or handler is None:
        return None
    view = discord.ui.View(timeout=None)
    for item in items[:25]:
        custom_id = str(item.get("custom_id") or "")[:100]
        label = str(item.get("label") or "")[:80]
        if not custom_id or not label:
            continue
        button = discord.ui.Button(label=label, custom_id=custom_id, style=_button_style(str(item.get("style") or "secondary")))

        async def _callback(interaction, cid=custom_id):
            user_id = _maybe_str(getattr(getattr(interaction, "user", None), "id", None))
            message_id = _maybe_str(getattr(getattr(interaction, "message", None), "id", None))
            try:
                response = await handler(cid, user_id, message_id)
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                response = f"Feedback failed: {type(exc).__name__}"
                log.warning("discord_component_handler_failed", custom_id=cid, error=type(exc).__name__)
            responder = getattr(interaction, "response", None)
            if responder is not None and callable(getattr(responder, "send_message", None)):
                await responder.send_message(response or "Feedback recorded.", ephemeral=True)

        button.callback = _callback
        view.add_item(button)
    return view


def _button_style(style: str):
    if discord is None:
        return None
    mapping = {
        "primary": discord.ButtonStyle.primary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "secondary": discord.ButtonStyle.secondary,
    }
    return mapping.get(style, discord.ButtonStyle.secondary)


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)
