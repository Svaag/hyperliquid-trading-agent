from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.markets.holdings_importer import StateStreetSPYHoldingsImporter
from hyperliquid_trading_agent.app.markets.schemas import InstrumentRef, WatchlistChangeRequest, stable_instrument_id


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class InstrumentSeed:
    underlying_id: str
    venue_id: str
    provider_symbol: str
    instrument_type: str
    quote_currency: str = "USD"
    session_timezone: str = "UTC"
    status: str = "active"
    aliases: tuple[str, ...] = ()
    capabilities: dict[str, Any] = field(default_factory=dict)

    def ref(self) -> InstrumentRef:
        return InstrumentRef(
            underlying_id=self.underlying_id,
            venue_id=self.venue_id,
            provider_symbol=self.provider_symbol,
            instrument_type=self.instrument_type,  # type: ignore[arg-type]
            quote_currency=self.quote_currency,
            session_timezone=self.session_timezone,
            tradability_status=self.status,  # type: ignore[arg-type]
            capabilities=self.capabilities,
            metadata={"aliases": list(self.aliases), "seeded": True},
        )


MAIN_CRYPTO_SYMBOLS = ("BTC", "ETH", "HYPE", "SOL", "ZEC", "LIT", "AAVE", "XMR", "AERO")

XYZ_ACTIVE_INDICES = ("SP500", "XYZ100", "JP225", "KR200")
XYZ_ACTIVE_COMMODITIES = ("GOLD", "SILVER", "CL", "BRENTOIL", "NATGAS", "COPPER", "PLATINUM", "PALLADIUM")
XYZ_ACTIVE_FX = ("JPY", "EUR", "GBP")
XYZ_ACTIVE_SYNTHETICS = (
    "SPCX",
    "CRCL",
    "COIN",
    "DELL",
    "ZHIPU",
    "MINIMAX",
    "DRAM",
    "MU",
    "SNDK",
    "SKHY",
    "CBRS",
    "TSM",
    "SOFTBANK",
    "QNT",
    "BOT",
    "IBM",
    "ASML",
    "INTC",
    "AMD",
    "MRVL",
    "MSFT",
    "ORCL",
    "HOOD",
    "CRWV",
    "AVGO",
    "GME",
    "BABA",
    "NFLX",
    "COST",
    "XLE",
)
XYZ_DELISTED = ("KRW", "DXY", "CORN", "WHEAT")
XYZ_ABSENT = ("CRM", "XLF", "DOW_JONES", "HANG_SENG")

# Exchange-listed names receive a second provider-specific identity for hosted
# Alpaca Paper. Private-company, basket, and non-US synthetics remain HIP-3-only.
ALPACA_PAPER_SYMBOLS = (
    "CRCL",
    "COIN",
    "DELL",
    "MU",
    "SNDK",
    "TSM",
    "IBM",
    "ASML",
    "INTC",
    "AMD",
    "MRVL",
    "MSFT",
    "ORCL",
    "HOOD",
    "CRWV",
    "AVGO",
    "GME",
    "BABA",
    "NFLX",
    "CRM",
    "COST",
    "XLE",
    "XLF",
)
ALPACA_ETF_SYMBOLS = {"XLE", "XLF"}
ALPACA_EQUITY_SYMBOLS = set(ALPACA_PAPER_SYMBOLS) - ALPACA_ETF_SYMBOLS

CANONICAL_ALIASES: dict[str, str] = {
    "S&P500": "xyz:SP500",
    "SPX": "xyz:SP500",
    "NASDAQ": "xyz:XYZ100",
    "NASDAQ100": "xyz:XYZ100",
    "NIKKEI": "xyz:JP225",
    "NIKKEI225": "xyz:JP225",
    "KOSPI": "xyz:KR200",
    "WTIOIL": "xyz:CL",
    "USDJPY": "xyz:JPY",
    "EURUSD": "xyz:EUR",
    "GBPUSD": "xyz:GBP",
    "USDKRW": "xyz:KRW",
}


