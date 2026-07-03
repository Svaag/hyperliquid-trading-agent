from __future__ import annotations

import html
from typing import Any

from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent

_COLOR_BREAKING = 0xE74C3C
_COLOR_HIGH = 0xF39C12
_COLOR_DIGEST = 0x3498DB
_COLOR_NORMAL = 0x95A5A6


def _icon(event: NewswireEvent) -> str:
    if event.urgency == "breaking":
        return "🚨"
    if event.importance_score >= 70:
        return "⚠️"
    return "📰"


def _symbols(event: NewswireEvent) -> str:
    return " ".join(f"${symbol}" for symbol in event.symbols)


def _action_note(event: NewswireEvent) -> str:
    if event.action == "updated":
        return " (correction/update)"
    if event.action == "removed":
        return " (retracted)"
    return ""


def _color(event: NewswireEvent) -> int:
    if event.urgency == "breaking":
        return _COLOR_BREAKING
    if event.importance_score >= 70:
        return _COLOR_HIGH
    return _COLOR_NORMAL


def _trim(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clean_text(value: Any) -> str:
    return html.unescape(" ".join(str(value or "").split()))


def _event_description(event: NewswireEvent) -> str:
    parts: list[str] = []
    if event.enrichment and event.enrichment.get("summary"):
        parts.append(_trim(event.enrichment["summary"], 700))
    elif event.body:
        parts.append(_trim(event.body, 900))
    if event.enrichment and event.enrichment.get("why_it_matters"):
        parts.append(f"**Why it matters:** {_trim(event.enrichment['why_it_matters'], 450)}")
    if event.tradability.halted_symbols:
        parts.append(f"⛔ Halt state: {', '.join(event.tradability.halted_symbols)} — confirm before acting.")
    return _trim("\n\n".join(parts), 3500) or _trim(event.headline, 3500)


def format_news_event(event: NewswireEvent) -> str:
    """Plaintext fallback retained for tests, logs, and non-embed sinks."""
    symbols = _symbols(event) or "—"
    why = ""
    if event.enrichment and event.enrichment.get("why_it_matters"):
        why = f"\n_why:_ {_trim(event.enrichment['why_it_matters'], 240)}"
    url = f"\n{event.url}" if event.url else ""
    halt = ""
    if event.tradability.halted_symbols:
        halt = f"\n⛔ Halt state: {', '.join(event.tradability.halted_symbols)} — confirm before acting."
    return (
        f"{_icon(event)} **{_trim(event.headline, 500)}**{_action_note(event)}\n"
        f"`{event.source}` · {event.event_type} · {event.asset_class} · {event.sentiment} · "
        f"score {event.importance_score:.0f} · {symbols}"
        f"{why}{url}{halt}"
    )


def format_news_digest(events: list[NewswireEvent]) -> str:
    """Plaintext fallback retained for tests, logs, and non-embed sinks."""
    lines = [f"📰 **Newswire digest — {len(events)} update(s)**"]
    for event in events[:25]:
        symbols = _symbols(event)
        tail = f" · {symbols}" if symbols else ""
        lines.append(f"- {_icon(event)} `{event.source}` {_trim(event.headline, 160)}{tail} (score {event.importance_score:.0f})")
    return "\n".join(lines)


def format_news_event_message(event: NewswireEvent) -> dict[str, Any]:
    """Discord message payload for a single high-importance event.

    The dict is intentionally serializable so tests and non-Discord transports can
    inspect it without importing discord.py.
    """
    symbols = _symbols(event) or "—"
    fallback_content = format_news_event(event)
    fields = [
        {"name": "Source", "value": _trim(f"{event.source} / {event.provider}", 1024), "inline": True},
        {"name": "Type", "value": _trim(f"{event.event_type} · {event.asset_class}", 1024), "inline": True},
        {"name": "Score", "value": f"{event.importance_score:.0f} · {event.urgency}", "inline": True},
        {"name": "Symbols", "value": _trim(symbols, 1024), "inline": True},
        {"name": "Sentiment", "value": event.sentiment, "inline": True},
    ]
    embed: dict[str, Any] = {
        "title": _trim(f"{_icon(event)} {event.headline}{_action_note(event)}", 256),
        "description": _event_description(event),
        "color": _color(event),
        "fields": fields,
    }
    if event.url:
        embed["url"] = event.url
    return {"content": "", "fallback_content": _trim(fallback_content, 1800), "embeds": [embed]}


def format_news_digest_message(events: list[NewswireEvent], *, max_items: int = 10) -> dict[str, Any]:
    shown = events[: max(1, max_items)]
    lines = []
    for event in shown:
        symbols = _symbols(event)
        tail = f" · {symbols}" if symbols else ""
        lines.append(f"{_icon(event)} **{_trim(event.headline, 160)}**{tail} — score {event.importance_score:.0f} (`{event.source}`)")
    hidden = max(0, len(events) - len(shown))
    if hidden:
        lines.append(f"…and {hidden} more update(s) in this digest batch.")
    embed = {
        "title": f"📰 Newswire digest — {len(events)} update(s)",
        "description": _trim("\n".join(lines), 3500) or "No digest items.",
        "color": _COLOR_DIGEST,
    }
    fallback_content = format_news_digest(events[: max(1, max_items)])
    if hidden:
        fallback_content += f"\n…and {hidden} more update(s) in this digest batch."
    return {"content": "", "fallback_content": _trim(fallback_content, 1800), "embeds": [embed]}
