"""Vendor-agnostic TradFi data layer for equities and options.

Provides:
- ``TradFiProvider`` ABC + Alpaca/Alpha Vantage provider implementations
- ``TradFiClient`` facade with TTL cache and rate guard
- ``CompositeTradFiProvider`` for ordered vendor fallback
- ``OptionsFlowDetector`` / ``FlowEnricher`` for unusual options activity
- ``schemas`` with Pydantic models for all data types
"""

from hyperliquid_trading_agent.app.tradfi.alpha_vantage_provider import AlphaVantageTradFiProvider
from hyperliquid_trading_agent.app.tradfi.base import TradFiProvider
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.composite_provider import CompositeTradFiProvider
from hyperliquid_trading_agent.app.tradfi.options_flow import FlowEnricher, OptionsFlowDetector

__all__ = [
    "AlphaVantageTradFiProvider",
    "CompositeTradFiProvider",
    "FlowEnricher",
    "OptionsFlowDetector",
    "TradFiClient",
    "TradFiProvider",
]
