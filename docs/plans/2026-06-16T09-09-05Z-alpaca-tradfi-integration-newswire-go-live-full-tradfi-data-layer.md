---
created: 2026-06-16T09:09:05.571Z
source: pi-plan-mode
status: accepted-for-execution
---

# Alpaca/TradFi Integration — Newswire Go-Live + Full TradFi Data Layer

## Summary

Two-phase delivery:

**Phase 1** ships the existing newswire into production by enabling the Alpaca News WebSocket (`ALPACA_NEWS_ENABLED=true` + API keys) and validating end-to-end.

**Phase 2** builds a vendor-agnostic `app/tradfi/` data layer (Alpaca-powered) with equities quotes/bars/options chains/greeks/corporate actions, 8-12 new LLM agent tools, a separate equity paper portfolio, and equity signal generation via the shared autonomy signal engine with asset-class-specific features. Options unusual flow detection uses deterministic pre-filtering + LLM-assisted context analysis. All execution remains paper-only with human signoff; live Alpaca Trading API integration is gated behind a config flag (disabled by default).

---

## Implementation Steps

### Phase 1: Newswire Go-Live
1. Validate Alpaca News WS adapter with live credentials, document required env vars.
2. Add smoke-test checklist to docs (adapter auth, first message received, normalization pipeline, bus publish, Discord consumer delivery).
3. Add `ALPACA_API_KEY` / `ALPACA_API_SECRET` to `.env`, set `ALPACA_NEWS_ENABLED=true`.

### Phase 2: TradFi Data Layer
4. Add `alpaca-py` to `pyproject.toml` dependencies.
5. Create `app/tradfi/` package: `__init__.py`, `schemas.py`, `base.py` (provider interface), `alpaca_provider.py`, `client.py`.
6. Implement `TradFiProvider` ABC with methods for quotes, bars, snapshots, corporate actions, options chains.
7. Implement `AlpacaTradFiProvider` using `alpaca-py` `StockHistoricalDataClient`, `OptionHistoricalDataClient`, `StockLatestTradeClient`, `StockLatestQuoteClient`, `StockSnapshotClient`, `CorporateActionsClient`.
8. Implement `TradFiClient` as a thin facade with TTL cache and process-local rate guard (same pattern as `HyperliquidClient`).
9. Add `app/tradfi/options_flow.py` — deterministic pre-filter (volume/OI spikes, premium outliers, unusual strike clustering) + enricher interface for LLM second-pass.

### Phase 3: Config
10. Add TradFi config to `Settings`: `tradfi_enabled`, `alpaca_trading_enabled` (gated, default `false`), `autonomy_equity_universe`, `autonomy_equity_max_tracked`, equity signal thresholds, equity paper portfolio params, options flow detection thresholds.

### Phase 4: Agent Tools
11. Add `get_stock_quote`, `get_stock_bars`, `get_options_chain`, `get_earnings_calendar`, `get_corporate_actions`, `get_market_snapshot_tradfi` tools to `AgentTools`.
12. Add analysis tools: `analyze_options_flow`, `compare_stocks`, `sector_heatmap`, `stock_screener`, `estimate_option_greeks`.

### Phase 5: Equity Paper Portfolio
13. Create `app/tradfi/paper/` with `schemas.py` and `simulator.py` — separate equity paper portfolio (EquityPaperPortfolio, EquityPaperOrder, EquityPaperFill, EquityPaperPosition).
14. Portfolio tracks cash, equity, P&L independently from the crypto paper book. Risk controls: max position size, max single-name exposure, max gross leverage.

### Phase 6: Equity Autonomy
15. Extend `SignalEngine` with asset-class dispatch: equity-specific feature extraction functions (earnings catalyst scoring, options flow signal, technical breakout, sector rotation).
16. Add `app/autonomy/equity_features.py` with feature extractors for equity signals.
17. Wire equity universe into the autonomy loop alongside the crypto universe. Loop processes both, with separate signal thresholds per asset class.
18. Add LLM-assisted options flow analysis: deterministic pre-filter → enricher (reusing the `Enricher` pattern from `app/newswire/enrich.py`).

