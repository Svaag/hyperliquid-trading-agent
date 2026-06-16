from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report

RequireAuth = Callable[[Settings, str | None], None]


def register_dashboard_routes(app: FastAPI, settings: Settings, require_auth: RequireAuth) -> None:
    @app.get("/dashboard", response_class=HTMLResponse)
    async def unified_dashboard() -> HTMLResponse:
        return HTMLResponse(_dashboard_html())

    @app.get("/dashboard/data")
    async def unified_dashboard_data(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_auth(settings, authorization)
        repository = app.state.repository
        engine_service = getattr(app.state, "engine_service", None)
        validation_monitor = getattr(app.state, "engine_validation_monitor", None)
        pnl_loop = getattr(app.state, "engine_pnl_attribution", None)
        readiness = await build_paper_readiness_scorecard(repository, settings, engine_service, limit=1000)
        validation = await build_engine_validation_report(repository, limit=500)
        latest_replay = await latest_engine_replay_comparison(repository)
        proposals = await repository.list_candidate_config_diffs(limit=20) if getattr(repository, "enabled", False) else []
        risk_decisions = await repository.list_risk_gateway_decisions(limit=20) if getattr(repository, "enabled", False) else []
        return {
            "runtime": {
                "environment": settings.environment,
                "paper_only": not settings.hyperliquid_exchange_enabled and not settings.alpaca_trading_enabled and not settings.engine_live_enabled,
            },
            "engine": {
                "status": engine_service.status() if engine_service is not None and callable(getattr(engine_service, "status", None)) else {},
                "validation_monitor": validation_monitor.status() if validation_monitor is not None and callable(getattr(validation_monitor, "status", None)) else {},
                "pnl_attribution": pnl_loop.status() if pnl_loop is not None and callable(getattr(pnl_loop, "status", None)) else {},
                "validation_report": validation,
                "readiness": readiness,
                "latest_replay_comparison": latest_replay,
            },
            "governance": {"proposals": proposals, "risk_decisions": risk_decisions},
            "alerts": [*readiness.get("hard_blocks", []), *readiness.get("warnings", [])],
        }


def _dashboard_html() -> str:
    return """
<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Trading Agent Dashboard</title>
<style>:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2f;--muted:#94a3b8;--text:#e2e8f0;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--accent:#38bdf8}body{margin:0;font-family:Inter,ui-sans-serif,system-ui;background:var(--bg);color:var(--text)}header{padding:22px;border-bottom:1px solid #1f2a44;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}main{padding:22px;display:grid;gap:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.panel{background:var(--panel);border:1px solid #1f2a44;border-radius:14px;padding:14px;overflow:auto}.metric{font-size:28px;font-weight:800}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}button,input{background:#0f172a;color:var(--text);border:1px solid #334155;border-radius:8px;padding:8px}button{cursor:pointer}.tabs button{margin-right:6px}.tab{display:none}.tab.active{display:block}pre{background:#0f172a;border:1px solid #26334f;border-radius:8px;padding:10px;max-height:520px;overflow:auto}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #243149;padding:7px;text-align:left}.pill{border-radius:999px;background:#1e293b;padding:2px 8px}</style></head>
<body><header><div><h1>Trading Agent Dashboard</h1><div class="muted">Unified engine readiness, shadow validation, PnL, and governance shell.</div></div><div><input id="token" type="password" placeholder="Bearer token"/><button onclick="saveToken()">Save</button><button onclick="load()">Refresh</button></div></header>
<main><section class="grid" id="summary"></section><div class="tabs"><button onclick="show('readiness')">Readiness</button><button onclick="show('engine')">Engine</button><button onclick="show('replay')">Replay</button><button onclick="show('pnl')">PnL</button><button onclick="show('governance')">Governance</button><button onclick="show('raw')">Raw</button></div>
<section id="readiness" class="tab active panel"></section><section id="engine" class="tab panel"></section><section id="replay" class="tab panel"></section><section id="pnl" class="tab panel"></section><section id="governance" class="tab panel"></section><section id="raw" class="tab panel"><pre id="rawpre"></pre></section></main>
<script>
const $=id=>document.getElementById(id);function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}function headers(){const t=localStorage.getItem('agentToken')||'';return t?{'Authorization':'Bearer '+t}:{}}function saveToken(){localStorage.setItem('agentToken',$('token').value.trim());load()}function show(id){document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));$(id).classList.add('active')}function metric(k,v,cls=''){return `<div class="panel"><div class="muted">${esc(k)}</div><div class="metric ${cls}">${esc(v)}</div></div>`}function list(items){return '<ul>'+(items||[]).map(i=>`<li><b>${esc(i.code||i.type)}</b> — ${esc(i.detail||'')}</li>`).join('')+'</ul>'}function table(items,cols){if(!items||!items.length)return '<div class="muted">No rows.</div>';return '<table><thead><tr>'+cols.map(c=>`<th>${esc(c[0])}</th>`).join('')+'</tr></thead><tbody>'+items.map(r=>'<tr>'+cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[1]])}</td>`).join('')+'</tr>').join('')+'</tbody></table>'}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...headers(),...(opts.headers||{})}});if(!r.ok)throw new Error(r.status+' '+await r.text());return await r.json()}async function runReplay(){await api('/engine/replay-comparisons/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({window_hours:24,baseline_config:{},candidate_config:{},variant_id:'current_self_check'})});await load();show('replay')}
async function load(){try{$('token').value=localStorage.getItem('agentToken')||'';const d=await api('/dashboard/data');const r=d.engine.readiness||{}, v=d.engine.validation_report||{}, s=v.summary||{}, rep=d.engine.latest_replay_comparison||{};$('summary').innerHTML=[metric('Paper readiness',r.ready_for_paper?'READY':'BLOCKED',r.ready_for_paper?'ok':'bad'),metric('Score',r.score??0,(r.score>=85?'ok':r.score>=70?'warn':'bad')),metric('Hard blocks',(r.hard_blocks||[]).length),metric('Shadow intents',s.shadow_intent_count??0),metric('Risk rejects',s.risk_reject_count??0),metric('Open positions',s.open_position_count??0)].join('');$('readiness').innerHTML=`<h2>Readiness: ${esc(r.grade)}</h2><p>Recommendation: <span class="pill">${esc(r.recommendation)}</span></p><h3>Hard blocks</h3>${list(r.hard_blocks)}<h3>Warnings</h3>${list(r.warnings)}<h3>Metrics</h3><pre>${esc(JSON.stringify(r.metrics,null,2))}</pre>`;$('engine').innerHTML=`<h2>Engine validation</h2><pre>${esc(JSON.stringify({status:d.engine.status,validation:v,monitor:d.engine.validation_monitor},null,2))}</pre>`;$('replay').innerHTML=`<h2>Shadow replay</h2><button onclick="runReplay()">Run 24h default comparison</button><pre>${esc(JSON.stringify(rep,null,2))}</pre>`;$('pnl').innerHTML=`<h2>PnL attribution</h2><pre>${esc(JSON.stringify({loop:d.engine.pnl_attribution,pnl:v.pnl_attribution_by_strategy,ev:v.ev_calibration},null,2))}</pre>`;$('governance').innerHTML='<h2>Governance</h2>'+table(d.governance.proposals,[['ID','proposal_id'],['Status','status'],['Strategy','strategy_id']])+'<h3>Risk decisions</h3>'+table(d.governance.risk_decisions,[['ID','decision_id'],['Decision','decision'],['Intent','intent_id']]);$('rawpre').textContent=JSON.stringify(d,null,2)}catch(e){$('summary').innerHTML=`<div class="panel bad"><b>Load failed</b><pre>${esc(e.message)}</pre></div>`}}load();
</script></body></html>
""".strip()
