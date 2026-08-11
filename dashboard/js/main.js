/** Boot, poll loop, render orchestration */
import { $, fmtTokens, fmtUsd, fmtMs, esc } from './fmt.js';
import { renderRoundTree } from './tree.js';
import { drawLineChart, drawBars, setCostUnit } from './charts.js';
import { fillSessionSelect, switchSession, bindPoll } from './sessions.js';

function render(state) {
  window.__lastState = state;
  if (!state || state.error) {
    $("liveBadge").textContent = "error";
    $("liveBadge").className = "badge warn";
    if (state && state.error) $("sessionMeta").textContent = state.error;
    return;
  }
  $("liveBadge").textContent = state.watching ? "LIVE" : "idle";
  $("liveBadge").className = "badge " + (state.watching ? "live" : "warn");

  const sid = state.session_id || "—";
  const short = sid.length > 12 ? sid.slice(0, 8) + "…" : sid;
  $("sessionMeta").innerHTML = `<code title="${sid}">${short}</code>`;
  fillSessionSelect(state);

  const live = state.live || {};
  $("phaseMeta").textContent = live.phase ? `phase: ${live.phase}` : "";

  const sig = state.signals || {};
  const ctx = live.context_tokens_ui ?? live.context_tokens ?? sig.contextTokensUsed;
  const win = sig.contextWindowTokens || 500000;
  $("ctxNow").textContent = fmtTokens(ctx);
  const pct = ctx != null && win ? Math.min(100, (ctx / win) * 100) : 0;
  const ctxBar = $("ctxBar");
  if (ctxBar) ctxBar.style.width = pct + "%";

  const totals = state.totals || {};
  $("costOfficial").textContent = fmtUsd(totals.official_usd);
  $("costEstimate").textContent = fmtUsd(totals.estimate_usd);

  const last = (state.turns || []).slice(-1)[0];
  if (last && last.gen_tokens_per_sec != null) {
    $("genRate").textContent = last.gen_tokens_per_sec.toFixed(1) + "/s";
  } else {
    $("genRate").textContent = "—";
  }

  drawLineChart($("ctxChart"), state.context_series || [], "#3d9cf0", state.rounds || []);
  drawBars($("costChart"), state.turns || [], state.rounds || []);
  renderRoundTree(state.rounds || []);

  $("signalsBox").innerHTML = `
    <div><b>Latency</b></div>
    avg TTFT ${fmtMs(sig.avgTimeToFirstTokenMs)} · avg response ${fmtMs(sig.avgResponseTimeMs)}<br>
    ITL p50 ${fmtMs(sig.itlP50Ms)} · p99 ${fmtMs(sig.itlP99Ms)} · mean ${fmtMs(sig.itlMeanMs)}<br>
    chunks ${sig.totalChunkCount ?? "—"} · tools ${sig.toolCallCount ?? "—"} · turns ${sig.turnCount ?? "—"}<br>
    duration ${sig.sessionDurationSeconds != null ? Math.round(sig.sessionDurationSeconds) + "s" : "—"} ·
    models ${(sig.modelsUsed || []).join(", ") || "—"}
    <div style="margin-top:8px"><b>Tools used</b><br>${(sig.toolsUsed || []).join(", ") || "—"}</div>
  `;

  const pr = state.pricing || {};
  const notes = (pr.notes || []).map(n => `· ${esc(n)}`).join("<br>");
  $("pricingBox").innerHTML = `
    <div><b>Published rates (per 1M)</b></div>
    ≤200k: in $${pr.low?.input ?? 2} · out $${pr.low?.output ?? 6} · cache $${pr.low?.cached_input ?? 0.3}<br>
    &gt;200k: in $${pr.high?.input ?? 4} · out $${pr.high?.output ?? 12} · cache $${pr.high?.cached_input ?? 0.6}<br>
    <div style="margin-top:6px"><b>Estimate formula</b><br>
    uncached = input − cache<br>
    $ = uncached×in + cache×cache_rate + output×out<br>
    tier from peak single-prompt context during the round
    </div>
    <div style="margin-top:6px">${notes}</div>
    <div style="margin-top:8px"><b>Live</b><br>
    last event: ${live.last_kind || "—"} · prompt ${live.prompt_id ? live.prompt_id.slice(0,8)+"…" : "—"}<br>
    stream chars/s (proxy): ${live.chars_per_sec != null ? live.chars_per_sec.toFixed(0) : "—"}<br>
    ${state.context_now_estimate_note ? esc(state.context_now_estimate_note) : ""}
    </div>
  `;
}

let _pollBusy = false;

async function poll() {
  if (_pollBusy) return;
  _pollBusy = true;
  try {
    const r = await fetch("/api/state?_=" + Date.now());
    if (!r.ok) throw new Error("HTTP " + r.status);
    const state = await r.json();
    render(state);
  } catch (e) {
    console.error("dashboard poll/render failed:", e);
    $("liveBadge").textContent = "offline";
    $("liveBadge").className = "badge warn";
    const meta = $("sessionMeta");
    if (meta) meta.textContent = String(e && e.message ? e.message : e).slice(0, 120);
  } finally {
    _pollBusy = false;
  }
}

bindPoll(poll);

$("sessionSelect")?.addEventListener("change", (ev) => {
  switchSession(ev.target.value || null);
});

// Cost chart controls
$("costDetailToggle")?.addEventListener("change", (ev) => {
  window.__costChart.detail = !!ev.target.checked;
  if (window.__lastState) {
    drawBars($("costChart"), window.__lastState.turns || [], window.__lastState.rounds || []);
  }
});
$("costUnitUsd")?.addEventListener("click", () => setCostUnit("usd"));
$("costUnitTok")?.addEventListener("click", () => setCostUnit("tokens"));
$("costDrillBack")?.addEventListener("click", () => {
  window.__costChart.drillTurn = null;
  if (window.__lastState) {
    drawBars($("costChart"), window.__lastState.turns || [], window.__lastState.rounds || []);
  }
});

poll();
setInterval(poll, 1000);
window.addEventListener("resize", () => {
  if (window.__lastState) render(window.__lastState);
});

