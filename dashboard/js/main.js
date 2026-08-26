/** Boot, poll loop, render orchestration */
import {
  $,
  fmtTokens,
  fmtUsd,
  fmtUsdPerM,
  esc,
  isSubagentKind,
  implyOfficialRatesPerM,
  normalizeModelFamily,
} from './fmt.js';
import { renderRoundTree, setTreeDensity, setRoundsOpen, clearRoundFocus, revealRound } from './tree.js';
import { drawLineChart, drawBars, setCostUnit, drawRateChart, buildSessionRatePoints, clearRateHost, hideAllChartTips, bindCtxChartResize } from './charts.js';
import { fillSessionSelect, switchSession, bindPoll } from './sessions.js';
import {
  bindPeriodPoll,
  bindPeriodControls,
  fetchPeriod,
  isPeriodScope,
  leavePeriodView,
  redrawPeriod,
  restoreScope,
  beginViewLoad,
  endViewLoad,
  openSessionFromPeriod,
  setScope,
} from './period.js';
import { FEATURES, applyFeatureGates } from './features.js';

const TIER_CLIFF = 200000;
const CTX_GRAPH_MIN_H = 180;
const CTX_GRAPH_MAX_H = 720;
let _taskTab = "main";
let _pendingRevealRound = null;
let _graphMode = false;
let _rateMode = false;
let _ctxRateGrain = "call";
let _graphEngine = null;
let _graphCreate = null;
let _d3Promise = null;
let _graphRoot = null;
let _graphGen = 0;
let _graphActBusy = false;
let _graphHydrated = false;
let _graphSeen = new Set();
let _graphFocusSid = undefined;
let _officialFlipBound = false;

/** Sum official In (uncached) / Cached / Out tokens from turn rows. */
function sumTurnIoTokens(turns) {
  let unc = 0, cached = 0, out = 0;
  for (const t of turns || []) {
    const c = Number(t.cached_read_tokens) || 0;
    const inn = Number(t.uncached_input_tokens);
    const input = Number(t.input_tokens) || 0;
    unc += Number.isFinite(inn) ? inn : Math.max(0, input - c);
    cached += c;
    out += Number(t.output_tokens) || 0;
  }
  return { unc, cached, out, all: unc + cached + out };
}

/** Skip rounds whose context already crossed the pricing cliff. */
const RATE_CTX_CAP = 190000;

function turnPeakCtx(t) {
  const peak = Number(t && (t.peak_context_tokens ?? t.context_tokens_for_tier));
  if (Number.isFinite(peak) && peak > 0) return peak;
  const end = Number(t && t.context_end);
  return Number.isFinite(end) && end > 0 ? end : 0;
}

/** API usage tokens only (not tree In that adds recap/compact). */
function turnApiIoTokens(t) {
  const c = Number(t.cached_read_tokens) || 0;
  const inn = Number(t.uncached_input_tokens);
  const input = Number(t.input_tokens) || 0;
  const unc = Number.isFinite(inn) ? inn : Math.max(0, input - c);
  const out = Number(t.output_tokens) || 0;
  return { unc, cached: c, out, all: unc + c + out };
}

function viewModelFamily(view, state) {
  const rounds = (view && view.rounds) || [];
  for (let i = rounds.length - 1; i >= 0; i--) {
    const fam = normalizeModelFamily(
      rounds[i] && (rounds[i].model_family || rounds[i].model_id)
    );
    if (fam) return fam;
  }
  const sig = (state && state.signals) || {};
  const used = sig.modelsUsed;
  if (Array.isArray(used) && used.length) return normalizeModelFamily(used[0]);
  return normalizeModelFamily(sig.model || (view && view.live && view.live.model));
}

/**
 * Session Official $/M: sum Official $ + API tokens over rounds ≤190k,
 * then one imply (same helper as period I/O $/M). Avoids component-wise
 * max Frankenstein and keeps card ↔ graph consistent.
 */
