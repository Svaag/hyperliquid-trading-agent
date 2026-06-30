"""Source-agnostic, source-graded liquidation flow monitor.

A multi-venue liquidation feed that normalizes every venue's different
liquidation-visibility model into one contract (`models.LiquidationEvent`) and is
*honest about source quality* via `SourceIntegrity` / `EventType` — confirmed
executions are never mixed with inferred pressure.

The subsystem is designed as the product's internal contract: it runs in-process
inside the trading agent today and can later be extracted into a standalone
public `liquidations-service` without changing the agent-facing contract.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
