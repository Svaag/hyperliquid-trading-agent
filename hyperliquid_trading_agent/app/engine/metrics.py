from __future__ import annotations

from prometheus_client import Counter, Gauge

ENGINE_EVENTS = Counter("engine_events_total", "Normalized engine events", ["event_type", "asset_class"])
ENGINE_FEATURES = Counter("engine_features_total", "Engine features computed", ["feature_group", "feature_name"])
ENGINE_CANDIDATES = Counter("engine_candidates_total", "Alpha candidates generated", ["strategy_id", "status"])
ENGINE_EV_ESTIMATES = Counter("engine_ev_estimates_total", "EV estimates generated", ["model_version_id"])
ENGINE_ALLOCATIONS = Counter("engine_allocations_total", "Portfolio allocation decisions", ["status"])
ENGINE_EXECUTION_REPORTS = Counter("engine_execution_reports_total", "Paper/shadow execution reports", ["execution_mode", "status"])
ENGINE_OPEN_POSITION_THESES = Gauge("engine_open_position_theses", "Open engine position theses")
