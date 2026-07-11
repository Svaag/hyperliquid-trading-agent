from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

import httpx

SPY_HOLDINGS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-spy.xlsx"
)
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


@dataclass(frozen=True, slots=True)
class HoldingsImport:
    symbols: list[str]
    source_url: str
    fetched_at_ms: int
    content_sha256: str
    source_name: str = "State Street SPY daily holdings"


class StateStreetSPYHoldingsImporter:
    """Fetch the issuer's complete daily SPY holdings workbook.

    SPY is used as the broad US large-cap spine. Provider verification is a
    separate step, so a holding never becomes paper-tradable merely because it
    appeared in the workbook.
    """

    def __init__(
        self,
        *,
        url: str = SPY_HOLDINGS_URL,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.client = client

    async def fetch(self) -> HoldingsImport:
        if self.client is None:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "hyperliquid-trading-agent/0.1 watchlist-import",
                    "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                },
            ) as client:
                response = await client.get(self.url)
        else:
            response = await self.client.get(self.url)
        response.raise_for_status()
        if len(response.content) > 25 * 1024 * 1024:
            raise ValueError("SPY holdings workbook exceeds the 25 MiB safety limit")
        symbols = parse_holdings_xlsx(response.content)
        if len(symbols) < 100:
            raise ValueError(f"SPY holdings import failed quality gate: only {len(symbols)} ticker rows")
        return HoldingsImport(
            symbols=symbols,
            source_url=self.url,
            fetched_at_ms=int(time.time() * 1000),
            content_sha256=hashlib.sha256(response.content).hexdigest(),
        )


def parse_holdings_xlsx(content: bytes) -> list[str]:
    """Parse ticker cells from a simple XLSX without optional Excel packages."""

    try:
        workbook = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("holdings response is not a valid XLSX workbook") from exc
    with workbook:
        members = workbook.infolist()
        if len(members) > 2_000 or sum(item.file_size for item in members) > 100 * 1024 * 1024:
            raise ValueError("holdings workbook exceeds expanded XLSX safety limits")
        shared = _shared_strings(workbook)
        sheet_names = sorted(
            name
            for name in workbook.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        if not sheet_names:
            raise ValueError("holdings workbook has no worksheets")
        rows = _worksheet_rows(workbook.read(sheet_names[0]), shared)
    header_row_index = -1
    ticker_column = -1
    for row_index, row in enumerate(rows):
        for column, value in row.items():
            normalized = " ".join(value.strip().lower().replace("_", " ").split())
            if normalized in {"ticker", "ticker symbol", "symbol"}:
                header_row_index = row_index
                ticker_column = column
                break
        if ticker_column >= 0:
            break
    if ticker_column < 0:
        raise ValueError("holdings workbook does not contain a ticker column")
    symbols: list[str] = []
    for row in rows[header_row_index + 1 :]:
        raw = row.get(ticker_column, "").strip().upper()
        if _TICKER.fullmatch(raw) and raw not in {"USD", "CASH"}:
            symbols.append(raw)
    return list(dict.fromkeys(symbols))


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter() if _local(node.tag) == "t") for item in root]


def _worksheet_rows(content: bytes, shared: list[str]) -> list[dict[int, str]]:
    root = ElementTree.fromstring(content)
    rows: list[dict[int, str]] = []
    for raw_row in (node for node in root.iter() if _local(node.tag) == "row"):
        row: dict[int, str] = {}
        for cell in (node for node in raw_row if _local(node.tag) == "c"):
            match = _CELL_REF.match(str(cell.attrib.get("r") or ""))
            if match is None:
                continue
            column = _column_index(match.group(1))
            value_node = next((node for node in cell.iter() if _local(node.tag) == "v"), None)
            inline_text = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
            raw_value = (value_node.text or "") if value_node is not None else inline_text
            if cell.attrib.get("t") == "s" and raw_value:
                try:
                    value = shared[int(raw_value)]
                except (IndexError, ValueError):
                    value = ""
            else:
                value = raw_value
            row[column] = value
        rows.append(row)
    return rows


def _column_index(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
