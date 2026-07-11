from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.markets.lighter_adapter import LighterSDKMarketDataAdapter
from hyperliquid_trading_agent.app.markets.schemas import CrossVenueFeatureSnapshot, InstrumentRef, VenueMarketSnapshot
from hyperliquid_trading_agent.app.markets.universe import WatchlistService
from hyperliquid_trading_agent.app.tradfi.alpaca_paper_execution import AlpacaPaperExecutionAdapter


def _now_ms() -> int:
    return int(time.time() * 1000)


class MarketUniverseSyncService:
    """Refresh provider capabilities and batch market snapshots for the watchlist."""

    def __init__(
        self,
        *,
        settings: Any,
        repository: Any,
        hyperliquid: Any,
        lighter_adapter: LighterSDKMarketDataAdapter | None = None,
        alpaca_adapter: AlpacaPaperExecutionAdapter | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self.watchlist = WatchlistService(repository)
        self.lighter = lighter_adapter
        self.alpaca = alpaca_adapter
        self.last_run_at_ms: int | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None
        self._depth_rotation_cursor = 0

    async def close(self) -> None:
        if self.lighter is not None:
            await self.lighter.close()

    async def sync_once(self) -> dict[str, Any]:
        started = _now_ms()
        try:
            await self.watchlist.seed_if_empty(actor="market_universe_sync")
            desired = await self.watchlist.list(limit=20_000)
            hyperliquid_rows = [item for item in desired if str(item.get("venue_id") or "").startswith("hyperliquid:")]
            async def no_snapshots() -> list[VenueMarketSnapshot]:
                return []

            provider_results = await asyncio.gather(
                self._sync_hyperliquid(hyperliquid_rows),
                self._sync_lighter(desired) if self.lighter is not None else no_snapshots(),
                self._sync_alpaca(desired) if self.alpaca is not None else no_snapshots(),
                return_exceptions=True,
            )
            provider_names = ("hyperliquid", "lighter", "alpaca_paper")
            provider_errors = {
                name: type(result).__name__
                for name, result in zip(provider_names, provider_results, strict=True)
                if isinstance(result, BaseException)
            }
            snapshots, lighter_snapshots, alpaca_snapshots = (
                result if isinstance(result, list) else [] for result in provider_results
            )
            universe_snapshot = await self.watchlist.publish_if_changed(
                actor="market_universe_sync",
                reason="provider_metadata_refresh",
            )
            cross_count = await self._record_cross_venue([*snapshots, *lighter_snapshots, *alpaca_snapshots])
            result = {
                "desired_count": len(desired),
                "hyperliquid_snapshot_count": len(snapshots),
                "lighter_snapshot_count": len(lighter_snapshots),
                "alpaca_snapshot_count": len(alpaca_snapshots),
                "cross_venue_snapshot_count": cross_count,
                "provider_errors": provider_errors,
                "universe_version": universe_snapshot.get("version"),
                "duration_ms": _now_ms() - started,
            }
            self.last_run_at_ms = _now_ms()
            self.last_error = None
            self.last_result = result
            return result
        except Exception as exc:
            self.last_run_at_ms = _now_ms()
            self.last_error = type(exc).__name__
            raise

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.settings, "market_universe_enabled", True)),
            "last_run_at_ms": self.last_run_at_ms,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "lighter_enabled": self.lighter is not None,
            "lighter_read_only": True,
            "alpaca_paper_enabled": self.alpaca is not None,
            "alpaca_live_capable": False,
        }

    async def _sync_hyperliquid(self, rows: list[dict[str, Any]]) -> list[VenueMarketSnapshot]:
        depth_instrument_ids = self._rotating_depth_instrument_ids(rows)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("venue_id") or "hyperliquid:main")].append(row)
        snapshots: list[VenueMarketSnapshot] = []
        for venue_id, instruments in grouped.items():
            dex = "" if venue_id == "hyperliquid:main" else venue_id.split(":", 1)[1]
            try:
                raw = await self.hyperliquid.meta_and_asset_ctxs(dex=dex)
            except Exception:
                continue
            universe, contexts = _split_meta_and_contexts(raw)
            provider_rows: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            for meta, ctx in zip(universe, contexts, strict=False):
                name = str(meta.get("name") or ctx.get("coin") or "")
                if name:
                    provider_rows[name.lower()] = (meta, ctx)
            depth_by_instrument = await self._fetch_hyperliquid_depth(
                [
                    item
                    for item in instruments
                    if item.get("instrument_id") in depth_instrument_ids
                    and str(item.get("provider_symbol") or "").lower() in provider_rows
                ]
            )
            observed_at_ms = _now_ms()
            for item in instruments:
                provider_symbol = str(item.get("provider_symbol") or "")
                pair = provider_rows.get(provider_symbol.lower())
                if pair is None:
                    status = "absent"
                    meta: dict[str, Any] = {}
                    ctx: dict[str, Any] = {}
                else:
                    meta, ctx = pair
                    status = "delisted" if bool(meta.get("isDelisted")) else "active"
                ref = InstrumentRef(
                    instrument_id=str(item["instrument_id"]),
                    underlying_id=str(item.get("underlying_id") or "UNKNOWN"),
                    venue_id=venue_id,
                    provider_symbol=provider_symbol,
                    instrument_type=str(item.get("instrument_type") or "unknown"),  # type: ignore[arg-type]
                    quote_currency=str(item.get("quote_currency") or "USDC"),
                    session_timezone=str(item.get("session_timezone") or "UTC"),
                    tradability_status=status,  # type: ignore[arg-type]
                    capabilities={
                        **dict(item.get("capabilities") or {}),
                        "mark": pair is not None,
                        "index": pair is not None,
                        "funding": pair is not None,
                        "open_interest": pair is not None,
                        "l2": status == "active",
                        "paper_simulation": status == "active",
                        "max_leverage": meta.get("maxLeverage"),
                        "only_isolated": bool(meta.get("onlyIsolated")),
                    },
                    mapping_version=int(item.get("mapping_version") or 1),
                    display_symbol=str(item.get("display_symbol") or provider_symbol.split(":", 1)[-1]),
                    metadata={**dict(item.get("metadata") or {}), "provider_meta": meta},
                )
                await self.repository.upsert_instrument(ref.model_dump(mode="json"), observed_at_ms=observed_at_ms)
                membership = dict(item.get("membership") or {})
                if membership:
                    membership.update({"enabled": status == "active", "updated_at_ms": observed_at_ms})
                    await self.repository.upsert_watchlist_membership(membership)
                if status != "active":
                    continue
                depth = depth_by_instrument.get(ref.instrument_id) or {}
                bids = depth.get("bids") if isinstance(depth.get("bids"), list) else []
                asks = depth.get("asks") if isinstance(depth.get("asks"), list) else []
                bid = _float((bids[0] if bids else {}).get("px"))
                ask = _float((asks[0] if asks else {}).get("px"))
                quoted_mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
                snapshot = VenueMarketSnapshot(
                    snapshot_id=f"vms_{venue_id.replace(':', '_')}_{ref.instrument_id}_{observed_at_ms}_{uuid4().hex[:6]}",
                    instrument_id=ref.instrument_id,
                    underlying_id=ref.underlying_id,
                    venue_id=ref.venue_id,
                    provider_symbol=ref.provider_symbol,
                    bid_px=bid,
                    ask_px=ask,
                    mark_px=_float(ctx.get("markPx")),
                    index_px=_float(ctx.get("oraclePx")),
                    mid_px=quoted_mid or _float(ctx.get("midPx")) or _float(ctx.get("markPx")),
                    volume_24h=_float(ctx.get("dayNtlVlm")),
                    open_interest=_float(ctx.get("openInterest")),
                    funding_rate=_float(ctx.get("funding")),
                    depth_bands={"bids": bids, "asks": asks},
                    received_ts_ms=observed_at_ms,
                    source_integrity="confirmed",
                    metadata={"provider": "hyperliquid", "dex": dex or "main", "read_only": True},
                )
                await self.repository.record_venue_market_snapshot(snapshot.model_dump(mode="json"))
                snapshots.append(snapshot)
        return snapshots

    def _rotating_depth_instrument_ids(self, rows: list[dict[str, Any]]) -> set[str]:
        active = sorted(
            (
                item
                for item in rows
                if (item.get("membership") or {}).get("desired")
                and item.get("tradability_status") == "active"
            ),
            key=lambda item: (
                0 if (item.get("membership") or {}).get("tier") == "pinned" else 1,
                str(item.get("instrument_id") or ""),
            ),
        )
        if not active:
            return set()
        capacity = min(
            len(active),
            max(1, int(getattr(self.settings, "market_universe_deep_scan_capacity", 40))),
        )
        start = self._depth_rotation_cursor % len(active)
        selected = [active[(start + offset) % len(active)] for offset in range(capacity)]
        self._depth_rotation_cursor = (start + capacity) % len(active)
        return {str(item.get("instrument_id") or "") for item in selected}

    async def _fetch_hyperliquid_depth(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, list[dict[str, float]]]]:
        semaphore = asyncio.Semaphore(6)

        async def fetch(item: dict[str, Any]) -> tuple[str, dict[str, list[dict[str, float]]]] | None:
            try:
                async with semaphore:
                    raw = await self.hyperliquid.l2_book(str(item.get("provider_symbol") or ""))
            except Exception:
                return None
            depth = _normalized_depth(raw)
            return (str(item.get("instrument_id") or ""), depth) if depth else None

        fetched = await asyncio.gather(*(fetch(item) for item in rows))
        return {item[0]: item[1] for item in fetched if item is not None}

    async def _sync_alpaca(self, desired: list[dict[str, Any]]) -> list[VenueMarketSnapshot]:
        assert self.alpaca is not None
        rows = [item for item in desired if item.get("venue_id") == "alpaca:paper"]
        refs = [InstrumentRef.model_validate(item) for item in rows]
        resolved = await self.alpaca.refresh_instruments(
            refs,
            cache_seconds=int(getattr(self.settings, "market_universe_metadata_refresh_seconds", 900)),
        )
        now_ms = _now_ms()
        memberships = {
            str(item.get("instrument_id")): dict(item.get("membership") or {})
            for item in rows
        }
        for ref in resolved:
            await self.repository.upsert_instrument(ref.model_dump(mode="json"), observed_at_ms=now_ms)
            membership = memberships.get(ref.instrument_id) or {}
            if membership:
                membership.update(
                    {
                        "enabled": ref.tradability_status == "active",
                        "updated_at_ms": now_ms,
                    }
                )
                await self.repository.upsert_watchlist_membership(membership)
        snapshots = await self.alpaca.market_snapshots(resolved)
        for snapshot in snapshots:
            await self.repository.record_venue_market_snapshot(snapshot.model_dump(mode="json"))
        return snapshots

    async def _sync_lighter(self, desired: list[dict[str, Any]]) -> list[VenueMarketSnapshot]:
        assert self.lighter is not None
        refs = await self.lighter.list_instruments()
        desired_underlyings = {
            str(item.get("underlying_id"))
            for item in desired
            if item.get("venue_id") == "hyperliquid:main" and item.get("tradability_status") == "active"
        }
        now_ms = _now_ms()
        selected: list[InstrumentRef] = []
        for ref in refs:
            await self.repository.upsert_instrument(ref.model_dump(mode="json"), observed_at_ms=now_ms)
            if ref.underlying_id not in desired_underlyings or ref.tradability_status != "active":
                continue
            existing = await self.repository.get_watchlist_membership_by_instrument(ref.instrument_id)
            if existing is not None and not existing.get("desired"):
                continue
            membership = existing or {
                    "membership_id": f"wmem_{ref.instrument_id}",
                    "instrument_id": ref.instrument_id,
                    "tier": "pinned",
                    "desired": True,
                    "enabled": True,
                    "source": "cross_venue_auto",
                    "created_by": "market_universe_sync",
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                    "metadata": {"read_only": True, "alternative_to": "hyperliquid:main"},
                }
            membership.update({"enabled": True, "updated_at_ms": now_ms})
            await self.repository.upsert_watchlist_membership(membership)
            selected.append(ref)
        semaphore = asyncio.Semaphore(4)

        async def fetch_snapshot(ref: InstrumentRef) -> VenueMarketSnapshot | None:
            try:
                async with semaphore:
                    return await self.lighter.snapshot(ref)
            except Exception:
                return None

        selected = selected[: max(1, int(getattr(self.settings, "market_universe_deep_scan_capacity", 40)))]
        fetched = await asyncio.gather(*(fetch_snapshot(ref) for ref in selected))
        snapshots: list[VenueMarketSnapshot] = []
        for snapshot in fetched:
            if snapshot is None:
                continue
            await self.repository.record_venue_market_snapshot(snapshot.model_dump(mode="json"))
            snapshots.append(snapshot)
        return snapshots

    async def _record_cross_venue(self, snapshots: list[VenueMarketSnapshot]) -> int:
        grouped: dict[str, list[VenueMarketSnapshot]] = defaultdict(list)
        for snapshot in snapshots:
            if snapshot.mid_px and snapshot.mid_px > 0:
                grouped[snapshot.underlying_id].append(snapshot)
        count = 0
        for underlying_id, rows in grouped.items():
            home = next((item for item in rows if item.venue_id == "hyperliquid:main"), None)
            home = home or next((item for item in rows if item.venue_id == "hyperliquid:xyz"), None)
            if home is None:
                continue
            for comparison in rows:
                if comparison.instrument_id == home.instrument_id:
                    continue
                skew = abs(home.received_ts_ms - comparison.received_ts_ms)
                max_skew = int(
                    getattr(self.settings, "market_universe_max_cross_venue_clock_skew_ms", 15_000)
                )
                quality_flags = [] if skew <= max_skew else ["clock_skew_exceeded"]
                if any((item.staleness_ms or 0) > 60_000 for item in (home, comparison)):
                    quality_flags.append("stale_provider_quote")
                price_delta = ((comparison.mid_px / home.mid_px) - 1.0) * 10_000.0 if home.mid_px and comparison.mid_px else None
                volume_imbalance = None
                if home.volume_24h is not None and comparison.volume_24h is not None:
                    total_volume = home.volume_24h + comparison.volume_24h
                    if total_volume > 0:
                        volume_imbalance = (comparison.volume_24h - home.volume_24h) / total_volume
                snapshot = CrossVenueFeatureSnapshot(
                    snapshot_id=f"cvfs_{home.instrument_id}_{comparison.instrument_id}_{max(home.received_ts_ms, comparison.received_ts_ms)}",
                    underlying_id=underlying_id,
                    reference_instrument_id=home.instrument_id,
                    comparison_instrument_id=comparison.instrument_id,
                    reference_venue_id=home.venue_id,
                    comparison_venue_id=comparison.venue_id,
                    as_of_ms=max(home.received_ts_ms, comparison.received_ts_ms),
                    price_delta_bps=price_delta,
                    volume_imbalance=volume_imbalance,
                    max_clock_skew_ms=skew,
                    quality_flags=quality_flags,
                    metadata={
                        "reference_snapshot_id": home.snapshot_id,
                        "comparison_snapshot_id": comparison.snapshot_id,
                        "pairwise_not_averaged": True,
                        "reference_mid": home.mid_px,
                        "comparison_mid": comparison.mid_px,
                        "reference_volume_24h": home.volume_24h,
                        "comparison_volume_24h": comparison.volume_24h,
                        "reference_provider_symbol": home.provider_symbol,
                        "comparison_provider_symbol": comparison.provider_symbol,
                    },
                )
                await self.repository.record_cross_venue_feature_snapshot(snapshot.model_dump(mode="json"))
                count += 1
        return count


