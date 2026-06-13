from __future__ import annotations

from pydantic import BaseModel, Field


class ToolEnvelope(BaseModel):
    source: str
    timestamp_ms: int
    freshness: str = "live"
    data: object


class AssetContext(BaseModel):
    coin: str
    mark_px: str | None = Field(default=None, alias="markPx")
    mid_px: str | None = Field(default=None, alias="midPx")
    oracle_px: str | None = Field(default=None, alias="oraclePx")
    funding: str | None = None
    open_interest: str | None = Field(default=None, alias="openInterest")
    day_ntl_vlm: str | None = Field(default=None, alias="dayNtlVlm")
    prev_day_px: str | None = Field(default=None, alias="prevDayPx")
