from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hyperliquid_trading_agent.app.config import ServiceRole

CommandSideEffect = Literal["none", "db_write", "external_request", "discord_send", "paper_trade"]
HandlerStatus = Literal["required", "implemented", "unsupported"]


@dataclass(frozen=True)
class WorkerCommandSpec:
    command_type: str
    target_role: ServiceRole
    handler_name: str
    source_endpoints: tuple[str, ...]
    description: str
    paper_state_mutation: bool = False
    external_side_effect: CommandSideEffect = "none"
    idempotency_key_fields: tuple[str, ...] = ()
    handler_required: bool = True
    handler_status: HandlerStatus = "required"


COMMAND_SPECS: tuple[WorkerCommandSpec, ...] = (
    WorkerCommandSpec(
        "ask",
        ServiceRole.AGENT,
        "_handle_ask",
        ("POST /ask",),
        "Execute the LLM ask path outside the public API process.",
        external_side_effect="external_request",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "trade_proposal",
        ServiceRole.AGENT,
        "_handle_trade_proposal",
        ("POST /trade/proposals",),
        "Run the high-stakes trade proposal graph outside the public API process.",
        external_side_effect="external_request",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "world_model_adapter_poll",
        ServiceRole.WORLD_MODEL,
        "_handle_adapter_poll",
        ("POST /world-model/adapters/poll", "POST /world-model/adapters/{adapter_name}/poll"),
        "Poll one or all World Model REST adapters from the world_model worker.",
        external_side_effect="external_request",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "world_model_dev_seed",
        ServiceRole.WORLD_MODEL,
        "_handle_dev_seed",
        ("POST /world-model/dev/seed",),
        "Create a local/dev seed event through the world_model worker.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "discord_test",
        ServiceRole.DISCORD_PUBLISHER,
        "_handle_discord_test",
        ("POST /newswire/discord/test",),
        "Send a Newswire Discord test message from the send-only publisher worker.",
        external_side_effect="discord_send",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "engine_strategy_regime_refresh",
        ServiceRole.TRADER,
        "_handle_engine_strategy_regime_refresh",
        ("POST /engine/strategy-regime-performance/refresh",),
        "Refresh report-only strategy/regime performance rows.",
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "engine_position_thesis_cleanup",
        ServiceRole.TRADER,
        "_handle_engine_position_thesis_cleanup",
        ("POST /engine/position-theses/cleanup",),
        "Bulk-close stale shadow/paper position theses opened before a cutoff.",
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "engine_bandit_run",
        ServiceRole.TRADER,
        "_handle_engine_bandit_run",
        ("POST /engine/bandit-recommendations/run",),
        "Run report-only offline contextual bandit recommendations.",
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "engine_replay_comparison_run",
        ServiceRole.TRADER,
        "_handle_engine_replay_comparison_run",
        ("POST /engine/replay-comparisons/run",),
        "Run ledger-based shadow replay comparison.",
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "hip4_loop_run_once",
        ServiceRole.TRADER,
        "_handle_hip4_loop_run_once",
        ("POST /hip4/loop/run-once",),
        "Run one bounded HIP4 proactive loop iteration.",
        paper_state_mutation=True,
        external_side_effect="external_request",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "hip4_scan_run",
        ServiceRole.TRADER,
        "_handle_hip4_scan_run",
        ("POST /hip4/scan/run",),
        "Run one HIP4 scan in trader-owned runtime.",
        external_side_effect="external_request",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "hip4_paper_execute",
        ServiceRole.TRADER,
        "_handle_hip4_paper_execute",
        ("POST /hip4/paper/execute/{candidate_id}",),
        "Execute a HIP4 candidate in paper mode after existing risk checks.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("candidate_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "hip4_reconcile_run",
        ServiceRole.TRADER,
        "_handle_hip4_reconcile_run",
        ("POST /hip4/reconcile/run",),
        "Run HIP4 paper reconciliation.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "hip4_manual_ticket",
        ServiceRole.TRADER,
        "_handle_hip4_manual_ticket",
        ("POST /hip4/manual-ticket/{candidate_id}",),
        "Export a HIP4 manual ticket through the trader worker boundary.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        idempotency_key_fields=("candidate_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_pause",
        ServiceRole.TRADER,
        "_handle_autonomy_pause",
        ("POST /autonomy/pause",),
        "Pause the autonomy loop from the trader worker.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_resume",
        ServiceRole.TRADER,
        "_handle_autonomy_resume",
        ("POST /autonomy/resume",),
        "Resume the autonomy loop from the trader worker.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_signal_approve",
        ServiceRole.TRADER,
        "_handle_autonomy_signal_approve",
        ("POST /autonomy/signals/{signal_id}/approve",),
        "Approve an autonomy signal into paper order/fill/position state.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("signal_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_signal_reject",
        ServiceRole.TRADER,
        "_handle_autonomy_signal_reject",
        ("POST /autonomy/signals/{signal_id}/reject",),
        "Reject an autonomy signal from the trader worker.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        idempotency_key_fields=("signal_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_signal_expire",
        ServiceRole.TRADER,
        "_handle_autonomy_signal_expire",
        ("POST /autonomy/signals/{signal_id}/expire",),
        "Expire an autonomy signal from the trader worker.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        idempotency_key_fields=("signal_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_equity_signal_approve",
        ServiceRole.TRADER,
        "_handle_autonomy_equity_signal_approve",
        ("POST /autonomy/equity/signals/{signal_id}/approve",),
        "Approve an equity autonomy signal into paper state.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("signal_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "autonomy_equity_signal_reject",
        ServiceRole.TRADER,
        "_handle_autonomy_equity_signal_reject",
        ("POST /autonomy/equity/signals/{signal_id}/reject",),
        "Reject an equity autonomy signal from the trader worker.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        idempotency_key_fields=("signal_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "paper_trade_draft",
        ServiceRole.TRADER,
        "_handle_paper_trade_draft",
        ("POST /paper/trades/draft",),
        "Draft a manual paper trade; no fill is created until confirmation.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "paper_trade_confirm",
        ServiceRole.TRADER,
        "_handle_paper_trade_confirm",
        ("POST /paper/trades/{order_id}/confirm",),
        "Confirm a drafted manual paper trade into order/fill/position state.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("order_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "paper_trade_cancel",
        ServiceRole.TRADER,
        "_handle_paper_trade_cancel",
        ("POST /paper/trades/{order_id}/cancel",),
        "Cancel a drafted manual paper trade.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        idempotency_key_fields=("order_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "paper_position_close",
        ServiceRole.TRADER,
        "_handle_paper_position_close",
        ("POST /paper/positions/{position_id}/close",),
        "Close a paper position by id or unique symbol reference.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("position_ref",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "prediction_market_bet_draft",
        ServiceRole.TRADER,
        "_handle_prediction_market_bet_draft",
        ("POST /prediction-markets/paper/drafts",),
        "Draft a Discord player prediction-market paper bet.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "prediction_market_bet_confirm",
        ServiceRole.TRADER,
        "_handle_prediction_market_bet_confirm",
        ("POST /prediction-markets/paper/drafts/{draft_id}/confirm",),
        "Confirm a prediction-market paper draft into a player position.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("draft_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "prediction_market_bet_cancel",
        ServiceRole.TRADER,
        "_handle_prediction_market_bet_cancel",
        ("POST /prediction-markets/paper/drafts/{draft_id}/cancel",),
        "Cancel a prediction-market paper draft.",
        paper_state_mutation=True,
        external_side_effect="db_write",
        idempotency_key_fields=("draft_id",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "prediction_market_position_close",
        ServiceRole.TRADER,
        "_handle_prediction_market_position_close",
        ("POST /prediction-markets/paper/positions/{position_ref}/close",),
        "Close a prediction-market paper position.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("position_ref",),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "prediction_market_settlement_apply",
        ServiceRole.TRADER,
        "_handle_prediction_market_settlement_apply",
        ("POST /prediction-markets/settlements",),
        "Apply a provider/admin settlement to open prediction-market paper positions.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        idempotency_key_fields=("venue", "market_id", "outcome_id"),
        handler_status="implemented",
    ),
    WorkerCommandSpec(
        "prediction_market_settlement_sweep",
        ServiceRole.TRADER,
        "_handle_prediction_market_settlement_sweep",
        ("POST /prediction-markets/settlements/sweep",),
        "Sweep settled provider prediction signals into player paper settlements.",
        paper_state_mutation=True,
        external_side_effect="paper_trade",
        handler_status="implemented",
    ),
    WorkerCommandSpec("tracking_pause", ServiceRole.TRADER, "_handle_tracking_pause", ("POST /tracking/positions/{tracker_id}/pause",), "Pause a tracker through the trader worker.", paper_state_mutation=True, external_side_effect="db_write", idempotency_key_fields=("tracker_id",), handler_status="implemented"),
    WorkerCommandSpec("tracking_resume", ServiceRole.TRADER, "_handle_tracking_resume", ("POST /tracking/positions/{tracker_id}/resume",), "Resume a tracker through the trader worker.", paper_state_mutation=True, external_side_effect="db_write", idempotency_key_fields=("tracker_id",), handler_status="implemented"),
    WorkerCommandSpec("tracking_stop", ServiceRole.TRADER, "_handle_tracking_stop", ("POST /tracking/positions/{tracker_id}/stop",), "Stop a tracker through the trader worker.", paper_state_mutation=True, external_side_effect="db_write", idempotency_key_fields=("tracker_id",), handler_status="implemented"),
    WorkerCommandSpec("admin_debug_seed_flip_demo", ServiceRole.TRADER, "_handle_admin_debug_seed_flip_demo", ("POST /admin/debug/seed-flip-demo",), "Seed a local flip demo from the trader worker.", paper_state_mutation=True, external_side_effect="db_write", handler_status="implemented"),
    WorkerCommandSpec("orchestration_wave_run_once", ServiceRole.SCHEDULER, "_handle_orchestration_wave_run_once", ("POST /orchestration/wave/run-once",), "Run one Wave Supervisor pass from the scheduler worker.", external_side_effect="db_write", handler_status="implemented"),
    WorkerCommandSpec("autonomy_evaluations_run", ServiceRole.SCHEDULER, "_handle_autonomy_evaluations_run", ("POST /autonomy/evaluations/run",), "Mark due autonomy signal and event evaluations.", external_side_effect="db_write", handler_status="implemented"),
    WorkerCommandSpec("autonomy_evaluations_backfill", ServiceRole.SCHEDULER, "_handle_autonomy_evaluations_backfill", ("POST /autonomy/evaluations/backfill",), "Backfill missing signal evaluations.", external_side_effect="db_write", handler_status="implemented"),
    WorkerCommandSpec("autonomy_event_evaluations_backfill", ServiceRole.SCHEDULER, "_handle_autonomy_event_evaluations_backfill", ("POST /autonomy/evaluations/events/backfill",), "Backfill missing alpha-event evaluations.", external_side_effect="db_write", handler_status="implemented"),
    WorkerCommandSpec("autonomy_daily_report_run", ServiceRole.SCHEDULER, "_handle_autonomy_daily_report_run", ("POST /autonomy/reports/daily/run",), "Generate a daily autonomy report.", external_side_effect="db_write", handler_status="implemented"),
    WorkerCommandSpec("autonomy_weekly_report_run", ServiceRole.SCHEDULER, "_handle_autonomy_weekly_report_run", ("POST /autonomy/reports/weekly/run",), "Generate a weekly autonomy report.", external_side_effect="db_write", handler_status="implemented"),
)

COMMAND_REGISTRY: dict[str, WorkerCommandSpec] = {spec.command_type: spec for spec in COMMAND_SPECS}


def command_spec(command_type: str) -> WorkerCommandSpec | None:
    return COMMAND_REGISTRY.get(str(command_type))


def command_specs_for_role(role: ServiceRole | str) -> tuple[WorkerCommandSpec, ...]:
    role_value = role.value if isinstance(role, ServiceRole) else str(role)
    return tuple(spec for spec in COMMAND_SPECS if spec.target_role.value == role_value)


def command_registry_json() -> list[dict[str, object]]:
    return [
        {
            "command_type": spec.command_type,
            "target_role": spec.target_role.value,
            "handler_name": spec.handler_name,
            "source_endpoints": list(spec.source_endpoints),
            "description": spec.description,
            "paper_state_mutation": spec.paper_state_mutation,
            "external_side_effect": spec.external_side_effect,
            "idempotency_key_fields": list(spec.idempotency_key_fields),
            "handler_required": spec.handler_required,
            "handler_status": spec.handler_status,
        }
        for spec in COMMAND_SPECS
    ]
