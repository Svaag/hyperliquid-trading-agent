from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse

from hyperliquid_trading_agent.app.config import Settings
from hyperliquid_trading_agent.app.engine.diagnostics import build_candidate_funnel, build_strategy_funnel
from hyperliquid_trading_agent.app.engine.news_risk_counterfactual import latest_news_risk_counterfactual
from hyperliquid_trading_agent.app.engine.readiness import build_paper_readiness_scorecard
from hyperliquid_trading_agent.app.engine.replay_compare import latest_engine_replay_comparison
from hyperliquid_trading_agent.app.engine.signal_quality import build_signal_quality_report
from hyperliquid_trading_agent.app.engine.validation_report import build_engine_validation_report
from hyperliquid_trading_agent.app.newswire.feedback import build_newswire_feedback_summary

RequireAuth = Callable[[Settings, str | None], None]

_NEWS_RISK_SCORE = {"no_event": 0.0, "catalyst": 1.0, "event_risk": 2.0, "event_shock": 3.0}
_TREND_SCORE = {"bear": -1.0, "range": 0.0, "transition": 0.0, "unknown": 0.0, "bull": 1.0}
_VOL_SCORE = {"compressed": 0.25, "normal": 1.0, "elevated": 2.0, "extreme": 3.0, "unknown": 0.0}


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
        regime = await _regime_dashboard_data(repository, settings)
        publisher_start_ms = await _publisher_start_ms(repository)
        candidate_funnel, strategy_funnel, signal_quality, feedback, counterfactual = await asyncio.gather(
            _safe_dashboard_component(build_candidate_funnel(repository, window_hours=24)),
            _safe_dashboard_component(build_strategy_funnel(repository, window_hours=24)),
            _safe_dashboard_component(build_signal_quality_report(repository, window_hours=24)),
            _safe_dashboard_component(
                build_newswire_feedback_summary(
                    repository,
                    cohort_start_ms=publisher_start_ms,
                )
            ),
            _safe_dashboard_component(latest_news_risk_counterfactual(repository)),
        )
        engine_status = engine_service.status() if engine_service is not None and callable(getattr(engine_service, "status", None)) else {}
        return {
            "runtime": {
                "environment": settings.environment,
                "paper_only": not settings.hyperliquid_exchange_enabled and not settings.alpaca_trading_enabled and not settings.engine_live_enabled,
            },
            "engine": {
                "status": engine_status,
                "strategy_catalog": engine_status.get("strategy_catalog") or (engine_status.get("strategy_registry") if isinstance(engine_status.get("strategy_registry"), dict) else {}),
                "validation_monitor": validation_monitor.status() if validation_monitor is not None and callable(getattr(validation_monitor, "status", None)) else {},
                "pnl_attribution": pnl_loop.status() if pnl_loop is not None and callable(getattr(pnl_loop, "status", None)) else {},
                "validation_report": validation,
                "readiness": readiness,
                "latest_replay_comparison": latest_replay,
                "regime": regime,
                "candidate_funnel": candidate_funnel,
                "strategy_funnel": strategy_funnel,
                "signal_quality": signal_quality,
                "news_risk_counterfactual": counterfactual,
            },
            "newswire": {"feedback": feedback, "publisher_cohort_start_ms": publisher_start_ms},
            "governance": {"proposals": proposals, "risk_decisions": risk_decisions},
            "alerts": [*readiness.get("hard_blocks", []), *readiness.get("warnings", [])],
        }


async def _safe_dashboard_component(awaitable: Any) -> Any:
    try:
        return await awaitable
    except Exception as exc:
        return {"status": "unavailable", "error": type(exc).__name__}


async def _publisher_start_ms(repository: Any) -> int:
    method = getattr(repository, "list_service_heartbeats", None)
    if not callable(method):
        return 0
    try:
        rows = await method(service_role="discord_publisher", limit=5)
    except Exception:
        return 0
    current = next((item for item in rows if item.get("status") == "running"), None)
    return int((current or {}).get("started_at_ms") or 0)