### Phase 7: DB Migration
19. Create Alembic migration `0007_tradfi.py` with new tables:
    - `equity_market_observations` — price/volume snapshots
    - `equity_options_flow_events` — detected unusual options activity
    - `equity_paper_portfolios` — equity-specific paper accounts
    - `equity_paper_orders`, `equity_paper_fills`, `equity_paper_positions` — order lifecycle
    - `equity_portfolio_snapshots` — periodic equity P&L snapshots
20. Add corresponding SQLAlchemy models to `app/db/models.py`.
21. Add repository methods in `app/db/repository.py`.

### Phase 8: Wiring & FastAPI Endpoints
22. Wire `TradFiClient` into `main.py` lifespan (start/stop with the app).
23. Wire equity paper portfolio and signal engine into `AutonomousTradingLoopService`.
24. Add FastAPI endpoints under `/tradfi/` prefix: quotes, bars, options chains, corporate actions, earnings calendar.
25. Add `/autonomy/equity/` endpoints: universe, signals, portfolio, positions, orders, fills, market-map.
26. Register new TradFi agent tools in the `AgentTools` constructor.

### Phase 9: Tests
27. Unit tests for `app/tradfi/schemas.py`, `app/tradfi/alpaca_provider.py` (mocked), `app/tradfi/client.py`.
28. Unit tests for `app/tradfi/options_flow.py` (deterministic pre-filter).
29. Unit tests for `app/tradfi/paper/simulator.py`.
30. Unit tests for `app/autonomy/equity_features.py`.
31. Integration test: equity signal generation → paper order → fill → position lifecycle.
32. Smoke test: TradFi client against live Alpaca sandbox.

---

## Architecture

### Package Layout

```
hyperliquid_trading_agent/app/
├── tradfi/                          # NEW — vendor-agnostic TradFi layer
│   ├── __init__.py
│   ├── schemas.py                   # StockQuote, OptionsChain, Bar, CorpAction, etc.
│   ├── base.py                      # TradFiProvider ABC
│   ├── alpaca_provider.py           # AlpacaTradFiProvider (wraps alpaca-py)
│   ├── client.py                    # TradFiClient (facade, TTL cache, rate guard)
│   ├── options_flow.py              # Deterministic pre-filter + enricher contract
│   └── paper/                       # Equity/options paper simulation
│       ├── __init__.py
│       ├── schemas.py               # EquityPaperPortfolio, EquityPaperOrder, etc.
│       └── simulator.py             # EquityPaperSimulator
├── autonomy/
│   ├── equity_features.py           # NEW — equity-specific signal feature extractors
│   ├── service.py                   # EXTENDED — processes both universes
│   ├── signals.py                   # EXTENDED — asset-class dispatch
│   └── ...                          # (existing files unchanged)
├── agent/
│   └── tools.py                     # EXTENDED — 8-12 new TradFi tools
├── config.py                        # EXTENDED — TradFi settings
├── db/
│   ├── models.py                    # EXTENDED — equity tables
│   └── repository.py                # EXTENDED — equity persistence methods
└── main.py                          # EXTENDED — wiring, endpoints
```

### Provider Interface

```python
class TradFiProvider(ABC):
    """Vendor-agnostic TradFi data provider."""

    @abstractmethod
    async def get_latest_quote(self, symbol: str) -> StockQuote: ...

    @abstractmethod
    async def get_latest_trade(self, symbol: str) -> StockTrade: ...

    @abstractmethod
    async def get_snapshot(self, symbols: list[str]) -> dict[str, StockSnapshot]: ...

    @abstractmethod
    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int | None = None
    ) -> list[Bar]: ...

    @abstractmethod
    async def get_options_chain(
        self, symbol: str, expiration: date | None = None, strike_min: float | None = None
    ) -> OptionsChain: ...

    @abstractmethod
    async def get_corporate_actions(
        self, symbol: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[CorporateAction]: ...

    @abstractmethod
    async def get_calendar(
        self, start: date, end: date, calendar_types: list[str] | None = None
    ) -> list[CalendarEvent]: ...
```

`AlpacaTradFiProvider` implements this using:
- `StockLatestTradeClient` / `StockLatestQuoteClient` → quotes/trades
- `StockSnapshotClient` → multi-asset snapshots
- `StockHistoricalDataClient` → bars
- `OptionHistoricalDataClient` → options chains (snapshots with greeks)
- `CorporateActionsClient` → splits, dividends, mergers
- The calendar endpoint is not directly in alpaca-py; either hit the REST endpoint directly or use a thin httpx wrapper.

