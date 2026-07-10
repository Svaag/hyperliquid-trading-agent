from __future__ import annotations

import hashlib
import re
from collections import deque
from difflib import SequenceMatcher
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from hyperliquid_trading_agent.app.newswire.schemas import (
    NewswireEvent,
    NewswireStory,
    NewswireStoryRevision,
    StoryStatus,
    StoryUpdateType,
)

_STORY_WINDOW_MS = 24 * 60 * 60 * 1000
_TRACKING_QUERY_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source"}


class NewswireStoryClusterer:
    """Deterministic, bounded story clustering with source-level corroboration."""

    def __init__(self, max_stories: int = 2000):
        self.max_stories = max(100, int(max_stories))
        self._stories: dict[str, NewswireStory] = {}
        self._order: deque[str] = deque()
        self._last_emitted_at_ms = 0

    def hydrate(self, stories: Iterable[NewswireStory]) -> None:
        for story in sorted(stories, key=lambda item: item.last_updated_at_ms):
            self._put(story)

    def upsert(self, event: NewswireEvent) -> tuple[NewswireStory, StoryUpdateType]:
        existing = self._find(event)
        if existing is None:
            story = self._new_story(event)
            self._put(story)
            return story, "created"
        if _same_source_duplicate(existing, event):
            return existing, "duplicate"
        update_type = self._update_type(existing, event)
        sources = sorted(set([*existing.sources, event.source]))
        providers = sorted(set([*existing.providers, event.provider]))
        members = [*existing.member_event_ids]
        if event.event_id not in members:
            members.append(event.event_id)
        use_new_canonical = _canonical_rank(event) > _story_canonical_rank(existing)
        status: StoryStatus = (
            "retracted"
            if event.action == "removed"
            else "corrected"
            if update_type == "corrected"
            else "active"
            if existing.status == "retracted"
            else existing.status
        )
        updated = existing.model_copy(
            update={
                "revision": existing.revision + 1,
                "canonical_event_id": event.event_id if use_new_canonical else existing.canonical_event_id,
                "headline": event.headline if use_new_canonical else existing.headline,
                "body": event.body if use_new_canonical else existing.body,
                "url": event.url if use_new_canonical else existing.url,
                "source": event.source if use_new_canonical else existing.source,
                "provider": event.provider if use_new_canonical else existing.provider,
                "sources": sources,
                "providers": providers,
                "member_event_ids": members[-100:],
                "symbols": sorted(set([*existing.symbols, *event.symbols])),
                "topics": sorted(set([*existing.topics, *event.topics])),
                "asset_class": event.asset_class if existing.asset_class == "unknown" else existing.asset_class,
                "event_type": event.event_type if _event_type_rank(event.event_type) > _event_type_rank(existing.event_type) else existing.event_type,
                "urgency": "breaking" if event.urgency == "breaking" or existing.urgency == "breaking" else event.urgency,
                "sentiment": _merge_sentiment(existing.sentiment, event.sentiment),
                "source_score": max(existing.source_score, event.source_score),
                "confidence": max(existing.confidence, event.confidence),
                "published_at_ms": _min_optional(existing.published_at_ms, event.published_at_ms),
                # Durable consumers page by (emitted_at_ms, revision_id). Keep story
                # revision time strictly monotonic even when two adapters arrive in the
                # same millisecond so no revision can sort behind an acknowledged one.
                "last_updated_at_ms": max(
                    event.received_at_ms,
                    existing.last_updated_at_ms + 1,
                    self._last_emitted_at_ms + 1,
                ),
                "source_count": len(sources),
                "independent_source_count": len(sources),
                "status": status,
                "assessment": None,
                "metadata": {
                    **existing.metadata,
                    "last_update_type": update_type,
                    "canonical_changed": use_new_canonical,
                    "normalized_url": _normalized_url(event.url) or existing.metadata.get("normalized_url"),
                },
            }
        )
        self._put(updated)
        return updated, update_type

    def replace(self, story: NewswireStory) -> None:
        self._put(story)

    def get(self, story_id: str) -> NewswireStory | None:
        return self._stories.get(story_id)

    def list(self, limit: int = 100) -> list[NewswireStory]:
        return sorted(self._stories.values(), key=lambda item: item.last_updated_at_ms, reverse=True)[:limit]

    def revision(self, story: NewswireStory, update_type: StoryUpdateType) -> NewswireStoryRevision:
        digest = hashlib.sha1(f"{story.story_id}:{story.revision}:{update_type}".encode()).hexdigest()[:24]
        return NewswireStoryRevision(
            revision_id="nwsr_" + digest,
            story_id=story.story_id,
            revision=story.revision,
            update_type=update_type,
            emitted_at_ms=story.last_updated_at_ms,
            story=story,
        )

    def status(self) -> dict[str, int]:
        return {"stories": len(self._stories), "max_stories": self.max_stories}

    def _new_story(self, event: NewswireEvent) -> NewswireStory:
        normalized = _normalized_headline(event.headline)
        story_id = "nws_" + hashlib.sha1(f"{normalized}:{event.published_at_ms or event.received_at_ms // _STORY_WINDOW_MS}".encode()).hexdigest()[:24]
        status: StoryStatus = "retracted" if event.action == "removed" else "active"
        emitted_at_ms = max(event.received_at_ms, self._last_emitted_at_ms + 1)
        return NewswireStory(
            story_id=story_id,
            canonical_event_id=event.event_id,
            headline=event.headline,
            body=event.body,
            url=event.url,
            source=event.source,
            provider=event.provider,
            sources=[event.source],
            providers=[event.provider],
            member_event_ids=[event.event_id],
            symbols=list(event.symbols),
            topics=list(event.topics),
            asset_class=event.asset_class,
            event_type=event.event_type,
            urgency=event.urgency,
            sentiment=event.sentiment,
            source_score=event.source_score,
            confidence=event.confidence,
            published_at_ms=event.published_at_ms,
            first_seen_at_ms=event.received_at_ms,
            last_updated_at_ms=emitted_at_ms,
            source_count=1,
            independent_source_count=1,
            status=status,
            metadata={
                "last_update_type": "created",
                "normalized_headline": normalized,
                "normalized_url": _normalized_url(event.url),
            },
        )

    def _find(self, event: NewswireEvent) -> NewswireStory | None:
        event_url = _normalized_url(event.url)
        event_title = _normalized_headline(event.headline)
        event_tokens = _headline_tokens(event_title)
        cutoff = event.received_at_ms - _STORY_WINDOW_MS
        best: tuple[float, NewswireStory] | None = None
        for story in reversed(self.list(limit=self.max_stories)):
            if story.last_updated_at_ms < cutoff:
                continue
            if event.event_id == story.canonical_event_id or event.event_id in story.member_event_ids:
                return story
            story_url = str(story.metadata.get("normalized_url") or "")
            if event_url and story_url and event_url == story_url:
                return story
            story_title = str(story.metadata.get("normalized_headline") or _normalized_headline(story.headline))
            similarity = _headline_similarity(event_title, event_tokens, story_title)
            if similarity >= 0.78 and (best is None or similarity > best[0]):
                best = (similarity, story)
        return best[1] if best is not None else None

    def _update_type(self, story: NewswireStory, event: NewswireEvent) -> StoryUpdateType:
        if event.action == "removed":
            return "retracted"
        if event.action == "updated":
            return "corrected" if event.source in story.sources else "updated"
        if event.source not in story.sources:
            return "confirmed"
        return "updated"

    def _put(self, story: NewswireStory) -> None:
        if story.story_id in self._stories:
            try:
                self._order.remove(story.story_id)
            except ValueError:
                pass
        self._stories[story.story_id] = story
        self._order.append(story.story_id)
        self._last_emitted_at_ms = max(self._last_emitted_at_ms, story.last_updated_at_ms)
        while len(self._stories) > self.max_stories:
            oldest = self._order.popleft()
            self._stories.pop(oldest, None)


