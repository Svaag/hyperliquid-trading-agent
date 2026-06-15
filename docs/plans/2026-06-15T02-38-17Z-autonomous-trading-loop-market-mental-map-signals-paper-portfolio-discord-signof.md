---
created: 2026-06-15T02:38:17.142Z
source: pi-plan-mode
status: accepted-for-execution
---

# Autonomous Trading Loop: Market Mental Map → Signals → Paper Portfolio → Discord Signoff

## Summary

Build the first autonomous-loop iteration as a **non-executing, paper-trading, human-signoff trading agent** that continuously watches Hyperliquid markets, builds a deterministic “mental map” of market structure, detects trade signals, posts them to Discord `#ai-bot-alerts`, and tracks a paper portfolio with PnL, Sharpe, drawdown, exposure, and trade logs.

This extends the current codebase instead of replacing it:

- Reuse `HyperliquidWebSocketWorker` for market streaming.
- Reuse `PositionTrackingService` concepts for no-LLM hot loops.
- Reuse high-stakes debate/model review only as optional non-blocking insight.
- Reuse `NewsService`, RSS/search/X scaffolding.
- Add real paper portfolio lifecycle beyond the current sizing-only `PaperTradeSimulator`.
- Keep `HYPERLIQUID_EXCHANGE_ENABLED=false`; no live order signing.

## Implementation Steps

1. Add autonomous-loop configuration and health visibility.
2. Add `app/autonomy/` package with schemas, universe resolver, market map, signal engine, paper portfolio, newswire loop, Discord alerting, and service orchestration.
3. Add Alembic migration for autonomous market state, signals, paper portfolio, orders, fills, positions, snapshots, and event log.
4. Extend Hyperliquid data collection for universe discovery, HIP-3 alias resolution, active market context, L2/orderflow features, candles, funding, and known public liquidation data.
5. Implement deterministic market mental-map reducer.
6. Implement deterministic signal engine and scoring contract.
7. Implement optional model-insight review path for high-value candidates only.
8. Implement paper portfolio, order/fill simulation, risk controls, and performance metrics.
9. Add Discord `#ai-bot-alerts` signal/log posting and signoff commands.
10. Add FastAPI endpoints for loop status, market map, signals, portfolio, positions, orders, fills, and performance.
11. Wire `AutonomousTradingLoopService` into `main.py` lifespan behind `AUTONOMY_ENABLED`.
12. Add Prometheus metrics, audit logging, and readiness degradation rules.
13. Add tests for deterministic reducers, signals, portfolio math, Discord commands, APIs, and no-execution guarantees.
14. Update README, `.env.example`, and deployment runbook.
15. Roll out disabled-by-default, then enable paper/signoff mode locally.

## Current Codebase Grounding

Already available and should be reused:

- `hyperliquid_trading_agent/app/hyperliquid/ws_worker.py`
  - Dynamic WebSocket subscriptions.
  - Supports `allMids`, `l2Book`, `trades`, `bbo`, `activeAssetCtx`, candles, user channels.
- `hyperliquid_trading_agent/app/hyperliquid/client.py`
  - Official `/info` client with rate guard.
  - Supports `allMids`, `meta`, `metaAndAssetCtxs`, `perpDexs`, `l2Book`, `candleSnapshot`, `fundingHistory`, `predictedFundings`, public account/portfolio endpoints.
- `hyperliquid_trading_agent/app/tracking/`
  - No-LLM live tracking loop.
  - Level-hit event model already keeps `exchange_actions=[]`.
- `hyperliquid_trading_agent/app/news/`
  - RSS, Tavily, SerpAPI, NewsAPI, Perplexity, X recent search.
- `hyperliquid_trading_agent/app/paper/`
  - Current sizing calculator only; needs real portfolio lifecycle.
- `hyperliquid_trading_agent/app/db/models.py`
  - Existing persistence for decisions, proposals, trackers, news, conversations, paper ideas.
- `hyperliquid_trading_agent/app/discord_bot.py`
  - Discord bot, threads, tracking commands, persisted conversation context.
- Existing safety:
  - `HYPERLIQUID_EXCHANGE_ENABLED=true` is rejected.
  - No SDK `Exchange` use.
  - High-stakes proposals are paper/manual only.

## V1 Scope

### In scope

