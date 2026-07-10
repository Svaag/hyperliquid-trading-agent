from __future__ import annotations

from typing import Any

import anyio

from hyperliquid_trading_agent.app.agent.runner import AgentContext
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.discord_bot import DiscordMentionPathDiagnostics, _ensure_thread
from hyperliquid_trading_agent.app.workers.discord_bot_worker import CommandBackedAgentRunner, DiscordBotWorker


class _CompletedCommandRepository:
    def __init__(self) -> None:
        self.enqueued: dict[str, Any] | None = None

    async def enqueue_worker_command(self, **kwargs) -> dict[str, Any]:
        self.enqueued = kwargs
        return {"command_id": "cmd_discord_smoke", "status": "pending"}

    async def get_worker_command(self, command_id: str) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "status": "completed",
            "result": {
                "content": "All services are healthy. No trade was placed.",
                "model_used": "deterministic-test",
            },
        }


class _ThreadPermissionFailureMessage:
    def __init__(self) -> None:
        self.channel = object()

    async def create_thread(self, *, name: str):
        raise PermissionError(f"cannot create {name}")


def test_command_backed_runner_records_full_safe_mention_command_path() -> None:
    async def run():
        repository = _CompletedCommandRepository()
        diagnostics = DiscordMentionPathDiagnostics()
        runner = CommandBackedAgentRunner(
            repository=repository,  # type: ignore[arg-type]
            settings=Settings(environment="test", _env_file=None),
            diagnostics=diagnostics,
        )
        response = await runner.answer(
            "Return service health only; do not trade.",
            context=AgentContext(source="discord", discord_channel_id="123", discord_thread_id="456"),
        )
        return repository, diagnostics.status(), response

    repository, status, response = anyio.run(run)

    assert repository.enqueued is not None
    assert repository.enqueued["target_role"] == "agent"
    assert repository.enqueued["command_type"] == "ask"
    assert status["last_command_id_enqueued"] == "cmd_discord_smoke"
    assert status["last_command_status"] == "completed"
    assert status["last_command_error"] is None
    assert response.content.endswith("No trade was placed.")


def test_thread_permission_failure_is_visible_and_falls_back_safely() -> None:
    async def run():
        diagnostics = DiscordMentionPathDiagnostics()
        message = _ThreadPermissionFailureMessage()
        channel = await _ensure_thread(message, "safe health check", diagnostics=diagnostics)
        return message, channel, diagnostics.status()

    message, channel, status = anyio.run(run)

    assert channel is message.channel
    assert status["thread_fallback_count"] == 1
    assert "PermissionError" in status["last_thread_error"]


def test_discord_worker_heartbeat_distinguishes_online_from_working_mention_path() -> None:
    worker = DiscordBotWorker(Settings(environment="test", _env_file=None))
    worker.diagnostics.message_seen()
    worker.diagnostics.auth_rejected()
    worker.diagnostics.command_enqueued("cmd_1")
    worker.diagnostics.command_finished("failed", error="agent worker unavailable")
    worker.diagnostics.reply_failed("Missing Permissions")

    discord = worker.heartbeat_metadata()["discord_bot"]
    mention_path = discord["mention_path"]

    assert discord["ready"] is False
    assert mention_path["last_message_seen_at_ms"] is not None
    assert mention_path["auth_rejection_count"] == 1
    assert mention_path["last_command_id_enqueued"] == "cmd_1"
    assert mention_path["last_command_status"] == "failed"
    assert mention_path["last_reply_error"] == "Missing Permissions"
