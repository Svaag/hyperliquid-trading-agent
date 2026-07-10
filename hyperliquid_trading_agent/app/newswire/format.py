from __future__ import annotations

import html
from typing import Any

from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent

_COLOR_BREAKING = 0xE74C3C
_COLOR_HIGH = 0xF39C12
_COLOR_DIGEST = 0x3498DB
_COLOR_NORMAL = 0x3498DB
_DISCORD_MAX_EMBEDS = 10


def _icon(event: NewswireEvent) -> str:
    if event.action == "removed":
        return "⛔"
    if event.urgency == "breaking":
        return "🚨"
    if event.importance_score >= 70:
        return "⚠️"
    return ""


def _symbols(event: NewswireEvent) -> str:
    return " ".join(f"${symbol}" for symbol in event.symbols)


def _action_note(event: NewswireEvent) -> str:
    if event.action == "updated":
        return " · Updated"
    if event.action == "removed":
        return " · Retracted"
    return ""


def _color(event: NewswireEvent) -> int:
    if event.urgency == "breaking":
        return _COLOR_BREAKING
    if event.importance_score >= 70:
        return _COLOR_HIGH
    return _COLOR_NORMAL


def _event_title(event: NewswireEvent, *, limit: int = 256) -> str:
    icon = _icon(event)
    prefix = f"{icon} " if icon else ""
    return _trim(f"{prefix}{event.headline}{_action_note(event)}", limit)


