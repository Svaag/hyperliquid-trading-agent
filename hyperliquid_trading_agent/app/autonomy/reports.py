from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.autonomy.discord import AutonomyAlertSink
from hyperliquid_trading_agent.app.autonomy.schemas import (
    AlphaEventEvaluation,
    AutonomyReport,
    TokenCapitalSnapshot,
    TuningProposal,
)
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    AUTONOMY_DAILY_REPORTS_POSTED,
    AUTONOMY_WEEKLY_REPORTS_POSTED,
    TOKEN_CAPITAL_SCORE,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class ReportWindow:
    key: str
    start_ms: int
    end_ms: int


class TokenCapitalScorer:
    """Hybrid scoreboard for the memory loop.

    The aggregate is intentionally explainable: paper PnL contributes, but hard
    gates and quality/reliability/memory components prevent raw PnL from hiding
    bad process.
    """

    def compute(
        self,
        *,
        window: str,
        timestamp_ms: int,
        portfolio_snapshot: Any | None,
        event_evaluations: list[AlphaEventEvaluation] | None = None,
        equity_portfolio_snapshot: Any | None = None,
        memory_counts: dict[str, int] | None = None,
        feedback_items: list[dict[str, Any]] | None = None,
        reliability: dict[str, Any] | None = None,
        hard_gates: list[dict[str, Any]] | None = None,
        created_from_report_id: str | None = None,
    ) -> TokenCapitalSnapshot:
        memory_counts = memory_counts or {}
        event_evaluations = event_evaluations or []
        feedback_items = feedback_items or []
        reliability = reliability or {}
        hard_gates = hard_gates or []
        risk_adjusted = self._risk_adjusted_performance(portfolio_snapshot)
        signal_quality = self._event_signal_quality(event_evaluations) or 50.0
        memory_score = self._memory_compounding(memory_counts)
        risk_discipline = self._risk_discipline(hard_gates)
        operator_score = self._operator_communication(feedback_items)
        reliability_score = self._reliability(reliability)
        total = (
            risk_adjusted * 0.30
            + signal_quality * 0.20
            + memory_score * 0.20
            + risk_discipline * 0.15
            + operator_score * 0.10
            + reliability_score * 0.05
        )
        for gate in hard_gates:
            cap = gate.get("score_cap")
            penalty = gate.get("penalty")
            if isinstance(cap, (int, float)):
                total = min(total, float(cap))
            if isinstance(penalty, (int, float)):
                total -= float(penalty)
        total = _clamp(total)
        return TokenCapitalSnapshot(
            id=f"tc_{uuid4().hex}",
            timestamp_ms=timestamp_ms,
            window=window,  # type: ignore[arg-type]
            total_score=total,
            risk_adjusted_performance_score=risk_adjusted,
            signal_quality_score=signal_quality,
            memory_compounding_score=memory_score,
            risk_discipline_score=risk_discipline,
            operator_communication_score=operator_score,
            reliability_score=reliability_score,
            hard_gate_penalties=hard_gates,
            component_details={
                "portfolio": _snapshot_details(portfolio_snapshot),
                "equity_portfolio": _snapshot_details(equity_portfolio_snapshot),
                "event_evaluation_count": len(event_evaluations),
                "completed_event_evaluation_count": len([item for item in event_evaluations if item.status == "complete"]),
                "event_quality": _event_quality_details(event_evaluations),
                "memory_counts": memory_counts,
                "feedback_count": len(feedback_items),
                "reliability": reliability,
                "weights": {
                    "risk_adjusted_performance": 0.30,
                    "signal_quality": 0.20,
                    "memory_compounding": 0.20,
                    "risk_discipline": 0.15,
                    "operator_communication": 0.10,
                    "reliability": 0.05,
                },
            },
            created_from_report_id=created_from_report_id,
            metadata={"definition": "validation-weighted ability to improve risk-adjusted paper outcomes through memory, catalyst interpretation, risk discipline, and operator communication", "exchange_actions": []},
        )

    def _risk_adjusted_performance(self, snapshot: Any | None) -> float:
        score = 50.0
        metrics = getattr(snapshot, "metrics", {}) or {}
        return_pct = _float(metrics.get("return_pct"))
        if return_pct is None and snapshot is not None:
            initial = _float(metrics.get("initial_equity_usd")) or 0
            equity = _float(getattr(snapshot, "equity_usd", None)) or 0
            return_pct = (equity / initial - 1) * 100 if initial else None
        if return_pct is not None:
            score += max(-20.0, min(20.0, return_pct * 8))
        sharpe = _float(getattr(snapshot, "sharpe", None)) if snapshot is not None else None
        if sharpe is not None:
            score += max(-10.0, min(12.0, sharpe * 4))
        drawdown = _float(getattr(snapshot, "drawdown_pct", None)) if snapshot is not None else None
        if drawdown is not None:
            score -= min(25.0, drawdown * 3)
        gross = _float(getattr(snapshot, "gross_exposure_usd", None)) if snapshot is not None else None
        equity_for_leverage = _float(getattr(snapshot, "equity_usd", None)) if snapshot is not None else None
        if gross is not None and equity_for_leverage and equity_for_leverage > 0:
            leverage = gross / equity_for_leverage
            if leverage > 3:
                score -= min(20.0, (leverage - 3) * 10)
        return _clamp(score)

    def _event_signal_quality(self, event_evaluations: list[AlphaEventEvaluation]) -> float | None:
        completed = [item for item in event_evaluations if item.status == "complete"]
        if not completed:
            return None
        worked = len([item for item in completed if item.terminal_outcome == "worked"])
        failed = len([item for item in completed if item.terminal_outcome == "failed"])
        volatility = len([item for item in completed if item.terminal_outcome == "volatility_only"])
        mixed = len([item for item in completed if item.terminal_outcome == "mixed"])
        score = 50.0 + worked / len(completed) * 30 - failed / len(completed) * 25 + volatility / len(completed) * 8 - mixed / len(completed) * 5
        return _clamp(score)

    def _memory_compounding(self, counts: dict[str, int]) -> float:
        active = counts.get("active_role_lessons", 0) + counts.get("active_operator_lessons", 0)
        shadow = counts.get("shadow_lessons", 0)
        candidates = counts.get("candidate_lessons", 0)
        archived = counts.get("archived_lessons", 0)
        score = 45.0 + min(25.0, active * 1.5) + min(10.0, shadow * 0.5) + min(10.0, candidates * 0.25) + min(5.0, archived * 0.1)
        return _clamp(score)

    def _risk_discipline(self, hard_gates: list[dict[str, Any]]) -> float:
        score = 85.0
        unsupported = len([gate for gate in hard_gates if gate.get("kind") in {"hallucinated_order", "stale_data", "schema_invalid"}])
        score -= unsupported * 15
        return _clamp(score)

    def _operator_communication(self, feedback_items: list[dict[str, Any]]) -> float:
        if not feedback_items:
            return 65.0
        positive = len([item for item in feedback_items if item.get("rating") in {"good", "useful"}])
        negative = len([item for item in feedback_items if item.get("rating") in {"bad", "wrong", "unclear", "too_noisy"}])
        return _clamp(60.0 + positive * 6 - negative * 8)

    def _reliability(self, reliability: dict[str, Any]) -> float:
        score = 90.0
        score -= min(30.0, float(reliability.get("evaluation_errors", 0) or 0) * 5)
        score -= min(20.0, float(reliability.get("report_errors", 0) or 0) * 8)
        stale = reliability.get("stale_market_data")
        if stale:
            score -= 20
        return _clamp(score)