function ratesPerMFromLowCtxTurns(turns, model) {
  const list = turns || [];
  let unc = 0, cached = 0, out = 0, off = 0;
  let n = 0;
  for (const t of list) {
    if (turnPeakCtx(t) > RATE_CTX_CAP) continue;
    const tok = turnApiIoTokens(t);
    const o = Number(t.official_usd) || 0;
    if (!(o > 0) || !(tok.unc + tok.cached + tok.out > 0)) continue;
    unc += tok.unc;
    cached += tok.cached;
    out += tok.out;
    off += o;
    n += 1;
  }
  let noteBase = "official · API tok · ≤190k ctx";
  if (!n) {
    for (const t of list) {
      const tok = turnApiIoTokens(t);
      const o = Number(t.official_usd) || 0;
      if (!(o > 0) || !(tok.unc + tok.cached + tok.out > 0)) continue;
      unc += tok.unc;
      cached += tok.cached;
      out += tok.out;
      off += o;
      n += 1;
    }
    noteBase = "official · API tok · all rounds";
  }
  if (!n) return null;
  const rates = implyOfficialRatesPerM(off, { unc, cached, out }, { model });
  if (!rates) return null;
  return { ...rates, note: rates.note ? `${noteBase} · ${rates.note}` : noteBase };
}

function paintOfficialFlip(officialUsd, tok, view, state) {
  const offSub = $("costOfficialSub");
  const model = viewModelFamily(view, state);
  const rates = ratesPerMFromLowCtxTurns((view && view.turns) || [], model);
  if (offSub) {
    if (rates) {
      offSub.textContent = `In ${fmtUsdPerM(rates.in)} · Cache ${fmtUsdPerM(rates.cached)} · Out ${fmtUsdPerM(rates.out)} /M`;
      offSub.className = "sub" + (rates.snapped ? " match" : "");
      offSub.title = rates.snapped
        ? `Published xAI rates (${rates.note})`
        : `Implied $/M from Official ÷ API tokens (${rates.note})`;
    } else if (view && view.kind === "sub") {
      offSub.textContent = view.title ? String(view.title) : "sub-agent session";
      offSub.className = "sub";
      offSub.title = "";
    } else {
      offSub.textContent = "";
      offSub.className = "sub";
      offSub.title = "";
    }
  }
  const tokVal = $("costOfficialTok");
  const tokSub = $("costOfficialTokSub");
  if (tokVal) tokVal.textContent = fmtTokens(tok.all);
  if (tokSub) {
    tokSub.textContent = `In ${fmtTokens(tok.unc)} · Cached ${fmtTokens(tok.cached)} · Out ${fmtTokens(tok.out)}`;
    tokSub.className = "sub";
  }
}

function setOfficialFlipEnabled(on) {
  const card = $("kpi2");
  if (!card) return;
  card.classList.toggle("card-flip-off", !on);
  if (!on) {
    card.classList.remove("is-flipped");
    card.setAttribute("aria-pressed", "false");
  }
  card.tabIndex = on ? 0 : -1;
}

function bindOfficialFlip() {
  const card = $("kpi2");
  if (!card || _officialFlipBound) return;
  _officialFlipBound = true;
  const toggle = () => {
    if (card.classList.contains("card-flip-off")) return;
    if (isPeriodScope()) return;
    const next = !card.classList.contains("is-flipped");
    card.classList.toggle("is-flipped", next);
    card.setAttribute("aria-pressed", next ? "true" : "false");
    const front = card.querySelector(".card-face-front");
    const back = card.querySelector(".card-face-back");
    if (front) front.setAttribute("aria-hidden", next ? "true" : "false");
    if (back) back.setAttribute("aria-hidden", next ? "false" : "true");
  };
  card.addEventListener("click", (ev) => {
    ev.preventDefault();
    toggle();
  });
  card.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    toggle();
  });
}

function resetCostChartMode() {
  const st = window.__costChart;
  if (!st) return;
  st.drillTurn = null;
  st.hiddenLegend = new Set();
  st._wasDrill = false;
}

function resolveTaskTabId(state, id) {
  const want = String(id || "").toLowerCase();
  if (!want || want === "main") return id || "main";
  const subs = (state && state.sub_sessions) || [];
  const hit = subs.find((s) => String(s.session_id || "").toLowerCase() === want)
    || subs.find((s) => String(s.root_session_id || "").toLowerCase() === want);
  return hit ? hit.session_id : (id || "main");
}