def _source_label(event: NewswireEvent) -> str:
    labels: list[str] = []
    seen: set[str] = set()
    for raw in (event.provider, event.source):
        normalized = str(raw or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        labels.append(_display_name(normalized))
    return " · ".join(labels) or "Unknown source"


def _display_name(value: str) -> str:
    aliases = {
        "ecb": "ECB",
        "sec_edgar": "SEC EDGAR",
        "x": "X",
        "x_cashtag": "X",
    }
    return aliases.get(value, value.replace("_", " ").title())


def _trim(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _trim_multiline(value: Any, limit: int) -> str:
    text = _clean_multiline(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clean_text(value: Any) -> str:
    return html.unescape(" ".join(str(value or "").split()))


def _clean_multiline(value: Any) -> str:
    raw = html.unescape(str(value or "").replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    cleaned: list[str] = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return "\n".join(cleaned).strip()


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
    return _trim_multiline("\n\n".join(parts), 3500) or _trim(event.headline, 3500)


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
        f"**{_event_title(event, limit=500)}**\n"
        f"{_source_label(event)} · {event.event_type} · {event.asset_class} · {event.sentiment} · "
        f"score {event.importance_score:.0f} · {symbols}"
        f"{why}{url}{halt}"
    )


def format_news_digest(events: list[NewswireEvent]) -> str:
    """Plaintext fallback retained for tests, logs, and non-embed sinks."""
    items: list[str] = []
    for event in events[:25]:
        symbols = _symbols(event)
        market = symbols or event.asset_class.title()
        url = f"\n{event.url}" if event.url else ""
        items.append(
            f"**{_event_title(event, limit=120)}**\n"
            f"{market} · {_source_label(event)} · {event.importance_score:.0f}/100{url}"
        )
    return "\n\n".join(items)


def format_news_event_message(event: NewswireEvent) -> dict[str, Any]:
    """Discord message payload for a single high-importance event.

    The dict is intentionally serializable so tests and non-Discord transports can
    inspect it without importing discord.py.
    """
    symbols = _symbols(event) or "—"
    fallback_content = format_news_event(event)
    fields = [
        {"name": "Source", "value": _trim(_source_label(event), 1024), "inline": True},
        {
            "name": "Type",
            "value": _trim(f"{event.event_type.title()} · {event.asset_class.title()}", 1024),
            "inline": True,
        },
        {"name": "Score", "value": f"{event.importance_score:.0f}/100 · {event.urgency.title()}", "inline": True},
        {"name": "Symbols", "value": _trim(symbols, 1024), "inline": True},
        {"name": "Sentiment", "value": event.sentiment.title(), "inline": True},
    ]
    decision = _policy_decision(event)
    if decision:
        fields.extend(
            [
                {
                    "name": "Policy",
                    "value": _trim(f"{decision.get('newswire_action')} / {decision.get('engine_action')}", 1024),
                    "inline": True,
                },
                {"name": "Quality", "value": f"{float(decision.get('quality_score') or 0):.0f}", "inline": True},
                {"name": "Impact", "value": f"{float(decision.get('market_impact_score') or 0):.0f}", "inline": True},
            ]
        )
    embed: dict[str, Any] = {
        "title": _event_title(event),
        "description": _event_description(event),
        "color": _color(event),
        "fields": fields,
    }
    if event.url:
        embed["url"] = event.url
    return {
        "content": "",
        "fallback_content": _trim_multiline(fallback_content, 1800),
        "embeds": [embed],
        "components": _feedback_components(event),
    }


def format_news_digest_message(events: list[NewswireEvent], *, max_items: int = 10) -> dict[str, Any]:
    """Compatibility formatter for sinks that can accept several embeds at once.

    The live publisher sends scheduled stories as individual messages so every story
    can retain its own feedback controls. This helper deliberately has no batch title:
    each event is represented by its own compact Discord card.
    """
    limit = min(_DISCORD_MAX_EMBEDS, max(1, max_items))
    shown = events[:limit]
    embeds = [_digest_embed(event) for event in shown]
    fallback_content = format_news_digest(shown) or "No news items."
    return {"content": "", "fallback_content": _trim_multiline(fallback_content, 1800), "embeds": embeds}


def _digest_embed(event: NewswireEvent) -> dict[str, Any]:
    symbols = _symbols(event) or event.asset_class.title()
    decision = _policy_decision(event)
    priority = str(decision.get("newswire_action") or event.urgency or "standard").replace("_", " ").title()
    embed: dict[str, Any] = {
        "title": _event_title(event),
        "color": _color(event) if event.urgency == "breaking" or event.importance_score >= 70 else _COLOR_DIGEST,
        "fields": [
            {"name": "Market", "value": _trim(symbols, 1024), "inline": True},
            {"name": "Priority", "value": f"{event.importance_score:.0f}/100 · {priority}", "inline": True},
            {"name": "Source", "value": _trim(_source_label(event), 1024), "inline": True},
        ],
    }
    if event.url:
        embed["url"] = event.url
    return embed


def _policy_decision(event: NewswireEvent) -> dict[str, Any]:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    decision = metadata.get("newswire_policy_decision")
    return decision if isinstance(decision, dict) else {}


def _feedback_components(event: NewswireEvent) -> list[dict[str, str]]:
    feedback_id = str(event.story_id or event.metadata.get("story_id") or event.event_id)
    return [
        {"label": "Useful", "custom_id": f"nwfb:{feedback_id}:quality:useful", "style": "success"},
        {"label": "Noise", "custom_id": f"nwfb:{feedback_id}:quality:noise", "style": "danger"},
        {"label": "Duplicate", "custom_id": f"nwfb:{feedback_id}:duplicate:true", "style": "secondary"},
        {"label": "Stale", "custom_id": f"nwfb:{feedback_id}:stale:true", "style": "secondary"},
        {"label": "Wrong Symbol", "custom_id": f"nwfb:{feedback_id}:symbol:false", "style": "secondary"},
        {"label": "Wrong Direction", "custom_id": f"nwfb:{feedback_id}:direction:false", "style": "secondary"},
        {"label": "Risk Only", "custom_id": f"nwfb:{feedback_id}:engine_action:risk_only", "style": "primary"},
        {"label": "Should Be High", "custom_id": f"nwfb:{feedback_id}:newswire_action:high", "style": "primary"},
        {"label": "Should Drop", "custom_id": f"nwfb:{feedback_id}:newswire_action:drop", "style": "danger"},
    ]
