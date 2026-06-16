"""Institutional trading engine package.

The engine is a paper/shadow-only, audit-first replacement path for the legacy
TradeSignal loop.  Step 1 exposes schemas only; runtime services are added in
subsequent milestones.
"""

from hyperliquid_trading_agent.app.engine.schemas import (
    AllocationDecision,
    AlphaCandidate,
    CandidateBookSnapshot,
    DebateDecision,
    EVEstimate,
    EvidencePack,
    ExecutionReport,
    FeatureRollup,
    FeatureSchemaVersion,
    FeatureSnapshot,
    FeatureValue,
    KillSwitchEvent,
    ModelTrainingRun,
    ModelVersion,
    NormalizedEvent,
    OrderIntent,
    PnLAttributionRecord,
    PositionThesis,
    ReconciliationRun,
    RegimeVector,
    ReplayResult,
    RetentionRun,
    StrategyPermissions,
)

__all__ = [
    "AllocationDecision",
    "AlphaCandidate",
    "CandidateBookSnapshot",
    "DebateDecision",
    "EVEstimate",
    "EvidencePack",
    "ExecutionReport",
    "FeatureRollup",
    "FeatureSchemaVersion",
    "FeatureSnapshot",
    "FeatureValue",
    "KillSwitchEvent",
    "ModelTrainingRun",
    "ModelVersion",
    "NormalizedEvent",
    "OrderIntent",
    "PnLAttributionRecord",
    "PositionThesis",
    "ReconciliationRun",
    "RegimeVector",
    "ReplayResult",
    "RetentionRun",
    "StrategyPermissions",
]
