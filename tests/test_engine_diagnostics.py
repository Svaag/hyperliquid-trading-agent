from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import anyio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from hyperliquid_trading_agent.app.engine.diagnostics import build_candidate_funnel, build_strategy_funnel


class _DiagnosticsRepository:
    def __init__(self) -> None:
        self.now = 2_000_000
        self.packet_summary_kwargs: list[dict[str, object]] = []
        self.candidate = {
            "candidate_id": "cand_1",
            "strategy_id": "microstructure_ofi_v2",
            "asset": "BTC",
            "side": "long",
            "horizon": "5m",
            "created_at_ms": self.now - 1_000,
            "metadata": {"strategy_family": "microstructure_orderflow"},
        }

    async def list_alpha_candidates(self, **kwargs):
        return [self.candidate]

    async def list_ev_estimates(self, **kwargs):
        return [{"estimate_id": "ev_1", "candidate_id": "cand_1", "created_at_ms": self.now - 900}]

    async def list_allocation_decisions(self, **kwargs):
        return [
            {
                "allocation_id": "alloc_1",
                "candidate_id": "cand_1",
                "status": "skip",
                "reason_codes": ["council_reject"],
                "created_at_ms": self.now - 800,
            }
        ]

    async def list_candidate_trade_packet_summaries(self, **kwargs):
        self.packet_summary_kwargs.append(kwargs)
        return [
            {
                "packet_id": "packet_1",
                "candidate_id": "cand_1",
                "created_at_ms": self.now - 850,
                "allocation": {
                    "status": "allocate",
                    "reason_codes": [],
                    "metadata": {
                        "diversity": {
                            "decision": "allow",
                            "reason_codes": [
                                "family_hard_share_exceeded",
                                "shadow_observation_report_only",
                            ],
                            "projected": {"shadow_observation_report_only": True},
                        }
                    },
                },
                "risk_decision": {"decision": "allow", "allowed": True},
            }
        ]

    async def list_candidate_trade_packets(self, **kwargs):
        raise AssertionError("funnel diagnostics must not load full candidate packets")

    async def list_council_reviews(self, **kwargs):
        return [
            {
                "review_id": "council_1",
                "candidate_id": "cand_1",
                "decision": "reject",
                "vetoes": ["concentration_cap_breach"],
                "created_at_ms": self.now - 800,
            }
        ]

    async def list_order_intents(self, **kwargs):
        return []

    async def list_execution_reports(self, **kwargs):
        return []

    async def list_candidate_outcome_attributions(self, **kwargs):
        return []

    async def list_engine_strategy_evaluations(self, **kwargs):
        return [
            {
                "evaluation_id": "seval_1",
                "engine_run_id": "erun_1",
                "evaluated_at_ms": self.now - 1_000,
                "asset": "BTC",
                "strategy_id": "microstructure_ofi_v2",
                "strategy_family": "microstructure_orderflow",
                "selection_status": "selected",
                "selection_reason": "selected",
                "missing_features": [],
                "stale_features": [],
                "generation_outcome": "generated",
                "trigger_fired": True,
                "candidate_count": 1,
                "reason_codes": ["candidate_generated"],
            },
            {
                "evaluation_id": "seval_2",
                "engine_run_id": "erun_1",
                "evaluated_at_ms": self.now - 1_000,
                "asset": "BTC",
                "strategy_id": "funding_carry_v1",
                "strategy_family": "funding_basis",
                "selection_status": "selected",
                "selection_reason": "selected",
                "missing_features": [],
                "stale_features": ["funding_hourly"],
                "generation_outcome": "no_trigger",
                "trigger_fired": False,
                "candidate_count": 0,
                "reason_codes": ["trigger_conditions_not_met", "stale_required_features"],
            },
        ]

    async def list_strategy_specs(self, **kwargs):
        return [
            {
                "strategy_id": "microstructure_ofi_v2",
                "family": "microstructure_orderflow",
                "counts_for_breadth": True,
                "metadata": {"paper_eligible": True},
            },
            {
                "strategy_id": "funding_carry_v1",
                "family": "funding_basis",
                "counts_for_breadth": True,
                "metadata": {"paper_eligible": True},
            },
        ]


def test_candidate_funnel_deduplicates_downstream_allocation_symptoms() -> None:
    repo = _DiagnosticsRepository()

    report = anyio.run(
        lambda: build_candidate_funnel(repo, as_of_ms=repo.now, window_hours=1)
    )

    assert report["candidate_count"] == 1
    assert report["stage_counts"]["allocator_approved"] == 1
    assert report["stage_counts"]["diversity_allowed"] == 1
    assert report["first_failure_counts"] == {"council_rejected": 1}
    assert report["reason_counts"]["concentration_cap_breach"] == 1
    assert "allocation_not_approved" not in report["reason_counts"]
    assert report["items"][0]["pre_council_allocation_status"] == "allocate"
    assert report["items"][0]["final_allocation_status"] == "skip"
    assert repo.packet_summary_kwargs == [
        {
            "limit": 20_000,
            "since_ms": repo.now - 3_600_000,
            "until_ms": repo.now,
            "strategy_id": None,
        }
    ]


def test_strategy_funnel_preserves_selected_no_trigger_and_freshness_evidence() -> None:
    repo = _DiagnosticsRepository()

    report = anyio.run(
        lambda: build_strategy_funnel(repo, as_of_ms=repo.now, window_hours=1)
    )

    assert report["activation_telemetry_available"] is True
    assert report["active_strategy_count"] == 1
    funding = next(item for item in report["groups"] if item["strategy_id"] == "funding_carry_v1")
    assert funding["selected_count"] == 1
    assert funding["triggered_evaluation_count"] == 0
    assert funding["stale_feature_counts"] == {"funding_hourly": 1}
    assert funding["no_candidate_reason_counts"]["trigger_conditions_not_met"] == 1


def test_strategy_evaluation_migration_creates_append_only_evidence_table() -> None:
    engine = create_engine("sqlite://")
    spec = spec_from_file_location(
        "migration_0029_engine_strategy_evaluations",
        Path("alembic/versions/0029_engine_strategy_evaluations.py"),
    )
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("engine_strategy_evaluations")}

    assert "engine_strategy_evaluations" in inspector.get_table_names()
    assert {
        "evaluation_id",
        "engine_run_id",
        "strategy_id",
        "selection_status",
        "generation_outcome",
        "feature_ages_ms_json",
        "reason_codes_json",
    } <= columns
