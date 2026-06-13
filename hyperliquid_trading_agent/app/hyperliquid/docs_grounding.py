from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

OFFICIAL_API_DOCS_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api"
OFFICIAL_API_DOCS_MARKDOWN_URL = f"{OFFICIAL_API_DOCS_URL}.md"
OFFICIAL_LLMS_INDEX_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/llms.txt"

GROUND_TRUTH_NOTES = [
    "Use /info for public and account query endpoints.",
    "Use the official Python SDK for signed exchange actions; mainnet execution is disabled in the MVP.",
    "Do not use an API wallet address when querying master/subaccount state.",
    "For spot, coin names may be PURR/USDC or @index from spotMeta.universe.",
]

DOC_PAGES = {
    "api": OFFICIAL_API_DOCS_MARKDOWN_URL,
    "info": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint.md",
    "perps": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals.md",
    "spot": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot.md",
    "exchange": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint.md",
    "websocket": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions.md",
    "rate_limits": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits.md",
    "tick_lot": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size.md",
    "signing": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing.md",
    "margining": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining.md",
    "funding": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding.md",
}


@dataclass(frozen=True)
class DocsAnswer:
    query: str
    source: str
    excerpt: str
    notes: list[str]


class HyperliquidDocs:
    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    async def ask(self, query: str, page: str = "api") -> DocsAnswer:
        source = DOC_PAGES.get(page, DOC_PAGES["api"])
        ask_url = f"{source}?{urlencode({'ask': query})}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(ask_url)
                response.raise_for_status()
                text = response.text.strip()
        except Exception:
            text = ""
        if not text:
            text = _static_excerpt(query)
        return DocsAnswer(query=query, source=source, excerpt=text[:3000], notes=GROUND_TRUTH_NOTES)

    async def fetch_page(self, page: str = "api") -> str:
        source = DOC_PAGES.get(page, DOC_PAGES["api"])
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(source)
            response.raise_for_status()
            return response.text


async def search_hyperliquid_docs(query: str) -> dict[str, object]:
    answer = await HyperliquidDocs().ask(query)
    return {"query": answer.query, "source": answer.source, "excerpt": answer.excerpt, "notes": answer.notes}


def _static_excerpt(query: str) -> str:
    lowered = query.lower()
    if "rate" in lowered or "limit" in lowered:
        return "REST requests share 1200 aggregate weight/minute/IP. allMids, l2Book, clearinghouseState, orderStatus, spotClearinghouseState have weight 2; most other info requests have weight 20."
    if "spot" in lowered:
        return "For spot endpoints, use PURR/USDC for PURR and @index for other spot pairs where index comes from spotMeta.universe."
    if "sign" in lowered or "api wallet" in lowered:
        return "Hyperliquid recommends using the official SDK for signatures. API wallets sign on behalf of master/subaccounts, but account queries must use the actual master/subaccount address."
    if "tick" in lowered or "size" in lowered:
        return "Prices can have up to 5 significant figures and no more than MAX_DECIMALS - szDecimals decimal places; MAX_DECIMALS is 6 for perps and 8 for spot."
    return "Official Hyperliquid docs are the ground truth for API behavior. MVP uses read-only /info endpoints and disables exchange actions."
