/** Daily / weekly / monthly aggregate view. */
import { $, fmtTokens, fmtUsd, esc, joinParts, totalPrice, isSubagentKind, ratesPerMFromIoCosts } from './fmt.js';
import { drawAggBars, drawTimeline, setCostUnit, hideAllChartTips, onGanttSelect, fitGanttToSessions, drawRateChart, drawIoStepChart, clearRateHost } from './charts.js';
import { switchSession } from './sessions.js';
import { FEATURES } from './features.js';

const PERIODS = new Set(["daily", "weekly", "monthly"]);
const GRAINS = {
  daily: [
    { id: "hour", label: "Hourly" },
    { id: "15m", label: "15 min" },
    { id: "session", label: "Session" },
  ],
  weekly: [
    { id: "hour", label: "Hourly" },
    { id: "day", label: "Daily" },
    { id: "session", label: "Session" },
  ],
  monthly: [
    { id: "day", label: "Daily" },
    { id: "week", label: "Weekly" },
    { id: "session", label: "Session" },
  ],
};
const GRAIN_DEFAULT = { daily: "hour", weekly: "day", monthly: "day" };
const AGG_MODES = new Set(["timeframe", "cumulative", "normalized"]);

let _scope = "session";
let _offset = 0;
let _grain = "hour";
let _mode = "timeframe";
let _byLabel = false;
let _stack = "io";
let _timeline = false;
let _rate = false;
let _ioStep = false;
let _rateGrain = "session";
let _pollRef = null;
let _lastAgg = null;
let _ganttSel = new Set();
let _periodReturn = null;
/** @type {AbortController | null} */
let _aggAbort = null;
/** @type {"full" | "chart" | null} */
let _aggLoadMode = null;
/** Last successfully painted aggregate key (scope|offset|grain|stack|rate). */
let _aggKey = "";
/** True when scope/grain/offset/rate changed and needs a fresh fetch + loader. */
let _aggDirty = true;
/**
 * Chart refetch waiting on first SSE progress: show loader only if cold>0
 * (attr cache miss / rebuild). Warm cache → no spinner.
 */
let _chartLoaderAwaitCold = false;

function aggRequestKey() {
  return [_scope, _offset, _grain, _stack, _rate ? 1 : 0].join("|");
}

export function bindPeriodPoll(fn) {
  _pollRef = fn;
}

export function currentScope() {
  return _scope;
}

export function isPeriodScope() {
  return PERIODS.has(_scope);
}

function persist() {
  try {
    localStorage.setItem("tt-scope", _scope);
    localStorage.setItem("tt-agg-mode", _mode);
    localStorage.setItem("tt-period-grain", _grain);
    localStorage.setItem("tt-agg-bylabel", _byLabel ? "1" : "0");
    localStorage.setItem("tt-agg-stack", _stack);
    localStorage.setItem("tt-agg-timeline", _timeline ? "1" : "0");
    localStorage.setItem("tt-agg-rate", _rate ? "1" : "0");
    localStorage.setItem("tt-agg-io-step", _ioStep ? "1" : "0");
    localStorage.setItem("tt-agg-rate-grain", _rateGrain);
  } catch { /* ignore */ }
}

export function restoreScope() {
  try {
    const s = localStorage.getItem("tt-scope");
    if (PERIODS.has(s) || s === "session") _scope = s;
    const m = localStorage.getItem("tt-agg-mode");
    if (AGG_MODES.has(m)) _mode = m;
    _byLabel = localStorage.getItem("tt-agg-bylabel") === "1";
    const sk = localStorage.getItem("tt-agg-stack");
    if (sk === "io" || sk === "parts" || sk === "tools") _stack = sk;
    // Always open on $ — never restore hourglass / tok/s / I/O$ from a prior visit.
    // Gated surfaces (gantt / tok/s) stay off even if prefs linger.
    _timeline = false;
    _rate = false;
    _ioStep = false;
    const rg = localStorage.getItem("tt-agg-rate-grain");
    if (rg === "round" || rg === "session") _rateGrain = rg;
    const g = localStorage.getItem("tt-period-grain") || localStorage.getItem("tt-monthly-grain");
    if (g) _grain = normalizeGrain(_scope, g);
  } catch { /* ignore */ }
  applyScopeChrome();
}

function normalizeGrain(scope, grain) {
  const allowed = (GRAINS[scope] || []).map((g) => g.id);
  if (allowed.includes(grain)) return grain;
  return GRAIN_DEFAULT[scope] || "hour";
}