- Autonomous market-watching loop.
- Configurable universe: BTC, ETH, HYPE, top-volume perps, and configured HIP-3/index aliases.
- Market mental map:
  - current prices,
  - trend/regime,
  - support/resistance,
  - volatility,
  - funding,
  - OI/volume,
  - order book imbalance,
  - liquidity walls,
  - known public liquidation levels when available.
- Newswire loop:
  - RSS/search/X polling,
  - dedup,
  - asset tagging,
  - importance scoring,
  - optional model summarization.
- Deterministic signal generation.
- Discord signal posting to `#ai-bot-alerts`.
- Human signoff required before any paper order.
- Paper portfolio:
  - holdings,
  - cash/treasury,
  - realized/unrealized PnL,
  - equity curve,
  - Sharpe,
  - max drawdown,
  - exposure,
  - win rate.
- Query APIs and Discord commands.

### Out of scope for V1

- No live exchange execution.
- No private keys, API wallets, signing, SDK `Exchange`, or `/exchange`.
- No automatic paper execution without human signoff.
- No claim that hidden market stop-loss orders are fully observable unless an official endpoint proves it.
- No LLM in the hot price/orderbook loop.
- No portfolio margin optimization beyond deterministic risk caps.

## Important Data-Availability Clarification

Hyperliquid exposes far more public data than traditional venues, but implementation must distinguish:

1. **Directly observable**
   - L2 book via `l2Book`.
   - BBO/trades where subscribed.
   - Mark/oracle/funding/OI/volume via asset contexts.
   - Public account liquidation prices for configured/public addresses via `clearinghouseState`.
   - Open orders/fills for configured public addresses where endpoint allows.

2. **Inferred**
   - Stop-loss clusters.
   - Broad liquidation heatmaps outside known public accounts.
   - Hidden trigger orders not exposed as visible orders.

So the system should build a **liquidity/liquidation map** with `source=direct|inferred` and never present inferred stop clusters as certain.

## New Package Layout

Add:

```text
hyperliquid_trading_agent/app/autonomy/
  __init__.py
  schemas.py
  universe.py
  market_map.py
  orderflow.py
  levels.py
  newswire.py
  signals.py
  portfolio.py
  discord.py
  service.py
  performance.py
```

Responsibilities:

- `schemas.py`: all Pydantic contracts.
- `universe.py`: asset discovery, HIP-3/index alias resolution.
- `market_map.py`: in-memory + persisted market mental map reducer.
- `orderflow.py`: L2/BBO/trades features.
- `levels.py`: support/resistance/liquidity-level detection.
- `newswire.py`: continuous news/X polling and event scoring.
- `signals.py`: deterministic signal candidate generation.
- `portfolio.py`: paper portfolio, orders, fills, positions.
- `discord.py`: alert formatting and command handling.
- `service.py`: background loop orchestration.
- `performance.py`: PnL, Sharpe, drawdown, win rate.

## Configuration

Add to `Settings` and `.env.example`:

```env
AUTONOMY_ENABLED=false
AUTONOMY_MODE=paper_signoff

AUTONOMY_ALERT_CHANNEL_ID=
AUTONOMY_REQUIRE_HUMAN_SIGNOFF=true
AUTONOMY_ADMIN_USER_IDS=
AUTONOMY_ADMIN_ROLE_IDS=

AUTONOMY_CORE_UNIVERSE=BTC,ETH,HYPE
AUTONOMY_UNIVERSE_TOP_N_PERPS=20
AUTONOMY_HIP3_DEXS=
AUTONOMY_HIP3_INDEX_ALIASES=SP500:SPX|SP500|SPY,NASDAQ100:NDX|NASDAQ|QQQ,NIKKEI225:NIKKEI|NKY,KOSPI:KOSPI

AUTONOMY_LOOP_INTERVAL_SECONDS=5
AUTONOMY_DEEP_SCAN_INTERVAL_SECONDS=60
AUTONOMY_L2_REFRESH_SECONDS=15
AUTONOMY_CANDLE_REFRESH_SECONDS=60
AUTONOMY_NEWS_REFRESH_SECONDS=60
AUTONOMY_PORTFOLIO_SNAPSHOT_SECONDS=60

AUTONOMY_MAX_TRACKED_ASSETS=40
AUTONOMY_MAX_HOT_L2_ASSETS=5
AUTONOMY_MAX_SIGNALS_PER_DAY=10
AUTONOMY_SIGNAL_TTL_MINUTES=30
AUTONOMY_MIN_SIGNAL_SCORE=75

AUTONOMY_PAPER_INITIAL_EQUITY_USD=100000
AUTONOMY_PAPER_RISK_PCT_PER_TRADE=0.25
AUTONOMY_PAPER_MAX_GROSS_LEVERAGE=3.0
AUTONOMY_PAPER_MAX_SINGLE_NAME_EXPOSURE_PCT=20
AUTONOMY_PAPER_TAKER_FEE_BPS=4.5
AUTONOMY_PAPER_MAKER_FEE_BPS=1.5
AUTONOMY_PAPER_DEFAULT_SLIPPAGE_BPS=2.0

AUTONOMY_MODEL_INSIGHTS_ENABLED=true
AUTONOMY_MODEL_INSIGHT_MIN_SCORE=80
AUTONOMY_MODEL_MAX_CALLS_PER_HOUR=12

NEWSWIRE_ENABLED=true
NEWSWIRE_QUERIES=BTC,ETH,HYPE,Hyperliquid,Fed,CPI,FOMC,crypto liquidation
X_WATCHLIST_USER_IDS=
X_MIN_PUBLIC_METRIC_SCORE=0
```

