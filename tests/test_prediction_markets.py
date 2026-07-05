from __future__ import annotations

import time

import anyio
import pytest

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.discord_bot import DiscordContext, DiscordTradingBot
from hyperliquid_trading_agent.app.prediction_markets.catalog import PredictionMarketCatalog
from hyperliquid_trading_agent.app.prediction_markets.discord import (
    format_prediction_market_result,
    format_prediction_market_search,
    parse_prediction_market_discord_command,
    parse_prediction_market_reaction,
)
from hyperliquid_trading_agent.app.prediction_markets.paper import PredictionMarketPaperService
from hyperliquid_trading_agent.app.prediction_markets.schemas import (
    PredictionMarketBetDraftRequest,
    PredictionMarketSettlementRequest,
)


def _signal(**overrides):
    now = int(time.time() * 1000)
    data = {
        "signal_id": "sig_1",
        "venue": "hip4",
        "market_id": "m1",
        "question": "Will BTC close above 100k?",
        "outcome_id": "yes",
        "outcome_name": "YES",
        "symbols": ["BTC"],
        "topics": ["prediction_market", "bitcoin"],
        "implied_probability": 0.4,
        "best_bid": 0.39,
        "best_ask": 0.4,
        "liquidity_usd": 10_000,
        "volume_usd": 20_000,
        "status": "open",
        "as_of_ms": now,
        "metadata": {},
    }
    data.update(overrides)
    return data