function grainTitle() {
  // Session cost chart — never leave a stale period "Tokens per second" title.
  if (!isPeriodScope()) return "Cost Per Turn";
  if (_ioStep) return "Official I/O $/M / session";
  if (_rate) return "Tokens per second";
  if (_grain === "session") return "Per-session usage";
  if (_scope === "daily") return _grain === "15m" ? "15-min usage" : "Hourly usage";
  if (_scope === "weekly") return _grain === "hour" ? "Hourly usage" : "Daily usage";
  if (_scope === "monthly") return _grain === "week" ? "Weekly usage" : "Daily usage";
  return "Cost Per Turn";
}

function applyScopeChrome() {
  if (!FEATURES.gantt) _timeline = false;
  if (!FEATURES.toksPerSec) _rate = false;
  if (!FEATURES.periodIoPriceStep) _ioStep = false;
  document.body.classList.toggle("scope-period", isPeriodScope());
  const sel = $("scopeSelect");
  if (sel && sel.value !== _scope) sel.value = _scope;
  const tb = $("periodToolbar");
  if (tb) tb.hidden = !isPeriodScope();
  const nav = $("periodNav");
  if (nav) nav.hidden = !isPeriodScope();
  _grain = normalizeGrain(_scope, _grain);
  const ganttOff = !!_timeline || !!_rate || !!_ioStep;
  const showTimeExtras = isPeriodScope() && !ganttOff && !_byLabel;
  const showLayout = isPeriodScope() && !ganttOff;
  const showStack = isPeriodScope() && !ganttOff;

  const grainWrap = $("periodGrain");
  const opts = GRAINS[_scope] || [];
  if (grainWrap) {
    grainWrap.hidden = !showTimeExtras || opts.length < 2;
    if (!grainWrap.hidden) {
      grainWrap.innerHTML = opts.map((opt) => {
        const on = _grain === opt.id;
        return `<button type="button" class="unit-btn${on ? " active" : ""}" data-grain="${opt.id}" aria-pressed="${on ? "true" : "false"}">${opt.label}</button>`;
      }).join("");
    }
    grainWrap.title = "Bar grain";
  }
  const title = $("costPanelTitle");
  if (title) title.textContent = grainTitle();
  const treeTitle = $("treeTitle");
  if (treeTitle) treeTitle.textContent = isPeriodScope() ? "Sessions" : "Round hierarchy";
  const tf = $("aggModeTf");
  const cu = $("aggModeCum");
  const nm = $("aggModeNorm");
  if (tf) {
    tf.classList.toggle("active", _mode === "timeframe");
    tf.setAttribute("aria-pressed", _mode === "timeframe" ? "true" : "false");
  }
  if (cu) {
    cu.classList.toggle("active", _mode === "cumulative");
    cu.setAttribute("aria-pressed", _mode === "cumulative" ? "true" : "false");
  }
  if (nm) {
    nm.classList.toggle("active", _mode === "normalized");
    nm.setAttribute("aria-pressed", _mode === "normalized" ? "true" : "false");
  }
  ["aggStackIo", "aggStackParts", "aggStackTools"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    const on = el.dataset.stack === _stack;
    el.classList.toggle("active", on);
    el.setAttribute("aria-pressed", on ? "true" : "false");
  });
  const t = $("aggLayoutTime");
  const l = $("aggLayoutLabel");
  if (t) {
    t.classList.toggle("active", !_byLabel);
    t.setAttribute("aria-pressed", !_byLabel ? "true" : "false");
  }
  if (l) {
    l.classList.toggle("active", !!_byLabel);
    l.setAttribute("aria-pressed", _byLabel ? "true" : "false");
  }
  const next = $("periodNext");
  if (next) next.disabled = _offset >= 0;
  const unit = (window.__costChart && window.__costChart.unit) || "usd";
  const usdBtn = $("aggUnitUsd");
  const tokBtn = $("aggUnitTok");
  const ioStepBtn = $("aggUnitIoStep");
  const timeBtn = $("aggUnitTime");
  const rateBtn = $("aggUnitRate");
  if (usdBtn) {
    usdBtn.disabled = false;
    usdBtn.classList.toggle("active", !_timeline && !_rate && !_ioStep && unit !== "tokens");
    usdBtn.setAttribute("aria-pressed", (!_timeline && !_rate && !_ioStep && unit !== "tokens") ? "true" : "false");
  }
  if (tokBtn) {
    tokBtn.disabled = false;
    tokBtn.classList.toggle("active", !_timeline && !_rate && !_ioStep && unit === "tokens");
    tokBtn.setAttribute("aria-pressed", (!_timeline && !_rate && !_ioStep && unit === "tokens") ? "true" : "false");
  }
  if (ioStepBtn) {
    ioStepBtn.hidden = !FEATURES.periodIoPriceStep;
    ioStepBtn.classList.toggle("active", !!_ioStep);
    ioStepBtn.setAttribute("aria-pressed", _ioStep ? "true" : "false");
  }
  if (timeBtn) {
    timeBtn.hidden = !FEATURES.gantt;
    timeBtn.classList.toggle("active", !!_timeline && !_rate && !_ioStep);
    timeBtn.setAttribute("aria-pressed", (_timeline && !_rate && !_ioStep) ? "true" : "false");
  }
  if (rateBtn) {
    rateBtn.hidden = !FEATURES.toksPerSec;
    rateBtn.classList.toggle("active", !!_rate && !_ioStep);
    rateBtn.setAttribute("aria-pressed", (_rate && !_ioStep) ? "true" : "false");
  }
  const rateGrain = $("periodRateGrain");
  if (rateGrain) {
    rateGrain.hidden = !_rate || !isPeriodScope();
    const sBtn = $("aggRateSession");
    const rBtn = $("aggRateRound");
    if (sBtn) {
      sBtn.classList.toggle("active", _rateGrain !== "round");
      sBtn.setAttribute("aria-pressed", _rateGrain !== "round" ? "true" : "false");
    }
    if (rBtn) {
      rBtn.classList.toggle("active", _rateGrain === "round");
      rBtn.setAttribute("aria-pressed", _rateGrain === "round" ? "true" : "false");
    }
  }
  // Hide unused groups (no grayed-out chrome).
  const layoutToggle = $("aggLayoutToggle");
  if (layoutToggle) layoutToggle.hidden = !showLayout;
  const tfCum = $("aggTfCum");
  if (tfCum) {
    tfCum.hidden = !showTimeExtras;
    tfCum.title = "Bar values";
  }
  const stackToggle = $("aggStackToggle");
  if (stackToggle) stackToggle.hidden = !showStack;

  if (!_timeline) {
    const rst = $("ganttReset");
    if (rst) rst.hidden = true;
  } else {
    syncGanttSelChrome();
  }
}

