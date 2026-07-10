from __future__ import annotations

import time
from typing import Any

import anyio

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.evidence_loop import EngineEvidenceRefreshLoopService


class FakeEvidenceRepo:
    enabled = True

    def __init__(self, now_ms: int):
        self.now_ms = now_ms
        self.performance_rows: list[dict[str, Any]] = []
        self.replay_artifacts: list[dict[str, Any]] = []
        self.replay_links: list[dict[str, Any]] = []
        self.outcomes = [
            {
                "attribution_id": "coa_1",
                "candidate_id": "cand_1",
                "allocation_id": "alloc_1",
                "allocation_status": "allocate",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_version": "2.0.0",
                "strategy_family": "microstructure_orderflow",
                "asset": "BTC",
                "venue": "hyperliquid",
                "candidate_horizon": "5m",
                "regime_snapshot_id": "reg_1",
                "outcome_window": "5m",
                "window_end_ms": now_ms - 1_000,
                "created_at_ms": now_ms - 1_000,
                "net_return_bps": 12.0,
                "realized_r": 0.4,
                "risk_decision": "allow",
                "council_decision": "allow_shadow",
                "terminal_state": "matured",
                "metadata": {"regime_label": "orderflow=buy_pressure"},
            }
        ]

    async def list_candidate_outcome_attributions(self, **kwargs):
        return list(self.outcomes)

    async def list_portfolio_concentration_events(self, **kwargs):
        return []

    async def upsert_strategy_regime_performance(self, row):
        self.performance_rows.append(row)
        return row.get("performance_id")

    async def list_alpha_candidates(self, **kwargs):
        return [{"candidate_id": "cand_1", "strategy_id": "microstructure_ofi_v2", "asset": "BTC", "created_at_ms": self.now_ms - 1_000}]

    async def list_ev_estimates(self, **kwargs):
        return [{"candidate_id": "cand_1", "net_ev_bps": 10, "risk_adjusted_utility": 0.5, "created_at_ms": self.now_ms - 1_000}]

    async def list_allocation_decisions(self, **kwargs):
        return [{"candidate_id": "cand_1", "status": "allocate", "allocated_notional_usd": 1000, "created_at_ms": self.now_ms - 1_000}]

    async def list_execution_reports(self, **kwargs):
        return [{"execution_mode": "shadow", "slippage_bps": 0, "fees_usd": 0, "created_at_ms": self.now_ms - 1_000}]

    async def list_risk_gateway_decisions(self, **kwargs):
        return []

    async def list_pnl_attribution(self, **kwargs):
        return []

    async def record_replay_result(self, artifact):
        self.replay_artifacts.append(artifact)
        return artifact["replay_id"]

    async def record_replay_result_link(self, link):
        self.replay_links.append(link)
        return link["link_id"]


def _settings(**overrides) -> Settings:
    defaults = dict(
        environment="test",
        engine_enabled=True,
        engine_replay_min_sample_candidates=1,
        engine_replay_min_shadow_intents=1,
        engine_readiness_max_strategy_allocation_share_pct=100,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_evidence_loop_runs_due_refreshers_and_respects_intervals():
    now_ms = int(time.time() * 1000)
    repo = FakeEvidenceRepo(now_ms)
    service = EngineEvidenceRefreshLoopService(settings=_settings(), repository=repo)

    async def run():
        first = await service.run_due(now_ms)
        immediately_after = await service.run_due(now_ms + 60_000)
        after_strategy_interval = await service.run_due(now_ms + 3_700_000)
        return first, immediately_after, after_strategy_interval

    first, immediately_after, after_strategy_interval = anyio.run(run)

    assert first == {"strategy_refreshed": True, "strategy_rows": 1, "replay_ran": True, "replay_status": "advisory_pass"}
    assert repo.performance_rows and repo.performance_rows[0]["strategy_id"] == "microstructure_ofi_v2"
    assert repo.replay_artifacts and repo.replay_artifacts[0]["metadata"]["verdict"] == "baseline_equivalence"
    assert immediately_after == {"strategy_refreshed": False, "replay_ran": False}
    assert after_strategy_interval["strategy_refreshed"] is True
    assert after_strategy_interval["replay_ran"] is False  # daily interval not yet elapsed
    assert service.status()["strategy_refresh_count"] == 2
    assert service.status()["replay_count"] == 1


def test_evidence_loop_honors_disabled_flags():
    now_ms = int(time.time() * 1000)
    repo = FakeEvidenceRepo(now_ms)
    service = EngineEvidenceRefreshLoopService(
        settings=_settings(engine_strategy_regime_refresh_enabled=False, engine_replay_comparison_schedule_enabled=False),
        repository=repo,
    )

    result = anyio.run(service.run_due, now_ms)

    assert result == {"strategy_refreshed": False, "replay_ran": False}
    assert repo.performance_rows == []
    assert repo.replay_artifacts == []
    assert service._enabled is False
