const byId = (id) => document.getElementById(id);
let asOfMs = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"})[char]);
}

function headers() {
  const token = localStorage.getItem("agentToken") || "";
  return token ? {Authorization: `Bearer ${token}`} : {};
}

async function getJson(path) {
  const response = await fetch(path, {headers: headers()});
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}

function percent(value) {
  return value == null ? "" : `${(Number(value) * 100).toFixed(1)}%`;
}

function number(value) {
  return value == null ? "" : Number(value).toFixed(2);
}

function table(rows, columns) {
  if (!rows?.length) return '<div class="empty">No rows.</div>';
  const head = columns.map(([label]) => `<th>${escapeHtml(label)}</th>`).join("");
  const body = rows.map((row) => `<tr>${columns.map(([,key,render]) => `<td>${render ? render(row) : escapeHtml(row[key])}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`;
}

function render(data) {
  const summary = data.summary || {};
  const quality = data.quality || {};
  byId("metrics").innerHTML = [
    metric("Macro factors", summary.macro_factors || 0),
    metric("Asset impacts", summary.asset_impacts || 0),
    metric("Relevant forecasts", summary.relevant_forecasts || 0),
    metric("Evidence", summary.evidence || 0),
    metric("Quarantine", summary.quarantined || 0),
    metric("Errors", data.status?.error_count || 0),
  ].join("");
  byId("macro").innerHTML = table(data.macro_state?.items, [
    ["Factor","factor_id"], ["Axis","semantic_axis"], ["Regime","regime"],
    ["Level","level_score",(row) => number(row.level_score)],
    ["Momentum","momentum_score",(row) => number(row.momentum_score)],
    ["Surprise","surprise_score",(row) => number(row.surprise_score)],
    ["Coverage","coverage",(row) => percent(row.coverage)],
  ]);
  byId("impacts").innerHTML = table(data.asset_impacts?.items, [
    ["Asset","instrument_id"], ["Factor","factor_id"], ["Horizon","horizon"],
    ["Effect","direction",(row) => `<span class="pill ${escapeHtml(row.direction)}">${escapeHtml(row.direction)}</span>`],
    ["Mode","mode"], ["Strength","strength",(row) => percent(row.strength)],
  ]);
  byId("forecasts").innerHTML = table(data.prediction_markets?.items, [
    ["Question","question"], ["Yes probability","yes_probability",(row) => percent(row.yes_probability)],
    ["Confidence","confidence",(row) => percent(row.confidence)],
    ["Factors","factor_ids",(row) => escapeHtml((row.factor_ids || []).join(", "))],
    ["Assets","instrument_ids",(row) => escapeHtml((row.instrument_ids || []).join(", "))],
  ]);
  byId("evidence").innerHTML = table(data.evidence?.items, [
    ["Provider","provider"], ["Title","title"],
    ["Factors","factor_ids",(row) => escapeHtml((row.factor_ids || []).join(", "))],
    ["Assets","instrument_ids",(row) => escapeHtml((row.instrument_ids || []).join(", "))],
  ]);
  byId("quarantine").innerHTML = table(quality.items, [
    ["Type","record_type"], ["Item","title",(row) => escapeHtml(row.title || row.question || row.market_key)],
    ["Reasons","admission_reason_codes",(row) => escapeHtml((row.admission_reason_codes || []).join(", "))],
  ]);
  byId("quality").textContent = JSON.stringify({flags:data.snapshot?.quality_flags || [], coverage:quality.coverage || {}}, null, 2);
  byId("runtime").textContent = JSON.stringify({status:data.status, streams:data.streams}, null, 2);
  byId("annotations").innerHTML = table(data.annotations?.items, [
    ["Action","action"], ["Target","target_id"], ["Note","note"],
  ]);
  const graph = data.graph || {nodes:[], edges:[]};
  const nodes = (graph.nodes || []).slice(0, 40).map((node) => `<span class="node">${escapeHtml(node.label || node.id)}</span>`);
  const edges = (graph.edges || []).slice(0, 20).map((edge) => `<span class="edge">${escapeHtml(edge.label || edge.type)} →</span>`);
  byId("world-model-graph").innerHTML = [...nodes, ...edges].join("") || '<div class="empty">No mapped state.</div>';
}

async function load() {
  try {
    const params = new URLSearchParams({limit:byId("limit").value});
    if (byId("symbol").value.trim()) params.set("symbol", byId("symbol").value.trim());
    if (asOfMs) params.set("as_of_ms", String(asOfMs));
    render(await getJson(`/world-model/dashboard/data?${params}`));
  } catch (error) {
    byId("runtime").textContent = `Dashboard load failed: ${error.message}`;
  }
}

async function loadSnapshots() {
  try {
    const data = await getJson("/world-model/snapshots?limit=100");
    const values = (data.items || []).map((item) => Number(item.as_of_ms)).filter(Number.isFinite).sort((a,b) => a-b);
    if (!values.length) return;
    byId("timeSlider").min = String(values[0]);
    byId("timeSlider").max = String(values[values.length - 1]);
    byId("timeSlider").value = String(values[values.length - 1]);
  } catch (_) { /* runtime panel reports primary API errors */ }
}

byId("token").value = localStorage.getItem("agentToken") || "";
byId("save").addEventListener("click", () => { localStorage.setItem("agentToken", byId("token").value.trim()); load(); });
byId("refresh").addEventListener("click", load);
byId("symbol").addEventListener("change", load);
byId("timeSlider").addEventListener("change", () => { asOfMs = Number(byId("timeSlider").value); load(); });
byId("now").addEventListener("click", () => { asOfMs = null; load(); });
loadSnapshots();
load();