class FakePredictionRepo:
    enabled = True

    def __init__(self, signals=None):
        self.signals = signals or [_signal()]
        self.accounts = {}
        self.drafts = {}
        self.positions = {}
        self.fills = []
        self.settlements = {}
        self.commands = {}
        self.enqueued = []

    async def list_prediction_market_signals(self, limit=100, venue=None, symbol=None):
        items = [item for item in self.signals if venue is None or item["venue"] == venue]
        return items[:limit]

    async def create_or_get_prediction_market_paper_account(self, *, discord_guild_id, discord_user_id, initial_cash_usd):
        key = (discord_guild_id, discord_user_id)
        if key not in self.accounts:
            self.accounts[key] = {
                "account_id": f"acct_{discord_guild_id}_{discord_user_id}",
                "discord_guild_id": discord_guild_id,
                "discord_user_id": discord_user_id,
                "status": "active",
                "initial_cash_usd": initial_cash_usd,
                "cash_usd": initial_cash_usd,
                "realized_pnl_usd": 0.0,
                "metadata": {},
            }
        return dict(self.accounts[key])

    async def get_prediction_market_paper_account(self, account_id):
        return next((dict(item) for item in self.accounts.values() if item["account_id"] == account_id), None)

    async def update_prediction_market_paper_account(self, account):
        key = (account["discord_guild_id"], account["discord_user_id"])
        self.accounts[key] = dict(account)

    async def create_prediction_market_bet_draft(self, draft):
        self.drafts[draft["draft_id"]] = dict(draft)

    async def get_prediction_market_bet_draft(self, draft_id):
        return self.drafts.get(draft_id)

    async def update_prediction_market_bet_draft(self, draft):
        self.drafts[draft["draft_id"]] = dict(draft)

    async def create_prediction_market_position(self, position):
        self.positions[position["position_id"]] = dict(position)

    async def get_prediction_market_position(self, position_id):
        return self.positions.get(position_id)

    async def update_prediction_market_position(self, position):
        self.positions[position["position_id"]] = dict(position)

    async def list_prediction_market_positions(self, **filters):
        items = list(self.positions.values())
        for key in ("account_id", "discord_guild_id", "discord_user_id", "venue", "market_id", "outcome_id", "status"):
            value = filters.get(key)
            if value is not None:
                items = [item for item in items if item.get(key) == value]
        return [dict(item) for item in items[: filters.get("limit", 100)]]

    async def record_prediction_market_fill(self, fill):
        self.fills.append(dict(fill))

    async def upsert_prediction_market_settlement(self, settlement):
        self.settlements[settlement["settlement_id"]] = dict(settlement)

    async def prediction_market_leaderboard(self, *, discord_guild_id, limit=20):
        rows = []
        for account in self.accounts.values():
            if account["discord_guild_id"] != discord_guild_id:
                continue
            positions = [item for item in self.positions.values() if item["account_id"] == account["account_id"]]
            open_positions = [item for item in positions if item["status"] == "open"]
            unrealized = sum(item.get("unrealized_pnl_usd", 0) for item in open_positions)
            total = account["realized_pnl_usd"] + unrealized
            rows.append(
                {
                    "discord_guild_id": discord_guild_id,
                    "discord_user_id": account["discord_user_id"],
                    "account_id": account["account_id"],
                    "cash_usd": account["cash_usd"],
                    "open_value_usd": sum(item.get("current_value_usd", 0) for item in open_positions),
                    "equity_usd": account["cash_usd"],
                    "realized_pnl_usd": account["realized_pnl_usd"],
                    "unrealized_pnl_usd": unrealized,
                    "total_pnl_usd": total,
                    "roi_pct": total / account["initial_cash_usd"] * 100,
                    "won": len([item for item in positions if item.get("result") == "won"]),
                    "lost": len([item for item in positions if item.get("result") == "lost"]),
                    "open_positions": len(open_positions),
                    "settled_positions": len([item for item in positions if item.get("status") == "settled"]),
                }
            )
        return rows[:limit]

    async def enqueue_worker_command(self, *, target_role, command_type, payload=None, requested_by=None, idempotency_key=None):
        command_id = f"cmd_{len(self.commands) + 1}"
        self.enqueued.append((target_role, command_type, payload or {}))
        if command_type == "prediction_market_bet_confirm":
            result = {
                "position": {
                    "position_id": "pmp_1",
                    "venue": "hip4",
                    "market_id": "739",
                    "outcome_id": "739:0",
                    "outcome_name": "Brazil",
                    "question": "World Cup Round of 16: Brazil vs Norway",
                    "side": "yes",
                    "cost_usd": 100,
                    "avg_entry_price": 0.4,
                    "shares": 250,
                }
            }
        elif command_type == "prediction_market_bet_cancel":
            result = {"draft": {"draft_id": (payload or {}).get("draft_id") or "pmd_1", "outcome_name": "Brazil", "question": "World Cup Round of 16: Brazil vs Norway"}}
        else:
            result = {
                "draft": {
                    "draft_id": "pmd_1",
                    "venue": "hip4",
                    "side": "yes",
                    "stake_usd": 100,
                    "price": 0.4,
                    "outcome_name": "Brazil",
                    "question": "World Cup Round of 16: Brazil vs Norway",
                }
            }
        self.commands[command_id] = {"command_id": command_id, "status": "completed", "result": {"result": result}}
        return self.commands[command_id]

    async def get_worker_command(self, command_id):
        return self.commands.get(command_id)


class FakeHyperliquidHip4:
    def __init__(self):
        self.now = int(time.time() * 1000)

    async def outcome_meta(self):
        return {
            "outcomes": [
                {
                    "outcome": 739,
                    "name": "World Cup Round of 16: Brazil vs Norway",
                    "description": "metadata=category:sports|subCategory:football",
                    "sideSpecs": [{"name": "Brazil"}, {"name": "Norway"}],
                    "quoteToken": "USDC",
                }
            ],
            "questions": [],
        }

    async def l2_book(self, coin):
        if coin == "#7390":
            return self._book(coin, bid="0.69", ask="0.70")
        if coin == "#7391":
            return self._book(coin, bid="0.30", ask="0.31")
        raise KeyError(coin)

    def _book(self, coin, *, bid, ask):
        return {
            "coin": coin,
            "time": self.now,
            "levels": [
                [{"px": bid, "sz": "100", "n": 1}],
                [{"px": ask, "sz": "100", "n": 1}],
            ],
        }


