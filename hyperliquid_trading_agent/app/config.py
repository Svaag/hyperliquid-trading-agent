from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL_CHAIN = "openrouter:anthropic/claude-sonnet-4.6,openrouter:deepseek/deepseek-v4-pro"
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
    discord_max_response_chars: int = 1800

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

    high_stakes_debate_enabled: bool = False
    high_stakes_activation_policy: Literal["risk_routed", "explicit_only", "all_trading_questions"] = "risk_routed"
    high_stakes_prompt_style: Literal["standard", "aggressive"] = "standard"
    high_stakes_info_provider: Literal["sdk_preferred", "rest_only", "sdk_only"] = "sdk_preferred"
    high_stakes_max_rounds: int = 3
    high_stakes_timeout_seconds: int = 90
    high_stakes_max_coins: int = 3
    high_stakes_max_data_escalations: int = 1
    high_stakes_require_account_for_autonomous: bool = False
    account_address_allowlist: str = ""
    high_stakes_smart_money_addresses: str = ""
    agent_api_bearer_token: str = ""

    debate_analyst_model_chain: str = ""
    debate_quant_model_chain: str = ""
    debate_research_model_chain: str = ""
    debate_adversary_model_chain: str = ""
    debate_risk_model_chain: str = ""
    debate_treasury_model_chain: str = ""
    debate_execution_model_chain: str = ""
    debate_judge_model_chain: str = ""

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

    def role_model_chain(self, role: str) -> list[str]:
        role_key = role.lower().strip().replace("-", "_")
        configured = {
            "analyst": self.debate_analyst_model_chain,
            "proposer": self.debate_analyst_model_chain,
            "quant": self.debate_quant_model_chain,
            "research": self.debate_research_model_chain,
            "adversary": self.debate_adversary_model_chain,
            "red_team": self.debate_adversary_model_chain,
            "risk": self.debate_risk_model_chain,
            "risk_manager": self.debate_risk_model_chain,
            "treasury": self.debate_treasury_model_chain,
            "execution": self.debate_execution_model_chain,
            "execution_strategist": self.debate_execution_model_chain,
            "judge": self.debate_judge_model_chain,
        }.get(role_key, "")
        return _csv(configured) or self.model_chain

    @property
    def debate_role_names(self) -> list[str]:
        return ["analyst", "quant", "research", "adversary", "risk", "treasury", "execution", "judge"]


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


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()
