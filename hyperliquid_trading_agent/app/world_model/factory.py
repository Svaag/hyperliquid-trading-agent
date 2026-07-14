from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.world_model.service import WorldModelService
from hyperliquid_trading_agent.app.world_model.v2_service import WorldModelV2Service


def build_world_model_service(*, settings: Any, repository: Any | None = None) -> WorldModelService | WorldModelV2Service:
    if bool(getattr(settings, "world_model_v2_enabled", False)):
        return WorldModelV2Service(settings=settings, repository=repository)
    return WorldModelService(settings=settings, repository=repository)