def test_prediction_market_discord_parser_natural_bet_and_search():
    search = parse_prediction_market_discord_command("pm search BTC 100k")
    bet = parse_prediction_market_discord_command("bet $50 yes on BTC above 100k prediction market")
    sports_bet = parse_prediction_market_discord_command("bet win on Brazil against Norway")
    pm_sports_bet = parse_prediction_market_discord_command("pm win brazil against norway")
    yes_sports_bet = parse_prediction_market_discord_command("bet yes on Brazil against Norway")
    stake_sports_bet = parse_prediction_market_discord_command("bet win brazil vs norway $100")
    more_sports_bet = parse_prediction_market_discord_command("buy more brazil win vs norway")
    hip4_ref_bet = parse_prediction_market_discord_command("bet yes on #7390")

    assert search is not None and search.action == "search" and search.query == "BTC 100k"
    assert bet is not None and bet.action == "draft"
    assert bet.side == "yes"
    assert bet.stake_usd == 50
    assert "BTC" in bet.query
    assert sports_bet is not None and sports_bet.action == "draft"
    assert sports_bet.side == "yes"
    assert sports_bet.stake_usd is None
    assert "Brazil" in sports_bet.query
    assert "Norway" in sports_bet.query
    assert pm_sports_bet is not None and pm_sports_bet.action == "draft"
    assert pm_sports_bet.side == "yes"
    assert pm_sports_bet.query == "brazil norway"
    assert yes_sports_bet is not None and yes_sports_bet.action == "draft"
    assert yes_sports_bet.side == "yes"
    assert "Brazil" in yes_sports_bet.query
    assert "Norway" in yes_sports_bet.query
    assert stake_sports_bet is not None and stake_sports_bet.action == "draft"
    assert stake_sports_bet.side == "yes"
    assert stake_sports_bet.stake_usd == 100
    assert stake_sports_bet.query == "brazil norway"
    assert more_sports_bet is not None and more_sports_bet.action == "draft"
    assert more_sports_bet.side == "yes"
    assert more_sports_bet.query == "brazil norway"
    assert hip4_ref_bet is not None and hip4_ref_bet.action == "draft"
    assert hip4_ref_bet.market_ref == "#7390"
    assert hip4_ref_bet.query == ""


def test_prediction_market_discord_parser_short_confirm_cancel_and_bets_alias():
    referenced = type("Message", (), {"content": "Drafted: **Brazil** on World Cup Round of 16: Brazil vs Norway\n| draft `pmd_8e981c4a80024f37`"})()

    confirm = parse_prediction_market_discord_command("yes", referenced_message=referenced)
    ok = parse_prediction_market_discord_command("ok", referenced_message=referenced)
    cancel = parse_prediction_market_discord_command("no", referenced_message=referenced)
    bets = parse_prediction_market_discord_command("bets")
    reaction = parse_prediction_market_reaction("✅", referenced_message=referenced)

    assert confirm is not None and confirm.action == "confirm" and confirm.draft_id == "pmd_8e981c4a80024f37"
    assert ok is not None and ok.action == "confirm" and ok.draft_id == "pmd_8e981c4a80024f37"
    assert cancel is not None and cancel.action == "cancel" and cancel.draft_id == "pmd_8e981c4a80024f37"
    assert bets is not None and bets.action == "portfolio"
    assert reaction is not None and reaction.action == "confirm" and reaction.draft_id == "pmd_8e981c4a80024f37"


@pytest.mark.asyncio
async def test_prediction_market_catalog_ranks_hip4_before_other_venues():
    repo = FakePredictionRepo(
        [
            _signal(signal_id="poly", venue="polymarket", market_id="p1", liquidity_usd=1_000_000),
            _signal(signal_id="hip4", venue="hip4", market_id="h1", liquidity_usd=100),
        ]
    )
    catalog = PredictionMarketCatalog(settings=Settings(environment="test", _env_file=None), repository=repo)

    quotes = await catalog.search("BTC 100k", limit=2)

    assert [quote.venue for quote in quotes] == ["hip4", "polymarket"]