def default_instrument_seeds() -> list[InstrumentSeed]:
    seeds = [
        InstrumentSeed(
            underlying_id=f"CRYPTO:{symbol}",
            venue_id="hyperliquid:main",
            provider_symbol=symbol,
            instrument_type="crypto_perp",
            quote_currency="USDC",
            capabilities={"mark": True, "index": True, "funding": True, "open_interest": True, "l2": True, "paper_simulation": True},
        )
        for symbol in MAIN_CRYPTO_SYMBOLS
    ]
    for symbol in XYZ_ACTIVE_INDICES:
        seeds.append(_xyz_seed(symbol, "index_benchmark"))
    for symbol in XYZ_ACTIVE_COMMODITIES:
        aliases = ("WTIOIL",) if symbol == "CL" else ()
        seeds.append(_xyz_seed(symbol, "commodity_perp", aliases=aliases))
    for symbol in XYZ_ACTIVE_FX:
        aliases = {"JPY": ("USDJPY",), "EUR": ("EURUSD",), "GBP": ("GBPUSD",)}.get(symbol, ())
        seeds.append(_xyz_seed(symbol, "fx_perp", aliases=aliases))
    for symbol in XYZ_ACTIVE_SYNTHETICS:
        seeds.append(_xyz_seed(symbol, "synthetic_perp", session_timezone="America/New_York"))
    for symbol in XYZ_DELISTED:
        instrument_type = "fx_perp" if symbol == "KRW" else "commodity_perp" if symbol in {"CORN", "WHEAT"} else "index_benchmark"
        aliases = ("USDKRW",) if symbol == "KRW" else ()
        seeds.append(_xyz_seed(symbol, instrument_type, status="delisted", aliases=aliases))
    for symbol in XYZ_ABSENT:
        seeds.append(_xyz_seed(symbol, "index_benchmark" if symbol in {"DOW_JONES", "HANG_SENG"} else "synthetic_perp", status="absent"))
    for symbol in ALPACA_PAPER_SYMBOLS:
        instrument_type = "etf" if symbol in ALPACA_ETF_SYMBOLS else "equity"
        underlying_type = "ETF" if symbol in ALPACA_ETF_SYMBOLS else "EQUITY"
        seeds.append(
            InstrumentSeed(
                underlying_id=f"{underlying_type}:{symbol}",
                venue_id="alpaca:paper",
                provider_symbol=symbol,
                instrument_type=instrument_type,
                quote_currency="USD",
                session_timezone="America/New_York",
                capabilities={
                    "quote": True,
                    "bars": True,
                    "paper_execution": True,
                    "hosted_paper_source_of_truth": True,
                },
            )
        )
    return seeds


def _xyz_seed(
    symbol: str,
    instrument_type: str,
    *,
    status: str = "active",
    aliases: tuple[str, ...] = (),
    session_timezone: str = "UTC",
) -> InstrumentSeed:
    provider_symbol = f"xyz:{symbol}"
    if instrument_type == "index_benchmark":
        underlying_group = "INDEX"
    elif instrument_type == "commodity_perp":
        underlying_group = "COMMODITY"
    elif instrument_type == "fx_perp":
        underlying_group = "FX"
    elif symbol in ALPACA_ETF_SYMBOLS:
        underlying_group = "ETF"
    elif symbol in ALPACA_EQUITY_SYMBOLS:
        underlying_group = "EQUITY"
    else:
        underlying_group = "SYNTHETIC"
    return InstrumentSeed(
        underlying_id=f"{underlying_group}:{symbol}",
        venue_id="hyperliquid:xyz",
        provider_symbol=provider_symbol,
        instrument_type=instrument_type,
        quote_currency="USDC",
        session_timezone=session_timezone,
        status=status,
        aliases=aliases,
        capabilities={
            "mark": status != "absent",
            "index": status != "absent",
            "funding": status != "absent",
            "open_interest": status != "absent",
            "l2": status == "active",
            "paper_simulation": status == "active",
            "isolated_margin": True,
        },
    )