function setAggProgress(done, total, meta) {
  const n = Math.max(0, Number(done) || 0);
  const t = Math.max(0, Number(total) || 0);
  const text = t > 0 ? `${n}/${t}` : "";
  // Chart-option refetch: spinner only when at least one session rebuilds cache.
  if (_chartLoaderAwaitCold) {
    _chartLoaderAwaitCold = false;
    const cold = meta && meta.cold != null ? Number(meta.cold) : 0;
    if (cold > 0) beginChartLoad();
  }
  const lab = $("viewLoaderLab");
  // Only rewrite the full-page label once aggregate progress starts (keep
  // "Loading…" for session switches / cache reset).
  if (lab && _aggLoadMode === "full" && t > 0) {
    lab.innerHTML = `Calculating sessions… <span class="view-loader-prog">${text}</span>`;
  }
  const cp = $("costChartProg");
  if (cp) cp.textContent = text || "…";
}

export function beginViewLoad() {
  endChartLoad();
  _aggLoadMode = "full";
  const lab = $("viewLoaderLab");
  if (lab) lab.textContent = "Loading…";
  const cp = $("costChartProg");
  if (cp) cp.textContent = "…";
  document.body.classList.add("is-loading");
  const el = $("viewLoader");
  if (el) el.hidden = false;
}

export function endViewLoad() {
  document.body.classList.remove("is-loading");
  const el = $("viewLoader");
  if (el) el.hidden = true;
  if (_aggLoadMode === "full") _aggLoadMode = null;
}

export function beginChartLoad() {
  // Prefer chart-local spinner when period data is already on screen.
  endViewLoad();
  _aggLoadMode = "chart";
  const cp = $("costChartProg");
  if (cp) cp.textContent = "…";
  const el = $("costChartLoader");
  if (el) el.hidden = false;
  const wrap = $("costChartWrap");
  if (wrap) wrap.classList.add("is-chart-loading");
}

export function endChartLoad() {
  const el = $("costChartLoader");
  if (el) el.hidden = true;
  const wrap = $("costChartWrap");
  if (wrap) wrap.classList.remove("is-chart-loading");
  if (_aggLoadMode === "chart") _aggLoadMode = null;
}

function endAggLoaders() {
  endChartLoad();
  endViewLoad();
  _aggLoadMode = null;
}

