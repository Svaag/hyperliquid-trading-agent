---
created: 2026-06-14T21:55:14.193Z
source: pi-plan-mode
status: accepted-for-execution
---

# Live Position Tracking + WebSocket Level Alerts

## Summary

Add a deterministic, no-LLM live tracking loop that automatically arms alerts after high-stakes position reviews. The same canonical levels shown in the answer will be persisted and monitored via Hyperliquid WebSocket `allMids`. Alerts will go to the same Discord thread when available and will also be stored/queryable through API endpoints. The design keeps execution disabled now, but creates a clean event/action interface for later autonomous trading.

## Locked Decisions

- Auto-arm: every high-stakes position review with `coin + side + entry + stop`.
- Alert destination: same Discord thread by default; API/event storage always.
- Lifecycle: active until hard stop/technical exit trigger or 7 days.
- Initial trigger source: Hyperliquid WebSocket `allMids` mid-price crossings.
- Repeat policy: one alert per level; re-arm only after meaningful reset/hysteresis.
- User controls: deterministic natural-language controls in the same Discord thread.
- No LLM calls in the live loop.
- No signed exchange actions in this phase.

## Current State Grounding

Existing scaffolding to reuse:

- `hyperliquid_trading_agent/app/hyperliquid/ws_worker.py`
  - Already has optional Hyperliquid WebSocket support.
  - Already handles `allMids` and `activeAssetCtx` cache updates.
- `hyperliquid_trading_agent/app/main.py`
  - Already manages background tasks for Discord and optional WS worker.
- `hyperliquid_trading_agent/app/agent/high_stakes/graph.py`
  - Already derives deterministic position levels/rationale from market/candle features.
  - Already persists high-stakes `trade_proposals`.
- `hyperliquid_trading_agent/app/agent/high_stakes/features.py`
  - Already computes `recent_support`, `recent_resistance`, `entry`, `stop`, funding, L2, etc.
- `hyperliquid_trading_agent/app/db/models.py` / `repository.py`
  - Already persist conversations, decision runs, proposals, audit events.
- `hyperliquid_trading_agent/app/discord_bot.py`
  - Already supports bot-created thread continuation without mention.
- Safety config already rejects `HYPERLIQUID_EXCHANGE_ENABLED=true`.

## Implementation Steps

1. Add canonical tracking schemas and level-derivation logic.
2. Add database models, repository methods, and Alembic migration for trackers/events.
3. Refactor the existing Hyperliquid WebSocket worker into a dynamic fan-out stream.
4. Implement the live position tracking service and deterministic crossing engine.
5. Wire automatic tracker creation into high-stakes proposal finalization.
6. Add Discord alert delivery and natural-language tracking controls.
7. Add protected tracking API endpoints and health/config visibility.
8. Add metrics, logging, docs/config updates, and tests.

## Detailed Design

### 1. New package layout

Add:

```text
hyperliquid_trading_agent/app/tracking/
  __init__.py
  schemas.py
  levels.py
  service.py
  alerts.py
  commands.py
```

Responsibilities:

- `schemas.py`: Pydantic models/enums for tracking plans, levels, events.
- `levels.py`: deterministic derivation of tracked levels from `TradeProposal`, `TradeSetupDraft`, and high-stakes features.
- `service.py`: in-memory active tracker loop, WebSocket event consumption, crossing detection, persistence.
- `alerts.py`: Discord/API alert sinks.
- `commands.py`: deterministic Discord text-command parser.

### 2. Tracking schemas

Add canonical models:

```python
TrackerStatus = Literal[
    "pending",
    "active",
    "paused",
    "completed",
    "expired",
    "stopped",
    "error",
]

LevelKind = Literal[
    "hard_stop",
    "technical_exit",
    "entry_trim",
    "entry_reclaim",
    "resistance_confirm",
    "support_confirm",
    "take_profit",
]

CrossDirection = Literal["cross_up", "cross_down"]

class TrackedLevelSpec(BaseModel):
    id: str
    kind: LevelKind
    label: str
    price: float
    direction: CrossDirection
    terminal: bool = False
    severity: Literal["info", "warning", "critical"] = "warning"
    armed: bool = True
    hit_count: int = 0
    rearm_band_bps: float = 10.0
    source: str = "deterministic_position_levels"
    metadata: dict[str, Any] = {}

class PositionTrackingPlan(BaseModel):
    id: str
    proposal_id: str | None = None
    run_id: str | None = None
    coin: str
    side: Literal["long", "short"]
    entry: float
    stop: float
    take_profit: float | None = None
    current_price_at_arm: float | None = None
    price_source: Literal["allMids"] = "allMids"
    levels: list[TrackedLevelSpec]
    status: TrackerStatus = "pending"
    expires_at_ms: int
    discord_guild_id: str | None = None
    discord_channel_id: str | None = None
    discord_thread_id: str | None = None
    discord_user_id: str | None = None
    metadata: dict[str, Any] = {}
```

Extend high-stakes `TradeProposal` with:

```python
tracking_plan: dict[str, Any] | None = None
```

Use a dict to avoid tight import coupling, but validate via `PositionTrackingPlan` before persistence.

### 3. Canonical level derivation

Do **not** parse random numbers out of the rendered answer.

Instead, generate one canonical `PositionTrackingPlan`, and make the response formatter mention only levels from that plan. This guarantees “levels mentioned” equals “levels tracked.”

For long positions:

- `hard_stop`
  - price = user stop.
  - direction = `cross_down`.
  - terminal = `True`.
  - severity = `critical`.

- `technical_exit`
  - price = recent candle support if valid.
  - valid when `recent_support > stop` and `recent_support < current_price * 1.01`.
  - direction = `cross_down`.
  - terminal = `True`.
  - severity = `critical`.

- `entry_trim`
  - price = entry when current price is above entry.
  - direction = `cross_down`.
  - terminal = `False`.
  - severity = `warning`.

- `entry_reclaim`
  - price = entry when current price is below entry.
  - direction = `cross_up`.
  - terminal = `False`.
  - severity = `info`.

- `resistance_confirm`
  - price = recent candle resistance when above current.
  - direction = `cross_up`.
  - terminal = `False`.
  - severity = `info`.

- `take_profit`
  - price = take profit if supplied.
  - direction = `cross_up`.
  - terminal = `False` for now; alert only.

For short positions, mirror the logic:

- hard stop and technical exit trigger on `cross_up`.
- reclaim/support confirmation trigger on `cross_down`.
- entry trim/caution triggers when price crosses back through entry against the position.

Deduplication:

- Drop duplicate levels within `2 bps` of each other.
- Always keep `hard_stop` if duplicate conflict exists.
- Skip non-positive or non-finite prices.
- If fewer than two levels are available, still arm hard stop.

### 4. Database migration

Add Alembic migration:

```text
alembic/versions/0003_position_tracking.py
```

New tables:

#### `position_trackers`

Columns:

```text
id                      string(64) primary key
proposal_id             string(64) nullable, FK trade_proposals.id
run_id                  string(64) nullable, FK decision_runs.id
source                  string(64) not null default "auto_high_stakes"
status                  string(32) not null
coin                    string(64) not null
side                    string(16) not null
entry_px                float not null
stop_px                 float not null
take_profit_px          float nullable
current_px              float nullable
last_px                 float nullable
last_price_at_ms        bigint nullable
price_source            string(32) not null default "allMids"
expires_at              DateTime(timezone=True) not null
completed_at            DateTime(timezone=True) nullable
discord_guild_id        string(32) nullable
discord_channel_id      string(32) nullable
discord_thread_id       string(32) nullable
discord_user_id         string(32) nullable
plan_json               JSON not null
metadata_json           JSON not null
created_at              DateTime(timezone=True) server_default now()
updated_at              DateTime(timezone=True) nullable
```

Indexes:

```text
ix_position_trackers_status_coin(status, coin)
ix_position_trackers_discord_thread(discord_thread_id)
ix_position_trackers_proposal_id(proposal_id)
```

#### `tracked_levels`

Columns:

```text
id                      string(64) primary key
tracker_id              string(64) FK position_trackers.id not null
kind                    string(64) not null
label                   text not null
price                   float not null
direction               string(16) not null
terminal                boolean not null
severity                string(16) not null
armed                   boolean not null
hit_count               integer not null default 0
rearm_band_bps          float not null default 10
last_triggered_at       DateTime(timezone=True) nullable
metadata_json           JSON not null
created_at              DateTime(timezone=True) server_default now()
```

Indexes:

```text
ix_tracked_levels_tracker_id(tracker_id)
ix_tracked_levels_kind(kind)
```

#### `tracking_events`

Columns:

```text
id                      string(64) primary key
tracker_id              string(64) FK position_trackers.id not null
level_id                string(64) nullable, FK tracked_levels.id
event_type              string(64) not null
coin                    string(64) not null
price                   float nullable
payload_json            JSON not null
alert_destination       string(64) nullable
alert_status            string(32) nullable
created_at              DateTime(timezone=True) server_default now()
```

Indexes:

```text
ix_tracking_events_tracker_id_created_at(tracker_id, created_at)
ix_tracking_events_event_type(event_type)
```

### 5. Repository methods

Add to `Repository`:

```python
create_position_tracker(plan: PositionTrackingPlan, proposal_id: str | None, run_id: str | None) -> str | None
get_active_position_trackers() -> list[dict[str, Any]]
get_position_tracker(tracker_id: str) -> dict[str, Any] | None
list_position_trackers(status: str | None = None, coin: str | None = None, discord_thread_id: str | None = None) -> list[dict[str, Any]]
update_position_tracker_price(tracker_id: str, current_px: float, previous_px: float | None, timestamp_ms: int) -> None
update_tracked_level_state(level_id: str, armed: bool, hit_count: int, last_triggered_at: datetime | None) -> None
set_position_tracker_status(tracker_id: str, status: str, reason: str = "") -> None
record_tracking_event(...) -> str | None
```

All methods must be best-effort like existing audit/tool persistence: failures log warnings but do not break user answers.

### 6. WebSocket worker refactor

Refactor `HyperliquidWebSocketWorker` instead of adding a separate WS client.

Add:

```python
async def subscribe(self, spec: SubscriptionSpec, callback: Callable[[dict[str, Any]], Awaitable[None] | None]) -> str
async def unsubscribe(self, subscription_id: str) -> None
def status(self) -> dict[str, Any]
```

Extend `SubscriptionSpec` with optional `interval`.

Supported identifiers initially:

- `allMids`
- `activeAssetCtx:<coin>`
- `bbo:<coin>` later
- `trades:<coin>` later

Behavior:

- Dynamic subscriptions are persisted in memory and re-sent after reconnect.
- If no subscriptions exist, worker waits and does not open a WebSocket connection.
- If `HYPERLIQUID_WS_ENABLED=true`, keep existing cache behavior by adding static `allMids`.
- If `POSITION_TRACKING_ENABLED=true`, tracking service subscribes to `allMids` only while active trackers exist.
- Incoming messages update cache and fan out to callbacks.
- Callback failures are caught/logged and do not kill the WebSocket loop.

### 7. Tracking service

Add `PositionTrackingService`.

Startup behavior:

1. Load active trackers from DB.
2. Expire trackers whose `expires_at <= now`.
3. If active trackers exist, subscribe to `allMids`.
4. Start two tasks:
   - `price_event_loop`
   - `periodic_reload_loop`

Runtime behavior:

- Receives `allMids` messages.
- Filters to coins with active trackers.
- Evaluates crossings using last persisted/in-memory price.
- Persists every level-hit event.
- Sends Discord alert if tracker has `discord_thread_id`.
- Marks terminal trackers completed on hard stop or technical exit.
- Re-arms non-terminal levels only after price moves away by `rearm_band_bps`.

Crossing rules:

```text
cross_down hit:
  previous_price > level.price and current_price <= level.price

cross_up hit:
  previous_price < level.price and current_price >= level.price
```

Initial breach rule:

- On first tick after arming, if current price is already beyond a `hard_stop` or `technical_exit`, immediately emit an “already breached” alert and complete the tracker.
- For non-terminal confirmation levels, require an actual crossing after arm.

Re-arm rules:

```text
cross_down level re-arms when current_price >= level.price * (1 + rearm_band_bps / 10000)

cross_up level re-arms when current_price <= level.price * (1 - rearm_band_bps / 10000)
```

Default config:

```env
POSITION_TRACKING_ENABLED=true
POSITION_TRACKING_AUTO_ARM=true
POSITION_TRACKING_DEFAULT_TTL_HOURS=168
POSITION_TRACKING_PRICE_SOURCE=allMids
POSITION_TRACKING_REARM_BAND_BPS=10
POSITION_TRACKING_RELOAD_SECONDS=10
POSITION_TRACKING_MAX_ACTIVE=250
POSITION_TRACKING_ALERT_RETRY_COUNT=3
```

Implementation detail:

- Default can be `true` because the worker is lazy and will not connect until a tracker exists.
- Tests should use `POSITION_TRACKING_ENABLED=false` or fake service unless explicitly testing tracking.

### 8. Auto-arm integration

Modify `HighStakesDebateGraph` constructor:

```python
tracking_registry: PositionTrackingRegistry | None = None
```

In `_build_proposal` or finalization:

1. Build the canonical tracking plan from deterministic features and draft.
2. Attach `proposal.tracking_plan`.
3. Persist `trade_proposal`.
4. Call `tracking_registry.auto_arm(...)` with:
   - proposal
   - proposal_id
   - run_id
   - agent context
5. If arming succeeds, final response includes:

```text
Live tracking:
- Armed in this thread for: hard stop 15.5, technical exit 15.629, entry trim 16.4, resistance confirm 16.904.
- Expires in 7 days or when a terminal exit/stop level hits.
- Say "tracking status" or "stop tracking" in this thread.
```

If arming fails:

```text
Live tracking:
- Not armed: <short reason>.
```

Do not call any model during arming.

### 9. Discord alerts

Add `DiscordAlertSink`.

Behavior:

- Uses the existing Discord client from `DiscordTradingBot`.
- Sends to `discord_thread_id` when available.
- If thread fetch/send fails, record `alert_failed` event.
- No configured global channel is required in v1.

Alert format example:

```text
🚨 VVV level hit — technical exit

VVV long from 16.40 just crossed down through 15.629.
Current mid: 15.62.

This was the preplanned reduce/exit trigger before the hard stop at 15.50.
Tracker is now completed. No trade was placed.
```

For non-terminal bullish confirmation:

```text
✅ VVV level hit — resistance confirm

VVV crossed up through 16.904.
Current mid: 16.93.

This improves the hold case from the original review. Tracker remains active.
No trade was placed.
```

### 10. Discord natural-language controls

Before sending a thread-continuation message to `TradingAgentRunner`, detect tracking commands deterministically.

Supported commands:

```text
tracking status
are you tracking?
stop tracking
pause tracking
resume tracking
tracking events
track until 24h
track until 7d
```

Scope:

- Commands in a Discord thread apply to active trackers tied to that thread.
- If multiple active trackers exist in the thread, status lists all.
- `stop tracking VVV` targets only VVV.
- `stop tracking <tracker_id>` targets exact tracker.

No LLM call should occur for these commands.

### 11. API endpoints

Add protected endpoints:

```http
GET  /tracking/positions
GET  /tracking/positions/{tracker_id}
GET  /tracking/positions/{tracker_id}/events
POST /tracking/positions/{tracker_id}/pause
POST /tracking/positions/{tracker_id}/resume
POST /tracking/positions/{tracker_id}/stop
```

Auth:

- Reuse `_require_agent_api`.
- In prod, require `AGENT_API_BEARER_TOKEN`.

Response shape for `GET /tracking/positions/{tracker_id}`:

```json
{
  "id": "...",
  "status": "active",
  "coin": "VVV",
  "side": "long",
  "entry": 16.4,
  "stop": 15.5,
  "current_price": 16.25,
  "expires_at": "...",
  "levels": [
    {
      "kind": "technical_exit",
      "price": 15.629,
      "direction": "cross_down",
      "armed": true,
      "hit_count": 0
    }
  ],
  "discord_thread_id": "...",
  "proposal_id": "..."
}
```

### 12. Health/config visibility

Extend `/health/config`:

```json
"position_tracking": {
  "enabled": true,
  "auto_arm": true,
  "price_source": "allMids",
  "default_ttl_hours": 168,
  "rearm_band_bps": 10,
  "max_active": 250,
  "active_count": 3,
  "ws_status": {
    "connected": true,
    "subscriptions": ["allMids"]
  }
}
```

Extend `/ready`:

- If tracking enabled but no active trackers, status can still be ready.
- If active trackers exist and WS has been disconnected for more than 2 minutes, report degraded:

```json
"position_tracking": "degraded:websocket_stale"
```

### 13. Metrics

Add to `metrics.py`:

```python
HL_WS_MESSAGES = Counter("hyperliquid_trading_agent_hl_ws_messages_total", "...", ["channel"])
HL_WS_RECONNECTS = Counter("hyperliquid_trading_agent_hl_ws_reconnects_total", "...")

POSITION_TRACKERS = Gauge("hyperliquid_trading_agent_position_trackers", "...", ["status"])
POSITION_TRACKING_EVENTS = Counter("hyperliquid_trading_agent_position_tracking_events_total", "...", ["event_type", "level_kind"])
POSITION_TRACKING_ALERTS = Counter("hyperliquid_trading_agent_position_tracking_alerts_total", "...", ["destination", "result"])
POSITION_TRACKING_PRICE_UPDATES = Counter("hyperliquid_trading_agent_position_tracking_price_updates_total", "...", ["coin"])
```

### 14. Future autonomous trading bridge

Add an internal deterministic event object:

```python
class LevelHitEvent(BaseModel):
    tracker_id: str
    coin: str
    side: str
    level_kind: str
    level_price: float
    current_price: float
    recommended_action: Literal["notify", "trim", "exit", "confirm_hold"]
    exchange_actions: list[dict[str, Any]] = []
```

For this phase:

- `exchange_actions` is always `[]`.
- No `Exchange` SDK import.
- No private keys.
- No signed payloads.

Later autonomous trading can consume the same `LevelHitEvent` through a gated execution policy without changing the alerting/tracking core.

## Edge Cases

- DB unavailable: high-stakes answer still returns, but live tracking says not armed.
- No Discord context: tracker is persisted and API-queryable, but no Discord alert is sent.
- Service restart: reload active trackers and continue using persisted `last_px` and level armed state.
- Coin missing from `allMids`: tracker remains pending/active but no alerts until price appears.
- Multiple trackers for same coin/thread: allowed; command responses list all.
- Expired tracker: marked `expired`, unsubscribed if no active trackers remain.
- Alert send failure: record `alert_failed`, retry up to 3 times, do not block price processing.
- Terminal hit: mark tracker `completed`; future commands can resume only by explicit `resume tracking`.

## Test Plan

Add tests for:

- Tracking plan derivation for VVV long:
  - hard stop
  - technical exit
  - entry trim/reclaim
  - resistance confirm
- Long and short crossing detection.
- Re-arm hysteresis.
- Terminal vs non-terminal level behavior.
- Initial already-breached hard stop.
- WebSocket worker `allMids` fan-out.
- Tracker persistence repository methods with fake/integration session.
- High-stakes auto-arm with fake tracking registry.
- Discord tracking command parser.
- Discord alert formatting.
- API auth for tracking endpoints.
- `/health/config` includes tracking status.
- No model gateway calls from tracking service.
- Existing safety: `exchange_actions=[]`, `HYPERLIQUID_EXCHANGE_ENABLED=true` still rejected.

Validation commands:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy hyperliquid_trading_agent
uv run alembic upgrade head --sql >/tmp/hla_tracking_migration.sql
docker compose config
```

## Acceptance Criteria

- Same VVV question returns a useful review plus a clear “Live tracking armed” section.
- No user needs to explicitly ask to start tracking after a valid position review.
- If VVV crosses any tracked level, the bot alerts in the same Discord thread.
- Alerts include exact level, current mid, trigger meaning, and no-trade statement.
- Tracking loop uses WebSocket data and performs zero LLM calls.
- Trackers survive service restarts.
- Users can say `tracking status` and `stop tracking` in the thread.
- API can list trackers and events.
- No signed exchange execution is introduced.