Defaults are safe:

- disabled by default,
- paper only,
- signoff required,
- no live execution.

`AUTONOMY_ALERT_CHANNEL_ID` must be the Discord channel ID for `#ai-bot-alerts`.

## Runtime Architecture

### Loop topology

```text
Startup
  -> resolve universe
  -> load paper portfolio
  -> load open signals/orders/positions
  -> subscribe to allMids
  -> start periodic loops

Hot loop, every allMids tick:
  -> update prices
  -> mark paper positions
  -> update mental map prices
  -> evaluate active signal/order/position stops
  -> emit portfolio snapshots on interval

Deep market loop, every 15–60s:
  -> refresh activeAssetCtx / l2Book / candles / funding
  -> recompute support/resistance/orderflow features
  -> update market mental map
  -> run signal engine

Newswire loop, every 60s:
  -> poll RSS/search/X
  -> dedupe + score + tag assets
  -> update news state
  -> trigger signal re-check if important

Model insight loop, non-blocking:
  -> only high-score candidate or major regime/news event
  -> structured model review
  -> attach insight to signal
  -> never blocks hot loop

Discord/log loop:
  -> post signals
  -> accept approve/reject commands
  -> log paper orders/fills/position closes
```

## Market Universe Resolution

Implement `MarketUniverseResolver`.

Inputs:

- `AUTONOMY_CORE_UNIVERSE`
- `AUTONOMY_UNIVERSE_TOP_N_PERPS`
- `AUTONOMY_HIP3_DEXS`
- `AUTONOMY_HIP3_INDEX_ALIASES`

Process:

1. Fetch `metaAndAssetCtxs()` for main perps.
2. Rank by `dayNtlVlm`, include top N.
3. Always include core tickers if resolvable.
4. If `AUTONOMY_HIP3_DEXS` configured:
   - call `perpDexs()`,
   - call `meta(dex=<dex>)` / `metaAndAssetCtxs(dex=<dex>)`,
   - resolve aliases against actual coin names.
5. Store unresolved aliases in health/config warnings.

Output schema:

```python
class MarketAsset(BaseModel):
    symbol: str
    display_name: str
    source: Literal["core", "top_volume", "hip3_alias"]
    dex: str | None = None
    kind: Literal["perp", "spot", "hip3_index"]
    sz_decimals: int | None = None
    max_leverage: int | None = None
    day_volume_usd: float | None = None
    metadata: dict[str, Any] = {}
```

## Market Mental Map Contract

Add:

```python
class AssetMarketState(BaseModel):
    symbol: str
    timestamp_ms: int
    mid: float | None
    mark: float | None
    oracle: float | None
    funding_hourly: float | None
    open_interest: float | None
    day_volume_usd: float | None
    trend: Literal["up", "down", "range", "unknown"]
    volatility_regime: Literal["low", "normal", "high", "unknown"]
    support_levels: list[MarketLevel]
    resistance_levels: list[MarketLevel]
    liquidity_levels: list[MarketLevel]
    orderflow: OrderflowState | None
    news_state: AssetNewsState | None
    regime_score: float
    metadata: dict[str, Any] = {}

class GlobalMarketMap(BaseModel):
    timestamp_ms: int
    risk_regime: Literal["risk_on", "risk_off", "mixed", "unknown"]
    leaders: list[str]
    laggards: list[str]
    btc_beta_notes: dict[str, float]
    correlated_clusters: list[list[str]]
    key_themes: list[str]
    assets: dict[str, AssetMarketState]
```