function switchTaskTab(id, roundIndex) {
  const last = window.__lastState;
  _taskTab = resolveTaskTabId(last, id) || "main";
  _pendingRevealRound = (roundIndex != null && roundIndex !== "")
    ? String(roundIndex)
    : null;
  resetCostChartMode();
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
    ...subs.map((s, i) => {
      const n = s.n != null ? s.n : (i + 1);
      return {
        id: s.session_id,
        label: `Sub ${n}`,
        tip: [s.title || s.label, s.agent_name ? `type ${s.agent_name}` : ""]
          .filter(Boolean).join(" · "),
      };
    }),
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
        estimate_usd: sub.estimate_usd != null ? sub.estimate_usd : sub.official_usd,
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
  const miss = list.some((r) => {
    if (!r) return false;
    const missTok = Number(r.cache_miss_in_tokens)
      || Number(r.breakdown && r.breakdown.cache_miss_in_tokens)
      || 0;
    if (missTok > 0) return true;
    const up = r.user_prompt;
    return !!(r.session_restart || r.cache_miss || r.context_reread
      || (up && (up.session_restart || up.cache_miss || up.context_reread)));
  });
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

function sessionCwd(state) {
  const sid = state && state.session_id;
  const rows = (state && state.sessions) || [];
  const row = rows.find((s) => s.session_id === sid);
  const cwd = row && row.cwd;
  return typeof cwd === "string" ? cwd.trim() : "";
}

function graphFocusSid() {
  return _taskTab && _taskTab !== "main" ? _taskTab : null;
}

function graphActivitySids() {
  const st = window.__lastState || {};
  const ids = [];
  if (st.session_id) ids.push(String(st.session_id));
  for (const s of st.sub_sessions || []) {
    if (s && s.session_id) ids.push(String(s.session_id));
  }
  return [...new Set(ids)];
}

function graphEventKey(ev) {
  if (!ev) return "";
  return (ev.t ?? "") + "\t" + (ev.sid ?? "") + "\t" + (ev.path ?? "") + "\t"
    + (ev.kind ?? "") + "\t" + (ev.tool ?? "");
}

function clampGraphH(h) {
  return Math.min(CTX_GRAPH_MAX_H, Math.max(CTX_GRAPH_MIN_H, Number(h) || 320));
}

function storedGraphH() {
  try {
    const n = parseInt(localStorage.getItem("tt-ctx-graph-h") || "", 10);
    if (Number.isFinite(n)) return clampGraphH(n);
  } catch { /* ignore */ }
  return 320;
}

function applyGraphHeight(h) {
  const wrap = $("ctxGraphWrap");
  if (!wrap) return;
  wrap.style.height = clampGraphH(h) + "px";
  if (_graphEngine) _graphEngine.resize();
}

function showGraphEmpty(msg) {
  const el = $("ctxGraphEmpty");
  if (el) {
    el.hidden = !msg;
    el.textContent = msg || "";
  }
}

function syncCtxChrome() {
  const tok = $("ctxModeTokens");
  const rate = $("ctxModeRate");
  const gra = $("ctxModeGraph");
  if (tok) {
    tok.classList.toggle("active", !_graphMode && !_rateMode);
    tok.setAttribute("aria-pressed", (!_graphMode && !_rateMode) ? "true" : "false");
  }
  if (rate) {
    rate.classList.toggle("active", !!_rateMode);
    rate.setAttribute("aria-pressed", _rateMode ? "true" : "false");
  }
  if (gra) {
    gra.classList.toggle("active", _graphMode);
    gra.setAttribute("aria-pressed", _graphMode ? "true" : "false");
  }
  const ctxTitle = $("ctxPanelTitle");
  if (ctxTitle) {
    ctxTitle.textContent = _graphMode
      ? "Graph"
      : (_rateMode ? "Tokens per second" : "Context tokens");
  }
  const grain = $("ctxRateGrain");
  if (grain) grain.hidden = !_rateMode || isPeriodScope();
  const callBtn = $("ctxRateCall");
  const rndBtn = $("ctxRateRound");
  if (callBtn) {
    callBtn.classList.toggle("active", _ctxRateGrain !== "round");
    callBtn.setAttribute("aria-pressed", _ctxRateGrain !== "round" ? "true" : "false");
  }
  if (rndBtn) {
    rndBtn.classList.toggle("active", _ctxRateGrain === "round");
    rndBtn.setAttribute("aria-pressed", _ctxRateGrain === "round" ? "true" : "false");
  }
  const chartWrap = $("ctxChartWrap");
  const graphWrap = $("ctxGraphWrap");
  const graphResize = $("ctxGraphResize");
  const chartResize = $("ctxChartResize");
  if (chartWrap) {
    chartWrap.hidden = _graphMode;
    chartWrap.classList.toggle("is-rate", !!_rateMode && !_graphMode);
  }
  if (graphWrap) graphWrap.hidden = !_graphMode;
  if (graphResize) graphResize.hidden = !_graphMode;
  if (chartResize) chartResize.hidden = !!_graphMode;
}

function persistCtxMode() {
  try {
    localStorage.setItem("tt-ctx-graph", _graphMode ? "1" : "0");
    localStorage.setItem("tt-ctx-rate", _rateMode ? "1" : "0");
    localStorage.setItem("tt-ctx-rate-grain", _ctxRateGrain);
  } catch { /* ignore */ }
}

function paintCtxChart(state) {
  if (!state || isPeriodScope()) return;
  const view = activeTaskView(state);
  if (_graphMode) {
    syncGraph(state);
    return;
  }
  if (_rateMode) {
    const pts = buildSessionRatePoints(view.rounds || [], _ctxRateGrain);
    drawRateChart($("ctxChart"), pts, {
      host: "ctx",
      grain: _ctxRateGrain,
      color: "#7ec8ff",
      onClick: (p) => { if (p && p.round != null) revealRound(p.round); },
    });
    return;
  }
  clearRateHost("ctx");
  const c = $("ctxChart");
  if (c) {
    c.style.width = "100%";
    c.style.height = "320px";
  }
  drawLineChart(
    $("ctxChart"),
    view.kind === "main" ? (state.context_series || []) : [],
    "#3d9cf0",
    view.rounds || []
  );
}

function teardownGraphEngine() {
  _graphActBusy = false;
  _graphHydrated = false;
  _graphSeen = new Set();
  _graphFocusSid = undefined;
  if (_graphEngine) {
    try { _graphEngine.destroy(); } catch { /* already gone */ }
    _graphEngine = null;
  }
  const canvas = $("ctxGraph");
  if (canvas) {
    const ctx = canvas.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
}

function ensureD3() {
  // d3 is not on index.html — inject once, engine reads globalThis.d3.
  if (globalThis.d3) return Promise.resolve();
  if (_d3Promise) return _d3Promise;
  _d3Promise = new Promise((resolve, reject) => {
    const existing = document.querySelector('script[src="/vendor/d3.min.js"]');
    const onOk = () => {
      if (globalThis.d3) resolve();
      else reject(new Error("d3 missing"));
    };
    if (existing) {
      if (globalThis.d3) { resolve(); return; }
      existing.addEventListener("load", onOk);
      existing.addEventListener("error", () => reject(new Error("d3")));
      return;
    }
    const s = document.createElement("script");
    s.src = "/vendor/d3.min.js";
    s.async = true;
    s.onload = onOk;
    s.onerror = () => reject(new Error("d3"));
    document.head.appendChild(s);
  });
  return _d3Promise;
}

async function loadCreateGraph() {
  await ensureD3();
  if (!_graphCreate) {
    const mod = await import("./graph-engine.js");
    _graphCreate = mod.createGraph;
  }
  return _graphCreate;
}

async function tickGraphActivity() {
  if (!_graphMode || isPeriodScope() || !_graphEngine || !_graphRoot || _graphActBusy) return;
  _graphActBusy = true;
  const gen = _graphGen;
  try {
    const sids = graphActivitySids();
    const sidQ = sids.length ? "&session_id=" + encodeURIComponent(sids.join(",")) : "";
    const r = await fetch(
      "/api/graph/activity?root=" + encodeURIComponent(_graphRoot) + sidQ + "&_=" + Date.now()
    );
    if (gen !== _graphGen || !_graphEngine) return;
    if (!r.ok) return;
    const data = await r.json();
    if (gen !== _graphGen || !_graphEngine) return;
    const events = Array.isArray(data.events) ? data.events : [];
    if (!_graphHydrated) {
      for (const ev of events) _graphSeen.add(graphEventKey(ev));
      _graphEngine.setActivity(events);
      _graphHydrated = true;
      return;
    }
    for (const ev of events) {
      const key = graphEventKey(ev);
      if (_graphSeen.has(key)) continue;
      _graphSeen.add(key);
      if (_graphSeen.size > 5000) _graphSeen.delete(_graphSeen.values().next().value);
      _graphEngine.appendActivity(ev);
    }
  } catch {
    /* keep last graph */
  } finally {
    _graphActBusy = false;
  }
}

async function bootGraph(cwd) {
  const gen = ++_graphGen;
  teardownGraphEngine();
  showGraphEmpty("");
  let create;
  try {
    create = await loadCreateGraph();
  } catch {
    if (gen !== _graphGen) return;
    showGraphEmpty("missing d3");
    return;
  }
  if (gen !== _graphGen) return;
  let r;
  try {
    r = await fetch("/api/graph?root=" + encodeURIComponent(cwd) + "&_=" + Date.now());
  } catch {
    if (gen !== _graphGen) return;
    showGraphEmpty("no files");
    return;
  }
  if (gen !== _graphGen) return;
  if (r.status === 400 || r.status === 403 || r.status === 404) {
    showGraphEmpty("cwd missing");
    return;
  }
  if (!r.ok) {
    showGraphEmpty("no files");
    return;
  }
  let data;
  try {
    data = await r.json();
  } catch {
    if (gen !== _graphGen) return;
    showGraphEmpty("no files");
    return;
  }
  if (gen !== _graphGen) return;
  const nodes = data.nodes || [];
  if (!nodes.length) {
    showGraphEmpty("no files");
    return;
  }
  const wrap = $("ctxGraphWrap");
  const canvas = $("ctxGraph");
  if (!wrap || !canvas || wrap.hidden) return;
  applyGraphHeight(storedGraphH());
  await new Promise((res) => requestAnimationFrame(res));
  if (gen !== _graphGen) return;
  if (wrap.hidden) return;
  showGraphEmpty("");
  _graphEngine = create(canvas, { nodes, edges: data.edges || [] });
  const sid = graphFocusSid();
  _graphFocusSid = sid;
  _graphEngine.highlightSession(sid);
  tickGraphActivity();
}

function syncGraph(state) {
  if (!_graphMode || isPeriodScope()) return;
  const cwd = sessionCwd(state);
  if (!cwd) {
    _graphGen += 1;
    teardownGraphEngine();
    _graphRoot = null;
    showGraphEmpty("cwd missing");
    return;
  }
  if (_graphRoot !== cwd) {
    _graphRoot = cwd;
    bootGraph(cwd);
    return;
  }
  if (_graphEngine) {
    const sid = graphFocusSid();
    if (_graphFocusSid !== sid) {
      _graphFocusSid = sid;
      _graphEngine.highlightSession(sid);
    }
  }
  tickGraphActivity();
}

function setCtxMode(mode) {
  let next = mode === "graph" ? "graph" : (mode === "rate" ? "rate" : "tokens");
  if (next === "graph" && !FEATURES.agentAnimationGraph) next = "tokens";
  if (next === "rate" && !FEATURES.toksPerSec) next = "tokens";
  const wasGraph = _graphMode;
  _graphMode = next === "graph";
  _rateMode = next === "rate";
  hideAllChartTips();
  persistCtxMode();
  syncCtxChrome();
  if (wasGraph && !_graphMode) {
    _graphGen += 1;
    teardownGraphEngine();
    _graphRoot = null;
    showGraphEmpty("");
  }
  const st = window.__lastState;
  if (st && !isPeriodScope()) paintCtxChart(st);
  if (_graphMode && !isPeriodScope()) {
    applyGraphHeight(storedGraphH());
    if (st) syncGraph(st);
  }
}

function dropGraphForPeriod() {
  if (!_graphEngine && !_graphRoot) return;
  _graphGen += 1;
  teardownGraphEngine();
  _graphRoot = null;
}

function bindCtxGraphResize() {
  const btn = $("ctxGraphResize");
  const wrap = $("ctxGraphWrap");
  if (!btn || !wrap || btn._bound) return;
  btn._bound = true;
  btn.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    btn.setPointerCapture(ev.pointerId);
    btn._rd = {
      y0: ev.clientY,
      h0: wrap.getBoundingClientRect().height || storedGraphH(),
    };
  });
  btn.addEventListener("pointermove", (ev) => {
    const d = btn._rd;
    if (!d) return;
    applyGraphHeight(d.h0 + (ev.clientY - d.y0));
  });
  const end = () => {
    if (!btn._rd) return;
    btn._rd = null;
    try {
      localStorage.setItem(
        "tt-ctx-graph-h",
        String(Math.round(clampGraphH(wrap.getBoundingClientRect().height)))
      );
    } catch { /* ignore */ }
  };
  btn.addEventListener("pointerup", end);
  btn.addEventListener("pointercancel", end);
}

