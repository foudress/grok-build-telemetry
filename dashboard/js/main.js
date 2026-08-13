/** Boot, poll loop, render orchestration */
import { $, fmtTokens, fmtUsd, fmtMs, esc } from './fmt.js';
import { renderRoundTree, setTreeDensity, setRoundsOpen, clearRoundFocus } from './tree.js';
import { drawLineChart, drawBars, setCostUnit } from './charts.js';
import { fillSessionSelect, switchSession, bindPoll } from './sessions.js';

const TIER_CLIFF = 200000;
let _taskTab = "main";

function resetCostChartMode() {
  const st = window.__costChart;
  if (!st) return;
  st.drillTurn = null;
  st.hiddenLegend = new Set();
  st._wasDrill = false;
}

function switchTaskTab(id) {
  _taskTab = id || "main";
  resetCostChartMode();
  const last = window.__lastState;
  if (last) render(last);
}
window.__switchTaskTab = switchTaskTab;

function paintTaskTabs(state) {
  const el = $("taskTabs");
  if (!el) return;
  const subs = state.sub_sessions || [];
  if (!subs.length) {
    el.hidden = true;
    el.innerHTML = "";
    _taskTab = "main";
    return;
  }
  el.hidden = false;
  const bits = [
    { id: "main", label: "Main" },
    ...subs.map((s, i) => ({
      id: s.session_id,
      label: `Sub ${i + 1}`,
      tip: [s.title || s.label, s.agent_name ? `type ${s.agent_name}` : ""]
        .filter(Boolean).join(" · "),
    })),
  ];
  if (_taskTab !== "main" && !subs.some((s) => s.session_id === _taskTab)) {
    _taskTab = "main";
  }
  el.innerHTML = bits.map((b) => (
    `<button type="button" class="task-tab${_taskTab === b.id ? " is-on" : ""}" data-tab="${esc(b.id)}" title="${esc(b.tip || b.label)}">${esc(b.label)}</button>`
  )).join("");
  el.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchTaskTab(btn.getAttribute("data-tab") || "main");
    });
  });
}

function activeTaskView(state) {
  const subs = state.sub_sessions || [];
  if (_taskTab && _taskTab !== "main") {
    const sub = subs.find((s) => s.session_id === _taskTab);
    if (sub) {
      return {
        kind: "sub",
        rounds: sub.rounds || [],
        turns: sub.turns || [],
        live: sub.live || {},
        official_usd: sub.official_usd,
        estimate_usd: sub.official_usd,
        title: sub.title || sub.label,
      };
    }
  }
  return {
    kind: "main",
    rounds: state.rounds || [],
    turns: state.turns || [],
    live: state.live || {},
    official_usd: (state.totals || {}).official_usd,
    estimate_usd: (state.totals || {}).estimate_usd,
    title: null,
  };
}

function showBanner(msg, kind) {
  const el = $("statusBanner");
  if (!el) return;
  if (!msg) {
    el.hidden = true;
    el.textContent = "";
    el.className = "status-banner";
    return;
  }
  el.hidden = false;
  el.textContent = msg;
  el.className = "status-banner" + (kind === "error" ? " is-error" : "");
}

function paintPressure(ctx, win) {
  const bar = $("ctxPressure");
  const fill = $("ctxPressureFill");
  const notch = $("ctxPressureNotch");
  if (!bar || !fill) return;
  if (ctx == null || !win) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const pct = Math.min(100, (ctx / win) * 100);
  fill.style.width = pct + "%";
  bar.classList.toggle("is-tier", ctx >= TIER_CLIFF);
  bar.classList.toggle("is-hot", win > 0 && ctx / win >= 0.8);
  if (notch) notch.style.left = Math.min(100, (TIER_CLIFF / win) * 100) + "%";
}

function paintKvChip(rounds) {
  const chip = $("kvChip");
  if (!chip) return;
  const list = rounds || [];
  if (!list.length) {
    chip.hidden = true;
    return;
  }
  const newest = list[list.length - 1] || {};
  const miss = list.some((r) => r && (r.session_restart || r.cache_miss || r.context_reread
    || (r.user_prompt && (r.user_prompt.session_restart || r.user_prompt.cache_miss || r.user_prompt.context_reread))));
  const idle = Number(newest.idle_gap_ms);
  chip.hidden = false;
  chip.className = "badge kv";
  if (miss) {
    chip.classList.add("miss");
    chip.textContent = "KV miss";
    chip.title = "A round re-read prior context as uncached Input (cache miss / session restart).";
  } else if (Number.isFinite(idle) && idle >= 10 * 60 * 1000) {
    const min = Math.round(idle / 60000);
    chip.classList.add("stale");
    chip.textContent = `KV stale? · ${min}m idle`;
    chip.title = "Long idle often drops the provider KV cache. Empiric miss often ≥ 5–10 min.";
  } else {
    chip.classList.add("warm");
    chip.textContent = "KV warm";
    chip.title = "No cache-miss flag on loaded rounds.";
  }
}

