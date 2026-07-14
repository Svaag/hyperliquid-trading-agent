from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.autonomy.schemas import (
    AlphaEventEvaluation,
    CandidateLesson,
    MemoryObservation,
    OperatorFeedback,
    OperatorOutputLessonMemory,
    RoleLessonMemory,
)
from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.policy import (
    MemoryPolicyEngine,
    default_allowed_contexts,
    default_forbidden_contexts,
)
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.metrics import (
    CANDIDATE_LESSONS_CREATED,
    MEMORY_OBSERVATIONS_CREATED,
    OPERATOR_FEEDBACK_TOTAL,
    ROLE_LESSONS_ACTIVE,
    ROLE_LESSONS_ARCHIVED,
)

log = get_logger(__name__)

ROLE_ORDER = ["analyst", "quant", "research", "risk", "treasury", "execution", "adversary", "judge"]


class MemoryService:
    """Evidence-gated persistent memory pipeline.

    V1 favors fewer scoped memories over broad recall. Strategy/risk/execution/
    capital-affecting lessons can be persisted as needs-human-review but are not
    injected as active strategy mutation.
    """

    def __init__(self, *, settings: Settings, repository: Any = None):
        self.settings = settings
        self.repository = repository
        self.observations: dict[str, MemoryObservation] = {}
        self.candidates: dict[str, CandidateLesson] = {}
        self.shadow_role_lessons: dict[str, RoleLessonMemory] = {}
        self.role_lessons: dict[str, RoleLessonMemory] = {}
        self.operator_lessons: dict[str, OperatorOutputLessonMemory] = {}
        self.policy = MemoryPolicyEngine()
        self.archived_lessons = 0
        self.last_promotion_at_ms: int | None = None
        self.last_error: str | None = None
        self.error_count = 0

    async def load(self) -> None:
        if not self._repo_enabled():
            return
        try:
            for item in await self.repository.list_candidate_lessons(status=None, limit=500):
                self.candidates[item["id"]] = CandidateLesson(**item)
            for item in await self.repository.list_role_lessons(status="active", limit=self.settings.autonomy_memory_role_max_active):
                self.role_lessons[item["id"]] = RoleLessonMemory(**item)
            for item in await self.repository.list_role_lessons(status="shadow", include_shadow=True, limit=500):
                self.shadow_role_lessons[item["id"]] = RoleLessonMemory(**item)
            for item in await self.repository.list_operator_output_lessons(status="active", limit=self.settings.autonomy_memory_operator_max_active):
                self.operator_lessons[item["id"]] = OperatorOutputLessonMemory(**item)
        except Exception as exc:  # pragma: no cover
            self._record_error(exc)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.autonomy_memory_enabled,
            "effective_enabled": self.settings.autonomy_memory_effective_enabled,
            "observations": len(self.observations),
            "candidate_lessons": len([item for item in self.candidates.values() if item.status == "candidate"]),
            "shadow_lessons": len(self.shadow_role_lessons),
            "active_role_lessons": len([item for item in self.role_lessons.values() if item.validation_status == "active"]),
            "active_operator_lessons": len([item for item in self.operator_lessons.values() if item.validation_status == "active"]),
            "archived_lessons": self.archived_lessons,
            "last_promotion_at_ms": self.last_promotion_at_ms,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }

    async def observe_event_evaluation(self, evaluation: AlphaEventEvaluation) -> list[CandidateLesson]:
        if not self.settings.autonomy_memory_enabled:
            return []
        observation = _observation_from_event_evaluation(evaluation)
        await self.record_observation(observation)
        candidates: list[CandidateLesson] = []
        for candidate in _candidates_from_event_evaluation(evaluation, observation, self.settings):
            candidates.append(await self.upsert_candidate(candidate))
        promoted = await self.promote_candidates()
        return [*candidates, *promoted]

    async def record_feedback(self, feedback: OperatorFeedback) -> CandidateLesson | None:
        OPERATOR_FEEDBACK_TOTAL.labels(target_type=feedback.target_type, rating=feedback.rating).inc()
        if self._repo_enabled():
            await self.repository.record_operator_feedback(feedback.model_dump(mode="json"))
            await self.repository.record_autonomy_event("operator_feedback_recorded", actor="autonomy_memory", payload={"target_type": feedback.target_type, "target_id": feedback.target_id, "rating": feedback.rating, "exchange_actions": []})
        observation = MemoryObservation(
            id=f"obs_{uuid4().hex}",
            source_type="operator_feedback",
            source_id=feedback.id,
            observation=f"Operator marked {feedback.target_type} {feedback.target_id} as {feedback.rating}. {feedback.note}".strip(),
            evidence=[feedback.model_dump(mode="json")],
            severity="warning" if feedback.rating in {"bad", "wrong", "unclear", "too_noisy"} else "info",
            created_at_ms=feedback.created_at_ms,
            metadata={"exchange_actions": []},
        )
        await self.record_observation(observation)
        if feedback.target_type in {"bot", "discord_message", "report"}:
            candidate = CandidateLesson(
                id=f"cand_{uuid4().hex}",
                lesson_type="operator_output",
                role=None,
                scope={"target_type": feedback.target_type, "rating": feedback.rating},
                claim=_operator_feedback_claim(feedback),
                evidence=[feedback.model_dump(mode="json")],
                source_observation_ids=[observation.id],
                sample_size=1,
                confidence=0.65 if feedback.rating in {"bad", "wrong", "good", "useful"} else 0.55,
                expected_future_behavior_change=_operator_feedback_instruction(feedback),
                status="candidate",
                created_at_ms=feedback.created_at_ms,
                expires_at_ms=feedback.created_at_ms + self.settings.autonomy_memory_candidate_ttl_days * 86_400_000,
                metadata={"exchange_actions": []},
            )
            stored = await self.upsert_candidate(candidate)
            await self.promote_candidates()
            return stored
        return None

    async def record_observation(self, observation: MemoryObservation) -> None:
        self.observations[observation.id] = observation
        MEMORY_OBSERVATIONS_CREATED.labels(source_type=observation.source_type, severity=observation.severity).inc()
        if self._repo_enabled():
            await self.repository.record_memory_observation(observation.model_dump(mode="json"))
            await self.repository.record_autonomy_event("memory_observation_created", actor="autonomy_memory", symbol=observation.symbol, payload={"observation_id": observation.id, "source_type": observation.source_type, "exchange_actions": []})

    async def upsert_candidate(self, candidate: CandidateLesson) -> CandidateLesson:
        existing = self._find_matching_candidate(candidate)
        if existing is not None:
            merged = existing.model_copy(
                update={
                    "sample_size": existing.sample_size + candidate.sample_size,
                    "confidence": min(0.95, max(existing.confidence, candidate.confidence) + 0.03),
                    "evidence": [*existing.evidence, *candidate.evidence][-20:],
                    "source_observation_ids": _dedupe([*existing.source_observation_ids, *candidate.source_observation_ids]),
                    "source_run_ids": _dedupe([*existing.source_run_ids, *candidate.source_run_ids]),
                    "expires_at_ms": max(existing.expires_at_ms, candidate.expires_at_ms),
                }
            )
            self.candidates[merged.id] = merged
            if self._repo_enabled():
                await self.repository.upsert_candidate_lesson(merged.model_dump(mode="json"))
            return merged
        self.candidates[candidate.id] = candidate
        CANDIDATE_LESSONS_CREATED.labels(lesson_type=candidate.lesson_type, role=candidate.role or "operator").inc()
        if self._repo_enabled():
            await self.repository.upsert_candidate_lesson(candidate.model_dump(mode="json"))
            await self.repository.record_autonomy_event("candidate_lesson_created", actor="autonomy_memory", symbol=candidate.scope.get("symbol"), payload={"candidate_id": candidate.id, "lesson_type": candidate.lesson_type, "exchange_actions": []})
        return candidate

    async def promote_candidates(self, *, now_ms: int | None = None) -> list[CandidateLesson]:
        now_ms = now_ms or _now_ms()
        promoted: list[CandidateLesson] = []
        for candidate in list(self.candidates.values()):
            if candidate.status not in {"candidate", "shadow"}:
                continue
            if candidate.expires_at_ms <= now_ms:
                expired = candidate.model_copy(update={"status": "expired"})
                self.candidates[expired.id] = expired
                if self._repo_enabled():
                    await self.repository.set_candidate_lesson_status(expired.id, "expired")
                continue
            if _should_shadow(candidate):
                shadow_lesson = self._role_lesson_from_candidate(candidate, status="shadow", now_ms=now_ms)
                if shadow_lesson is not None and shadow_lesson.id not in self.shadow_role_lessons:
                    self.shadow_role_lessons[shadow_lesson.id] = shadow_lesson
                    await self._persist_shadow_lesson(shadow_lesson)
                    candidate = candidate.model_copy(update={"status": "shadow"})
                    self.candidates[candidate.id] = candidate
                    if self._repo_enabled():
                        await self.repository.set_candidate_lesson_status(candidate.id, "shadow")
            if self._can_promote(candidate):
                if candidate.lesson_type == "operator_output":
                    operator_lesson = OperatorOutputLessonMemory(
                        id=f"opmem_{uuid4().hex}",
                        scope=candidate.scope,
                        issue_or_pattern=candidate.claim,
                        preferred_behavior=candidate.expected_future_behavior_change,
                        bad_examples=[item for item in candidate.evidence if candidate.scope.get("rating") in {"bad", "wrong", "unclear", "too_noisy"}],
                        good_examples=[item for item in candidate.evidence if candidate.scope.get("rating") in {"good", "useful"}],
                        confidence=candidate.confidence,
                        sample_size=candidate.sample_size,
                        validation_status="active",
                        created_at_ms=now_ms,
                        expires_at_ms=now_ms + self.settings.autonomy_memory_process_ttl_days * 86_400_000,
                        metadata={"source_candidate_id": candidate.id, "exchange_actions": []},
                    )
                    self.operator_lessons[operator_lesson.id] = operator_lesson
                    await self._persist_operator_lesson(operator_lesson)
                else:
                    status = "needs_human_review" if _requires_human_review(candidate) else "active"
                    role_lesson = self._role_lesson_from_candidate(candidate, status=status, now_ms=now_ms)
                    if role_lesson is not None:
                        self.role_lessons[role_lesson.id] = role_lesson
                        ROLE_LESSONS_ACTIVE.labels(role=role_lesson.role).set(len([item for item in self.role_lessons.values() if item.role == role_lesson.role and item.validation_status == "active"]))
                        await self._persist_role_lesson(role_lesson)
                candidate = candidate.model_copy(update={"status": "promoted"})
                self.candidates[candidate.id] = candidate
                if self._repo_enabled():
                    await self.repository.set_candidate_lesson_status(candidate.id, "promoted")
                promoted.append(candidate)
        if promoted:
            self.last_promotion_at_ms = now_ms
        return promoted

    async def retrieve(
        self,
        *,
        role: str | None = None,
        symbol: str | None = None,
        signal_type: str | None = None,
        market_regime: str | None = None,
        max_items: int = 8,
    ) -> list[RoleLessonMemory]:
        lessons = list(self.role_lessons.values())
        if self._repo_enabled() and not lessons:
            lessons = [RoleLessonMemory(**item) for item in await self.repository.list_role_lessons(role=role, status="active", limit=self.settings.autonomy_memory_role_max_active)]
        scored: list[tuple[int, RoleLessonMemory]] = []
        for lesson in lessons:
            if lesson.validation_status != "active":
                continue
            if role and lesson.role != role:
                continue
            score = _scope_score(lesson.scope, symbol=symbol, signal_type=signal_type, market_regime=market_regime)
            scored.append((score, lesson))
        scored.sort(key=lambda item: (item[0], item[1].confidence, item[1].sample_size, item[1].created_at_ms), reverse=True)
        return [item for _, item in scored[:max_items]]

    async def operator_memory(self, max_items: int = 3) -> list[OperatorOutputLessonMemory]:
        lessons = list(self.operator_lessons.values())
        if self._repo_enabled() and not lessons:
            lessons = [OperatorOutputLessonMemory(**item) for item in await self.repository.list_operator_output_lessons(status="active", limit=self.settings.autonomy_memory_operator_max_active)]
        lessons = [item for item in lessons if item.validation_status == "active"]
        lessons.sort(key=lambda item: (item.confidence, item.sample_size, item.created_at_ms), reverse=True)
        return lessons[:max_items]

    async def memory_block_for_role(
        self,
        role: str,
        *,
        symbol: str | None = None,
        signal_type: str | None = None,
        market_regime: str | None = None,
        max_items: int = 5,
        run_id: str | None = None,
        context_type: str | None = None,
    ) -> str:
        lessons = await self.retrieve(role=role, symbol=symbol, signal_type=signal_type, market_regime=market_regime, max_items=max_items)
        decisions = [self.policy.can_inject(lesson, role=role, context_type=context_type, mode="paper") for lesson in lessons]
        allowed_ids = {decision.memory_id for decision in decisions if decision.allowed and _role_prompt_list_allows(decision.status, role, self.settings)}
        blocked_ids = [decision.memory_id for decision in decisions if decision.memory_id not in allowed_ids]
        allowed_lessons = [lesson for lesson in lessons if lesson.id in allowed_ids]
        await self._record_memory_injection_event(
            run_id=run_id,
            role=role,
            context_type=context_type or self.policy.context_for_role(role),
            allowed_ids=[lesson.id for lesson in allowed_lessons],
            blocked_ids=blocked_ids,
            decisions=[decision.model_dump() for decision in decisions],
        )
        if not allowed_lessons:
            return ""
        lines = ["Relevant validated role memories (advisory context only):"]
        for lesson in allowed_lessons:
            scope = ",".join(f"{key}={value}" for key, value in lesson.scope.items() if value)
            lines.append(f"- [{lesson.role}:{scope or 'general'}] {lesson.instruction} confidence={lesson.confidence:.2f} sample={lesson.sample_size} expires_at_ms={lesson.expires_at_ms}")
        return "\n".join(lines)[:1500]

    async def archive_expired(self, *, now_ms: int | None = None) -> int:
        now_ms = now_ms or _now_ms()
        archived = 0
        for lesson in list(self.role_lessons.values()):
            if lesson.validation_status == "active" and lesson.expires_at_ms <= now_ms:
                updated = lesson.model_copy(update={"validation_status": "expired"})
                self.role_lessons[lesson.id] = updated
                await self._persist_role_lesson(updated)
                ROLE_LESSONS_ARCHIVED.inc()
                archived += 1
        for operator_lesson in list(self.operator_lessons.values()):
            if operator_lesson.validation_status == "active" and operator_lesson.expires_at_ms <= now_ms:
                updated_operator = operator_lesson.model_copy(update={"validation_status": "expired"})
                self.operator_lessons[operator_lesson.id] = updated_operator
                await self._persist_operator_lesson(updated_operator)
                archived += 1
        self.archived_lessons += archived
        return archived

    async def list_observations(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return await self.repository.list_memory_observations(limit=limit)
        return [item.model_dump(mode="json") for item in sorted(self.observations.values(), key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def list_candidates(self, status: str | None = None, role: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return await self.repository.list_candidate_lessons(status=status, role=role, limit=limit)
        items = list(self.candidates.values())
        if status:
            items = [item for item in items if item.status == status]
        if role:
            items = [item for item in items if item.role == role]
        return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def list_lessons(self, role: str | None = None, status: str | None = "active", include_shadow: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if self._repo_enabled():
            return await self.repository.list_role_lessons(role=role, status=status, include_shadow=include_shadow, limit=limit)
        source = self.shadow_role_lessons if include_shadow else self.role_lessons
        items = list(source.values())
        if role:
            items = [item for item in items if item.role == role]
        if status:
            items = [item for item in items if item.validation_status == status]
        return [item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.created_at_ms, reverse=True)[:limit]]

    async def get_lesson(self, lesson_id: str, include_shadow: bool = True) -> dict[str, Any] | None:
        if lesson_id in self.role_lessons:
            return self.role_lessons[lesson_id].model_dump(mode="json")
        if include_shadow and lesson_id in self.shadow_role_lessons:
            return self.shadow_role_lessons[lesson_id].model_dump(mode="json")
        if self._repo_enabled():
            return await self.repository.get_role_lesson(lesson_id, include_shadow=include_shadow)
        return None

    async def reject_candidate(self, candidate_id: str) -> None:
        candidate = self.candidates.get(candidate_id)
        if candidate is not None:
            self.candidates[candidate_id] = candidate.model_copy(update={"status": "rejected"})
        if self._repo_enabled():
            await self.repository.set_candidate_lesson_status(candidate_id, "rejected")

    async def promote_candidate_to_shadow(self, candidate_id: str) -> RoleLessonMemory | None:
        candidate = self.candidates.get(candidate_id)
        if candidate is None and self._repo_enabled():
            data = await self.repository.get_candidate_lesson(candidate_id)
            candidate = CandidateLesson(**data) if data else None
        if candidate is None:
            return None
        lesson = self._role_lesson_from_candidate(candidate, status="shadow", now_ms=_now_ms())
        if lesson is None:
            return None
        self.shadow_role_lessons[lesson.id] = lesson
        await self._persist_shadow_lesson(lesson)
        if self._repo_enabled():
            await self.repository.set_candidate_lesson_status(candidate_id, "shadow")
        return lesson

    async def promote_candidate_to_active(
        self,
        candidate_id: str,
        *,
        human_review_confirmed: bool = False,
        change_control_id: str = "",
        approved_for_role_injection_roles: list[str] | None = None,
        reviewer: str = "",
    ) -> RoleLessonMemory | None:
        candidate = self.candidates.get(candidate_id)
        if candidate is None and self._repo_enabled():
            data = await self.repository.get_candidate_lesson(candidate_id)
            candidate = CandidateLesson(**data) if data else None
        if candidate is None:
            return None
        target_role = candidate.role or _default_role_for_lesson(candidate.lesson_type)
        gated_role = target_role in {"risk", "execution", "treasury"}
        if _requires_human_review(candidate) and not human_review_confirmed:
            raise PermissionError("human_review_confirmed required for strategy/risk/execution/capital-affecting memory")
        if gated_role and self.settings.autonomy_memory_require_change_control_for_risk_execution and not change_control_id:
            raise PermissionError("change_control_id required before risk/execution/treasury memory can become injectable")
        status = "active"
        metadata = {
            "memory_status": "approved_policy",
            "change_control_id": change_control_id,
            "approved_for_role_injection_roles": approved_for_role_injection_roles or ([target_role] if target_role else []),
            "reviewer": reviewer,
            "promotion_decision": "manual_candidate_promotion",
        }
        lesson = self._role_lesson_from_candidate(candidate, status=status, now_ms=_now_ms(), metadata=metadata)
        if lesson is None:
            return None
        self.role_lessons[lesson.id] = lesson
        await self._persist_role_lesson(lesson)
        if self._repo_enabled():
            await self.repository.set_candidate_lesson_status(candidate_id, "promoted")
        return lesson

    async def archive_lesson(self, lesson_id: str) -> None:
        lesson = self.role_lessons.get(lesson_id)
        if lesson is not None:
            self.role_lessons[lesson_id] = lesson.model_copy(update={"validation_status": "archived"})
        if self._repo_enabled():
            await self.repository.archive_role_lesson(lesson_id)
        self.archived_lessons += 1

    def _find_matching_candidate(self, candidate: CandidateLesson) -> CandidateLesson | None:
        for existing in self.candidates.values():
            if existing.status not in {"candidate", "shadow"}:
                continue
            if existing.lesson_type == candidate.lesson_type and existing.role == candidate.role and existing.claim == candidate.claim and existing.scope == candidate.scope:
                return existing
        return None

    def _can_promote(self, candidate: CandidateLesson) -> bool:
        if candidate.lesson_type == "operator_output":
            return candidate.sample_size >= self.settings.autonomy_operator_lesson_min_samples and candidate.confidence >= self.settings.autonomy_lesson_min_confidence
        if _requires_human_review(candidate):
            return candidate.sample_size >= self.settings.autonomy_role_lesson_min_samples and candidate.confidence >= self.settings.autonomy_strategy_lesson_min_confidence
        return candidate.sample_size >= self.settings.autonomy_role_lesson_min_samples and candidate.confidence >= self.settings.autonomy_lesson_min_confidence

    def _role_lesson_from_candidate(self, candidate: CandidateLesson, *, status: str, now_ms: int, metadata: dict[str, Any] | None = None) -> RoleLessonMemory | None:
        role = candidate.role or _default_role_for_lesson(candidate.lesson_type)
        if role not in ROLE_ORDER:
            role = "judge"
        ttl_days = self.settings.autonomy_memory_incident_ttl_days if candidate.lesson_type == "incident_warning" else self.settings.autonomy_memory_role_ttl_days
        metadata = {**(metadata or {})}
        memory_status = _memory_status_for_lesson_status(status, metadata)
        allowed_contexts = default_allowed_contexts(memory_status, role=role, metadata=metadata)
        forbidden_contexts = default_forbidden_contexts(memory_status)
        promotion_history = []
        if metadata.get("change_control_id") or metadata.get("reviewer"):
            promotion_history.append(
                {
                    "reviewer": metadata.get("reviewer"),
                    "change_control_id": metadata.get("change_control_id"),
                    "memory_status": memory_status,
                    "created_at_ms": now_ms,
                }
            )
        return RoleLessonMemory(
            id=f"mem_{uuid4().hex}",
            role=role,  # type: ignore[arg-type]
            lesson_type=candidate.lesson_type,
            scope=candidate.scope,
            claim=candidate.claim,
            instruction=candidate.expected_future_behavior_change or candidate.claim,
            evidence=candidate.evidence,
            source_candidate_id=candidate.id,
            source_run_ids=candidate.source_run_ids,
            confidence=candidate.confidence,
            sample_size=candidate.sample_size,
            counterexamples=candidate.counterexamples,
            validation_status=status,  # type: ignore[arg-type]
            strategy_affecting=candidate.strategy_affecting,
            risk_affecting=candidate.risk_affecting,
            execution_affecting=candidate.execution_affecting,
            capital_allocation_affecting=candidate.capital_allocation_affecting,
            created_at_ms=now_ms,
            activated_at_ms=now_ms if status == "active" else None,
            expires_at_ms=now_ms + ttl_days * 86_400_000,
            memory_status=memory_status,  # type: ignore[arg-type]
            allowed_contexts=allowed_contexts,
            forbidden_contexts=forbidden_contexts,
            promotion_history=promotion_history,
            rollback_target=metadata.get("rollback_target"),
            metadata={"source": "evidence_gated_memory_pipeline", **metadata, "memory_status": memory_status, "exchange_actions": []},
        )

    async def _persist_shadow_lesson(self, lesson: RoleLessonMemory) -> None:
        if self._repo_enabled():
            await self.repository.upsert_shadow_role_lesson(lesson.model_dump(mode="json"))
            await self.repository.record_autonomy_event("shadow_lesson_created", actor="autonomy_memory", symbol=lesson.scope.get("symbol"), payload={"lesson_id": lesson.id, "role": lesson.role, "exchange_actions": []})

    async def _persist_role_lesson(self, lesson: RoleLessonMemory) -> None:
        if self._repo_enabled():
            await self.repository.upsert_role_lesson(lesson.model_dump(mode="json"))
            await self.repository.record_autonomy_event("role_lesson_promoted", actor="autonomy_memory", symbol=lesson.scope.get("symbol"), payload={"lesson_id": lesson.id, "role": lesson.role, "validation_status": lesson.validation_status, "exchange_actions": []})

    async def _persist_operator_lesson(self, lesson: OperatorOutputLessonMemory) -> None:
        if self._repo_enabled():
            await self.repository.upsert_operator_output_lesson(lesson.model_dump(mode="json"))
            await self.repository.record_autonomy_event("operator_output_lesson_promoted", actor="autonomy_memory", payload={"lesson_id": lesson.id, "exchange_actions": []})

    async def _record_memory_injection_event(
        self,
        *,
        run_id: str | None,
        role: str,
        context_type: str,
        allowed_ids: list[str],
        blocked_ids: list[str],
        decisions: list[dict[str, Any]],
    ) -> None:
        if not self._repo_enabled():
            return
        record = getattr(self.repository, "record_memory_injection_event", None)
        if not callable(record):
            return
        await record(
            {
                "run_id": run_id,
                "role": role,
                "context_type": context_type,
                "memory_ids": allowed_ids,
                "blocked_memory_ids": blocked_ids,
                "policy_decision": {"decisions": decisions},
                "created_at_ms": _now_ms(),
                "metadata": {"exchange_actions": []},
            }
        )

    def _repo_enabled(self) -> bool:
        return self.repository is not None and getattr(self.repository, "enabled", False)

    def _record_error(self, exc: Exception) -> None:
        self.error_count += 1
        self.last_error = type(exc).__name__
        log.warning("memory_service_error", error=type(exc).__name__)


def _observation_from_event_evaluation(evaluation: AlphaEventEvaluation) -> MemoryObservation:
    bps_text = "n/a" if evaluation.realized_or_marked_bps is None else f"{evaluation.realized_or_marked_bps:.1f} bps"
    return MemoryObservation(
        id=f"obs_{uuid4().hex}",
        source_type="event_evaluation",
        source_id=evaluation.id,
        role="research",
        symbol=evaluation.symbol,
        signal_type=evaluation.event_type,
        market_regime=evaluation.market_regime,
        observation=f"{evaluation.symbol} {evaluation.event_type} catalyst from {evaluation.event_source} completed as {evaluation.terminal_outcome}; marked move {bps_text}; max favorable={evaluation.max_favorable_bps}, max adverse={evaluation.max_adverse_bps}, max abs={evaluation.max_abs_move_bps}.",
        evidence=[evaluation.model_dump(mode="json", exclude={"marks"})],
        severity="warning" if evaluation.terminal_outcome == "failed" else "info",
        created_at_ms=_now_ms(),
        metadata={"exchange_actions": []},
    )


def _candidates_from_event_evaluation(evaluation: AlphaEventEvaluation, observation: MemoryObservation, settings: Settings) -> list[CandidateLesson]:
    candidates: list[CandidateLesson] = []
    now_ms = _now_ms()
    base: dict[str, Any] = {
        "scope": {
            "symbol": evaluation.symbol,
            "event_type": evaluation.event_type,
            "source": evaluation.event_source,
            "asset_class": evaluation.asset_class,
            "sentiment": evaluation.sentiment,
            "direction": evaluation.direction,
            "market_regime": evaluation.market_regime,
        },
        "evidence": observation.evidence,
        "source_observation_ids": [observation.id],
        "sample_size": 1,
        "created_at_ms": now_ms,
        "expires_at_ms": now_ms + settings.autonomy_memory_candidate_ttl_days * 86_400_000,
        "metadata": {"event_id": evaluation.event_id, "event_evaluation_id": evaluation.id, "exchange_actions": []},
    }
    if evaluation.terminal_outcome == "worked":
        candidates.append(
            CandidateLesson(
                id=f"cand_{uuid4().hex}",
                lesson_type="catalyst_quality",
                role="research",
                claim=f"{evaluation.event_source} {evaluation.event_type} {evaluation.sentiment} catalysts showed directional follow-through for {evaluation.symbol} in this scope.",
                confidence=0.58,
                expected_future_behavior_change="When this catalyst scope repeats, preserve it as advisory research evidence but require normal engine and risk confirmation.",
                **base,
            )
        )
    elif evaluation.terminal_outcome == "failed":
        candidates.append(
            CandidateLesson(
                id=f"cand_{uuid4().hex}",
                lesson_type="catalyst_quality",
                role="adversary",
                claim=f"{evaluation.event_source} {evaluation.event_type} {evaluation.sentiment} catalysts failed to follow through for {evaluation.symbol} in this scope.",
                confidence=0.60,
                expected_future_behavior_change="Challenge similar catalysts in research review unless price/orderflow confirmation appears; do not relax thresholds from this evidence alone.",
                **base,
            )
        )
    elif evaluation.terminal_outcome == "volatility_only":
        candidates.append(
            CandidateLesson(
                id=f"cand_{uuid4().hex}",
                lesson_type="catalyst_quality",
                role="research",
                claim=f"{evaluation.event_source} {evaluation.event_type} catalysts generated volatility but weak direction for {evaluation.symbol}.",
                confidence=0.55,
                expected_future_behavior_change="Treat similar events as volatility catalysts, not standalone directional edge, until follow-through evidence accumulates.",
                **base,
            )
        )
    return candidates


def _operator_feedback_claim(feedback: OperatorFeedback) -> str:
    if feedback.rating in {"good", "useful"}:
        return "Operator found this output/action format useful."
    if feedback.rating == "too_noisy":
        return "Operator flagged output as too noisy."
    if feedback.rating == "unclear":
        return "Operator flagged output as unclear."
    return "Operator flagged output as low quality or wrong."


def _operator_feedback_instruction(feedback: OperatorFeedback) -> str:
    note = f" Specific note: {feedback.note}" if feedback.note else ""
    if feedback.rating in {"good", "useful"}:
        return f"Reuse the concise structure that made this output actionable.{note}"
    if feedback.rating == "too_noisy":
        return f"Reduce repeated boilerplate and lead with actionable deltas.{note}"
    if feedback.rating == "unclear":
        return f"Use clearer labels, exact IDs, risk context, and next commands.{note}"
    return f"Avoid repeating the flagged pattern; include evidence and distinguish fact from inference.{note}"


def _requires_human_review(candidate: CandidateLesson) -> bool:
    return candidate.strategy_affecting or candidate.risk_affecting or candidate.execution_affecting or candidate.capital_allocation_affecting


def _should_shadow(candidate: CandidateLesson) -> bool:
    return candidate.confidence >= 0.50 and candidate.status == "candidate"


def _default_role_for_lesson(lesson_type: str) -> str:
    return {
        "risk_discipline": "risk",
        "operator_output": "judge",
        "data_quality": "research",
        "incident_warning": "adversary",
        "catalyst_quality": "research",
    }.get(lesson_type, "analyst")


def _role_memory_injection_allowed(role: str, settings: Settings) -> bool:
    return role.lower().strip().replace("-", "_") in set(settings.autonomy_memory_prompt_role_list)


def _role_prompt_list_allows(status: str, role: str, settings: Settings) -> bool:
    role_key = role.lower().strip().replace("-", "_")
    if role_key in set(settings.autonomy_memory_prompt_role_list):
        return True
    # Explicitly approved policy memories may enter their approved role even if
    # the broad prompt-role list excludes that role.
    return status == "approved_policy"


def _lesson_injection_allowed_for_role(lesson: RoleLessonMemory, role: str, settings: Settings) -> bool:
    policy = MemoryPolicyEngine()
    decision = policy.can_inject(lesson, role=role, mode="paper")
    return decision.allowed and _role_prompt_list_allows(decision.status, role, settings)


def _memory_status_for_lesson_status(status: str, metadata: dict[str, Any]) -> str:
    explicit = metadata.get("memory_status")
    if explicit in {"candidate", "validated_advisory", "approved_policy", "deprecated", "reverted"}:
        return str(explicit)
    if status == "shadow":
        return "validated_advisory"
    if status == "active" and metadata.get("change_control_id"):
        return "approved_policy"
    if status == "active":
        return "validated_advisory"
    if status in {"archived", "expired", "rejected"}:
        return "deprecated"
    return "candidate"


def _scope_score(scope: dict[str, Any], *, symbol: str | None, signal_type: str | None, market_regime: str | None) -> int:
    score = 0
    if symbol and str(scope.get("symbol") or "").upper() == symbol.upper():
        score += 4
    if signal_type and scope.get("signal_type") == signal_type:
        score += 3
    if market_regime and scope.get("market_regime") == market_regime:
        score += 2
    return score


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)