function render(state) {
  if (isPeriodScope()) return;
  leavePeriodView();
  const tree = $("roundTree");
  if (tree) tree.classList.remove("sess-list");
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

  const meta = $("sessionMeta");
  if (meta) meta.textContent = "";
  fillSessionSelect(state);

  if (typeof window.__pendingTaskTab === "string" && window.__pendingTaskTab) {
    const tab = window.__pendingTaskTab;
    window.__pendingTaskTab = null;
    _taskTab = resolveTaskTabId(state, tab) || "main";
  }

  paintTaskTabs(state);
  const view = activeTaskView(state);
  const live = view.live || state.live || {};
  const phaseEl = $("phaseMeta");
  if (phaseEl) {
    phaseEl.textContent = "";
    phaseEl.hidden = true;
  }

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
  const estSub = $("costEstimateSub");
  const tok = sumTurnIoTokens(view.turns || []);
  paintOfficialFlip(off, tok, view, state);
  setOfficialFlipEnabled(!isPeriodScope());
  if (view.kind === "sub") {
    if (estSub) {
      estSub.textContent = "child session (own API bill)";
      estSub.className = "sub";
    }
  } else if (Number(totals.subagent_count) > 0) {
    if (estSub) {
      const childEst = Number(totals.children_estimate_usd);
      const parentEst = Number(totals.parent_estimate_usd);
      if (Number.isFinite(childEst) && Number.isFinite(parentEst)) {
        estSub.textContent = `parent ${fmtUsd(parentEst)} + ${totals.subagent_count} sub ${fmtUsd(childEst)}`;
      } else if (Number.isFinite(est) && Number.isFinite(off)) {
        estSub.textContent = `Δ ${est - off >= 0 ? "+" : ""}${fmtUsd(est - off)} vs official`;
      } else {
        estSub.textContent = "";
      }
      estSub.className = "sub";
    }
  } else if (Number.isFinite(off) && Number.isFinite(est)) {
    const delta = est - off;
    const match = Math.abs(delta) < 0.0005;
    if (estSub) {
      estSub.textContent = match ? "matches official" : `Δ ${delta >= 0 ? "+" : ""}${fmtUsd(delta)} vs official`;
      estSub.className = "sub" + (match ? " match" : " drift");
    }
  } else {
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
  const lastRate = last && last.gen_tokens_per_sec;
  const sessRate = (state.totals && state.totals.gen_tokens_per_sec)
    || state.gen_tokens_per_sec;
  if (lastRate != null) {
    $("genRate").textContent = Number(lastRate).toFixed(1) + "/s";
    if (genSub) {
      const tn = last.turn_index != null ? last.turn_index : last.index;
      const bits = [];
      if (tn != null) bits.push(`round ${tn}`);
      if (sessRate != null) bits.push(`sess ${Number(sessRate).toFixed(1)}/s`);
      genSub.textContent = bits.join(" · ");
    }
  } else {
    $("genRate").textContent = "—";
    if (genSub) genSub.textContent = view.kind === "sub" ? "sub-agent" : "";
  }

  paintKvChip(view.rounds || []);
  paintCtxChart(state);
  const superAgent = view.kind === "sub"
    || isSubagentKind(state.session_kind);
  drawBars($("costChart"), view.turns || [], view.rounds || [], { superAgent });
  renderRoundTree(view.rounds || [], {
    superAgent,
    subSessions: state.sub_sessions || [],
  });
  if (_pendingRevealRound) {
    revealRound(_pendingRevealRound);
    _pendingRevealRound = null;
  }
  document.querySelectorAll("[data-sub-tab]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = el.getAttribute("data-sub-tab");
      if (!id) return;
      switchTaskTab(id, el.getAttribute("data-sub-round"));
    });
  });

}

