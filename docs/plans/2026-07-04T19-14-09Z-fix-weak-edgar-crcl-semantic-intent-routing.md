---
created: 2026-07-04T19:14:09.618Z
source: pi-plan-mode
status: accepted-for-execution
---

# Fix Weak EDGAR / CRCL Semantic Intent Routing

## Summary

The chat shows three weak routing points:

1. **Company-name/entity resolution is too thin**: “Circle” was not resolved to public ticker `CRCL`, so the model answered from stale memory and said Circle was private.
2. **EDGAR filing intent is routed like generic news/market context**: “latest quarterly report from EDGAR” should deterministically call an SEC filing lookup, not market/news tools or LLM memory.
3. **Discord follow-up context is underused for terse corrections**: after “link me Circle report from EDGAR”, the one-token follow-up “CRCL” should inherit the prior EDGAR filing task, not ask whether the user means equity vs Hyperliquid perp.

## Implementation Steps

1. Add deterministic SEC EDGAR company/ticker + filing lookup support.
2. Expand company alias/entity resolution, including `Circle` → `CRCL`.
3. Add explicit SEC filing intent fields to the market/intent parser.
4. Update runner routing so filing requests call EDGAR tools and avoid irrelevant market snapshots.
5. Add narrow Discord elliptical-context carryover for terse symbol follow-ups.
6. Add deterministic local answers for direct “link me latest filing” requests.
7. Update prompts/guards to prevent unsupported public/private or filing claims.
8. Add regression tests for the full chat failure mode.

## Key Implementation Details

### New EDGAR client

Create `hyperliquid_trading_agent/app/tradfi/sec_edgar.py`.

Use SEC public endpoints:

- Company ticker map: `https://www.sec.gov/files/company_tickers.json`
- Submissions: `https://data.sec.gov/submissions/CIK##########.json`
- Filing URL:
  - document: `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_document}`
  - index: `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{accession_number}-index.html`

Add Pydantic models:

- `SecCompany`
  - `ticker`
  - `cik`
  - `title`
  - `matched_by`
- `SecFiling`
  - `form`
  - `filing_date`
  - `report_date`
  - `accession_number`
  - `primary_document`
  - `primary_doc_description`
  - `document_url`
  - `filing_detail_url`
- `SecFilingSearchResult`
  - `query`
  - `company`
  - `forms_requested`
  - `filings`
  - `note`

Add settings:

- `sec_edgar_user_agent`
- `sec_edgar_timeout_seconds = 10`
- `sec_edgar_company_cache_ttl_seconds = 86400`
- `sec_edgar_submissions_cache_ttl_seconds = 300`

### Shared company aliases

Create `hyperliquid_trading_agent/app/tradfi/company_aliases.py`.

Include existing aliases plus:

- `circle` → `CRCL`
- `circle internet` → `CRCL`
- `circle internet group` → `CRCL`
- `circle internet financial` → `CRCL`

Update `markets/resolution.py` to use this shared alias map instead of the private `_COMPANY_NAME_TO_TICKER`.

### Intent parser changes

In `hyperliquid_trading_agent/app/markets/resolution.py`, extend `MarketIntent` with:

- `wants_sec_filing: bool = False`
- `filing_forms: list[str] = []`

Inference rules:

- `10-Q`, `quarterly`, `quarterly report` → `["10-Q"]`
- `10-K`, `annual`, `annual report` → `["10-K"]`
- `8-K`, `current report` → `["8-K"]`
- `S-1`, `prospectus`, `registration statement` → `["S-1"]`
- `EDGAR`, `SEC filing`, explicit SEC forms → `wants_sec_filing=True`

For the target chat:

- `"link me latest Circle quarterly earnings report from edgar"`
  - symbols: `["CRCL"]`
  - `wants_tradfi=True`
  - `wants_sec_filing=True`
  - `filing_forms=["10-Q"]`

### Agent tool changes

In `AgentTools`, add:

```python
async def get_sec_filings(
    self,
    query: str,
    symbols: list[str] | None = None,
    forms: list[str] | None = None,
    limit: int = 5,
) -> ToolResult:
    ...
```

Source should be `sec-edgar:data.sec.gov`.

Behavior:

- Prefer explicit `symbols`.
- Fall back to alias/company-name lookup.
- Return no fabricated data if no company or filing is found.
- For quarterly report requests, default to `10-Q`.
- Do not summarize filing contents unless actual filing text parsing is later added.

### Runner routing changes

In `TradingAgentRunner._gather_context`:

- Build a `routing_prompt` that may include narrow prior filing intent.
- If `parse_market_intent(routing_prompt).wants_sec_filing`:
  - call `resolve_market_intent(routing_prompt)`
  - call `get_sec_filings(...)`
  - do **not** call Hyperliquid market snapshots unless current prompt explicitly asks for perp/price/read/funding/orderbook.
  - do **not** call generic corporate actions for “earnings” when the request is an EDGAR filing request.