@pytest.mark.asyncio
async def test_prediction_market_catalog_strict_search_filters_outrights_from_match_query():
    repo = FakePredictionRepo(
        [
            _signal(signal_id="usa_match", market_id="m_usa", question="World Cup Round of 16: USA vs Belgium", outcome_id="usa", outcome_name="USA", liquidity_usd=100_000),
            _signal(signal_id="belgium_match", market_id="m_bel", question="World Cup Round of 16: USA vs Belgium", outcome_id="belgium", outcome_name="Belgium", liquidity_usd=90_000),
            _signal(signal_id="usa_outright", market_id="usa_wc", question="Will USA win the 2026 FIFA World Cup?", outcome_id="yes", outcome_name="Yes", liquidity_usd=2_000_000),
            _signal(signal_id="belgium_outright", market_id="bel_wc", question="Will Belgium win the 2026 FIFA World Cup?", outcome_id="yes", outcome_name="Yes", liquidity_usd=1_000_000),
        ]
    )
    catalog = PredictionMarketCatalog(settings=Settings(environment="test", _env_file=None), repository=repo)

    quotes = await catalog.search("USA Belgium", limit=10)
    formatted = format_prediction_market_search(quotes)

    assert [quote.outcome_name for quote in quotes] == ["USA", "Belgium"]
    assert "Will USA win the 2026 FIFA World Cup?" not in formatted
    assert "**1. World Cup Round of 16: USA vs Belgium**" in formatted
    assert "- **USA** @ `" in formatted
    assert "- **Belgium** @ `" in formatted
    assert "id `pm:" in formatted


@pytest.mark.asyncio
async def test_prediction_market_catalog_searches_live_hip4_books_by_match_text():
    repo = FakePredictionRepo([])
    catalog = PredictionMarketCatalog(settings=Settings(environment="test", _env_file=None), repository=repo, hyperliquid=FakeHyperliquidHip4())

    brazil_first = await catalog.search("Brazil against Norway", limit=2)
    norway_first = await catalog.search("Norway against Brazil", limit=2)
    by_ref = await catalog.resolve("#7391")
    by_asset_id = await catalog.resolve("100007391")
    by_outcome_ref = await catalog.resolve("hip4:739")

    assert [quote.outcome_name for quote in brazil_first[:2]] == ["Brazil", "Norway"]
    assert brazil_first[0].market_id == "739"
    assert brazil_first[0].outcome_id == "739:0"
    assert brazil_first[0].price == pytest.approx(0.70)
    assert [quote.outcome_name for quote in norway_first[:2]] == ["Norway", "Brazil"]
    assert by_ref is not None
    assert by_ref.outcome_name == "Norway"
    assert by_ref.price == pytest.approx(0.31)
    assert by_asset_id is not None and by_asset_id.outcome_name == "Norway"
    assert by_outcome_ref is not None and by_outcome_ref.outcome_name == "Brazil"


@pytest.mark.asyncio
async def test_prediction_market_paper_draft_confirm_settle_leaderboard():
    repo = FakePredictionRepo()
    service = PredictionMarketPaperService(
        settings=Settings(environment="test", prediction_market_paper_enabled=True, prediction_market_paper_initial_cash_usd=10_000, prediction_market_paper_default_stake_usd=100, _env_file=None),
        repository=repo,
    )

    draft = await service.draft_bet(PredictionMarketBetDraftRequest(discord_guild_id="g1", discord_user_id="u1", side="yes", query="BTC 100k"))
    confirmed = await service.confirm_draft(draft["draft"]["draft_id"], actor="u1")
    settled = await service.apply_settlement(PredictionMarketSettlementRequest(venue="hip4", market_id="m1", outcome_id="yes", settlement_fraction=1.0, source="admin", actor="admin"))
    leaderboard = await service.leaderboard(discord_guild_id="g1")

    assert confirmed["position"]["status"] == "open"
    assert repo.accounts[("g1", "u1")]["cash_usd"] == pytest.approx(10_150.0)
    assert settled["count"] == 1
    assert leaderboard[0].won == 1
    assert leaderboard[0].total_pnl_usd == pytest.approx(150.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected_stake"),
    [
        ("bet win on Brazil against Norway", 70),
        ("bet win brazil vs norway $100", 100),
        ("buy more brazil win vs norway", 70),
    ],
)
async def test_prediction_market_paper_drafts_live_hip4_match_side(prompt, expected_stake):
    repo = FakePredictionRepo([])
    service = PredictionMarketPaperService(
        settings=Settings(environment="test", prediction_market_paper_enabled=True, prediction_market_paper_default_stake_usd=70, _env_file=None),
        repository=repo,
        hyperliquid=FakeHyperliquidHip4(),
    )
    command = parse_prediction_market_discord_command(prompt)
    assert command is not None

    result = await service.draft_bet(
        PredictionMarketBetDraftRequest(
            discord_guild_id="g1",
            discord_user_id="u1",
            side=command.side,
            query=command.query,
            market_ref=command.market_ref,
            stake_usd=command.stake_usd,
        )
    )

    assert "error" not in result
    assert result["draft"]["market_id"] == "739"
    assert result["draft"]["outcome_id"] == "739:0"
    assert result["draft"]["outcome_name"] == "Brazil"
    assert result["draft"]["price"] == pytest.approx(0.70)
    assert result["draft"]["stake_usd"] == pytest.approx(expected_stake)


