from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from hyperliquid_trading_agent.app.hip4.ids import OUTCOME_ASSET_ID_OFFSET, OutcomeAssetId, coin, parse_identifier
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.registry import parse_outcomes
from hyperliquid_trading_agent.app.hip4.schemas import ONE, ZERO, NormalizedOutcomeBook, OutcomeSpec

_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "against",
    "beating",
    "beat",
    "beats",
    "bet",
    "buy",
    "defeat",
    "defeating",
    "defeats",
    "for",
    "in",
    "market",
    "no",
    "of",
    "on",
    "or",
    "paper",
    "pm",
    "predict",
    "prediction",
    "the",
    "to",
    "versus",
    "vs",
    "will",
    "win",
    "winning",
    "wins",
    "yes",
}


@dataclass(frozen=True)
class Hip4ResolvedRef:
    outcome: OutcomeSpec
    side: Literal[0, 1] | None = None
    asset: OutcomeAssetId | None = None


@dataclass(frozen=True)
class Hip4SideQuote:
    outcome: OutcomeSpec
    side: Literal[0, 1]
    book: NormalizedOutcomeBook

    @property
    def coin(self) -> str:
        return self.book.coin

    @property
    def outcome_name(self) -> str:
        return self.outcome.side0_name if self.side == 0 else self.outcome.side1_name

    @property
    def best_bid(self) -> Decimal | None:
        return self.book.bids[0].px if self.book.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.book.asks[0].px if self.book.asks else None

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return _clamp_probability((self.best_bid + self.best_ask) / Decimal("2"))
        return self.best_bid if self.best_bid is not None else self.best_ask

    @property
    def buy_price(self) -> Decimal | None:
        return self.best_ask if self.best_ask is not None else self.mid_price

    @property
    def liquidity_usd(self) -> Decimal | None:
        total = ZERO
        for level in [*self.book.bids, *self.book.asks]:
            total += level.px * level.sz
        return total if total > ZERO else None


class Hip4MarketData:
    def __init__(self, *, hip4_client: Any):
        self.hip4_client = hip4_client

    async def list_outcomes(self, query: str | None = None, *, strict: bool = True, include_settled: bool = False) -> list[OutcomeSpec]:
        payload = await self.hip4_client.outcome_meta()
        outcomes = parse_outcomes(payload if isinstance(payload, dict) else {})
        if not include_settled:
            outcomes = [item for item in outcomes if not item.settled]
        tokens = _query_terms(query or "")
        if tokens:
            outcomes = [item for item in outcomes if _matches_outcome(item, tokens, strict=strict)]
        return outcomes

    async def resolve_identifier(self, ref: str) -> Hip4ResolvedRef | None:
        parsed = _parse_ref(ref)
        if parsed is None:
            return None
        outcome_id, side, asset = parsed
        outcomes = {item.outcome_id: item for item in await self.list_outcomes(include_settled=True)}
        outcome = outcomes.get(outcome_id) or _synthetic_outcome(outcome_id)
        return Hip4ResolvedRef(outcome=outcome, side=side, asset=asset)

    async def quote_outcome_side(self, outcome: OutcomeSpec, side: Literal[0, 1]) -> Hip4SideQuote | None:
        book_coin = coin(outcome.outcome_id, side)
        payload = await self.hip4_client.l2_book(book_coin)
        book = parse_l2_book(book_coin, payload, source="rest")
        return Hip4SideQuote(outcome=outcome, side=side, book=book)

    async def quotes_for_outcome(self, outcome: OutcomeSpec, *, sides: list[int] | None = None) -> list[Hip4SideQuote]:
        quotes: list[Hip4SideQuote] = []
        for side in sides or [0, 1]:
            if side not in {0, 1}:
                continue
            quote = await self.quote_outcome_side(outcome, side)  # type: ignore[arg-type]
            if quote is not None:
                quotes.append(quote)
        return quotes


def _parse_ref(ref: str) -> tuple[int, Literal[0, 1] | None, OutcomeAssetId | None] | None:
    raw = ref.strip().lower()
    if not raw:
        return None
    prefixed = raw.startswith("hip4:") or raw.startswith("hl:")
    raw = raw.removeprefix("hip4:").removeprefix("hl:")
    match = re.fullmatch(r"(\d+):([01])", raw)
    if match:
        side = int(match.group(2))
        return int(match.group(1)), side, OutcomeAssetId(outcome_id=int(match.group(1)), side=side)  # type: ignore[arg-type]
    try:
        asset = parse_identifier(raw)
        return asset.outcome_id, asset.side, asset  # type: ignore[return-value]
    except ValueError:
        if prefixed and raw.isdigit() and int(raw) < OUTCOME_ASSET_ID_OFFSET:
            return int(raw), None, None
        return None


def _synthetic_outcome(outcome_id: int) -> OutcomeSpec:
    return OutcomeSpec(outcome_id=outcome_id, name=f"HIP-4 outcome {outcome_id}", side0_name="Side 0", side1_name="Side 1")


def _matches_outcome(outcome: OutcomeSpec, tokens: list[str], *, strict: bool) -> bool:
    text = _outcome_haystack(outcome)
    if strict:
        return all(token in text for token in tokens)
    return all(token in text for token in tokens) or any(token in text for token in tokens[:3])


def _outcome_haystack(outcome: OutcomeSpec) -> str:
    text = " ".join(
        [
            outcome.name,
            outcome.description,
            outcome.side0_name,
            outcome.side1_name,
            str(outcome.outcome_id),
        ]
    ).lower()
    if "round of 16" in text:
        text = f"{text} r16"
    return text


def _query_terms(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", query.lower().replace("$", " ")) if token and token not in _QUERY_STOPWORDS]


def _clamp_probability(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE, value))