Add deterministic post-tool response path in `answer()`:

```python
sec_answer = _sec_filing_direct_answer(prompt, tool_results)
if sec_answer is not None:
    return AgentResponse(content=sec_answer, tool_results=tool_results, model_used="local:sec-edgar-router")
```

For direct link requests, answer locally from tool output.

Example expected answer shape:

> Latest CRCL 10-Q on SEC EDGAR: filed 2026-05-11, report period 2026-03-31.  
> Filing page: ...  
> Primary document: ...

If no filing:

> I found CRCL on SEC EDGAR, but no recent 10-Q matched in the submissions feed. I’m not going to fabricate a filing link.

### Narrow Discord follow-up carryover

Add helper in `runner.py`:

- `_is_terse_entity_followup(prompt)`
  - true for 1–3 token prompts like `CRCL`, `$CRCL`, `Circle`, `same CRCL`
  - false if current prompt says `perp`, `Hyperliquid`, `funding`, `orderbook`, `price`, `trade`, etc.
- `_prior_user_filing_intent(context)`
  - scan only prior `User:` lines or referenced user content
  - match `edgar`, `sec filing`, `10-q`, `10-k`, `quarterly report`, `annual report`
  - ignore assistant lines to avoid carrying forward hallucinations

If both are true, route as:

```text
{current prompt}

Prior user filing request for routing only:
{last prior user filing request}
```

This fixes the second chat turn: `CRCL` inherits “latest Circle quarterly report from EDGAR”.

### Prompt update

Update `SYSTEM_PROMPT`:

- For SEC/EDGAR filing requests, use tool-provided SEC data only.
- Do not claim a company is private/public unless SEC/company lookup evidence supports it.
- Do not invent filing dates, CIKs, revenue, EPS, or filing URLs.
- If the EDGAR tool has no match, say no matching filing was found.

## Tests

Add/extend tests in `tests/test_market_intent_router.py`:

- `Circle quarterly report EDGAR` resolves to `CRCL`.
- `CRCL quarterly report EDGAR` routes TradFi only, not Hyperliquid.
- `do you have access to SEC EDGAR?` still has no symbols.
- `AAPL 10-K in EDGAR?` requests `10-K`.

Add new tests for `SecEdgarClient`:

- resolves `CRCL` from fake `company_tickers.json`
- resolves `Circle` through alias
- returns latest `10-Q` with valid SEC URLs
- returns empty result without fabrication when no matching form exists

Add runner regression tests:

- Initial prompt: `link me latest Circle quarterly earnings report from edgar`
  - calls `resolve_market_intent`
  - calls `get_sec_filings`
  - does not call Hyperliquid market snapshot
  - deterministic answer includes SEC URL
- Follow-up prompt: `CRCL` with prior EDGAR user context
  - calls `get_sec_filings`
  - does not ask equity-vs-perp clarification
- Explicit override: `CRCL perp read` with same context
  - does not inherit EDGAR intent
  - routes Hyperliquid normally
- Capability question still returns local EDGAR capability answer without invoking high-stakes routing.

## Acceptance Criteria

- The bot never answers an EDGAR filing request from stale model memory alone.
- `Circle` and `CRCL` both route to the same SEC company lookup path.
- A terse `CRCL` follow-up after an EDGAR request returns the filing link, not a market read.
- Hyperliquid-vs-equity ambiguity remains for plain standalone `CRCL` without filing context.
- No invented filing dates, CIKs, EPS, revenue, or URLs appear when tool data is missing.
- Existing market-router behavior for crypto, HIP-3 perps, commodities, and ETF requests remains unchanged.

## Implementation Notes

- Preserve existing uncommitted work in the repo; do not reset or overwrite unrelated changes.
- No database migration is required.
- Update `.env.example` with `SEC_EDGAR_USER_AGENT`.
- Keep the first version link-only; full filing text/XBRL parsing is out of scope.








<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[~]` in progress, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Add deterministic SEC EDGAR company/ticker + filing lookup support. _(done)_
- [x] 2. Expand company alias/entity resolution, including Circle → CRCL. _(done)_
- [x] 3. Add explicit SEC filing intent fields to the market/intent parser. _(done)_
- [x] 4. Update runner routing so filing requests call EDGAR tools and avoid irrelevant market snapshots. _(done)_
- [x] 5. Add narrow Discord elliptical-context carryover for terse symbol follow-ups. _(done)_
- [x] 6. Add deterministic local answers for direct “link me latest filing” requests. _(done)_
- [x] 7. Update prompts/guards to prevent unsupported public/private or filing claims. _(done)_
- [x] 8. Add regression tests for the full chat failure mode. _(done)_

<!-- pi-plan-progress:end -->
