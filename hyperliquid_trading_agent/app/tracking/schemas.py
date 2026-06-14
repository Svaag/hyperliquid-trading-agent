from __future__ import annotations

import math
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

TrackerStatus = Literal["pending", "active", "paused", "completed", "expired", "stopped", "error"]
LevelKind = Literal[
    "hard_stop",
    "technical_exit",
    "entry_trim",
    "entry_reclaim",
    "resistance_confirm",
    "support_confirm",
    "take_profit",
]
CrossDirection = Literal["cross_up", "cross_down"]
AlertSeverity = Literal["info", "warning", "critical"]
PriceSource = Literal["allMids"]
RecommendedAction = Literal["notify", "trim", "exit", "confirm_hold"]


def _id() -> str:
    return uuid4().hex


class TrackedLevelSpec(BaseModel):
    id: str = Field(default_factory=_id)
    kind: LevelKind
    label: str
    price: float
    direction: CrossDirection
    terminal: bool = False
    severity: AlertSeverity = "warning"
    armed: bool = True
    hit_count: int = Field(default=0, ge=0)
    rearm_band_bps: float = Field(default=10.0, ge=0.0)
    source: str = "deterministic_position_levels"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("price")
    @classmethod
    def price_must_be_positive(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("tracked level price must be positive and finite")
        return value


class PositionTrackingPlan(BaseModel):
    id: str = Field(default_factory=_id)
    proposal_id: str | None = None
    run_id: str | None = None
    coin: str
    side: Literal["long", "short"]
    entry: float
    stop: float
    take_profit: float | None = None
    current_price_at_arm: float | None = None
    price_source: PriceSource = "allMids"
    levels: list[TrackedLevelSpec] = Field(default_factory=list)
    status: TrackerStatus = "pending"
    expires_at_ms: int
    discord_guild_id: str | None = None
    discord_channel_id: str | None = None
    discord_thread_id: str | None = None
    discord_user_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entry", "stop", "take_profit", "current_price_at_arm")
    @classmethod
    def prices_must_be_positive_when_present(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("tracking plan prices must be positive and finite")
        return value


class LevelHitEvent(BaseModel):
    tracker_id: str
    coin: str
    side: Literal["long", "short"]
    level_id: str
    level_kind: LevelKind
    level_price: float
    current_price: float
    direction: CrossDirection
    terminal: bool = False
    recommended_action: RecommendedAction = "notify"
    exchange_actions: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