function render(state) {
  window.__lastState = state;
  if (!state || state.error) {
    $("liveBadge").textContent = "error";
    $("liveBadge").className = "badge warn";
    const err = state && state.error ? String(state.error) : "no state";
    if (state && state.error) $("sessionMeta").textContent = err;
    showBanner(err, "error");
    return;
  }
  showBanner("");

  $("liveBadge").textContent = state.watching ? "LIVE" : "idle";
  $("liveBadge").className = "badge " + (state.watching ? "live" : "idle");

  const sid = state.session_id || "—";
  const short = sid.length > 12 ? sid.slice(0, 8) + "…" : sid;
  $("sessionMeta").innerHTML = `<code title="${esc(sid)}">${esc(short)}</code>`;
  fillSessionSelect(state);

  paintTaskTabs(state);
  const view = activeTaskView(state);
  const live = view.live || state.live || {};
  $("phaseMeta").textContent = live.phase
    ? `phase: ${live.phase}`
    : (view.kind === "sub" ? "sub-agent" : "");

  const sig = state.signals || {};
  const ctx = live.context_tokens_ui ?? live.context_tokens ?? (view.kind === "main" ? sig.contextTokensUsed : null);
  const win = sig.contextWindowTokens || 500000;
  $("ctxNow").textContent = fmtTokens(ctx);
  const pct = ctx != null && win ? Math.min(100, (ctx / win) * 100) : 0;
  const ctxBar = $("ctxBar");
  const ctxProgress = ctxBar && ctxBar.parentElement;
  if (ctxBar) ctxBar.style.width = pct + "%";
  if (ctxProgress && ctxProgress.classList.contains("progress")) {
    ctxProgress.classList.toggle("is-tier", ctx != null && ctx >= TIER_CLIFF);
    ctxProgress.classList.toggle("is-hot", ctx != null && win > 0 && ctx / win >= 0.8);
  }
  const ctxSub = $("ctxSub");
  if (ctxSub) {
    if (ctx == null) {
      ctxSub.textContent = "";
    } else {
      const tier = ctx >= TIER_CLIFF ? ">200k rates" : "≤200k rates";
      ctxSub.textContent = `${pct.toFixed(1)}% of ${fmtTokens(win)} · ${tier}`;
    }
  }
  paintPressure(ctx, win);

  const totals = state.totals || {};
  const offShow = view.kind === "sub" ? view.official_usd : totals.official_usd;
  const estShow = view.kind === "sub" ? view.estimate_usd : totals.estimate_usd;
  $("costOfficial").textContent = fmtUsd(offShow);
  $("costEstimate").textContent = fmtUsd(estShow);
  const off = Number(offShow);
  const est = Number(estShow);
  const offSub = $("costOfficialSub");
  const estSub = $("costEstimateSub");
  if (view.kind === "sub") {
    if (offSub) {
      offSub.textContent = view.title ? String(view.title) : "sub-agent session";
      offSub.className = "sub";
    }
    if (estSub) {
      estSub.textContent = "child session (own API bill)";
      estSub.className = "sub";
    }
  } else if (Number(totals.subagent_count) > 0) {
    if (offSub) {
      offSub.textContent = `général · parent ${fmtUsd(totals.parent_only_usd)} + ${totals.subagent_count} sub ${fmtUsd(totals.children_usd)}`;
      offSub.className = "sub";
    }
    if (estSub) {
      estSub.textContent = Number.isFinite(est) && Number.isFinite(off)
        ? `Δ ${est - off >= 0 ? "+" : ""}${fmtUsd(est - off)} vs official`
        : "";
      estSub.className = "sub";
    }
  } else if (Number.isFinite(off) && Number.isFinite(est)) {
    const delta = est - off;
    const match = Math.abs(delta) < 0.0005;
    if (offSub) {
      offSub.textContent = match ? "matches estimate" : `est ${fmtUsd(est)}`;
      offSub.className = "sub" + (match ? " match" : " drift");
    }
    if (estSub) {
      estSub.textContent = match ? "matches official" : `Δ ${delta >= 0 ? "+" : ""}${fmtUsd(delta)} vs official`;
      estSub.className = "sub" + (match ? " match" : " drift");
    }
  } else {
    if (offSub) { offSub.textContent = ""; offSub.className = "sub"; }
    if (estSub) { estSub.textContent = ""; estSub.className = "sub"; }
  }

  const pr = state.pricing || {};
  const modelBadge = $("modelBadge");
  if (modelBadge) {
    const mid = pr.model_ids && pr.model_ids[0] || live.model || pr.model;
    if (mid || pr.model_label) {
      modelBadge.hidden = false;
      modelBadge.textContent = pr.assumed
        ? (pr.model_label || "Grok 4.5") + " ?"
        : (pr.model_label || mid);
      modelBadge.title = (pr.model_ids || []).join(", ") || pr.model || "";
      modelBadge.className = "badge model" + (pr.assumed || pr.mixed ? " warn" : "");
    } else {
      modelBadge.hidden = true;
    }
  }

  const last = (view.turns || state.turns || []).slice(-1)[0];
  const genSub = $("genRateSub");
  if (last && last.gen_tokens_per_sec != null) {
    $("genRate").textContent = last.gen_tokens_per_sec.toFixed(1) + "/s";
    if (genSub) {
      const tn = last.turn_index != null ? last.turn_index : last.index;
      genSub.textContent = tn != null ? `round ${tn}` : "";
    }
  } else {
    $("genRate").textContent = "—";
    if (genSub) genSub.textContent = view.kind === "sub" ? "sub-agent" : "";
  }

  paintKvChip(view.rounds || []);
  drawLineChart(
    $("ctxChart"),
    view.kind === "main" ? (state.context_series || []) : [],
    "#3d9cf0",
    view.rounds || []
  );
  drawBars($("costChart"), view.turns || [], view.rounds || []);
  renderRoundTree(view.rounds || []);
  document.querySelectorAll("[data-sub-tab]").forEach((el) => {
    el.addEventListener("click", () => {
      const id = el.getAttribute("data-sub-tab");
      if (!id) return;
      switchTaskTab(id);
    });
  });

  $("signalsBox").innerHTML = `
    <div><b>Latency</b></div>
    avg TTFT ${fmtMs(sig.avgTimeToFirstTokenMs)} · avg response ${fmtMs(sig.avgResponseTimeMs)}<br>
    ITL p50 ${fmtMs(sig.itlP50Ms)} · p99 ${fmtMs(sig.itlP99Ms)} · mean ${fmtMs(sig.itlMeanMs)}<br>
    chunks ${sig.totalChunkCount ?? "—"} · tools ${sig.toolCallCount ?? "—"} · turns ${sig.turnCount ?? "—"}<br>
    duration ${sig.sessionDurationSeconds != null ? Math.round(sig.sessionDurationSeconds) + "s" : "—"} ·
    models ${(sig.modelsUsed || []).join(", ") || "—"}
    <div style="margin-top:8px"><b>Tools used</b><br>${(sig.toolsUsed || []).join(", ") || "—"}</div>
  `;

  const notes = (pr.notes || []).map(n => `· ${esc(n)}`).join("<br>");
  const modelLine = pr.assumed
    ? `assuming ${esc(pr.model_label || "Grok 4.5")} (not detected)`
    : `${esc(pr.model_label || pr.model || "—")}${(pr.model_ids || []).length ? " · " + esc(pr.model_ids.join(", ")) : ""}${pr.mixed ? " · mixed" : ""}`;
  $("pricingBox").innerHTML = `
    <div><b>Published rates (per 1M)</b></div>
    model ${modelLine}<br>
    ≤200k: in $${pr.low?.input ?? 2} · out $${pr.low?.output ?? 6} · cache $${pr.low?.cached_input ?? 0.3}<br>
    &gt;200k: in $${pr.high?.input ?? 4} · out $${pr.high?.output ?? 12} · cache $${pr.high?.cached_input ?? 0.6}<br>
    <div style="margin-top:6px"><b>Estimate formula</b><br>
    uncached = input − cache<br>
    $ = uncached×in + cache×cache_rate + output×out<br>
    tier from each LLM call’s <code>context_start</code> — never the round peak
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
    showBanner("Dashboard offline — last numbers kept on screen.", "error");
  } finally {
    _pollBusy = false;
  }
}

function restorePrefs() {
  try {
    const dens = localStorage.getItem("tt-tree-density");
    if (dens) setTreeDensity(dens);
    else setTreeDensity("standard");
    const unit = localStorage.getItem("tt-cost-unit");
    if (unit === "tokens" || unit === "usd") setCostUnit(unit);
    const detail = localStorage.getItem("tt-cost-detail");
    if (detail === "1") {
      window.__costChart.detail = true;
      const cb = $("costDetailToggle");
      if (cb) cb.checked = true;
    }
  } catch {
    setTreeDensity("standard");
  }
}

bindPoll(poll);
restorePrefs();

$("sessionSelect")?.addEventListener("change", (ev) => {
  switchSession(ev.target.value || null);
});

$("costDetailToggle")?.addEventListener("change", (ev) => {
  window.__costChart.detail = !!ev.target.checked;
  try { localStorage.setItem("tt-cost-detail", ev.target.checked ? "1" : "0"); } catch { /* ignore */ }
  const st = window.__costChart;
  if (st) drawBars($("costChart"), st.turns, st.rounds);
});
$("costUnitUsd")?.addEventListener("click", () => setCostUnit("usd"));
$("costUnitTok")?.addEventListener("click", () => setCostUnit("tokens"));
$("costDrillBack")?.addEventListener("click", () => {
  window.__costChart.drillTurn = null;
  clearRoundFocus();
  const st = window.__costChart;
  if (st) drawBars($("costChart"), st.turns, st.rounds);
});

$("densStandard")?.addEventListener("click", () => setTreeDensity("standard"));
$("densExpert")?.addEventListener("click", () => setTreeDensity("expert"));
$("treeCollapse")?.addEventListener("click", () => setRoundsOpen(false));

poll();
setInterval(poll, 1000);
window.addEventListener("resize", () => {
  const st = window.__lastState;
  if (!st) return;
  drawLineChart($("ctxChart"), st.context_series || [], "#3d9cf0", st.rounds || []);
  drawBars($("costChart"), st.turns || [], st.rounds || []);
});