let _pollBusy = false;
let _pollEpoch = 0;
let _pollAgain = false;

async function poll() {
  if (_pollBusy) {
    // Mode switch / offset while a fetch is in flight — queue one follow-up
    // and invalidate the in-flight paint so we don't keep stale stack/rate.
    _pollAgain = true;
    _pollEpoch += 1;
    return;
  }
  _pollBusy = true;
  const epoch = _pollEpoch;
  try {
    if (isPeriodScope()) {
      dropGraphForPeriod();
      await fetchPeriod();
      if (epoch !== _pollEpoch) return;
      endViewLoad();
      return;
    }
    const r = await fetch("/api/state?_=" + Date.now());
    if (!r.ok) throw new Error("HTTP " + r.status);
    const state = await r.json();
    if (epoch !== _pollEpoch) return;
    if (typeof window.__pendingSid === "string" && window.__pendingSid
      && state.session_id && state.session_id !== window.__pendingSid) {
      return;
    }
    window.__pendingSid = null;
    render(state);
    endViewLoad();
  } catch (e) {
    console.error("dashboard poll/render failed:", e);
    const msg = String(e && e.message ? e.message : e).slice(0, 160);
    const net = /HTTP |Failed to fetch|NetworkError|Load failed/i.test(msg)
      || (e && e.name === "TypeError");
    $("liveBadge").textContent = net ? "offline" : "error";
    $("liveBadge").className = "badge warn";
    const meta = $("sessionMeta");
    if (meta) meta.textContent = msg;
    showBanner(
      net
        ? "Dashboard offline — last numbers kept on screen."
        : "Dashboard render error: " + msg,
      "error",
    );
    endViewLoad();
  } finally {
    _pollBusy = false;
    if (_pollAgain) {
      _pollAgain = false;
      poll();
    }
  }
}

