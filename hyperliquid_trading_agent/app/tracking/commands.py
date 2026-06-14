from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TrackingAction = Literal["status", "stop", "pause", "resume", "events", "set_ttl"]

_COIN_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,15}\b")
_TRACKER_ID_RE = re.compile(r"\b[a-fA-F0-9]{16,64}\b")
_TTL_RE = re.compile(r"(?:track\s+until|tracking\s+until|for)\s+(?P<value>\d+)\s*(?P<unit>h|hr|hrs|hour|hours|d|day|days)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TrackingCommand:
    action: TrackingAction
    coin: str | None = None
    tracker_id: str | None = None
    ttl_hours: int | None = None


def parse_tracking_command(text: str) -> TrackingCommand | None:
    cleaned = " ".join(text.strip().split())
    lowered = cleaned.lower()
    if not cleaned:
        return None
    if "track" not in lowered and "level alert" not in lowered:
        return None

    ttl = _ttl_hours(cleaned)
    if ttl is not None:
        return TrackingCommand(action="set_ttl", coin=_coin(cleaned), tracker_id=_tracker_id(cleaned), ttl_hours=ttl)
    if any(phrase in lowered for phrase in ["tracking status", "track status", "are you tracking", "what are you tracking"]):
        return TrackingCommand(action="status", coin=_coin(cleaned), tracker_id=_tracker_id(cleaned))
    if any(phrase in lowered for phrase in ["tracking events", "track events", "level alerts"]):
        return TrackingCommand(action="events", coin=_coin(cleaned), tracker_id=_tracker_id(cleaned))
    if any(phrase in lowered for phrase in ["stop tracking", "cancel tracking", "disable tracking"]):
        return TrackingCommand(action="stop", coin=_coin(cleaned), tracker_id=_tracker_id(cleaned))
    if any(phrase in lowered for phrase in ["pause tracking", "suspend tracking"]):
        return TrackingCommand(action="pause", coin=_coin(cleaned), tracker_id=_tracker_id(cleaned))
    if any(phrase in lowered for phrase in ["resume tracking", "restart tracking", "enable tracking"]):
        return TrackingCommand(action="resume", coin=_coin(cleaned), tracker_id=_tracker_id(cleaned))
    return None


def _coin(text: str) -> str | None:
    for match in _COIN_RE.finditer(text):
        token = match.group(0).upper()
        if token not in {"TRACK", "TRACKING", "STATUS", "STOP", "PAUSE", "RESUME", "EVENTS", "UNTIL"}:
            return token
    return None


def _tracker_id(text: str) -> str | None:
    match = _TRACKER_ID_RE.search(text)
    return match.group(0).lower() if match else None


def _ttl_hours(text: str) -> int | None:
    match = _TTL_RE.search(text)
    if not match:
        return None
    value = int(match.group("value"))
    unit = match.group("unit").lower()
    return value * 24 if unit.startswith("d") else value
