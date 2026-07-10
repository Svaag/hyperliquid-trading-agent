from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from hyperliquid_trading_agent.app.agent.runner import AgentContext
from hyperliquid_trading_agent.app.autonomy.discord import parse_autonomy_command
from hyperliquid_trading_agent.app.charting import (
    ChartCommand,
    ChartingService,
    parse_chart_command,
    parse_chart_prompt,
)
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import DISCORD_MESSAGES
from hyperliquid_trading_agent.app.paper.discord import (
    PaperDiscordCommand,
    format_paper_result,
    parse_paper_discord_command,
)
from hyperliquid_trading_agent.app.prediction_markets.discord import (
    PredictionMarketDiscordCommand,
    format_prediction_market_leaderboard,
    format_prediction_market_portfolio,
    format_prediction_market_positions,
    format_prediction_market_result,
    format_prediction_market_search,
    parse_prediction_market_discord_command,
    parse_prediction_market_reaction,
)
from hyperliquid_trading_agent.app.prediction_markets.paper import PredictionMarketPaperService
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


class DiscordMentionPathDiagnostics:
    """In-process proof points for the interactive Discord mention path."""

    def __init__(self) -> None:
        self.gateway_ready_at_ms: int | None = None
        self.last_message_seen_at_ms: int | None = None
        self.last_authorized_mention_at_ms: int | None = None
        self.last_authorized_path_at_ms: int | None = None
        self.last_command_id_enqueued: str | None = None
        self.last_command_status: str | None = None
        self.last_command_error: str | None = None
        self.last_reply_success_at_ms: int | None = None
        self.last_reply_error_at_ms: int | None = None
        self.last_reply_error: str | None = None
        self.auth_rejection_count = 0
        self.thread_fallback_count = 0
        self.last_thread_error: str | None = None

    def gateway_ready(self) -> None:
        self.gateway_ready_at_ms = _diagnostic_now_ms()

    def message_seen(self) -> None:
        self.last_message_seen_at_ms = _diagnostic_now_ms()

    def authorized_path(self, *, mentioned: bool) -> None:
        now = _diagnostic_now_ms()
        self.last_authorized_path_at_ms = now
        if mentioned:
            self.last_authorized_mention_at_ms = now

    def auth_rejected(self) -> None:
        self.auth_rejection_count += 1

    def command_enqueued(self, command_id: str) -> None:
        self.last_command_id_enqueued = str(command_id)
        self.last_command_status = "enqueued"
        self.last_command_error = None

    def command_finished(self, status: str, *, error: str | None = None) -> None:
        self.last_command_status = str(status)
        self.last_command_error = str(error)[:500] if error else None

    def reply_succeeded(self) -> None:
        self.last_reply_success_at_ms = _diagnostic_now_ms()
        self.last_reply_error = None

    def reply_failed(self, error: str) -> None:
        self.last_reply_error_at_ms = _diagnostic_now_ms()
        self.last_reply_error = str(error)[:500]

    def thread_fallback(self, error: str) -> None:
        self.thread_fallback_count += 1
        self.last_thread_error = str(error)[:500]

    def status(self) -> dict[str, Any]:
        return {
            "gateway_ready_at_ms": self.gateway_ready_at_ms,
            "last_message_seen_at_ms": self.last_message_seen_at_ms,
            "last_authorized_mention_at_ms": self.last_authorized_mention_at_ms,
            "last_authorized_path_at_ms": self.last_authorized_path_at_ms,
            "last_command_id_enqueued": self.last_command_id_enqueued,
            "last_command_status": self.last_command_status,
            "last_command_error": self.last_command_error,
            "last_reply_success_at_ms": self.last_reply_success_at_ms,
            "last_reply_error_at_ms": self.last_reply_error_at_ms,
            "last_reply_error": self.last_reply_error,
            "auth_rejection_count": self.auth_rejection_count,
            "thread_fallback_count": self.thread_fallback_count,
            "last_thread_error": self.last_thread_error,
        }


