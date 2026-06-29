"""In-process per-IP token-bucket rate limiting for the public export API.

No external dependency (no Redis/slowapi): a monotonic-clock token bucket keyed
by client IP, with periodic eviction of idle buckets so memory stays bounded.
The limiter is pure and unit-testable; ``build_rate_limit_dependency`` wraps it
as a FastAPI dependency that raises ``429`` with a ``Retry-After`` header.

The layer is intentionally key-agnostic: the public posture is open + IP-limited,
but a future ``X-API-Key`` tier can supply a different key (and per-key limits)
without changing the response contract.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token bucket: refill ``rate_per_min`` tokens/min up to ``burst`` capacity."""

    def __init__(
        self,
        rate_per_min: float,
        burst: int,
        *,
        sweep_interval_s: float = 300.0,
        idle_ttl_s: float = 900.0,
    ) -> None:
        self._rate_per_s = max(rate_per_min, 1e-9) / 60.0
        self._burst = float(max(1, burst))
        self._idle_ttl_s = idle_ttl_s
        self._sweep_interval_s = sweep_interval_s
        self._buckets: dict[str, _Bucket] = {}
        self._last_sweep = 0.0

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, float]:
        """Consume one token for ``key``. Returns ``(allowed, retry_after_s)``."""
        now = time.monotonic() if now is None else now
        self._maybe_sweep(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, updated=now)
            self._buckets[key] = bucket
        else:
            bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated) * self._rate_per_s)
            bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        retry_after = (1.0 - bucket.tokens) / self._rate_per_s if self._rate_per_s > 0 else self._idle_ttl_s
        return False, retry_after

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < self._sweep_interval_s:
            return
        self._last_sweep = now
        stale = [key for key, bucket in self._buckets.items() if now - bucket.updated > self._idle_ttl_s]
        for key in stale:
            del self._buckets[key]


def client_ip(request: Request, *, trust_proxy: bool) -> str:
    """Best-effort client IP; honors the first ``X-Forwarded-For`` hop behind a
    trusted proxy only (otherwise it would be trivially spoofable)."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def build_rate_limit_dependency(
    limiter: RateLimiter,
    *,
    trust_proxy: bool,
    on_rejected: Callable[[], None] | None = None,
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency enforcing ``limiter`` per client IP."""

    async def _enforce(request: Request) -> None:
        allowed, retry_after = limiter.check(client_ip(request, trust_proxy=trust_proxy))
        if not allowed:
            if on_rejected is not None:
                on_rejected()
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )

    return _enforce