async def _regime_dashboard_data(repository: Any, settings: Settings, *, per_asset_limit: int = 240) -> dict[str, Any]:
    method = getattr(repository, "list_regime_snapshots", None)
    assets = list(settings.autonomy_core_symbols or ["BTC", "ETH", "HYPE"])
    if not callable(method):
        return {"assets": assets, "history": [], "latest_by_asset": {}, "changes": [], "percept": _percept_metadata()}
    rows: list[dict[str, Any]] = []
    for asset in assets:
        try:
            snapshots = await method(primary_asset=asset, limit=per_asset_limit)
        except Exception:
            snapshots = []
        mapped = [_regime_snapshot_to_chart_row(item) for item in snapshots]
        rows.extend(item for item in mapped if item is not None)
    rows.sort(key=lambda item: (str(item.get("asset") or ""), int(item.get("created_at_ms") or 0)))
    changes = _annotate_regime_changes(rows)
    latest_by_asset: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest_by_asset[str(row["asset"])] = row
    return {
        "assets": assets,
        "history": rows,
        "latest_by_asset": latest_by_asset,
        "changes": changes,
        "percept": _percept_metadata(),
    }


def _regime_snapshot_to_chart_row(item: dict[str, Any]) -> dict[str, Any] | None:
    vector = item.get("vector") if isinstance(item.get("vector"), dict) else item
    if not isinstance(vector, dict):
        return None
    labels = vector.get("derived_labels") if isinstance(vector.get("derived_labels"), dict) else {}
    metadata = vector.get("metadata") if isinstance(vector.get("metadata"), dict) else item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    news = metadata.get("news") if isinstance(metadata.get("news"), dict) else {}
    created_at_ms = int(item.get("created_at_ms") or vector.get("created_at_ms") or vector.get("as_of_ms") or 0)
    as_of_ms = int(item.get("as_of_ms") or vector.get("as_of_ms") or created_at_ms)
    asset = str(item.get("primary_asset") or vector.get("primary_asset") or "GLOBAL").upper()
    news_risk_tier = str(labels.get("news_risk_tier") or news.get("risk_tier") or "no_event")
    news_direction = str(labels.get("news_direction") or news.get("direction") or "unknown")
    regime_label = str(vector.get("regime_label") or labels.get("regime_label") or "unknown")
    stability = _float(vector.get("regime_stability_score")) or 0.0
    news_pressure = _float(vector.get("news_catalyst_pressure"))
    if news_pressure is None:
        news_pressure = _float(news.get("pressure")) or 0.0
    return {
        "regime_snapshot_id": item.get("regime_snapshot_id") or vector.get("regime_snapshot_id"),
        "asset": asset,
        "created_at_ms": created_at_ms,
        "as_of_ms": as_of_ms,
        "regime_label": regime_label,
        "trend_state": vector.get("trend_state") or "unknown",
        "trend_score": _TREND_SCORE.get(str(vector.get("trend_state") or "unknown"), 0.0),
        "volatility_state": vector.get("volatility_state") or "unknown",
        "volatility_score": _VOL_SCORE.get(str(vector.get("volatility_state") or "unknown"), 0.0),
        "liquidity_state": vector.get("liquidity_state") or "unknown",
        "spread_state": vector.get("spread_state") or "unknown",
        "funding_state": vector.get("funding_state") or "unknown",
        "oi_state": vector.get("oi_state") or "unknown",
        "liquidation_state": vector.get("liquidation_state") or "unknown",
        "orderflow_state": vector.get("orderflow_state") or "unknown",
        "news_state": vector.get("news_state") or "no_event",
        "news_risk_tier": news_risk_tier,
        "news_risk_score": _NEWS_RISK_SCORE.get(news_risk_tier, 0.0),
        "news_direction": news_direction,
        "news_pressure": news_pressure,
        "regime_stability_score": stability,
        "feature_coverage_pct": _float(vector.get("feature_coverage_pct")) or 0.0,
        "changed": False,
        "change_reasons": [],
    }


