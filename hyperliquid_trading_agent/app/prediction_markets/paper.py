from __future__ import annotations

import hashlib
import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.prediction_markets.catalog import (
    PredictionMarketCatalog,
    quote_matches_required_terms,
    required_query_terms,
)
from hyperliquid_trading_agent.app.prediction_markets.schemas import (
    PredictionMarketBetDraft,
    PredictionMarketBetDraftRequest,
    PredictionMarketFill,
    PredictionMarketLeaderboardRow,
    PredictionMarketPaperAccount,
    PredictionMarketPosition,
    PredictionMarketQuote,
    PredictionMarketSettlement,
    PredictionMarketSettlementRequest,
)


class PredictionMarketPaperService:
    def __init__(self, *, settings: Settings, repository: Any):
        self.settings = settings
        self.repository = repository
        self.catalog = PredictionMarketCatalog(settings=settings, repository=repository)

    async def search(self, query: str = "", *, venue: str | None = None, limit: int = 10) -> list[PredictionMarketQuote]:
        return await self.catalog.search(query, venue=venue, limit=limit)

    async def account(self, *, discord_guild_id: str, discord_user_id: str) -> PredictionMarketPaperAccount:
        return PredictionMarketPaperAccount.model_validate(
            await self.repository.create_or_get_prediction_market_paper_account(
                discord_guild_id=str(discord_guild_id),
                discord_user_id=str(discord_user_id),
                initial_cash_usd=float(self.settings.prediction_market_paper_initial_cash_usd),
            )
        )

    async def portfolio(self, *, discord_guild_id: str, discord_user_id: str) -> dict[str, Any]:
        account = await self.account(discord_guild_id=discord_guild_id, discord_user_id=discord_user_id)
        positions = [PredictionMarketPosition.model_validate(item) for item in await self.repository.list_prediction_market_positions(account_id=account.account_id, limit=100)]
        open_positions = [await self._marked_position(position) for position in positions if position.status == "open"]
        realized = float(account.realized_pnl_usd)
        unrealized = sum(position.unrealized_pnl_usd for position in open_positions)
        open_value = sum(position.current_value_usd for position in open_positions)
        equity = float(account.cash_usd) + open_value
        return {
            "account": account.model_dump(mode="json"),
            "cash_usd": account.cash_usd,
            "open_value_usd": open_value,
            "equity_usd": equity,
            "realized_pnl_usd": realized,
            "unrealized_pnl_usd": unrealized,
            "total_pnl_usd": realized + unrealized,
            "positions": [position.model_dump(mode="json") for position in open_positions],
        }

    async def leaderboard(self, *, discord_guild_id: str, limit: int = 20) -> list[PredictionMarketLeaderboardRow]:
        rows = await self.repository.prediction_market_leaderboard(discord_guild_id=str(discord_guild_id), limit=limit)
        return [PredictionMarketLeaderboardRow.model_validate(item) for item in rows]

    async def draft_bet(self, request: PredictionMarketBetDraftRequest) -> dict[str, Any]:
        self._require_enabled()
        try:
            quote = await self._resolve_quote(request)
        except ValueError as exc:
            suggestions = await self.catalog.search(request.query, limit=5) if request.query else []
            return {
                "error": "no_match",
                "message": str(exc),
                "query": request.query,
                "market_ref": request.market_ref,
                "suggestions": [quote.model_dump(mode="json") for quote in suggestions],
            }
        stake = float(request.stake_usd or self.settings.prediction_market_paper_default_stake_usd)
        if stake <= 0:
            raise ValueError("stake must be positive")
        if stake > float(self.settings.prediction_market_paper_max_stake_usd):
            raise ValueError("stake exceeds prediction-market paper cap")
        price = _side_price(quote, request.side)
        if price <= 0 or price > 1:
            raise ValueError("quote price is not executable for paper betting")
        account = await self.account(discord_guild_id=request.discord_guild_id, discord_user_id=request.discord_user_id)
        shares = stake / price
        now = _now_ms()
        draft = PredictionMarketBetDraft(
            draft_id=f"pmd_{uuid4().hex[:16]}",
            account_id=account.account_id,
            discord_guild_id=request.discord_guild_id,
            discord_user_id=request.discord_user_id,
            venue=quote.venue,
            market_id=quote.market_id,
            outcome_id=quote.outcome_id,
            outcome_name=quote.outcome_name,
            question=quote.question,
            side=request.side,
            stake_usd=stake,
            price=price,
            shares=shares,
            quote_signal_id=quote.signal_id,
            created_at_ms=now,
            expires_at_ms=now + max(1, self.settings.prediction_market_paper_draft_ttl_seconds) * 1000,
            metadata={**request.metadata, "quote": quote.model_dump(mode="json"), "source": request.source, "actor": request.actor},
        )
        await self.repository.create_prediction_market_bet_draft(draft.model_dump(mode="json"))
        return {"draft": draft.model_dump(mode="json"), "quote": quote.model_dump(mode="json"), "account": account.model_dump(mode="json")}

    async def confirm_draft(self, draft_id: str, *, actor: str = "discord") -> dict[str, Any]:
        self._require_enabled()
        draft = PredictionMarketBetDraft.model_validate(await self._required_draft(draft_id))
        now = _now_ms()
        if draft.status != "new":
            raise ValueError("prediction-market draft is not confirmable")
        if draft.expires_at_ms <= now:
            await self.repository.update_prediction_market_bet_draft(draft.model_copy(update={"status": "expired"}).model_dump(mode="json"))
            raise ValueError("prediction-market draft expired")
        account = PredictionMarketPaperAccount.model_validate(await self.repository.get_prediction_market_paper_account(draft.account_id))
        if account.cash_usd < draft.stake_usd:
            raise ValueError("insufficient prediction-market paper cash")
        position = PredictionMarketPosition(
            position_id=f"pmp_{uuid4().hex[:16]}",
            account_id=account.account_id,
            discord_guild_id=draft.discord_guild_id,
            discord_user_id=draft.discord_user_id,
            draft_id=draft.draft_id,
            venue=draft.venue,
            market_id=draft.market_id,
            outcome_id=draft.outcome_id,
            outcome_name=draft.outcome_name,
            question=draft.question,
            side=draft.side,
            shares=draft.shares,
            avg_entry_price=draft.price,
            cost_usd=draft.stake_usd,
            mark_price=draft.price,
            current_value_usd=draft.stake_usd,
            opened_at_ms=now,
            metadata={"confirmed_by": actor, "draft": draft.model_dump(mode="json")},
        )
        fill = PredictionMarketFill(
            fill_id=f"pmf_{uuid4().hex[:16]}",
            account_id=account.account_id,
            position_id=position.position_id,
            draft_id=draft.draft_id,
            action="open",
            venue=draft.venue,
            market_id=draft.market_id,
            outcome_id=draft.outcome_id,
            shares=draft.shares,
            price=draft.price,
            cash_delta_usd=-draft.stake_usd,
            created_at_ms=now,
            metadata={"actor": actor},
        )
        account.cash_usd -= draft.stake_usd
        await self.repository.update_prediction_market_paper_account(account.model_dump(mode="json"))
        await self.repository.create_prediction_market_position(position.model_dump(mode="json"))
        await self.repository.record_prediction_market_fill(fill.model_dump(mode="json"))
        confirmed = draft.model_copy(update={"status": "confirmed", "confirmed_at_ms": now, "metadata": {**draft.metadata, "confirmed_by": actor}})
        await self.repository.update_prediction_market_bet_draft(confirmed.model_dump(mode="json"))
        return {
            "draft": confirmed.model_dump(mode="json"),
            "position": position.model_dump(mode="json"),
            "fill": fill.model_dump(mode="json"),
            "account": account.model_dump(mode="json"),
        }

    async def cancel_draft(self, draft_id: str, *, actor: str = "discord", reason: str = "cancelled") -> dict[str, Any]:
        draft = PredictionMarketBetDraft.model_validate(await self._required_draft(draft_id))
        if draft.status != "new":
            raise ValueError("prediction-market draft is not cancellable")
        updated = draft.model_copy(update={"status": "cancelled", "cancelled_at_ms": _now_ms(), "metadata": {**draft.metadata, "cancelled_by": actor, "cancel_reason": reason}})
        await self.repository.update_prediction_market_bet_draft(updated.model_dump(mode="json"))
        return {"draft": updated.model_dump(mode="json")}

    async def close_position(self, position_ref: str, *, actor: str = "discord", reason: str = "manual") -> dict[str, Any]:
        position = PredictionMarketPosition.model_validate(await self._required_position(position_ref))
        if position.status != "open":
            raise ValueError("prediction-market position is not open")
        quote = await self._quote_for_position(position)
        price = _side_close_price(quote, position.side) if quote is not None else float(position.mark_price or position.avg_entry_price)
        return await self._close_with_price(position, price=price, actor=actor, reason=reason)

    async def apply_settlement(self, request: PredictionMarketSettlementRequest) -> dict[str, Any]:
        now = _now_ms()
        settlement = PredictionMarketSettlement(
            settlement_id=_settlement_id(request.venue, request.market_id, request.outcome_id),
            venue=request.venue.lower(),
            market_id=request.market_id,
            outcome_id=request.outcome_id,
            settlement_fraction=float(request.settlement_fraction),
            source=request.source,
            applied_by=request.actor,
            created_at_ms=now,
            metadata=request.metadata,
        )
        await self.repository.upsert_prediction_market_settlement(settlement.model_dump(mode="json"))
        positions = [
            PredictionMarketPosition.model_validate(item)
            for item in await self.repository.list_prediction_market_positions(
                venue=settlement.venue,
                market_id=settlement.market_id,
                outcome_id=settlement.outcome_id,
                status="open",
                limit=1000,
            )
        ]
        settled = []
        for position in positions:
            fraction = settlement.settlement_fraction if position.side == "yes" else 1.0 - settlement.settlement_fraction
            settled.append((await self._settle_position(position, settlement=settlement, payout_fraction=fraction)).model_dump(mode="json"))
        return {"settlement": settlement.model_dump(mode="json"), "settled_positions": settled, "count": len(settled)}

    async def settlement_sweep(self) -> dict[str, Any]:
        signals = await self.repository.list_prediction_market_signals(limit=500)
        applied = []
        seen: set[tuple[str, str, str | None]] = set()
        for signal in signals:
            if str(signal.get("status") or "").lower() != "settled":
                continue
            probability = _optional_float(signal.get("implied_probability"))
            if probability is None:
                continue
            key = (str(signal.get("venue") or "").lower(), str(signal.get("market_id") or ""), str(signal.get("outcome_id")) if signal.get("outcome_id") is not None else None)
            if key in seen:
                continue
            seen.add(key)
            result = await self.apply_settlement(
                PredictionMarketSettlementRequest(
                    venue=key[0],
                    market_id=key[1],
                    outcome_id=key[2],
                    settlement_fraction=probability,
                    source="provider",
                    actor="settlement_sweep",
                    metadata={"signal_id": signal.get("signal_id")},
                )
            )
            applied.append(result)
        return {"settlements": applied, "count": len(applied)}

    async def _marked_position(self, position: PredictionMarketPosition) -> PredictionMarketPosition:
        quote = await self._quote_for_position(position)
        if quote is None:
            return position
        price = _side_mark_price(quote, position.side)
        value = position.shares * price
        updated = position.model_copy(update={"mark_price": price, "current_value_usd": value, "unrealized_pnl_usd": value - position.cost_usd})
        await self.repository.update_prediction_market_position(updated.model_dump(mode="json"))
        return updated

    async def _close_with_price(self, position: PredictionMarketPosition, *, price: float, actor: str, reason: str) -> dict[str, Any]:
        now = _now_ms()
        proceeds = position.shares * max(0.0, min(1.0, price))
        realized = proceeds - position.cost_usd
        account = PredictionMarketPaperAccount.model_validate(await self.repository.get_prediction_market_paper_account(position.account_id))
        account.cash_usd += proceeds
        account.realized_pnl_usd += realized
        updated = position.model_copy(
            update={
                "status": "closed",
                "mark_price": price,
                "current_value_usd": proceeds,
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": 0.0,
                "closed_at_ms": now,
                "result": "closed",
                "metadata": {**position.metadata, "closed_by": actor, "close_reason": reason},
            }
        )
        fill = PredictionMarketFill(
            fill_id=f"pmf_{uuid4().hex[:16]}",
            account_id=position.account_id,
            position_id=position.position_id,
            draft_id=position.draft_id,
            action="close",
            venue=position.venue,
            market_id=position.market_id,
            outcome_id=position.outcome_id,
            shares=position.shares,
            price=price,
            cash_delta_usd=proceeds,
            realized_pnl_usd=realized,
            created_at_ms=now,
            metadata={"actor": actor, "reason": reason},
        )
        await self.repository.update_prediction_market_paper_account(account.model_dump(mode="json"))
        await self.repository.update_prediction_market_position(updated.model_dump(mode="json"))
        await self.repository.record_prediction_market_fill(fill.model_dump(mode="json"))
        return {"position": updated.model_dump(mode="json"), "fill": fill.model_dump(mode="json"), "account": account.model_dump(mode="json")}

    async def _settle_position(self, position: PredictionMarketPosition, *, settlement: PredictionMarketSettlement, payout_fraction: float) -> PredictionMarketPosition:
        now = _now_ms()
        fraction = max(0.0, min(1.0, payout_fraction))
        payout = position.shares * fraction
        realized = payout - position.cost_usd
        account = PredictionMarketPaperAccount.model_validate(await self.repository.get_prediction_market_paper_account(position.account_id))
        account.cash_usd += payout
        account.realized_pnl_usd += realized
        result = "push" if abs(realized) < 0.000001 else "won" if realized > 0 else "lost"
        updated = position.model_copy(
            update={
                "status": "settled",
                "mark_price": fraction,
                "current_value_usd": payout,
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": 0.0,
                "settled_at_ms": now,
                "result": result,
                "metadata": {**position.metadata, "settlement_id": settlement.settlement_id, "settlement_source": settlement.source},
            }
        )
        fill = PredictionMarketFill(
            fill_id=f"pmf_{uuid4().hex[:16]}",
            account_id=position.account_id,
            position_id=position.position_id,
            draft_id=position.draft_id,
            action="settle",
            venue=position.venue,
            market_id=position.market_id,
            outcome_id=position.outcome_id,
            shares=position.shares,
            price=fraction,
            cash_delta_usd=payout,
            realized_pnl_usd=realized,
            created_at_ms=now,
            metadata={"settlement_id": settlement.settlement_id},
        )
        await self.repository.update_prediction_market_paper_account(account.model_dump(mode="json"))
        await self.repository.update_prediction_market_position(updated.model_dump(mode="json"))
        await self.repository.record_prediction_market_fill(fill.model_dump(mode="json"))
        return updated

    async def _quote_for_position(self, position: PredictionMarketPosition) -> PredictionMarketQuote | None:
        ref = position.metadata.get("quote", {}) if isinstance(position.metadata, dict) else {}
        if not ref and isinstance(position.metadata.get("draft") if isinstance(position.metadata, dict) else None, dict):
            draft_data = position.metadata.get("draft")  # type: ignore[assignment]
            ref = ((draft_data or {}).get("metadata") or {}).get("quote", {}) if isinstance(draft_data, dict) else {}
        quote_id = str(ref.get("quote_id") or "") if isinstance(ref, dict) else ""
        if quote_id:
            resolved = await self.catalog.resolve(quote_id)
            if resolved is not None:
                return resolved
        matches = await self.catalog.search(f"{position.venue} {position.market_id} {position.outcome_name}", venue=position.venue, limit=20)
        for quote in matches:
            if quote.market_id == position.market_id and quote.outcome_id == position.outcome_id:
                return quote
        return None

    async def _resolve_quote(self, request: PredictionMarketBetDraftRequest) -> PredictionMarketQuote:
        if request.market_ref:
            quote = await self.catalog.resolve(request.market_ref)
            if quote is not None:
                return quote
            raise ValueError("prediction-market quote not found")
        if not request.query.strip() or not required_query_terms(request.query):
            raise ValueError("missing prediction-market query")
        matches = await self.catalog.search(request.query, limit=8)
        if not matches:
            raise ValueError("no prediction market matched the request")
        matches = [quote for quote in matches if quote_matches_required_terms(quote, request.query)]
        if not matches:
            raise ValueError("no prediction market matched all request terms")
        if len(matches) > 1 and _rank_collision(matches):
            raise ValueError("multiple prediction markets matched; use `pm search` and include the market id")
        return matches[0]

    async def _required_draft(self, draft_id: str) -> dict[str, Any]:
        draft = await self.repository.get_prediction_market_bet_draft(draft_id)
        if draft is None:
            raise KeyError("prediction-market draft not found")
        return draft

    async def _required_position(self, position_ref: str) -> dict[str, Any]:
        position = await self.repository.get_prediction_market_position(position_ref)
        if position is None:
            raise KeyError("prediction-market position not found")
        return position

    def _require_enabled(self) -> None:
        if not self.settings.prediction_market_paper_enabled:
            raise RuntimeError("PREDICTION_MARKET_PAPER_ENABLED is false")


