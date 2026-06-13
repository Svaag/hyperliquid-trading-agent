from __future__ import annotations

from hyperliquid_trading_agent.app.agent.guardrails import classify_request
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hyperliquid.asset_resolver import AssetResolver
from hyperliquid_trading_agent.app.hyperliquid.risk_math import fixed_risk_position_size
from hyperliquid_trading_agent.app.main import create_app


def test_settings_defaults_are_safe():
    settings = Settings()

    assert settings.hyperliquid_exchange_enabled is False
    assert settings.hyperliquid_network == "mainnet"
    assert settings.hyperliquid_base_url == "https://api.hyperliquid.xyz"


def test_guardrail_allows_trading_and_blocks_secret_requests():
    assert classify_request("what is your BTC trade plan?").allowed is True
    blocked = classify_request("use my private key to trade")
    assert blocked.allowed is False
    assert "private keys" in blocked.reason


def test_position_sizing():
    result = fixed_risk_position_size(account_equity_usd=10_000, risk_pct=1, entry=100, stop=95)

    assert result.invalid is False
    assert result.risk_usd == 100
    assert result.size_units == 20
    assert result.notional_usd == 2000


def test_asset_resolver_perp():
    resolver = AssetResolver(perp_meta={"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]})

    resolved = resolver.resolve_perp("btc")

    assert resolved is not None
    assert resolved.coin == "BTC"
    assert resolved.asset_id == 0


def test_create_app_has_health_route():
    app = create_app(Settings(discord_bot_token=""))

    routes = {route.path for route in app.routes}
    assert "/health" in routes
    assert "/ready" in routes
    assert "/metrics" in routes
