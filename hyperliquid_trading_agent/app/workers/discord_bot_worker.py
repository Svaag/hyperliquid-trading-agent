from __future__ import annotations

from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.workers.base import BaseWorker


class DiscordBotWorker(BaseWorker):
    role = ServiceRole.DISCORD_BOT
    lock_name = "service:discord_bot"

    def __init__(self, settings: Settings):
        super().__init__(settings)

    async def run(self) -> None:
        await self.wait_until_stopped()

    def heartbeat_metadata(self) -> dict[str, object]:
        return {"discord_bot": {"enabled": self.settings.discord_bot_enabled, "configured": bool(self.settings.discord_bot_token), "status": "deferred_to_agent_commands"}}
