"""Vendor-agnostic TradFi data layer for equities and options.

Provides:
- ``TradFiProvider`` ABC + ``AlpacaTradFiProvider`` implementation
- ``TradFiClient`` facade with TTL cache and rate guard
- ``OptionsFlowDetector`` / ``FlowEnricher`` for unusual options activity
- ``schemas`` with Pydantic models for all data types
"""

from hyperliquid_trading_agent.app.tradfi.base import TradFiProvider
from hyperliquid_trading_agent.app.tradfi.client import TradFiClient
from hyperliquid_trading_agent.app.tradfi.options_flow import FlowEnricher, OptionsFlowDetector

__all__ = [
    "FlowEnricher",
    "OptionsFlowDetector",
    "TradFiClient",
    "TradFiProvider",
]