## Support/Resistance Detection

Use deterministic multi-timeframe levels.

Initial timeframes:

```text
5m, 15m, 1h, 4h, 1d
```

Level sources:

- rolling recent high/low,
- pivot highs/lows,
- VWAP approximation from candles if enough data,
- prior day high/low,
- entry/stop levels from active paper positions,
- large L2 resting liquidity walls.

`MarketLevel`:

```python
class MarketLevel(BaseModel):
    id: str
    symbol: str
    kind: Literal[
      "support",
      "resistance",
      "liquidity_wall",
      "liquidation_known",
      "liquidation_inferred",
      "prior_high",
      "prior_low",
      "vwap"
    ]
    price: float
    strength: float
    timeframe: str
    source: Literal["candles", "l2", "public_account", "inferred"]
    first_seen_ms: int
    last_seen_ms: int
    expires_at_ms: int | None = None
    metadata: dict[str, Any] = {}
```

Deduplicate levels within 5 bps per asset/timeframe, keeping the highest-strength level.

## Orderflow and Liquidity Map

Use available Hyperliquid data:

- `l2Book` REST for configured universe and hot assets.
- Optional WS `bbo`, `trades`, `l2Book` for hot assets only.
- `activeAssetCtx` for funding/OI/premium/volume.

Orderflow features:

```python
class OrderflowState(BaseModel):
    spread_bps: float | None
    top_depth_usd: float | None
    depth_10bps_bid_usd: float | None
    depth_10bps_ask_usd: float | None
    depth_50bps_bid_usd: float | None
    depth_50bps_ask_usd: float | None
    imbalance_top: float | None
    imbalance_10bps: float | None
    microprice: float | None
    large_bid_walls: list[MarketLevel]
    large_ask_walls: list[MarketLevel]
    recent_trade_imbalance: float | None
    cvd_proxy: float | None
```

Hot asset policy:

- All assets get `allMids`.
- Core + top-volume assets get periodic L2.
- Only top `AUTONOMY_MAX_HOT_L2_ASSETS` get high-frequency L2/trades/BBO subscriptions.
- Hot set chosen by:
  - active paper position,
  - pending signal,
  - abnormal move,
  - high news importance,
  - volume spike.

## Liquidation and Stop-Loss Mapping

Implement `LiquidationMapBuilder`.

Direct data:

- For configured public addresses:
  - `clearinghouseState`,
  - positions,
  - `liquidationPx`,
  - size/notional,
  - side.
- For smart-money/watchlist addresses already supported by high-stakes config.

Inferred data:

- recent swing highs/lows,
- clustered liquidity walls,
- high OI + funding stress,
- round-number levels,
- failed breakout/breakdown levels.

Schema:

```python
class LiquidationCluster(BaseModel):
    symbol: str
    price: float
    side_at_risk: Literal["longs", "shorts", "unknown"]
    notional_usd_known: float | None
    confidence: Literal["direct", "inferred_low", "inferred_medium"]
    source: Literal["public_account", "market_structure", "orderbook"]
    accounts: list[str] = []
    metadata: dict[str, Any] = {}
```

Do not render inferred clusters as known fact.

## Newswire and X Integration

Extend current pull-on-demand `NewsService` into a loop.

Add `AutonomyNewswire`.

Behavior:

1. Poll RSS feeds.
2. Poll configured search providers.
3. Poll X recent search.
4. Poll optional X watchlist accounts.
5. Dedupe by URL/tweet ID/title hash.
6. Score importance.
7. Tag assets.
8. Persist and attach to market map.

Add X query improvements:

- include `expansions=author_id`,
- include `user.fields=verified,public_metrics,username`,
- include `tweet.fields=created_at,public_metrics,author_id,entities`,
- compute public metric score.

Newswire event schema:

