from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, WatchPriority

log = get_logger(__name__)

_PRIORITY_RANK: dict[WatchPriority, int] = {
    "unwatched": 0,
    "top_volume": 1,
    "active": 2,
    "core": 3,
    "position": 4,
}

_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "xbt"),
    "ETH": ("ethereum", "ether"),
    "HYPE": ("hyperliquid",),
    "SOL": ("solana",),
    "DOGE": ("dogecoin",),
    "XRP": ("ripple",),
    "BNB": ("binance coin",),
    "AVAX": ("avalanche",),
    "LINK": ("chainlink",),
}

_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("monetary_policy", re.compile(r"\b(fomc|rate decision|interest rates?|federal funds|quantitative easing|quantitative tightening)\b", re.I)),
    ("inflation", re.compile(r"\b(cpi|pce|inflation|consumer prices?)\b", re.I)),
    ("employment", re.compile(r"\b(nonfarm|payrolls?|unemployment|jobless claims?)\b", re.I)),
    ("regulation", re.compile(r"\b(sec|cftc|regulat(?:or|ion|ory)|lawsuit|enforcement|sanction)\b", re.I)),
    ("exchange_risk", re.compile(r"\b(exchange|trading halt|halted|outage|withdrawals?|delist(?:ing|ed)?)\b", re.I)),
    ("protocol_security", re.compile(r"\b(hack(?:ed)?|exploit(?:ed)?|bridge attack|smart contract bug|depeg)\b", re.I)),
    ("etf_flows", re.compile(r"\b(etfs?|inflows?|outflows?)\b", re.I)),
    ("liquidations", re.compile(r"\bliquidat(?:e|ed|es|ing|ion|ions)\b", re.I)),
)


@dataclass(frozen=True)
class WatchSetSnapshot:
    generated_at_ms: int
    priorities: dict[str, WatchPriority] = field(default_factory=dict)
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def symbols(self) -> list[str]:
        return sorted(self.priorities)

    def priority_for(self, symbols: list[str]) -> WatchPriority:
        result: WatchPriority = "unwatched"
        for symbol in symbols:
            candidate = self.priorities.get(symbol.upper(), "unwatched")
            if _PRIORITY_RANK[candidate] > _PRIORITY_RANK[result]:
                result = candidate
        return result


@dataclass(frozen=True)
class EntityMatch:
    symbols: list[str]
    reasons: dict[str, list[str]]
    topics: list[str]
    watch_priority: WatchPriority


class DynamicNewswireWatchSet:
    """Repository-backed priority universe with a safe configured fallback."""

    def __init__(self, settings: Settings, repository: Any | None = None):
        self.settings = settings
        self.repository = repository
        self.refresh_ms = max(5, int(getattr(settings, "newswire_watch_refresh_seconds", 30))) * 1000
        self._snapshot = self._fallback_snapshot()
        self._last_refresh_attempt_ms = 0

    @property
    def snapshot(self) -> WatchSetSnapshot:
        return self._snapshot

    async def refresh_if_due(self, *, force: bool = False) -> WatchSetSnapshot:
        now = _now_ms()
        if not force and now - self._last_refresh_attempt_ms < self.refresh_ms:
            return self._snapshot
        self._last_refresh_attempt_ms = now
        priorities = dict(self._fallback_snapshot().priorities)
        warnings: list[str] = []
        repo = self.repository
        if repo is None or not getattr(repo, "enabled", False):
            self._snapshot = self._build_snapshot(priorities, warnings)
            return self._snapshot

        await self._merge_rows(priorities, warnings, "list_position_theses", "position", state="open", limit=1000)
        await self._merge_rows(priorities, warnings, "list_paper_positions", "position", status="open", limit=1000)
        await self._merge_rows(priorities, warnings, "list_equity_paper_positions", "position", status="open", limit=1000)
        await self._merge_rows(priorities, warnings, "list_alpha_candidates", "active", limit=1000)
        await self._merge_rows(priorities, warnings, "list_autonomy_trade_signals", "active", limit=1000)
        top_count = max(0, int(getattr(self.settings, "newswire_top_volume_watch_count", 20)))
        if top_count:
            try:
                method = getattr(repo, "list_latest_feature_assets", None)
                rows = await method(feature_name="day_volume_usd", limit=top_count) if callable(method) else []
                for row in rows:
                    _set_priority(priorities, str(row.get("asset") or ""), "top_volume")
            except Exception as exc:  # pragma: no cover - repository degradation
                warnings.append(f"top_volume:{type(exc).__name__}")

        self._snapshot = self._build_snapshot(priorities, warnings)
        return self._snapshot

    async def _merge_rows(
        self,
        priorities: dict[str, WatchPriority],
        warnings: list[str],
        method_name: str,
        priority: WatchPriority,
        **kwargs: Any,
    ) -> None:
        method = getattr(self.repository, method_name, None)
        if not callable(method):
            return
        try:
            rows = await method(**kwargs)
        except TypeError:
            # Some compatibility repositories expose a narrower list signature.
            try:
                rows = await method(limit=kwargs.get("limit", 1000))
            except Exception as exc:  # pragma: no cover
                warnings.append(f"{method_name}:{type(exc).__name__}")
                return
        except Exception as exc:  # pragma: no cover
            warnings.append(f"{method_name}:{type(exc).__name__}")
            return
        for row in rows or []:
            state = str(row.get("status") or row.get("position_state") or "").lower()
            if priority == "active" and state in {"rejected", "expired", "cancelled", "closed", "executed"}:
                continue
            symbol = str(row.get("asset") or row.get("symbol") or row.get("coin") or "")
            _set_priority(priorities, symbol, priority)

    def _fallback_snapshot(self) -> WatchSetSnapshot:
        priorities: dict[str, WatchPriority] = {}
        for symbol in getattr(self.settings, "newswire_explicit_watch_symbols", []):
            _set_priority(priorities, symbol, "core")
        for symbol in self.settings.autonomy_core_symbols:
            _set_priority(priorities, symbol, "core")
        for symbol in self.settings.newswire_cashtag_list:
            _set_priority(priorities, symbol, "active")
        return self._build_snapshot(priorities, [])

    def _build_snapshot(self, priorities: dict[str, WatchPriority], warnings: list[str]) -> WatchSetSnapshot:
        aliases = {symbol: _ALIASES.get(symbol, ()) for symbol in priorities}
        return WatchSetSnapshot(generated_at_ms=_now_ms(), priorities=priorities, aliases=aliases, warnings=tuple(warnings))

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for priority in self._snapshot.priorities.values():
            counts[priority] = counts.get(priority, 0) + 1
        return {
            "generated_at_ms": self._snapshot.generated_at_ms,
            "symbol_count": len(self._snapshot.priorities),
            "counts": counts,
            "symbols": self._snapshot.symbols,
            "warnings": list(self._snapshot.warnings),
        }