def _normalized_headline(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").lower()).strip()
    text = re.sub(r"^(breaking|update|exclusive|alert)\s*[:\-\N{EN DASH}\N{EM DASH}]\s*", "", text)
    return re.sub(r"[^a-z0-9$% ]+", " ", text).strip()


def _headline_tokens(value: str) -> set[str]:
    return {token for token in value.split() if len(token) > 2}


def _headline_similarity(event_title: str, event_tokens: set[str], story_title: str) -> float:
    story_tokens = _headline_tokens(story_title)
    union = event_tokens | story_tokens
    jaccard = len(event_tokens & story_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, event_title, story_title).ratio()
    return max(jaccard, sequence * 0.95)


def _normalized_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        split = urlsplit(value)
    except ValueError:
        return value
    query = urlencode([(key, val) for key, val in parse_qsl(split.query, keep_blank_values=True) if key.lower() not in _TRACKING_QUERY_KEYS])
    path = split.path.rstrip("/") or "/"
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), path, query, ""))


def _canonical_rank(event: NewswireEvent) -> tuple[float, int, int]:
    return (float(event.source_score), len(event.body), len(event.headline))


def _story_canonical_rank(story: NewswireStory) -> tuple[float, int, int]:
    return (float(story.source_score), len(story.body), len(story.headline))


def _same_source_duplicate(story: NewswireStory, event: NewswireEvent) -> bool:
    if event.action != "created" or event.source not in story.sources or event.event_id in story.member_event_ids:
        return False
    same_headline = _normalized_headline(event.headline) == _normalized_headline(story.headline)
    same_body = re.sub(r"\s+", " ", event.body).strip() == re.sub(r"\s+", " ", story.body).strip()
    return same_headline and same_body


def _event_type_rank(event_type: str) -> int:
    ordered = {
        "other": 0,
        "headline": 1,
        "social": 2,
        "press_release": 3,
        "analyst_rating": 4,
        "earnings": 5,
        "sec_filing": 6,
        "mna": 7,
        "regulatory": 8,
        "macro": 9,
        "exchange_status": 10,
        "crypto_protocol": 11,
        "halt": 12,
    }
    return ordered.get(event_type, 0)


def _merge_sentiment(existing: str, incoming: str) -> str:
    if incoming == "unknown":
        return existing
    if existing == "unknown" or existing == incoming:
        return incoming
    return "mixed"


def _min_optional(first: int | None, second: int | None) -> int | None:
    values = [item for item in (first, second) if item is not None]
    return min(values) if values else None
