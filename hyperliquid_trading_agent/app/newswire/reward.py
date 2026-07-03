from __future__ import annotations

import hashlib
import time
from typing import Any

from hyperliquid_trading_agent.app.newswire.policy import EngineAction, NewsDecision, NewsReward, NewswireAction


def build_reward(decision: NewsDecision | dict[str, Any], evals: list[dict[str, Any]]) -> NewsReward:
    data = decision.model_dump(mode="json") if isinstance(decision, NewsDecision) else dict(decision)
    labels = _aggregate_labels(evals)
    newswire_action = str(data.get("newswire_action") or "")
    engine_action = str(data.get("engine_action") or "")
    posted = newswire_action not in {NewswireAction.DROP.value, ""}
    engine_used = engine_action in {EngineAction.RISK_ONLY.value, EngineAction.DIRECTIONAL_FEATURE.value, EngineAction.MACRO_PROXY.value}
    reward = 0.0
    components: dict[str, float] = {}
    reasons: list[str] = []

    quality = _bool_label(labels, "quality")
    material = _bool_label(labels, "material")
    tradable = _bool_label(labels, "tradable")
    duplicate = _bool_label(labels, "duplicate")
    stale = _bool_label(labels, "stale")
    direction_correct = _bool_label(labels, "direction_correct")
    if direction_correct is None:
        direction_correct = _bool_label(labels, "direction")
    correct_newswire_action = _str_label(labels, "correct_newswire_action") or _str_label(labels, "newswire_action")
    correct_engine_action = _str_label(labels, "correct_engine_action") or _str_label(labels, "engine_action")

    if quality is True and posted:
        _add(components, "quality_reward", 2.0)
        reasons.append("published_quality_event")
    if quality is False and posted:
        penalty = 2.0
        if newswire_action == NewswireAction.HIGH.value:
            penalty += 2.0
        elif newswire_action == NewswireAction.BREAKING.value:
            penalty += 4.0
        _add(components, "false_positive_penalty", -penalty)
        reasons.append("published_low_quality_event")
    if quality is False and not posted:
        _add(components, "quality_reward", 1.0)
        reasons.append("correctly_dropped_noise")
    if quality is True and material is True and not posted:
        _add(components, "false_negative_penalty", -4.0)
        reasons.append("missed_material_event")

    if duplicate is True and posted:
        _add(components, "duplicate_penalty", -2.0)
        reasons.append("published_duplicate")
    if stale is True and posted:
        _add(components, "timeliness_reward", -2.0)
        reasons.append("published_stale_event")

    if engine_used and tradable is False:
        _add(components, "engine_reward", -6.0)
        reasons.append("sent_non_tradable_event_to_engine")
    if engine_used and quality is False:
        _add(components, "engine_reward", -8.0)
        reasons.append("sent_low_quality_event_to_engine")
    if engine_action == EngineAction.DIRECTIONAL_FEATURE.value:
        if direction_correct is False:
            _add(components, "direction_reward", -5.0)
            reasons.append("wrong_directional_feature")
        elif direction_correct is True:
            _add(components, "direction_reward", 3.0)
            reasons.append("correct_directional_feature")
    if engine_action == EngineAction.RISK_ONLY.value and material is True:
        _add(components, "engine_reward", 2.0)
        reasons.append("correct_risk_only_routing")

    if correct_newswire_action and correct_newswire_action != newswire_action:
        _add(components, "policy_action_reward", -2.0)
        reasons.append("wrong_newswire_action")
    elif correct_newswire_action and correct_newswire_action == newswire_action:
        _add(components, "policy_action_reward", 1.0)
        reasons.append("correct_newswire_action")
    if correct_engine_action and correct_engine_action != engine_action:
        _add(components, "policy_engine_reward", -3.0)
        reasons.append("wrong_engine_action")
    elif correct_engine_action and correct_engine_action == engine_action:
        _add(components, "policy_engine_reward", 1.0)
        reasons.append("correct_engine_action")

    reward = round(sum(components.values()), 4)
    event_id = str(data.get("event_id") or "")
    policy_version = str(data.get("policy_version") or "")
    decision_id = str(data.get("decision_id") or "") or None
    reward_id = "nwr_" + hashlib.sha1(f"{event_id}:{policy_version}:{decision_id}:{len(evals)}".encode()).hexdigest()[:24]
    return NewsReward(
        reward_id=reward_id,
        event_id=event_id,
        decision_id=decision_id,
        policy_version=policy_version,
        total_reward=reward,
        reward_components=components,
        labels=labels,
        reasons=reasons,
        created_at_ms=_now_ms(),
        metadata={"eval_count": len(evals)},
    )


def _aggregate_labels(evals: list[dict[str, Any]]) -> dict[str, Any]:
    labels: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    for item in evals:
        label_type = str(item.get("label_type") or "")
        if not label_type:
            continue
        conf = float(item.get("confidence") or 0.0)
        if conf >= confidence.get(label_type, -1.0):
            labels[label_type] = item.get("label_value")
            confidence[label_type] = conf
    return labels


def _bool_label(labels: dict[str, Any], key: str) -> bool | None:
    value = labels.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "yes", "good", "correct", "useful"}:
            return True
        if lowered in {"false", "no", "bad", "wrong", "noise"}:
            return False
    return None


def _str_label(labels: dict[str, Any], key: str) -> str | None:
    value = labels.get(key)
    return None if value is None else str(value)


def _add(components: dict[str, float], key: str, value: float) -> None:
    components[key] = round(float(components.get(key, 0.0)) + value, 4)


def _now_ms() -> int:
    return int(time.time() * 1000)
