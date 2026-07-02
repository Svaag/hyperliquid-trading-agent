from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Literal

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hyperliquid.client import HyperliquidClient
from hyperliquid_trading_agent.app.markets.resolution import KNOWN_CRYPTO_SYMBOLS
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.schemas import Bar

ChartHorizon = Literal["h", "d", "m", "y"]

_CHART_COMMAND_RE = re.compile(r"^;;\s*([A-Za-z][A-Za-z0-9._:-]{0,24})\s+([hHdDmMyY])\s*$")


@dataclass(frozen=True)
class ChartCommand:
    symbol: str
    horizon: ChartHorizon


@dataclass(frozen=True)
class ChartResult:
    content: str
    image_png: bytes
    filename: str


@dataclass(frozen=True)
class _HorizonConfig:
    label: str
    tradfi_timeframe: str
    hyperliquid_interval: str
    lookback_hours: int
    limit: int


@dataclass(frozen=True)
class _TechnicalSummary:
    trend: str
    rsi14: float | None
    atr14: float | None
    support: float | None
    resistance: float | None
    volume_label: str
    pattern_label: str
    ema20: float | None
    ema50: float | None


_HORIZONS: dict[ChartHorizon, _HorizonConfig] = {
    "h": _HorizonConfig("hourly", "1h", "1h", 24 * 7, 180),
    "d": _HorizonConfig("daily", "1d", "1d", 24 * 185, 190),
    "m": _HorizonConfig("monthly", "1M", "1M", 24 * 365 * 5, 90),
    "y": _HorizonConfig("yearly", "1M", "1M", 24 * 365 * 12, 160),
}

_HL_BG = "#07100e"
_HL_PANEL = "#0c1a17"
_HL_LINE = "#163029"
_HL_MUTED = "#6b8a82"
_HL_TEXT = "#d7f4ec"
_HL_ACCENT = "#4fe0c0"
_HL_UP = "#2ee6a6"
_HL_DOWN = "#f1556c"
_HL_WARN = "#f5b34a"


def parse_chart_command(content: str) -> ChartCommand | None:
    match = _CHART_COMMAND_RE.match(str(content or "").strip())
    if match is None:
        return None
    return ChartCommand(symbol=_canonical_chart_symbol(match.group(1)), horizon=match.group(2).lower())  # type: ignore[arg-type]


class ChartingService:
    def __init__(
        self,
        *,
        settings: Settings,
        hyperliquid: HyperliquidClient | None = None,
        tradfi: TradFiClient | None = None,
    ) -> None:
        self.settings = settings
        self.hyperliquid = hyperliquid
        self.tradfi = tradfi

    async def render(self, command: ChartCommand) -> ChartResult:
        config = _HORIZONS[command.horizon]
        source = ""
        bars: list[Bar] = []
        prefer_hyperliquid = _prefer_hyperliquid(command.symbol)
        if prefer_hyperliquid:
            bars, source = await self._hyperliquid_bars(command.symbol, config)
        if not bars and self.tradfi is not None and ":" not in command.symbol:
            bars = await self.tradfi.get_bars(command.symbol, timeframe=config.tradfi_timeframe, lookback_hours=config.lookback_hours, limit=config.limit)
            source = f"tradfi:{self.tradfi.provider.name}"
        if not bars and prefer_hyperliquid and self.hyperliquid is not None:
            bars, source = await self._hyperliquid_bars(command.symbol, config)
        bars = _dedupe_sort_bars(bars)
        if command.horizon == "y":
            bars = _resample_yearly(bars)
        if len(bars) < 2:
            return ChartResult(
                content=f"No usable candle data found for {command.symbol} {config.label}. No trade was placed.",
                image_png=b"",
                filename=f"{_safe_filename(command.symbol)}_{command.horizon}.png",
            )
        bars = bars[-config.limit :]
        summary = _technical_summary(bars)
        image = _render_png(command.symbol, config.label, bars, summary, source or "unknown")
        return ChartResult(
            content=_caption(command.symbol, config.label, bars, summary, source or "unknown"),
            image_png=image,
            filename=f"{_safe_filename(command.symbol)}_{command.horizon}.png",
        )

    async def _hyperliquid_bars(self, symbol: str, config: _HorizonConfig) -> tuple[list[Bar], str]:
        if self.hyperliquid is None:
            return [], ""
        end = int(time.time() * 1000)
        start = end - config.lookback_hours * 60 * 60 * 1000
        try:
            payload = await self.hyperliquid.candle_snapshot(_canonical_hyperliquid_symbol(symbol), config.hyperliquid_interval, start, end)
        except Exception:
            return [], "hyperliquid"
        return _parse_hyperliquid_bars(symbol, config.hyperliquid_interval, payload), "hyperliquid"


