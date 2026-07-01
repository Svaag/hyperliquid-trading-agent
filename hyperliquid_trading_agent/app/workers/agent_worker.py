from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.agent.high_stakes.context import HighStakesContextBuilder
from hyperliquid_trading_agent.app.agent.high_stakes.graph import HighStakesDebateGraph
from hyperliquid_trading_agent.app.agent.high_stakes.roles import HighStakesRoleRunner
from hyperliquid_trading_agent.app.agent.high_stakes.schemas import TradeProposalRequest
from hyperliquid_trading_agent.app.agent.model_gateway import ModelGateway
from hyperliquid_trading_agent.app.agent.runner import AgentContext, TradingAgentRunner
from hyperliquid_trading_agent.app.agent.tools import AgentTools
from hyperliquid_trading_agent.app.autonomy.memory import MemoryService
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.governance.decision_context import DecisionContextRecorder
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.hyperliquid.ws_worker import HyperliquidWebSocketWorker
from hyperliquid_trading_agent.app.news.service import NewsService
from hyperliquid_trading_agent.app.tracking.service import PositionTrackingService
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.world_model.service import WorldModelService


class AgentWorker(BaseWorker):
    role = ServiceRole.AGENT
    lock_name = "service:agent"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.hyperliquid: HyperliquidClient | None = None
        self.runner: TradingAgentRunner | None = None
        self.graph: HighStakesDebateGraph | None = None

    async def run(self) -> None:
        self.hyperliquid = HyperliquidClient(settings=self.settings)
        news = NewsService(settings=self.settings, repository=self.repository)
        model_gateway = ModelGateway(settings=self.settings)
        tools = AgentTools(hyperliquid=self.hyperliquid, news=news, repository=self.repository, tradfi=None, options_flow=None)
        memory_service = MemoryService(settings=self.settings, repository=self.repository)
        world_model_service = WorldModelService(settings=self.settings, repository=self.repository)
        decision_context_recorder = DecisionContextRecorder(settings=self.settings, repository=self.repository, code_version=__version__)
        await decision_context_recorder.snapshot_startup()
        ws_worker = HyperliquidWebSocketWorker(settings=self.settings)
        tracking_service = PositionTrackingService(settings=self.settings, repository=self.repository, ws_worker=ws_worker)
        high_stakes_context = HighStakesContextBuilder(tools=tools, settings=self.settings, sdk_info=None, world_model_service=world_model_service)
        high_stakes_roles = HighStakesRoleRunner(model_gateway=model_gateway, settings=self.settings, memory_service=memory_service, world_model_service=world_model_service)
        self.graph = HighStakesDebateGraph(
            settings=self.settings,
            context_builder=high_stakes_context,
            role_runner=high_stakes_roles,
            repository=self.repository,
            tracking_service=tracking_service,
            decision_context_recorder=decision_context_recorder,
        )
        self.runner = TradingAgentRunner(tools=tools, model_gateway=model_gateway, repository=self.repository, settings=self.settings, high_stakes_graph=self.graph)
        try:
            await self.command_loop({"ask": self._handle_ask, "trade_proposal": self._handle_trade_proposal})
        finally:
            await self.hyperliquid.close()

    async def _handle_ask(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.runner is None:
            raise RuntimeError("agent_runner_unavailable")
        payload = command.get("payload") or {}
        response = await self.runner.answer(str(payload.get("prompt") or ""), context=AgentContext(source="api-command"))
        return {
            "content": response.content,
            "refused": response.refused,
            "fallback_used": response.fallback_used,
            "model_used": response.model_used,
            "tool_count": len(response.tool_results),
            "decision_run_id": response.decision_run_id,
            "proposal_id": response.proposal_id,
            "high_stakes": response.high_stakes,
        }

    async def _handle_trade_proposal(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("high_stakes_graph_unavailable")
        if not self.settings.high_stakes_debate_enabled:
            raise RuntimeError("high_stakes_debate_disabled")
        payload = command.get("payload") or {}
        request = TradeProposalRequest.model_validate(payload).model_copy(update={"force_debate": True, "dry_run": True})
        response = await self.graph.run(request, agent_context={"source": "api-command", "actor": "api"})
        return response.model_dump(mode="json")

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {"agent": {"runner_configured": self.runner is not None, "high_stakes_configured": self.graph is not None}}
