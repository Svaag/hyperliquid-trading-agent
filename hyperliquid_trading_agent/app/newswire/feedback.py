from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _score_bucket(score: float) -> str:
    if score < 35:
        return "<35"
    if score < 50:
        return "35-49"
    if score < 70:
        return "50-69"
    if score < 80:
        return "70-79"
    return "80+"


async def _list(repository: Any, method_name: str, *, limit: int, **kwargs: Any) -> list[dict[str, Any]]:
    method = getattr(repository, method_name, None)
    if not callable(method):
        return []
    try:
        return list(await method(limit=limit, **kwargs))
    except TypeError:
        try:
            return list(await method(limit=limit))
        except TypeError:
            return list(await method())


def _summary(posted_story_ids: set[str], votes: list[dict[str, Any]]) -> dict[str, Any]:
    quality = [vote for vote in votes if str(vote.get("label_type") or "") == "quality"]
    useful = sum(vote.get("label_value") is True for vote in quality)
    noise = sum(vote.get("label_value") is False for vote in quality)
    voted_story_ids = {str(vote.get("event_id") or "") for vote in votes}
    wrong_symbol_story_ids = {
        str(vote.get("event_id") or "")
        for vote in votes
        if str(vote.get("label_type") or "") == "symbol_correct" and vote.get("label_value") is False
    }
    wrong_direction_story_ids = {
        str(vote.get("event_id") or "")
        for vote in votes
        if str(vote.get("label_type") or "") == "direction_correct" and vote.get("label_value") is False
    }
    evaluator_ids = {str(vote.get("evaluator_id") or "anonymous") for vote in votes}
    posted_count = len(posted_story_ids)
    return {
        "posted_story_count": posted_count,
        "voted_story_count": len(voted_story_ids & posted_story_ids),
        "vote_coverage_pct": round(len(voted_story_ids & posted_story_ids) / posted_count * 100.0, 4) if posted_count else 0.0,
        "quality_vote_count": len(quality),
        "useful_count": useful,
        "noise_count": noise,
        "useful_rate_pct": round(useful / len(quality) * 100.0, 4) if quality else 0.0,
        "noise_rate_pct": round(noise / len(quality) * 100.0, 4) if quality else 0.0,
        "wrong_symbol_story_count": len(wrong_symbol_story_ids & posted_story_ids),
        "wrong_symbol_flag_rate_pct": round(len(wrong_symbol_story_ids & posted_story_ids) / posted_count * 100.0, 4) if posted_count else 0.0,
        "wrong_direction_story_count": len(wrong_direction_story_ids & posted_story_ids),
        "wrong_direction_flag_rate_pct": round(len(wrong_direction_story_ids & posted_story_ids) / posted_count * 100.0, 4) if posted_count else 0.0,
        "unique_evaluator_count": len(evaluator_ids),
        "total_latest_vote_count": len(votes),
    }


async def build_newswire_feedback_summary(
    repository: Any,
    *,
    cohort_start_ms: int | None = None,
    as_of_ms: int | None = None,
    source: str | None = None,
    score_bucket: str | None = None,
    limit: int = 100_000,
) -> dict[str, Any]:
    end_ms = int(as_of_ms or _now_ms())
    start_ms = int(cohort_start_ms or 0)
    deliveries = await _list(
        repository,
        "list_newswire_deliveries",
        limit=limit,
        destination="discord",
        status="posted",
        since_ms=start_ms,
        until_ms=end_ms,
    )
    deliveries = [
        row
        for row in deliveries
        if str(row.get("status") or "") == "posted"
        and start_ms <= int(row.get("posted_at_ms") or 0) <= end_ms
    ]
    stories = await _list(repository, "list_newswire_stories", limit=limit)
    story_by_id = {str(row.get("story_id") or ""): row for row in stories}
    posted: dict[str, dict[str, Any]] = {}
    for delivery in deliveries:
        story_id = str(delivery.get("story_id") or "")
        story = story_by_id.get(story_id)
        if story is None:
            continue
        assessment = _dict(story.get("assessment"))
        priority_score = float(assessment.get("priority_score") or 0.0)
        item = {
            "story_id": story_id,
            "source": str(story.get("source") or "unknown"),
            "score_bucket": _score_bucket(priority_score),
            "priority_score": priority_score,
            "posted_at_ms": delivery.get("posted_at_ms"),
        }
        if source and item["source"] != source:
            continue
        if score_bucket and item["score_bucket"] != score_bucket:
            continue
        existing = posted.get(story_id)
        if existing is None or int(item.get("posted_at_ms") or 0) >= int(existing.get("posted_at_ms") or 0):
            posted[story_id] = item
    evals = await _list(
        repository,
        "list_newswire_evals",
        limit=limit,
        since_ms=start_ms,
        until_ms=end_ms,
    )
    latest_votes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for vote in evals:
        story_id = str(vote.get("event_id") or "")
        if story_id not in posted:
            continue
        key = (
            story_id,
            str(vote.get("evaluator_id") or "anonymous"),
            str(vote.get("label_type") or "unknown"),
        )
        current = latest_votes.get(key)
        if current is None or int(vote.get("created_at_ms") or 0) >= int(current.get("created_at_ms") or 0):
            latest_votes[key] = vote
    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"story_ids": set(), "votes": []}
    )
    for story in posted.values():
        groups[(story["source"], story["score_bucket"])]["story_ids"].add(story["story_id"])
    for vote in latest_votes.values():
        story = posted[str(vote.get("event_id") or "")]
        groups[(story["source"], story["score_bucket"])]["votes"].append(vote)
    group_rows = [
        {
            "source": key[0],
            "score_bucket": key[1],
            **_summary(set(value["story_ids"]), list(value["votes"])),
        }
        for key, value in groups.items()
    ]
    return {
        "generated_at_ms": _now_ms(),
        "cohort": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "basis": "discord_delivery_posted_at",
            "post_deploy_only": bool(start_ms),
        },
        "filters": {"source": source, "score_bucket": score_bucket},
        "overall": _summary(set(posted), list(latest_votes.values())),
        "groups": sorted(group_rows, key=lambda item: (item["source"], item["score_bucket"])),
        "data_quality": {
            "posted_delivery_count": len(deliveries),
            "joined_posted_story_count": len(posted),
            "delivery_story_join_coverage_pct": round(len(posted) / len(deliveries) * 100.0, 4) if deliveries else 0.0,
            "raw_eval_count": len(evals),
            "latest_deduplicated_vote_count": len(latest_votes),
        },
        "semantics": {
            "quality_denominator": "latest quality votes",
            "wrong_symbol_denominator": "posted stories; flags only",
            "wrong_direction_denominator": "posted stories; flags only",
            "absence_of_flag_is_not_correctness": True,
            "latest_vote_key": "story_id_x_evaluator_id_x_label_type",
        },
    }
