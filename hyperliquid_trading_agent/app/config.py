from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL_CHAIN = "openrouter:openai/gpt-oss-120b:free,openrouter:openai/gpt-oss-20b:free,openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:nvidia/nemotron-3-nano-30b-a3b:free"
ROLE_ORDER = ["analyst", "quant", "research", "risk", "treasury", "execution", "adversary", "judge"]
DEFAULT_DEBATE_ROLE_MODEL_CHAINS = {
    # Development/free defaults intentionally use different primary models so
    # adversarial review is not one model grading its own work. In production,
    # set DEBATE_JUDGE_MODEL_CHAIN to the strongest available frontier/main model.
    "analyst": "openrouter:qwen/qwen3-next-80b-a3b-instruct:free,openrouter:openai/gpt-oss-120b:free,openrouter:nex-agi/nex-n2-pro:free",
    "quant": "openrouter:nvidia/nemotron-3-nano-30b-a3b:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free",
    "research": "openrouter:google/gemma-4-26b-a4b-it:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free",
    "risk": "openrouter:openai/gpt-oss-20b:free,openrouter:nvidia/nemotron-3-nano-30b-a3b:free,openrouter:nex-agi/nex-n2-pro:free",
    "treasury": "openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free",
    "execution": "openrouter:nex-agi/nex-n2-pro:free,openrouter:liquid/lfm-2.5-1.2b-instruct:free,openrouter:openai/gpt-oss-20b:free",
    "adversary": "openrouter:meta-llama/llama-3.3-70b-instruct:free,openrouter:openai/gpt-oss-120b:free,openrouter:nex-agi/nex-n2-pro:free",
    "judge": "openrouter:openai/gpt-oss-120b:free,openrouter:openai/gpt-oss-20b:free,openrouter:nex-agi/nex-n2-pro:free",
}
DEFAULT_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
    ",https://cointelegraph.com/rss"
    ",https://www.federalreserve.gov/feeds/press_all.xml"
)


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

    database_url: str = "postgresql+asyncpg://hlagent:hlagent@postgres:5432/hlagent"

    discord_bot_token: str = Field(default="", validation_alias="DISCORD_BOT_TOKEN")
    discord_allowed_guild_ids: str = ""
    discord_allowed_channel_ids: str = ""
    discord_allowed_role_ids: str = ""
    discord_admin_user_ids: str = ""
    discord_max_response_chars: int = 1900

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
    hyperliquid_network: Literal["mainnet", "testnet"] = "mainnet"
    hyperliquid_ws_enabled: bool = False
    hyperliquid_exchange_enabled: bool = False

    position_tracking_enabled: bool = True
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
    newswire_enabled: bool = True
    newswire_queries: str = "BTC,ETH,HYPE,Hyperliquid,Fed,CPI,FOMC,crypto liquidation"
    x_watchlist_user_ids: str = ""
    x_min_public_metric_score: int = 0

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

    @field_validator("hyperliquid_exchange_enabled")
    @classmethod
    def mainnet_exchange_must_remain_disabled(cls, value: bool) -> bool:
        # MVP guardrail: no signed exchange actions are exposed by this service.
        if value:
            raise ValueError("HYPERLIQUID_EXCHANGE_ENABLED must remain false for the MVP")
        return value

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
    def autonomy_core_symbols(self) -> list[str]:
        return [symbol.upper() for symbol in _csv(self.autonomy_core_universe)]

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
    def newswire_query_terms(self) -> list[str]:
        return _csv(self.newswire_queries)

    @property
    def x_watchlist_users(self) -> list[str]:
        return _csv(self.x_watchlist_user_ids)

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
        duplicates = _duplicate_primary_models(primary)
        judge_model = primary.get("judge")
        reviewer_roles = [role for role in self.debate_role_names if role != "judge"]
        judge_overlap = [role for role in reviewer_roles if primary.get(role) == judge_model and judge_model]
        homework_pairs = []
        if primary.get("analyst") and primary.get("adversary") == primary.get("analyst"):
            homework_pairs.append("analyst/adversary")
        if primary.get("quant") and primary.get("risk") == primary.get("quant"):
            homework_pairs.append("quant/risk")
        warnings: list[str] = []
        if duplicates:
            warnings.append("duplicate primary models across roles")
        if judge_overlap:
            warnings.append("judge primary model overlaps with reviewer roles")
        if homework_pairs:
            warnings.append("adversarial reviewer primary overlaps with reviewed role")
        ok = not warnings
        status = "ok" if ok else "violation" if self.debate_model_diversity_policy == "strict" else "warning"
        return {
            "policy": self.debate_model_diversity_policy,
            "status": status,
            "primary_by_role": primary,
            "duplicate_primary_models": duplicates,
            "judge_primary_model": judge_model,
            "judge_primary_overlaps_roles": judge_overlap,
            "homework_overlap_pairs": homework_pairs,
            "warnings": warnings,
            "production_guidance": "Use a distinct strongest/frontier model for DEBATE_JUDGE_MODEL_CHAIN; keep analyst/quant/risk/adversary on different primary model families.",
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
    return Settings()
