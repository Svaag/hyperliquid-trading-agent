from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.ids import coin
from hyperliquid_trading_agent.app.hip4.mechanics import apply_actions, residual_inventory
from hyperliquid_trading_agent.app.hip4.orderbook import book_is_fresh, executable_vwap, total_size
from hyperliquid_trading_agent.app.hip4.schemas import (
    ZERO,
    ExecutableLeg,
    Hip4Candidate,
    Hip4CapabilityProbe,
    NormalizedOutcomeBook,
    OutcomeSpec,
    PaperNativeAction,
    QuestionSpec,
)

BPS = Decimal("10000")


class Hip4Scanner:
    def __init__(self, *, settings: Settings):
        self.settings = settings
        self.last_scan_at_ms: int | None = None
        self.last_rejects: list[dict[str, Any]] = []

    def scan(
        self,
        *,
        outcomes: dict[int, OutcomeSpec],
        questions: dict[int, QuestionSpec],
        books: dict[str, NormalizedOutcomeBook],
        capabilities: Hip4CapabilityProbe | None = None,
    ) -> list[Hip4Candidate]:
        self.last_scan_at_ms = int(time.time() * 1000)
        self.last_rejects = []
        if not self.settings.hip4_scan_enabled:
            self.last_rejects.append({"code": "scan_disabled", "message": "HIP4_SCAN_ENABLED is false"})
            return []
        if capabilities is not None and not capabilities.supports_abstract_native_mechanics:
            self.last_rejects.append({"code": "native_action_modeling_unsupported", "message": "Capability probe disabled paper action modeling"})
            return []

        candidates: list[Hip4Candidate] = []
        for outcome in outcomes.values():
            if outcome.settled and not self.settings.hip4_include_partially_settled:
                continue
            candidates.extend(self._scan_binary(outcome, books))

        if capabilities is None or capabilities.supports_question_mechanics:
            for question in questions.values():
                if question.status != "open" and not self.settings.hip4_include_partially_settled:
                    self.last_rejects.append({"code": "question_settled_or_partial", "question_id": question.question_id})
                    continue
                candidates.extend(self._scan_question(question, books, outcomes))
        return candidates

    def _scan_binary(self, outcome: OutcomeSpec, books: dict[str, NormalizedOutcomeBook]) -> list[Hip4Candidate]:
        side0 = books.get(coin(outcome.outcome_id, 0))
        side1 = books.get(coin(outcome.outcome_id, 1))
        if side0 is None or side1 is None:
            self.last_rejects.append({"code": "missing_binary_books", "outcome_id": outcome.outcome_id})
            return []
        if not self._books_fresh([side0, side1], context={"strategy": "binary", "outcome_id": outcome.outcome_id}):
            return []
        if not outcome.quote_token:
            self.last_rejects.append({"code": "quote_token_missing", "outcome_id": outcome.outcome_id})
            return []
        return [
            item
            for item in [
                self._binary_split_sell(outcome, side0, side1),
                self._binary_buy_merge(outcome, side0, side1),
            ]
            if item is not None
        ]

    def _binary_split_sell(self, outcome: OutcomeSpec, side0: NormalizedOutcomeBook, side1: NormalizedOutcomeBook) -> Hip4Candidate | None:
        size = min(total_size(side0.bids), total_size(side1.bids), self.settings.hip4_max_paper_notional_per_candidate_usd)
        if size <= ZERO:
            self.last_rejects.append({"code": "partial_depth", "strategy": "binary_split_sell", "outcome_id": outcome.outcome_id})
            return None
        filled0, px0 = executable_vwap(side0.bids, size)
        filled1, px1 = executable_vwap(side1.bids, size)
        if filled0 != size or filled1 != size:
            self.last_rejects.append({"code": "partial_depth", "strategy": "binary_split_sell", "outcome_id": outcome.outcome_id})
            return None
        proceeds = size * (px0 + px1)
        fees = _fee_stress(size, self.settings.hip4_fee_stress_bps)
        edge_usd = proceeds - size - fees
        edge_bps = edge_usd / size * BPS if size > ZERO else ZERO
        if size < self.settings.hip4_min_depth_usd or not self._passes_edge(edge_bps, edge_usd):
            self.last_rejects.append({"code": "edge_below_threshold", "strategy": "binary_split_sell", "outcome_id": outcome.outcome_id})
            return None
        actions = [
            PaperNativeAction(action_type="SPLIT_OUTCOME", outcome_id=outcome.outcome_id, amount=size),
            PaperNativeAction(action_type="SELL_SIDE_TOKEN", outcome_id=outcome.outcome_id, side=0, coin=side0.coin, amount=size, price=px0),
            PaperNativeAction(action_type="SELL_SIDE_TOKEN", outcome_id=outcome.outcome_id, side=1, coin=side1.coin, amount=size, price=px1),
        ]
        quote_token = str(outcome.quote_token)
        residual = residual_inventory(apply_actions({quote_token: size}, actions, quote_token=quote_token), quote_token=quote_token)
        return self._candidate(
            strategy_type="binary_split_sell",
            outcome_ids=[outcome.outcome_id],
            size=size,
            gross=proceeds,
            edge_usd=edge_usd,
            edge_bps=edge_bps,
            legs=[_leg(side0, "bid", size, px0), _leg(side1, "bid", size, px1)],
            actions=actions,
            residual=residual,
            quote_token=quote_token,
            book_times={side0.coin: side0.as_of_ms, side1.coin: side1.as_of_ms},
        )

    def _binary_buy_merge(self, outcome: OutcomeSpec, side0: NormalizedOutcomeBook, side1: NormalizedOutcomeBook) -> Hip4Candidate | None:
        size = min(total_size(side0.asks), total_size(side1.asks), self.settings.hip4_max_paper_notional_per_candidate_usd)
        if size <= ZERO:
            self.last_rejects.append({"code": "partial_depth", "strategy": "binary_buy_merge", "outcome_id": outcome.outcome_id})
            return None
        filled0, px0 = executable_vwap(side0.asks, size)
        filled1, px1 = executable_vwap(side1.asks, size)
        if filled0 != size or filled1 != size:
            self.last_rejects.append({"code": "partial_depth", "strategy": "binary_buy_merge", "outcome_id": outcome.outcome_id})
            return None
        cost = size * (px0 + px1)
        fees = _fee_stress(size, self.settings.hip4_fee_stress_bps)
        edge_usd = size - cost - fees
        edge_bps = edge_usd / cost * BPS if cost > ZERO else ZERO
        if cost < self.settings.hip4_min_depth_usd or not self._passes_edge(edge_bps, edge_usd):
            self.last_rejects.append({"code": "edge_below_threshold", "strategy": "binary_buy_merge", "outcome_id": outcome.outcome_id})
            return None
        actions = [
            PaperNativeAction(action_type="BUY_SIDE_TOKEN", outcome_id=outcome.outcome_id, side=0, coin=side0.coin, amount=size, price=px0),
            PaperNativeAction(action_type="BUY_SIDE_TOKEN", outcome_id=outcome.outcome_id, side=1, coin=side1.coin, amount=size, price=px1),
            PaperNativeAction(action_type="MERGE_OUTCOME", outcome_id=outcome.outcome_id, amount=size),
        ]
        quote_token = str(outcome.quote_token)
        residual = residual_inventory(apply_actions({quote_token: cost}, actions, quote_token=quote_token), quote_token=quote_token)
        return self._candidate(
            strategy_type="binary_buy_merge",
            outcome_ids=[outcome.outcome_id],
            size=size,
            gross=cost,
            edge_usd=edge_usd,
            edge_bps=edge_bps,
            legs=[_leg(side0, "ask", size, px0), _leg(side1, "ask", size, px1)],
            actions=actions,
            residual=residual,
            quote_token=quote_token,
            book_times={side0.coin: side0.as_of_ms, side1.coin: side1.as_of_ms},
        )

    def _scan_question(self, question: QuestionSpec, books: dict[str, NormalizedOutcomeBook], outcomes: dict[int, OutcomeSpec]) -> list[Hip4Candidate]:
        if len(question.outcome_ids) < 2:
            return []
        quote_token = self._question_quote_token(question, outcomes)
        if quote_token is None:
            return []
        side0_books: list[NormalizedOutcomeBook] = []
        for outcome_id in question.outcome_ids:
            book = books.get(coin(outcome_id, 0))
            if book is None:
                self.last_rejects.append({"code": "missing_question_book", "question_id": question.question_id, "outcome_id": outcome_id})
                return []
            side0_books.append(book)
        if not self._books_fresh(side0_books, context={"strategy": "question", "question_id": question.question_id}):
            return []
        return [item for item in [self._question_sell(question, side0_books, quote_token), self._question_buy(question, side0_books, quote_token)] if item is not None]

    def _question_sell(self, question: QuestionSpec, side0_books: list[NormalizedOutcomeBook], quote_token: str) -> Hip4Candidate | None:
        size = min(*[total_size(book.bids) for book in side0_books], self.settings.hip4_max_paper_notional_per_candidate_usd)
        if size <= ZERO:
            self.last_rejects.append({"code": "partial_depth", "strategy": "question_complete_set_sell", "question_id": question.question_id})
            return None
        legs: list[ExecutableLeg] = []
        sum_px = ZERO
        for book in side0_books:
            filled, px = executable_vwap(book.bids, size)
            if filled != size:
                self.last_rejects.append({"code": "partial_depth", "strategy": "question_complete_set_sell", "question_id": question.question_id})
                return None
            sum_px += px
            legs.append(_leg(book, "bid", size, px))
        proceeds = size * sum_px
        fees = _fee_stress(size, self.settings.hip4_fee_stress_bps)
        edge_usd = proceeds - size - fees
        edge_bps = edge_usd / size * BPS if size > ZERO else ZERO
        if size < self.settings.hip4_min_depth_usd or not self._passes_edge(edge_bps, edge_usd):
            self.last_rejects.append({"code": "edge_below_threshold", "strategy": "question_complete_set_sell", "question_id": question.question_id})
            return None
        seed = question.outcome_ids[0]
        actions = [
            PaperNativeAction(action_type="SPLIT_OUTCOME", outcome_id=seed, amount=size),
            PaperNativeAction(action_type="NEGATE_OUTCOME", outcome_id=seed, question_id=question.question_id, amount=size, metadata={"question_outcome_ids": question.outcome_ids}),
        ]
        actions.extend(PaperNativeAction(action_type="SELL_SIDE_TOKEN", outcome_id=book.outcome_id, side=0, coin=book.coin, amount=size, price=leg.avg_price) for book, leg in zip(side0_books, legs, strict=True))
        residual = residual_inventory(apply_actions({quote_token: size}, actions, quote_token=quote_token), quote_token=quote_token)
        return self._candidate("question_complete_set_sell", question.outcome_ids, size, proceeds, edge_usd, edge_bps, legs, actions, residual, question_id=question.question_id, quote_token=quote_token, book_times={book.coin: book.as_of_ms for book in side0_books})

    def _question_buy(self, question: QuestionSpec, side0_books: list[NormalizedOutcomeBook], quote_token: str) -> Hip4Candidate | None:
        size = min(*[total_size(book.asks) for book in side0_books], self.settings.hip4_max_paper_notional_per_candidate_usd)
        if size <= ZERO:
            self.last_rejects.append({"code": "partial_depth", "strategy": "question_complete_set_buy", "question_id": question.question_id})
            return None
        legs: list[ExecutableLeg] = []
        sum_px = ZERO
        for book in side0_books:
            filled, px = executable_vwap(book.asks, size)
            if filled != size:
                self.last_rejects.append({"code": "partial_depth", "strategy": "question_complete_set_buy", "question_id": question.question_id})
                return None
            sum_px += px
            legs.append(_leg(book, "ask", size, px))
        cost = size * sum_px
        fees = _fee_stress(size, self.settings.hip4_fee_stress_bps)
        edge_usd = size - cost - fees
        edge_bps = edge_usd / cost * BPS if cost > ZERO else ZERO
        if cost < self.settings.hip4_min_depth_usd or not self._passes_edge(edge_bps, edge_usd):
            self.last_rejects.append({"code": "edge_below_threshold", "strategy": "question_complete_set_buy", "question_id": question.question_id})
            return None
        actions = [PaperNativeAction(action_type="BUY_SIDE_TOKEN", outcome_id=book.outcome_id, side=0, coin=book.coin, amount=size, price=leg.avg_price) for book, leg in zip(side0_books, legs, strict=True)]
        actions.append(PaperNativeAction(action_type="MERGE_QUESTION", question_id=question.question_id, amount=size, metadata={"question_outcome_ids": question.outcome_ids}))
        residual = residual_inventory(apply_actions({quote_token: cost}, actions, quote_token=quote_token), quote_token=quote_token)
        return self._candidate("question_complete_set_buy", question.outcome_ids, size, cost, edge_usd, edge_bps, legs, actions, residual, question_id=question.question_id, quote_token=quote_token, book_times={book.coin: book.as_of_ms for book in side0_books})

    def _passes_edge(self, edge_bps: Decimal, edge_usd: Decimal) -> bool:
        if self.settings.hip4_edge_threshold_mode == "either":
            return edge_bps >= self.settings.hip4_min_edge_bps or edge_usd >= self.settings.hip4_min_edge_usd
        return edge_bps >= self.settings.hip4_min_edge_bps and edge_usd >= self.settings.hip4_min_edge_usd

    def _candidate(
        self,
        strategy_type: Any,
        outcome_ids: list[int],
        size: Decimal,
        gross: Decimal,
        edge_usd: Decimal,
        edge_bps: Decimal,
        legs: list[ExecutableLeg],
        actions: list[PaperNativeAction],
        residual: dict[str, Decimal],
        *,
        question_id: int | None = None,
        quote_token: str,
        book_times: dict[str, int],
    ) -> Hip4Candidate:
        reject_reasons = [] if not residual else ["residual_inventory"]
        min_book_as_of_ms = min(book_times.values()) if book_times else int(time.time() * 1000)
        scan_at_ms = self.last_scan_at_ms or int(time.time() * 1000)
        max_book_age_ms = scan_at_ms - min_book_as_of_ms
        return Hip4Candidate(
            candidate_id=f"hip4cand_{uuid4().hex}",
            strategy_type=strategy_type,
            mode="shadow",
            question_id=question_id,
            outcome_ids=outcome_ids,
            as_of_ms=min_book_as_of_ms,
            size=size,
            gross_cost_or_proceeds=gross,
            expected_net_edge_usd=edge_usd,
            expected_net_edge_bps=edge_bps,
            min_profit_usd=self.settings.hip4_min_edge_usd,
            fee_stress_bps=self.settings.hip4_fee_stress_bps,
            quote_token=quote_token,
            legs=legs,
            actions=actions,
            residual_inventory=residual,
            proof={
                "residual_inventory_zero": not residual,
                "equal_leg_size": str(size),
                "executable_depth": True,
                "book_as_of_ms_by_coin": book_times,
                "min_book_as_of_ms": min_book_as_of_ms,
                "max_book_age_ms": max_book_age_ms,
            },
            risk_flags=reject_reasons,
            reject_reasons=reject_reasons,
            status="rejected" if reject_reasons else "candidate",
        )

    def _books_fresh(self, books: list[NormalizedOutcomeBook], *, context: dict[str, Any]) -> bool:
        now_ms = self.last_scan_at_ms or int(time.time() * 1000)
        stale = [book.coin for book in books if not book_is_fresh(book, now_ms=now_ms, max_staleness_ms=self.settings.hip4_scan_max_book_staleness_ms)]
        if stale:
            item = {"code": "stale_book", "coins": stale}
            item.update(context)
            self.last_rejects.append(item)
            return False
        return True

    def _question_quote_token(self, question: QuestionSpec, outcomes: dict[int, OutcomeSpec]) -> str | None:
        tokens: set[str] = set()
        missing: list[int] = []
        for outcome_id in question.outcome_ids:
            outcome = outcomes.get(outcome_id)
            if outcome is None or not outcome.quote_token:
                missing.append(outcome_id)
            else:
                tokens.add(str(outcome.quote_token))
        if missing:
            self.last_rejects.append({"code": "quote_token_missing", "question_id": question.question_id, "outcome_ids": missing})
            return None
        if len(tokens) != 1:
            self.last_rejects.append({"code": "mixed_quote_tokens", "question_id": question.question_id, "quote_tokens": sorted(tokens)})
            return None
        return next(iter(tokens))


def _leg(book: NormalizedOutcomeBook, book_side: str, size: Decimal, avg_price: Decimal) -> ExecutableLeg:
    return ExecutableLeg(
        coin=book.coin,
        outcome_id=book.outcome_id,
        side=book.side,
        book_side=book_side,  # type: ignore[arg-type]
        size=size,
        avg_price=avg_price,
        notional=size * avg_price,
    )


def _fee_stress(size: Decimal, bps: Decimal) -> Decimal:
    return size * bps / BPS