def resolve_entities(event: NewswireEvent, snapshot: WatchSetSnapshot) -> EntityMatch:
    text = " ".join([event.headline, event.body]).strip()
    explicit = {str(symbol).upper().lstrip("$") for symbol in event.symbols if symbol}
    symbols = set(explicit)
    trusted = {str(symbol).upper() for symbol in event.metadata.get("trusted_source_symbols", [])}
    reasons: dict[str, list[str]] = {
        symbol: ["trusted_source_symbol" if symbol in trusted else "provider_symbol"] for symbol in explicit
    }
    for symbol in snapshot.symbols:
        match_reasons = _symbol_match_reasons(text, symbol, snapshot.aliases.get(symbol, ()))
        if match_reasons:
            symbols.add(symbol)
            reasons.setdefault(symbol, []).extend(match_reasons)
    if "hyperliquid" in text.lower() and "HYPE" in snapshot.priorities:
        symbols.add("HYPE")
        reasons.setdefault("HYPE", []).append("entity_alias:hyperliquid")
    ordered = sorted(symbols)
    topics = sorted({topic for topic, pattern in _TOPIC_PATTERNS if pattern.search(text)})
    return EntityMatch(
        symbols=ordered,
        reasons={symbol: sorted(set(values)) for symbol, values in reasons.items()},
        topics=topics,
        watch_priority=snapshot.priority_for(ordered),
    )


def _symbol_match_reasons(text: str, symbol: str, aliases: tuple[str, ...]) -> list[str]:
    reasons: list[str] = []
    if re.search(rf"(?<![A-Za-z0-9])\${re.escape(symbol)}(?![A-Za-z0-9])", text, re.I):
        reasons.append("cashtag")
    # Ambiguous short symbols require uppercase/cashtag/provider evidence. This avoids
    # matching ordinary words such as ONE, MOVE or IN inside prose.
    if len(symbol) >= 4:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", text, re.I):
            reasons.append("ticker")
    elif re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", text):
        reasons.append("uppercase_ticker")
    lowered = text.lower()
    for alias in aliases:
        if re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", lowered):
            reasons.append(f"entity_alias:{alias.lower()}")
    return reasons


def _set_priority(priorities: dict[str, WatchPriority], raw_symbol: str, priority: WatchPriority) -> None:
    symbol = str(raw_symbol or "").upper().lstrip("$").strip()
    if not symbol or len(symbol) > 24 or not re.fullmatch(r"[A-Z0-9._:-]+", symbol):
        return
    current = priorities.get(symbol, "unwatched")
    if _PRIORITY_RANK[priority] > _PRIORITY_RANK[current]:
        priorities[symbol] = priority


def _now_ms() -> int:
    return int(time.time() * 1000)
