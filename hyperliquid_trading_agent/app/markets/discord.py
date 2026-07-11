from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from hyperliquid_trading_agent.app.markets.schemas import WatchlistChangeRequest


@dataclass(frozen=True, slots=True)
class WatchlistDiscordCommand:
    action: str
    symbol: str | None = None
    symbols: tuple[str, ...] = ()
    instrument_id: str | None = None
    venue_id: str | None = None
    tier: str | None = None
    change_id: str | None = None
    status: str | None = None

    @property
    def mutating(self) -> bool:
        return self.action in {"add", "move", "remove", "import_us_large_cap", "confirm"}


def parse_watchlist_command(prompt: str) -> WatchlistDiscordCommand | None:
    try:
        parts = shlex.split(" ".join(prompt.strip().split()))
    except ValueError:
        return None
    if not parts or parts[0].lower() != "watchlist":
        return None
    if len(parts) == 1:
        return WatchlistDiscordCommand(action="list")
    action = parts[1].lower()
    options = _options(parts[2:])
    positional = [item for item in parts[2:] if "=" not in item]
    if action == "list":
        return WatchlistDiscordCommand(action="list", tier=options.get("tier"), venue_id=options.get("venue"), status=options.get("status"))
    if action == "unresolved":
        return WatchlistDiscordCommand(action="unresolved")
    if action == "history":
        return WatchlistDiscordCommand(action="history")
    if action == "add" and positional:
        symbols = tuple(
            symbol
            for item in positional
            for symbol in (part.strip() for part in item.split(","))
            if symbol
        )
        return WatchlistDiscordCommand(
            action="add",
            symbol=symbols[0],
            symbols=symbols,
            venue_id=options.get("venue"),
            tier=options.get("tier", "pinned"),
        )
    if action == "move" and positional:
        return WatchlistDiscordCommand(action="move", instrument_id=positional[0], tier=options.get("tier", "pinned"))
    if action == "remove" and positional:
        return WatchlistDiscordCommand(action="remove", instrument_id=positional[0])
    if action == "confirm" and positional:
        return WatchlistDiscordCommand(action="confirm", change_id=positional[0])
    if action == "import" and positional and positional[0].lower() in {"us-large-cap", "us_large_cap"}:
        return WatchlistDiscordCommand(action="import_us_large_cap")
    return WatchlistDiscordCommand(action="invalid")


async def handle_watchlist_discord_command(
    command: WatchlistDiscordCommand,
    *,
    service: Any,
    repository: Any,
    actor: str,
) -> str:
    await service.seed_if_empty(actor="bootstrap")
    if command.action == "list":
        rows = await service.list(tier=command.tier, venue_id=command.venue_id, status=command.status, limit=100)
        summary = await service.summary()
        return _format_watchlist(rows, summary)
    if command.action == "unresolved":
        return _format_unresolved(await service.unresolved(limit=100))
    if command.action == "history":
        return _format_history(await repository.list_watchlist_change_events(limit=20))
    if command.action == "confirm" and command.change_id:
        result = await service.confirm(command.change_id, actor=actor)
        applied = result.get("result") or {}
        if applied.get("imported_count") is not None:
            return (
                f"Watchlist import `{command.change_id}` applied: `{applied.get('imported_count')}` "
                "official SPY holdings staged in the broad Alpaca universe. Provider verification "
                "is still required before paper tradability."
            )
        return f"Watchlist change `{command.change_id}` applied. Universe snapshot was republished."
    if command.action == "invalid":
        return _usage()
    if command.action == "add" and len(command.symbols) > 1:
        results = []
        for symbol in command.symbols:
            results.append(
                await service.request_change(
                    WatchlistChangeRequest(
                        action="add",
                        symbol=symbol,
                        venue_id=command.venue_id,
                        tier=(command.tier or "pinned"),  # type: ignore[arg-type]
                        actor=actor,
                        reason="discord_admin_bulk",
                    )
                )
            )
        return (
            f"Watchlist updated for `{len(results)}` provider instruments: "
            + ", ".join(f"`{(item.get('instrument') or {}).get('provider_symbol')}`" for item in results)
            + "."
        )
    request = WatchlistChangeRequest(
        action=command.action,  # type: ignore[arg-type]
        symbol=command.symbol,
        venue_id=command.venue_id,
        instrument_id=command.instrument_id,
        tier=(command.tier or "pinned"),  # type: ignore[arg-type]
        actor=actor,
        reason="discord_admin",
    )
    result = await service.request_change(request)
    if result.get("status") == "pending_confirmation":
        return f"Watchlist change staged as `{result.get('change_id')}`. Review it, then run `watchlist confirm {result.get('change_id')}`."
    instrument = result.get("instrument") or {}
    membership = result.get("membership") or {}
    return (
        f"Watchlist updated: `{instrument.get('provider_symbol') or command.instrument_id}` "
        f"status `{instrument.get('tradability_status', 'active')}` tier `{membership.get('tier', command.tier or 'pinned')}`."
    )


def _options(parts: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in parts:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        values[key.lower()] = value
    return values


def _format_watchlist(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "**Canonical watchlist**",
        f"Provider instruments `{summary.get('desired_count', 0)}` | underlyings `{summary.get('underlying_count', 0)}` "
        f"| active `{summary.get('active_count', 0)}` | unavailable `{summary.get('unavailable_count', 0)}`",
    ]
    for item in rows[:40]:
        membership = item.get("membership") or {}
        lines.append(
            f"- `{item.get('instrument_id')}` `{item.get('provider_symbol')}` @ `{item.get('venue_id')}` "
            f"status `{item.get('tradability_status')}` tier `{membership.get('tier')}`"
        )
    if len(rows) > 40:
        lines.append(f"- … {len(rows) - 40} more; use the authenticated `/engine/universe` endpoint for the full list.")
    return "\n".join(lines)


def _format_unresolved(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No desired instruments are currently absent, delisted, or disabled."
    lines = ["**Unresolved/delisted watchlist instruments**"]
    for item in rows:
        lines.append(f"- `{item.get('provider_symbol')}` @ `{item.get('venue_id')}` — `{item.get('tradability_status')}`")
    return "\n".join(lines)


def _format_history(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No watchlist changes have been recorded."
    lines = ["**Watchlist change history**"]
    for item in rows:
        lines.append(f"- `{item.get('change_id')}` `{item.get('action')}` `{item.get('status')}` by `{item.get('actor')}`")
    return "\n".join(lines)


def _usage() -> str:
    return (
        "Watchlist commands: `watchlist list`, `watchlist add <symbol[,symbol...]> venue=<venue> tier=pinned|broad`, "
        "`watchlist move <instrument_id> tier=<tier>`, `watchlist remove <instrument_id>`, "
        "`watchlist unresolved`, `watchlist history`, `watchlist import us-large-cap`, "
        "`watchlist confirm <change_id>`."
    )