class AutonomyReportService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any = None,
        event_evaluation_service: Any | None = None,
        memory_service: Any | None = None,
        tuning_service: Any | None = None,
        portfolio_service: Any | None = None,
        equity_portfolio_service: Any | None = None,
        alert_sink: AutonomyAlertSink | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.event_evaluation_service = event_evaluation_service
        self.memory_service = memory_service
        self.tuning_service = tuning_service
        self.portfolio_service = portfolio_service
        self.equity_portfolio_service = equity_portfolio_service
        self.alert_sink = alert_sink
        self.scorer = TokenCapitalScorer()
        self.last_daily_key: str | None = None
        self.last_weekly_key: str | None = None
        self.latest_token_capital: TokenCapitalSnapshot | None = None
        self.report_error_count = 0
        self.last_report_at_ms: int | None = None
        self.last_daily_report_at_ms: int | None = None
        self.last_weekly_report_at_ms: int | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.autonomy_reports_enabled,
            "effective_enabled": self.settings.autonomy_reports_effective_enabled,
            "daily_enabled": self.settings.autonomy_daily_report_enabled,
            "weekly_enabled": self.settings.autonomy_weekly_report_enabled,
            "last_report_at_ms": self.last_report_at_ms,
            "last_daily_report_at_ms": self.last_daily_report_at_ms,
            "last_weekly_report_at_ms": self.last_weekly_report_at_ms,
            "latest_token_capital_score": self.latest_token_capital.total_score if self.latest_token_capital else None,
            "error_count": self.report_error_count,
            "last_error": self.last_error,
        }

    async def maybe_run_scheduled(self, now_ms: int | None = None) -> list[AutonomyReport]:
        if not self.settings.autonomy_reports_enabled:
            return []
        now = datetime.fromtimestamp((now_ms or _now_ms()) / 1000, tz=UTC)
        reports: list[AutonomyReport] = []
        if self.settings.autonomy_daily_report_enabled and _time_reached(now, self.settings.autonomy_daily_report_utc):
            window = _daily_window(now)
            if window.key != self.last_daily_key:
                existing = await self._get_report("daily", window.key)
                if existing is None:
                    reports.append(await self.generate_daily(now_ms=now_ms, post=True))
                self.last_daily_key = window.key
        if self.settings.autonomy_weekly_report_enabled and now.strftime("%a").upper()[:3] == self.settings.autonomy_weekly_report_day_normalized and _time_reached(now, self.settings.autonomy_weekly_report_utc):
            window = _weekly_window(now)
            if window.key != self.last_weekly_key:
                existing = await self._get_report("weekly", window.key)
                if existing is None:
                    reports.append(await self.generate_weekly(now_ms=now_ms, post=True))
                self.last_weekly_key = window.key
        return reports

    async def generate_daily(self, now_ms: int | None = None, *, post: bool = False) -> AutonomyReport:
        now = datetime.fromtimestamp((now_ms or _now_ms()) / 1000, tz=UTC)
        return await self._generate("daily", _daily_window(now), post=post)

    async def generate_weekly(self, now_ms: int | None = None, *, post: bool = False) -> AutonomyReport:
        now = datetime.fromtimestamp((now_ms or _now_ms()) / 1000, tz=UTC)
        return await self._generate("weekly", _weekly_window(now), post=post)

    async def list_reports(self, report_type: str = "daily", limit: int = 30) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return await self.repository.list_autonomy_reports(report_type=report_type, limit=limit)
        return []

    async def get_report(self, report_type: str, key: str) -> dict[str, Any] | None:
        return await self._get_report(report_type, key)

    async def token_capital_history(self, window: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return await self.repository.list_token_capital_snapshots(window=window, limit=limit)
        return [self.latest_token_capital.model_dump(mode="json")] if self.latest_token_capital else []

    async def _generate(self, report_type: str, window: ReportWindow, *, post: bool) -> AutonomyReport:
        try:
            event_evaluations = await self._event_evaluations_for_window(window)
            memory_counts = await self._memory_counts()
            feedback = await self._feedback_for_window(window)
            proposals = await self._tuning_proposals_for_window(event_evaluations)
            snapshot = self.portfolio_service.latest_snapshot() if self.portfolio_service is not None else None
            equity_snapshot = _latest_equity_snapshot(self.equity_portfolio_service)
            reliability = {
                "evaluation_errors": getattr(self.event_evaluation_service, "error_count", 0),
                "report_errors": self.report_error_count,
                "stale_market_data": False,
            }
            report_id = f"{report_type}_{uuid4().hex}"
            token_capital = self.scorer.compute(
                window="weekly" if report_type == "weekly" else "daily",
                timestamp_ms=window.end_ms,
                portfolio_snapshot=snapshot,
                event_evaluations=event_evaluations,
                equity_portfolio_snapshot=equity_snapshot,
                memory_counts=memory_counts,
                feedback_items=feedback,
                reliability=reliability,
                hard_gates=[],
                created_from_report_id=report_id,
            )
            report_json = self._build_report_json(report_type, window, event_evaluations, snapshot, equity_snapshot, token_capital, memory_counts, feedback, proposals)
            summary = format_report_summary(report_type, window.key, report_json)
            report = AutonomyReport(
                id=report_id,
                report_type=report_type,  # type: ignore[arg-type]
                key=window.key,
                period_start_ms=window.start_ms,
                period_end_ms=window.end_ms,
                generated_at_ms=_now_ms(),
                token_capital=token_capital,
                summary=summary,
                report=report_json,
                metadata={"exchange_actions": []},
            )
            if post:
                report = await self._post(report)
            await self._persist(report)
            self.latest_token_capital = token_capital
            self.last_report_at_ms = report.generated_at_ms
            TOKEN_CAPITAL_SCORE.labels(window=token_capital.window).set(token_capital.total_score)
            if report_type == "weekly":
                self.last_weekly_report_at_ms = report.generated_at_ms
            else:
                self.last_daily_report_at_ms = report.generated_at_ms
            return report
        except Exception as exc:
            self.report_error_count += 1
            self.last_error = type(exc).__name__
            metric = AUTONOMY_WEEKLY_REPORTS_POSTED if report_type == "weekly" else AUTONOMY_DAILY_REPORTS_POSTED
            metric.labels(result="error").inc()
            log.warning("autonomy_report_generate_failed", report_type=report_type, error=type(exc).__name__)
            raise

    async def _post(self, report: AutonomyReport) -> AutonomyReport:
        if self.alert_sink is None or not self.settings.autonomy_alert_channel_configured:
            return report
        message_id = await self.alert_sink.send(self.settings.autonomy_alert_channel_id, report.summary)
        metric = AUTONOMY_WEEKLY_REPORTS_POSTED if report.report_type == "weekly" else AUTONOMY_DAILY_REPORTS_POSTED
        metric.labels(result="ok" if message_id else "unknown").inc()
        return report.model_copy(update={"discord_channel_id": self.settings.autonomy_alert_channel_id, "discord_message_id": message_id})

    async def _persist(self, report: AutonomyReport) -> None:
        if not self._repo_enabled():
            return
        await self.repository.record_token_capital_snapshot(report.token_capital.model_dump(mode="json"))
        await self.repository.upsert_autonomy_report(report.model_dump(mode="json"))
        await self.repository.record_autonomy_event(
            f"{report.report_type}_report_generated",
            actor="autonomy_reports",
            payload={"report_id": report.id, "key": report.key, "token_capital_score": report.token_capital.total_score, "exchange_actions": []},
        )

    async def _event_evaluations_for_window(self, window: ReportWindow) -> list[AlphaEventEvaluation]:
        if self.event_evaluation_service is None:
            return []
        try:
            evaluations = await self.event_evaluation_service.list_evaluations(limit=2000)
        except Exception:
            return []
        return [item for item in evaluations if window.start_ms <= item.received_at_ms <= window.end_ms or (item.completed_at_ms is not None and window.start_ms <= item.completed_at_ms <= window.end_ms)]

    async def _memory_counts(self) -> dict[str, int]:
        if self.memory_service is None:
            return {}
        status = self.memory_service.status()
        return {
            "active_role_lessons": int(status.get("active_role_lessons", 0) or 0),
            "active_operator_lessons": int(status.get("active_operator_lessons", 0) or 0),
            "candidate_lessons": int(status.get("candidate_lessons", 0) or 0),
            "shadow_lessons": int(status.get("shadow_lessons", 0) or 0),
            "archived_lessons": int(status.get("archived_lessons", 0) or 0),
        }

    async def _feedback_for_window(self, window: ReportWindow) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return [item for item in await self.repository.list_operator_feedback(limit=500) if window.start_ms <= int(item.get("created_at_ms") or 0) <= window.end_ms]
        return []

    async def _tuning_proposals_for_window(self, event_evaluations: list[AlphaEventEvaluation]) -> list[TuningProposal]:
        if self.tuning_service is None:
            return []
        proposals: list[TuningProposal] = []
        generate_events = getattr(self.tuning_service, "generate_from_event_evaluations", None)
        if callable(generate_events):
            proposals.extend(await generate_events(event_evaluations))
        return proposals

    def _build_report_json(
        self,
        report_type: str,
        window: ReportWindow,
        event_evaluations: list[AlphaEventEvaluation],
        snapshot: Any | None,
        equity_snapshot: Any | None,
        token_capital: TokenCapitalSnapshot,
        memory_counts: dict[str, int],
        feedback: list[dict[str, Any]],
        proposals: list[TuningProposal],
    ) -> dict[str, Any]:
        completed_events = [item for item in event_evaluations if item.status == "complete"]
        best_events = sorted(completed_events, key=lambda item: item.max_favorable_bps or -99999, reverse=True)[:3]
        worst_events = sorted(completed_events, key=lambda item: item.max_adverse_bps or 99999)[:3]
        return {
            "report_type": report_type,
            "key": window.key,
            "period": {"start_ms": window.start_ms, "end_ms": window.end_ms},
            "token_capital": token_capital.model_dump(mode="json"),
            "portfolio": _snapshot_details(snapshot),
            "equity_portfolio": _snapshot_details(equity_snapshot),
            "events": {
                "evaluated": len(event_evaluations),
                "completed": len(completed_events),
                "worked": len([item for item in completed_events if item.terminal_outcome == "worked"]),
                "failed": len([item for item in completed_events if item.terminal_outcome == "failed"]),
                "volatility_only": len([item for item in completed_events if item.terminal_outcome == "volatility_only"]),
                "hit_rate": _event_hit_rate(completed_events),
            },
            "best_events": [_event_evaluation_brief(item) for item in best_events],
            "worst_events": [_event_evaluation_brief(item) for item in worst_events],
            "memory": memory_counts,
            "operator_feedback": {"count": len(feedback), "recent": feedback[:5]},
            "tuning_proposals": [item.model_dump(mode="json") for item in proposals[:10]],
            "hard_gates": token_capital.hard_gate_penalties,
            "safety": {"paper_only": True, "exchange_actions": [], "tuning_auto_apply_enabled": False},
        }

    async def _get_report(self, report_type: str, key: str) -> dict[str, Any] | None:
        if self._repo_enabled():
            return await self.repository.get_autonomy_report(report_type, key)
        return None

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)