function syncPeriodBack() {
  const btn = $("periodBack");
  if (!btn) return;
  const show = !isPeriodScope() && !!_periodReturn;
  btn.hidden = !show;
  if (show) btn.textContent = "← Back";
}

export function openSessionFromPeriod(sid) {
  if (PERIODS.has(_scope)) {
    _periodReturn = {
      scope: _scope,
      offset: _offset,
      grain: _grain,
      timeline: _timeline,
      rate: _rate,
      ioStep: _ioStep,
      rateGrain: _rateGrain,
      mode: _mode,
      stack: _stack,
      byLabel: _byLabel,
    };
  }
  beginViewLoad();
  // Do not kick a parallel /api/state poll before POST /api/session finishes —
  // that race left is-loading stuck when __pendingSid never matched.
  setScope("session", { poll: false });
  const want = String(sid || "").toLowerCase();
  const row = (_lastAgg && _lastAgg.sessions || []).find(
    (s) => String(s.session_id || "").toLowerCase() === want
  );
  let target = sid;
  let focusSub = null;
  if (row && (Number(row.depth) > 0 || isSubagentKind(row.session_kind))) {
    // Never open the dedicated sub-agent session page — parent + Sub N tab.
    target = row.parent_id || sid;
    focusSub = row.session_id;
  }
  window.__pendingTaskTab = focusSub || "main";
  switchSession(target);
  syncPeriodBack();
}

export function restorePeriodReturn() {
  const snap = _periodReturn;
  _periodReturn = null;
  syncPeriodBack();
  if (!snap) return;
  beginViewLoad();
  _scope = snap.scope;
  _offset = snap.offset || 0;
  _grain = normalizeGrain(_scope, snap.grain);
  _timeline = !!snap.timeline;
  _rate = !!snap.rate;
  _ioStep = !!snap.ioStep && !!FEATURES.periodIoPriceStep;
  if (_rate || _ioStep) _timeline = false;
  if (_ioStep) _rate = false;
  _rateGrain = snap.rateGrain === "round" ? "round" : "session";
  _mode = AGG_MODES.has(snap.mode) ? snap.mode : "timeframe";
  _stack = snap.stack || "io";
  _byLabel = !!snap.byLabel;
  _aggDirty = true;
  _aggKey = "";
  persist();
  applyScopeChrome();
  if (_pollRef) _pollRef();
}

export function setScope(scope, opts = {}) {
  const next = PERIODS.has(scope) || scope === "session" ? scope : "session";
  if (next !== _scope) {
    beginViewLoad();
    if (PERIODS.has(next)) {
      _periodReturn = null;
      _aggDirty = true;
      _aggKey = "";
    }
    _scope = next;
    _offset = 0;
    _grain = normalizeGrain(_scope, _grain);
    hideAllChartTips();
    if (next === "session") _ganttSel = new Set();
    syncPeriodBack();
  }
  persist();
  applyScopeChrome();
  if (opts.poll === false) return;
  if (_pollRef) _pollRef();
}

function requestAggFetch({ mode } = {}) {
  // Full-page on first period paint; chart overlay only if SSE reports cold cache work.
  _aggDirty = true;
  // Abort any quiet/in-flight stream so a grain switch does not paint stale data.
  if (_aggAbort) {
    try { _aggAbort.abort(); } catch { /* ignore */ }
    _aggAbort = null;
  }
  const m = mode || (_lastAgg ? "chart" : "full");
  if (m === "chart") {
    endChartLoad();
    _aggLoadMode = null;
    _chartLoaderAwaitCold = true;
  } else {
    _chartLoaderAwaitCold = false;
    beginViewLoad();
  }
  if (_pollRef) _pollRef();
}

function setOffset(delta) {
  _offset += delta;
  if (_offset > 0) _offset = 0;
  applyScopeChrome();
  requestAggFetch();
}

function setGrain(g) {
  _grain = normalizeGrain(_scope, g);
  persist();
  applyScopeChrome();
  requestAggFetch();
}

function setMode(m) {
  _mode = AGG_MODES.has(m) ? m : "timeframe";
  persist();
  applyScopeChrome();
  if (_lastAgg) paintPeriod(_lastAgg);
}

