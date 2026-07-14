from __future__ import annotations

import asyncio
import calendar
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from hyperliquid_trading_agent.app.world_model.v2_reducer import SERIES_FACTORS, stable_id
from hyperliquid_trading_agent.app.world_model.v2_schemas import MacroObservationV2

BLS_SERIES = ("CUSR0000SA0", "CUSR0000SA0L1E", "CES0000000001", "LNS14000000")
FRED_SERIES = ("PCEPI", "PCEPILFE", "GDPC1", "INDPRO", "RSAFS", "ICSA", "FEDFUNDS", "DFF", "DTWEXBGS", "WALCL", "RRPONTSYD", "WTREGEN", "NFCI")
FRED_HIGH_FREQUENCY = {"ICSA", "DFF", "DTWEXBGS", "WALCL", "RRPONTSYD", "WTREGEN", "NFCI"}


class OfficialMacroBaseline:
    def __init__(self, *, settings: Any, service: Any):
        self.settings = settings
        self.service = service
        self.last_poll_at_ms: int | None = None
        self.last_error: str | None = None
        self.counts: dict[str, int] = {}

    async def backfill(self) -> dict[str, int]:
        cutover_ms = int(datetime.now(UTC).timestamp() * 1000)
        self.last_error = None
        years = max(5, min(10, int(self.settings.world_model_v2_macro_backfill_years)))
        current_year = datetime.now(UTC).year
        timeout = float(self.settings.world_model_adapter_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            tasks = [self._bls(client, current_year - years + 1, current_year, cutover_ms), self._treasury(client, current_year - 5 + 1, current_year, cutover_ms)]
            if self.settings.world_model_v2_fred_api_key:
                tasks.append(self._fred(client, cutover_ms))
            results = await asyncio.gather(*tasks, return_exceptions=True)
        counts = {"bls": 0, "treasury": 0, "fred": 0, "errors": 0}
        all_observations: list[MacroObservationV2] = []
        for result in results:
            if isinstance(result, Exception):
                counts["errors"] += 1
                self.last_error = type(result).__name__
                continue
            source, observations, source_errors = result
            counts[source] += len(observations)
            counts["errors"] += source_errors
            all_observations.extend(observations)
        if all_observations:
            await self.service.observe_macro_observations(all_observations)
        self.last_poll_at_ms = cutover_ms
        self.counts = counts
        return counts

    async def _bls(self, client: httpx.AsyncClient, start_year: int, end_year: int, available_at_ms: int) -> tuple[str, list[MacroObservationV2], int]:
        response = await client.post("https://api.bls.gov/publicAPI/v2/timeseries/data/", json={"seriesid": list(BLS_SERIES), "startyear": str(start_year), "endyear": str(end_year)})
        response.raise_for_status()
        return "bls", parse_bls_response(response.json(), available_at_ms=available_at_ms), 0

    async def _treasury(self, client: httpx.AsyncClient, start_year: int, end_year: int, available_at_ms: int) -> tuple[str, list[MacroObservationV2], int]:
        observations: list[MacroObservationV2] = []
        errors = 0
        for year in range(start_year, end_year + 1):
            for dataset, factor in (("daily_treasury_yield_curve", "rates"), ("daily_treasury_real_yield_curve", "real_rates")):
                for attempt in range(2):
                    try:
                        response = await client.get("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml", params={"data": dataset, "field_tdr_date_value": year})
                        response.raise_for_status()
                        observations.extend(parse_treasury_xml(response.text, available_at_ms=available_at_ms, factor_id=factor))
                        break
                    except (httpx.HTTPError, ElementTree.ParseError) as exc:
                        if attempt == 0:
                            continue
                        errors += 1
                        self.last_error = type(exc).__name__
        return "treasury", observations, errors

    async def _fred(self, client: httpx.AsyncClient, available_at_ms: int) -> tuple[str, list[MacroObservationV2], int]:
        observations: list[MacroObservationV2] = []
        errors = 0
        for series_id in FRED_SERIES:
            lookback_years = 5 if series_id in FRED_HIGH_FREQUENCY else 10
            observation_start = datetime(datetime.now(UTC).year - lookback_years, 1, 1, tzinfo=UTC).date().isoformat()
            try:
                response = await client.get("https://api.stlouisfed.org/fred/series/observations", params={
                    "series_id": series_id, "api_key": self.settings.world_model_v2_fred_api_key,
                    "file_type": "json", "sort_order": "asc", "limit": 5000, "observation_start": observation_start,
                })
                response.raise_for_status()
                observations.extend(parse_fred_response(series_id, response.json(), available_at_ms=available_at_ms))
            except (httpx.HTTPError, ValueError) as exc:
                errors += 1
                self.last_error = type(exc).__name__
        return "fred", observations, errors

    def status(self) -> dict[str, Any]:
        return {"last_poll_at_ms": self.last_poll_at_ms, "last_error": self.last_error, "counts": self.counts, "fred_enabled": bool(self.settings.world_model_v2_fred_api_key)}


def parse_bls_response(payload: dict[str, Any], *, available_at_ms: int) -> list[MacroObservationV2]:
    out: list[MacroObservationV2] = []
    for series in payload.get("Results", {}).get("series", []):
        series_id = str(series.get("seriesID") or "")
        factor = SERIES_FACTORS.get(series_id)
        if not factor:
            continue
        for row in series.get("data", []):
            period = str(row.get("period") or "")
            if period == "M13":
                continue
            try:
                month = int(period.removeprefix("M"))
                year = int(row["year"])
                value = float(str(row["value"]).replace(",", ""))
            except (KeyError, TypeError, ValueError):
                continue
            event_ms = _date_ms(year, month, calendar.monthrange(year, month)[1])
            period_name = f"{year:04d}-{month:02d}"
            out.append(MacroObservationV2(
                observation_id=stable_id("macro", "bls", series_id, period_name, str(available_at_ms)),
                series_id=series_id, factor_id=factor, period=period_name, value=value,
                units="index_or_level", frequency="monthly", vintage=str(available_at_ms),
                event_at_ms=event_ms, available_at_ms=max(event_ms, available_at_ms), source="bls",
                metadata={"baseline_backfill": True, "footnotes": row.get("footnotes", [])},
            ))
    return out


def parse_fred_response(series_id: str, payload: dict[str, Any], *, available_at_ms: int) -> list[MacroObservationV2]:
    factor = SERIES_FACTORS.get(series_id)
    if not factor:
        return []
    out: list[MacroObservationV2] = []
    for row in payload.get("observations", []):
        try:
            value = float(row["value"])
            event_ms = int(datetime.fromisoformat(str(row["date"])).replace(tzinfo=UTC).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        period = str(row["date"])
        out.append(MacroObservationV2(
            observation_id=stable_id("macro", "fred", series_id, period, str(available_at_ms)), series_id=series_id,
            factor_id=factor, period=period, value=value, units="provider_units", frequency="provider_frequency",
            vintage=str(available_at_ms), event_at_ms=event_ms, available_at_ms=max(event_ms, available_at_ms), source="fred",
            metadata={"baseline_backfill": True, "realtime_start": row.get("realtime_start"), "realtime_end": row.get("realtime_end")},
        ))
    return out


def parse_treasury_xml(xml: str, *, available_at_ms: int, factor_id: str = "rates") -> list[MacroObservationV2]:
    out: list[MacroObservationV2] = []
    root = ElementTree.fromstring(xml)
    fields = {"BC_2YEAR": "DGS2", "BC_5YEAR": "DGS5", "BC_10YEAR": "DGS10", "BC_30YEAR": "DGS30"} if factor_id == "rates" else {"TC_5YEAR": "DFII5", "TC_10YEAR": "DFII10", "TC_30YEAR": "DFII30"}
    for properties in (node for node in root.iter() if node.tag.endswith("properties")):
        values = {node.tag.rsplit("}", 1)[-1]: node.text for node in properties}
        date = values.get("NEW_DATE") or values.get("Date")
        if not date:
            continue
        try:
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            event_ms = int(dt.timestamp() * 1000)
        except ValueError:
            continue
        period = dt.date().isoformat()
        for field, series_id in fields.items():
            try:
                value = float(values[field])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(MacroObservationV2(
                observation_id=stable_id("macro", "treasury", series_id, period, str(available_at_ms)), series_id=series_id,
                factor_id=factor_id, period=period, value=value, units="percent", frequency="daily",
                vintage=str(available_at_ms), event_at_ms=event_ms, available_at_ms=max(event_ms, available_at_ms), source="us_treasury",
                metadata={"baseline_backfill": True, "tenor": field},
            ))
    return out


def _date_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)