class DiscordTradingBot:
    """Mention-driven Discord support desk bot."""

    def __init__(
        self,
        settings: Settings,
        runner: Any | None = None,
        tracking_service: PositionTrackingService | None = None,
        autonomy_service: Any | None = None,
        charting_service: ChartingService | None = None,
        hyperliquid: Any | None = None,
        diagnostics: DiscordMentionPathDiagnostics | None = None,
    ):
        self.settings = settings
        self.runner = runner
        self.tracking_service = tracking_service
        self.autonomy_service = autonomy_service
        self.charting_service = charting_service
        self.hyperliquid = hyperliquid
        self.diagnostics = diagnostics or DiscordMentionPathDiagnostics()
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

    async def send_channel_message(self, channel_id: str, content: str, embeds: list[dict[str, Any]] | None = None) -> str | None:
        if self.client is None or not channel_id:
            return None
        channel = self.client.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if channel is None and callable(getattr(self.client, "fetch_channel", None)):
            try:
                channel = await self.client.fetch_channel(int(channel_id))
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                log.warning("discord_autonomy_channel_fetch_failed", channel_id=channel_id, error=type(exc).__name__)
                return None
        if channel is None or not callable(getattr(channel, "send", None)):
            log.warning("discord_autonomy_channel_unresolved", channel_id=channel_id)
            return None
        sent = await channel.send(content=content, embeds=_build_embeds(embeds))
        log.info("discord_autonomy_message_sent", channel_id=channel_id, message_id=_maybe_str(getattr(sent, "id", None)), preview=content[:200])
        return _maybe_str(getattr(sent, "id", None))

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
            self.diagnostics.gateway_ready()
            log.info("discord_bot_ready", user=str(self.client.user), guild_count=len(self.client.guilds))

        @self.client.event
        async def on_message(message):
            if message.author.bot or self.client.user is None:
                return
            self.diagnostics.message_seen()
            raw_content = str(getattr(message, "content", "") or "")
            chart_command = parse_chart_command(raw_content)
            channel_id = _authorized_channel_id(message)
            role_ids = {int(getattr(role, "id", 0)) for role in getattr(message.author, "roles", [])}
            context = DiscordContext(
                guild_id=getattr(getattr(message, "guild", None), "id", None),
                channel_id=channel_id,
                author_id=getattr(getattr(message, "author", None), "id", None),
            )
            if chart_command is not None:
                handled = await self._handle_chart_command(message, chart_command, context=context, role_ids=role_ids)
                if handled:
                    return
            mentioned = self.client.user in message.mentions
            thread_continuation = _is_bot_thread(getattr(message, "channel", None), self.client.user)
            prompt = _message_prompt_without_mentions(message.content)
            referenced_message = await _resolve_referenced_message(message)
            autonomy_command = parse_autonomy_command(prompt, referenced_message=referenced_message) if (prompt or referenced_message is not None) else None
            prediction_market_command = parse_prediction_market_discord_command(prompt, referenced_message=referenced_message) if prompt else None
            paper_command = parse_paper_discord_command(prompt, referenced_message=referenced_message) if prompt else None
            autonomy_alert_channel = bool(self.settings.autonomy_alert_channel_id and str(channel_id) == str(self.settings.autonomy_alert_channel_id))
            prediction_market_context_reply = (
                prediction_market_command is not None
                and prediction_market_command.action in {"confirm", "cancel"}
                and _is_message_from_bot(referenced_message, self.client.user)
            )
            if not mentioned and not thread_continuation and not prediction_market_context_reply and not (autonomy_command is not None and autonomy_alert_channel):
                return
            if not self.is_authorized(context, role_ids=role_ids) and not (autonomy_command is not None and autonomy_alert_channel):
                self.diagnostics.auth_rejected()
                DISCORD_MESSAGES.labels(result="unauthorized").inc()
                await message.reply("Not authorized for this bot/channel.", mention_author=False)
                return
            self.diagnostics.authorized_path(mentioned=mentioned)
            if not prompt:
                await message.reply("Mention me with a trading, Hyperliquid, market, macro, or news question.", mention_author=False)
                return
            natural_chart_command = parse_chart_command(prompt) or parse_chart_prompt(prompt)
            if natural_chart_command is not None:
                handled = await self._handle_chart_command(message, natural_chart_command, context=context, role_ids=role_ids)
                if handled:
                    return
            tracking_command = parse_tracking_command(prompt)
            try:
                async with message.channel.typing():
                    if prediction_market_command is not None:
                        content = await self._handle_prediction_market_command(
                            prediction_market_command,
                            context=context,
                            user_id=_maybe_str(getattr(message.author, "id", None)),
                            role_ids=role_ids,
                        )
                        for chunk in _chunk(content, self.settings.discord_max_response_chars):
                            await message.reply(chunk, mention_author=False)
                        DISCORD_MESSAGES.labels(result="prediction_market_command").inc()
                        return
                    if paper_command is not None:
                        content = await self._handle_paper_command(
                            paper_command,
                            user_id=_maybe_str(getattr(message.author, "id", None)),
                            role_ids=role_ids,
                        )
                        for chunk in _chunk(content, self.settings.discord_max_response_chars):
                            await message.reply(chunk, mention_author=False)
                        DISCORD_MESSAGES.labels(result="paper_command").inc()
                        return
                    if autonomy_command is not None and self.autonomy_service is not None and autonomy_alert_channel:
                        content = await self.autonomy_service.handle_discord_command(
                            autonomy_command,
                            user_id=_maybe_str(getattr(message.author, "id", None)),
                            role_ids=role_ids,
                        )
                        for chunk in _chunk(content, self.settings.discord_max_response_chars):
                            await message.reply(chunk, mention_author=False)
                        DISCORD_MESSAGES.labels(result="autonomy_command").inc()
                        return
                    if self.runner is None:
                        DISCORD_MESSAGES.labels(result="no_runner").inc()
                        await message.reply("Trading agent runtime is not ready yet.", mention_author=False)
                        return
                    thread = await _ensure_thread(message, prompt, diagnostics=self.diagnostics)
                    thread_id = _maybe_str(getattr(thread, "id", None))
                    if tracking_command is not None and self.tracking_service is not None:
                        content = await self.tracking_service.handle_thread_command(tracking_command, thread_id or "")
                        for chunk in _chunk(content, self.settings.discord_max_response_chars):
                            await thread.send(chunk)
                        DISCORD_MESSAGES.labels(result="tracking_command").inc()
                        return
                    repository = getattr(self.runner, "repository", None)
                    db_thread_id = None
                    recent_messages: list[dict[str, Any]] = []
                    if repository is not None:
                        db_thread_id = await repository.upsert_discord_thread(
                            discord_guild_id=_maybe_str(getattr(getattr(message, "guild", None), "id", None)),
                            discord_channel_id=_maybe_str(channel_id),
                            discord_thread_id=thread_id,
                            title=_thread_name(prompt),
                        )
                        recent_messages = await repository.get_recent_messages(db_thread_id, limit=8)
                        await repository.add_message(db_thread_id, "user", prompt, discord_user_id=_maybe_str(getattr(message.author, "id", None)))
                    referenced_content = await _referenced_message_content(message, self.client.user)
                    agent_context = AgentContext(
                        source="discord",
                        discord_guild_id=_maybe_str(getattr(getattr(message, "guild", None), "id", None)),
                        discord_channel_id=_maybe_str(channel_id),
                        discord_thread_id=thread_id,
                        discord_user_id=_maybe_str(getattr(message.author, "id", None)),
                        conversation_context=_build_conversation_context(referenced_content, recent_messages),
                    )
                    response = await self.runner.answer(prompt, context=agent_context)
                    for chunk in _chunk(response.content, self.settings.discord_max_response_chars):
                        await thread.send(chunk)
                    self.diagnostics.reply_succeeded()
                    if repository is not None:
                        await repository.add_message(db_thread_id, "assistant", response.content)
                DISCORD_MESSAGES.labels(result="ok").inc()
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                self.diagnostics.reply_failed(f"{type(exc).__name__}: {exc}")
                DISCORD_MESSAGES.labels(result="error").inc()
                log.exception("discord_message_failed", error=type(exc).__name__)
                await message.reply("I hit an internal error while answering. No trade was placed.", mention_author=False)

        @self.client.event
        async def on_reaction_add(reaction, user):
            if getattr(user, "bot", False) or self.client.user is None:
                return
            message = getattr(reaction, "message", None)
            if not _is_message_from_bot(message, self.client.user):
                return
            command = parse_prediction_market_reaction(str(getattr(reaction, "emoji", "") or ""), referenced_message=message)
            if command is None:
                return
            channel_id = _authorized_channel_id(message)
            role_ids = {int(getattr(role, "id", 0)) for role in getattr(user, "roles", [])}
            context = DiscordContext(
                guild_id=getattr(getattr(message, "guild", None), "id", None),
                channel_id=channel_id,
                author_id=getattr(user, "id", None),
            )
            if not self.is_authorized(context, role_ids=role_ids):
                DISCORD_MESSAGES.labels(result="unauthorized").inc()
                return
            try:
                content = await self._handle_prediction_market_command(
                    command,
                    context=context,
                    user_id=_maybe_str(getattr(user, "id", None)),
                    role_ids=role_ids,
                )
                for chunk in _chunk(content, self.settings.discord_max_response_chars):
                    await message.reply(chunk, mention_author=False)
                DISCORD_MESSAGES.labels(result="prediction_market_reaction").inc()
            except Exception as exc:  # pragma: no cover - Discord runtime behavior
                DISCORD_MESSAGES.labels(result="error").inc()
                log.exception("discord_reaction_failed", error=type(exc).__name__)

    async def _handle_paper_command(self, command: PaperDiscordCommand, *, user_id: str | None, role_ids: set[int]) -> str:
        if command.action in {"portfolio", "positions", "orders"}:
            return await self._handle_paper_read(command)
        if not self.settings.paper_trading_enabled:
            return "Manual paper trading is disabled for this runtime. Set PAPER_TRADING_ENABLED=true to use Discord paper commands. No live trade was placed."
        if command.error:
            return f"{command.error} No live trade was placed."
        if not self._is_admin(user_id, role_ids):
            return "Not authorized for paper trading commands."
        repository = self._repository()
        if repository is None:
            return "Paper command routing is unavailable in this Discord worker. No live trade was placed."

        if command.action == "council_send":
            return await self._handle_council_paper_send(command, user_id=user_id)
        if command.action == "draft":
            command_type = "paper_trade_draft"
            if command.draft is not None:
                payload = command.draft.model_dump(mode="json")
                payload["actor"] = user_id or "discord"
            else:
                payload = {"proposal_id": command.proposal_id, "actor": user_id or "discord", "source": "manual_discord"}
        elif command.action == "confirm":
            command_type = "paper_trade_confirm"
            payload = {"order_id": command.order_id, "actor": user_id or "discord", "close_opposite": command.close_opposite}
        elif command.action == "cancel":
            command_type = "paper_trade_cancel"
            payload = {"order_id": command.order_id, "actor": user_id or "discord", "reason": command.reason or "discord_cancel"}
        elif command.action == "close":
            command_type = "paper_position_close"
            payload = {"position_ref": command.position_ref, "actor": user_id or "discord", "price": command.price, "reason": command.reason or "discord_manual_close"}
        else:
            return "Unknown paper command. No live trade was placed."

        queued = await repository.enqueue_worker_command(
            target_role=ServiceRole.TRADER.value,
            command_type=command_type,
            payload={key: value for key, value in payload.items() if value is not None},
            requested_by="discord_bot",
        )
        return await self._paper_command_response(command, queued)

    async def _handle_council_paper_send(self, command: PaperDiscordCommand, *, user_id: str | None) -> str:
        repository = self._repository()
        if repository is None:
            return "Paper command routing is unavailable in this Discord worker. No live trade was placed."
        if not self.settings.high_stakes_debate_enabled:
            return "Agent Council is disabled for this runtime. Set HIGH_STAKES_DEBATE_ENABLED=true to derive levels before paper-send. No live trade was placed."
        if not command.symbol or not command.side:
            return "Missing symbol or side for Agent Council paper-send. No live trade was placed."

        proposal_payload: dict[str, Any] = {
            "prompt": _council_paper_order_prompt(command),
            "dry_run": True,
            "force_debate": True,
        }
        if command.risk_pct is not None:
            proposal_payload["risk_pct"] = command.risk_pct
        proposal_command = await repository.enqueue_worker_command(
            target_role=ServiceRole.AGENT.value,
            command_type="trade_proposal",
            payload=proposal_payload,
            requested_by="discord_bot",
        )
        proposal_state = await self._wait_worker_command(proposal_command, timeout_seconds=_council_timeout_seconds(self.settings))
        proposal_status = str(proposal_state.get("status") or "")
        proposal_id = str(proposal_state.get("command_id") or "")
        if proposal_status != "completed":
            return _worker_not_completed_message("Agent Council", proposal_id, proposal_status, proposal_state)

        proposal_result = proposal_state.get("result") if isinstance(proposal_state.get("result"), dict) else {}
        proposal = proposal_result.get("proposal") if isinstance(proposal_result.get("proposal"), dict) else {}
        response_status = str(proposal_result.get("status") or proposal.get("status") or "")
        if response_status != "paper_ready":
            content = str(proposal_result.get("content") or "Agent Council did not return a paper-ready setup.")
            return f"{content}\n\nNo paper order was sent."
        mismatch = _proposal_mismatch(command, proposal)
        if mismatch:
            content = str(proposal_result.get("content") or "Agent Council returned a proposal that did not match the requested order.")
            return f"{content}\n\nCouncil paper-send blocked: {mismatch}. No paper order was sent."

        draft_payload = _paper_payload_from_council_proposal(command, proposal, proposal_result, user_id=user_id)
        draft_command = await repository.enqueue_worker_command(
            target_role=ServiceRole.TRADER.value,
            command_type="paper_trade_draft",
            payload=draft_payload,
            requested_by="discord_bot",
        )
        draft_state = await self._wait_worker_command(draft_command, timeout_seconds=_paper_mutation_timeout_seconds(self.settings))
        if str(draft_state.get("status") or "") != "completed":
            return _worker_not_completed_message("Paper draft", str(draft_state.get("command_id") or ""), str(draft_state.get("status") or ""), draft_state)
        draft_result = draft_state.get("result") if isinstance(draft_state.get("result"), dict) else {}
        order = draft_result.get("order") if isinstance(draft_result.get("order"), dict) else {}
        order_id = str(order.get("id") or "")
        if not order_id:
            return "Agent Council produced levels, but the paper draft returned no order id. No paper order was sent."

        confirm_command = await repository.enqueue_worker_command(
            target_role=ServiceRole.TRADER.value,
            command_type="paper_trade_confirm",
            payload={"order_id": order_id, "actor": user_id or "discord", "close_opposite": command.close_opposite},
            requested_by="discord_bot",
        )
        confirm_state = await self._wait_worker_command(confirm_command, timeout_seconds=_paper_mutation_timeout_seconds(self.settings))
        if str(confirm_state.get("status") or "") != "completed":
            return _worker_not_completed_message("Paper confirm", str(confirm_state.get("command_id") or ""), str(confirm_state.get("status") or ""), confirm_state)
        confirm_result = confirm_state.get("result") if isinstance(confirm_state.get("result"), dict) else {}
        confirmation = format_paper_result(PaperDiscordCommand(action="confirm", order_id=order_id), confirm_result)
        return _format_council_paper_sent(command, proposal_result, proposal, confirmation)

    async def _paper_command_response(self, paper_command: PaperDiscordCommand, queued: dict[str, Any]) -> str:
        repository = self._repository()
        command_id = str(queued.get("command_id") or "")
        if repository is None or not command_id:
            return "Paper command was accepted, but status polling is unavailable. No live trade was placed."
        timeout_seconds = min(30.0, max(5.0, float(getattr(self.settings, "discord_command_timeout_seconds", 30.0))))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        current = queued
        while asyncio.get_running_loop().time() < deadline:
            current = await repository.get_worker_command(command_id) or current
            status = str(current.get("status") or "")
            if status == "completed":
                result = current.get("result") if isinstance(current.get("result"), dict) else {}
                return format_paper_result(paper_command, result)
            if status in {"failed", "cancelled"}:
                return f"Paper command `{command_id}` {status}: {current.get('last_error') or 'unknown error'}. No live trade was placed."
            await asyncio.sleep(1.0)
        return f"Accepted paper command `{command_id}`; it is still pending. Check `/commands/{command_id}`. No live trade was placed."

    async def _wait_worker_command(self, queued: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        repository = self._repository()
        command_id = str(queued.get("command_id") or "")
        if repository is None or not command_id:
            return queued
        if str(queued.get("status") or "") == "accepted_unpersisted":
            return queued
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        current = queued
        while asyncio.get_running_loop().time() < deadline:
            current = await repository.get_worker_command(command_id) or current
            if str(current.get("status") or "") in {"completed", "failed", "cancelled"}:
                return current
            await asyncio.sleep(1.0)
        return current

    async def _handle_paper_read(self, command: PaperDiscordCommand) -> str:
        repository = self._repository()
        if repository is None:
            return "Paper portfolio storage is unavailable."
        if command.action == "portfolio":
            snapshot = await repository.get_latest_portfolio_snapshot()
            if snapshot:
                content = _format_paper_snapshot_dict(snapshot)
            else:
                portfolio = await _maybe_get_or_create_paper_portfolio(repository, self.settings)
                content = _format_paper_portfolio_dict(portfolio) if portfolio else "No paper portfolio snapshot yet."
            positions = _open_position_dicts(await _maybe_list_paper_positions(repository, limit=20))
            if positions:
                content += "\n\n" + _format_paper_positions_dict(positions)
            return content
        if command.action == "positions":
            return _format_paper_positions_dict(await repository.list_paper_positions(limit=20))
        if command.action == "orders":
            return _format_paper_orders_dict(await repository.list_paper_orders(limit=20))
        return "Unknown paper read command."

    def _repository(self) -> Any | None:
        repository = getattr(self.runner, "repository", None)
        return repository if repository is not None and getattr(repository, "enabled", False) else None

    def _is_admin(self, user_id: str | None, role_ids: set[int]) -> bool:
        if user_id and user_id.isdigit() and int(user_id) in self.settings.autonomy_admin_users:
            return True
        return bool(role_ids & self.settings.autonomy_admin_roles)

    async def _handle_chart_command(
        self,
        message: Any,
        command: ChartCommand,
        *,
        context: DiscordContext,
        role_ids: set[int],
    ) -> bool:
        if not self.is_authorized(context, role_ids=role_ids):
            DISCORD_MESSAGES.labels(result="unauthorized").inc()
            await message.reply("Not authorized for this bot/channel.", mention_author=False)
            return True
        if not self.settings.discord_chart_command_enabled:
            DISCORD_MESSAGES.labels(result="chart_disabled").inc()
            await message.reply(
                "Chart commands are disabled for this Discord worker. Set DISCORD_CHART_COMMAND_ENABLED=true and restart the worker. No trade was placed.",
                mention_author=False,
            )
            return True
        if self.charting_service is None:
            DISCORD_MESSAGES.labels(result="chart_unavailable").inc()
            await message.reply("Charting is not configured for this Discord worker. No trade was placed.", mention_author=False)
            return True
        try:
            typing = getattr(getattr(message, "channel", None), "typing", None)
            if callable(typing):
                async with typing():
                    result = await self.charting_service.render(command)
                    await _reply_chart(message, result)
            else:
                result = await self.charting_service.render(command)
                await _reply_chart(message, result)
            DISCORD_MESSAGES.labels(result="chart_command").inc()
        except Exception as exc:  # pragma: no cover - Discord runtime behavior
            DISCORD_MESSAGES.labels(result="error").inc()
            log.exception("discord_chart_command_failed", error=type(exc).__name__)
            await message.reply("I hit an internal error while rendering the chart. No trade was placed.", mention_author=False)
        return True

    async def _handle_prediction_market_command(
        self,
        command: PredictionMarketDiscordCommand,
        *,
        context: DiscordContext,
        user_id: str | None,
        role_ids: set[int],
    ) -> str:
        repository = self._repository()
        if repository is None:
            return "Prediction-market storage is unavailable."
        service = PredictionMarketPaperService(settings=self.settings, repository=repository, hyperliquid=self.hyperliquid)
        guild_id = _maybe_str(context.guild_id) or "dm"
        discord_user_id = user_id or "unknown"

        if command.action == "search":
            return format_prediction_market_search(await service.search(command.query, limit=10))
        if command.action == "portfolio":
            return format_prediction_market_portfolio(await service.portfolio(discord_guild_id=guild_id, discord_user_id=discord_user_id))
        if command.action == "positions":
            items = await repository.list_prediction_market_positions(discord_guild_id=guild_id, discord_user_id=discord_user_id, limit=20)
            return format_prediction_market_positions(items)
        if command.action == "leaderboard":
            return format_prediction_market_leaderboard([row.model_dump(mode="json") for row in await service.leaderboard(discord_guild_id=guild_id, limit=20)])

        if not self.settings.prediction_market_paper_enabled:
            return "Prediction-market paper trading is disabled for this runtime. Set PREDICTION_MARKET_PAPER_ENABLED=true to use Discord prediction-market paper commands."
        if command.error:
            return command.error
        if command.action in {"settle", "settlement_sweep"} and not self._is_admin(user_id, role_ids):
            return "Not authorized for prediction-market settlement commands."

        if command.action == "draft":
            payload = {
                "discord_guild_id": guild_id,
                "discord_user_id": discord_user_id,
                "side": command.side,
                "stake_usd": command.stake_usd,
                "query": command.query,
                "market_ref": command.market_ref,
                "actor": discord_user_id,
                "source": "discord",
            }
            command_type = "prediction_market_bet_draft"
        elif command.action == "confirm":
            command_type = "prediction_market_bet_confirm"
            payload = {"draft_id": command.draft_id, "actor": discord_user_id}
        elif command.action == "cancel":
            command_type = "prediction_market_bet_cancel"
            payload = {"draft_id": command.draft_id, "actor": discord_user_id, "reason": "discord_cancel"}
        elif command.action == "close":
            command_type = "prediction_market_position_close"
            payload = {"position_ref": command.position_ref, "actor": discord_user_id, "reason": "discord_manual_close"}
        elif command.action == "settle" and command.settlement is not None:
            command_type = "prediction_market_settlement_apply"
            payload = command.settlement.model_copy(update={"actor": discord_user_id}).model_dump(mode="json")
        elif command.action == "settlement_sweep":
            command_type = "prediction_market_settlement_sweep"
            payload = {"actor": discord_user_id}
        else:
            return "Unknown prediction-market paper command."

        queued = await repository.enqueue_worker_command(
            target_role=ServiceRole.TRADER.value,
            command_type=command_type,
            payload={key: value for key, value in payload.items() if value is not None},
            requested_by="discord_bot",
        )
        return await self._prediction_market_command_response(command, queued)

    async def _prediction_market_command_response(self, command: PredictionMarketDiscordCommand, queued: dict[str, Any]) -> str:
        repository = self._repository()
        command_id = str(queued.get("command_id") or "")
        if repository is None or not command_id:
            return "Prediction-market paper command was accepted, but status polling is unavailable."
        timeout_seconds = min(30.0, max(5.0, float(getattr(self.settings, "discord_command_timeout_seconds", 30.0))))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        current = queued
        while asyncio.get_running_loop().time() < deadline:
            current = await repository.get_worker_command(command_id) or current
            status = str(current.get("status") or "")
            if status == "completed":
                result = current.get("result") if isinstance(current.get("result"), dict) else {}
                return format_prediction_market_result(command, result)
            if status in {"failed", "cancelled"}:
                return f"Prediction-market paper command `{command_id}` {status}: {current.get('last_error') or 'unknown error'}."
            await asyncio.sleep(1.0)
        return f"Accepted prediction-market paper command `{command_id}`; it is still pending. Check `/commands/{command_id}`."


def _format_paper_snapshot_dict(snapshot: dict[str, Any]) -> str:
    sharpe = snapshot.get("sharpe")
    sharpe_text = "n/a" if sharpe is None else f"{float(sharpe):.2f}"
    return (
        "**Paper portfolio**\n"
        f"Equity: `${float(snapshot.get('equity_usd') or 0):,.2f}`\n"
        f"Cash/Treasury: `${float(snapshot.get('cash_usd') or 0):,.2f}`\n"
        f"Realized PnL: `${float(snapshot.get('realized_pnl_usd') or 0):,.2f}`\n"
        f"Unrealized PnL: `${float(snapshot.get('unrealized_pnl_usd') or 0):,.2f}`\n"
        f"Total PnL: `${float(snapshot.get('total_pnl_usd') or 0):,.2f}`\n"
        f"Gross exposure: `${float(snapshot.get('gross_exposure_usd') or 0):,.2f}` | Net: `${float(snapshot.get('net_exposure_usd') or 0):,.2f}`\n"
        f"Max drawdown: `{float(snapshot.get('drawdown_pct') or 0):.2f}%` | Sharpe: `{sharpe_text}`"
    )


def _format_paper_portfolio_dict(portfolio: dict[str, Any]) -> str:
    initial = float(portfolio.get("initial_equity_usd") or 0)
    cash = float(portfolio.get("cash_usd") or initial)
    realized = float(portfolio.get("realized_pnl_usd") or 0)
    equity = cash
    return (
        "**Paper portfolio**\n"
        f"Equity: `${equity:,.2f}`\n"
        f"Cash/Treasury: `${cash:,.2f}`\n"
        f"Realized PnL: `${realized:,.2f}`\n"
        "Unrealized PnL: `$0.00`\n"
        f"Total PnL: `${equity - initial:,.2f}`\n"
        "Gross exposure: `$0.00` | Net: `$0.00`\n"
        "Max drawdown: `0.00%` | Sharpe: `n/a`"
    )


def _format_paper_positions_dict(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "No paper positions."
    lines = ["**Paper positions**"]
    for item in positions[:20]:
        lines.append(
            f"- `{str(item.get('id') or '')[:8]}` {item.get('symbol')} {item.get('side')} {item.get('status')}: "
            f"qty `{float(item.get('quantity') or 0):.6g}` entry `{float(item.get('avg_entry_px') or 0):.6g}` "
            f"mark `{float(item.get('mark_px') or item.get('avg_entry_px') or 0):.6g}` "
            f"uPnL `${float(item.get('unrealized_pnl_usd') or 0):,.2f}` rPnL `${float(item.get('realized_pnl_usd') or 0):,.2f}`"
        )
    return "\n".join(lines)


async def _maybe_get_or_create_paper_portfolio(repository: Any, settings: Settings) -> dict[str, Any] | None:
    create_or_get = getattr(repository, "create_or_get_paper_portfolio", None)
    if not callable(create_or_get):
        return None
    return await create_or_get(
        name="default",
        initial_equity_usd=settings.autonomy_paper_initial_equity_usd,
        mode=settings.autonomy_mode,
    )


async def _maybe_list_paper_positions(repository: Any, *, limit: int) -> list[dict[str, Any]]:
    list_positions = getattr(repository, "list_paper_positions", None)
    if not callable(list_positions):
        return []
    try:
        return await list_positions(status="open", limit=limit)
    except TypeError:
        return await list_positions(limit=limit)


def _open_position_dicts(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in positions if str(item.get("status") or "open") == "open"]


def _format_paper_orders_dict(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return "No paper orders."
    lines = ["**Paper orders**"]
    for item in orders[:20]:
        fill = item.get("filled_px") or item.get("requested_px") or 0
        lines.append(
            f"- `{str(item.get('id') or '')[:8]}` {item.get('symbol')} {item.get('side')} {item.get('status')}: "
            f"qty `{float(item.get('quantity') or 0):.6g}` px `{float(fill):.6g}` source `{(item.get('metadata') or {}).get('source', '-')}`"
        )
    return "\n".join(lines)


def _council_paper_order_prompt(command: PaperDiscordCommand) -> str:
    side_word = "buy/long" if command.side == "long" else "sell/short"
    original = command.raw_prompt or f"{side_word} {command.symbol} for paper portfolio"
    risk = f"\nOperator risk override: {command.risk_pct:g}%." if command.risk_pct is not None else ""
    flip = "\nOperator allows closing an opposite paper position before opening the new side." if command.close_opposite else ""
    return (
        f"Operator requested a PAPER-ONLY portfolio order proposal: {side_word} {command.symbol}.\n"
        "The operator supplied no entry, stop, or take-profit levels. Agent Council must debate whether current evidence supports a desk-derived setup.\n"
        "If evidence supports the trade, return paper_ready with precise coin, side, entry, stop, take_profit, timeframe, thesis, and invalidation. "
        "If edge, liquidity, invalidation, or risk/reward are not good enough, return no_trade, needs_more_data, or manual_review_required.\n"
        "This is local paper portfolio simulation only. exchange_actions must remain empty; do not claim live execution.\n"
        f"Original Discord request: {original}.{risk}{flip}"
    )


def _proposal_mismatch(command: PaperDiscordCommand, proposal: dict[str, Any]) -> str:
    requested_symbol = str(command.symbol or "").upper()
    proposed_symbol = str(proposal.get("coin") or "").upper()
    requested_side = str(command.side or "").lower()
    proposed_side = str(proposal.get("side") or "").lower()
    if not proposed_symbol or proposed_symbol != requested_symbol:
        return f"proposal symbol `{proposed_symbol or 'missing'}` did not match requested `{requested_symbol}`"
    if not proposed_side or proposed_side != requested_side:
        return f"proposal side `{proposed_side or 'missing'}` did not match requested `{requested_side}`"
    if _positive_float(proposal.get("entry")) is None:
        return "proposal entry was missing or invalid"
    if _positive_float(proposal.get("stop")) is None:
        return "proposal stop was missing or invalid"
    return ""


def _paper_payload_from_council_proposal(
    command: PaperDiscordCommand,
    proposal: dict[str, Any],
    proposal_result: dict[str, Any],
    *,
    user_id: str | None,
) -> dict[str, Any]:
    metadata = {
        "source": "agent_council",
        "council_proposal_id": proposal_result.get("proposal_id"),
        "council_run_id": proposal_result.get("run_id"),
        "original_prompt": command.raw_prompt,
        "paper_only": True,
        "exchange_actions": [],
    }
    payload: dict[str, Any] = {
        "symbol": str(proposal.get("coin") or command.symbol or "").upper(),
        "side": str(proposal.get("side") or command.side or "").lower(),
        "entry": _positive_float(proposal.get("entry")),
        "stop": _positive_float(proposal.get("stop")),
        "take_profit": _positive_float(proposal.get("take_profit")),
        "risk_pct": _positive_float(proposal.get("risk_pct")) or command.risk_pct,
        "thesis": str(proposal.get("thesis") or proposal.get("judge_summary") or "Agent Council desk-derived paper setup.")[:1000],
        "actor": user_id or "discord",
        "source": "agent_council",
        "proposal_id": proposal_result.get("proposal_id"),
        "close_opposite": command.close_opposite,
        "metadata": metadata,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _format_council_paper_sent(
    command: PaperDiscordCommand,
    proposal_result: dict[str, Any],
    proposal: dict[str, Any],
    confirmation: str,
) -> str:
    proposal_id = str(proposal_result.get("proposal_id") or "-")
    run_id = str(proposal_result.get("run_id") or "-")
    tp = _fmt_optional(proposal.get("take_profit"))
    summary = str(proposal.get("judge_summary") or proposal_result.get("status") or "Agent Council returned paper_ready.")
    return (
        "Agent Council returned `paper_ready` for the no-level paper order.\n"
        f"Setup: {command.symbol} {command.side} entry `{_fmt_optional(proposal.get('entry'))}` stop `{_fmt_optional(proposal.get('stop'))}` TP `{tp}`.\n"
        f"Council: proposal `{proposal_id}` run `{run_id}`. {summary[:240]}\n"
        f"{confirmation}"
    )


def _worker_not_completed_message(label: str, command_id: str, status: str, state: dict[str, Any]) -> str:
    status_text = status or "unknown"
    if status_text in {"failed", "cancelled"}:
        return f"{label} command `{command_id}` {status_text}: {state.get('last_error') or 'unknown error'}. No paper order was sent."
    if status_text == "accepted_unpersisted":
        return f"{label} command was accepted, but persistent worker polling is unavailable. No paper order was sent."
    return f"{label} command `{command_id}` is still `{status_text}`. Check `/commands/{command_id}`. No paper order was sent."


def _council_timeout_seconds(settings: Settings) -> float:
    return min(240.0, max(30.0, float(getattr(settings, "high_stakes_timeout_seconds", 90)) + 30.0))


def _paper_mutation_timeout_seconds(settings: Settings) -> float:
    return min(45.0, max(10.0, float(getattr(settings, "discord_command_timeout_seconds", 30.0))))


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _fmt_optional(value: object) -> str:
    number = _positive_float(value)
    return "n/a" if number is None else f"{number:.6g}"


async def _referenced_message_content(message, bot_user) -> str:
    reference = getattr(message, "reference", None)
    if reference is None:
        return ""
    resolved = getattr(reference, "resolved", None)
    if resolved is None and callable(getattr(getattr(message, "channel", None), "fetch_message", None)):
        message_id = getattr(reference, "message_id", None)
        if message_id is not None:
            try:
                resolved = await message.channel.fetch_message(message_id)
            except Exception:  # pragma: no cover - Discord fetch permissions/runtime
                resolved = None
    if resolved is None:
        return ""
    bot_id = getattr(bot_user, "id", None)
    author_id = getattr(getattr(resolved, "author", None), "id", None)
    if bot_id is not None and author_id is not None and author_id != bot_id:
        return ""
    return str(getattr(resolved, "content", "") or "").strip()


def _build_conversation_context(referenced_content: str = "", recent_messages: list[dict[str, Any]] | None = None) -> str:
    parts: list[str] = []
    if referenced_content.strip():
        parts.append("Message being replied to:\n" + _trim_context_text(referenced_content, 1800))
    recent = recent_messages or []
    if recent:
        lines = []
        for item in recent[-8:]:
            role = str(item.get("role") or "message").title()
            raw_content = str(item.get("content") or "")
            if _skip_memory_message(role, raw_content):
                continue
            content = _trim_context_text(raw_content, 700)
            if content:
                lines.append(f"{role}: {content}")
        if lines:
            parts.append("Recent thread memory:\n" + "\n".join(lines))
    return "\n\n".join(parts)[:5000]


def _skip_memory_message(role: str, content: str) -> bool:
    if role.lower() != "assistant":
        return False
    lowered = " ".join(content.lower().split())
    noisy_markers = (
        "high-stakes debate timed out before convergence",
        "deterministic_debate_fallback",
        "manual_review_required",
    )
    return any(marker in lowered for marker in noisy_markers)


def _trim_context_text(text: str, max_chars: int) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= max_chars else cleaned[: max_chars - 1].rstrip() + "…"


def _message_prompt_without_mentions(content: str) -> str:
    return " ".join(MENTION_RE.sub(" ", content).split())


def _authorized_channel_id(message) -> int | None:
    channel = getattr(message, "channel", None)
    parent = getattr(channel, "parent", None)
    return getattr(parent, "id", None) or getattr(channel, "id", None)


async def _ensure_thread(
    message,
    prompt: str,
    *,
    diagnostics: DiscordMentionPathDiagnostics | None = None,
):
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
                if diagnostics is not None:
                    diagnostics.thread_fallback(f"{type(exc).__name__}: {exc}")
                log.warning("discord_thread_create_failed", error=type(exc).__name__)
        except Exception as exc:  # pragma: no cover - Discord permission/runtime behavior
            if diagnostics is not None:
                diagnostics.thread_fallback(f"{type(exc).__name__}: {exc}")
            log.warning("discord_thread_create_failed", error=type(exc).__name__)
    return message.channel


def _diagnostic_now_ms() -> int:
    return int(time.time() * 1000)


def _is_thread_channel(channel) -> bool:
    if channel is None:
        return False
    if discord is not None and hasattr(discord, "Thread") and isinstance(channel, discord.Thread):
        return True
    channel_type = getattr(getattr(channel, "type", None), "name", "")
    if channel_type in {"public_thread", "private_thread", "news_thread"}:
        return True
    return getattr(channel, "owner_id", None) is not None and getattr(channel, "parent", None) is not None


async def _resolve_referenced_message(message) -> Any:
    """Resolve a referenced (replied-to) message so the parser can infer context.

    In discord.py v2, ``Message.reference`` may already be a fully-resolved
    ``Message`` instance, or a ``MessageReference`` with a ``resolved``
    attribute that needs to be fetched. Returns ``None`` if not a reply or if
    the reference cannot be resolved.
    """
    ref = getattr(message, "reference", None)
    if ref is None:
        return None
    resolved = getattr(ref, "resolved", None)
    if resolved is not None:
        return resolved
    message_id = getattr(ref, "message_id", None)
    channel = getattr(message, "channel", None)
    if message_id is None or channel is None:
        return None
    fetch = getattr(channel, "fetch_message", None)
    if fetch is None:
        return None
    try:
        return await fetch(message_id)
    except Exception as exc:  # pragma: no cover - Discord permission/runtime behavior
        log.warning("discord_reference_resolve_failed", error=type(exc).__name__)
        return None


def _is_bot_thread(channel, bot_user) -> bool:
    if not _is_thread_channel(channel):
        return False
    bot_id = getattr(bot_user, "id", None)
    if bot_id is None:
        return False
    return getattr(channel, "owner_id", None) == bot_id


def _is_message_from_bot(message: Any, bot_user: Any) -> bool:
    if message is None or bot_user is None:
        return False
    bot_id = getattr(bot_user, "id", None)
    author_id = getattr(getattr(message, "author", None), "id", None)
    return bot_id is not None and author_id is not None and author_id == bot_id


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


def _build_embeds(items: list[dict[str, Any]] | None):
    if not items or discord is None:
        return None
    return [discord.Embed.from_dict(item) for item in items]


def _build_file(filename: str, data: bytes):
    if not data or discord is None:
        return None
    return discord.File(BytesIO(data), filename=filename)


async def _reply_chart(message: Any, result) -> None:
    file = _build_file(result.filename, result.image_png)
    if file is not None:
        await message.reply(content=result.content, file=file, mention_author=False)
    else:
        await message.reply(result.content, mention_author=False)


def _maybe_str(value) -> str | None:
    return None if value is None else str(value)
