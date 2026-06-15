from __future__ import annotations

from hyperliquid_trading_agent.app.newswire.normalize import now_ms
from hyperliquid_trading_agent.app.newswire.schemas import NewswireEvent, Tradability

_HALT_TTL_MS = 60 * 60 * 1000  # forget a halt after an hour without confirmation
_RESUME_WORDS = ("resume", "resumed", "resumption", "trading resumes", "lifted")


class HaltStateGate:
    """Tracks halted symbols (from the Nasdaq halt feed) and stamps tradability.

    ``allow_auto_trade`` is always ``False`` — the gate only records halt state and that a
    human confirmation is required, consistent with the paper-only posture. The research's
    rule: never act blindly when halt state is ambiguous.
    """

    def __init__(self) -> None:
        self._halted: dict[str, int] = {}  # symbol -> halted_at_ms

    def observe(self, event: NewswireEvent) -> None:
        if event.event_type not in {"halt", "exchange_status"}:
            return
        resumed = any(word in event.headline.lower() for word in _RESUME_WORDS)
        observed_at = now_ms()
        for symbol in event.symbols:
            key = symbol.upper()
            if resumed:
                self._halted.pop(key, None)
            else:
                self._halted[key] = observed_at

    def halted_symbols(self) -> list[str]:
        self._expire()
        return sorted(self._halted.keys())

    def apply(self, event: NewswireEvent) -> NewswireEvent:
        self.observe(event)
        self._expire()
        hit = sorted({s.upper() for s in event.symbols} & set(self._halted.keys()))
        event.tradability = Tradability(
            allow_auto_trade=False,
            requires_confirmation=True,
            halt_state_checked=True,
            halted_symbols=hit,
        )
        return event

    def _expire(self) -> None:
        cutoff = now_ms() - _HALT_TTL_MS
        for symbol in [s for s, ts in self._halted.items() if ts < cutoff]:
            self._halted.pop(symbol, None)
