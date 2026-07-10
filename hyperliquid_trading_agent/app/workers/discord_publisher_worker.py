from __future__ import annotations

import asyncio
from typing import Any

from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.discord_publish import SendOnlyDiscordClient, SendOnlyDiscordSink
from hyperliquid_trading_agent.app.newswire.bus import InProcessNewswireBus
from hyperliquid_trading_agent.app.newswire.consumers.discord_news import DiscordNewsPublisher
from hyperliquid_trading_agent.app.newswire.enrich import Enricher
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.workers.stored_newswire_story_pump import StoredNewswireStoryPump


class DiscordPublisherWorker(BaseWorker):
    role = ServiceRole.DISCORD_PUBLISHER
    lock_name = "service:discord_publisher"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.client: SendOnlyDiscordClient | None = None
        self.publisher: DiscordNewsPublisher | None = None
        self.pump: StoredNewswireStoryPump | None = None
        self.bus = InProcessNewswireBus()

    async def run(self) -> None:
        self.client = SendOnlyDiscordClient(token=self.settings.discord_bot_token)
        sink = SendOnlyDiscordSink(self.client)
        self.publisher = DiscordNewsPublisher(
            settings=self.settings,
            bus=self.bus,
            alert_sink=sink,
            enricher=Enricher(settings=self.settings, model_gateway=ModelGateway(settings=self.settings)),
            repository=self.repository,
        )
        self.client.component_handler = self.publisher.handle_feedback_component
        self.pump = StoredNewswireStoryPump(
            consumer_name="discord_publisher:newswire",
            repository=self.repository,
            callbacks=[self.bus.publish],
            poll_seconds=self.settings.consumer_poll_seconds,
            batch_size=self.settings.consumer_batch_size,
        )
        client_task = asyncio.create_task(self.client.run_forever(), name="discord-news-send-only")
        await self.client.wait_until_ready(timeout=30)
        await self.publisher.start()
        tasks = [
            client_task,
            asyncio.create_task(self.pump.run_forever(), name="discord-publisher-newswire-pump"),
            asyncio.create_task(self.command_loop({"discord_test": self._handle_discord_test}), name="discord-publisher-command-loop"),
        ]
        try:
            await self.wait_until_stopped()
        finally:
            if self.pump is not None:
                await self.pump.stop()
            if self.publisher is not None:
                await self.publisher.stop()
            if self.client is not None:
                await self.client.stop()
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _handle_discord_test(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.publisher is None:
            raise RuntimeError("discord_publisher_unavailable")
        payload = command.get("payload") or {}
        return await self.publisher.send_test_message(channel_id=payload.get("channel_id"), dry_run=bool(payload.get("dry_run")))

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {
            "discord": self.client.status() if self.client is not None and hasattr(self.client, "status") else {"configured": bool(self.settings.discord_bot_token)},
            "publisher": self.publisher.status() if self.publisher is not None else {},
            "pump": self.pump.status() if self.pump is not None else {},
        }
