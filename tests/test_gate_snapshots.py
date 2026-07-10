from __future__ import annotations

import anyio
import httpx
import respx

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.orchestration.gate_snapshots import GateEvidenceSnapshotService
from hyperliquid_trading_agent.app.orchestration.wave_supervisor import GitHubIssueEscalator


class _GateRepository:
    def __init__(self, anchor_ms: int) -> None:
        self.anchor_ms = anchor_ms
        self.runs: list[dict] = []

    async def list_service_heartbeats(self, **kwargs):
        return [
            {"service_role": "trader", "instance_id": "trader-1", "status": "running", "started_at_ms": self.anchor_ms, "updated_at_ms": self.anchor_ms},
            {"service_role": "newswire", "instance_id": "news-1", "status": "running", "started_at_ms": self.anchor_ms + 10, "updated_at_ms": self.anchor_ms},
        ]

    async def list_wave_supervisor_runs(self, **kwargs):
        return self.runs


class _RecordingGateService(GateEvidenceSnapshotService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captures: list[dict] = []

    async def capture(self, **kwargs):
        self.captures.append(kwargs)
        return {"run_id": kwargs["run_id"], "status": "completed"}


def test_gate_milestones_are_not_captured_early_and_use_later_clock() -> None:
    anchor = 1_000_000
    repo = _GateRepository(anchor)
    service = _RecordingGateService(
        settings=Settings(environment="test", _env_file=None, orchestration_gate_snapshot_milestone_hours="24,72"),
        repository=repo,
        engine_service=None,
    )

    early = anyio.run(lambda: service.capture_due(now_ms=anchor + 24 * 3_600_000))
    due = anyio.run(lambda: service.capture_due(now_ms=anchor + 10 + 24 * 3_600_000))

    assert early == []
    assert len(due) == 1
    assert service.captures[0]["anchor_ms"] == anchor + 10
    assert service.captures[0]["milestone_hours"] == 24


@respx.mock
def test_github_gate_comment_is_idempotent_by_marker() -> None:
    settings = Settings(
        environment="test",
        _env_file=None,
        orchestration_wave_supervisor_handoff_repo="Svaag/hyperliquid-trading-agent",
        orchestration_wave_supervisor_github_token="token",
    )
    client = GitHubIssueEscalator(settings=settings)
    url = "https://api.github.com/repos/Svaag/hyperliquid-trading-agent/issues/10/comments"
    marker = "<!-- gate-snapshot:gate_1_24h_v1:issue:10 -->"
    get_route = respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[{"id": 99, "html_url": "https://example/comment/99", "body": marker}]),
        ]
    )
    post_route = respx.post(url).mock(
        return_value=httpx.Response(201, json={"id": 99, "html_url": "https://example/comment/99"})
    )

    async def run():
        first = await client.upsert_comment(issue_number=10, body=f"{marker}\nbody", marker=marker)
        second = await client.upsert_comment(issue_number=10, body=f"{marker}\nbody", marker=marker)
        return first, second

    first, second = anyio.run(run)

    assert first["status"] == "created"
    assert second["status"] == "existing"
    assert get_route.call_count == 2
    assert post_route.call_count == 1
