from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from hyperliquid_trading_agent.app.config import Settings

_PAYLOAD_SCHEMA = "hyperliquid.wave_lhp.v1"
_TEXT_LIMIT = 1200
_TOKEN_ALLOWED = {"_", "-", ":", ".", "/"}
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\b(password|passwd|secret|token|credential|api[-_\s]?key)\s*[:=]\s*[^\s,;]+", re.I),
)


def stable_hash(value: Any, *, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def render_wave_handoff_payload(
    *,
    settings: Settings,
    run_id: str,
    classification: dict[str, Any],
    readiness: dict[str, Any],
    replay: dict[str, Any] | None,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Render a bounded LHP-compatible payload for wave blocker/promotion work.

    This intentionally does not mutate deployment config. It creates the artifact
    that an escalation transport (GitHub issue / NOC CaseService) can carry into
    the Engineering Loop, where any code/config change still ends as a draft PR.
    """

    state = str(classification.get("state") or "unknown")
    objective_key = str(classification.get("objective_key") or _objective_key_for_state(state))
    blockers = list(classification.get("blockers") or [])[:30]
    blocker_codes = [str(item.get("code") or "unknown") for item in blockers]
    handoff_id = "hwave_" + stable_hash(
        {
            "objective_key": objective_key,
            "state": state,
            "codes": blocker_codes,
            "wave1c_enabled": settings.engine_wave1c_enabled,
            "wave2_enabled": settings.engine_wave2_enabled,
        },
        length=12,
    )
    case_id = "hcase_" + stable_hash({"objective_key": objective_key, "repository": settings.orchestration_wave_supervisor_handoff_repo}, length=12)
    objective = _objective_for_state(state)
    now_ms = int(time.time() * 1000)
    evidence = _evidence_from_readiness(readiness, replay)
    payload = {
        "schema_version": _PAYLOAD_SCHEMA,
        "lhp_compatibility": {
            "protocol": "lhp.v1-inspired",
            "transport_neutral": True,
            "bounded_payload": True,
            "promotion_policy": "draft_pr_and_human_or_signed_deploy_gate_only",
        },
        "case": {
            "case_id": case_id,
            "case_type": "hyperliquid_wave_orchestration",
            "title": objective,
            "status": "requested",
            "severity": _severity_for_state(state),
            "resource_id": "hyperliquid-trading-agent:engine:waves",
            "created_at_ms": now_ms,
        },
        "handoff": {
            "handoff_id": handoff_id,
            "case_id": case_id,
            "source_loop": "hyperliquid-wave-supervisor",
            "target_loop": "engineering",
            "objective": objective,
            "objective_key": objective_key,
            "status": "requested",
            "idempotency_key": f"{case_id}:engineering:{objective_key}:v1",
            "correlation_id": run_id,
            "trace_id": run_id,
            "repository": settings.orchestration_wave_supervisor_handoff_repo,
            "constraints": [
                "do_not_enable_live_execution",
                "do_not_enable_engine_wave2",
                "do_not_bypass_risk_gateway_or_council",
                "do_not_directly_mutate_runtime_config_from_supervisor",
                "promotion_must_end_as_draft_pr_or_signed_operator_change",
                "preserve_replayability_explainability_and_outcome_attribution",
            ],
            "acceptance_criteria": _acceptance_criteria_for_state(state),
            "resource": {
                "service": "hyperliquid-trading-agent",
                "engine_mode": settings.engine_mode,
                "wave1c_enabled": settings.engine_wave1c_enabled,
                "wave2_enabled": settings.engine_wave2_enabled,
                "public_execution_authority": "none",
            },
        },
        "verification_objectives": _verification_objectives_for_state(case_id, handoff_id, state),
        "evidence": evidence,
        "blockers": [_sanitize_mapping(item) for item in blockers],
        "readiness_summary": _readiness_summary(readiness),
        "latest_replay": _replay_summary(replay),
        "actions": [_sanitize_mapping(item) for item in actions[:20]],
        "created_at_ms": now_ms,
    }
    payload["payload_hash"] = payload_hash(payload)
    return payload


def render_engineering_issue_body(payload: dict[str, Any]) -> str:
    handoff = payload.get("handoff") if isinstance(payload.get("handoff"), dict) else {}
    case = payload.get("case") if isinstance(payload.get("case"), dict) else {}
    objective = sanitize_text(handoff.get("objective") or case.get("title") or "Hyperliquid wave orchestration task")
    handoff_id = sanitize_token(handoff.get("handoff_id"))
    case_id = sanitize_token(handoff.get("case_id") or case.get("case_id"))
    criteria = handoff.get("acceptance_criteria") if isinstance(handoff.get("acceptance_criteria"), list) else []
    constraints = handoff.get("constraints") if isinstance(handoff.get("constraints"), list) else []
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    return "\n".join(
        [
            f"# {objective}",
            "",
            "This issue was generated by the Hyperliquid Wave Supervisor.",
            "It is a candidate handoff only. The Engineering Loop may prepare a draft PR after human/policy promotion to `loop:approved`; deployment remains a separate approval gate.",
            "",
            f"hyperliquid-wave-handoff-id:{handoff_id}",
            f"hyperliquid-wave-case-id:{case_id}",
            f"hyperliquid-wave-payload-hash:{sanitize_token(payload.get('payload_hash'), limit=80)}",
            "",
            "## Constraints",
            *(f"- {sanitize_text(item)}" for item in constraints),
            "",
            "## Acceptance criteria",
            *(f"- {sanitize_text(item)}" for item in criteria),
            "",
            "## Current blockers",
            *(f"- `{sanitize_token(item.get('code') if isinstance(item, dict) else 'unknown')}` — {sanitize_text((item or {}).get('detail') if isinstance(item, dict) else item)}" for item in blockers[:20]),
            "",
            "## Bounded payload",
            "```json",
            json.dumps(_json_safe(payload), indent=2, sort_keys=True),
            "```",
        ]
    )


def sanitize_token(value: Any, *, limit: int = 160) -> str:
    text = str(value or "")
    rendered = "".join(ch for ch in text if ch.isalnum() or ch in _TOKEN_ALLOWED)
    return (rendered or "unknown")[: max(1, limit)]


def sanitize_text(value: Any, *, limit: int = _TEXT_LIMIT) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = "".join(" " if ch in "`<>[]{}" or ord(ch) < 32 else ch for ch in text)
    return (" ".join(text.split()) or "—")[: max(1, limit)]


def _objective_key_for_state(state: str) -> str:
    if state == "wave1c_promotion_candidate":
        return "enable-wave1c-controlled-canary-v1"
    if state == "collecting_wave1a_evidence":
        return "continue-wave1a-shadow-observation-v1"
    if state == "wave1c_canary":
        return "verify-wave1c-canary-v1"
    return "resolve-wave-readiness-blockers-v1"


def _objective_for_state(state: str) -> str:
    if state == "wave1c_promotion_candidate":
        return "Enable Wave 1C as a controlled evidence-gated canary"
    if state == "collecting_wave1a_evidence":
        return "Continue Wave 1A shadow observation until evidence gates mature"
    if state == "wave1c_canary":
        return "Verify Wave 1C canary remains safe and evidence-producing"
    return "Resolve Hyperliquid engine wave readiness blockers"


def _severity_for_state(state: str) -> str:
    if state in {"blocked", "wave1c_canary_regressed"}:
        return "HIGH"
    if state == "wave1c_promotion_candidate":
        return "MEDIUM"
    return "LOW"


def _acceptance_criteria_for_state(state: str) -> list[str]:
    common = [
        "GET /health returns ok after changes",
        "GET /engine/readiness records the expected blocker or pass state",
        "Latest replay comparison is passed or advisory_pass before promotion",
        "No paper/live execution is enabled by the change",
        "Wave 2 remains integrated under the same RiskGateway and Council controls",
    ]
    if state == "wave1c_promotion_candidate":
        return [
            "Wave 1B evidence spine blockers are absent or explicitly waived by human review",
            "Wave 1C is enabled only as a controlled canary with RiskGateway and Council still active",
            "Deployment plan includes rollback by setting ENGINE_WAVE1C_ENABLED=false",
            *common,
        ]
    if state == "wave1c_canary":
        return [
            "Wave 1C canary produces candidate evidence links and outcome attribution",
            "No strategy/family/symbol concentration hard block persists after canary window",
            *common,
        ]
    return [
        "Readiness hard block root cause is fixed or escalated with concrete evidence",
        "Regression tests cover the fixed gate or orchestration behavior",
        *common,
    ]


def _verification_objectives_for_state(case_id: str, handoff_id: str, state: str) -> list[dict[str, Any]]:
    objectives = [
        {
            "objective_id": f"vo_{stable_hash({'handoff': handoff_id, 'key': 'health'}, length=12)}",
            "case_id": case_id,
            "handoff_id": handoff_id,
            "objective_key": "service-health-ok",
            "objective_type": "http_health",
            "name": "service health endpoint returns ok",
            "required_status": "pass",
            "required": True,
            "required_consecutive_passes": 1,
            "payload": {"path": "/health"},
        },
        {
            "objective_id": f"vo_{stable_hash({'handoff': handoff_id, 'key': 'readiness'}, length=12)}",
            "case_id": case_id,
            "handoff_id": handoff_id,
            "objective_key": "engine-readiness-reviewed",
            "objective_type": "engine_readiness",
            "name": "engine readiness reviewed after remediation",
            "required_status": "pass",
            "required": True,
            "required_consecutive_passes": 1,
            "payload": {"path": "/engine/readiness"},
        },
        {
            "objective_id": f"vo_{stable_hash({'handoff': handoff_id, 'key': 'wave2'}, length=12)}",
            "case_id": case_id,
            "handoff_id": handoff_id,
            "objective_key": "wave2-remains-integrated",
            "objective_type": "config_guardrail",
            "name": "Wave 2 remains enabled in the integrated catalog",
            "required_status": "pass",
            "required": True,
            "required_consecutive_passes": 1,
            "payload": {"setting": "ENGINE_ALPHA_CATALOG_MODE", "expected": "integrated"},
        },
    ]
    if state == "wave1c_promotion_candidate":
        objectives.append(
            {
                "objective_id": f"vo_{stable_hash({'handoff': handoff_id, 'key': 'wave1c'}, length=12)}",
                "case_id": case_id,
                "handoff_id": handoff_id,
                "objective_key": "wave1c-canary-enabled-only-after-review",
                "objective_type": "human_gate",
                "name": "Wave 1C canary promotion has review evidence",
                "required_status": "pass",
                "required": True,
                "required_consecutive_passes": 1,
                "payload": {"setting": "ENGINE_WAVE1C_ENABLED", "allowed_target": True, "requires_pr_or_signed_change": True},
            }
        )
    return objectives


def _evidence_from_readiness(readiness: dict[str, Any], replay: dict[str, Any] | None) -> list[dict[str, Any]]:
    evidence = [
        {
            "type": "engine_readiness",
            "ref": "/engine/readiness",
            "summary": f"grade={readiness.get('grade')} score={readiness.get('score')} ready_for_paper={readiness.get('ready_for_paper')}",
            "payload": _readiness_summary(readiness),
        }
    ]
    if replay:
        evidence.append(
            {
                "type": "engine_replay_comparison",
                "ref": str(replay.get("replay_id") or "/engine/replay-comparisons/latest"),
                "summary": f"status={replay.get('status')} verdict={(replay.get('metadata') or {}).get('verdict')}",
                "payload": _replay_summary(replay),
            }
        )
    return evidence


def _readiness_summary(readiness: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready_for_paper": readiness.get("ready_for_paper"),
        "score": readiness.get("score"),
        "grade": readiness.get("grade"),
        "recommendation": readiness.get("recommendation"),
        "hard_block_codes": [item.get("code") for item in readiness.get("hard_blocks", []) if isinstance(item, dict)][:50],
        "warning_codes": [item.get("code") for item in readiness.get("warnings", []) if isinstance(item, dict)][:50],
        "next_actions": readiness.get("next_actions", [])[:20] if isinstance(readiness.get("next_actions"), list) else [],
    }


def _replay_summary(replay: dict[str, Any] | None) -> dict[str, Any]:
    if not replay:
        return {"status": "missing"}
    metadata = replay.get("metadata") if isinstance(replay.get("metadata"), dict) else {}
    candidate = replay.get("candidate_metrics") if isinstance(replay.get("candidate_metrics"), dict) else {}
    return {
        "replay_id": replay.get("replay_id"),
        "status": replay.get("status"),
        "verdict": metadata.get("verdict"),
        "promotion_decision": metadata.get("promotion_decision"),
        "data_window": metadata.get("data_window"),
        "candidate_count": candidate.get("candidate_count"),
        "allocated_count": candidate.get("allocated_count"),
        "outcome_attribution_coverage_pct": candidate.get("outcome_attribution_coverage_pct"),
    }


def _sanitize_mapping(value: Any) -> dict[str, Any]:
    sanitized = sanitize_payload(value)
    return sanitized if isinstance(sanitized, dict) else {"value": sanitized}


def sanitize_payload(value: Any, *, depth: int = 5) -> Any:
    if depth <= 0:
        return sanitize_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return sanitize_text(value, limit=2000)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, child) in enumerate(value.items()):
            if idx >= 100:
                break
            safe_key = sanitize_token(key, limit=80)
            if any(marker in safe_key.lower() for marker in {"secret", "token", "password", "credential", "authorization"}):
                out[safe_key] = "[redacted]"
            else:
                out[safe_key] = sanitize_payload(child, depth=depth - 1)
        return out
    if isinstance(value, list | tuple | set):
        return [sanitize_payload(item, depth=depth - 1) for item in list(value)[:100]]
    if hasattr(value, "model_dump"):
        try:
            return sanitize_payload(value.model_dump(mode="json"), depth=depth - 1)
        except Exception:
            return sanitize_text(value)
    return sanitize_text(value)


def _json_safe(value: Any) -> Any:
    return sanitize_payload(value)
