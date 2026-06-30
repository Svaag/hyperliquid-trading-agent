// Liquidation Flow Monitor — public page logic.
// Polls /api/summary + /api/venues, seeds the tape from /api/recent, then
// streams live rows over SSE. No build step / no external deps (self-contained,
// dashboard.py-style) for Phase 0; extracted to Vite/Tailwind at Phase 4.
"use strict";

const $ = (id) => document.getElementById(id);
const BADGES = {
  confirmed: "confirmed",
  verifiable: "verifiable",
  snapshot_throttled: "snapshot",
  account_private: "account",
  derived: "derived",
  vendor: "vendor",
};

function usd(n) {
  n = Number(n);
  if (!isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
  return "$" + n.toFixed(2);
}
function price(n) {
  n = Number(n);
  if (!isFinite(n)) return "—";
  return n >= 100 ? n.toFixed(2) : n >= 1 ? n.toFixed(4) : n.toPrecision(4);
}
function hhmmss(ms) {
  const d = new Date(ms);
  return d.toTimeString().slice(0, 8);
}
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[c]);
}
function badge(integrity) {
  const label = BADGES[integrity] || integrity || "?";
  return `<span class="badge b-${esc(integrity)}">${esc(label)}</span>`;
}
const EXECUTION_KINDS = new Set(["liquidation", "backstop", "adl", "deleverage", "market_settlement"]);

// ---- summary + charts -------------------------------------------------------
let lastSeries = [];

async function refreshSummary() {
  try {
    const d = await (await fetch("/liquidations/api/summary")).json();
    const w = d.windows || {};
    const h1 = w["1h"] || {};
    $("h-total").textContent = usd(h1.total_notional_usd);
    $("h-long").textContent = "longs " + usd(h1.long_notional_usd);
    $("h-short").textContent = "shorts " + usd(h1.short_notional_usd);
    $("h-1m").textContent = usd((w["1m"] || {}).total_notional_usd);
    $("h-5m").textContent = usd((w["5m"] || {}).total_notional_usd);
    $("h-count").textContent = (h1.count ?? 0).toLocaleString();
    const mx = h1.max_single;
    $("h-max").textContent = mx ? usd(mx.notional_usd) : "—";
    $("h-max-sub").textContent = mx ? `${esc(mx.symbol)} · ${esc(mx.side)} · ${esc(mx.venue)}` : "—";
    renderBreakdown("byvenue", h1.by_venue);
    renderBreakdown("bysymbol", h1.by_symbol);
    lastSeries = d.series || [];
    drawChart();
  } catch (e) {
    /* keep last good values */
  }
}

function renderBreakdown(id, obj) {
  const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    $(id).innerHTML = '<span class="muted">—</span>';
    return;
  }
  const max = entries[0][1] || 1;
  $(id).innerHTML = entries
    .map(([k, v]) => {
      const pct = Math.max(2, (v / max) * 100);
      return `<div style="display:flex;align-items:center;gap:8px;margin:4px 0">
        <div style="width:74px" class="num">${esc(k)}</div>
        <div style="flex:1;background:#0a1714;border-radius:5px;overflow:hidden;height:14px">
          <div style="width:${pct}%;height:100%;background:linear-gradient(90deg,#1b6,#4fe0c0)"></div>
        </div>
        <div class="num muted" style="width:74px;text-align:right">${usd(v)}</div>
      </div>`;
    })
    .join("");
}

function drawChart() {
  const c = $("chart");
  const dpr = window.devicePixelRatio || 1;
  const W = (c.width = c.clientWidth * dpr);
  const H = (c.height = c.clientHeight * dpr);
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  const data = lastSeries;
  if (!data.length) return;
  const pad = 6 * dpr;
  const max = Math.max(1, ...data.map((b) => b.total));
  const bw = (W - pad * 2) / data.length;
  for (let i = 0; i < data.length; i++) {
    const b = data[i];
    const x = pad + i * bw;
    const lh = (b.long / max) * (H - pad * 2);
    const sh = (b.short / max) * (H - pad * 2);
    ctx.fillStyle = "#f1556c"; // liquidated longs
    ctx.fillRect(x + bw * 0.12, H - pad - lh, bw * 0.76, lh);
    ctx.fillStyle = "rgba(46,230,166,0.85)"; // liquidated shorts, stacked on top
    ctx.fillRect(x + bw * 0.12, H - pad - lh - sh, bw * 0.76, sh);
  }
}
window.addEventListener("resize", drawChart);

// ---- venues -----------------------------------------------------------------
async function refreshVenues() {
  try {
    const d = await (await fetch("/liquidations/api/venues")).json();
    const vs = d.venues || [];
    if (!vs.length) {
      $("venues").innerHTML = '<span class="muted">no adapters connected</span>';
      return;
    }
    $("venues").innerHTML = vs
      .map((v) => {
        const live = v.connected && !v.stale;
        return `<span class="venue">
          <span class="dot ${live ? "on" : ""}"></span>
          <b>${esc(v.venue)}</b> ${badge(v.source_integrity)}
          <span class="muted num">${(v.events_total ?? 0).toLocaleString()}</span>
        </span>`;
      })
      .join("");
  } catch (e) {
    /* ignore */
  }
}

// ---- tape -------------------------------------------------------------------
const MAX_ROWS = 200;
function addRow(ev, flash) {
  const tb = $("tape");
  const tr = document.createElement("tr");
  const execution = EXECUTION_KINDS.has(ev.event_type);
  tr.className = (ev.liquidated_side || "") + (flash ? " flash" : "") + (execution ? "" : " pressure");
  tr.innerHTML =
    `<td class="muted num">${hhmmss(ev.timestamp_ms)}</td>` +
    `<td>${esc(ev.venue)}</td>` +
    `<td>${badge(ev.source_integrity)}</td>` +
    `<td><b>${esc(ev.symbol)}</b></td>` +
    `<td class="sd">${esc(ev.liquidated_side)}</td>` +
    `<td class="num">${usd(ev.notional_usd)}</td>` +
    `<td class="num muted">${price(ev.price)}</td>` +
    `<td class="muted">${esc(ev.event_type)}</td>`;
  tb.insertBefore(tr, tb.firstChild);
  while (tb.childNodes.length > MAX_ROWS) tb.removeChild(tb.lastChild);
}

async function seedTape() {
  try {
    const d = await (await fetch("/liquidations/api/recent?limit=60")).json();
    (d.items || []).reverse().forEach((ev) => addRow(ev, false));
  } catch (e) {
    /* ignore */
  }
}

function connectSSE() {
  const es = new EventSource("/liquidations/sse");
  es.onopen = () => {
    $("livedot").classList.add("on");
    $("livetxt").textContent = "live";
  };
  es.onerror = () => {
    $("livedot").classList.remove("on");
    $("livetxt").textContent = "reconnecting…";
  };
  es.onmessage = (m) => {
    try {
      addRow(JSON.parse(m.data), true);
    } catch (e) {
      /* ignore malformed frame */
    }
  };
}

// ---- boot -------------------------------------------------------------------
seedTape();
refreshSummary();
refreshVenues();
// `?static` skips the live stream + polling so headless screenshots can settle.
if (!location.search.includes("static")) {
  connectSSE();
  setInterval(refreshSummary, 3000);
  setInterval(refreshVenues, 5000);
} else {
  $("livedot").classList.add("on");
  $("livetxt").textContent = "static preview";
}
