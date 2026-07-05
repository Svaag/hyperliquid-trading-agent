from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hyperliquid_trading_agent.app.vault import load_vault_environment

# Paid OpenRouter model pool (June 2026). Free-tier latency was unfixable, so the debate
# runs on paid models sized to each role: frontier reasoners on the decision spine,
# cheap-but-strong reasoners on the parallel reviewers.
_OPUS = "openrouter:anthropic/claude-opus-4.8"  # #1 AA Index, GDPval (financial) leader
_SONNET = "openrouter:anthropic/claude-sonnet-4.6"  # best on Finance-Agent bench
_QWEN_MAX = "openrouter:qwen/qwen3.7-max"  # top reasoning+agentic, 1M ctx
_GEMINI_PRO = "openrouter:google/gemini-3.1-pro-preview"  # leads reasoning/data-analysis
_GROK = "openrouter:x-ai/grok-4.3"  # strong agentic/tool-use, cheap frontier
_DS_PRO = "openrouter:deepseek/deepseek-v4-pro"  # #2 open reasoning, GDPval value leader
_DS_FLASH = "openrouter:deepseek/deepseek-v4-flash"  # near-Pro reasoning, cheapest workhorse
_RING = "openrouter:inclusionai/ring-2.6-1t"  # elite reasoning (ARC-AGI/AIME), very cheap
_MIMO = "openrouter:xiaomi/mimo-v2.5"  # Pro-level agentic at half cost
_MINIMAX = "openrouter:minimax/minimax-m3"  # best agentic tool-use (weak raw reasoning)

DEFAULT_MODEL_CHAIN = ",".join([_DS_FLASH, _MIMO, _DS_PRO])

ROLE_ORDER = ["analyst", "quant", "research", "risk", "treasury", "execution", "adversary", "judge"]

# Role -> model chain grid (primary first, then reliable paid fallbacks). The decision
# spine (analyst -> adversary -> judge) uses three distinct frontier labs (Alibaba ->
# Google -> Anthropic) for genuine cross-examination; the analyst is distinct from every
# reviewer (so no reviewer grades its own draft) and quant != risk. Six model families
# span the eight roles. See DEBATE_INTERACTION_EDGES for the enforced relations.
DEFAULT_DEBATE_ROLE_MODEL_CHAINS = {
    "analyst": ",".join([_QWEN_MAX, _DS_PRO, _MIMO]),
    "quant": ",".join([_DS_PRO, _DS_FLASH, _RING]),
    "research": ",".join([_MIMO, _DS_FLASH, _MINIMAX]),
    "risk": ",".join([_RING, _DS_FLASH, _MIMO]),
    "treasury": ",".join([_DS_FLASH, _MIMO]),
    "execution": ",".join([_DS_FLASH, _MIMO, _MINIMAX]),
    "adversary": ",".join([_GEMINI_PRO, _GROK, _DS_PRO]),
    "judge": ",".join([_OPUS, _SONNET, _DS_PRO]),
}

# Roles that directly scrutinise each other's output. Sharing a primary model across any
# of these edges means one model effectively reviews its own homework, so the grid keeps
# the endpoints on distinct primaries. Independent parallel reviewers (e.g. treasury vs
# execution) are deliberately NOT linked here, so they may share a cheap reviewer model.
DEBATE_INTERACTION_EDGES: tuple[tuple[str, str], ...] = (
    # Decision spine: proposer -> adversary -> judge.
    ("analyst", "adversary"),
    ("analyst", "judge"),
    ("adversary", "judge"),
    # Every reviewer grades the analyst's draft.
    ("analyst", "quant"),
    ("analyst", "research"),
    ("analyst", "risk"),
    ("analyst", "treasury"),
    ("analyst", "execution"),
    # The adversary attacks using the reviewers' findings.
    ("adversary", "quant"),
    ("adversary", "research"),
    ("adversary", "risk"),
    ("adversary", "treasury"),
    ("adversary", "execution"),
    # The judge weighs every reviewer.
    ("judge", "quant"),
    ("judge", "research"),
    ("judge", "risk"),
    ("judge", "treasury"),
    ("judge", "execution"),
    # Quant and risk cross-check the same numbers, so keep them diverse.
    ("quant", "risk"),
)
DEFAULT_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
    ",https://cointelegraph.com/rss"
    ",https://www.federalreserve.gov/feeds/press_all.xml"
)
# Newswire reliability layer: filings, halts, press releases, macro, crypto. All keyless.
# GlobeNewswire / Business Wire / ECB feeds are easy to add via NEWSWIRE_RSS_FEEDS.
DEFAULT_NEWSWIRE_RSS_FEEDS = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom"
    ",http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
    ",https://www.federalreserve.gov/feeds/press_all.xml"
    ",https://www.coindesk.com/arc/outboundfeeds/rss/"
    ",https://cointelegraph.com/rss"
)
DEFAULT_AUTONOMY_EVAL_HORIZONS = "15m,1h,4h,24h,expiry"
DEFAULT_AUTONOMY_EVENT_EVAL_HORIZONS = "15m,1h,4h,24h,72h"
DEFAULT_AUTONOMY_MEMORY_PROMPT_ROLES = "analyst,quant,research,adversary,judge"
AUTONOMY_ALLOWED_EVAL_HORIZONS = {"5m", "15m", "1h", "4h", "24h", "72h", "expiry"}
AUTONOMY_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
EngineAlphaCatalogMode = Literal["wave1a_locked", "wave1c", "shadow_full_catalog", "specs_only"]


class ServiceRole(StrEnum):
    API = "api"
    NEWSWIRE = "newswire"
    WORLD_MODEL = "world_model"
    TRADER = "trader"
    DISCORD_PUBLISHER = "discord_publisher"
    DISCORD_BOT = "discord_bot"
    AGENT = "agent"
    LIQUIDATIONS = "liquidations"
    SCHEDULER = "scheduler"


LEGACY_RUNTIME_PROFILES = {"full", "dashboard_only", "world_model_live"}