### Data Flow

```
Alpaca Data API ──→ TradFiProvider ──→ TradFiClient (TTL cache + rate guard)
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
              AgentTools           Autonomy Loop          FastAPI /tradfi/
         (LLM-callable tools)   (signal engine,        (REST endpoints)
                                 market map,
                                 paper portfolio)
```

### Config Additions

```python
# TradFi
tradfi_enabled: bool = False
alpaca_trading_enabled: bool = False  # gated, default false
alpaca_data_base_url: str = "https://data.alpaca.markets"

# Equity Autonomy
autonomy_equity_enabled: bool = False
autonomy_equity_universe: str = "AAPL,NVDA,MSFT,SPY,QQQ"
autonomy_equity_max_signals_per_day: int = 5
autonomy_equity_min_signal_score: float = 75.0

# Equity Paper
autonomy_equity_paper_initial_equity_usd: float = 100_000.0
autonomy_equity_paper_risk_pct_per_trade: float = 0.25
autonomy_equity_paper_max_gross_leverage: float = 2.0
autonomy_equity_paper_max_single_name_exposure_pct: float = 15.0

# Options Flow
options_flow_enabled: bool = False
options_flow_min_volume_oi_ratio: float = 3.0
options_flow_min_premium: float = 1_000_000.0
options_flow_llm_enrich_enabled: bool = True
```

### Agent Tools (8-12 tools)

**Data retrieval (6):**
1. `get_stock_quote(symbol)` — latest bid/ask/last/size
2. `get_stock_bars(symbol, timeframe, lookback_hours)` — OHLCV bars
3. `get_options_chain(symbol, expiration?, strike_min?)` — chain with greeks
4. `get_earnings_calendar(symbol?, start, end)` — upcoming earnings
5. `get_corporate_actions(symbol)` — splits, dividends, mergers
6. `get_market_snapshot_tradfi(symbols)` — multi-asset snapshot (price, change, volume)

**Analysis (5):**
7. `analyze_options_flow(symbol)` — run deterministic pre-filter + LLM context
8. `compare_stocks(symbols)` — side-by-side key metrics
9. `sector_heatmap(sector?)` — sector-level performance view
10. `stock_screener(criteria)` — basic screening (price, volume, market cap ranges)
11. `estimate_option_greeks(symbol, strike, expiration, option_type)` — theoretical greeks for any strike

### Options Flow Detection

Two-phase as chosen:

**Phase 1 — Deterministic pre-filter** (`app/tradfi/options_flow.py`):
- Volume/OI ratio exceeds `options_flow_min_volume_oi_ratio`
- Total premium (volume × mid price × 100) exceeds `options_flow_min_premium`
- Unusual strike clustering (same expiry, tight strike range, elevated volume)
- Sweep detection (multiple same-side orders at ask/bid in rapid succession)

Output is a scored `OptionsFlowEvent`.

**Phase 2 — LLM-assisted context** (reuses `Enricher` pattern):
- Above-threshold events are enriched with: earnings proximity, upcoming catalysts, sector context, whether the flow appears directional or hedging
- The LLM summary is added to the signal's evidence, not used to gate tradability

### Signal Engine Extension

The existing `SignalEngine` gets an asset-class dispatch:

```python
class SignalEngine:
    async def generate_signals(
        self, market_map: MarketState, universe: list[MarketAsset]
    ) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        for asset in universe:
            if asset.asset_class == "crypto":
                signals.extend(await self._crypto_signals(asset, market_map))
            elif asset.asset_class == "equity":
                signals.extend(await self._equity_signals(asset, market_map))
        return self._rank_and_filter(signals)
```

Equity-specific signal types (in `app/autonomy/equity_features.py`):
- **Earnings play** — upcoming earnings with elevated IV, pre/post earnings drift signals
- **Technical breakout** — multi-timeframe breakout on volume (similar to crypto but with equity-specific thresholds)
- **Options flow** — unusual activity flagged by the flow detector
- **Sector rotation** — relative strength shifts between sectors

Each signal type produces a scored `TradeSignal` with `asset_class="equity"`, flowing through the same approval → evaluation → memory pipeline.

### Paper Equities Lifecycle

Mirrors `app/paper/` but separate:

