"""Derived-vs-confirmed reconciliation for Hyperliquid.

The public HL feed is ``derived`` (``liquidation_pressure`` inferred from large
book sweeps); a confirmed source (the managed gRPC ``StreamFills`` provider, or a
watched account's exact fills) is the ground truth. This harness buckets both by
``(symbol, time)`` and measures how well the cheap derived heuristic tracks the
truth — match rate, notional delta, and an honest **confirmed coverage** fraction.

It is intentionally usable *before* any confirmed source exists: with only derived
events it reports ``confirmed_coverage = 0.0`` and ``confirmed_source =
"not_configured"`` rather than implying corroboration we don't have. The function
is pure (no DB, no metrics) so the math is unit-tested against replayed frames;
the service feeds it live HL events from the in-memory tape and sets the gauges.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hyperliquid_trading_agent.app.liquidations.models import EventType, LiquidationEvent, SourceIntegrity

# Source-integrity values that count as ground-truth confirmation for HL.
_CONFIRMED_INTEGRITY = frozenset(
    {str(SourceIntegrity.CONFIRMED), str(SourceIntegrity.VENDOR), str(SourceIntegrity.ACCOUNT_PRIVATE)}
)


def _notional(event: LiquidationEvent) -> float:
    return float(event.notional_usd) if event.notional_usd is not None else 0.0


def reconcile(
    events: Iterable[LiquidationEvent],
    *,
    bucket_ms: int,
    window_ms: int,
    now_ms: int,
    confirmed_source: str = "not_configured",
) -> dict[str, Any]:
    """Compare HL derived pressure vs confirmed fills over the trailing window.

    A bucket is ``(symbol, timestamp_ms // bucket_ms)``. ``match_rate`` is the
    fraction of derived buckets that also have a confirmed fill; ``confirmed_only``
    buckets are confirmed liqs the derived heuristic missed (false negatives);
    ``derived_only`` buckets are inferred pressure with no confirmation (possible
    false positives). ``confirmed_coverage`` is the confirmed share of all observed
    HL liquidation notional.
    """
    cutoff = now_ms - window_ms
    derived: dict[tuple[str, int], float] = {}
    confirmed: dict[tuple[str, int], float] = {}
    for event in events:
        if str(event.venue) != "hyperliquid" or event.timestamp_ms < cutoff:
            continue
        key = (event.symbol, event.timestamp_ms // bucket_ms)
        if event.event_type == EventType.LIQUIDATION_PRESSURE:
            derived[key] = derived.get(key, 0.0) + _notional(event)
        elif event.is_execution and str(event.source_integrity) in _CONFIRMED_INTEGRITY:
            confirmed[key] = confirmed.get(key, 0.0) + _notional(event)

    derived_keys, confirmed_keys = set(derived), set(confirmed)
    matched = derived_keys & confirmed_keys
    derived_only = derived_keys - confirmed_keys
    confirmed_only = confirmed_keys - derived_keys

    derived_notional = sum(derived.values())
    confirmed_notional = sum(confirmed.values())
    derived_only_notional = sum(derived[k] for k in derived_only)
    observed_notional = confirmed_notional + derived_only_notional
    coverage = confirmed_notional / observed_notional if observed_notional > 0 else 0.0
    match_rate = len(matched) / len(derived_keys) if derived_keys else 0.0

    symbols = sorted({sym for sym, _ in derived_keys | confirmed_keys})
    by_symbol = {
        sym: {
            "derived_buckets": sum(1 for s, _ in derived_keys if s == sym),
            "confirmed_buckets": sum(1 for s, _ in confirmed_keys if s == sym),
            "matched_buckets": sum(1 for s, _ in matched if s == sym),
            "derived_notional_usd": round(sum(v for (s, _), v in derived.items() if s == sym), 2),
            "confirmed_notional_usd": round(sum(v for (s, _), v in confirmed.items() if s == sym), 2),
        }
        for sym in symbols
    }

    return {
        "as_of_ms": now_ms,
        "window_ms": window_ms,
        "bucket_ms": bucket_ms,
        "confirmed_source": confirmed_source,
        "derived_buckets": len(derived_keys),
        "confirmed_buckets": len(confirmed_keys),
        "matched_buckets": len(matched),
        "derived_only_buckets": len(derived_only),
        "confirmed_only_buckets": len(confirmed_only),
        "match_rate": round(match_rate, 4),
        "derived_notional_usd": round(derived_notional, 2),
        "confirmed_notional_usd": round(confirmed_notional, 2),
        "notional_delta_usd": round(confirmed_notional - derived_notional, 2),
        "confirmed_coverage": round(coverage, 4),
        "by_symbol": by_symbol,
    }