function restorePrefs() {
  try {
    const dens = localStorage.getItem("tt-tree-density");
    if (dens) setTreeDensity(dens);
    else setTreeDensity("standard");
    const unit = localStorage.getItem("tt-cost-unit");
    if (unit === "tokens" || unit === "usd") setCostUnit(unit);
    let stack = localStorage.getItem("tt-cost-stack");
    if (!stack && localStorage.getItem("tt-cost-detail") === "1") stack = "parts";
    if (stack === "io" || stack === "parts" || stack === "tools")
      window.__costChart.stack = stack;
    const byLabel = localStorage.getItem("tt-cost-bylabel");
    if (byLabel === "1") window.__costChart.byLabel = true;
    _graphMode = FEATURES.agentAnimationGraph && localStorage.getItem("tt-ctx-graph") === "1";
    _rateMode =
      FEATURES.toksPerSec && !_graphMode && localStorage.getItem("tt-ctx-rate") === "1";
    const rg = localStorage.getItem("tt-ctx-rate-grain");
    if (rg === "round" || rg === "call") _ctxRateGrain = rg;
    syncCostChrome();
    syncCtxChrome();
    applyGraphHeight(storedGraphH());
  } catch {
    setTreeDensity("standard");
  }
  restoreScope();
  // Period always opens on $ (hourglass/tok/s are session-local only after open).
  if (isPeriodScope()) setCostUnit("usd");
  restorePanelCollapse();
  applyFeatureGates();
}