function paintCards(tot) {
  const l1 = $("kpi1Label");
  const l2 = $("kpi2Label");
  const l3 = $("kpi3Label");
  const l4 = $("kpi4Label");
  if (l1) l1.textContent = "Total In";
  if (l2) l2.textContent = "Total Cached";
  if (l3) l3.textContent = "Total Out";
  if (l4) l4.textContent = "Total All";
  $("ctxNow").textContent = fmtTokens(tot.tokens_in);
  $("costOfficial").textContent = fmtTokens(tot.tokens_cached);
  $("costEstimate").textContent = fmtTokens(tot.tokens_out);
  $("genRate").textContent = fmtTokens(tot.tokens_all);
  const s1 = $("ctxSub");
  const s2 = $("costOfficialSub");
  const s3 = $("costEstimateSub");
  const s4 = $("genRateSub");
  const allUsd =
    (Number(tot.cost_in_usd) || 0)
    + (Number(tot.cost_cached_usd) || 0)
    + (Number(tot.cost_out_usd) || 0);
  if (s1) { s1.textContent = fmtUsd(tot.cost_in_usd); s1.className = "sub"; }
  if (s2) { s2.textContent = fmtUsd(tot.cost_cached_usd); s2.className = "sub"; }
  if (s3) { s3.textContent = fmtUsd(tot.cost_out_usd); s3.className = "sub"; }
  // Match the three I/O cards (not official API bill, which can diverge).
  if (s4) { s4.textContent = fmtUsd(allUsd); s4.className = "sub"; }
  // Period reuses kpi2 — disable session flip chrome; amber = Cached.
  const kpi2 = $("kpi2");
  if (kpi2) {
    kpi2.classList.add("card-flip-off", "amber");
    kpi2.classList.remove("is-flipped", "green");
    kpi2.setAttribute("aria-pressed", "false");
    kpi2.removeAttribute("title");
    kpi2.tabIndex = -1;
  }
}

function restoreSessionCardLabels() {
  const l1 = $("kpi1Label");
  const l2 = $("kpi2Label");
  const l3 = $("kpi3Label");
  const l4 = $("kpi4Label");
  if (l1) l1.textContent = "Context now (TUI)";
  if (l2) l2.textContent = "Session cost (official)";
  if (l3) l3.textContent = "Session cost (estimate)";
  if (l4) l4.textContent = "Last turn gen rate";
}

export function leavePeriodView() {
  restoreSessionCardLabels();
  hideAllChartTips();
  const kpi2 = $("kpi2");
  if (kpi2) {
    kpi2.classList.remove("card-flip-off", "amber");
    kpi2.classList.add("green");
    kpi2.title = "Flip: implied $/M ↔ session tokens";
    kpi2.tabIndex = 0;
  }
}

function ganttGroupIds(sid, sessions) {
  const id = String(sid || "").toLowerCase();
  const s = (sessions || []).find((x) => String(x.session_id).toLowerCase() === id);
  if (!s) return [id];
  if (s.depth > 0 || isSubagentKind(s.session_kind)) return [id];
  const out = [id];
  for (const c of sessions) {
    if (String(c.parent_id || "").toLowerCase() === id)
      out.push(String(c.session_id).toLowerCase());
  }
  return out;
}

function syncGanttSelChrome() {
  const btn = $("ganttReset");
  if (btn) btn.hidden = !_timeline || _ganttSel.size === 0;
}

function toggleGanttSel(sid) {
  if (!_timeline) return;
  const sessions = (_lastAgg && _lastAgg.sessions) || [];
  const group = ganttGroupIds(sid, sessions);
  const allOn = group.every((id) => _ganttSel.has(id));
  if (allOn) group.forEach((id) => _ganttSel.delete(id));
  else group.forEach((id) => _ganttSel.add(id));
  if (window.__aggChart) window.__aggChart.selected = _ganttSel;
  const picked = sessions.filter((s) => _ganttSel.has(String(s.session_id).toLowerCase()));
  if (picked.length) fitGanttToSessions(picked);
  else if (window.__aggChart) {
    window.__aggChart._gt0 = null;
    window.__aggChart._gt1 = null;
  }
  syncGanttSelChrome();
  paintSessionList(sessions);
  if (_lastAgg) drawTimeline($("costChart"), _lastAgg);
}

function clearGanttSel() {
  _ganttSel = new Set();
  if (window.__aggChart) {
    window.__aggChart.selected = _ganttSel;
    window.__aggChart._gt0 = null;
    window.__aggChart._gt1 = null;
  }
  syncGanttSelChrome();
  if (_lastAgg) {
    paintSessionList(_lastAgg.sessions || []);
    if (_timeline) drawTimeline($("costChart"), _lastAgg);
  }
}

