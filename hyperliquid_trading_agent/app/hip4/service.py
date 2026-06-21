from __future__ import annotations

import asyncio
import contextlib
import time
from decimal import Decimal
from typing import Any

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.hip4.capabilities import Hip4CapabilityProbeService
from hyperliquid_trading_agent.app.hip4.client import Hip4InfoClient
from hyperliquid_trading_agent.app.hip4.discord import format_hip4_digest
from hyperliquid_trading_agent.app.hip4.ids import coin
from hyperliquid_trading_agent.app.hip4.orderbook import parse_l2_book
from hyperliquid_trading_agent.app.hip4.paper import Hip4PaperLedger
from hyperliquid_trading_agent.app.hip4.registry import Hip4Registry
from hyperliquid_trading_agent.app.hip4.risk import Hip4RiskChecker
from hyperliquid_trading_agent.app.hip4.scanner import Hip4Scanner
from hyperliquid_trading_agent.app.hip4.schemas import (
    Hip4Candidate,
    Hip4CapabilityProbe,
    Hip4SafetyPosture,
    Hip4ServiceStatus,
)
from hyperliquid_trading_agent.app.hip4.ws import Hip4WsSubscriptionManager, prioritize_hot_coins
from hyperliquid_trading_agent.app.logging import get_logger

log = get_logger(__name__)