function restorePanelCollapse() {
  ["ctxPanel", "costPanel"].forEach((id) => {
    try {
      if (localStorage.getItem("tt-collapse-" + id) === "1") {
        const el = $(id);
        if (el) el.classList.add("is-collapsed");
      }
    } catch { /* ignore */ }
  });
}

function redrawExpandedPanel(id) {
  // Layout must settle (display:none → visible) before canvas DPR/size or we get stretch/blur.
  void document.body.offsetHeight;
  requestAnimationFrame(() => {
    if (id === "ctxPanel") {
      if (_graphMode) applyGraphHeight(storedGraphH());
      else if (window.__lastState) paintCtxChart(window.__lastState);
      return;
    }
    if (id === "costPanel") {
      if (isPeriodScope()) redrawPeriod();
      else {
        const st = window.__costChart;
        if (st) drawBars($("costChart"), st.turns, st.rounds);
      }
    }
  });
}

function bindPanelCollapse() {
  document.querySelectorAll(".panel-head-toggle[data-collapse-panel]").forEach((head) => {
    if (head._collapseBound) return;
    head._collapseBound = true;
    head.addEventListener("click", (ev) => {
      if (ev.target.closest("button, .unit-toggle, .period-nav, a, select, input")) return;
      const id = head.getAttribute("data-collapse-panel");
      const panel = id && $(id);
      if (!panel) return;
      panel.classList.toggle("is-collapsed");
      const collapsed = panel.classList.contains("is-collapsed");
      try {
        localStorage.setItem("tt-collapse-" + id, collapsed ? "1" : "0");
      } catch { /* ignore */ }
      if (!collapsed) redrawExpandedPanel(id);
    });
  });
}