function paintSessionList(sessions) {
  const tree = $("roundTree");
  if (!tree) return;
  tree.classList.add("sess-list");
  if (!sessions || !sessions.length) {
    tree.innerHTML = `<div class="sess-empty">No sessions in this period</div>`;
    return;
  }
  tree.innerHTML = sessions.map((s) => {
    const ledger = joinParts([
      `<span class="tok-in">In +${fmtTokens(s.tokens_in || 0)}</span> <span class="cost-in">${fmtUsd(s.cost_in_usd || 0)}</span>`,
      `<span class="tok-cached">Cached ${fmtTokens(s.tokens_cached || 0)}</span> <span class="cost-cached">${fmtUsd(s.cost_cached_usd || 0)}</span>`,
      `<span class="tok-out">Out +${fmtTokens(s.tokens_out || 0)}</span> <span class="cost-out">${fmtUsd(s.cost_out_usd || 0)}</span>`,
    ]);
    const sub = (isSubagentKind(s.session_kind) || Number(s.depth) > 0) ? " is-sub" : "";
    const name = s.label || (sub ? `Sub Agent ${s.child_n || s.n}` : `Session ${s.n}`);
    const sid = String(s.session_id);
    const picked = _timeline && _ganttSel.size > 0 && _ganttSel.has(sid.toLowerCase()) ? " is-picked" : "";
    const tip = _timeline
      ? `${name} — ${s.title || sid}\nClick to drill · Double-click to open\n${sid}`
      : `${name} — ${s.title || sid}\nClick to open\n${sid}`;
    return `<div class="sess-row${sub}${picked}" data-sid="${esc(sid)}" title="${esc(tip)}">
      <span class="sess-n" title="${esc(tip)}">${esc(name)}</span>
      <span class="sess-title" title="${esc(s.title || sid)}">${esc(s.title || "")}</span>
      <span class="sess-ledger">${ledger}</span>
      <span class="sess-price">${totalPrice(
        s.estimate_usd != null
          ? s.estimate_usd
          : (Number(s.cost_in_usd) || 0) + (Number(s.cost_cached_usd) || 0) + (Number(s.cost_out_usd) || 0)
      )}</span>
    </div>`;
  }).join("");
  tree.querySelectorAll("[data-sid]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      const sid = el.getAttribute("data-sid");
      if (!sid) return;
      if (_timeline) {
        ev.preventDefault();
        toggleGanttSel(sid);
        return;
      }
      openSessionFromPeriod(sid);
    });
    el.addEventListener("dblclick", () => {
      const sid = el.getAttribute("data-sid");
      if (!sid) return;
      openSessionFromPeriod(sid);
    });
  });
}

export function paintPeriod(agg) {
  if (!isPeriodScope()) return;
  _lastAgg = agg;
  if (!agg || agg.error) {
    showPeriodError(agg && agg.error ? String(agg.error) : "no aggregate");
    return;
  }
  $("liveBadge").textContent = "period";
  $("liveBadge").className = "badge idle";
  // Date lives in periodNav — do not duplicate in the header meta strip.
  const meta = $("sessionMeta");
  if (meta) meta.textContent = "";
  const lab = $("periodLabel");
  if (lab) lab.textContent = agg.label || "—";
  const next = $("periodNext");
  if (next) next.disabled = _offset >= 0;
  paintCards(agg.totals || {});
  const unit = (window.__costChart && window.__costChart.unit) || "usd";
  if (!window.__aggChart) window.__aggChart = {};
  window.__aggChart.rate = !!_rate;
  window.__aggChart.ioStep = !!_ioStep;
  window.__aggChart.rateGrain = _rateGrain;
  if (_ioStep) {
    window.__aggChart.timeline = false;
    window.__aggChart.ratePts = null;
    const pts = (agg.sessions || []).map((s) => {
      const depth = Number(s.depth) || 0;
      const isSub = depth > 0 || isSubagentKind(s.session_kind);
      let label = String(s.n ?? "");
      if (isSub && s.child_n != null) label = `${s.n != null ? s.n : "?"}.${s.child_n}`;
      else if (isSub) label = `↳${s.n ?? "?"}`;
      // $/M = Official $ ÷ API tokens (same as Session cost Official subline).
      const rates = ratesPerMFromIoCosts(s);
      return {
        label,
        title: s.label || s.title || label,
        session_id: s.session_id,
        kind: "session",
        n: s.n,
        child_n: s.child_n,
        depth,
        in: rates.in,
        cached: rates.cached,
        out: rates.out,
        snapped: !!rates.snapped,
        cost_in_usd: Number(s.cost_in_usd) || 0,
        cost_cached_usd: Number(s.cost_cached_usd) || 0,
        cost_out_usd: Number(s.cost_out_usd) || 0,
        tokens_in: s.tokens_in,
        tokens_cached: s.tokens_cached,
        tokens_out: s.tokens_out,
      };
    });
    drawIoStepChart($("costChart"), pts, {
      onClick: (p) => {
        if (p && p.session_id) openSessionFromPeriod(p.session_id);
      },
    });
  } else if (_rate) {
    window.__aggChart.timeline = false;
    window.__aggChart.ioStepPts = null;
    const raw = _rateGrain === "round" ? (agg.tps_rounds || []) : (agg.tps_sessions || []);
    const pts = raw.map((p) => ({
      // Server already emits Session-grain style labels (29 / 29.1 / 29 R2).
      label: p.label || (p.round != null ? `R${p.round}` : String(p.n ?? "")),
      v: p.v,
      kind: _rateGrain === "round" ? "round" : "session",
      round: p.round,
      session_id: p.session_id,
      gen_ms: p.gen_ms,
      tokens_out: p.gen_out_tokens ?? p.out,
      n: p.n,
      child_n: p.child_n,
      depth: p.depth,
    }));
    drawRateChart($("costChart"), pts, {
      host: "cost",
      grain: _rateGrain,
      color: "#7ec8ff",
      onClick: (p) => {
        if (p && p.session_id) openSessionFromPeriod(p.session_id);
      },
    });
  } else if (_timeline) {
    clearRateHost("cost");
    window.__aggChart.ioStepPts = null;
    const key = `${agg.start}|${agg.end}`;
    if (window.__aggChart._ganttPeriod !== key) {
      window.__aggChart._gt0 = null;
      window.__aggChart._gt1 = null;
      window.__aggChart._ganttPeriod = key;
    }
    window.__aggChart.selected = _ganttSel;
    drawTimeline($("costChart"), agg);
    syncGanttSelChrome();
  } else {
    clearRateHost("cost");
    window.__aggChart.ratePts = null;
    window.__aggChart.ioStepPts = null;
    drawAggBars($("costChart"), agg.buckets || [], {
      unit,
      cumulative: _mode === "cumulative",
      normalized: _mode === "normalized",
      byLabel: _byLabel,
      stack: _stack,
    });
  }
  paintSessionList(agg.sessions || []);
}

