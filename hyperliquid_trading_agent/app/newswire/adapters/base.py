from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from hyperliquid_trading_agent.app.newswire.schemas import RawNewsItem

# Returns the normalized event (or None if dropped); adapters await it and ignore the result.
RawEmit = Callable[[RawNewsItem], Awaitable[Any]]


class NewswireAdapter(ABC):
    """Long-lived ingest adapter.

    ``run`` is supervised by the service: it should loop until cancelled, calling
    ``await emit(item)`` for each raw item. Transient failures should raise (the service
    restarts with backoff) rather than being swallowed, so reconnects are observable.
    """

    name: str = "adapter"

    @abstractmethod
    async def run(self, emit: RawEmit) -> None: ...

    async def stop(self) -> None:  # pragma: no cover - default no-op
        return None

    def status(self) -> dict[str, Any]:
        return {"name": self.name}