bindPoll(poll);
bindPeriodPoll(poll);
restorePrefs();
bindPeriodControls();
bindPanelCollapse();
bindOfficialFlip();
bindCtxChartResize(() => {
  if (window.__lastState) paintCtxChart(window.__lastState);
});

$("cacheReset")?.addEventListener("click", async () => {
  const btn = $("cacheReset");
  if (btn) btn.disabled = true;
  beginViewLoad();
  try {
    await fetch("/api/cache/reset", { method: "POST" });
  } catch { /* ignore */ }
  _pollEpoch += 1;
  _pollBusy = false;
  try {
    await poll();
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("sessionSelect")?.addEventListener("change", (ev) => {
  const sid = ev.target.value || null;
  // D/W/M: picking a session must leave period and open that session (not only POST /api/session).
  if (isPeriodScope()) {
    if (sid) {
      openSessionFromPeriod(sid);
      return;
    }
    beginViewLoad();
    setScope("session");
    switchSession(null);
    return;
  }
  beginViewLoad();
  switchSession(sid);
});

function syncCostChrome() {
  const st = window.__costChart || {};
  const stack = st.stack || "io";
  ["stackIo", "stackParts", "stackTools"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    const on = el.dataset.stack === stack;
    el.classList.toggle("active", on);
    el.setAttribute("aria-pressed", on ? "true" : "false");
  });
  const r = $("costLayoutRounds");
  const l = $("costLayoutLabel");
  if (r) {
    r.classList.toggle("active", !st.byLabel);
    r.setAttribute("aria-pressed", !st.byLabel ? "true" : "false");
  }
  if (l) {
    l.classList.toggle("active", !!st.byLabel);
    l.setAttribute("aria-pressed", st.byLabel ? "true" : "false");
  }
}

function setCostStack(stack) {
  window.__costChart.stack = stack;
  try { localStorage.setItem("tt-cost-stack", stack); } catch { /* ignore */ }
  syncCostChrome();
  const st = window.__costChart;
  if (st) drawBars($("costChart"), st.turns, st.rounds);
}

function setCostByLabel(on) {
  window.__costChart.byLabel = !!on;
  try { localStorage.setItem("tt-cost-bylabel", on ? "1" : "0"); } catch { /* ignore */ }
  syncCostChrome();
  const st = window.__costChart;
  if (st) drawBars($("costChart"), st.turns, st.rounds);
}

$("stackIo")?.addEventListener("click", () => setCostStack("io"));
$("stackParts")?.addEventListener("click", () => setCostStack("parts"));
$("stackTools")?.addEventListener("click", () => setCostStack("tools"));
$("costLayoutRounds")?.addEventListener("click", () => setCostByLabel(false));
$("costLayoutLabel")?.addEventListener("click", () => setCostByLabel(true));
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

$("ctxModeTokens")?.addEventListener("click", () => setCtxMode("tokens"));
$("ctxModeRate")?.addEventListener("click", () => setCtxMode("rate"));
$("ctxModeGraph")?.addEventListener("click", () => setCtxMode("graph"));
$("ctxRateCall")?.addEventListener("click", () => {
  _ctxRateGrain = "call";
  hideAllChartTips();
  persistCtxMode();
  syncCtxChrome();
  if (window.__lastState) paintCtxChart(window.__lastState);
});
$("ctxRateRound")?.addEventListener("click", () => {
  _ctxRateGrain = "round";
  hideAllChartTips();
  persistCtxMode();
  syncCtxChrome();
  if (window.__lastState) paintCtxChart(window.__lastState);
});
bindCtxGraphResize();

poll();
setInterval(poll, 1000);
window.addEventListener("resize", () => {
  if (isPeriodScope()) {
    redrawPeriod();
    return;
  }
  const st = window.__lastState;
  if (!st) return;
  if (_graphMode) {
    if (_graphEngine) _graphEngine.resize();
  } else {
    paintCtxChart(st);
  }
  drawBars($("costChart"), st.turns || [], st.rounds || []);
});