class Hip4Service:
    """Read-only/paper/shadow HIP-4 subsystem coordinator."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any | None = None,
        hyperliquid: Any | None = None,
        ws_worker: Any | None = None,
        risk_gateway: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.hyperliquid = hyperliquid
        self.ws_worker = ws_worker
        self.risk_gateway = risk_gateway
        self.alert_sink: Any | None = None
        self.hip4_client = Hip4InfoClient(settings=settings, hyperliquid=hyperliquid) if hyperliquid is not None else None
        self.capability_probe_service = Hip4CapabilityProbeService(settings=settings, hip4_client=self.hip4_client, repository=repository, ws_worker=ws_worker)
        self.capabilities: Hip4CapabilityProbe | None = None
        self.registry = Hip4Registry(settings=settings, hip4_client=self.hip4_client, repository=repository)
        self.ws_manager = Hip4WsSubscriptionManager(settings=settings, ws_worker=ws_worker, hip4_client=self.hip4_client)
        self.scanner = Hip4Scanner(settings=settings)
        self.paper = Hip4PaperLedger(settings=settings, repository=repository)
        self.risk = Hip4RiskChecker(settings=settings, risk_gateway=risk_gateway)
        self.candidates: dict[str, Hip4Candidate] = {}
        self._started = False
        self._last_error: str | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._loop_running = False
        self._loop_started_at_ms: int | None = None
        self._loop_last_cycle_at_ms: int | None = None
        self._loop_last_digest_at_ms: int | None = None
        self._loop_last_reconcile_at_ms: int | None = None
        self._loop_cycle_count = 0
        self._loop_error_count = 0
        self._loop_last_error: str | None = None
        self._loop_last_summary: dict[str, Any] = {}
        self._loop_alert_keys: dict[str, int] = {}
        self._learning: dict[str, Any] = _empty_learning_state()

    async def start(self) -> None:
        if not self.settings.hip4_enabled:
            log.info("hip4_disabled")
            return
        self._started = True
        try:
            self.capabilities = await self.capability_probe_service.probe()
            if self.capabilities.outcome_meta_available and self.capabilities.supports_outcomes:
                await self.registry.refresh()
                await self._update_market_subscriptions()
        except Exception as exc:  # pragma: no cover - external API behavior
            self._last_error = type(exc).__name__
            log.warning("hip4_start_degraded", error=type(exc).__name__)
        await self.send_discord_digest(reason="startup")
        await self.start_proactive_loop()
        log.info("hip4_service_started", mode=self.settings.hip4_mode)

    async def stop(self) -> None:
        await self.stop_proactive_loop()
        await self.ws_manager.stop()
        if self._started:
            log.info("hip4_service_stopped")
        self._started = False

    async def refresh_registry(self) -> dict[str, Any]:
        if not self.settings.hip4_enabled:
            return self.status()
        self.capabilities = await self.capability_probe_service.probe()
        if self.capabilities.outcome_meta_available and self.capabilities.supports_outcomes:
            await self.registry.refresh()
            await self._update_market_subscriptions()
        return self.status()

    async def run_scan(self, *, send_digest: bool = True) -> list[dict[str, Any]]:
        candidates = await self._scan_candidates()
        if send_digest:
            await self.send_discord_digest(candidates=candidates, reason="manual_scan")
        return [candidate.model_dump(mode="json") for candidate in candidates]

    async def _scan_candidates(self) -> list[Hip4Candidate]:
        if not self.settings.hip4_enabled or not self.settings.hip4_scan_enabled:
            return []
        if not self.settings.hip4_mode_allows_scan:
            raise PermissionError("HIP-4 mode does not allow scanning")
        if self.registry.last_refresh_at_ms is None:
            await self.refresh_registry()
        await self._refresh_books_for_scan()
        candidates = self.scanner.scan(outcomes=self.registry.outcomes, questions=self.registry.questions, books=self.ws_manager.books, capabilities=self.capabilities)
        self.candidates.update({candidate.candidate_id: candidate for candidate in candidates})
        await self._persist_candidates(candidates)
        return candidates

    async def execute_paper_candidate(self, candidate_id: str) -> dict[str, Any]:
        if not self.settings.hip4_mode_allows_paper:
            raise PermissionError("HIP-4 mode does not allow paper execution")
        if self.capabilities is None:
            raise PermissionError("HIP-4 capability probe has not run")
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError("HIP-4 candidate not found")
        paper_candidate = candidate.model_copy(update={"mode": "paper"})
        decision = await self.risk.check_candidate(
            paper_candidate,
            capabilities=self.capabilities,
            question=self.registry.questions.get(paper_candidate.question_id) if paper_candidate.question_id is not None else None,
            registry_last_refresh_at_ms=self.registry.last_refresh_at_ms,
        )
        if not decision.allowed:
            raise PermissionError({"risk_decision": decision.model_dump(mode="json")})
        result = await self.paper.execute_candidate(paper_candidate)
        self.candidates[candidate_id] = paper_candidate.model_copy(update={"status": "paper_executed"})
        return result

    async def reconcile_paper(self) -> dict[str, Any]:
        if not self.settings.hip4_mode_allows_paper:
            raise PermissionError("HIP-4 mode does not allow paper reconciliation")
        result = self.paper.reconcile()
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_hip4_reconciliation_run", None)
            if callable(record):
                await record(result)
        return result

    async def start_proactive_loop(self) -> None:
        if self._loop_task is not None or not self._proactive_loop_configured():
            return
        self._loop_running = True
        self._loop_started_at_ms = int(time.time() * 1000)
        self._loop_task = asyncio.create_task(self._proactive_loop(), name="hip4-proactive-shadow-loop")
        log.info("hip4_proactive_loop_started", interval_seconds=self.settings.hip4_proactive_loop_interval_seconds)

    async def stop_proactive_loop(self) -> None:
        self._loop_running = False
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            log.info("hip4_proactive_loop_stopped")

    async def run_proactive_cycle(self, *, manual: bool = False) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        cycle_id = f"hip4loop_{now_ms}_{self._loop_cycle_count + 1}"
        summary: dict[str, Any] = {
            "cycle_id": cycle_id,
            "manual": manual,
            "status": "started",
            "created_at_ms": now_ms,
            "mode": self.settings.hip4_mode,
            "paper_execution_requested": self._proactive_paper_configured(),
        }
        if not self._proactive_scan_allowed():
            summary.update({"status": "skipped", "reason": "proactive_scan_not_allowed"})
            self._record_loop_summary(summary)
            return summary

        try:
            candidates = await self._scan_candidates()
            decisions = await self._risk_classify_candidates(candidates)
            executions = await self._execute_proactive_paper(candidates, decisions)
            reconciliation = await self._maybe_reconcile_paper(now_ms)
            self._update_learning(candidates=candidates, decisions=decisions, executions=executions)
            summary.update(
                {
                    "status": "ok",
                    "candidate_count": len(candidates),
                    "allowed_count": sum(1 for item in decisions.values() if item.get("allowed")),
                    "rejected_count": sum(1 for item in decisions.values() if not item.get("allowed")),
                    "paper_execution_count": len([item for item in executions if item.get("status") == "ok"]),
                    "paper_execution_error_count": len([item for item in executions if item.get("status") == "error"]),
                    "top_candidates": [_candidate_summary(item) for item in _rank_candidates(candidates)[:5]],
                    "executions": executions[-5:],
                    "reconciliation": reconciliation,
                    "rejects": self.scanner.last_rejects[-10:],
                }
            )
            await self._record_loop_audit(summary)
            await self._maybe_send_proactive_digest(candidates=candidates, executions=executions, summary=summary, now_ms=now_ms)
        except Exception as exc:  # pragma: no cover - external API/runtime behavior
            self._loop_error_count += 1
            self._loop_last_error = type(exc).__name__
            summary.update({"status": "error", "error": type(exc).__name__})
            log.warning("hip4_proactive_cycle_failed", error=type(exc).__name__)
        self._record_loop_summary(summary)
        return summary

    def proactive_loop_status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.hip4_proactive_loop_enabled,
            "configured": self._proactive_loop_configured(),
            "running": self._loop_running,
            "task_active": self._loop_task is not None and not self._loop_task.done(),
            "started_at_ms": self._loop_started_at_ms,
            "last_cycle_at_ms": self._loop_last_cycle_at_ms,
            "last_digest_at_ms": self._loop_last_digest_at_ms,
            "last_reconcile_at_ms": self._loop_last_reconcile_at_ms,
            "cycle_count": self._loop_cycle_count,
            "error_count": self._loop_error_count,
            "last_error": self._loop_last_error,
            "last_summary": self._loop_last_summary,
            "settings": {
                "interval_seconds": self.settings.hip4_proactive_loop_interval_seconds,
                "paper_execution_enabled": self.settings.hip4_proactive_paper_execution_enabled,
                "max_paper_executions_per_cycle": self.settings.hip4_proactive_max_paper_executions_per_cycle,
                "alert_min_edge_usd": str(self.settings.hip4_proactive_alert_min_edge_usd),
                "alert_min_edge_bps": str(self.settings.hip4_proactive_alert_min_edge_bps),
                "reconcile_interval_seconds": self.settings.hip4_proactive_reconcile_interval_seconds,
                "learning_enabled": self.settings.hip4_proactive_learning_enabled,
            },
        }

    def learning_status(self) -> dict[str, Any]:
        recommendations = _learning_recommendations(self._learning)
        return {**self._learning, "recommendations": recommendations}

    async def _proactive_loop(self) -> None:
        while self._loop_running and self._proactive_loop_configured():
            await self.run_proactive_cycle()
            await asyncio.sleep(max(1, self.settings.hip4_proactive_loop_interval_seconds))

    def _proactive_loop_configured(self) -> bool:
        return self.settings.hip4_enabled and self.settings.hip4_proactive_loop_enabled and self.settings.hip4_scan_enabled and self.settings.hip4_mode_allows_scan

    def _proactive_scan_allowed(self) -> bool:
        return self.settings.hip4_enabled and self.settings.hip4_scan_enabled and self.settings.hip4_mode_allows_scan

    def _proactive_paper_configured(self) -> bool:
        return self.settings.hip4_proactive_paper_execution_enabled and self.settings.hip4_paper_execution_enabled and self.settings.hip4_mode_allows_paper

    async def _risk_classify_candidates(self, candidates: list[Hip4Candidate]) -> dict[str, dict[str, Any]]:
        decisions: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            decision = await self.risk.check_candidate(
                candidate,
                capabilities=self.capabilities,
                question=self.registry.questions.get(candidate.question_id) if candidate.question_id is not None else None,
                registry_last_refresh_at_ms=self.registry.last_refresh_at_ms,
            )
            decisions[candidate.candidate_id] = decision.model_dump(mode="json")
        return decisions

    async def _execute_proactive_paper(self, candidates: list[Hip4Candidate], decisions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._proactive_paper_configured():
            return []
        executed: list[dict[str, Any]] = []
        max_count = max(0, self.settings.hip4_proactive_max_paper_executions_per_cycle)
        for candidate in _rank_candidates(candidates, learning=self._learning):
            if len([item for item in executed if item.get("status") == "ok"]) >= max_count:
                break
            if not decisions.get(candidate.candidate_id, {}).get("allowed"):
                continue
            if candidate.candidate_id in self.paper.executed_candidate_ids:
                continue
            try:
                result = await self.execute_paper_candidate(candidate.candidate_id)
                executed.append({"status": "ok", "candidate_id": candidate.candidate_id, "strategy_type": candidate.strategy_type, "result": result})
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                executed.append({"status": "error", "candidate_id": candidate.candidate_id, "strategy_type": candidate.strategy_type, "error": type(exc).__name__})
        return executed

    async def _maybe_reconcile_paper(self, now_ms: int) -> dict[str, Any] | None:
        if not self.settings.hip4_mode_allows_paper or self.settings.hip4_proactive_reconcile_interval_seconds <= 0:
            return None
        interval_ms = self.settings.hip4_proactive_reconcile_interval_seconds * 1000
        if self._loop_last_reconcile_at_ms is not None and now_ms - self._loop_last_reconcile_at_ms < interval_ms:
            return None
        try:
            result = await self.reconcile_paper()
            self._loop_last_reconcile_at_ms = now_ms
            return result
        except PermissionError:
            return None

    async def _maybe_send_proactive_digest(
        self,
        *,
        candidates: list[Hip4Candidate],
        executions: list[dict[str, Any]],
        summary: dict[str, Any],
        now_ms: int,
    ) -> bool:
        alert_candidates = self._alert_worthy_candidates(candidates, now_ms=now_ms)
        interval_ms = max(60, self.settings.hip4_discord_digest_interval_seconds) * 1000
        digest_due = self._loop_last_digest_at_ms is None or now_ms - self._loop_last_digest_at_ms >= interval_ms
        has_execution = any(item.get("status") == "ok" for item in executions)
        if not (digest_due or alert_candidates or has_execution):
            return False
        digest_candidates = alert_candidates or _rank_candidates(candidates)[:5]
        sent = await self.send_discord_digest(candidates=digest_candidates, reason="proactive_cycle", executions=executions, loop=summary)
        if sent:
            self._loop_last_digest_at_ms = now_ms
        return sent

    def _alert_worthy_candidates(self, candidates: list[Hip4Candidate], *, now_ms: int) -> list[Hip4Candidate]:
        self._prune_alert_keys(now_ms)
        worthy: list[Hip4Candidate] = []
        for candidate in _rank_candidates(candidates):
            usd_ok = candidate.expected_net_edge_usd >= self.settings.hip4_proactive_alert_min_edge_usd
            bps_ok = candidate.expected_net_edge_bps >= self.settings.hip4_proactive_alert_min_edge_bps
            threshold_ok = usd_ok or bps_ok if self.settings.hip4_edge_threshold_mode == "either" else usd_ok and bps_ok
            if not threshold_ok:
                continue
            key = _candidate_alert_key(candidate)
            if key in self._loop_alert_keys:
                continue
            self._loop_alert_keys[key] = now_ms
            worthy.append(candidate)
        return worthy

    def _prune_alert_keys(self, now_ms: int) -> None:
        ttl_ms = max(1, self.settings.hip4_proactive_alert_dedupe_seconds) * 1000
        self._loop_alert_keys = {key: seen_ms for key, seen_ms in self._loop_alert_keys.items() if now_ms - seen_ms <= ttl_ms}

    def _update_learning(
        self,
        *,
        candidates: list[Hip4Candidate],
        decisions: dict[str, dict[str, Any]],
        executions: list[dict[str, Any]],
    ) -> None:
        if not self.settings.hip4_proactive_learning_enabled:
            return
        self._learning["cycles"] = int(self._learning.get("cycles") or 0) + 1
        self._learning["candidate_count"] = int(self._learning.get("candidate_count") or 0) + len(candidates)
        reject_codes = self._learning.setdefault("reject_code_counts", {})
        strategy_stats = self._learning.setdefault("strategy_stats", {})
        for reject in self.scanner.last_rejects:
            code = str(reject.get("code") or "unknown")
            reject_codes[code] = int(reject_codes.get(code) or 0) + 1
        for candidate in candidates:
            stats = strategy_stats.setdefault(
                str(candidate.strategy_type),
                {
                    "seen": 0,
                    "allowed": 0,
                    "rejected": 0,
                    "paper_executed": 0,
                    "edge_usd_sum": "0",
                    "realized_pnl_sum": "0",
                    "best_edge_usd": "0",
                    "best_edge_bps": "0",
                },
            )
            stats["seen"] = int(stats.get("seen") or 0) + 1
            stats["edge_usd_sum"] = str(Decimal(str(stats.get("edge_usd_sum") or "0")) + candidate.expected_net_edge_usd)
            if candidate.expected_net_edge_usd > Decimal(str(stats.get("best_edge_usd") or "0")):
                stats["best_edge_usd"] = str(candidate.expected_net_edge_usd)
                stats["best_edge_bps"] = str(candidate.expected_net_edge_bps)
            decision = decisions.get(candidate.candidate_id) or {}
            if decision.get("allowed"):
                stats["allowed"] = int(stats.get("allowed") or 0) + 1
            else:
                stats["rejected"] = int(stats.get("rejected") or 0) + 1
                for violation in decision.get("violations") or []:
                    code = str(violation.get("code") or "unknown")
                    reject_codes[code] = int(reject_codes.get(code) or 0) + 1
        for item in executions:
            if item.get("status") != "ok":
                continue
            candidate = self.candidates.get(str(item.get("candidate_id") or ""))
            if candidate is None:
                continue
            stats = strategy_stats.setdefault(str(candidate.strategy_type), {"seen": 0, "allowed": 0, "rejected": 0, "paper_executed": 0, "edge_usd_sum": "0", "realized_pnl_sum": "0", "best_edge_usd": "0", "best_edge_bps": "0"})
            stats["paper_executed"] = int(stats.get("paper_executed") or 0) + 1
            stats["realized_pnl_sum"] = str(Decimal(str(stats.get("realized_pnl_sum") or "0")) + candidate.expected_net_edge_usd)
        self._learning["last_updated_at_ms"] = int(time.time() * 1000)

    async def _record_loop_audit(self, summary: dict[str, Any]) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_audit_event", None)
        if callable(record):
            await record("hip4_proactive_cycle", actor="hip4", payload=summary)

    def _record_loop_summary(self, summary: dict[str, Any]) -> None:
        self._loop_cycle_count += 1
        self._loop_last_cycle_at_ms = int(summary.get("created_at_ms") or int(time.time() * 1000))
        self._loop_last_summary = summary

    async def manual_ticket(self, candidate_id: str) -> dict[str, Any]:
        if not self.settings.hip4_mode_allows_manual_ticket:
            raise PermissionError("HIP-4 mode does not allow manual tickets")
        if not self.settings.hip4_manual_ticket_export_enabled:
            raise PermissionError("HIP4_MANUAL_TICKET_EXPORT_ENABLED is false")
        if self.capabilities is None:
            raise PermissionError("HIP-4 capability probe has not run")
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError("HIP-4 candidate not found")
        ticket_candidate = candidate.model_copy(update={"mode": "manual_ticket"})
        decision = await self.risk.check_candidate(
            ticket_candidate,
            capabilities=self.capabilities,
            question=self.registry.questions.get(ticket_candidate.question_id) if ticket_candidate.question_id is not None else None,
            registry_last_refresh_at_ms=self.registry.last_refresh_at_ms,
            manual_ticket=True,
        )
        if not decision.allowed:
            raise PermissionError({"risk_decision": decision.model_dump(mode="json")})
        return {
            "candidate_id": candidate_id,
            "non_executable": True,
            "instructions": [
                "Review the HIP-4 candidate and current Hyperliquid UI/order books manually.",
                "If proceeding, independently construct limit orders/native conversions outside this service.",
                "Do not treat this ticket as a signable or postable exchange payload.",
            ],
            "summary": {
                "strategy_type": candidate.strategy_type,
                "outcome_ids": candidate.outcome_ids,
                "question_id": candidate.question_id,
                "size": str(candidate.size),
                "expected_net_edge_usd": str(candidate.expected_net_edge_usd),
                "expected_net_edge_bps": str(candidate.expected_net_edge_bps),
                "coins": [leg.coin for leg in candidate.legs],
            },
            "operator_checklist": [
                "Confirm books are still fresh.",
                "Confirm size and price limits manually.",
                "Confirm no settled/partially-settled market is involved.",
                "Use isolated subaccount and tiny caps if a future approved live workflow exists.",
            ],
            "risk_decision": decision.model_dump(mode="json"),
        }

    async def send_discord_digest(
        self,
        *,
        candidates: list[Hip4Candidate] | None = None,
        reason: str = "digest",
        executions: list[dict[str, Any]] | None = None,
        loop: dict[str, Any] | None = None,
    ) -> bool:
        if not self.settings.hip4_discord_digest_enabled or not self.settings.hip4_alert_channel_configured or self.alert_sink is None:
            return False
        try:
            content = format_hip4_digest(
                status=self.status(),
                capabilities=self.capabilities,
                candidates=candidates or list(self.candidates.values()),
                rejects=self.scanner.last_rejects,
                paper=self.paper.snapshot(),
                reason=reason,
                executions=executions or [],
                loop=loop or self.proactive_loop_status(),
                learning=self.learning_status(),
            )
            await self.alert_sink.send(self.settings.hip4_alert_channel_id, content)
            return True
        except Exception as exc:  # pragma: no cover - Discord runtime behavior
            log.warning("hip4_discord_digest_failed", error=type(exc).__name__)
            return False

    def status(self) -> dict[str, Any]:
        enabled = bool(self.settings.hip4_enabled)
        degraded_reasons: list[str] = []
        status = "ok"
        if not enabled:
            status = "disabled"
            degraded_reasons.append("hip4_disabled")
        else:
            if not self._started:
                degraded_reasons.append("hip4_service_not_started")
            if self._last_error:
                degraded_reasons.append(f"last_error:{self._last_error}")
            if self.capabilities is None:
                degraded_reasons.append("hip4_capability_probe_not_run")
            elif self.capabilities.degraded_reasons:
                degraded_reasons.extend(self.capabilities.degraded_reasons)
            if self.registry.status().get("stale"):
                degraded_reasons.append("registry_stale")
            status = "degraded" if degraded_reasons else "ok"

        capability_status = {
            "capability_probe_implemented": True,
            "registry_implemented": True,
            "market_data_implemented": True,
            "scanner_implemented": True,
            "paper_ledger_implemented": True,
            "manual_ticket_export_registered": self.settings.hip4_manual_ticket_export_enabled,
            "mode_allows_scan": self.settings.hip4_mode_allows_scan,
            "mode_allows_paper": self.settings.hip4_mode_allows_paper,
            "mode_allows_manual_ticket": self.settings.hip4_mode_allows_manual_ticket,
            "config_warnings": self.settings.hip4_config_warnings(),
        }
        if self.capabilities is not None:
            capability_status["probe"] = self.capabilities.model_dump(mode="json")

        return Hip4ServiceStatus(
            enabled=enabled,
            mode=self.settings.hip4_mode,
            status=status,  # type: ignore[arg-type]
            degraded_reasons=degraded_reasons,
            safety=Hip4SafetyPosture(),
            capabilities=capability_status,
            registry=self.registry.status(),
            market_data=self.ws_manager.status(),
            scanner={"last_scan_at_ms": self.scanner.last_scan_at_ms, "candidate_count": len(self.candidates), "rejects": self.scanner.last_rejects[-10:]},
            paper=self.paper.snapshot(),
            proactive_loop=self.proactive_loop_status(),
            learning=self.learning_status(),
        ).model_dump(mode="json")

    def list_outcomes(self) -> list[dict[str, Any]]:
        return self.registry.list_outcomes()

    def list_questions(self) -> list[dict[str, Any]]:
        return self.registry.list_questions()

    def list_books(self) -> list[dict[str, Any]]:
        return [book.model_dump(mode="json") for book in self.ws_manager.books.values()]

    def list_edges(self) -> list[dict[str, Any]]:
        return [candidate.model_dump(mode="json") for candidate in self.candidates.values()]

    async def _update_market_subscriptions(self) -> None:
        coins = self._hot_coins()
        await self.ws_manager.update_hot_subscriptions(coins)

    async def _refresh_books_for_scan(self) -> None:
        coins = self._hot_coins()
        await self.ws_manager.update_hot_subscriptions(coins)
        if self.hip4_client is None:
            return
        for item in coins[: self.settings.hip4_max_hot_outcome_sides]:
            current = self.ws_manager.books.get(item)
            now_ms = int(time.time() * 1000)
            if current is not None and now_ms - current.as_of_ms <= self.settings.hip4_scan_max_book_staleness_ms:
                continue
            try:
                payload = await self.hip4_client.l2_book(item)
            except Exception:
                continue
            book = parse_l2_book(item, payload, source="rest", as_of_ms=now_ms)
            self.ws_manager.books[item] = book
            await self._persist_market_snapshot(book)
        self.ws_manager.mark_stale()

    async def refresh_settlement(self, outcome_id: int) -> dict[str, Any]:
        if self.hip4_client is None:
            raise RuntimeError("HIP-4 client unavailable")
        raw = await self.hip4_client.settled_outcome(outcome_id)
        payload = raw if isinstance(raw, dict) else {"raw": raw}
        settlement = {
            "outcome_id": outcome_id,
            "settle_fraction": payload.get("settleFraction") or payload.get("settle_fraction"),
            "details": payload.get("details") or payload.get("settlement_details"),
            "raw": payload,
            "as_of_ms": int(time.time() * 1000),
        }
        if self.repository is not None and getattr(self.repository, "enabled", False):
            record = getattr(self.repository, "record_hip4_settlement", None)
            if callable(record):
                await record(settlement)
        return settlement

    async def _persist_market_snapshot(self, book: Any) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_hip4_market_snapshot", None)
        if callable(record):
            await record(book.model_dump(mode="json"))

    def _hot_coins(self) -> list[str]:
        allowlisted_outcomes: list[int] = []
        for question_id in self.settings.hip4_question_allowlist_ids:
            question = self.registry.questions.get(question_id)
            if question is not None:
                allowlisted_outcomes.extend(question.outcome_ids)
        all_outcomes = list(self.registry.outcomes.keys())[: self.settings.hip4_max_hot_outcome_sides]
        allowlisted = [coin(outcome_id, side) for outcome_id in allowlisted_outcomes for side in (0, 1)]
        remaining = [coin(outcome_id, side) for outcome_id in all_outcomes for side in (0, 1)]
        return prioritize_hot_coins(allowlisted=allowlisted, active=[], liquid=[], remaining=remaining, budget=self.settings.hip4_ws_max_subscriptions)

    async def _persist_candidates(self, candidates: list[Hip4Candidate]) -> None:
        if self.repository is None or not getattr(self.repository, "enabled", False):
            return
        record = getattr(self.repository, "record_hip4_edge_candidates", None)
        if callable(record):
            await record([candidate.model_dump(mode="json") for candidate in candidates])


def _empty_learning_state() -> dict[str, Any]:
    return {
        "cycles": 0,
        "candidate_count": 0,
        "strategy_stats": {},
        "reject_code_counts": {},
        "last_updated_at_ms": None,
        "policy": "observe_rank_and_recommend_only_no_live_autonomy",
    }


def _rank_candidates(candidates: list[Hip4Candidate], *, learning: dict[str, Any] | None = None) -> list[Hip4Candidate]:
    def _score(candidate: Hip4Candidate) -> Decimal:
        score = candidate.expected_net_edge_usd
        stats = ((learning or {}).get("strategy_stats") or {}).get(str(candidate.strategy_type)) or {}
        executed = Decimal(str(stats.get("paper_executed") or "0"))
        if executed > 0:
            realized = Decimal(str(stats.get("realized_pnl_sum") or "0"))
            score += realized / executed / Decimal("10")
        return score

    return sorted(candidates, key=_score, reverse=True)


def _candidate_summary(candidate: Hip4Candidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "strategy_type": candidate.strategy_type,
        "question_id": candidate.question_id,
        "outcome_ids": candidate.outcome_ids,
        "size": str(candidate.size),
        "expected_net_edge_usd": str(candidate.expected_net_edge_usd),
        "expected_net_edge_bps": str(candidate.expected_net_edge_bps),
        "quote_token": candidate.quote_token,
        "status": candidate.status,
    }


def _candidate_alert_key(candidate: Hip4Candidate) -> str:
    outcomes = ",".join(str(item) for item in sorted(candidate.outcome_ids))
    leg_key = ",".join(sorted(leg.coin for leg in candidate.legs))
    edge_bucket = int(candidate.expected_net_edge_bps // Decimal("10"))
    return f"{candidate.strategy_type}:{candidate.question_id}:{outcomes}:{leg_key}:{edge_bucket}"


def _learning_recommendations(learning: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    reject_counts = dict(learning.get("reject_code_counts") or {})
    if int(reject_counts.get("stale_book") or 0) or int(reject_counts.get("stale_candidate_books") or 0):
        recommendations.append("Observed stale HIP-4 books; increase WebSocket coverage, reduce hot universe size, or keep REST resnapshot cadence tight.")
    if int(reject_counts.get("edge_below_threshold") or 0) > int(learning.get("candidate_count") or 0):
        recommendations.append("Most observed paths are below edge thresholds; keep collecting data before tightening thresholds further.")
    if int(reject_counts.get("quote_token_missing") or 0):
        recommendations.append("Some outcome metadata lacks quoteToken; keep those markets read-only until quote-token support is stable.")
    best_strategy = None
    best_pnl = Decimal("0")
    for strategy, stats in dict(learning.get("strategy_stats") or {}).items():
        realized = Decimal(str(stats.get("realized_pnl_sum") or "0"))
        if realized > best_pnl:
            best_pnl = realized
            best_strategy = strategy
    if best_strategy is not None:
        recommendations.append(f"Best observed paper PnL strategy so far: {best_strategy}; prioritize reviewing those opportunities first.")
    if not recommendations:
        recommendations.append("Continue shadow collection; no safe automatic parameter change is recommended yet.")
    return recommendations