def _technical_summary(bars: list[Bar]) -> _TechnicalSummary:
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    ema20_values = _ema(closes, 20)
    ema50_values = _ema(closes, 50)
    ema20 = ema20_values[-1] if ema20_values else None
    ema50 = ema50_values[-1] if ema50_values else None
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(highs, lows, closes, 14)
    recent = bars[-min(20, len(bars)) :]
    support = min(bar.low for bar in recent) if recent else None
    resistance = max(bar.high for bar in recent) if recent else None
    trend = "mixed"
    if ema20 is not None and ema50 is not None:
        if closes[-1] > ema20 > ema50:
            trend = "uptrend"
        elif closes[-1] < ema20 < ema50:
            trend = "downtrend"
    volume_label = "volume neutral"
    if len(volumes) >= 21:
        avg_volume = sum(volumes[-21:-1]) / 20
        if avg_volume > 0 and volumes[-1] >= avg_volume * 1.5:
            volume_label = "volume expansion"
        elif avg_volume > 0 and volumes[-1] <= avg_volume * 0.6:
            volume_label = "volume contraction"
    pattern_label = "inside range"
    if len(bars) >= 21:
        prior_resistance = max(bar.high for bar in bars[-21:-1])
        prior_support = min(bar.low for bar in bars[-21:-1])
        if closes[-1] > prior_resistance:
            pattern_label = "breakout above 20-bar range"
        elif closes[-1] < prior_support:
            pattern_label = "breakdown below 20-bar range"
    return _TechnicalSummary(
        trend=trend,
        rsi14=rsi14,
        atr14=atr14,
        support=support,
        resistance=resistance,
        volume_label=volume_label,
        pattern_label=pattern_label,
        ema20=ema20,
        ema50=ema50,
    )


def _render_png(symbol: str, label: str, bars: list[Bar], summary: _TechnicalSummary, source: str) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    import pandas as pd

    frame = pd.DataFrame(
        {
            "Open": [bar.open for bar in bars],
            "High": [bar.high for bar in bars],
            "Low": [bar.low for bar in bars],
            "Close": [bar.close for bar in bars],
            "Volume": [bar.volume for bar in bars],
        },
        index=pd.DatetimeIndex([bar.timestamp for bar in bars]),
    )
    addplots = []
    ema20 = _ema([bar.close for bar in bars], 20)
    ema50 = _ema([bar.close for bar in bars], 50)
    if ema20:
        addplots.append(mpf.make_addplot(_pad_series(ema20, len(frame)), color=_HL_ACCENT, width=1.1))
    if ema50:
        addplots.append(mpf.make_addplot(_pad_series(ema50, len(frame)), color=_HL_WARN, width=1.1))
    title = f"{symbol} {label} candles | {source} | {summary.trend} | RSI14 {_format_optional(summary.rsi14)}"
    plot_kwargs: dict[str, Any] = {}
    if addplots:
        plot_kwargs["addplot"] = addplots
    market_colors = mpf.make_marketcolors(
        up=_HL_UP,
        down=_HL_DOWN,
        edge={"up": _HL_UP, "down": _HL_DOWN},
        wick={"up": _HL_UP, "down": _HL_DOWN},
        volume={"up": "#1f8f6d", "down": "#8f3342"},
    )
    style = mpf.make_mpf_style(
        marketcolors=market_colors,
        figcolor=_HL_BG,
        facecolor=_HL_PANEL,
        edgecolor=_HL_LINE,
        gridcolor=_HL_LINE,
        gridstyle="-",
        y_on_right=True,
        rc={
            "axes.edgecolor": _HL_LINE,
            "axes.labelcolor": _HL_TEXT,
            "axes.titlecolor": _HL_TEXT,
            "figure.facecolor": _HL_BG,
            "font.family": "DejaVu Sans Mono",
            "grid.alpha": 0.55,
            "savefig.facecolor": _HL_BG,
            "text.color": _HL_TEXT,
            "xtick.color": _HL_MUTED,
            "ytick.color": _HL_MUTED,
        },
    )
    fig, _axes = mpf.plot(
        frame,
        type="candle",
        style=style,
        volume=True,
        title=title,
        ylabel="Price",
        ylabel_lower="Volume",
        returnfig=True,
        figsize=(11, 7),
        tight_layout=True,
        **plot_kwargs,
    )
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=140, facecolor=_HL_BG, edgecolor=_HL_BG)
    plt.close(fig)
    return out.getvalue()


