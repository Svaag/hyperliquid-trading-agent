from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.agent.runner import AgentContext, AgentResponse
from hyperliquid_trading_agent.app.charting import ChartingService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.discord_bot import DiscordTradingBot
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.factory import build_tradfi_client
from hyperliquid_trading_agent.app.workers.base import BaseWorker

log = get_logger(__name__)


class CommandBackedAgentRunner:
    """Discord-facing runner proxy that keeps LLM execution in the agent worker."""

    def __init__(self, *, repository: Repository, settings: Settings, requested_by: str = "discord_bot") -> None:
        self.repository = repository
        self.settings = settings
        self.requested_by = requested_by

    async def answer(self, prompt: str, context: AgentContext | None = None) -> AgentResponse:
        context = context or AgentContext(source="discord")
        request_id = f"discord_{uuid4().hex}"
        command = await self.repository.enqueue_worker_command(
            target_role=ServiceRole.AGENT.value,
            command_type="ask",
            payload={"prompt": prompt, "context": _agent_context_payload(context), "request_id": request_id},
            requested_by=self.requested_by,
            idempotency_key=f"{self.requested_by}:ask:{request_id}",
            metadata={"source": "discord", "discord_thread_id": context.discord_thread_id, "discord_channel_id": context.discord_channel_id},
        )
        command_id = str(command.get("command_id") or "")
        timeout_seconds = max(10.0, float(getattr(self.settings, "discord_command_timeout_seconds", 180.0)))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            current = await self.repository.get_worker_command(command_id)
            status = str((current or command).get("status") or "")
            if status == "completed":
                result = (current or {}).get("result") or {}
                if not isinstance(result, dict):
                    result = {}
                return AgentResponse(
                    content=str(result.get("content") or "I completed the request, but no response content was returned. No trade was placed."),
                    refused=bool(result.get("refused")),
                    model_used=str(result.get("model_used") or "") or None,
                    fallback_used=bool(result.get("fallback_used")),
                    decision_run_id=str(result.get("decision_run_id") or "") or None,
                    proposal_id=str(result.get("proposal_id") or "") or None,
                    high_stakes=bool(result.get("high_stakes")),
                )
            if status in {"failed", "cancelled"}:
                last_error = str((current or {}).get("last_error") or "unknown error")
                return AgentResponse(content=f"Trading agent command `{command_id}` {status}: {last_error}. No trade was placed.", fallback_used=True)
            await asyncio.sleep(1.0)
        return AgentResponse(
            content=(
                f"I accepted the request as worker command `{command_id}`, but it did not finish within "
                f"{int(timeout_seconds)}s. Check `/commands/{command_id}` for status. No trade was placed."
            ),
            fallback_used=True,
        )


class DiscordBotWorker(BaseWorker):
    role = ServiceRole.DISCORD_BOT
    lock_name = "service:discord_bot"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.runner: CommandBackedAgentRunner | None = None
        self.bot: DiscordTradingBot | None = None
        self.hyperliquid: HyperliquidClient | None = None
        self.tradfi: TradFiClient | None = None
        self._bot_task: asyncio.Task | None = None
        self._last_error: str | None = None

    async def run(self) -> None:
        if not self.settings.discord_bot_enabled or not self.settings.discord_bot_token:
            await self.wait_until_stopped()
            return
        self.runner = CommandBackedAgentRunner(repository=self.repository, settings=self.settings)
        charting_service = None
        if self.settings.discord_chart_command_enabled:
            self.hyperliquid = HyperliquidClient(settings=self.settings)
            self.tradfi = await build_tradfi_client(self.settings)
            charting_service = ChartingService(settings=self.settings, hyperliquid=self.hyperliquid, tradfi=self.tradfi)
        self.bot = DiscordTradingBot(settings=self.settings, runner=self.runner, charting_service=charting_service)
        self._bot_task = asyncio.create_task(self.bot.start(), name="discord-command-bot")
        stop_task = asyncio.create_task(self.wait_until_stopped(), name="discord-command-bot-stop")
        try:
            done, _ = await asyncio.wait({self._bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if self._bot_task in done and not self._stop.is_set():
                exc = self._bot_task.exception()
                self._last_error = type(exc).__name__ if exc is not None else "completed_unexpectedly"
                if exc is not None:
                    raise exc
                raise RuntimeError("discord_bot_completed_unexpectedly")
        finally:
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
            await self._stop_bot()
            if self.tradfi is not None:
                await self.tradfi.close()
                self.tradfi = None
            if self.hyperliquid is not None:
                await self.hyperliquid.close()
                self.hyperliquid = None

    async def _stop_bot(self) -> None:
        if self.bot is not None:
            await self.bot.stop()
        if self._bot_task is not None:
            if not self._bot_task.done():
                self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self._last_error = type(exc).__name__
                log.warning("discord_bot_task_failed", error=type(exc).__name__)
            self._bot_task = None

    def heartbeat_metadata(self) -> dict[str, object]:
        client = getattr(self.bot, "client", None) if self.bot is not None else None
        return {
            "discord_bot": {
                "enabled": self.settings.discord_bot_enabled,
                "configured": bool(self.settings.discord_bot_token),
                "available": client is not None,
                "running": bool(self._bot_task is not None and not self._bot_task.done()),
                "ready": bool(client is not None and callable(getattr(client, "is_ready", None)) and client.is_ready()),
                "message_content_intent": bool(getattr(getattr(client, "intents", None), "message_content", False)) if client is not None else False,
                "runner": "agent_worker_command_proxy" if self.runner is not None else None,
                "charting": {
                    "enabled": self.settings.discord_chart_command_enabled,
                    "configured": self.bot is not None and getattr(self.bot, "charting_service", None) is not None,
                    "tradfi": self.tradfi.status() if self.tradfi is not None else {},
                },
                "last_error": self._last_error,
            }
        }



def _agent_context_payload(context: AgentContext) -> dict[str, Any]:
    return {key: value for key, value in asdict(context).items() if value is not None}