def format_report_summary(report_type: str, key: str, report: dict[str, Any]) -> str:
    token = report.get("token_capital", {})
    portfolio = report.get("portfolio", {})
    events = report.get("events", {})
    proposals = report.get("tuning_proposals", [])
    title = "AI Trading Desk Weekly Review" if report_type == "weekly" else "AI Trading Desk Daily Report"
    lines = [f"📊 **{title} — {key} UTC**", ""]
    lines.append(f"Token Capital: **{token.get('total_score', 0):.0f}/100**")
    if portfolio:
        lines.append(f"Paper equity: `${portfolio.get('equity_usd', 0):,.2f}` | PnL: `${portfolio.get('total_pnl_usd', 0):,.2f}` | Max DD: `{portfolio.get('drawdown_pct', 0):.2f}%`")
    lines.extend(
        [
            "",
            "**Catalyst evaluation:**",
            f"- Catalysts evaluated: `{events.get('evaluated', 0)}` | Completed: `{events.get('completed', 0)}` | Worked/failed/vol-only: `{events.get('worked', 0)}`/`{events.get('failed', 0)}`/`{events.get('volatility_only', 0)}` | Hit rate: `{_fmt(events.get('hit_rate'))}`",
        ]
    )
    if report.get("best_events"):
        lines.append("\n**Best catalysts:**")
        lines.extend(f"- {item['symbol']} {item['event_type']} {item['direction']}: MFE `{_fmt(item.get('max_favorable_bps'))}` bps, outcome `{item['terminal_outcome']}`" for item in report["best_events"][:3])
    if report.get("worst_events"):
        lines.append("\n**Worst catalysts:**")
        lines.extend(f"- {item['symbol']} {item['event_type']} {item['direction']}: MAE `{_fmt(item.get('max_adverse_bps'))}` bps, outcome `{item['terminal_outcome']}`" for item in report["worst_events"][:3])
    lines.append("\n**Memory:**")
    memory = report.get("memory", {})
    lines.append(f"- Active role lessons: `{memory.get('active_role_lessons', 0)}` | Shadow: `{memory.get('shadow_lessons', 0)}` | Candidates: `{memory.get('candidate_lessons', 0)}`")
    if proposals:
        lines.append("\n**Tuning proposals:**")
        lines.extend(f"- `{item['id']}` {item['title']} (confidence `{item.get('confidence', 0):.2f}`)" for item in proposals[:3])
    lines.append("\nNo live trades placed. Tuning proposals are not auto-applied.")
    return "\n".join(lines)