@pytest.mark.asyncio
async def test_prediction_market_paper_no_match_returns_related_markets_without_draft():
    repo = FakePredictionRepo(
        [
            _signal(signal_id="brazil_wc", market_id="br_wc", question="Will Brazil win the 2026 FIFA World Cup?", outcome_name="Yes", topics=["soccer", "world cup"]),
            _signal(signal_id="norway_wc", market_id="no_wc", question="Will Norway win the 2026 FIFA World Cup?", outcome_name="Yes", topics=["soccer", "world cup"]),
        ]
    )
    service = PredictionMarketPaperService(
        settings=Settings(environment="test", prediction_market_paper_enabled=True, _env_file=None),
        repository=repo,
    )
    command = parse_prediction_market_discord_command("bet yes on Brazil against Norway")
    assert command is not None

    result = await service.draft_bet(
        PredictionMarketBetDraftRequest(
            discord_guild_id="g1",
            discord_user_id="u1",
            side=command.side,
            query=command.query,
        )
    )
    message = format_prediction_market_result(command, {"result": result})

    assert result["error"] == "no_match"
    assert len(result["suggestions"]) == 2
    assert repo.drafts == {}
    assert "No prediction market matched" in message
    assert "Related markets:" in message


def test_discord_prediction_market_draft_queues_for_authorized_non_admin_user():
    async def run():
        repo = FakePredictionRepo()
        runner = type("Runner", (), {"repository": repo})()
        bot = DiscordTradingBot(settings=Settings(environment="test", prediction_market_paper_enabled=True, _env_file=None), runner=runner)
        command = parse_prediction_market_discord_command("bet $100 yes on BTC above 100k prediction market")
        assert command is not None

        response = await bot._handle_prediction_market_command(command, context=DiscordContext(guild_id=42, channel_id=7, author_id=11), user_id="11", role_ids=set())

        assert "Drafted:" in response
        assert "World Cup Round of 16: Brazil vs Norway" in response
        assert "No live trade was placed" not in response
        assert repo.enqueued[0][1] == "prediction_market_bet_draft"
        assert repo.enqueued[0][2]["discord_guild_id"] == "42"
        assert repo.enqueued[0][2]["discord_user_id"] == "11"

    anyio.run(run)


def test_discord_prediction_market_short_reply_confirms_referenced_draft():
    async def run():
        repo = FakePredictionRepo()
        runner = type("Runner", (), {"repository": repo})()
        bot = DiscordTradingBot(settings=Settings(environment="test", prediction_market_paper_enabled=True, _env_file=None), runner=runner)
        referenced = type("Message", (), {"content": "Drafted: **Brazil** on World Cup Round of 16: Brazil vs Norway\n| draft `pmd_8e981c4a80024f37`"})()
        command = parse_prediction_market_discord_command("ok", referenced_message=referenced)
        assert command is not None

        response = await bot._handle_prediction_market_command(command, context=DiscordContext(guild_id=42, channel_id=7, author_id=11), user_id="11", role_ids=set())

        assert response.startswith("Confirmed: **Brazil** on World Cup Round of 16: Brazil vs Norway")
        assert "position `pmp_1`" in response
        assert "No live trade was placed" not in response
        assert repo.enqueued[0][1] == "prediction_market_bet_confirm"
        assert repo.enqueued[0][2]["draft_id"] == "pmd_8e981c4a80024f37"

    anyio.run(run)