```python
class NewsEvent(BaseModel):
    id: str
    source: str
    provider: str
    title: str
    text: str
    url: str | None
    author_id: str | None
    created_at_ms: int | None
    observed_at_ms: int
    assets: list[str]
    importance_score: float
    sentiment: Literal["bullish", "bearish", "mixed", "unknown"]
    freshness: Literal["breaking", "fresh", "stale"]
    metadata: dict[str, Any] = {}
```

Model summarization allowed only for clusters or important events; not every headline.

## Signal Engine

Implement deterministic `SignalEngine`.

Signal types:

```text
breakout_retest
support_bounce
resistance_rejection
liquidation_sweep_reversal
funding_oi_squeeze
news_catalyst_momentum
trend_continuation
risk_off_deleveraging
```

Candidate schema:

```python
class TradeSignal(BaseModel):
    id: str
    symbol: str
    side: Literal["long", "short"]
    signal_type: str
    status: Literal[
      "candidate",
      "posted",
      "approved",
      "rejected",
      "expired",
      "paper_ordered",
      "cancelled"
    ]
    score: float
    confidence: float
    created_at_ms: int
    expires_at_ms: int
    entry: float
    stop: float
    take_profit: float | None
    invalidation: str
    thesis: str
    evidence: list[SignalEvidence]
    feature_snapshot: dict[str, Any]
    risk_plan: dict[str, Any]
    model_insight: dict[str, Any] | None = None
    discord_channel_id: str | None = None
    discord_message_id: str | None = None
```

Score components:

```text
market_structure       25
orderflow/liquidity    20
risk_reward            15
funding/OI             15
news/catalyst          10
cross-asset regime     10
execution quality       5
```

Post to Discord only when:

```text
score >= AUTONOMY_MIN_SIGNAL_SCORE
RR >= 1.5
stop exists
entry exists
spread acceptable
daily signal cap not exceeded
no portfolio risk veto
```

Risk vetoes:

- missing stop,
- stop inside normal noise without reason,
- RR < 1.5,
- spread too wide,
- top depth too thin,
- same-direction exposure exceeds cap,
- signal stale/duplicate,
- conflicting high-importance news.

## Optional Model Insight

Use LLMs as a senior analyst, not as the hot loop.

Trigger model insight when:

- deterministic signal score >= `AUTONOMY_MODEL_INSIGHT_MIN_SCORE`,
- major news event score >= 80,
- global market regime flips,
- scheduled market-map summary every 4h.

Contract:

```python
class ModelMarketInsight(BaseModel):
    stance: Literal["support", "oppose", "needs_more_data"]
    confidence: float
    thesis_quality: float
    hidden_risks: list[str]
    what_would_invalidate: list[str]
    suggested_adjustments: list[str]
    summary: str
```

Rules:

- Model cannot place trades.
- Model cannot approve a signal.
- Model output is attached as evidence.
- If model fails, signal can still post with deterministic evidence.
- Rate limit: `AUTONOMY_MODEL_MAX_CALLS_PER_HOUR`.

## Paper Portfolio

Replace sizing-only paper trading with a full portfolio simulator.

Initial portfolio:

```text
name: "default"
initial_equity: AUTONOMY_PAPER_INITIAL_EQUITY_USD
cash: initial_equity
mode: paper_signoff
```

Paper order lifecycle:

```text
signal posted
  -> human approve
  -> paper order created
  -> fill simulation
  -> paper position open
  -> mark-to-market via allMids
  -> stop/take-profit/manual close
  -> paper position closed
```

Order fill rules:

- V1 uses market paper fill on approval.
- Fill price:
  - long: `mid * (1 + slippage_bps / 10000)`
  - short: `mid * (1 - slippage_bps / 10000)`
- Fee:
  - taker default `4.5 bps`.
- Position size:
  - fixed fractional risk using stop distance,
  - default risk `0.25%` equity per trade.
- Hard caps:
  - max gross leverage `3.0x`,
  - max single-name exposure `20%`,
  - max daily new signals `10`.

Portfolio metrics:

- cash,
- equity,
- gross exposure,
- net exposure,
- realized PnL,
- unrealized PnL,
- total PnL,
- return pct,
- max drawdown,
- Sharpe ratio,
- win rate,
- average win/loss,
- open risk to stops.

Sharpe calculation:

- Use hourly equity returns.
- If fewer than 30 hourly returns, return `null` with reason `insufficient_history`.
- Annualization factor: `sqrt(24 * 365)` for crypto.