def _daily_window(now: datetime) -> ReportWindow:
    end = datetime(now.year, now.month, now.day, tzinfo=UTC)
    if now.hour > 0 or now.minute >= 5:
        # At/after the scheduled report time, report the just-completed UTC day.
        pass
    start = end - timedelta(days=1)
    return ReportWindow(key=start.date().isoformat(), start_ms=_ms(start), end_ms=_ms(end) - 1)


def _weekly_window(now: datetime) -> ReportWindow:
    this_monday = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
    start = this_monday - timedelta(days=7)
    end = this_monday
    iso_year, iso_week, _ = start.isocalendar()
    return ReportWindow(key=f"{iso_year}-W{iso_week:02d}", start_ms=_ms(start), end_ms=_ms(end) - 1)


def _time_reached(now: datetime, hhmm: str) -> bool:
    hour, _, minute = hhmm.partition(":")
    return (now.hour, now.minute) >= (int(hour), int(minute))


def _event_evaluation_brief(item: AlphaEventEvaluation) -> dict[str, Any]:
    return {
        "evaluation_id": item.id,
        "event_id": item.event_id,
        "symbol": item.symbol,
        "event_source": item.event_source,
        "provider": item.provider,
        "event_type": item.event_type,
        "asset_class": item.asset_class,
        "direction": item.direction,
        "sentiment": item.sentiment,
        "terminal_outcome": item.terminal_outcome,
        "max_favorable_bps": item.max_favorable_bps,
        "max_adverse_bps": item.max_adverse_bps,
        "max_abs_move_bps": item.max_abs_move_bps,
    }