def _caption(symbol: str, label: str, bars: list[Bar], summary: _TechnicalSummary, source: str) -> str:
    first = bars[0].close
    last = bars[-1].close
    change_pct = ((last - first) / first * 100) if first else 0.0
    parts = [
        f"{symbol} {label} chart",
        f"source {source}",
        f"close {_format_price(last)} ({change_pct:+.2f}%)",
        f"{summary.trend}",
        f"RSI14 {_format_optional(summary.rsi14)}",
        f"ATR14 {_format_optional(summary.atr14)}",
        f"S/R {_format_optional(summary.support)} / {_format_optional(summary.resistance)}",
        summary.volume_label,
        summary.pattern_label,
        "Informational only; no trade was placed.",
    ]
    return " | ".join(parts)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def _rsi(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, current in pairwise(values):
        delta = current - prev
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=False):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    if len(highs) <= period or len(lows) != len(highs) or len(closes) != len(highs):
        return None
    true_ranges = []
    for index in range(1, len(highs)):
        true_ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def _parse_hyperliquid_bars(symbol: str, interval: str, payload: Any) -> list[Bar]:
    if not isinstance(payload, list):
        return []
    bars: list[Bar] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        timestamp_ms = _float_or_none(item.get("t") or item.get("T"))
        open_px = _float_or_none(item.get("o"))
        high = _float_or_none(item.get("h"))
        low = _float_or_none(item.get("l"))
        close = _float_or_none(item.get("c"))
        volume = _float_or_none(item.get("v") or item.get("n") or 0)
        if timestamp_ms is None or open_px is None or high is None or low is None or close is None or volume is None:
            continue
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                open=open_px,
                high=high,
                low=low,
                close=close,
                volume=volume,
                timeframe=interval,
            )
        )
    return sorted(bars, key=lambda item: item.timestamp)


def _resample_yearly(bars: list[Bar]) -> list[Bar]:
    by_year: dict[int, list[Bar]] = {}
    for bar in bars:
        by_year.setdefault(bar.timestamp.year, []).append(bar)
    yearly: list[Bar] = []
    for year in sorted(by_year):
        group = sorted(by_year[year], key=lambda item: item.timestamp)
        yearly.append(
            Bar(
                symbol=group[-1].symbol,
                timestamp=datetime(year, 12, 31, tzinfo=UTC),
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum(item.volume for item in group),
                timeframe="1Year",
            )
        )
    return yearly


def _dedupe_sort_bars(bars: list[Bar]) -> list[Bar]:
    by_timestamp: dict[datetime, Bar] = {}
    for bar in bars:
        by_timestamp[bar.timestamp] = bar
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _prefer_hyperliquid(symbol: str) -> bool:
    return ":" in symbol or symbol.upper() in KNOWN_CRYPTO_SYMBOLS


def _canonical_chart_symbol(symbol: str) -> str:
    cleaned = symbol.strip()
    if ":" in cleaned:
        dex, base = cleaned.split(":", 1)
        return f"{dex.lower()}:{base.upper()}"
    return cleaned.upper()


def _canonical_hyperliquid_symbol(symbol: str) -> str:
    if ":" in symbol:
        dex, base = symbol.split(":", 1)
        return f"{dex.lower()}:{base.upper()}"
    return symbol.upper()


def _safe_filename(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol).strip("_") or "chart"


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pad_series(values: list[float], length: int) -> list[float | None]:
    if len(values) >= length:
        out: list[float | None] = list(values[-length:])
        return out
    return [None] * (length - len(values)) + values


def _format_optional(value: float | None) -> str:
    return "n/a" if value is None else _format_price(value)


def _format_price(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.6f}"
