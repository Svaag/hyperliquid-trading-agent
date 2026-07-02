from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

import pytest
from PIL import Image

from hyperliquid_trading_agent.app.charting import ChartCommand, ChartingService, ChartResult, parse_chart_command
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.discord_bot import DiscordContext, DiscordTradingBot
from hyperliquid_trading_agent.app.tradfi.schemas import Bar


def test_parse_chart_command_exact_prefix():
    assert parse_chart_command(";;TSLA h") == ChartCommand(symbol="TSLA", horizon="h")
    assert parse_chart_command(";;spcx d") == ChartCommand(symbol="SPCX", horizon="d")
    assert parse_chart_command(";; xyz:msft D") == ChartCommand(symbol="xyz:MSFT", horizon="d")
    assert parse_chart_command(";;TSLA q") is None
    assert parse_chart_command(";;help") is None
    assert parse_chart_command("<@123> ;;TSLA h") is None


class _FakeTradFi:
    class _Provider:
        name = "fake"

    provider = _Provider()

    def __init__(self, bars: list[Bar]):
        self.bars = bars
        self.calls: list[tuple[str, str, int, int | None]] = []

    async def get_bars(self, symbol: str, timeframe: str = "1d", lookback_hours: int = 24, limit: int | None = None) -> list[Bar]:
        self.calls.append((symbol, timeframe, lookback_hours, limit))
        return self.bars[-limit:] if limit else self.bars


class _FakeHyperliquid:
    def __init__(self):
        self.calls: list[tuple[str, str, int, int]] = []

    async def candle_snapshot(self, coin: str, interval: str, start_time: int, end_time: int) -> list[dict[str, Any]]:
        self.calls.append((coin, interval, start_time, end_time))
        return []


def _bars(count: int = 80) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for idx in range(count):
        base = 100 + idx * 0.75
        out.append(
            Bar(
                symbol="TSLA",
                timestamp=start + timedelta(days=idx),
                open=base,
                high=base + 2,
                low=base - 2,
                close=base + 1,
                volume=1_000_000 + idx * 10_000,
                timeframe="1Day",
            )
        )
    return out


@pytest.mark.asyncio
async def test_charting_service_renders_png_from_tradfi_bars():
    tradfi = _FakeTradFi(_bars())
    service = ChartingService(settings=Settings(), tradfi=tradfi)  # type: ignore[arg-type]

    result = await service.render(ChartCommand(symbol="TSLA", horizon="d"))

    assert result.filename == "TSLA_d.png"
    assert result.image_png.startswith(b"\x89PNG")
    assert "TSLA daily chart" in result.content
    assert "Informational only; no trade was placed." in result.content
    assert tradfi.calls[0][1] == "1d"


@pytest.mark.asyncio
async def test_charting_service_uses_hyperliquid_dark_theme():
    tradfi = _FakeTradFi(_bars())
    service = ChartingService(settings=Settings(), tradfi=tradfi)  # type: ignore[arg-type]

    result = await service.render(ChartCommand(symbol="TSLA", horizon="d"))
    image = Image.open(BytesIO(result.image_png)).convert("RGB")

    assert image.getpixel((5, 5)) == (7, 16, 14)
    assert image.getpixel((image.width // 2, image.height // 2)) == (12, 26, 23)


@pytest.mark.asyncio
async def test_charting_service_no_data_returns_no_trade_message():
    service = ChartingService(settings=Settings(), tradfi=_FakeTradFi([]))  # type: ignore[arg-type]

    result = await service.render(ChartCommand(symbol="TSLA", horizon="d"))

    assert result.image_png == b""
    assert "No usable candle data" in result.content
    assert "No trade was placed" in result.content


@pytest.mark.asyncio
async def test_charting_service_does_not_fallback_to_hyperliquid_for_equities():
    hyperliquid = _FakeHyperliquid()
    service = ChartingService(settings=Settings(), hyperliquid=hyperliquid, tradfi=_FakeTradFi([]))  # type: ignore[arg-type]

    result = await service.render(ChartCommand(symbol="TSLA", horizon="h"))

    assert result.image_png == b""
    assert hyperliquid.calls == []


class _FakeChartingService:
    def __init__(self):
        self.commands: list[ChartCommand] = []

    async def render(self, command: ChartCommand) -> ChartResult:
        self.commands.append(command)
        return ChartResult(content="chart rendered", image_png=b"png-bytes", filename="TSLA_h.png")


class _FakeMessage:
    def __init__(self):
        self.replies: list[dict[str, Any]] = []
        self.channel = object()

    async def reply(self, *args, **kwargs):
        self.replies.append({"args": args, "kwargs": kwargs})


@pytest.mark.asyncio
async def test_discord_chart_handler_replies_with_attachment_without_runner():
    charting = _FakeChartingService()
    bot = DiscordTradingBot(
        settings=Settings(discord_chart_command_enabled=True, discord_allowed_channel_ids="42"),
        runner=None,
        charting_service=charting,  # type: ignore[arg-type]
    )
    message = _FakeMessage()

    handled = await bot._handle_chart_command(
        message,
        ChartCommand(symbol="TSLA", horizon="h"),
        context=DiscordContext(guild_id=1, channel_id=42, author_id=7),
        role_ids=set(),
    )

    assert handled is True
    assert charting.commands == [ChartCommand(symbol="TSLA", horizon="h")]
    assert message.replies[0]["kwargs"]["content"] == "chart rendered"
    assert "file" in message.replies[0]["kwargs"]


@pytest.mark.asyncio
async def test_discord_chart_handler_uses_existing_authorization():
    charting = _FakeChartingService()
    bot = DiscordTradingBot(
        settings=Settings(discord_chart_command_enabled=True, discord_allowed_channel_ids="42"),
        runner=None,
        charting_service=charting,  # type: ignore[arg-type]
    )
    message = _FakeMessage()

    handled = await bot._handle_chart_command(
        message,
        ChartCommand(symbol="TSLA", horizon="h"),
        context=DiscordContext(guild_id=1, channel_id=99, author_id=7),
        role_ids=set(),
    )

    assert handled is True
    assert charting.commands == []
    assert message.replies[0]["args"][0] == "Not authorized for this bot/channel."


@pytest.mark.asyncio
async def test_discord_chart_handler_acknowledges_disabled_charting():
    charting = _FakeChartingService()
    bot = DiscordTradingBot(
        settings=Settings(discord_chart_command_enabled=False, discord_allowed_channel_ids="42"),
        runner=None,
        charting_service=charting,  # type: ignore[arg-type]
    )
    message = _FakeMessage()

    handled = await bot._handle_chart_command(
        message,
        ChartCommand(symbol="SPCX", horizon="d"),
        context=DiscordContext(guild_id=1, channel_id=42, author_id=7),
        role_ids=set(),
    )

    assert handled is True
    assert charting.commands == []
    assert "DISCORD_CHART_COMMAND_ENABLED=true" in message.replies[0]["args"][0]
