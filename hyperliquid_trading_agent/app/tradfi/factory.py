"""Runtime construction helpers for TradFi providers."""

from __future__ import annotations

from alpaca.data.enums import DataFeed

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.tradfi.alpaca_provider import AlpacaTradFiProvider
from hyperliquid_trading_agent.app.tradfi.alpha_vantage_provider import AlphaVantageTradFiProvider
from hyperliquid_trading_agent.app.tradfi.base import TradFiProvider
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.composite_provider import CompositeTradFiProvider

log = get_logger(__name__)


async def build_tradfi_client(settings: Settings) -> TradFiClient | None:
    """Build and start the configured TradFi client stack."""

    if not settings.tradfi_enabled:
        return None
    providers: list[TradFiProvider] = []
    for provider_name in settings.tradfi_provider_names:
        candidate: TradFiProvider | None
        if provider_name in {"alpha_vantage", "alphavantage", "av"}:
            candidate = _build_alpha_vantage_provider(settings)
        elif provider_name == "alpaca":
            candidate = _build_alpaca_provider(settings)
        else:
            log.warning("tradfi_unknown_provider_ignored", provider=provider_name)
            candidate = None
        if candidate is not None:
            providers.append(candidate)
    if not providers:
        log.warning("tradfi_client_not_started_no_configured_providers")
        return None
    selected_provider: TradFiProvider = providers[0] if len(providers) == 1 else CompositeTradFiProvider(providers)
    client = TradFiClient(selected_provider)
    await client.start()
    log.info("tradfi_client_started", provider=selected_provider.name, providers=[item.name for item in providers])
    return client


def _build_alpha_vantage_provider(settings: Settings) -> AlphaVantageTradFiProvider | None:
    if not settings.alpha_vantage_enabled:
        return None
    if not settings.alpha_vantage_api_key:
        log.warning("alpha_vantage_disabled_missing_api_key")
        return None
    return AlphaVantageTradFiProvider(
        api_key=settings.alpha_vantage_api_key,
        base_url=settings.alpha_vantage_base_url,
        mcp_url=settings.alpha_vantage_mcp_url,
        mcp_auth_header=settings.alpha_vantage_mcp_auth_header,
        mcp_auth_scheme=settings.alpha_vantage_mcp_auth_scheme,
        transport=settings.alpha_vantage_transport,
        timeout_seconds=settings.alpha_vantage_timeout_seconds,
    )


def _build_alpaca_provider(settings: Settings) -> AlpacaTradFiProvider | None:
    if not (settings.alpaca_api_key and settings.alpaca_api_secret):
        log.warning("alpaca_tradfi_disabled_missing_keys")
        return None
    return AlpacaTradFiProvider(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        feed=DataFeed(settings.alpaca_data_feed),
    )