## Database Migration

Add Alembic migration:

```text
alembic/versions/0004_autonomous_loop.py
```

New tables:

```text
autonomy_events
market_assets
market_observations
market_levels
news_events
trade_signals
paper_portfolios
paper_orders
paper_fills
paper_positions
portfolio_snapshots
```

### `autonomy_events`

Generic event log.

Columns:

```text
id string primary key
event_type string not null
symbol string nullable
payload_json JSON not null
created_at timestamptz server_default now()
```

### `market_assets`

```text
symbol string primary key
display_name string
kind string
source string
dex string nullable
sz_decimals int nullable
max_leverage int nullable
metadata_json JSON
updated_at timestamptz
created_at timestamptz
```

### `market_observations`

Downsampled feature snapshots.

```text
id string primary key
symbol string index
timestamp_ms bigint index
mid float nullable
mark float nullable
oracle float nullable
funding_hourly float nullable
open_interest float nullable
day_volume_usd float nullable
features_json JSON
created_at timestamptz
```

Index:

```text
(symbol, timestamp_ms desc)
```

### `market_levels`

```text
id string primary key
symbol string index
kind string index
price float
strength float
timeframe string
source string
first_seen_ms bigint
last_seen_ms bigint
expires_at_ms bigint nullable
metadata_json JSON
created_at timestamptz
```

### `news_events`

```text
id string primary key
provider string
source string
title text
text text
url text nullable
author_id string nullable
created_at_ms bigint nullable
observed_at_ms bigint index
importance_score float
sentiment string
assets_json JSON
metadata_json JSON
created_at timestamptz
```

### `trade_signals`

```text
id string primary key
symbol string index
side string
signal_type string
status string index
score float
confidence float
created_at_ms bigint
expires_at_ms bigint
entry_px float
stop_px float
take_profit_px float nullable
thesis text
invalidation text
evidence_json JSON
feature_snapshot_json JSON
risk_plan_json JSON
model_insight_json JSON nullable
discord_channel_id string nullable
discord_message_id string nullable
approved_by_discord_user_id string nullable
approved_at timestamptz nullable
rejected_by_discord_user_id string nullable
rejected_at timestamptz nullable
created_at timestamptz
```

### `paper_portfolios`

```text
id string primary key
name string unique
status string
initial_equity_usd float
cash_usd float
realized_pnl_usd float
metadata_json JSON
created_at timestamptz
updated_at timestamptz
```

### `paper_orders`

```text
id string primary key
portfolio_id string fk
signal_id string fk nullable
symbol string index
side string
order_type string
status string index
quantity float
requested_px float nullable
filled_px float nullable
stop_px float nullable
take_profit_px float nullable
fee_bps float
slippage_bps float
created_at timestamptz
filled_at timestamptz nullable
cancelled_at timestamptz nullable
metadata_json JSON
```

### `paper_fills`

```text
id string primary key
order_id string fk
portfolio_id string fk
symbol string index
side string
quantity float
price float
fee_usd float
slippage_usd float
created_at timestamptz
metadata_json JSON
```

### `paper_positions`

```text
id string primary key
portfolio_id string fk
symbol string index
side string
status string index
quantity float
avg_entry_px float
mark_px float nullable
stop_px float
take_profit_px float nullable
realized_pnl_usd float
unrealized_pnl_usd float
opened_at timestamptz
closed_at timestamptz nullable
metadata_json JSON
```

### `portfolio_snapshots`

```text
id string primary key
portfolio_id string fk
timestamp_ms bigint index
cash_usd float
equity_usd float
gross_exposure_usd float
net_exposure_usd float
realized_pnl_usd float
unrealized_pnl_usd float
total_pnl_usd float
drawdown_pct float
sharpe float nullable
metrics_json JSON
created_at timestamptz
```

## Repository Methods

Add methods to `Repository`:

```python
upsert_market_asset(...)
record_market_observation(...)
upsert_market_levels(...)
record_news_event(...)
create_trade_signal(...)
update_trade_signal_status(...)
get_trade_signal(...)
list_trade_signals(...)
create_or_get_paper_portfolio(...)
create_paper_order(...)
mark_paper_order_filled(...)
upsert_paper_position(...)
close_paper_position(...)
list_paper_positions(...)
record_paper_fill(...)
record_portfolio_snapshot(...)
get_latest_portfolio_snapshot(...)
list_portfolio_snapshots(...)
record_autonomy_event(...)
```

