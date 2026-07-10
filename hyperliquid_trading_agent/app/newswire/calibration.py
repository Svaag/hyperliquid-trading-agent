from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from hyperliquid_trading_agent.app.newswire.assessment import ASSESSMENT_VERSION
from hyperliquid_trading_agent.app.newswire.schemas import NewswireStory

CALIBRATION_THRESHOLDS = (35.0, 50.0, 70.0, 80.0)


def build_calibration_report(rows: list[dict[str, Any] | NewswireStory]) -> dict[str, Any]:
    """Summarize persisted V2 assessments without inventing a second scoring policy."""
    stories: list[NewswireStory] = []
    invalid_rows = 0
    for row in rows:
        try:
            stories.append(row if isinstance(row, NewswireStory) else NewswireStory.model_validate(row))
        except Exception:
            invalid_rows += 1

    assessed = [story for story in stories if story.assessment is not None]
    dimensions: dict[str, dict[str, list[NewswireStory]]] = {
        name: defaultdict(list)
        for name in ("source", "provider", "event_type", "asset_class", "watch_priority", "audience_scope")
    }
    for story in assessed:
        assessment = story.assessment
        assert assessment is not None
        values = {
            "source": story.source,
            "provider": story.provider,
            "event_type": story.event_type,
            "asset_class": story.asset_class,
            "watch_priority": assessment.watch_priority,
            "audience_scope": assessment.audience_scope,
        }
        for name, value in values.items():
            dimensions[name][str(value)].append(story)

    overall = _summarize(assessed)
    threshold_rates = {
        _threshold_label(threshold): {
            "threshold": threshold,
            "included": len(
                [story for story in assessed if story.assessment and story.assessment.priority_score >= threshold]
            ),
            "inclusion_rate_pct": _pct(
                len([story for story in assessed if story.assessment and story.assessment.priority_score >= threshold]),
                len(assessed),
            ),
        }
        for threshold in CALIBRATION_THRESHOLDS
    }
    unwatched_equity_escalations = [
        story.story_id
        for story in assessed
        if story.assessment
        and story.assessment.audience_scope == "unwatched_single_name"
        and story.assessment.feed_action in {"standard", "high", "breaking"}
    ]
    return {
        "assessment_version": ASSESSMENT_VERSION,
        "sample": {
            "rows_received": len(rows),
            "valid_stories": len(stories),
            "assessed_stories": len(assessed),
            "invalid_rows": invalid_rows,
        },
        "overall": overall,
        "threshold_inclusion": threshold_rates,
        "dimensions": {
            name: {value: _summarize(items) for value, items in sorted(groups.items())}
            for name, groups in dimensions.items()
        },
        "guardrails": {
            "unwatched_single_name_escalation_count": len(unwatched_equity_escalations),
            "unwatched_single_name_escalation_story_ids": unwatched_equity_escalations[:100],
            "passes_unwatched_equity_cap": not unwatched_equity_escalations,
        },
        "recommended_bands": {
            "drop": "priority<35",
            "watch": "35<=priority<50",
            "standard": "50<=priority<70",
            "high": "70<=priority<80",
            "breaking": "priority>=80 or a trusted watched/broad-market shock",
            "unwatched_single_name_equity": "capped_at_watch",
        },
    }


def _summarize(stories: list[NewswireStory]) -> dict[str, Any]:
    assessments = [story.assessment for story in stories if story.assessment is not None]
    scores = sorted(float(item.priority_score) for item in assessments)
    return {
        "count": len(assessments),
        "priority": {
            "min": round(scores[0], 4) if scores else None,
            "p25": _quantile(scores, 0.25),
            "p50": _quantile(scores, 0.50),
            "p75": _quantile(scores, 0.75),
            "p90": _quantile(scores, 0.90),
            "max": round(scores[-1], 4) if scores else None,
            "mean": round(mean(scores), 4) if scores else None,
        },
        "feed_actions": dict(sorted(Counter(item.feed_action for item in assessments).items())),
        "engine_actions": dict(sorted(Counter(item.engine_action for item in assessments).items())),
        "model_review_states": dict(sorted(Counter(item.model_review_state for item in assessments).items())),
    }


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 4)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    weight = position - lower
    return round(values[lower] * (1.0 - weight) + values[upper] * weight, 4)


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100.0, 4) if denominator else 0.0


def _threshold_label(threshold: float) -> str:
    return str(int(threshold)) if threshold.is_integer() else str(threshold)
