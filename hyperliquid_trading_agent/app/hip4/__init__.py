"""HIP-4 outcome-market subsystem.

This bounded package is intentionally read-only/paper/shadow only. It must not
instantiate signing clients, create private-key settings, post to Hyperliquid's
mutation API, or route execution through autonomy/the institutional engine.
"""

from hyperliquid_trading_agent.app.hip4.service import Hip4Service

__all__ = ["Hip4Service"]
