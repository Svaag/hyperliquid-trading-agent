from __future__ import annotations

from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent


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


def format_news_event(event: NewswireEvent) -> str:
    symbols = _symbols(event) or "—"
    why = ""
    if event.enrichment and event.enrichment.get("why_it_matters"):
        why = f"\n_why:_ {str(event.enrichment['why_it_matters'])[:240]}"
    url = f"\n{event.url}" if event.url else ""
    halt = ""
    if event.tradability.halted_symbols:
        halt = f"\n⛔ Halt state: {', '.join(event.tradability.halted_symbols)} — confirm before acting."
    return (
        f"{_icon(event)} **{event.headline}**{_action_note(event)}\n"
        f"`{event.source}` · {event.event_type} · {event.asset_class} · {event.sentiment} · "
        f"score {event.importance_score:.0f} · {symbols}"
        f"{why}{url}{halt}\n"
        f"News feed only — no trade was placed."
    )


def format_news_digest(events: list[NewswireEvent]) -> str:
    lines = [f"📰 **Newswire digest — {len(events)} update(s)**"]
    for event in events[:25]:
        symbols = _symbols(event)
        tail = f" · {symbols}" if symbols else ""
        lines.append(f"- {_icon(event)} `{event.source}` {event.headline[:160]}{tail} (score {event.importance_score:.0f})")
    lines.append("News feed only — no trade was placed.")
    return "\n".join(lines)
