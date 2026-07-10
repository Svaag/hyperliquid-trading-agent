from __future__ import annotations

import hashlib
import time
from collections import Counter
from typing import Any
from uuid import uuid4

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.event_ledger import EventLedger
from hyperliquid_trading_agent.app.engine.feature_store import FeatureStore
from hyperliquid_trading_agent.app.engine.newswire_bridge import newswire_event_to_engine_event
from hyperliquid_trading_agent.app.newswire.schemas import NewswireStoryRevision


class NewswireEngineReplayService:
    """Bounded historical Newswire replay that cannot mutate live trading state."""

    consumer_name = "trader:engine_newswire"

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any,
        replay_ledger: Any | None = None,
        replay_feature_store: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.replay_ledger = replay_ledger or EventLedger(repository)
        self.replay_feature_store = replay_feature_store or FeatureStore(
            repository,
            cross_venue_dexes=getattr(settings, "engine_cross_venue_dex_list", []),
            max_age_seconds=int(getattr(settings, "engine_feature_store_max_age_seconds", 7200)),
            funding_max_age_seconds=int(getattr(settings, "engine_feature_store_funding_max_age_seconds", 90000)),
            max_points_per_series=int(getattr(settings, "engine_feature_store_max_points_per_series", 4096)),
            full_universe_enabled=bool(getattr(settings, "engine_feature_full_universe_enabled", False)),
        )

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        generated_at_ms = _now_ms()
        end_ms = int(payload.get("end_ms") or generated_at_ms)
        window_hours = max(1, min(24 * 30, int(payload.get("window_hours") or 24)))
        start_ms = int(payload.get("start_ms") or end_ms - window_hours * 3_600_000)
        if start_ms <= 0 or end_ms <= start_ms:
            raise ValueError("replay window must have positive start_ms < end_ms")
        limit = max(1, min(5000, int(payload.get("limit") or 1000)))
        symbols = {str(symbol).upper() for symbol in payload.get("symbols") or [] if symbol}
        source = str(payload.get("source") or "").strip().lower()
        min_importance = max(0.0, min(100.0, float(payload.get("min_importance") or 0.0)))
        dry_run = bool(payload.get("dry_run", True))
        replay_run_id = str(payload.get("replay_run_id") or f"nwr_{uuid4().hex[:20]}")

        offset_before = await self.repository.get_consumer_offset(
            self.consumer_name,
            source_table="newswire_story_revisions",
        )
        rows = await self.repository.list_newswire_story_revisions(limit=limit)
        rows = sorted(rows, key=lambda item: (int(item.get("emitted_at_ms") or 0), str(item.get("revision_id") or "")))
        skip_reasons: Counter[str] = Counter()
        normalized_count = 0
        recorded_count = 0
        features_created = 0
        matched_count = 0
        invalid_rows = 0
        sample_event_ids: list[str] = []

        for row in rows:
            emitted_at_ms = int(row.get("emitted_at_ms") or 0)
            if not start_ms <= emitted_at_ms <= end_ms:
                skip_reasons["outside_window"] += 1
                continue
            try:
                revision = NewswireStoryRevision.model_validate(row)
            except Exception:
                invalid_rows += 1
                skip_reasons["invalid_revision"] += 1
                continue
            story = revision.story
            assessment = story.assessment
            if source and story.source.lower() != source and source not in {item.lower() for item in story.sources}:
                skip_reasons["source_filter"] += 1
                continue
            if symbols and not symbols.intersection({item.upper() for item in story.symbols}):
                skip_reasons["symbol_filter"] += 1
                continue
            if assessment is None or assessment.priority_score < min_importance:
                skip_reasons["importance_filter"] += 1
                continue
            matched_count += 1
            event = story.to_event(update_type=revision.update_type)
            replay_decision = dict(event.metadata.get("newswire_policy_decision") or {})
            replay_decision["shadow_only"] = False
            event = event.model_copy(
                update={
                    "metadata": {
                        **event.metadata,
                        "newswire_policy_decision": replay_decision,
                        "replay": True,
                        "replay_run_id": replay_run_id,
                        "replay_revision_id": revision.revision_id,
                        "execution_authority": "none",
                    }
                }
            )
            normalized = newswire_event_to_engine_event(event, settings=self.settings)
            if normalized is None:
                skip_reasons["no_engine_symbols"] += 1
                continue
            normalized_count += 1
            replay_event_id = "evt_nwr_" + hashlib.sha1(
                f"{replay_run_id}:{revision.revision_id}".encode()
            ).hexdigest()[:24]
            normalized = normalized.model_copy(
                update={
                    "event_id": replay_event_id,
                    "computed_ts_ms": normalized.received_ts_ms,
                    "payload": {
                        **normalized.payload,
                        "replay": True,
                        "replay_run_id": replay_run_id,
                        "execution_authority": "none",
                    },
                    "metadata": {
                        **normalized.metadata,
                        "replay": True,
                        "replay_run_id": replay_run_id,
                        "replay_revision_id": revision.revision_id,
                        "execution_authority": "none",
                        "live_consumer_offset_mutation": False,
                    },
                }
            )
            sample_event_ids.append(replay_event_id)
            if dry_run:
                continue
            await self.replay_ledger.record(normalized)
            recorded_count += 1
            engine_action = assessment.engine_action
            if engine_action in {"ignore", "ledger_only"}:
                skip_reasons[f"policy_{engine_action}"] += 1
                continue
            if event.source_score < float(self.settings.engine_news_min_source_score):
                skip_reasons["source_score_below_minimum"] += 1
                continue
            features = await self.replay_feature_store.features_for_event(normalized)
            features_created += len(features)

        offset_after = await self.repository.get_consumer_offset(
            self.consumer_name,
            source_table="newswire_story_revisions",
        )
        return {
            "replay_run_id": replay_run_id,
            "status": "dry_run" if dry_run else "completed",
            "dry_run": dry_run,
            "generated_at_ms": generated_at_ms,
            "window": {"start_ms": start_ms, "end_ms": end_ms, "window_hours": window_hours},
            "filters": {
                "symbols": sorted(symbols),
                "source": source or None,
                "min_importance": min_importance,
                "limit": limit,
            },
            "rows_scanned": len(rows),
            "rows_matched": matched_count,
            "events_normalized": normalized_count,
            "events_recorded": recorded_count,
            "features_created": features_created,
            "invalid_rows": invalid_rows,
            "skip_reasons": dict(sorted(skip_reasons.items())),
            "sample_replay_event_ids": sample_event_ids[:100],
            "live_offset_before": offset_before,
            "live_offset_after": offset_after,
            "live_offset_write_performed": False,
            "order_intents_created": 0,
            "execution_reports_created": 0,
            "risk_state_transitions_created": 0,
            "execution_authority": "none",
            "original_timestamps_preserved": True,
            "isolated_from_live_in_memory_state": True,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)