def _side_price(quote: PredictionMarketQuote, side: str) -> float:
    if side == "no":
        return max(0.000001, min(1.0, 1.0 - quote.price))
    return quote.price


def _side_mark_price(quote: PredictionMarketQuote, side: str) -> float:
    price = quote.implied_probability if quote.implied_probability is not None else quote.price
    if side == "no":
        return max(0.0, min(1.0, 1.0 - price))
    return max(0.0, min(1.0, price))


def _side_close_price(quote: PredictionMarketQuote, side: str) -> float:
    price = quote.best_bid if quote.best_bid is not None else quote.implied_probability if quote.implied_probability is not None else quote.price
    if side == "no":
        return max(0.0, min(1.0, 1.0 - price))
    return max(0.0, min(1.0, price))


def _rank_collision(matches: list[PredictionMarketQuote]) -> bool:
    if len(matches) < 2:
        return False
    top, second = matches[0], matches[1]
    if top.venue == "hip4":
        return False
    return second.venue == "hip4" or top.market_id != second.market_id


def _settlement_id(venue: str, market_id: str, outcome_id: str | None) -> str:
    digest = hashlib.sha1(f"{venue.lower()}:{market_id}:{outcome_id or ''}".encode()).hexdigest()[:16]
    return f"pms_{digest}"


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