All repository writes remain best-effort where appropriate, but portfolio/order/fill writes should fail loudly inside the paper portfolio service because consistency matters.

## Discord Behavior

Use `#ai-bot-alerts` via:

```env
AUTONOMY_ALERT_CHANNEL_ID=<channel-id>
```

Signal post format:

```text
🚨 AI Trading Signal — HYPE LONG

Score: 82/100 | Confidence: 0.71 | Expires: 30m
Entry: 34.20
Stop: 33.45
TP: 36.10
RR: 2.5

Why:
- Reclaimed 1h resistance with positive orderflow.
- Funding neutral; OI rising without extreme premium.
- Bid depth > ask depth inside 10 bps.
- BTC/ETH regime supportive.

Risks:
- Break below 33.45 invalidates.
- Thin top-book depth above 36.00.

Human signoff required:
approve signal <id>
reject signal <id>

No live trade will be placed. Approval creates a paper trade only.
```

Discord commands in alert channel:

```text
approve signal <id>
reject signal <id>
signal <id>
signals
portfolio
positions
orders
market map
pause autonomy
resume autonomy
```

Authorization:

- Approval/rejection/pause/resume require:
  - `AUTONOMY_ADMIN_USER_IDS`, or
  - `AUTONOMY_ADMIN_ROLE_IDS`, or
  - existing `DISCORD_ADMIN_USER_IDS`.

## API Endpoints

Protected with existing `_require_agent_api`.

Add:

```http
GET  /autonomy/status
POST /autonomy/pause
POST /autonomy/resume

GET  /autonomy/universe
GET  /autonomy/market-map
GET  /autonomy/market-map/{symbol}

GET  /autonomy/signals
GET  /autonomy/signals/{signal_id}
POST /autonomy/signals/{signal_id}/approve
POST /autonomy/signals/{signal_id}/reject
POST /autonomy/signals/{signal_id}/expire

GET  /autonomy/portfolio
GET  /autonomy/portfolio/snapshots
GET  /autonomy/positions
GET  /autonomy/orders
GET  /autonomy/fills

GET  /autonomy/news
```

## Health and Metrics

Extend `/health/config`:

```json
"autonomy": {
  "enabled": true,
  "mode": "paper_signoff",
  "alert_channel_id_configured": true,
  "universe_count": 23,
  "hot_l2_assets": ["BTC", "ETH", "HYPE"],
  "signals_today": 2,
  "open_positions": 1,
  "model_insights_enabled": true
}
```

Extend `/ready`:

- If autonomy disabled: no effect.
- If enabled but Discord alert channel missing: degraded.
- If enabled and no market data for > 2 minutes: degraded.
- If portfolio persistence unavailable: degraded.

Add metrics:

```python
AUTONOMY_LOOP_ITERATIONS
AUTONOMY_MARKET_OBSERVATIONS
AUTONOMY_SIGNALS_CREATED
AUTONOMY_SIGNALS_POSTED
AUTONOMY_SIGNALS_APPROVED
AUTONOMY_SIGNALS_REJECTED
AUTONOMY_PAPER_ORDERS
AUTONOMY_PAPER_FILLS
AUTONOMY_PORTFOLIO_EQUITY
AUTONOMY_PORTFOLIO_DRAWDOWN
AUTONOMY_MODEL_INSIGHT_CALLS
NEWSWIRE_EVENTS
```

## Risk Controls

Hard V1 controls:

- No live execution.
- Human signoff required for every signal.
- Max signals/day.
- Max risk/trade.
- Max gross leverage.
- Max single-name exposure.
- Signal TTL.
- Duplicate signal suppression.
- Stop required.
- Portfolio kill switch through `pause autonomy`.
- All actions persisted in `autonomy_events`.
- All paper fills include fee/slippage assumptions.

## Hedge-Fund Best-Practice Shape

Use these institutional principles:

