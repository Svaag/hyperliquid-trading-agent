from __future__ import annotations

import math
import time
from typing import Any, Iterable
from uuid import uuid4

from hyperliquid_trading_agent.app.tracking.schemas import (
    CrossDirection,
    LevelKind,
    PositionTrackingPlan,
    TrackedLevelSpec,
)

DEFAULT_TRACKING_TTL_HOURS = 168
DEFAULT_REARM_BAND_BPS = 10.0
DEDUP_BAND_BPS = 2.0

_LEVEL_PRIORITY: dict[str, int] = {
    "hard_stop": 0,
    "technical_exit": 1,
    "entry_trim": 2,
    "entry_reclaim": 3,
    "take_profit": 4,
    "resistance_confirm": 5,
    "support_confirm": 5,
}


def derive_position_tracking_plan(
    *,
    coin: str | None,
    side: str | None,
    entry: float | int | str | None,
    stop: float | int | str | None,
    take_profit: float | int | str | None = None,
    features: dict[str, Any] | None = None,
    proposal_id: str | None = None,
    run_id: str | None = None,
    agent_context: dict[str, Any] | None = None,
    ttl_hours: int = DEFAULT_TRACKING_TTL_HOURS,
    rearm_band_bps: float = DEFAULT_REARM_BAND_BPS,
    now_ms: int | None = None,
) -> PositionTrackingPlan | None:
    """Build canonical level alerts for a position review.

    This is deliberately deterministic and consumes structured market features,
    not rendered answer text. The response formatter and future live tracker
    should use this plan as the single source of truth for mentioned levels.
    """

    normalized_coin = str(coin or "").strip().upper()
    normalized_side = str(side or "").lower().strip()
    entry_px = _float_or_none(entry)
    stop_px = _float_or_none(stop)
    tp_px = _float_or_none(take_profit)
    if not normalized_coin or normalized_side not in {"long", "short"} or entry_px is None or stop_px is None:
        return None
    if not _valid_price(entry_px) or not _valid_price(stop_px):
        return None
    if tp_px is not None and not _valid_price(tp_px):
        tp_px = None

    features = features or {}
    current = _current_price(features, normalized_coin)
    candles = _feature_for_coin(features.get("candles"), normalized_coin)
    recent_support = _float_or_none(candles.get("recent_support")) if isinstance(candles, dict) else None
    recent_resistance = _float_or_none(candles.get("recent_resistance")) if isinstance(candles, dict) else None
    now_ms = now_ms or int(time.time() * 1000)
    plan_id = uuid4().hex
    levels: list[TrackedLevelSpec] = []

    def add(
        kind: LevelKind,
        label: str,
        price: float | None,
        direction: CrossDirection,
        *,
        terminal: bool = False,
        severity: str = "warning",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if price is None or not _valid_price(price):
            return
        level_price_value = float(price)
        levels.append(
            TrackedLevelSpec(
                id=_level_id(plan_id, kind, level_price_value),
                kind=kind,
                label=label,
                price=level_price_value,
                direction=direction,
                terminal=terminal,
                severity=severity,  # type: ignore[arg-type]
                rearm_band_bps=rearm_band_bps,
                metadata=metadata or {},
            )
        )

    is_long = normalized_side == "long"
    if is_long:
        add("hard_stop", "Hard stop", stop_px, "cross_down", terminal=True, severity="critical")
        if _valid_long_technical_exit(recent_support, stop_px, current):
            add(
                "technical_exit",
                "Technical reduce/exit trigger",
                recent_support,
                "cross_down",
                terminal=True,
                severity="critical",
                metadata={"basis": "recent_support"},
            )
        if current is not None:
            if current > entry_px:
                add("entry_trim", "Entry trim/caution", entry_px, "cross_down", severity="warning")
            elif current < entry_px:
                add("entry_reclaim", "Entry reclaim", entry_px, "cross_up", severity="info")
            if recent_resistance is not None and _valid_price(recent_resistance) and recent_resistance > current:
                add(
                    "resistance_confirm",
                    "Resistance confirmation",
                    recent_resistance,
                    "cross_up",
                    severity="info",
                    metadata={"basis": "recent_resistance"},
                )
        if tp_px is not None:
            add("take_profit", "Take-profit alert", tp_px, "cross_up", severity="info")
    else:
        add("hard_stop", "Hard stop", stop_px, "cross_up", terminal=True, severity="critical")
        if _valid_short_technical_exit(recent_resistance, stop_px, current):
            add(
                "technical_exit",
                "Technical reduce/exit trigger",
                recent_resistance,
                "cross_up",
                terminal=True,
                severity="critical",
                metadata={"basis": "recent_resistance"},
            )
        if current is not None:
            if current < entry_px:
                add("entry_trim", "Entry trim/caution", entry_px, "cross_up", severity="warning")
            elif current > entry_px:
                add("entry_reclaim", "Entry reclaim", entry_px, "cross_down", severity="info")
            if recent_support is not None and _valid_price(recent_support) and recent_support < current:
                add(
                    "support_confirm",
                    "Support confirmation",
                    recent_support,
                    "cross_down",
                    severity="info",
                    metadata={"basis": "recent_support"},
                )
        if tp_px is not None:
            add("take_profit", "Take-profit alert", tp_px, "cross_down", severity="info")

    levels = _dedupe_levels(levels)
    if not levels:
        return None

    context = agent_context or {}
    return PositionTrackingPlan(
        id=plan_id,
        proposal_id=proposal_id,
        run_id=run_id,
        coin=normalized_coin,
        side=normalized_side,  # type: ignore[arg-type]
        entry=entry_px,
        stop=stop_px,
        take_profit=tp_px,
        current_price_at_arm=current,
        levels=levels,
        expires_at_ms=now_ms + int(ttl_hours * 60 * 60 * 1000),
        discord_guild_id=_maybe_str(context.get("discord_guild_id")),
        discord_channel_id=_maybe_str(context.get("discord_channel_id")),
        discord_thread_id=_maybe_str(context.get("discord_thread_id")),
        discord_user_id=_maybe_str(context.get("discord_user_id") or context.get("actor")),
        metadata={
            "source": "auto_high_stakes_position_review",
            "dedup_band_bps": DEDUP_BAND_BPS,
            "ttl_hours": ttl_hours,
        },
    )


def summarize_tracking_plan(plan: PositionTrackingPlan | dict[str, Any] | None, *, max_levels: int = 8) -> list[str]:
    parsed = _parse_plan(plan)
    if parsed is None:
        return []
    lines: list[str] = []
    for level in parsed.levels[:max_levels]:
        direction = "cross down through" if level.direction == "cross_down" else "cross up through"
        terminal = "terminal" if level.terminal else "alert"
        lines.append(f"{level.label}: {direction} {level.price:g} ({level.severity}, {terminal})")
    return lines


def level_price(plan: PositionTrackingPlan | dict[str, Any] | None, kind: LevelKind) -> float | None:
    parsed = _parse_plan(plan)
    if parsed is None:
        return None
    for level in parsed.levels:
        if level.kind == kind:
            return level.price
    return None


def level_by_kind(plan: PositionTrackingPlan | dict[str, Any] | None, kind: LevelKind) -> TrackedLevelSpec | None:
    parsed = _parse_plan(plan)
    if parsed is None:
        return None
    for level in parsed.levels:
        if level.kind == kind:
            return level
    return None


def _parse_plan(plan: PositionTrackingPlan | dict[str, Any] | None) -> PositionTrackingPlan | None:
    if plan is None:
        return None
    if isinstance(plan, PositionTrackingPlan):
        return plan
    try:
        return PositionTrackingPlan.model_validate(plan)
    except Exception:
        return None


def _valid_long_technical_exit(recent_support: float | None, stop: float, current: float | None) -> bool:
    if recent_support is None or current is None or not _valid_price(recent_support):
        return False
    return bool(recent_support > stop and recent_support < current * 1.01)


def _valid_short_technical_exit(recent_resistance: float | None, stop: float, current: float | None) -> bool:
    if recent_resistance is None or current is None or not _valid_price(recent_resistance):
        return False
    return bool(recent_resistance < stop and recent_resistance > current * 0.99)


def _dedupe_levels(levels: Iterable[TrackedLevelSpec]) -> list[TrackedLevelSpec]:
    ordered = sorted(levels, key=lambda item: (_LEVEL_PRIORITY.get(item.kind, 99), item.price))
    kept: list[TrackedLevelSpec] = []
    for candidate in ordered:
        duplicate_index = next((idx for idx, item in enumerate(kept) if _within_bps(candidate.price, item.price, DEDUP_BAND_BPS)), None)
        if duplicate_index is None:
            kept.append(candidate)
            continue
        existing = kept[duplicate_index]
        if candidate.kind == "hard_stop" and existing.kind != "hard_stop":
            kept[duplicate_index] = candidate
    return kept


def _within_bps(first: float, second: float, band_bps: float) -> bool:
    if second == 0:
        return False
    return abs(first - second) / abs(second) * 10_000 <= band_bps


def _current_price(features: dict[str, Any], coin: str) -> float | None:
    market = _feature_for_coin(features.get("market"), coin)
    if isinstance(market, dict):
        for key in ["mid", "mark", "oracle"]:
            value = _float_or_none(market.get(key))
            if _valid_price(value):
                return value
    candles = _feature_for_coin(features.get("candles"), coin)
    if isinstance(candles, dict):
        return _float_or_none(candles.get("last_close"))
    return None


def _feature_for_coin(container: Any, coin: str) -> dict[str, Any]:
    if not isinstance(container, dict):
        return {}
    if coin in container and isinstance(container[coin], dict):
        return container[coin]
    upper = coin.upper()
    for key, value in container.items():
        if str(key).upper() == upper and isinstance(value, dict):
            return value
    return {}


def _valid_price(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _level_id(plan_id: str, kind: str, price: float) -> str:
    safe_price = f"{price:.10g}".replace("-", "m").replace(".", "p")
    return f"{plan_id}:{kind}:{safe_price}"


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)