1. Signal is approved (human signoff)
2. `EquityPaperSimulator` creates an `EquityPaperOrder`
3. Order fills at market price (from latest quote)
4. Creates `EquityPaperFill` and `EquityPaperPosition`
5. Portfolio tracks equity, unrealized/realized P&L, exposure
6. Periodic snapshots record portfolio state
7. Risk controls enforce max position size, max single-name exposure, max gross leverage

Options paper simulation is deferred to a future iteration (requires more complex pricing/P&L modeling). The options tools are research/DD only for now; signals from options flow result in equity (stock) paper orders, not option orders.

### DB Migration `0007_tradfi`

New tables:

```sql
-- equity_market_observations: symbol-level price/volume snapshots
-- equity_options_flow_events: unusual options activity records
-- equity_paper_portfolios: separate paper account
-- equity_paper_orders: equity paper orders
-- equity_paper_fills: equity paper fills
-- equity_paper_positions: equity paper positions
-- equity_portfolio_snapshots: periodic P&L snapshots
```

All tables follow the existing naming/indexing conventions from `0004_autonomous_loop`.

### Safety Posture

- `alpaca_trading_enabled` defaults to `false` (same pattern as `HYPERLIQUID_EXCHANGE_ENABLED`)
- All execution is paper-only
- Human signoff required for all equity signals
- No automatic strategy/risk/sizing changes
- Options paper trading deferred (research/DD tools only)
- Live Alpaca Trading API integration gated behind `alpaca_trading_enabled` with config validation

---

## Test Plan

| Layer | Tests | Approach |
|-------|-------|----------|
| `app/tradfi/schemas.py` | Pydantic validation, serialization | Standard unit tests |
| `app/tradfi/alpaca_provider.py` | Provider methods with mocked alpaca-py clients | `pytest` + `unittest.mock` |
| `app/tradfi/client.py` | TTL cache hit/miss, rate guard, provider dispatch | Unit tests with fake provider |
| `app/tradfi/options_flow.py` | Pre-filter thresholds, event scoring | Unit tests with synthetic chains |
| `app/tradfi/paper/simulator.py` | Order → fill → position lifecycle, risk controls | Unit tests (async) |
| `app/autonomy/equity_features.py` | Feature extraction per signal type | Unit tests |
| Agent tools | Tool invocation, error handling, empty results | Extend `test_runtime_components.py` |
| Autonomy integration | Full equity signal → paper lifecycle | Integration test |
| FastAPI endpoints | `/tradfi/*`, `/autonomy/equity/*` | Extend `test_newswire.py` style |
| Config migration | New settings parsed, validation, warnings | Unit tests |

---

## Assumptions

1. **Shared signal engine** with asset-class-specific feature sets (agent-recommended, not overridden by user).
2. **Options paper trading deferred** — options tools provide research/DD data only; signals from options flow result in equity stock paper orders (not option orders).
3. **alpaca-py** is the chosen SDK, added to `pyproject.toml` dependencies.
4. **Calendar endpoint** may require a thin httpx call since `alpaca-py` doesn't expose a dedicated calendar client (the Alpaca REST API has `/v2/calendar`).
5. **No breaking changes** to existing crypto autonomy paths — equity features are additive, gated behind `tradfi_enabled` and `autonomy_equity_enabled`.
6. **Phase 1 is config-only** (keys + enable flag) per user preference — no additional ops hardening in this plan.
7. **Existing newswire Alpaca News WS** is code-complete and tested; go-live is purely operational.






<!-- pi-plan-progress:start -->
## Progress

Status legend: `[x]` done, `[-]` skipped, `[>]` deferred, `[!]` blocked, `[ ]` pending.

- [x] 1. Phase 1: Newswire Go-Live _(done)_
- [x] 2. Phase 2: TradFi Data Layer _(done)_
- [x] 3. Phase 3: Config _(done)_
- [x] 4. Phase 4: Agent Tools _(done)_
- [x] 5. Phase 5: Equity Paper Portfolio _(done)_
- [x] 6. Phase 6: Equity Autonomy _(done)_
- [x] 7. Phase 7: DB Migration _(done)_
- [ ] 8. Phase 8: Wiring & FastAPI Endpoints _(pending)_
- [x] 9. Phase 9: Tests _(done)_

<!-- pi-plan-progress:end -->