- **Event-driven architecture:** market data, news, signal, order, fill, portfolio events.
- **Separation of alpha and execution:** signal engine cannot execute; portfolio service cannot create alpha.
- **No LLM hot path:** models are slow advisory reviewers, not tick processors.
- **Replayability:** persist observations/signals/fills so decisions can be audited.
- **Risk first:** every signal has stop, invalidation, RR, exposure impact.
- **Paper/live parity:** paper order lifecycle mirrors future real order lifecycle.
- **Human-in-the-loop before autonomy:** signoff now, policy-gated automation later.
- **Performance truth:** portfolio metrics are the scoreboard.
- **Graceful degradation:** if models/news fail, deterministic market loop continues.
- **No hidden certainty:** inferred liquidations/stops are labeled as inferred.

## Testing Plan

Add tests for:

- Config defaults and validation.
- Universe resolver:
  - core assets,
  - top-volume inclusion,
  - unresolved HIP-3 alias warning.
- Market map reducer:
  - price updates,
  - trend/regime,
  - support/resistance dedupe.
- Orderflow:
  - spread,
  - depth buckets,
  - imbalance,
  - wall detection.
- Liquidation map:
  - direct public liquidation levels,
  - inferred clusters labeled correctly.
- Newswire:
  - RSS/search/X dedupe,
  - asset tagging,
  - importance scoring.
- Signal engine:
  - generates valid signal,
  - rejects missing stop,
  - rejects low RR,
  - suppresses duplicates,
  - respects daily cap.
- Model insight:
  - only triggers above threshold,
  - failure does not block signal.
- Paper portfolio:
  - approval creates paper order,
  - fill price/fee/slippage math,
  - long/short PnL,
  - stop close,
  - equity snapshots,
  - Sharpe insufficient-history behavior,
  - max drawdown.
- Discord:
  - signal formatting,
  - approve/reject command parsing,
  - auth enforcement.
- API:
  - auth required,
  - endpoints return expected shapes.
- Safety:
  - no SDK `Exchange`,
  - `exchange_actions=[]`,
  - `HYPERLIQUID_EXCHANGE_ENABLED=true` still rejected.
- Runtime:
  - service starts disabled by default,
  - readiness degrades on stale market data.

Validation commands:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy hyperliquid_trading_agent
uv run alembic upgrade head --sql >/tmp/autonomy_migration.sql
docker compose config
```

## Acceptance Criteria

- When `AUTONOMY_ENABLED=false`, current bot behavior is unchanged.
- When enabled with `AUTONOMY_MODE=paper_signoff`, the service watches configured assets continuously.
- `/health/config` shows autonomy status and universe.
- `#ai-bot-alerts` receives signal posts for qualifying setups.
- Every signal includes entry, stop, invalidation, RR, evidence, score, and signoff instructions.
- No paper order is created until an authorized user approves.
- Approved signals create paper orders/fills/positions.
- Portfolio can be queried by API and Discord.
- Portfolio shows holdings, cash/treasury, realized/unrealized PnL, Sharpe, max drawdown, exposure, and win rate.
- News/X events are persisted and attached to relevant assets/signals.
- Model insight can enrich high-value signals but cannot block the loop or execute trades.
- No live trade is placed under any V1 configuration.

## Rollout Plan

1. Ship code with `AUTONOMY_ENABLED=false`.
2. Run tests and migration SQL dry run.
3. Configure local `.env`:
   - `AUTONOMY_ENABLED=true`
   - `AUTONOMY_ALERT_CHANNEL_ID=<#ai-bot-alerts channel id>`
   - `AUTONOMY_MODE=paper_signoff`
4. Start with:
   - `AUTONOMY_CORE_UNIVERSE=BTC,ETH,HYPE`
   - `AUTONOMY_UNIVERSE_TOP_N_PERPS=5`
   - `AUTONOMY_MAX_SIGNALS_PER_DAY=3`
5. Observe Discord signal quality and portfolio metrics for 48h.
6. Expand universe to top 20 + HIP-3 aliases after stability.
7. Tune signal scoring thresholds based on paper PnL/drawdown.
8. Only after sustained paper performance, design a separate testnet/live execution plan.

## Assumptions

- `#ai-bot-alerts` will be configured by channel ID in `AUTONOMY_ALERT_CHANNEL_ID`.
- HIP-3 index symbols vary by DEX, so exact index ticker mapping is config-driven rather than hard-coded.
- V1 paper fills use mid-price plus configured slippage/fees.
- Sharpe is hourly-return based and unavailable until at least 30 hourly returns exist.
- Hidden stop-loss clusters are inferred unless official API data directly proves them.
- Human signoff remains mandatory in V1.
- Live execution remains impossible in V1.
