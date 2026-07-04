from __future__ import annotations

import re
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.tradfi.company_aliases import resolve_company_alias

_SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_TICKER_RE = re.compile(r"(?<![A-Za-z0-9_$])\$?([A-Z][A-Z0-9.]{0,12})(?![A-Za-z0-9_])")
_DEFAULT_USER_AGENT = "hyperliquid-trading-agent/0.1 sec-edgar-contact@example.com"


class SecCompany(BaseModel):
    ticker: str
    cik: str
    title: str = ""
    matched_by: str = ""


class SecFiling(BaseModel):
    form: str
    filing_date: str = ""
    report_date: str = ""
    accession_number: str
    primary_document: str = ""
    primary_doc_description: str = ""
    document_url: str = ""
    filing_detail_url: str = ""


class SecFilingSearchResult(BaseModel):
    query: str
    company: SecCompany | None = None
    forms_requested: list[str] = Field(default_factory=list)
    filings: list[SecFiling] = Field(default_factory=list)
    note: str = ""


class SecEdgarClient:
    """Small deterministic SEC EDGAR facade for ticker/company lookup and filing links.

    This intentionally retrieves filing metadata and URLs only. It does not parse filing
    bodies or XBRL, so callers must not summarize financial line items from this tool.
    """

    def __init__(self, settings: Settings | None = None, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or Settings()
        self.user_agent = (getattr(self.settings, "sec_edgar_user_agent", "") or _DEFAULT_USER_AGENT).strip()
        self.timeout = float(getattr(self.settings, "sec_edgar_timeout_seconds", 10.0))
        self.company_cache_ttl_seconds = int(getattr(self.settings, "sec_edgar_company_cache_ttl_seconds", 86_400))
        self.submissions_cache_ttl_seconds = int(getattr(self.settings, "sec_edgar_submissions_cache_ttl_seconds", 300))
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=self.timeout, headers=self._headers())
        self._company_cache: tuple[float, list[SecCompany]] | None = None
        self._submissions_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def resolve_company(self, query: str, *, symbols: list[str] | None = None) -> SecCompany | None:
        companies = await self._company_tickers()
        by_ticker = {company.ticker.upper(): company for company in companies}
        for symbol in _candidate_tickers(query, symbols):
            company = by_ticker.get(symbol.upper())
            if company is not None:
                return company.model_copy(update={"matched_by": f"ticker:{symbol.upper()}"})

        alias = resolve_company_alias(query)
        if alias:
            company = by_ticker.get(alias.upper())
            if company is not None:
                return company.model_copy(update={"matched_by": f"alias:{alias.upper()}"})

        normalized_query = _normalize_name(query)
        if normalized_query:
            exact = [company for company in companies if _normalize_name(company.title) == normalized_query]
            if exact:
                return exact[0].model_copy(update={"matched_by": "company_name_exact"})
            contains = [company for company in companies if normalized_query in _normalize_name(company.title)]
            if contains:
                return contains[0].model_copy(update={"matched_by": "company_name_contains"})
        return None

    async def latest_filings(
        self,
        query: str,
        *,
        symbols: list[str] | None = None,
        forms: list[str] | None = None,
        limit: int = 5,
    ) -> SecFilingSearchResult:
        forms_requested = _normalize_forms(forms or [])
        company = await self.resolve_company(query, symbols=symbols)
        if company is None:
            return SecFilingSearchResult(query=query, forms_requested=forms_requested, note="company_not_found")

        submissions = await self._submissions(company.cik)
        filings = _recent_filings(submissions, company.cik, forms_requested=forms_requested)
        filings.sort(key=lambda item: (item.filing_date, item.report_date, item.accession_number), reverse=True)
        selected = filings[: max(1, min(limit, 40))]
        note = ""
        if forms_requested and not selected:
            note = "no_matching_filings"
        elif not selected:
            note = "no_recent_filings"
        return SecFilingSearchResult(query=query, company=company, forms_requested=forms_requested, filings=selected, note=note)

    async def _company_tickers(self) -> list[SecCompany]:
        now = time.time()
        if self._company_cache is not None:
            cached_at, companies = self._company_cache
            if now - cached_at < self.company_cache_ttl_seconds:
                return companies
        data = await self._get_json(_SEC_COMPANY_TICKERS_URL)
        companies: list[SecCompany] = []
        if isinstance(data, dict):
            rows = data.values()
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper().strip()
            cik_value = row.get("cik_str") or row.get("cik") or row.get("CIK")
            if not ticker or cik_value is None:
                continue
            companies.append(
                SecCompany(
                    ticker=ticker,
                    cik=_pad_cik(cik_value),
                    title=str(row.get("title") or row.get("name") or "").strip(),
                    matched_by="company_tickers",
                )
            )
        self._company_cache = (now, companies)
        return companies

    async def _submissions(self, cik: str) -> dict[str, Any]:
        cik_padded = _pad_cik(cik)
        now = time.time()
        cached = self._submissions_cache.get(cik_padded)
        if cached is not None:
            cached_at, data = cached
            if now - cached_at < self.submissions_cache_ttl_seconds:
                return data
        data = await self._get_json(_SEC_SUBMISSIONS_URL.format(cik=cik_padded))
        if not isinstance(data, dict):
            data = {}
        self._submissions_cache[cik_padded] = (now, data)
        return data

    async def _get_json(self, url: str) -> Any:
        try:
            response = await self._http.get(url, headers=self._headers())
        except TypeError:
            response = await self._http.get(url)
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def _recent_filings(submissions: dict[str, Any], cik: str, *, forms_requested: list[str]) -> list[SecFiling]:
    recent = submissions.get("filings", {}).get("recent", {}) if isinstance(submissions.get("filings"), dict) else {}
    if not isinstance(recent, dict):
        return []
    forms = _list_field(recent, "form")
    accession_numbers = _list_field(recent, "accessionNumber")
    filing_dates = _list_field(recent, "filingDate")
    report_dates = _list_field(recent, "reportDate")
    primary_documents = _list_field(recent, "primaryDocument")
    descriptions = _list_field(recent, "primaryDocDescription")
    wanted = set(forms_requested)
    out: list[SecFiling] = []
    for idx, form in enumerate(forms):
        form_norm = _normalize_form(form)
        if wanted and form_norm not in wanted:
            continue
        accession = _at(accession_numbers, idx)
        if not accession:
            continue
        primary_document = _at(primary_documents, idx)
        accession_no_dashes = accession.replace("-", "")
        cik_int = str(int(_pad_cik(cik)))
        filing_detail_url = f"{_SEC_ARCHIVES_BASE}/{cik_int}/{accession_no_dashes}/{accession}-index.html"
        document_url = f"{_SEC_ARCHIVES_BASE}/{cik_int}/{accession_no_dashes}/{primary_document}" if primary_document else filing_detail_url
        out.append(
            SecFiling(
                form=form_norm,
                filing_date=_at(filing_dates, idx),
                report_date=_at(report_dates, idx),
                accession_number=accession,
                primary_document=primary_document,
                primary_doc_description=_at(descriptions, idx),
                document_url=document_url,
                filing_detail_url=filing_detail_url,
            )
        )
    return out


def _candidate_tickers(query: str, symbols: list[str] | None) -> list[str]:
    out: list[str] = []
    for symbol in symbols or []:
        cleaned = str(symbol or "").split(":", 1)[-1].upper().strip().lstrip("$")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    for match in _TICKER_RE.finditer(query or ""):
        token = match.group(1).upper()
        if token and token not in out:
            out.append(token)
    return out


def _normalize_forms(forms: list[str]) -> list[str]:
    out: list[str] = []
    for form in forms:
        normalized = _normalize_form(form)
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _normalize_form(form: Any) -> str:
    return " ".join(str(form or "").upper().replace("_", "-").split())


def _normalize_name(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    stop = {"inc", "incorporated", "corp", "corporation", "class", "common", "stock", "plc", "ltd", "co", "company"}
    tokens = [token for token in text.split() if token not in stop]
    return " ".join(tokens)


def _pad_cik(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(10)[:10]


def _list_field(recent: dict[str, Any], key: str) -> list[Any]:
    value = recent.get(key) or []
    return list(value) if isinstance(value, list) else []


def _at(values: list[Any], idx: int) -> str:
    if idx >= len(values):
        return ""
    return str(values[idx] or "").strip()