function showPeriodError(msg) {
  $("liveBadge").textContent = "error";
  $("liveBadge").className = "badge warn";
  const tree = $("roundTree");
  if (tree) tree.innerHTML = `<div class="sess-empty">${esc(msg)}</div>`;
}

async function readAggregateSse(response, onProgress) {
  if (!response.body || typeof response.body.getReader !== "function") {
    // Extremely old engines — fall back to buffering the whole SSE body.
    const text = await response.text();
    let result = null;
    for (const block of text.split("\n\n")) {
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trimStart();
      }
      if (!data) continue;
      const obj = JSON.parse(data);
      if (event === "progress" && onProgress) onProgress(obj.done, obj.total, obj);
      else if (event === "result") result = obj;
      else if (event === "error") throw new Error(obj.error || "aggregate error");
    }
    return result;
  }
  const reader = response.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let result = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trimStart();
      }
      if (!data) continue;
      let obj;
      try { obj = JSON.parse(data); } catch { continue; }
      if (event === "progress" && onProgress) onProgress(obj.done, obj.total, obj);
      else if (event === "result") result = obj;
      else if (event === "error") throw new Error(obj.error || "aggregate error");
    }
  }
  return result;
}

export async function fetchPeriod() {
  const reqKey = aggRequestKey();
  // Quiet 1s poll: same period params already on screen — do not restream / flash loader.
  if (!_aggDirty && _lastAgg && reqKey === _aggKey) {
    return;
  }

  const badge = $("liveBadge");
  if (badge && !_lastAgg) {
    badge.textContent = "loading";
    badge.className = "badge idle";
  }
  // Loader: full-page on first paint; chart overlay deferred until SSE cold>0.
  if (_aggDirty && !_aggLoadMode && !_chartLoaderAwaitCold) {
    if (_lastAgg) beginChartLoad();
    else beginViewLoad();
  }
  if (_aggAbort) {
    try { _aggAbort.abort(); } catch { /* ignore */ }
  }
  _aggAbort = new AbortController();
  const signal = _aggAbort.signal;
  // rate=1 when user is on tok/s (includes sub-agent points). Otherwise still
  // returns Parts/Tools cats + mains tps so stack switches stay local.
  const url = `/api/aggregate?period=${encodeURIComponent(_scope)}&offset=${_offset}&grain=${encodeURIComponent(_grain)}&stack=${encodeURIComponent(_stack)}&rate=${_rate ? "1" : "0"}&stream=1&_=${Date.now()}`;
  let agg = null;
  try {
    const r = await fetch(url, { signal });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const ctype = (r.headers.get("content-type") || "").toLowerCase();
    if (ctype.includes("text/event-stream")) {
      agg = await readAggregateSse(r, setAggProgress);
    } else {
      agg = await r.json();
    }
  } catch (e) {
    if (e && e.name === "AbortError") return;
    _chartLoaderAwaitCold = false;
    endAggLoaders();
    throw e;
  }
  if (!isPeriodScope()) {
    _chartLoaderAwaitCold = false;
    endAggLoaders();
    return;
  }
  // Params changed while this stream ran — keep dirty; a follow-up poll will refetch.
  if (aggRequestKey() !== reqKey) {
    _aggDirty = true;
    return;
  }
  if (!agg || agg.error) {
    _chartLoaderAwaitCold = false;
    endAggLoaders();
    throw new Error((agg && agg.error) || "empty aggregate");
  }
  paintPeriod(agg);
  _aggKey = reqKey;
  _aggDirty = false;
  _chartLoaderAwaitCold = false;
  // Hide chart-local overlay immediately; full-page spinner is cleared by poll().
  endChartLoad();
  // endViewLoad left to poll() so a superseded in-flight fetch does not hide the spinner.
}