class WatchlistService:
    """Persistent, versioned canonical universe used by API and Discord surfaces."""

    def __init__(self, repository: Any, *, holdings_importer: Any | None = None):
        self.repository = repository
        self.holdings_importer = holdings_importer or StateStreetSPYHoldingsImporter()

    async def seed_if_empty(self, *, actor: str = "bootstrap") -> dict[str, Any]:
        existing_memberships = await self.repository.list_watchlist_memberships(limit=20_000)
        membership_ids = {str(item.get("instrument_id") or "") for item in existing_memberships}
        existing_instruments = await self.repository.list_instruments(limit=20_000)
        instrument_ids = {str(item.get("instrument_id") or "") for item in existing_instruments}
        now_ms = _now_ms()
        created = 0
        for seed in default_instrument_seeds():
            ref = seed.ref()
            if ref.instrument_id not in instrument_ids:
                await self.repository.upsert_instrument(ref.model_dump(mode="json"), observed_at_ms=now_ms)
                instrument_ids.add(ref.instrument_id)
            if ref.instrument_id in membership_ids:
                continue
            await self.repository.upsert_watchlist_membership(
                {
                    "membership_id": f"wmem_{ref.instrument_id}",
                    "instrument_id": ref.instrument_id,
                    "tier": "pinned",
                    "desired": True,
                    "enabled": ref.tradability_status == "active",
                    "source": "bootstrap_seed",
                    "created_by": actor,
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                    "metadata": {"initial_status": ref.tradability_status},
                }
            )
            membership_ids.add(ref.instrument_id)
            created += 1
        if created or not await self.repository.latest_universe_snapshot():
            await self._publish_snapshot(actor=actor, reason="initial_seed" if not existing_memberships else "seed_repair")
        return await self.summary()

    async def summary(self) -> dict[str, Any]:
        instruments = await self.repository.list_instruments(limit=10_000)
        memberships = await self.repository.list_watchlist_memberships(limit=10_000)
        by_id = {item["instrument_id"]: item for item in instruments}
        desired = [by_id[item["instrument_id"]] for item in memberships if item.get("desired") and item.get("instrument_id") in by_id]
        status_counts = Counter(str(item.get("tradability_status") or "unknown") for item in desired)
        venue_counts = Counter(str(item.get("venue_id") or "unknown") for item in desired)
        tier_counts = Counter(str(item.get("tier") or "unknown") for item in memberships if item.get("desired"))
        latest = await self.repository.latest_universe_snapshot()
        return {
            "desired_count": len(desired),
            "underlying_count": len({str(item.get("underlying_id") or "") for item in desired}),
            "active_count": status_counts.get("active", 0),
            "unavailable_count": sum(status_counts.get(key, 0) for key in ("delisted", "absent", "disabled")),
            "status_counts": dict(status_counts),
            "venue_counts": dict(venue_counts),
            "tier_counts": dict(tier_counts),
            "latest_snapshot": latest,
        }

    async def list(self, *, tier: str | None = None, venue_id: str | None = None, status: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        memberships = await self.repository.list_watchlist_memberships(tier=tier, limit=limit)
        instruments = await self.repository.list_instruments(venue_id=venue_id, tradability_status=status, limit=10_000)
        by_id = {item["instrument_id"]: item for item in instruments}
        return [
            {**by_id[item["instrument_id"]], "membership": item}
            for item in memberships
            if item.get("instrument_id") in by_id and item.get("desired")
        ]

    async def unresolved(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        memberships = await self.repository.list_watchlist_memberships(limit=limit)
        wanted = {item["instrument_id"]: item for item in memberships if item.get("desired")}
        instruments = await self.repository.list_instruments(limit=10_000)
        return [
            {**item, "membership": wanted[item["instrument_id"]]}
            for item in instruments
            if item.get("instrument_id") in wanted and item.get("tradability_status") in {"absent", "delisted", "disabled"}
        ]

    async def publish_if_changed(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Publish one atomic version only when desired/active membership changed."""

        memberships = await self.repository.list_watchlist_memberships(limit=20_000)
        desired_ids = sorted(
            {str(item.get("instrument_id")) for item in memberships if item.get("desired")}
        )
        active_ids = sorted(
            {
                str(item.get("instrument_id"))
                for item in memberships
                if item.get("desired") and item.get("enabled")
            }
        )
        latest = await self.repository.latest_universe_snapshot()
        if latest is not None:
            latest_desired = sorted(latest.get("desired_instrument_ids") or [])
            latest_active = sorted(latest.get("active_instrument_ids") or [])
            if desired_ids == latest_desired and active_ids == latest_active:
                return latest
        return await self._publish_snapshot(actor=actor, reason=reason)

    async def request_change(self, request: WatchlistChangeRequest) -> dict[str, Any]:
        if request.action in {"remove", "import_us_large_cap"}:
            return await self._stage_change(request)
        if request.action == "move":
            if not request.instrument_id:
                raise ValueError("instrument_id is required for move")
            membership = await self.repository.get_watchlist_membership_by_instrument(request.instrument_id)
            if membership is None:
                raise KeyError("watchlist membership not found")
            before = dict(membership)
            membership.update({"tier": request.tier, "updated_at_ms": _now_ms()})
            await self.repository.upsert_watchlist_membership(membership)
            await self._record_applied_change(request, before=before, after=membership)
            await self._publish_snapshot(actor=request.actor, reason="move")
            return {"status": "applied", "membership": membership}
        return await self._add(request)

    async def confirm(self, change_id: str, *, actor: str) -> dict[str, Any]:
        change = await self.repository.get_watchlist_change_event(change_id)
        if change is None:
            raise KeyError("watchlist change not found")
        if change.get("status") != "pending_confirmation":
            raise ValueError(f"watchlist change status is {change.get('status')}")
        payload = dict(change.get("request") or {})
        action = str(change.get("action") or "")
        if action == "remove":
            instrument_id = str(payload.get("instrument_id") or "")
            membership = await self.repository.get_watchlist_membership_by_instrument(instrument_id)
            if membership is None:
                raise KeyError("watchlist membership not found")
            membership.update({"desired": False, "enabled": False, "updated_at_ms": _now_ms()})
            await self.repository.upsert_watchlist_membership(membership)
            result = {"removed_instrument_id": instrument_id}
        elif action == "import_us_large_cap":
            imported = await self.holdings_importer.fetch()
            added = 0
            retained = 0
            now_ms = _now_ms()
            for symbol in imported.symbols:
                ref = InstrumentRef(
                    underlying_id=f"EQUITY:{symbol}",
                    venue_id="alpaca:paper",
                    provider_symbol=symbol,
                    instrument_type="equity",
                    quote_currency="USD",
                    session_timezone="America/New_York",
                    tradability_status="data_only",
                    capabilities={
                        "requires_provider_verification": True,
                        "holdings_membership": True,
                    },
                    metadata={
                        "holdings_source": imported.source_name,
                        "holdings_source_url": imported.source_url,
                        "holdings_content_sha256": imported.content_sha256,
                    },
                )
                existing_instrument = await self.repository.get_instrument(ref.instrument_id)
                if existing_instrument is not None:
                    retained += 1
                    ref = InstrumentRef.model_validate(
                        {
                            **ref.model_dump(mode="json"),
                            **existing_instrument,
                            "capabilities": {
                                **dict(existing_instrument.get("capabilities") or {}),
                                **ref.capabilities,
                            },
                            "metadata": {
                                **dict(existing_instrument.get("metadata") or {}),
                                **ref.metadata,
                            },
                        }
                    )
                else:
                    added += 1
                await self.repository.upsert_instrument(ref.model_dump(mode="json"), observed_at_ms=now_ms)
                existing_membership = await self.repository.get_watchlist_membership_by_instrument(ref.instrument_id)
                membership = existing_membership or {
                    "membership_id": f"wmem_{ref.instrument_id}",
                    "instrument_id": ref.instrument_id,
                    "tier": "broad",
                    "source": "official_spy_holdings",
                    "created_by": actor,
                    "created_at_ms": now_ms,
                    "metadata": {},
                }
                membership.update(
                    {
                        "desired": True,
                        "enabled": ref.tradability_status == "active",
                        "updated_at_ms": now_ms,
                        "metadata": {
                            **dict(membership.get("metadata") or {}),
                            "holdings_content_sha256": imported.content_sha256,
                            "holdings_source_url": imported.source_url,
                        },
                    }
                )
                await self.repository.upsert_watchlist_membership(membership)
            result = {
                "imported_count": len(imported.symbols),
                "added_instrument_count": added,
                "retained_instrument_count": retained,
                "source": imported.source_name,
                "source_url": imported.source_url,
                "content_sha256": imported.content_sha256,
                "fetched_at_ms": imported.fetched_at_ms,
                "paper_tradability_requires_provider_verification": True,
            }
        else:
            raise ValueError(f"unsupported staged action: {action}")
        await self.repository.update_watchlist_change_event(change_id, status="applied", confirmed_by=actor, confirmed_at_ms=_now_ms(), result=result)
        await self._publish_snapshot(actor=actor, reason=action)
        return {"status": "applied", "change_id": change_id, "result": result}

    async def _add(self, request: WatchlistChangeRequest) -> dict[str, Any]:
        if not request.symbol:
            raise ValueError("symbol is required for add")
        ref = resolve_requested_instrument(request.symbol, venue_id=request.venue_id)
        now_ms = _now_ms()
        existing_ref = await self.repository.get_instrument(ref.instrument_id)
        if existing_ref is not None:
            ref = InstrumentRef.model_validate(
                {
                    **ref.model_dump(mode="json"),
                    **existing_ref,
                    "metadata": {
                        **dict(existing_ref.get("metadata") or {}),
                        **ref.metadata,
                    },
                }
            )
        await self.repository.upsert_instrument(ref.model_dump(mode="json"), observed_at_ms=now_ms)
        membership = {
            "membership_id": f"wmem_{ref.instrument_id}",
            "instrument_id": ref.instrument_id,
            "tier": request.tier,
            "desired": True,
            "enabled": ref.tradability_status == "active",
            "source": "admin",
            "created_by": request.actor,
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "metadata": {"reason": request.reason},
        }
        await self.repository.upsert_watchlist_membership(membership)
        await self._record_applied_change(request, before={}, after=membership)
        await self._publish_snapshot(actor=request.actor, reason="add")
        return {"status": "applied", "instrument": ref.model_dump(mode="json"), "membership": membership}

    async def _stage_change(self, request: WatchlistChangeRequest) -> dict[str, Any]:
        if request.action == "remove" and not request.instrument_id:
            raise ValueError("instrument_id is required for remove")
        change_id = f"wchg_{uuid4().hex}"
        event = {
            "change_id": change_id,
            "action": request.action,
            "status": "pending_confirmation",
            "actor": request.actor,
            "request": request.model_dump(mode="json"),
            "before": {},
            "after": {},
            "result": {},
            "created_at_ms": _now_ms(),
            "metadata": {"reason": request.reason},
        }
        await self.repository.record_watchlist_change_event(event)
        return {"status": "pending_confirmation", "change_id": change_id, "preview": event}

    async def _record_applied_change(self, request: WatchlistChangeRequest, *, before: dict[str, Any], after: dict[str, Any]) -> None:
        await self.repository.record_watchlist_change_event(
            {
                "change_id": f"wchg_{uuid4().hex}",
                "action": request.action,
                "status": "applied",
                "actor": request.actor,
                "request": request.model_dump(mode="json"),
                "before": before,
                "after": after,
                "result": {},
                "created_at_ms": _now_ms(),
                "confirmed_by": request.actor,
                "confirmed_at_ms": _now_ms(),
                "metadata": {"reason": request.reason},
            }
        )

    async def _publish_snapshot(self, *, actor: str, reason: str) -> dict[str, Any]:
        memberships = await self.repository.list_watchlist_memberships(limit=20_000)
        desired_ids = sorted({str(item.get("instrument_id")) for item in memberships if item.get("desired")})
        active_ids = sorted({str(item.get("instrument_id")) for item in memberships if item.get("desired") and item.get("enabled")})
        now_ms = _now_ms()
        snapshot = {
            "snapshot_id": f"univ_{now_ms}_{uuid4().hex[:8]}",
            "version": now_ms,
            "desired_instrument_ids": desired_ids,
            "active_instrument_ids": active_ids,
            "created_at_ms": now_ms,
            "created_by": actor,
            "reason": reason,
            "metadata": {"atomic": True},
        }
        await self.repository.record_universe_snapshot(snapshot)
        return snapshot


def resolve_requested_instrument(symbol: str, *, venue_id: str | None = None) -> InstrumentRef:
    raw = symbol.strip()
    if not raw:
        raise ValueError("symbol is required")
    upper = raw.upper()
    canonical = CANONICAL_ALIASES.get(upper, raw)
    seeds = default_instrument_seeds()
    for seed in seeds:
        ref = seed.ref()
        candidates = {ref.provider_symbol.upper(), ref.display_symbol.upper(), *(alias.upper() for alias in seed.aliases)}
        if canonical.upper() in candidates and (venue_id is None or ref.venue_id == venue_id):
            return ref
    if venue_id is None:
        raise ValueError("venue_id is required for an unknown or ambiguous symbol")
    provider_symbol = canonical if ":" in canonical or not venue_id.startswith("hyperliquid:") else f"{venue_id.split(':', 1)[1]}:{canonical}"
    # Unknown symbols remain disabled until a provider metadata refresh proves
    # that the exact provider instrument exists and is tradable.
    status = "absent"
    instrument_type = "equity" if venue_id == "alpaca:paper" else "unknown"
    return InstrumentRef(
        instrument_id=stable_instrument_id(venue_id, provider_symbol),
        underlying_id=f"UNKNOWN:{upper}",
        venue_id=venue_id,
        provider_symbol=provider_symbol,
        instrument_type=instrument_type,
        tradability_status=status,
        capabilities={"paper_execution": False},
        metadata={"admin_requested_symbol": raw, "requires_provider_resolution": True},
    )
