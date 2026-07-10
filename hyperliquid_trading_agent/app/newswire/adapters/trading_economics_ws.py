from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import websockets

from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.adapters.base import NewswireAdapter, RawEmit
from hyperliquid_trading_agent.app.newswire.normalize import created_at_ms_from
from hyperliquid_trading_agent.app.newswire.schemas import Action, RawNewsItem

log = get_logger(__name__)


class TradingEconomicsAdapter(NewswireAdapter):
    """Trading Economics calendar WebSocket — scheduled macro releases (CPI, FOMC, NFP...).

    Defensive about the exact payload shape; emits one macro event per calendar update.
    """

    name = "trading_economics"

    def __init__(self, *, ws_url: str, api_key: str):
        self.ws_url = ws_url
        self.api_key = api_key
        self._stop = asyncio.Event()
        self._seen_fingerprints: dict[str, str] = {}
        self.messages_received = 0
        self.events_emitted = 0
        self.duplicate_messages = 0
        self.update_events = 0
        self.last_event_at_ms: int | None = None

    def _connect_url(self) -> str:
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}client={self.api_key}" if self.api_key else self.ws_url

    async def run(self, emit: RawEmit) -> None:
        async with websockets.connect(self._connect_url(), ping_interval=20) as ws:
            await ws.send(json.dumps({"topic": "subscribe", "to": "calendar"}))
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.messages_received += 1
                messages = message if isinstance(message, list) else [message]
                for value in messages:
                    item = self._to_raw(value)
                    if item is not None:
                        await emit(item)
                        self.events_emitted += 1
                        self.last_event_at_ms = int(time.time() * 1000)

    async def stop(self) -> None:
        self._stop.set()

    def _to_raw(self, message: dict[str, Any]) -> RawNewsItem | None:
        if not isinstance(message, dict):
            return None
        nested = message.get("data")
        payload = dict(nested) if isinstance(nested, dict) else message
        if not payload.get("event"):
            return None
        country = str(payload.get("country") or "")
        event = str(payload.get("event") or "")
        actual = _present_value(payload.get("actual"))
        forecast = _first_present(payload, "forecast", "forecast ", "teforecast")
        previous = _present_value(payload.get("previous"))
        revised = _present_value(payload.get("revised"))
        headline = f"{country} {event}".strip()
        if actual is not None:
            headline += f": actual {actual}"
            if forecast is not None:
                headline += f" vs forecast {forecast}"
            if previous is not None:
                headline += f" (prev {previous})"
            if revised is not None and revised != previous:
                headline += f" revised {revised}"
        external_id = str(
            payload.get("CalendarId")
            or payload.get("calendarId")
            or f"{country}:{event}:{payload.get('date')}:{payload.get('ticker') or payload.get('symbol') or ''}"
        )
        fingerprint_fields = {
            "actual": actual,
            "forecast": forecast,
            "previous": previous,
            "revised": revised,
            "date": payload.get("date"),
            "importance": payload.get("importance"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_fields, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        previous_fingerprint = self._seen_fingerprints.get(external_id)
        if previous_fingerprint == fingerprint:
            self.duplicate_messages += 1
            return None
        action: Action = "updated" if previous_fingerprint is not None else "created"
        self._seen_fingerprints[external_id] = fingerprint
        if action == "updated":
            self.update_events += 1
        body_parts = [str(payload.get("category") or "").strip()]
        for label, value in (
            ("importance", _present_value(payload.get("importance"))),
            ("reference", _present_value(payload.get("reference"))),
            ("ticker", _present_value(payload.get("ticker") or payload.get("symbol"))),
            ("unit", _present_value(payload.get("unit"))),
        ):
            if value is not None:
                body_parts.append(f"{label}: {value}")
        return RawNewsItem(
            source="trading_economics",
            provider="trading_economics",
            transport="websocket",
            external_id=external_id,
            action=action,
            headline=headline,
            body="; ".join(part for part in body_parts if part),
            published_at_ms=created_at_ms_from(payload.get("date")),
            asset_class="macro",
            event_type="macro",
            raw=payload,
        )

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "messages_received": self.messages_received,
            "events_emitted": self.events_emitted,
            "duplicates_dropped": self.duplicate_messages,
            "updates_emitted": self.update_events,
            "last_event_at_ms": self.last_event_at_ms,
            "credentials_configured": bool(self.api_key),
            "update_semantics": "stable_calendar_id_revision",
            "delete_semantics": "not_applicable_calendar_stream",
        }

    def safe_error_detail(self, exc: Exception) -> str:
        detail = str(exc)
        if self.api_key:
            detail = detail.replace(self.api_key, "[REDACTED]")
        return detail[:500]


def _present_value(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a"}:
        return None
    return value


def _first_present(payload: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = _present_value(payload.get(key))
        if value is not None:
            return value
    return None