export function redrawPeriod() {
  if (_lastAgg) paintPeriod(_lastAgg);
}

export function bindPeriodControls() {
  $("scopeSelect")?.addEventListener("change", (ev) => {
    setScope(ev.target.value || "session");
  });
  $("periodPrev")?.addEventListener("click", () => setOffset(-1));
  $("periodNext")?.addEventListener("click", () => setOffset(1));
  $("periodGrain")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-grain]");
    if (!btn || btn.disabled) return;
    const g = btn.getAttribute("data-grain");
    if (g) setGrain(g);
  });
  $("aggModeTf")?.addEventListener("click", () => setMode("timeframe"));
  $("aggModeCum")?.addEventListener("click", () => setMode("cumulative"));
  $("aggModeNorm")?.addEventListener("click", () => setMode("normalized"));
  const setAggStack = (s) => {
    _stack = s;
    persist();
    applyScopeChrome();
    // Server always fills Parts/Tools cats with the first attr pass — no refetch.
    if (_lastAgg && (_lastAgg.cats_ready || Array.isArray((_lastAgg.totals || {}).parts))) {
      paintPeriod(_lastAgg);
    } else {
      requestAggFetch();
    }
  };
  $("aggStackIo")?.addEventListener("click", () => setAggStack("io"));
  $("aggStackParts")?.addEventListener("click", () => setAggStack("parts"));
  $("aggStackTools")?.addEventListener("click", () => setAggStack("tools"));
  $("aggLayoutTime")?.addEventListener("click", () => {
    _byLabel = false;
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggLayoutLabel")?.addEventListener("click", () => {
    _byLabel = true;
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitUsd")?.addEventListener("click", () => {
    _timeline = false;
    _rate = false;
    _ioStep = false;
    // Unit must update before chrome highlight (reads __costChart.unit).
    setCostUnit("usd");
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitTok")?.addEventListener("click", () => {
    _timeline = false;
    _rate = false;
    _ioStep = false;
    setCostUnit("tokens");
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitIoStep")?.addEventListener("click", () => {
    if (!FEATURES.periodIoPriceStep) return;
    _timeline = false;
    _rate = false;
    _ioStep = true;
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitTime")?.addEventListener("click", () => {
    if (!FEATURES.gantt) return;
    _timeline = true;
    _rate = false;
    _ioStep = false;
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitRate")?.addEventListener("click", () => {
    if (!FEATURES.toksPerSec) return;
    _timeline = false;
    _rate = true;
    _ioStep = false;
    persist();
    applyScopeChrome();
    // rate_full means this payload included sub-agent tok/s (rate=1 fetch).
    // Empty [] used to look "ready" via Array.isArray — painted blank with no spinner.
    if (_lastAgg && _lastAgg.rate_full) paintPeriod(_lastAgg);
    else requestAggFetch();
  });
  $("aggRateSession")?.addEventListener("click", () => {
    _rateGrain = "session";
    hideAllChartTips();
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggRateRound")?.addEventListener("click", () => {
    _rateGrain = "round";
    hideAllChartTips();
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("ganttReset")?.addEventListener("click", () => clearGanttSel());
  $("periodBack")?.addEventListener("click", () => restorePeriodReturn());
  onGanttSelect((sid) => toggleGanttSel(sid));
}