def _annotate_regime_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_by_asset: dict[str, dict[str, Any]] = {}
    changes: list[dict[str, Any]] = []
    for row in rows:
        asset = str(row.get("asset") or "GLOBAL")
        previous = previous_by_asset.get(asset)
        reasons: list[str] = []
        if previous is None:
            reasons.append("first_observation")
        else:
            for key in ("regime_label", "news_risk_tier", "trend_state", "volatility_state", "liquidation_state", "orderflow_state"):
                if previous.get(key) != row.get(key):
                    reasons.append(key)
        if reasons:
            row["changed"] = True
            row["change_reasons"] = reasons
            changes.append(row)
        previous_by_asset[asset] = row
    return changes


def _percept_metadata() -> dict[str, Any]:
    return {
        "provider": "percept.one",
        "library": "TitanCharts",
        "package": "@titancharts/react",
        "docs": "https://percept.one/docs/quickstart.md",
        "status": "experimental_client_loader_with_canvas_fallback",
    }


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _dashboard_html() -> str:
    return r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Trading Agent Dashboard</title>
<style>:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2f;--muted:#94a3b8;--text:#e2e8f0;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--accent:#38bdf8}body{margin:0;font-family:Inter,ui-sans-serif,system-ui;background:var(--bg);color:var(--text)}header{padding:22px;border-bottom:1px solid #1f2a44;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}main{padding:22px;display:grid;gap:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.panel{background:var(--panel);border:1px solid #1f2a44;border-radius:14px;padding:14px;overflow:auto}.metric{font-size:28px;font-weight:800}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}button,input,select{background:#0f172a;color:var(--text);border:1px solid #334155;border-radius:8px;padding:8px}button{cursor:pointer}.tabs button{margin-right:6px;margin-bottom:6px}.tab{display:none}.tab.active{display:block}pre{background:#0f172a;border:1px solid #26334f;border-radius:8px;padding:10px;max-height:520px;overflow:auto}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #243149;padding:7px;text-align:left}.pill{border-radius:999px;background:#1e293b;padding:2px 8px}.chartbox{height:360px;border:1px solid #26334f;border-radius:12px;background:#0f172a;margin:10px 0;position:relative}canvas{width:100%;height:100%;display:block}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.small{font-size:12px}</style></head>
<body><header><div><h1>Trading Agent Dashboard</h1><div class="muted">Unified engine readiness, regime history, shadow validation, PnL, and governance shell.</div></div><div><input id="token" type="password" placeholder="Bearer token"/><button onclick="saveToken()">Save</button><button onclick="load()">Refresh</button></div></header>
<main><section class="grid" id="summary"></section><div class="tabs"><button onclick="show('readiness')">Readiness</button><button onclick="show('regime')">Regime</button><button onclick="show('engine')">Engine</button><button onclick="show('diagnostics')">Funnels</button><button onclick="show('quality')">Signal Quality</button><button onclick="show('newswire')">Newswire Feedback</button><button onclick="show('catalog')">Catalog</button><button onclick="show('replay')">Replay</button><button onclick="show('pnl')">PnL</button><button onclick="show('governance')">Governance</button><button onclick="show('raw')">Raw</button></div>
<section id="readiness" class="tab active panel"></section><section id="regime" class="tab panel"></section><section id="engine" class="tab panel"></section><section id="diagnostics" class="tab panel"></section><section id="quality" class="tab panel"></section><section id="newswire" class="tab panel"></section><section id="catalog" class="tab panel"></section><section id="replay" class="tab panel"></section><section id="pnl" class="tab panel"></section><section id="governance" class="tab panel"></section><section id="raw" class="tab panel"><pre id="rawpre"></pre></section></main>
<script>
const $=id=>document.getElementById(id);let lastData=null;function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}function headers(){const t=localStorage.getItem('agentToken')||'';return t?{'Authorization':'Bearer '+t}:{}}function saveToken(){localStorage.setItem('agentToken',$('token').value.trim());load()}function show(id){document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));$(id).classList.add('active');if(id==='regime'&&lastData)renderRegime(lastData.engine.regime||{})}function metric(k,v,cls=''){return `<div class="panel"><div class="muted">${esc(k)}</div><div class="metric ${cls}">${esc(v)}</div></div>`}function list(items){return '<ul>'+(items||[]).map(i=>`<li><b>${esc(i.code||i.type)}</b> — ${esc(i.detail||'')}</li>`).join('')+'</ul>'}function table(items,cols){if(!items||!items.length)return '<div class="muted">No rows.</div>';return '<table><thead><tr>'+cols.map(c=>`<th>${esc(c[0])}</th>`).join('')+'</tr></thead><tbody>'+items.map(r=>'<tr>'+cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[1]])}</td>`).join('')+'</tr>').join('')+'</tbody></table>'}function fmtTime(ms){return ms?new Date(ms).toLocaleString():''}function fmtNum(n,d=3){return Number.isFinite(Number(n))?Number(n).toFixed(d):''}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...headers(),...(opts.headers||{})}});if(!r.ok)throw new Error(r.status+' '+await r.text());return await r.json()}async function runReplay(){await api('/engine/replay-comparisons/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({window_hours:24,baseline_config:{},candidate_config:{},variant_id:'current_self_check'})});await load();show('replay')}
function latestRows(regime){return Object.values(regime.latest_by_asset||{}).sort((a,b)=>String(a.asset).localeCompare(String(b.asset)))}function renderRegime(regime){const rows=regime.history||[], latest=latestRows(regime), changes=regime.changes||[];$('regime').innerHTML=`<h2>Current Regime</h2><div class="muted small">Chart provider target: ${esc((regime.percept||{}).provider)} / ${esc((regime.percept||{}).library)}. The dashboard tries a TitanCharts client module and falls back to the built-in canvas if unavailable.</div><div class="toolbar"><label>Asset <select id="regimeAsset" onchange="renderRegime(lastData.engine.regime||{})">${(regime.assets||[]).map(a=>`<option>${esc(a)}</option>`).join('')}</select></label><span class="pill">snapshots ${rows.length}</span><span class="pill">changes ${changes.length}</span></div><div id="perceptStatus" class="muted small"></div><div id="perceptChart" class="chartbox"></div><canvas id="regimeCanvas" class="chartbox"></canvas><h3>Latest by asset</h3>${table(latest,[['Asset','asset'],['Regime','regime_label'],['News tier','news_risk_tier',r=>`<span class="pill">${esc(r.news_risk_tier)}</span>`],['Trend','trend_state'],['Vol','volatility_state'],['Stability','regime_stability_score',r=>fmtNum(r.regime_stability_score)],['As of','as_of_ms',r=>fmtTime(r.as_of_ms)]])}<h3>Regime changes</h3>${table(changes.slice(-30).reverse(),[['Time','created_at_ms',r=>fmtTime(r.created_at_ms)],['Asset','asset'],['News','news_risk_tier'],['Trend','trend_state'],['Vol','volatility_state'],['Reasons','change_reasons',r=>esc((r.change_reasons||[]).join(', '))]])}`;const asset=$('regimeAsset')?.value||(regime.assets||[])[0];drawRegimeCanvas(rows.filter(r=>r.asset===asset));tryPerceptChart(rows.filter(r=>r.asset===asset))}
function drawRegimeCanvas(rows){const c=$('regimeCanvas');if(!c)return;const rect=c.getBoundingClientRect();c.width=Math.max(640,Math.floor(rect.width*devicePixelRatio));c.height=Math.max(280,Math.floor(rect.height*devicePixelRatio));const ctx=c.getContext('2d');ctx.scale(devicePixelRatio,devicePixelRatio);const w=c.width/devicePixelRatio,h=c.height/devicePixelRatio;ctx.clearRect(0,0,w,h);ctx.fillStyle='#0f172a';ctx.fillRect(0,0,w,h);ctx.strokeStyle='#243149';ctx.lineWidth=1;for(let i=0;i<=4;i++){const y=28+i*(h-58)/4;ctx.beginPath();ctx.moveTo(46,y);ctx.lineTo(w-14,y);ctx.stroke();ctx.fillStyle='#94a3b8';ctx.fillText(String(3-i),16,y+4)}if(!rows.length){ctx.fillStyle='#94a3b8';ctx.fillText('No regime history yet. Run the engine loop to persist regime snapshots.',24,40);return}const min=Math.min(...rows.map(r=>r.created_at_ms||r.as_of_ms||0)),max=Math.max(...rows.map(r=>r.created_at_ms||r.as_of_ms||0));const x=t=>46+((t-min)/Math.max(1,max-min))*(w-70);const y=v=>28+(3-Math.max(0,Math.min(3,v)))*(h-58)/3;function line(key,color,scale=1){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();rows.forEach((r,i)=>{const px=x(r.created_at_ms||r.as_of_ms),py=y((Number(r[key])||0)*scale);if(i)ctx.lineTo(px,py);else ctx.moveTo(px,py)});ctx.stroke()}line('news_risk_score','#f59e0b');line('volatility_score','#38bdf8');line('regime_stability_score','#22c55e',3);ctx.fillStyle='#f59e0b';ctx.fillText('news risk',54,18);ctx.fillStyle='#38bdf8';ctx.fillText('volatility',130,18);ctx.fillStyle='#22c55e';ctx.fillText('stability x3',205,18);rows.filter(r=>r.changed).forEach(r=>{ctx.strokeStyle='rgba(239,68,68,.55)';const px=x(r.created_at_ms||r.as_of_ms);ctx.beginPath();ctx.moveTo(px,24);ctx.lineTo(px,h-26);ctx.stroke()});ctx.fillStyle='#94a3b8';ctx.fillText(fmtTime(min),46,h-8);ctx.fillText(fmtTime(max),Math.max(46,w-185),h-8)}
async function tryPerceptChart(rows){const status=$('perceptStatus'), host=$('perceptChart');if(!status||!host)return;host.innerHTML='';if(!rows.length){host.style.display='none';status.textContent='Percept/TitanCharts not loaded: no data.';return}try{status.textContent='Trying Percept/TitanCharts module…';const React=await import('https://esm.sh/react@18');const ReactDOM=await import('https://esm.sh/react-dom@18/client');const mod=await import('https://esm.sh/@titancharts/react/auto');const TitanChart=mod.TitanChart;const data=rows.map(r=>({time:Math.floor((r.created_at_ms||r.as_of_ms)/1000),open:r.news_risk_score,high:Math.max(r.news_risk_score,r.volatility_score,r.regime_stability_score*3),low:0,close:r.news_risk_score,volume:r.feature_coverage_pct||0}));ReactDOM.createRoot(host).render(React.createElement(TitanChart,{data,market:{symbol:'REGIME',exchange:'engine'},defaultInterval:'1h',theme:'dark',settings:{title:'Regime risk score',showLegend:true,volume:{show:false}}}));host.style.display='block';status.textContent='Percept/TitanCharts rendered synthetic regime candles.'}catch(e){host.style.display='none';status.textContent='Percept/TitanCharts unavailable in this browser/runtime; using built-in canvas fallback. '+(e&&e.message?e.message:'')}}
function objectRows(value,key='name'){return Object.entries(value||{}).map(([name,count])=>({[key]:name,count})).sort((a,b)=>b.count-a.count)}
function renderDiagnostics(engine){const c=engine.candidate_funnel||{},s=engine.strategy_funnel||{},rows=s.groups||[];$('diagnostics').innerHTML=`<h2>Allocation funnel · 24h</h2><div class="muted small">Primary failures are mutually exclusive. Multi-label reasons are shown separately, so downstream symptoms do not double-count the cause.</div><div class="grid">${metric('Candidates',c.candidate_count??0)}${metric('Allocator approved',(c.stage_counts||{}).allocator_approved??0)}${metric('Council allowed',(c.stage_counts||{}).council_allowed??0)}${metric('Shadow intents',(c.stage_counts||{}).shadow_intent??0)}${metric('Breadth',`${s.active_strategy_count??0}/5 strategies`)}${metric('Families',`${s.active_strategy_family_count??0}/3`)}</div><h3>First terminal stage</h3>${table(objectRows(c.first_failure_counts),[['Stage','name'],['Count','count']])}<h3>Strategy activation</h3>${s.activation_telemetry_available?'':`<div class="warn">Historical activation telemetry is unavailable until the trader migration/restart. Candidate-stage evidence remains valid.</div>`}${table(rows,[['Strategy','strategy_id'],['Asset','asset'],['Evaluations','evaluation_count'],['Selected','selected_count'],['Feature ready','feature_ready_count'],['Triggered','triggered_evaluation_count'],['Candidates','generated_candidate_count'],['Top no-candidate reason','no_candidate_reason_counts',r=>esc((objectRows(r.no_candidate_reason_counts)[0]||{}).name||'')],['Council allowed','candidate_stage_counts',r=>esc((r.candidate_stage_counts||{}).council_allowed||0)],['Intents','candidate_stage_counts',r=>esc((r.candidate_stage_counts||{}).shadow_intent||0)]])}`}
function renderQuality(engine){const q=engine.signal_quality||{},dq=q.data_quality||{},groups=q.groups||[],cf=engine.news_risk_counterfactual||{},safety=(cf.metadata||{}).safety_decision||{};$('quality').innerHTML=`<h2>Modeled signal quality · fixed horizons</h2><div class="muted small">Returns are hypothetical mark outcomes after scorer-estimated costs, not execution PnL. Horizons are never pooled for promotion.</div><div class="grid">${metric('Strict rows',dq.usable_rows??0)}${metric('Missing marks',dq.missing_mark??0)}${metric('Late/fallback',dq.fallback_or_late_marks??0)}${metric('Regime joins',`${fmtNum(dq.regime_join_coverage_pct||0,1)}%`)}${metric('Overlay decision',safety.recommendation||'not run',safety.promotable?'ok':'warn')}</div><h3>By strategy x symbol x regime x horizon</h3>${table(groups.slice(0,150),[['Strategy','strategy_id'],['Symbol','symbol'],['Regime','regime_label'],['News','observed_news_risk_mode'],['Candidate H','candidate_horizon'],['Outcome H','outcome_window'],['N','n'],['Net hit','modeled_net_hit_rate_pct',r=>fmtNum(r.modeled_net_hit_rate_pct,1)+'%'],['Gross bps','mean_gross_return_bps',r=>fmtNum(r.mean_gross_return_bps,2)],['Modeled net bps','mean_modeled_net_return_bps',r=>fmtNum(r.mean_modeled_net_return_bps,2)],['ES05','expected_shortfall_05_bps',r=>fmtNum(r.expected_shortfall_05_bps,2)],['Confidence','confidence']])}<h3>Latest Newswire overlay counterfactual</h3><pre>${esc(JSON.stringify(cf,null,2))}</pre>`}
function renderNewswire(data){const f=(data.newswire||{}).feedback||{},o=f.overall||{};$('newswire').innerHTML=`<h2>Newswire human feedback</h2><div class="muted small">Cohort starts with the current discord-publisher instance. Wrong Symbol/Direction are flag rates; no click is not a correctness vote.</div><div class="grid">${metric('Posted stories',o.posted_story_count??0)}${metric('Vote coverage',fmtNum(o.vote_coverage_pct||0,1)+'%')}${metric('Useful',fmtNum(o.useful_rate_pct||0,1)+'%')}${metric('Noise',fmtNum(o.noise_rate_pct||0,1)+'%')}${metric('Wrong symbol',fmtNum(o.wrong_symbol_flag_rate_pct||0,1)+'%')}${metric('Wrong direction',fmtNum(o.wrong_direction_flag_rate_pct||0,1)+'%')}</div><h3>Source and score bucket</h3>${table(f.groups||[],[['Source','source'],['Score','score_bucket'],['Posted','posted_story_count'],['Coverage','vote_coverage_pct',r=>fmtNum(r.vote_coverage_pct,1)+'%'],['Useful','useful_rate_pct',r=>fmtNum(r.useful_rate_pct,1)+'%'],['Noise','noise_rate_pct',r=>fmtNum(r.noise_rate_pct,1)+'%'],['Wrong symbol','wrong_symbol_flag_rate_pct',r=>fmtNum(r.wrong_symbol_flag_rate_pct,1)+'%'],['Wrong direction','wrong_direction_flag_rate_pct',r=>fmtNum(r.wrong_direction_flag_rate_pct,1)+'%']])}`}
async function load(){try{$('token').value=localStorage.getItem('agentToken')||'';const d=await api('/dashboard/data');lastData=d;const r=d.engine.readiness||{}, v=d.engine.validation_report||{}, s=v.summary||{}, rep=d.engine.latest_replay_comparison||{}, reg=d.engine.regime||{}, cat=d.engine.strategy_catalog||{}, latest=latestRows(reg), tiers=latest.map(x=>x.asset+':'+x.news_risk_tier).join(' ')||'n/a';$('summary').innerHTML=[metric('Paper readiness',r.ready_for_paper?'READY':'BLOCKED',r.ready_for_paper?'ok':'bad'),metric('Score',r.score??0,(r.score>=85?'ok':r.score>=70?'warn':'bad')),metric('Catalog',`${cat.runtime_enabled??0}/${cat.total_specs??0}`),metric('Shadow-only',cat.shadow_only??0),metric('Regime',tiers),metric('Risk rejects',s.risk_reject_count??0)].join('');$('readiness').innerHTML=`<h2>Readiness: ${esc(r.grade)}</h2><p>Recommendation: <span class="pill">${esc(r.recommendation)}</span></p><h3>Hard blocks</h3>${list(r.hard_blocks)}<h3>Warnings</h3>${list(r.warnings)}<h3>Metrics</h3><pre>${esc(JSON.stringify(r.metrics,null,2))}</pre>`;renderRegime(reg);$('engine').innerHTML=`<h2>Engine validation</h2><pre>${esc(JSON.stringify({status:d.engine.status,validation:v,monitor:d.engine.validation_monitor},null,2))}</pre>`;renderDiagnostics(d.engine);renderQuality(d.engine);renderNewswire(d);$('catalog').innerHTML=`<h2>Strategy catalog: ${esc(cat.mode||'unknown')}</h2><div class="grid">${metric('Total specs',cat.total_specs??0)}${metric('Runtime enabled',cat.runtime_enabled??0)}${metric('Paper eligible',cat.paper_eligible??0)}${metric('Shadow only',cat.shadow_only??0)}${metric('Spec only',cat.spec_only??0)}</div><h3>Families</h3>${table(cat.families||[],[['Family','family'],['Total','total_specs'],['Runtime','runtime_enabled'],['Paper','paper_eligible'],['Shadow','shadow_only'],['Strategies','strategy_ids',r=>esc((r.strategy_ids||[]).join(', '))]])}<h3>Runtime IDs</h3><pre>${esc(JSON.stringify(cat.runtime_enabled_ids||[],null,2))}</pre>`;$('replay').innerHTML=`<h2>Shadow replay</h2><button onclick="runReplay()">Run 24h default comparison</button><pre>${esc(JSON.stringify(rep,null,2))}</pre>`;$('pnl').innerHTML=`<h2>PnL attribution</h2><pre>${esc(JSON.stringify({loop:d.engine.pnl_attribution,pnl:v.pnl_attribution_by_strategy,ev:v.ev_calibration},null,2))}</pre>`;$('governance').innerHTML='<h2>Governance</h2>'+table(d.governance.proposals,[['ID','proposal_id'],['Status','status'],['Strategy','strategy_id']])+'<h3>Risk decisions</h3>'+table(d.governance.risk_decisions,[['ID','decision_id'],['Decision','decision'],['Intent','intent_id']]);$('rawpre').textContent=JSON.stringify(d,null,2)}catch(e){$('summary').innerHTML=`<div class="panel bad"><b>Load failed</b><pre>${esc(e.message)}</pre></div>`}}load();
</script></body></html>
""".strip()
