from __future__ import annotations

from typing import Any

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tracking.schemas import LevelHitEvent, PositionTrackingPlan, TrackedLevelSpec

log = get_logger(__name__)


class DiscordAlertSink:
    def __init__(self, discord_bot: Any):
        self.discord_bot = discord_bot

    async def send_level_hit(self, tracker: PositionTrackingPlan, level: TrackedLevelSpec, event: LevelHitEvent) -> str:
        thread_id = tracker.discord_thread_id
        client = getattr(self.discord_bot, "client", None)
        if not thread_id:
            return "skipped:no_discord_thread"
        if client is None or getattr(client, "is_closed", lambda: True)():
            return "skipped:discord_unavailable"
        try:
            channel = client.get_channel(int(thread_id))
            if channel is None and callable(getattr(client, "fetch_channel", None)):
                channel = await client.fetch_channel(int(thread_id))
            if channel is None or not callable(getattr(channel, "send", None)):
                return "error:thread_not_found"
            await channel.send(format_level_hit_alert(tracker, level, event))
            return "sent"
        except Exception as exc:  # pragma: no cover - Discord runtime/network behavior
            log.warning("discord_tracking_alert_failed", tracker_id=tracker.id, thread_id=thread_id, error=type(exc).__name__)
            return f"error:{type(exc).__name__}"


def format_level_hit_alert(tracker: PositionTrackingPlan, level: TrackedLevelSpec, event: LevelHitEvent) -> str:
    icon = "🚨" if level.severity == "critical" else "⚠️" if level.severity == "warning" else "✅"
    direction = "crossed down through" if level.direction == "cross_down" else "crossed up through"
    action = _action_sentence(level, event)
    completed = "\nTracker is now completed." if level.terminal else "\nTracker remains active."
    return (
        f"{icon} {tracker.coin} level hit — {level.label}\n\n"
        f"{tracker.coin} {tracker.side} from {tracker.entry:g} just {direction} {level.price:g}.\n"
        f"Current mid: {event.current_price:g}.\n\n"
        f"{action}{completed}\n"
        "No trade was placed."
    )


def _action_sentence(level: TrackedLevelSpec, event: LevelHitEvent) -> str:
    if event.metadata.get("already_breached"):
        return "This level was already breached when live tracking received its first price update."
    if level.kind == "hard_stop":
        return "This is the hard stop/invalidation level from the original review."
    if level.kind == "technical_exit":
        return "This is the preplanned technical reduce/exit trigger before the hard stop."
    if level.kind == "entry_trim":
        return "This is the entry-loss/trim caution level from the original review."
    if level.kind in {"entry_reclaim", "resistance_confirm", "support_confirm"}:
        return "This improves the hold case from the original review."
    if level.kind == "take_profit":
        return "This is the take-profit alert level from the original review."
    return "This was one of the preplanned levels from the original review."
