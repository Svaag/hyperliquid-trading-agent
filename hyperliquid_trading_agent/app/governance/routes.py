from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.governance.export import ReviewExportService
from hyperliquid_trading_agent.app.governance.review import ReviewWorkflowService
from hyperliquid_trading_agent.app.governance.shadow import ShadowComparisonService


class PromotionDecisionRequest(BaseModel):
    reviewer: str = "api"
    decision: str = "approved"
    rationale: str = ""
    proposer_actor: str = "autonomy_tuning"
    approver_actor: str = "api"
    change_control_id: str = ""
    evidence_reviewed: list[str] = []
    tests_reviewed: list[str] = []
    approved_contexts: list[str] = []


def register_governance_routes(app: FastAPI, settings: Settings, require_auth: Callable[[Settings, str | None], None]) -> None:
    @app.get("/governance/config/active")
    async def governance_active_config(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        recorder = getattr(app.state, "decision_context_recorder", None)
        return recorder.active_refs() if recorder is not None else {}

    @app.get("/governance/decisions/{decision_id}")
    async def governance_decision(decision_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        item = await repository.get_decision_context(decision_id)
        if item is None:
            raise HTTPException(status_code=404, detail="decision context not found")
        return item

    @app.get("/governance/proposals")
    async def governance_proposals(status: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        if getattr(repository, "enabled", False):
            items = await repository.list_candidate_config_diffs(status=status, limit=limit)
        else:
            tuning_service = getattr(app.state, "tuning_service", None)
            raw = await tuning_service.list(status=status, limit=limit) if tuning_service is not None else []
            items = [(item.get("metadata") or {}).get("candidate_config_diff", item) for item in raw]
        return {"items": items, "count": len(items)}

    @app.get("/governance/proposals/review-ready")
    async def governance_review_ready_proposals(limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        items = await repository.list_candidate_config_diffs(status="review_ready", limit=limit) if getattr(repository, "enabled", False) else []
        return {"items": items, "count": len(items)}

    @app.get("/governance/replay-results")
    async def governance_replay_results(proposal_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        items = await repository.list_replay_results(proposal_id=proposal_id, limit=limit) if getattr(repository, "enabled", False) else []
        return {"items": items, "count": len(items)}

    @app.get("/governance/shadow-comparisons")
    async def governance_shadow_comparisons(proposal_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        items = await repository.list_shadow_comparisons(proposal_id=proposal_id, limit=limit) if getattr(repository, "enabled", False) else []
        return {"items": items, "count": len(items)}

    @app.get("/governance/review-packets")
    async def governance_review_packets(proposal_id: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        items = await repository.list_review_packets(proposal_id=proposal_id, limit=limit) if getattr(repository, "enabled", False) else []
        return {"items": items, "count": len(items)}

    @app.get("/governance/risk-decisions")
    async def governance_risk_decisions(decision: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        items = await repository.list_risk_gateway_decisions(limit=limit, decision=decision) if getattr(repository, "enabled", False) else []
        return {"items": items, "count": len(items)}

    @app.get("/governance/memory-injections")
    async def governance_memory_injections(role: str | None = None, limit: int = 100, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        items = await repository.list_memory_injection_events(limit=limit, role=role) if getattr(repository, "enabled", False) else []
        return {"items": items, "count": len(items)}

    @app.get("/governance/dashboard", response_class=HTMLResponse)
    async def governance_dashboard() -> HTMLResponse:
        return HTMLResponse(_dashboard_html())

    @app.get("/governance/dashboard/data")
    async def governance_dashboard_data(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        proposals = await repository.list_candidate_config_diffs(limit=100) if getattr(repository, "enabled", False) else []
        review_ready = [item for item in proposals if item.get("status") == "review_ready"]
        replays = await repository.list_replay_results(limit=20) if getattr(repository, "enabled", False) else []
        shadows = await repository.list_shadow_comparisons(limit=20) if getattr(repository, "enabled", False) else []
        review_packets = await repository.list_review_packets(limit=20) if getattr(repository, "enabled", False) else []
        risk_decisions = await repository.list_risk_gateway_decisions(limit=20) if getattr(repository, "enabled", False) else []
        memory_injections = await repository.list_memory_injection_events(limit=20) if getattr(repository, "enabled", False) else []
        autonomy_service = getattr(app.state, "autonomy_service", None)
        recorder = getattr(app.state, "decision_context_recorder", None)
        status_counts: dict[str, int] = {}
        for item in proposals:
            status = str(item.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "config": recorder.active_refs() if recorder is not None else {},
            "runtime": {
                "environment": settings.environment,
                "service_name": settings.service_name,
                "paper_only": not settings.hyperliquid_exchange_enabled and not settings.alpaca_trading_enabled,
                "autonomy": autonomy_service.status() if autonomy_service is not None else {},
            },
            "summary": {
                "proposal_count": len(proposals),
                "review_ready_count": len(review_ready),
                "replay_count": len(replays),
                "shadow_count": len(shadows),
                "risk_decision_count": len(risk_decisions),
                "proposal_status_counts": status_counts,
            },
            "proposals": proposals[:20],
            "review_ready": review_ready[:20],
            "replay_results": replays,
            "shadow_comparisons": shadows,
            "review_packets": review_packets,
            "risk_decisions": risk_decisions,
            "memory_injections": memory_injections,
        }

    @app.get("/governance/proposals/{proposal_id}")
    async def governance_proposal(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        item = await repository.get_candidate_config_diff(proposal_id) if getattr(repository, "enabled", False) else None
        if item is None:
            tuning_service = getattr(app.state, "tuning_service", None)
            raw = await tuning_service.get(proposal_id) if tuning_service is not None else None
            item = (raw.get("metadata") or {}).get("candidate_config_diff") if raw else None
        if item is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return item

    @app.get("/governance/proposals/{proposal_id}/review-export")
    async def governance_review_export(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        if not getattr(repository, "enabled", False):
            raise HTTPException(status_code=503, detail="governance repository unavailable")
        recorder = getattr(app.state, "decision_context_recorder", None)
        active_refs = recorder.active_refs() if recorder is not None else {}
        try:
            return await ReviewExportService(repository=repository).build(proposal_id, active_refs=active_refs)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="proposal not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/governance/proposals/{proposal_id}/request-replay")
    async def governance_request_replay(proposal_id: str, decision_id: str | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ShadowComparisonService = app.state.shadow_service
        result = await service.replay_candidate_diff(proposal_id, decision_id=decision_id)
        return result.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/request-shadow")
    async def governance_request_shadow(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ShadowComparisonService = app.state.shadow_service
        result = await service.compare_candidate_diff(proposal_id)
        return result.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/review-packet")
    async def governance_review_packet(proposal_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ReviewWorkflowService = app.state.review_service
        packet = await service.create_review_packet(proposal_id)
        return packet.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/approve")
    async def governance_approve(proposal_id: str, request: PromotionDecisionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ReviewWorkflowService = app.state.review_service
        decision = await service.record_promotion_decision(proposal_id=proposal_id, reviewer=request.reviewer, decision="approved", rationale=request.rationale, proposer_actor=request.proposer_actor, approver_actor=request.approver_actor, change_control_id=request.change_control_id, evidence_reviewed=request.evidence_reviewed, tests_reviewed=request.tests_reviewed, approved_contexts=request.approved_contexts)
        return decision.model_dump(mode="json")

    @app.post("/governance/proposals/{proposal_id}/reject")
    async def governance_reject(proposal_id: str, request: PromotionDecisionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service: ReviewWorkflowService = app.state.review_service
        decision = await service.record_promotion_decision(proposal_id=proposal_id, reviewer=request.reviewer, decision="rejected", rationale=request.rationale, proposer_actor=request.proposer_actor, approver_actor=request.approver_actor, change_control_id=request.change_control_id or "reject-no-change", evidence_reviewed=request.evidence_reviewed, tests_reviewed=request.tests_reviewed)
        return decision.model_dump(mode="json")

    @app.get("/governance/memories")
    async def governance_memories(role: str | None = None, status: str | None = "active", authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        service = app.state.memory_service
        items = await service.list_lessons(role=role, status=status, include_shadow=status == "shadow", limit=100)
        return {"items": items, "count": len(items)}

    @app.post("/governance/memories/{memory_id}/deprecate")
    async def governance_deprecate_memory(memory_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        await app.state.memory_service.archive_lesson(memory_id)
        return {"memory_id": memory_id, "status": "deprecated"}

    @app.post("/governance/freeze-live")
    async def governance_freeze_live(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        await app.state.repository.record_audit_event("live_trading_frozen", actor="api", payload={"paper_learning_continues": True, "exchange_actions": []})
        return {"live_trading_frozen": True, "paper_learning_continues": True}

    @app.post("/governance/paper-only")
    async def governance_paper_only(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        await app.state.repository.record_audit_event("paper_only_mode_confirmed", actor="api", payload={"exchange_actions": []})
        return {"paper_only": True, "exchange_actions": []}

def _dashboard_html() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trading Agent Governance Dashboard</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2f; --muted:#94a3b8; --text:#e2e8f0; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; --accent:#38bdf8; }
    * { box-sizing: border-box; } body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
    header { padding:24px; border-bottom:1px solid #1f2a44; display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
    h1 { margin:0; font-size:24px; } h2 { margin:0 0 12px; font-size:17px; } h3 { margin:12px 0 8px; font-size:14px; color:var(--muted); }
    .sub { color:var(--muted); margin-top:4px; } main { padding:24px; display:grid; gap:18px; }
    .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; } input, button { background:#0f172a; color:var(--text); border:1px solid #334155; border-radius:8px; padding:9px 10px; }
    input { min-width:280px; } button { cursor:pointer; } button:hover { border-color:var(--accent); }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; } .panel { background:var(--panel); border:1px solid #1f2a44; border-radius:14px; padding:16px; overflow:auto; }
    .metric { font-size:30px; font-weight:800; } .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); } .muted { color:var(--muted); }
    table { width:100%; border-collapse:collapse; font-size:13px; } th, td { text-align:left; border-bottom:1px solid #243149; padding:8px; vertical-align:top; } th { color:var(--muted); font-weight:600; }
    code, pre { background:#0f172a; border:1px solid #26334f; border-radius:8px; } code { padding:2px 5px; } pre { padding:10px; overflow:auto; max-height:360px; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; background:#1e293b; color:#cbd5e1; font-size:12px; } .actions button { margin:2px; padding:6px 8px; font-size:12px; }
    .two { display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:14px; }
  </style>
</head>
<body>
<header>
  <div><h1>Trading Agent Governance Dashboard</h1><div class="sub">Paper-only learning governance, replay/shadow evidence, review-ready proposals, risk and memory audit.</div></div>
  <div class="toolbar"><input id="token" type="password" placeholder="Bearer token (stored locally only)" /><button onclick="saveToken()">Save token</button><button onclick="loadDashboard()">Refresh</button></div>
</header>
<main>
  <section class="grid" id="summary"></section>
  <section class="two">
    <div class="panel"><h2>Review-ready proposals</h2><div id="reviewReady"></div></div>
    <div class="panel"><h2>Proposal status counts</h2><pre id="statusCounts">Loading…</pre></div>
  </section>
  <section class="panel"><h2>Recent proposals</h2><div id="proposals"></div></section>
  <section class="two">
    <div class="panel"><h2>Replay results</h2><div id="replays"></div></div>
    <div class="panel"><h2>Shadow comparisons</h2><div id="shadows"></div></div>
  </section>
  <section class="two">
    <div class="panel"><h2>Review packets</h2><div id="reviewPackets"></div></div>
    <div class="panel"><h2>Risk gateway decisions</h2><div id="riskDecisions"></div></div>
  </section>
  <section class="two">
    <div class="panel"><h2>Memory injection audit</h2><div id="memoryInjections"></div></div>
    <div class="panel"><h2>Runtime/config refs</h2><pre id="runtime"></pre></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function tokenHeaders(){ const t = localStorage.getItem('agentToken') || ''; return t ? {'Authorization':'Bearer '+t} : {}; }
function saveToken(){ localStorage.setItem('agentToken', $('token').value.trim()); loadDashboard(); }
async function api(path, opts={}){ const r = await fetch(path, {...opts, headers:{...tokenHeaders(), ...(opts.headers||{})}}); if(!r.ok){ throw new Error(r.status+' '+await r.text()); } return await r.json(); }
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }
function pill(v){ return `<span class="pill">${esc(v)}</span>`; }
function metric(label, value, cls=''){ return `<div class="panel"><div class="muted">${esc(label)}</div><div class="metric ${cls}">${esc(value)}</div></div>`; }
function table(items, cols){ if(!items || !items.length) return '<div class="muted">No rows.</div>'; return `<table><thead><tr>${cols.map(c=>`<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${items.map(row=>`<tr>${cols.map(c=>`<td>${c[2]?c[2](row):esc(row[c[1]])}</td>`).join('')}</tr>`).join('')}</tbody></table>`; }
async function runReplay(id){ await api(`/governance/proposals/${encodeURIComponent(id)}/request-replay`, {method:'POST'}); await loadDashboard(); }
async function runShadow(id){ await api(`/governance/proposals/${encodeURIComponent(id)}/request-shadow`, {method:'POST'}); await loadDashboard(); }
async function createReview(id){ try { await api(`/governance/proposals/${encodeURIComponent(id)}/review-packet`, {method:'POST'}); } catch(e){ alert(e.message); } await loadDashboard(); }
async function exportReview(id){
  try {
    const bundle = await api(`/governance/proposals/${encodeURIComponent(id)}/review-export`);
    const blob = new Blob([JSON.stringify(bundle, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob); const link = document.createElement('a');
    link.href = url; link.download = `governance-review-${id}.json`; link.click(); URL.revokeObjectURL(url);
  } catch(e){ alert(e.message); }
}
async function loadDashboard(){
  try{
    $('token').value = localStorage.getItem('agentToken') || '';
    const data = await api('/governance/dashboard/data');
    const s = data.summary || {}; const rt = data.runtime || {};
    $('summary').innerHTML = [
      metric('Paper-only', rt.paper_only ? 'YES' : 'NO', rt.paper_only ? 'ok':'bad'),
      metric('Proposals', s.proposal_count ?? 0),
      metric('Review-ready', s.review_ready_count ?? 0, (s.review_ready_count||0)>0?'warn':''),
      metric('Replays', s.replay_count ?? 0),
      metric('Shadows', s.shadow_count ?? 0),
      metric('Risk decisions', s.risk_decision_count ?? 0)
    ].join('');
    $('statusCounts').textContent = JSON.stringify(s.proposal_status_counts || {}, null, 2);
    $('runtime').textContent = JSON.stringify({runtime:data.runtime, config:data.config}, null, 2);
    const propCols = [['ID','proposal_id', r=>`<code>${esc(r.proposal_id)}</code>`], ['Status','status', r=>pill(r.status)], ['Risk','risk_direction', r=>pill(r.risk_direction)], ['Change','change_type'], ['Evidence','evidence', r=>esc((r.evidence||[]).length)], ['Actions','proposal_id', r=>`<span class="actions"><button onclick="runReplay('${esc(r.proposal_id)}')">Replay</button><button onclick="runShadow('${esc(r.proposal_id)}')">Shadow</button><button onclick="createReview('${esc(r.proposal_id)}')">Review packet</button><button onclick="exportReview('${esc(r.proposal_id)}')">Export review</button></span>`]];
    $('reviewReady').innerHTML = table(data.review_ready || [], propCols);
    $('proposals').innerHTML = table(data.proposals || [], propCols);
    $('replays').innerHTML = table(data.replay_results || [], [['Replay','replay_id', r=>`<code>${esc(r.replay_id)}</code>`], ['Proposal','proposal_id'], ['Status','status', r=>pill(r.status)], ['Sample','baseline_metrics', r=>esc((r.baseline_metrics||{}).sample_size ?? '-')], ['Avg R Δ','diffs', r=>esc((r.diffs||{}).avg_r ?? '-')], ['Created','created_at_ms']]);
    $('shadows').innerHTML = table(data.shadow_comparisons || [], [['Shadow','comparison_id', r=>`<code>${esc(r.comparison_id)}</code>`], ['Proposal','proposal_id'], ['Status','status', r=>pill(r.status)], ['Recommendation','recommendation', r=>pill(r.recommendation)], ['Replay','metadata', r=>esc((r.metadata||{}).replay_id ?? '-')]]);
    $('reviewPackets').innerHTML = table(data.review_packets || [], [['Packet','review_packet_id', r=>`<code>${esc(r.review_packet_id)}</code>`], ['Proposal','proposal_id'], ['Risk','risk_direction', r=>pill(r.risk_direction)], ['Rollback','rollback_plan_id']]);
    $('riskDecisions').innerHTML = table(data.risk_decisions || [], [['Decision','decision_id', r=>`<code>${esc(r.decision_id)}</code>`], ['Intent','intent_id'], ['Result','decision', r=>pill(r.decision)], ['Violations','violations', r=>esc((r.violations||[]).length)], ['Created','created_at_ms']]);
    $('memoryInjections').innerHTML = table(data.memory_injections || [], [['Run','run_id'], ['Role','role'], ['Context','context_type'], ['Allowed','memory_ids', r=>esc((r.memory_ids||[]).length)], ['Blocked','blocked_memory_ids', r=>esc((r.blocked_memory_ids||[]).length)], ['Created','created_at_ms']]);
  } catch(e){ $('summary').innerHTML = `<div class="panel bad"><b>Dashboard load failed</b><pre>${esc(e.message)}</pre><div class="muted">Enter AGENT_API_BEARER_TOKEN if configured.</div></div>`; }
}
loadDashboard();
</script>
</body>
</html>
""".strip()