class Settings(BaseSettings):
    """Runtime settings loaded from environment and .env.

    Secrets stay in environment variables. Defaults are safe for local development:
    Discord is disabled if no token is set, WebSocket streaming is disabled, and
    exchange/trading actions are explicitly disabled.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    service_name: str = "hyperliquid-trading-agent"
    environment: str = "dev"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8080
    public_hostname: str = ""
    service_role: ServiceRole = Field(default=ServiceRole.API, validation_alias="SERVICE_ROLE")
    runtime_profile: str = "dev"

    discord_bot_enabled: bool = False
    discord_publisher_enabled: bool = False
    service_heartbeat_interval_seconds: int = 15
    service_heartbeat_stale_seconds: int = 90
    worker_command_poll_seconds: float = 1.0
    worker_command_claim_stale_seconds: int = 300
    consumer_poll_seconds: float = 1.0
    consumer_batch_size: int = 100

    database_url: str = "postgresql+asyncpg://hlagent:hlagent@postgres:5432/hlagent"

    # Liquidation flow monitor (source-graded multi-venue liquidation feed).
    # Disabled by default so the subsystem never starts adapters in tests/CI.
    liquidations_enabled: bool = False
    liquidations_demo_enabled: bool = False  # local-only synthetic feed; never on a public deploy
    liquidations_recent_buffer: int = 5000  # in-memory tape size for /api/recent
    # Per-venue adapters (each gated independently; also require liquidations_enabled).
    liquidations_aster_enabled: bool = False
    liquidations_lighter_enabled: bool = False
    liquidations_hl_public_enabled: bool = False
    liquidations_hl_user_enabled: bool = False
    # Endpoints (overridable for testnet / self-host).
    aster_ws_url: str = "wss://fstream.asterdex.com"
    lighter_ws_url: str = "wss://mainnet.zklighter.elliot.ai/stream"
    lighter_markets_url: str = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
    lighter_max_markets: int = 120  # fallback subscribe range if the market list can't be fetched
    # Hyperliquid public-derived: emit liquidation_pressure only for sweeps >= this notional.
    hl_pressure_min_notional_usd: float = 50000.0
    hl_public_coins: str = "BTC,ETH,SOL,HYPE"  # csv of coins to watch on the public trades feed
    # Hyperliquid account-exact: addresses to watch (csv) + the HLP liquidator vault.
    liquidations_hl_watch_addresses: str = ""
    hl_liquidator_vault_address: str = ""
    # Public value-add export API (Phase 2): durable history + CSV/NDJSON/JSON export,
    # open + IP rate-limited. Read-only over the store; 503s when no DB is configured.
    liquidations_export_enabled: bool = True
    liquidations_trust_proxy: bool = False  # honor X-Forwarded-For only behind a trusted proxy
    liquidations_export_rate_per_min: float = 120.0
    liquidations_export_burst: int = 60
    liquidations_export_max_rows: int = 100_000  # hard cap per export request
    liquidations_export_max_range_ms: int = 7 * 24 * 3_600_000  # 7d max time range per query
    liquidations_stats_max_buckets: int = 5000  # reject stats requests that would exceed this
    # Derived-vs-confirmed reconciliation harness (Phase 2; off until a confirmed source exists).
    liquidations_reconcile_enabled: bool = False
    liquidations_reconcile_window_ms: int = 3_600_000
    liquidations_reconcile_bucket_ms: int = 1000  # match the HL derived dedupe second-bucket
    # Hyperliquid confirmed/global via managed gRPC StreamFills provider (Phase 2-live; transport gated).
    liquidations_hl_grpc_enabled: bool = False
    hl_grpc_endpoint: str = ""  # host:port of the managed StreamFills provider (no scheme)
    hl_grpc_api_key: str = ""
    hl_grpc_provider: str = ""  # provider label for the vendor badge, e.g. "thunderhead"
    hl_grpc_auth_header: str = "x-api-key"  # metadata header carrying hl_grpc_api_key
    hl_grpc_use_tls: bool = True  # managed providers require TLS; off only for local plaintext
    hl_grpc_resume_lookback_ms: int = 5000  # cold-start: stream from now-this; then resume at last fill

    vault_enabled: bool = False
    vault_addr: str = "http://127.0.0.1:8200"
    vault_namespace: str = ""
    vault_kv_mount: str = "kv"
    vault_kv_version: Literal["1", "2"] = "2"
    vault_secret_path: str = "hyperliquid-trading-agent/prod"
    vault_token_file: str = ""
    vault_env_override: bool = False
    vault_timeout_seconds: float = 3.0

    discord_bot_token: str = Field(default="", validation_alias="DISCORD_BOT_TOKEN")
    discord_allowed_guild_ids: str = ""
    discord_allowed_channel_ids: str = ""
    discord_allowed_role_ids: str = ""
    discord_admin_user_ids: str = ""
    discord_max_response_chars: int = 1900
    discord_chart_command_enabled: bool = False

    agent_model_chain: str = Field(default=DEFAULT_MODEL_CHAIN, validation_alias="AGENT_MODEL_CHAIN")
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.ai/v1"

    hyperliquid_mainnet_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_testnet_url: str = "https://api.hyperliquid-testnet.xyz"
    hyperliquid_mainnet_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    hyperliquid_testnet_ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    # HyperEVM JSON-RPC (chain 999), separate from Hyperliquid Core /info + WS above.
    # Tokened provider URLs belong in local .env or Vault, not in committed files.
    hyperliquid_evm_rpc_provider: str = ""
    hyperliquid_evm_rpc_url: str = ""
    hyperliquid_evm_ws_url: str = ""
    hyperliquid_network: Literal["mainnet", "testnet"] = "mainnet"
    hyperliquid_ws_enabled: bool = False
    hyperliquid_exchange_enabled: bool = False

    hip4_enabled: bool = False
    hip4_mode: Literal["read_only", "shadow", "paper_shadow"] = "paper_shadow"
    hip4_scan_enabled: bool = False
    hip4_paper_execution_enabled: bool = False
    hip4_manual_ticket_export_enabled: bool = False
    hip4_question_allowlist: str = ""
    hip4_max_questions: int = 25
    hip4_max_hot_questions: int = 10
    hip4_max_hot_outcome_sides: int = 120
    hip4_include_partially_settled: bool = False
    hip4_outcome_meta_refresh_seconds: int = 60
    hip4_settlement_refresh_seconds: int = 300
    hip4_registry_max_staleness_ms: int = 300_000
    hip4_scan_max_book_staleness_ms: int = 10_000
    hip4_paper_execution_max_book_staleness_ms: int = 5_000
    hip4_manual_ticket_max_book_staleness_ms: int = 3_000
    hip4_ws_enabled: bool = True
    hip4_probe_outcome_meta_ws: bool = False
    hip4_outcome_meta_ws_probe_timeout_seconds: float = 1.0
    hip4_docs_scope_status: Literal["verified_not_testnet_only", "testnet_only", "unknown"] = "unknown"
    hip4_ws_max_subscriptions: int = 150
    hip4_ws_resnapshot_on_reconnect: bool = True
    hip4_min_edge_bps: Decimal = Decimal("25")
    hip4_min_edge_usd: Decimal = Decimal("10")
    hip4_edge_threshold_mode: Literal["both", "either"] = "both"
    hip4_min_depth_usd: Decimal = Decimal("250")
    hip4_max_paper_notional_per_candidate_usd: Decimal = Decimal("10000")
    hip4_max_paper_daily_notional_usd: Decimal = Decimal("100000")
    hip4_paper_initial_equity_usd: Decimal = Decimal("100000")
    hip4_outcome_taker_fee_bps: Decimal = Decimal("0")
    hip4_outcome_maker_fee_bps: Decimal = Decimal("0")
    hip4_fee_stress_bps: Decimal = Decimal("10")
    hip4_allow_inventory_carry: bool = False
    hip4_allow_inferred_lot_size_for_paper: bool = False
    hip4_discord_digest_enabled: bool = True
    hip4_discord_digest_interval_seconds: int = 300
    hip4_alert_channel_id: str = ""
    hip4_proactive_loop_enabled: bool = False
    hip4_proactive_loop_interval_seconds: int = 30
    hip4_proactive_paper_execution_enabled: bool = False
    hip4_proactive_max_paper_executions_per_cycle: int = 1
    hip4_proactive_alert_min_edge_usd: Decimal = Decimal("10")
    hip4_proactive_alert_min_edge_bps: Decimal = Decimal("25")
    hip4_proactive_alert_dedupe_seconds: int = 300
    hip4_proactive_reconcile_interval_seconds: int = 300
    hip4_proactive_learning_enabled: bool = True

    position_tracking_enabled: bool = False
    position_tracking_auto_arm: bool = True
    position_tracking_default_ttl_hours: int = 168
    position_tracking_price_source: Literal["allMids"] = "allMids"
    position_tracking_rearm_band_bps: float = 10.0
    position_tracking_reload_seconds: int = 10
    position_tracking_max_active: int = 250
    position_tracking_alert_retry_count: int = 3

    high_stakes_debate_enabled: bool = False
    high_stakes_activation_policy: Literal["risk_routed", "explicit_only", "all_trading_questions"] = "risk_routed"
    high_stakes_prompt_style: Literal["standard", "aggressive"] = "standard"
    high_stakes_info_provider: Literal["sdk_preferred", "rest_only", "sdk_only"] = "sdk_preferred"
    high_stakes_max_rounds: int = 3
    high_stakes_timeout_seconds: int = 90
    high_stakes_review_concurrency: int = 3
    high_stakes_max_coins: int = 3
    high_stakes_max_data_escalations: int = 1
    high_stakes_require_account_for_autonomous: bool = False
    debate_model_diversity_policy: Literal["off", "warn", "strict"] = "warn"
    account_address_allowlist: str = ""
    high_stakes_smart_money_addresses: str = ""
    agent_api_bearer_token: str = ""

    paper_trading_enabled: bool = False
    paper_trading_require_confirm: bool = True
    prediction_market_paper_enabled: bool = True
    prediction_market_paper_initial_cash_usd: float = 10_000.0
    prediction_market_paper_default_stake_usd: float = 100.0
    prediction_market_paper_max_stake_usd: float = 1_000.0
    prediction_market_paper_draft_ttl_seconds: int = 300
    prediction_market_paper_settlement_sweep_seconds: int = 300
    prediction_market_search_max_staleness_seconds: int = 300

    autonomy_enabled: bool = False
    autonomy_mode: Literal["paper_signoff"] = "paper_signoff"
    autonomy_alert_channel_id: str = ""
    autonomy_require_human_signoff: bool = True
    autonomy_admin_user_ids: str = ""
    autonomy_admin_role_ids: str = ""
    autonomy_core_universe: str = "BTC,ETH,HYPE"
    autonomy_universe_top_n_perps: int = 20
    autonomy_hip3_dexs: str = ""
    autonomy_hip3_index_aliases: str = "SP500:SPX|SP500|SPY,NASDAQ100:NDX|NASDAQ|QQQ,NIKKEI225:NIKKEI|NKY,KOSPI:KOSPI"
    autonomy_loop_interval_seconds: int = 5
    autonomy_deep_scan_interval_seconds: int = 60
    autonomy_signals_run_with_engine_enabled: bool = False
    autonomy_l2_refresh_seconds: int = 15
    autonomy_candle_refresh_seconds: int = 60
    autonomy_news_refresh_seconds: int = 60
    autonomy_portfolio_snapshot_seconds: int = 60
    autonomy_max_tracked_assets: int = 40
    autonomy_max_hot_l2_assets: int = 5
    autonomy_max_signals_per_day: int = 10
    autonomy_signal_ttl_minutes: int = 30
    autonomy_min_signal_score: float = 75.0
    autonomy_paper_initial_equity_usd: float = 100_000.0
    autonomy_paper_risk_pct_per_trade: float = 0.25
    autonomy_paper_max_gross_leverage: float = 3.0
    autonomy_paper_max_single_name_exposure_pct: float = 20.0
    autonomy_paper_taker_fee_bps: float = 4.5
    autonomy_paper_maker_fee_bps: float = 1.5
    autonomy_paper_default_slippage_bps: float = 2.0
    autonomy_model_insights_enabled: bool = True
    autonomy_model_insight_min_score: float = 80.0
    autonomy_model_max_calls_per_hour: int = 12

    engine_enabled: bool = False
    engine_mode: Literal["paper_shadow"] = "paper_shadow"
    engine_execution_modes: str = "shadow"
    engine_event_retention_days: int = 7
    engine_feature_retention_days: int = 14
    engine_rollup_retention_days: int = 365
    engine_debate_enabled: bool = True
    engine_debate_max_per_day: int = 8
    engine_debate_priority_min: float = 0.35
    engine_min_net_ev_bps: float = 8.0
    engine_min_risk_adjusted_utility: float = 0.25
    engine_max_candidates_per_loop: int = 50
    engine_max_approved_candidates_per_loop: int = 5
    engine_model_artifact_dir: str = "/var/lib/hyperliquid-trading-agent/models"
    engine_approved_scorer_model_id: str = ""
    engine_scorer_fallback_mode: Literal["deterministic"] = "deterministic"
    engine_alpha_catalog_mode: EngineAlphaCatalogMode = "wave1a_locked"
    engine_cross_venue_dexes: str = ""
    engine_wave1c_enabled: bool = False
    engine_wave2_enabled: bool = False
    engine_loop_interval_seconds: int = 60
    engine_shadow_enabled: bool = True
    engine_paper_enabled: bool = False
    engine_live_enabled: bool = False
    engine_newsfeed_enabled: bool = True
    engine_news_min_importance: float = 35.0
    engine_news_min_source_score: float = 0.4
    engine_news_catalyst_threshold: float = 0.35
    engine_news_catalyst_ttl_seconds: int = 3600
    engine_news_macro_min_importance: float = 60.0
    engine_news_macro_proxy_symbols: str = ""
    engine_validation_digest_enabled: bool = True
    engine_validation_digest_interval_seconds: int = 3600
    engine_validation_alert_stale_loop_seconds: int = 180
    engine_validation_risk_reject_spike_count: int = 5
    engine_validation_missing_data_seconds: int = 300
    engine_validation_ev_drift_min_samples: int = 10
    engine_validation_ev_drift_loss_usd: float = -1.0
    engine_readiness_enabled: bool = True
    engine_readiness_window_hours: int = 24
    engine_readiness_min_runs: int = 100
    engine_readiness_min_candidates: int = 250
    engine_readiness_min_shadow_intents: int = 50
    engine_readiness_max_runtime_errors: int = 0
    engine_readiness_max_critical_alerts: int = 0
    engine_readiness_max_paper_intents_in_shadow: int = 0
    engine_readiness_max_live_intents: int = 0
    engine_readiness_min_ev_coverage_pct: float = 95.0
    engine_readiness_min_feature_coverage_pct: float = 95.0
    engine_readiness_min_regime_coverage_pct: float = 95.0
    engine_readiness_max_risk_reject_rate_pct: float = 25.0
    engine_readiness_min_allocation_rate_pct: float = 5.0
    engine_readiness_max_allocation_rate_pct: float = 60.0
    engine_readiness_max_strategy_allocation_share_pct: float = 55.0
    engine_diversity_controller_enabled: bool = True
    engine_diversity_lookback_hours: int = 24
    engine_diversity_strategy_target_share_pct: float = 45.0
    engine_diversity_strategy_hard_share_pct: float = 55.0
    engine_diversity_family_hard_share_pct: float = 60.0
    engine_diversity_symbol_strategy_hard_share_pct: float = 35.0
    engine_diversity_min_active_strategies_24h: int = 5
    engine_diversity_min_active_families_24h: int = 3
    engine_diversity_min_window_samples: int = 10
    engine_readiness_max_avg_slippage_bps: float = 8.0
    engine_readiness_max_fill_failure_rate_pct: float = 5.0
    engine_readiness_min_score_to_pass: int = 85
    engine_readiness_clean_window_start_ms: int = 0
    engine_readiness_min_active_strategy_count_24h: int = 5
    engine_readiness_min_active_strategy_family_count_24h: int = 3
    engine_readiness_max_strategy_family_allocation_share_pct: float = 60.0
    engine_readiness_max_symbol_strategy_allocation_share_pct: float = 35.0
    engine_readiness_min_candidate_strategy_metadata_coverage_pct: float = 100.0
    engine_readiness_min_candidate_evidence_link_coverage_pct: float = 100.0
    engine_readiness_min_council_packet_coverage_pct: float = 95.0
    engine_readiness_min_candidate_risk_gateway_coverage_pct: float = 100.0
    engine_readiness_min_matured_outcome_attribution_coverage_pct: float = 95.0
    engine_readiness_min_council_review_coverage_pct: float = 95.0
    engine_readiness_min_risk_gateway_coverage_pct: float = 100.0
    engine_readiness_min_strategy_regime_evidence_coverage_pct: float = 95.0
    engine_readiness_min_strategy_regime_sample_count: int = 5
    engine_readiness_min_strategy_regime_score: float = 45.0
    engine_readiness_require_latest_replay: bool = True
    engine_readiness_min_replay_window_hours: int = 24
    engine_readiness_min_replay_sample_size: int = 50
    engine_pnl_attribution_interval_seconds: int = 300
    engine_strategy_throttles_enabled: bool = True
    engine_strategy_max_candidates_per_loop: int = 15
    engine_strategy_max_allocations_per_loop: int = 3
    engine_strategy_max_allocation_share_pct: float = 55.0
    engine_strategy_throttle_lookback_hours: int = 24
    engine_strategy_throttle_cooldown_loops: int = 3
    engine_pnl_attribution_enabled: bool = False
    engine_pnl_attribution_mark_source: str = "all_mids"
    engine_pnl_attribution_close_on_expired_horizon: bool = True
    engine_pnl_attribution_max_position_age_hours: int = 48
    engine_pnl_attribution_min_mark_interval_seconds: int = 60

    # Agentic wave orchestration. These flags enable observation/diagnosis,
    # report-only maintenance, trace emission, and bounded handoff escalation.
    # They must not directly mutate runtime wave, paper, or live-execution flags.
    orchestration_wave_supervisor_enabled: bool = False
    orchestration_wave_supervisor_interval_seconds: int = 900
    orchestration_wave_supervisor_maintenance_enabled: bool = True
    orchestration_wave_supervisor_bandit_enabled: bool = False
    orchestration_wave_supervisor_escalation_enabled: bool = False
    orchestration_wave_supervisor_escalation_transport: Literal["disabled", "github_issue"] = "disabled"
    orchestration_wave_supervisor_handoff_repo: str = "Svaag/hyperliquid-trading-agent"
    orchestration_wave_supervisor_handoff_labels: str = "loop:candidate,hyperliquid-wave"
    orchestration_wave_supervisor_github_token: str = ""
    orchestration_wave_supervisor_request_timeout_seconds: float = 10.0
    agent_core_trace_enabled: bool = False
    agent_core_trace_path: str = ""
    agent_core_trace_collector_url: str = ""
    agent_core_trace_collector_token: str = ""

    autonomy_evaluation_enabled: bool = True
    autonomy_event_evaluation_enabled: bool = True
    autonomy_memory_enabled: bool = True
    autonomy_reports_enabled: bool = True
    autonomy_eval_horizons: str = DEFAULT_AUTONOMY_EVAL_HORIZONS
    autonomy_eval_max_open_signals: int = 500
    autonomy_eval_price_source: Literal["allMids"] = "allMids"
    autonomy_event_eval_horizons: str = DEFAULT_AUTONOMY_EVENT_EVAL_HORIZONS
    autonomy_event_eval_min_importance: float = 50.0
    autonomy_event_eval_min_source_score: float = 0.4
    autonomy_event_eval_max_open_events: int = 1000
    autonomy_event_eval_symbols_per_event: int = 5
    autonomy_event_eval_macro_proxies: str = "BTC,ETH,SPY,QQQ"
    autonomy_event_eval_worked_bps: float = 50.0
    autonomy_event_eval_failed_bps: float = -35.0
    autonomy_event_eval_volatility_bps: float = 75.0
    autonomy_daily_report_enabled: bool = True
    autonomy_daily_report_utc: str = "00:05"
    autonomy_weekly_report_enabled: bool = True
    autonomy_weekly_report_day: str = "MON"
    autonomy_weekly_report_utc: str = "00:30"
    autonomy_memory_role_max_active: int = 200
    autonomy_memory_operator_max_active: int = 100
    autonomy_memory_candidate_ttl_days: int = 30
    autonomy_memory_shadow_ttl_days: int = 60
    autonomy_memory_role_ttl_days: int = 30
    autonomy_memory_process_ttl_days: int = 90
    autonomy_memory_incident_ttl_days: int = 14
    autonomy_role_lesson_min_samples: int = 5
    autonomy_operator_lesson_min_samples: int = 3
    autonomy_signal_lesson_min_samples: int = 20
    autonomy_lesson_min_confidence: float = 0.70
    autonomy_strategy_lesson_min_confidence: float = 0.75
    autonomy_tuning_proposals_enabled: bool = True
    autonomy_tuning_proposal_ttl_days: int = 14
    autonomy_memory_prompt_roles: str = DEFAULT_AUTONOMY_MEMORY_PROMPT_ROLES
    autonomy_memory_require_change_control_for_risk_execution: bool = True

    newswire_enabled: bool = False
    newswire_gateway_enabled: bool = True
    autonomy_legacy_news_poll_enabled: bool = False
    news_signal_generation_enabled: bool = True
    news_event_risk_blocks_enabled: bool = True
    newswire_queries: str = "BTC,ETH,HYPE,Hyperliquid,Fed,CPI,FOMC,crypto liquidation"
    x_watchlist_user_ids: str = ""
    x_min_public_metric_score: int = 0

    # Free-standing Newswire gateway (ingest -> normalize -> bus -> #news / agent / WS)
    newswire_discord_enabled: bool = True
    newswire_news_channel_id: str = ""
    newswire_digest_interval_seconds: int = 300
    newswire_news_min_importance: float = 60.0
    newswire_breaking_min_importance: float = 80.0
    newswire_agent_min_importance: float = 50.0
    newswire_max_events_buffer: int = 500
    newswire_send_min_interval_ms: int = 1200
    newswire_discord_digest_max_items: int = 10
    newswire_discord_startup_grace_seconds: int = 300
    newswire_rss_feeds: str = DEFAULT_NEWSWIRE_RSS_FEEDS
    newswire_rss_poll_seconds: int = 60
    newswire_llm_enrich_enabled: bool = True
    newswire_llm_enrich_min_importance: float = 70.0
    newswire_llm_enrich_max_calls_per_hour: int = 30
    newswire_policy_enabled: bool = True
    newswire_policy_shadow_only: bool = True
    newswire_active_policy_version: str = ""
    newswire_policy_min_reward_rows: int = 50

    world_model_adapters_enabled: bool = False
    world_model_adapter_max_items: int = 25
    world_model_adapter_timeout_seconds: float = 10.0
    world_model_adapter_poll_interval_seconds: float = 60.0
    world_model_adapter_dedupe_ttl_seconds: float = 3600.0
    world_model_dev_seed_enabled: bool = False
    world_model_streams_enabled: bool = False
    world_model_stream_stale_after_seconds: int = 120
    world_model_stream_reconnect_max_seconds: int = 60
    world_model_polymarket_enabled: bool = False
    world_model_polymarket_base_url: str = "https://gamma-api.polymarket.com"
    world_model_polymarket_ws_enabled: bool = False
    world_model_polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    world_model_polymarket_ws_ping_seconds: int = 10
    world_model_kalshi_enabled: bool = False
    world_model_kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    world_model_x_enabled: bool = False
    world_model_x_query: str = "(BTC OR ETH OR HYPE OR Hyperliquid) lang:en -is:retweet"
    world_model_tavily_enabled: bool = False
    world_model_tavily_base_url: str = "https://api.tavily.com/search"
    world_model_tavily_queries: str = "BTC crypto market,Fed macro markets,Hyperliquid HYPE"

    # Alpaca News WebSocket (free, Benzinga-sourced)
    alpaca_news_enabled: bool = False
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_news_ws_url: str = "wss://stream.data.alpaca.markets/v1beta1/news"
    alpaca_news_symbols: str = "*"

    # Trading Economics macro calendar WebSocket (guest:guest allowed)
    trading_economics_enabled: bool = False
    trading_economics_api_key: str = ""
    trading_economics_ws_url: str = "wss://stream.tradingeconomics.com/"

    # Curated X / Twitter newswire (reuses X_BEARER_TOKEN + X_WATCHLIST_USER_IDS)
    x_newswire_enabled: bool = False
    x_cashtags: str = "BTC,ETH,HYPE,SOL"
    x_poll_seconds: int = 30

    # --- TradFi (equities & options via Alpaca Data API) -----------------------
    tradfi_enabled: bool = False
    tradfi_provider_order: str = "alpha_vantage,alpaca"
    alpha_vantage_enabled: bool = False
    alpha_vantage_api_key: str = ""
    alpha_vantage_base_url: str = "https://www.alphavantage.co/query"
    alpha_vantage_mcp_url: str = "https://mcp.alphavantage.co/mcp"
    alpha_vantage_mcp_auth_header: str = "Authorization"
    alpha_vantage_mcp_auth_scheme: str = "Bearer"
    alpha_vantage_transport: Literal["auto", "mcp", "rest"] = "auto"
    alpha_vantage_timeout_seconds: float = 10.0
    alpaca_trading_enabled: bool = False  # gated like HYPERLIQUID_EXCHANGE_ENABLED
    alpaca_data_feed: Literal["iex", "sip", "delayed_sip"] = "iex"  # IEX = free

    # SEC EDGAR (public, keyless; set User-Agent contact for production)
    sec_edgar_user_agent: str = "hyperliquid-trading-agent/0.1 sec-edgar-contact@example.com"
    sec_edgar_timeout_seconds: float = 10.0
    sec_edgar_company_cache_ttl_seconds: int = 86_400
    sec_edgar_submissions_cache_ttl_seconds: int = 300

    # Equity-specific autonomy (separate from crypto)
    autonomy_equity_enabled: bool = False
    autonomy_equity_universe: str = ""  # e.g. AAPL,NVDA,MSFT,SPY,QQQ
    autonomy_equity_max_tracked_assets: int = 20
    autonomy_equity_max_signals_per_day: int = 5
    autonomy_equity_min_signal_score: float = 75.0
    autonomy_equity_signal_ttl_minutes: int = 60
    autonomy_equity_loop_interval_seconds: int = 30
    autonomy_equity_deep_scan_interval_seconds: int = 300

    # Equity paper portfolio (separate from crypto paper)
    autonomy_equity_paper_initial_equity_usd: float = 100_000.0
    autonomy_equity_paper_risk_pct_per_trade: float = 0.25
    autonomy_equity_paper_max_gross_leverage: float = 2.0
    autonomy_equity_paper_max_single_name_exposure_pct: float = 15.0
    autonomy_equity_paper_taker_fee_bps: float = 2.0
    autonomy_equity_paper_maker_fee_bps: float = 0.5
    autonomy_equity_paper_default_slippage_bps: float = 1.0

    # Options flow detection
    options_flow_enabled: bool = False
    options_flow_min_volume_oi_ratio: float = 3.0
    options_flow_min_premium: float = 1_000_000.0
    options_flow_llm_enrich_enabled: bool = True
    options_flow_llm_enrich_max_calls_per_hour: int = 10

    debate_analyst_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["analyst"]
    debate_quant_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["quant"]
    debate_research_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["research"]
    debate_adversary_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["adversary"]
    debate_risk_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["risk"]
    debate_treasury_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["treasury"]
    debate_execution_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["execution"]
    debate_judge_model_chain: str = DEFAULT_DEBATE_ROLE_MODEL_CHAINS["judge"]

    cache_ttl_market_seconds: int = 15
    cache_ttl_news_seconds: int = 600
    metrics_bearer_token: str = ""

    news_rss_feeds: str = DEFAULT_RSS_FEEDS
    tavily_api_key: str = ""
    serpapi_api_key: str = ""
    newsapi_api_key: str = ""
    perplexity_api_key: str = ""
    x_bearer_token: str = ""

    @field_validator("alpaca_trading_enabled")
    @classmethod
    def live_alpaca_trading_must_remain_disabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("ALPACA_TRADING_ENABLED must remain false for the MVP")
        return value

    @field_validator("engine_live_enabled")
    @classmethod
    def engine_live_must_remain_disabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("ENGINE_LIVE_ENABLED must remain false until a separate live-execution project is approved")
        return value

    @field_validator("engine_alpha_catalog_mode", mode="before")
    @classmethod
    def normalize_engine_alpha_catalog_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("engine_wave2_enabled")
    @classmethod
    def engine_wave2_must_remain_deferred(cls, value: bool) -> bool:
        if value:
            raise ValueError("ENGINE_WAVE2_ENABLED must remain false until Wave 1 outcome attribution, replay, and readiness gates are reliable")
        return value

    @field_validator("hyperliquid_exchange_enabled")
    @classmethod
    def mainnet_exchange_must_remain_disabled(cls, value: bool) -> bool:
        # MVP guardrail: no signed exchange actions are exposed by this service.
        if value:
            raise ValueError("HYPERLIQUID_EXCHANGE_ENABLED must remain false for the MVP")
        return value

    @field_validator("hip4_scan_enabled", "hip4_paper_execution_enabled", "hip4_manual_ticket_export_enabled")
    @classmethod
    def hip4_feature_flags_do_not_enable_live_execution(cls, value: bool) -> bool:
        # These flags only enable read/paper/manual-instruction features. They must never
        # imply signing, private keys, /exchange mutation, or live orders.
        return value

    @model_validator(mode="after")
    def service_role_boundaries(self) -> "Settings":
        env = self.environment.lower()
        if self.runtime_profile in LEGACY_RUNTIME_PROFILES and env in {"prod", "production"}:
            raise ValueError(
                f"RUNTIME_PROFILE={self.runtime_profile!r} is deprecated for production. "
                "Use SERVICE_ROLE to select process behavior."
            )
        role_explicit = "service_role" in self.model_fields_set
        if env in {"dev", "local", "development", "test"} and not role_explicit:
            return self

        role = self.service_role
        if role == ServiceRole.API:
            self._reject_enabled(
                {
                    "NEWSWIRE_ENABLED": self.newswire_enabled,
                    "WORLD_MODEL_STREAMS_ENABLED": self.world_model_streams_enabled,
                    "WORLD_MODEL_ADAPTERS_ENABLED": self.world_model_adapters_enabled,
                    "ENGINE_ENABLED": self.engine_enabled,
                    "ENGINE_PNL_ATTRIBUTION_ENABLED": self.engine_pnl_attribution_enabled,
                    "POSITION_TRACKING_ENABLED": self.position_tracking_enabled,
                    "AUTONOMY_ENABLED": self.autonomy_enabled,
                    "HIP4_ENABLED": self.hip4_enabled,
                    "ORCHESTRATION_WAVE_SUPERVISOR_ENABLED": self.orchestration_wave_supervisor_enabled,
                    "LIQUIDATIONS_ENABLED": self.liquidations_enabled,
                    "TRADFI_ENABLED": self.tradfi_enabled,
                    "HYPERLIQUID_WS_ENABLED": self.hyperliquid_ws_enabled,
                    "DISCORD_BOT_ENABLED": self.discord_bot_enabled,
                    "DISCORD_PUBLISHER_ENABLED": self.discord_publisher_enabled,
                },
                "SERVICE_ROLE=api must be passive",
            )
        elif role == ServiceRole.NEWSWIRE:
            self._require_enabled({"NEWSWIRE_ENABLED": self.newswire_enabled}, "SERVICE_ROLE=newswire")
            self._reject_enabled(
                {
                    "DISCORD_PUBLISHER_ENABLED": self.discord_publisher_enabled,
                    "ENGINE_ENABLED": self.engine_enabled,
                    "AUTONOMY_ENABLED": self.autonomy_enabled,
                    "HIP4_ENABLED": self.hip4_enabled,
                    "WORLD_MODEL_STREAMS_ENABLED": self.world_model_streams_enabled,
                },
                "SERVICE_ROLE=newswire owns only external news ingestion",
            )
        elif role == ServiceRole.WORLD_MODEL:
            self._reject_enabled(
                {
                    "NEWSWIRE_ENABLED": self.newswire_enabled,
                    "ENGINE_ENABLED": self.engine_enabled,
                    "AUTONOMY_ENABLED": self.autonomy_enabled,
                    "HIP4_ENABLED": self.hip4_enabled,
                    "DISCORD_BOT_ENABLED": self.discord_bot_enabled,
                    "DISCORD_PUBLISHER_ENABLED": self.discord_publisher_enabled,
                },
                "SERVICE_ROLE=world_model consumes persisted events and prediction streams only",
            )
        elif role == ServiceRole.TRADER:
            self._reject_enabled(
                {
                    "NEWSWIRE_ENABLED": self.newswire_enabled,
                    "DISCORD_BOT_ENABLED": self.discord_bot_enabled,
                    "DISCORD_PUBLISHER_ENABLED": self.discord_publisher_enabled,
                },
                "SERVICE_ROLE=trader must not own news providers or Discord sessions",
            )
        elif role == ServiceRole.DISCORD_PUBLISHER:
            self._require_enabled({"DISCORD_PUBLISHER_ENABLED": self.discord_publisher_enabled}, "SERVICE_ROLE=discord_publisher")
            missing = []
            if not self.discord_bot_token:
                missing.append("DISCORD_BOT_TOKEN")
            if not self.newswire_news_channel_configured:
                missing.append("NEWSWIRE_NEWS_CHANNEL_ID")
            if missing:
                raise ValueError(f"SERVICE_ROLE=discord_publisher missing required settings: {', '.join(missing)}")
            self._reject_enabled(
                {
                    "NEWSWIRE_ENABLED": self.newswire_enabled,
                    "ENGINE_ENABLED": self.engine_enabled,
                    "AUTONOMY_ENABLED": self.autonomy_enabled,
                },
                "SERVICE_ROLE=discord_publisher publishes only from persisted events",
            )
        elif role == ServiceRole.DISCORD_BOT:
            self._require_enabled({"DISCORD_BOT_ENABLED": self.discord_bot_enabled}, "SERVICE_ROLE=discord_bot")
            self._reject_enabled(
                {
                    "NEWSWIRE_ENABLED": self.newswire_enabled,
                    "ENGINE_ENABLED": self.engine_enabled,
                    "AUTONOMY_ENABLED": self.autonomy_enabled,
                },
                "SERVICE_ROLE=discord_bot must not own ingestion or trading loops",
            )
        elif role == ServiceRole.AGENT:
            self._reject_enabled(
                {
                    "NEWSWIRE_ENABLED": self.newswire_enabled,
                    "ENGINE_ENABLED": self.engine_enabled,
                    "AUTONOMY_ENABLED": self.autonomy_enabled,
                    "HIP4_ENABLED": self.hip4_enabled,
                },
                "SERVICE_ROLE=agent owns LLM command execution only",
            )
        elif role == ServiceRole.LIQUIDATIONS:
            self._require_enabled({"LIQUIDATIONS_ENABLED": self.liquidations_enabled}, "SERVICE_ROLE=liquidations")
            self._reject_enabled({"ENGINE_ENABLED": self.engine_enabled, "AUTONOMY_ENABLED": self.autonomy_enabled}, "SERVICE_ROLE=liquidations must not trade")
        return self

    def _reject_enabled(self, flags: dict[str, bool], context: str) -> None:
        enabled = [name for name, value in flags.items() if bool(value)]
        if enabled:
            raise ValueError(f"{context}; disable: {', '.join(enabled)}")

    def _require_enabled(self, flags: dict[str, bool], context: str) -> None:
        missing = [name for name, value in flags.items() if not bool(value)]
        if missing:
            raise ValueError(f"{context} requires: {', '.join(missing)}")

    @model_validator(mode="after")
    def shadow_full_alpha_catalog_requires_shadow_only(self) -> "Settings":
        if self.engine_alpha_catalog_mode == "shadow_full_catalog":
            execution_modes = [item.lower() for item in _csv(self.engine_execution_modes)]
            if (
                not self.engine_shadow_enabled
                or self.engine_paper_enabled
                or self.engine_live_enabled
                or execution_modes != ["shadow"]
            ):
                raise ValueError(
                    "ENGINE_ALPHA_CATALOG_MODE=shadow_full_catalog requires ENGINE_SHADOW_ENABLED=true, "
                    "ENGINE_PAPER_ENABLED=false, ENGINE_LIVE_ENABLED=false, and ENGINE_EXECUTION_MODES=shadow"
                )
        return self

    @property
    def hyperliquid_base_url(self) -> str:
        return self.hyperliquid_testnet_url if self.hyperliquid_network == "testnet" else self.hyperliquid_mainnet_url

    @property
    def hyperliquid_ws_url(self) -> str:
        return self.hyperliquid_testnet_ws_url if self.hyperliquid_network == "testnet" else self.hyperliquid_mainnet_ws_url

    @property
    def model_chain(self) -> list[str]:
        return _csv(self.agent_model_chain)

    @property
    def allowed_guild_ids(self) -> set[int]:
        return _csv_ints(self.discord_allowed_guild_ids)

    @property
    def allowed_channel_ids(self) -> set[int]:
        return _csv_ints(self.discord_allowed_channel_ids)

    @property
    def allowed_role_ids(self) -> set[int]:
        return _csv_ints(self.discord_allowed_role_ids)

    @property
    def admin_user_ids(self) -> set[int]:
        return _csv_ints(self.discord_admin_user_ids)

    @property
    def rss_feed_urls(self) -> list[str]:
        return _csv(self.news_rss_feeds)

    @property
    def account_allowlist(self) -> set[str]:
        return {address.lower() for address in _csv(self.account_address_allowlist)}

    @property
    def smart_money_addresses(self) -> list[str]:
        return [address.lower() for address in _csv(self.high_stakes_smart_money_addresses)]

    @property
    def hl_public_coin_list(self) -> list[str]:
        return [coin.upper() for coin in _csv(self.hl_public_coins)]

    @property
    def hl_watch_address_list(self) -> list[str]:
        addresses = set(_csv(self.liquidations_hl_watch_addresses))
        if self.hl_liquidator_vault_address:
            addresses.add(self.hl_liquidator_vault_address)
        addresses.update(self.smart_money_addresses)
        return sorted(address.lower() for address in addresses if address)

    @property
    def autonomy_core_symbols(self) -> list[str]:
        return [symbol.upper() for symbol in _csv(self.autonomy_core_universe)]

    @property
    def hip4_question_allowlist_ids(self) -> set[int]:
        ids: set[int] = set()
        for item in _csv(self.hip4_question_allowlist):
            try:
                ids.add(int(item))
            except ValueError:
                continue
        return ids

    @property
    def hip4_mode_allows_scan(self) -> bool:
        return self.hip4_mode in {"shadow", "paper_shadow"}

    @property
    def hip4_mode_allows_paper(self) -> bool:
        return self.hip4_mode == "paper_shadow"

    @property
    def hip4_mode_allows_manual_ticket(self) -> bool:
        return self.hip4_mode == "paper_shadow"

    @property
    def hip4_alert_channel_configured(self) -> bool:
        return bool(str(self.hip4_alert_channel_id).strip())

    def hip4_config_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.hip4_scan_enabled and not self.hip4_mode_allows_scan:
            warnings.append("HIP4_SCAN_ENABLED is true but HIP4_MODE does not allow scanning")
        if self.hip4_paper_execution_enabled and not self.hip4_mode_allows_paper:
            warnings.append("HIP4_PAPER_EXECUTION_ENABLED is true but HIP4_MODE does not allow paper execution")
        if self.hip4_manual_ticket_export_enabled and not self.hip4_mode_allows_manual_ticket:
            warnings.append("HIP4_MANUAL_TICKET_EXPORT_ENABLED is true but HIP4_MODE does not allow manual tickets")
        if self.hip4_proactive_loop_enabled:
            if not self.hip4_enabled:
                warnings.append("HIP4_PROACTIVE_LOOP_ENABLED is true but HIP4_ENABLED is false")
            if not self.hip4_scan_enabled:
                warnings.append("HIP4_PROACTIVE_LOOP_ENABLED is true but HIP4_SCAN_ENABLED is false")
            if not self.hip4_mode_allows_scan:
                warnings.append("HIP4_PROACTIVE_LOOP_ENABLED is true but HIP4_MODE does not allow scanning")
        if self.hip4_proactive_paper_execution_enabled:
            if not self.hip4_paper_execution_enabled:
                warnings.append("HIP4_PROACTIVE_PAPER_EXECUTION_ENABLED is true but HIP4_PAPER_EXECUTION_ENABLED is false")
            if not self.hip4_mode_allows_paper:
                warnings.append("HIP4_PROACTIVE_PAPER_EXECUTION_ENABLED is true but HIP4_MODE does not allow paper execution")
        return warnings

    @property
    def autonomy_hip3_dex_names(self) -> list[str]:
        return _csv(self.autonomy_hip3_dexs)

    @property
    def autonomy_index_aliases(self) -> dict[str, list[str]]:
        return _alias_map(self.autonomy_hip3_index_aliases)

    @property
    def autonomy_alert_channel_configured(self) -> bool:
        return bool(str(self.autonomy_alert_channel_id).strip())

    @property
    def autonomy_admin_users(self) -> set[int]:
        return self.admin_user_ids | _csv_ints(self.autonomy_admin_user_ids)

    @property
    def autonomy_admin_roles(self) -> set[int]:
        return _csv_ints(self.autonomy_admin_role_ids)

    @property
    def autonomy_eval_horizon_list(self) -> list[str]:
        return [item.lower() for item in _csv(self.autonomy_eval_horizons)]

    @property
    def autonomy_event_eval_horizon_list(self) -> list[str]:
        return [item.lower() for item in _csv(self.autonomy_event_eval_horizons)]

    @property
    def autonomy_event_eval_macro_proxy_symbols(self) -> list[str]:
        return [symbol.upper() for symbol in _csv(self.autonomy_event_eval_macro_proxies)]

    @property
    def autonomy_memory_prompt_role_list(self) -> list[str]:
        return [_canonical_role(role) for role in _csv(self.autonomy_memory_prompt_roles)]

    @property
    def autonomy_weekly_report_day_normalized(self) -> str:
        return self.autonomy_weekly_report_day.strip().upper()

    @property
    def engine_execution_mode_list(self) -> list[str]:
        modes = [item.lower() for item in _csv(self.engine_execution_modes)]
        return [mode for mode in modes if mode in {"paper", "shadow"}]

    @property
    def engine_news_macro_proxy_symbol_list(self) -> list[str]:
        configured = [symbol.upper() for symbol in _csv(self.engine_news_macro_proxy_symbols)]
        return configured or self.autonomy_core_symbols

    @property
    def engine_cross_venue_dex_list(self) -> list[str]:
        return [dex.lower() for dex in _csv(self.engine_cross_venue_dexes)]

    @property
    def autonomy_evaluation_effective_enabled(self) -> bool:
        return self.autonomy_enabled and self.autonomy_evaluation_enabled

    @property
    def autonomy_event_evaluation_effective_enabled(self) -> bool:
        return self.autonomy_enabled and self.autonomy_event_evaluation_enabled

    @property
    def autonomy_memory_effective_enabled(self) -> bool:
        return self.autonomy_enabled and self.autonomy_memory_enabled

    @property
    def autonomy_reports_effective_enabled(self) -> bool:
        return self.autonomy_enabled and self.autonomy_reports_enabled

    @property
    def autonomy_tuning_proposals_effective_enabled(self) -> bool:
        return self.autonomy_enabled and self.autonomy_tuning_proposals_enabled

    @property
    def newswire_query_terms(self) -> list[str]:
        return _csv(self.newswire_queries)

    @property
    def x_watchlist_users(self) -> list[str]:
        return _csv(self.x_watchlist_user_ids)

    @property
    def newswire_rss_feed_urls(self) -> list[str]:
        return _csv(self.newswire_rss_feeds)

    @property
    def newswire_cashtag_list(self) -> list[str]:
        return [term.upper().lstrip("$") for term in _csv(self.x_cashtags)]

    @property
    def alpaca_news_symbol_list(self) -> list[str]:
        return _csv(self.alpaca_news_symbols)

    @property
    def newswire_news_channel_configured(self) -> bool:
        return bool(str(self.newswire_news_channel_id).strip())

    @property
    def newswire_symbols_universe(self) -> list[str]:
        """Symbols the normalizer scans for in free-text (core + cashtags + short queries)."""
        universe = set(self.autonomy_core_symbols) | set(self.newswire_cashtag_list)
        for term in self.newswire_query_terms:
            token = term.strip().upper()
            if token.isalpha() and 2 <= len(token) <= 6:
                universe.add(token)
        return sorted(universe)

    @property
    def autonomy_equity_symbols(self) -> list[str]:
        return [s.upper() for s in _csv(self.autonomy_equity_universe)]

    @property
    def tradfi_provider_names(self) -> list[str]:
        return [provider.lower() for provider in _csv(self.tradfi_provider_order)]

    @property
    def autonomy_equity_effective_enabled(self) -> bool:
        return self.tradfi_enabled and self.autonomy_equity_enabled

    @property
    def options_flow_effective_enabled(self) -> bool:
        return self.tradfi_enabled and self.options_flow_enabled

    def tradfi_config_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.tradfi_enabled:
            provider_names = set(self.tradfi_provider_names)
            alpha_configured = self.alpha_vantage_enabled and bool(self.alpha_vantage_api_key)
            alpaca_configured = bool(self.alpaca_api_key and self.alpaca_api_secret)
            if "alpha_vantage" in provider_names and self.alpha_vantage_enabled and not self.alpha_vantage_api_key:
                warnings.append("ALPHA_VANTAGE_ENABLED requires ALPHA_VANTAGE_API_KEY")
            if "alpaca" in provider_names and not alpaca_configured and not alpha_configured:
                warnings.append("TRADFI_ENABLED requires ALPACA_API_KEY and ALPACA_API_SECRET or a configured Alpha Vantage provider")
            if "alpha_vantage" in provider_names and not self.alpha_vantage_enabled and not alpaca_configured:
                warnings.append("TRADFI_PROVIDER_ORDER includes alpha_vantage but ALPHA_VANTAGE_ENABLED is false")
            if not provider_names:
                warnings.append("TRADFI_PROVIDER_ORDER is empty")
        if self.autonomy_equity_enabled and not self.tradfi_enabled:
            warnings.append("AUTONOMY_EQUITY_ENABLED requires TRADFI_ENABLED=true")
        if self.autonomy_equity_enabled and not self.autonomy_equity_symbols:
            warnings.append("AUTONOMY_EQUITY_UNIVERSE is empty")
        if self.options_flow_enabled and not self.tradfi_enabled:
            warnings.append("OPTIONS_FLOW_ENABLED requires TRADFI_ENABLED=true")
        if self.autonomy_equity_paper_max_gross_leverage > 3.0:
            warnings.append("AUTONOMY_EQUITY_PAPER_MAX_GROSS_LEVERAGE should not exceed 3.0 for equities")
        if self.autonomy_equity_paper_max_single_name_exposure_pct > 25.0:
            warnings.append("AUTONOMY_EQUITY_PAPER_MAX_SINGLE_NAME_EXPOSURE_PCT should not exceed 25%")
        return warnings

    def newswire_config_warnings(self) -> list[str]:
        warnings: list[str] = []
        discord_requested = self.newswire_enabled and self.newswire_discord_enabled and (self.discord_bot_token or self.newswire_news_channel_configured)
        if discord_requested and not self.newswire_news_channel_configured:
            warnings.append("NEWSWIRE_NEWS_CHANNEL_ID is required to post the news feed to #news")
        if discord_requested and not self.discord_bot_token:
            warnings.append("DISCORD_BOT_TOKEN is required for Newswire Discord #news publishing")
        if self.alpaca_news_enabled and not (self.alpaca_api_key and self.alpaca_api_secret):
            warnings.append("ALPACA_NEWS_ENABLED requires ALPACA_API_KEY and ALPACA_API_SECRET")
        if self.trading_economics_enabled and not self.trading_economics_api_key:
            warnings.append("TRADING_ECONOMICS_ENABLED requires TRADING_ECONOMICS_API_KEY (guest:guest allowed)")
        if self.x_newswire_enabled and not self.x_bearer_token:
            warnings.append("X_NEWSWIRE_ENABLED requires X_BEARER_TOKEN")
        if self.newswire_breaking_min_importance < self.newswire_news_min_importance:
            warnings.append("NEWSWIRE_BREAKING_MIN_IMPORTANCE should be >= NEWSWIRE_NEWS_MIN_IMPORTANCE")
        if self.engine_news_min_importance < 0 or self.engine_news_min_importance > 100:
            warnings.append("ENGINE_NEWS_MIN_IMPORTANCE must be between 0 and 100")
        if self.engine_news_min_source_score < 0 or self.engine_news_min_source_score > 1:
            warnings.append("ENGINE_NEWS_MIN_SOURCE_SCORE must be between 0 and 1")
        if self.engine_news_catalyst_threshold < 0 or self.engine_news_catalyst_threshold > 1:
            warnings.append("ENGINE_NEWS_CATALYST_THRESHOLD must be between 0 and 1")
        if self.engine_news_catalyst_ttl_seconds <= 0:
            warnings.append("ENGINE_NEWS_CATALYST_TTL_SECONDS must be positive")
        if self.engine_news_macro_min_importance < 0 or self.engine_news_macro_min_importance > 100:
            warnings.append("ENGINE_NEWS_MACRO_MIN_IMPORTANCE must be between 0 and 100")
        return warnings

    def autonomy_config_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.autonomy_enabled and not self.autonomy_alert_channel_configured:
            warnings.append("AUTONOMY_ALERT_CHANNEL_ID is required to post signals to #ai-bot-alerts")
        if self.autonomy_enabled and not self.autonomy_require_human_signoff:
            warnings.append("AUTONOMY_REQUIRE_HUMAN_SIGNOFF=false is unsafe for V1 paper-signoff mode")
        if self.autonomy_max_hot_l2_assets > self.autonomy_max_tracked_assets:
            warnings.append("AUTONOMY_MAX_HOT_L2_ASSETS exceeds AUTONOMY_MAX_TRACKED_ASSETS")
        if not self.autonomy_core_symbols:
            warnings.append("AUTONOMY_CORE_UNIVERSE is empty")
        if self.autonomy_evaluation_enabled:
            invalid_horizons = [item for item in self.autonomy_eval_horizon_list if item not in AUTONOMY_ALLOWED_EVAL_HORIZONS]
            if not self.autonomy_eval_horizon_list:
                warnings.append("AUTONOMY_EVAL_HORIZONS is empty")
            if invalid_horizons:
                warnings.append(f"AUTONOMY_EVAL_HORIZONS contains unsupported horizons: {','.join(invalid_horizons)}")
            if self.autonomy_eval_max_open_signals <= 0:
                warnings.append("AUTONOMY_EVAL_MAX_OPEN_SIGNALS must be positive")
        if self.autonomy_event_evaluation_enabled:
            invalid_event_horizons = [item for item in self.autonomy_event_eval_horizon_list if item not in AUTONOMY_ALLOWED_EVAL_HORIZONS or item == "expiry"]
            if not self.autonomy_event_eval_horizon_list:
                warnings.append("AUTONOMY_EVENT_EVAL_HORIZONS is empty")
            if invalid_event_horizons:
                warnings.append(f"AUTONOMY_EVENT_EVAL_HORIZONS contains unsupported horizons: {','.join(invalid_event_horizons)}")
            if self.autonomy_event_eval_max_open_events <= 0:
                warnings.append("AUTONOMY_EVENT_EVAL_MAX_OPEN_EVENTS must be positive")
            if self.autonomy_event_eval_symbols_per_event <= 0:
                warnings.append("AUTONOMY_EVENT_EVAL_SYMBOLS_PER_EVENT must be positive")
            if not self.autonomy_event_eval_macro_proxy_symbols:
                warnings.append("AUTONOMY_EVENT_EVAL_MACRO_PROXIES is empty")
            if self.autonomy_event_eval_min_source_score < 0 or self.autonomy_event_eval_min_source_score > 1:
                warnings.append("AUTONOMY_EVENT_EVAL_MIN_SOURCE_SCORE must be between 0 and 1")
        if self.autonomy_reports_enabled:
            if self.autonomy_daily_report_enabled and not _valid_hhmm(self.autonomy_daily_report_utc):
                warnings.append("AUTONOMY_DAILY_REPORT_UTC must be HH:MM")
            if self.autonomy_weekly_report_enabled:
                if self.autonomy_weekly_report_day_normalized not in AUTONOMY_WEEKDAYS:
                    warnings.append("AUTONOMY_WEEKLY_REPORT_DAY must be one of MON,TUE,WED,THU,FRI,SAT,SUN")
                if not _valid_hhmm(self.autonomy_weekly_report_utc):
                    warnings.append("AUTONOMY_WEEKLY_REPORT_UTC must be HH:MM")
        if self.autonomy_memory_enabled:
            ttl_values = {
                "AUTONOMY_MEMORY_CANDIDATE_TTL_DAYS": self.autonomy_memory_candidate_ttl_days,
                "AUTONOMY_MEMORY_SHADOW_TTL_DAYS": self.autonomy_memory_shadow_ttl_days,
                "AUTONOMY_MEMORY_ROLE_TTL_DAYS": self.autonomy_memory_role_ttl_days,
                "AUTONOMY_MEMORY_PROCESS_TTL_DAYS": self.autonomy_memory_process_ttl_days,
                "AUTONOMY_MEMORY_INCIDENT_TTL_DAYS": self.autonomy_memory_incident_ttl_days,
                "AUTONOMY_TUNING_PROPOSAL_TTL_DAYS": self.autonomy_tuning_proposal_ttl_days,
            }
            for name, value in ttl_values.items():
                if value <= 0:
                    warnings.append(f"{name} must be positive")
            if self.autonomy_lesson_min_confidence < 0 or self.autonomy_lesson_min_confidence > 1:
                warnings.append("AUTONOMY_LESSON_MIN_CONFIDENCE must be between 0 and 1")
            if self.autonomy_strategy_lesson_min_confidence < 0 or self.autonomy_strategy_lesson_min_confidence > 1:
                warnings.append("AUTONOMY_STRATEGY_LESSON_MIN_CONFIDENCE must be between 0 and 1")
            invalid_memory_roles = [role for role in self.autonomy_memory_prompt_role_list if role not in ROLE_ORDER]
            if invalid_memory_roles:
                warnings.append(f"AUTONOMY_MEMORY_PROMPT_ROLES contains unsupported roles: {','.join(invalid_memory_roles)}")
        return warnings

    def role_model_chain(self, role: str) -> list[str]:
        role_key = _canonical_role(role)
        configured = {
            "analyst": self.debate_analyst_model_chain,
            "quant": self.debate_quant_model_chain,
            "research": self.debate_research_model_chain,
            "adversary": self.debate_adversary_model_chain,
            "risk": self.debate_risk_model_chain,
            "treasury": self.debate_treasury_model_chain,
            "execution": self.debate_execution_model_chain,
            "judge": self.debate_judge_model_chain,
        }.get(role_key, "")
        default = DEFAULT_DEBATE_ROLE_MODEL_CHAINS.get(role_key, "")
        return _csv(configured) or _csv(default) or self.model_chain

    @property
    def debate_role_names(self) -> list[str]:
        return list(ROLE_ORDER)

    @property
    def debate_role_primary_models(self) -> dict[str, str | None]:
        return {role: (self.role_model_chain(role)[0] if self.role_model_chain(role) else None) for role in self.debate_role_names}

    def debate_model_contract(self) -> dict[str, object]:
        primary = self.debate_role_primary_models
        # A conflict is two *interacting* roles sharing a primary model — i.e. one model
        # reviewing its own homework. Independent parallel reviewers may share the team
        # primary (Nex), so a plain duplicate across non-interacting roles is allowed.
        conflicts = [
            f"{a}/{b}:{primary[a]}"
            for a, b in DEBATE_INTERACTION_EDGES
            if primary.get(a) and primary.get(a) == primary.get(b)
        ]
        shared_primary = _duplicate_primary_models(primary)
        warnings: list[str] = []
        if conflicts:
            warnings.append("interacting roles share a primary model (self-review)")
        ok = not warnings
        status = "ok" if ok else "violation" if self.debate_model_diversity_policy == "strict" else "warning"
        return {
            "policy": self.debate_model_diversity_policy,
            "status": status,
            "primary_by_role": primary,
            "interaction_conflicts": conflicts,
            "shared_primary_models": shared_primary,
            "judge_primary_model": primary.get("judge"),
            "warnings": warnings,
            "production_guidance": "Keep the decision spine (analyst/adversary/judge) on three distinct frontier models and quant/risk on distinct primaries. The judge should be the strongest available model (Claude Opus 4.8); openrouter/fusion is a viable ensemble-judge alternative.",
        }


def _canonical_role(role: str) -> str:
    role_key = role.lower().strip().replace("-", "_")
    return {
        "proposer": "analyst",
        "red_team": "adversary",
        "risk_manager": "risk",
        "execution_strategist": "execution",
    }.get(role_key, role_key)


def _duplicate_primary_models(primary: dict[str, str | None]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for role, model in primary.items():
        if model:
            grouped.setdefault(model, []).append(role)
    return {model: roles for model, roles in grouped.items() if len(roles) > 1}


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _csv_ints(value: str) -> set[int]:
    result: set[int] = set()
    for part in _csv(value):
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def _valid_hhmm(value: str) -> bool:
    hour, sep, minute = value.strip().partition(":")
    if sep != ":" or not hour.isdigit() or not minute.isdigit():
        return False
    return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59


def _alias_map(value: str) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for item in _csv(value):
        name, sep, raw_aliases = item.partition(":")
        canonical = name.strip().upper()
        if not canonical:
            continue
        parts = [canonical]
        if sep:
            parts.extend(part.strip().upper() for part in raw_aliases.split("|") if part.strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for part in parts:
            if part and part not in seen:
                seen.add(part)
                deduped.append(part)
        aliases[canonical] = deduped
    return aliases


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    load_vault_environment()
    return Settings()