def _event_quality_details(items: list[AlphaEventEvaluation]) -> dict[str, Any]:
    completed = [item for item in items if item.status == "complete"]
    by_type: dict[str, dict[str, int]] = {}
    for item in completed:
        key = f"{item.asset_class}:{item.event_source}:{item.event_type}:{item.sentiment}"
        bucket = by_type.setdefault(key, {"count": 0, "worked": 0, "failed": 0, "volatility_only": 0})
        bucket["count"] += 1
        if item.terminal_outcome in bucket:
            bucket[item.terminal_outcome] += 1
    return {
        "completed": len(completed),
        "hit_rate": _event_hit_rate(completed),
        "failed_rate": _event_failed_rate(completed),
        "volatility_only_count": len([item for item in completed if item.terminal_outcome == "volatility_only"]),
        "by_scope": by_type,
    }


def _event_hit_rate(items: list[AlphaEventEvaluation]) -> float | None:
    if not items:
        return None
    return len([item for item in items if item.terminal_outcome == "worked"]) / len(items)


def _event_failed_rate(items: list[AlphaEventEvaluation]) -> float | None:
    if not items:
        return None
    return len([item for item in items if item.terminal_outcome == "failed"]) / len(items)


def _latest_equity_snapshot(service: Any | None) -> Any | None:
    if service is None:
        return None
    snapshots = getattr(service, "snapshots", None)
    if snapshots:
        return snapshots[-1]
    snapshot = getattr(service, "snapshot", None)
    if callable(snapshot):
        try:
            return snapshot()
        except Exception:
            return None
    return None


def _snapshot_details(snapshot: Any | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    data = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
    return {
        "equity_usd": data.get("equity_usd"),
        "cash_usd": data.get("cash_usd"),
        "realized_pnl_usd": data.get("realized_pnl_usd"),
        "unrealized_pnl_usd": data.get("unrealized_pnl_usd"),
        "total_pnl_usd": data.get("total_pnl_usd"),
        "drawdown_pct": data.get("drawdown_pct"),
        "sharpe": data.get("sharpe"),
        "gross_exposure_usd": data.get("gross_exposure_usd"),
        "net_exposure_usd": data.get("net_exposure_usd"),
        "metrics": data.get("metrics", {}),
    }


def _avg(values: list[float | None]) -> float | None:
    clean = [float(item) for item in values if item is not None]
    return sum(clean) / len(clean) if clean else None


def _fmt(value: Any) -> str:
    numeric = _float(value)
    return "n/a" if numeric is None else f"{numeric:+.2f}"


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)