def _split_meta_and_contexts(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) < 2 or not isinstance(value[0], dict) or not isinstance(value[1], list):
        return [], []
    universe = value[0].get("universe") if isinstance(value[0].get("universe"), list) else []
    return [item for item in universe if isinstance(item, dict)], [item for item in value[1] if isinstance(item, dict)]


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _normalized_depth(raw: Any) -> dict[str, list[dict[str, float]]]:
    levels = raw.get("levels") if isinstance(raw, dict) else raw
    if not isinstance(levels, (list, tuple)) or len(levels) < 2:
        return {}

    def side(values: Any) -> list[dict[str, float]]:
        output: list[dict[str, float]] = []
        for level in values if isinstance(values, (list, tuple)) else []:
            if isinstance(level, dict):
                px = _float(level.get("px") or level.get("price"))
                size = _float(level.get("sz") or level.get("size"))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                px, size = _float(level[0]), _float(level[1])
            else:
                continue
            if px is not None and size is not None and px > 0 and size > 0:
                output.append({"px": px, "size": size})
        return output

    bids = side(levels[0])
    asks = side(levels[1])
    return {"bids": bids, "asks": asks} if bids or asks else {}


async def run_market_universe_sync_loop(service: MarketUniverseSyncService, stop_event: asyncio.Event, *, interval_seconds: int) -> None:
    interval = max(15, int(interval_seconds))
    while not stop_event.is_set():
        try:
            await service.sync_once()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
