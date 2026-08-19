/** Daily / weekly / monthly aggregate view. */
import { $, fmtTokens, fmtUsd, esc, joinParts, totalPrice } from './fmt.js';
import { drawAggBars, drawTimeline, setCostUnit, hideAllChartTips, onGanttSelect, fitGanttToSessions } from './charts.js';
import { switchSession } from './sessions.js';

const PERIODS = new Set(["daily", "weekly", "monthly"]);
const GRAINS = {
  daily: [
    { id: "hour", label: "Hourly" },
    { id: "15m", label: "15 min" },
  ],
  weekly: [
    { id: "hour", label: "Hourly" },
    { id: "day", label: "Daily" },
  ],
  monthly: [
    { id: "day", label: "Daily" },
    { id: "week", label: "Weekly" },
  ],
};
const GRAIN_DEFAULT = { daily: "hour", weekly: "day", monthly: "day" };

let _scope = "session";
let _offset = 0;
let _grain = "hour";
let _mode = "timeframe";
let _byLabel = false;
let _stack = "io";
let _timeline = false;
let _pollRef = null;
let _lastAgg = null;
let _ganttSel = new Set();
let _periodReturn = null;

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
  } catch { /* ignore */ }
}

export function restoreScope() {
  try {
    const s = localStorage.getItem("tt-scope");
    if (PERIODS.has(s) || s === "session") _scope = s;
    const m = localStorage.getItem("tt-agg-mode");
    if (m === "cumulative" || m === "timeframe") _mode = m;
    _byLabel = localStorage.getItem("tt-agg-bylabel") === "1";
    const sk = localStorage.getItem("tt-agg-stack");
    if (sk === "io" || sk === "parts" || sk === "tools") _stack = sk;
    _timeline = localStorage.getItem("tt-agg-timeline") === "1";
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
  if (_scope === "daily") return _grain === "15m" ? "15-min usage" : "Hourly usage";
  if (_scope === "weekly") return _grain === "hour" ? "Hourly usage" : "Daily usage";
  if (_scope === "monthly") return _grain === "week" ? "Weekly usage" : "Daily usage";
  return "Cost per round";
}

function applyScopeChrome() {
  document.body.classList.toggle("scope-period", isPeriodScope());
  const sel = $("scopeSelect");
  if (sel && sel.value !== _scope) sel.value = _scope;
  const tb = $("periodToolbar");
  if (tb) tb.hidden = !isPeriodScope();
  const nav = $("periodNav");
  if (nav) nav.hidden = !isPeriodScope();
  _grain = normalizeGrain(_scope, _grain);
  const grainWrap = $("periodGrain");
  const opts = GRAINS[_scope] || [];
  if (grainWrap) {
    grainWrap.hidden = !isPeriodScope() || opts.length < 2;
    const btns = [ $("grainA"), $("grainB") ];
    opts.forEach((opt, i) => {
      const btn = btns[i];
      if (!btn) return;
      btn.hidden = false;
      btn.dataset.grain = opt.id;
      btn.textContent = opt.label;
      const on = _grain === opt.id;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    btns.forEach((btn, i) => {
      if (btn && i >= opts.length) btn.hidden = true;
    });
  }
  const title = $("costPanelTitle");
  if (title) title.textContent = grainTitle();
  const treeTitle = $("treeTitle");
  if (treeTitle) treeTitle.textContent = isPeriodScope() ? "Sessions" : "Round hierarchy";
  const tf = $("aggModeTf");
  const cu = $("aggModeCum");
  if (tf) {
    tf.classList.toggle("active", _mode !== "cumulative");
    tf.setAttribute("aria-pressed", _mode !== "cumulative" ? "true" : "false");
  }
  if (cu) {
    cu.classList.toggle("active", _mode === "cumulative");
    cu.setAttribute("aria-pressed", _mode === "cumulative" ? "true" : "false");
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
  const timeBtn = $("aggUnitTime");
  if (usdBtn) {
    usdBtn.disabled = false;
    usdBtn.classList.toggle("active", !_timeline && unit !== "tokens");
    usdBtn.setAttribute("aria-pressed", (!_timeline && unit !== "tokens") ? "true" : "false");
  }
  if (tokBtn) {
    tokBtn.disabled = false;
    tokBtn.classList.toggle("active", !_timeline && unit === "tokens");
    tokBtn.setAttribute("aria-pressed", (!_timeline && unit === "tokens") ? "true" : "false");
  }
  if (timeBtn) {
    timeBtn.classList.toggle("active", !!_timeline);
    timeBtn.setAttribute("aria-pressed", _timeline ? "true" : "false");
  }
  if (!_timeline) {
    const rst = $("ganttReset");
    if (rst) rst.hidden = true;
  } else {
    syncGanttSelChrome();
  }
  const ganttOff = !!_timeline;
  const lockTime = !!_byLabel || ganttOff;
  ["periodGrain", "aggTfCum"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.classList.toggle("is-disabled", lockTime);
    el.setAttribute("aria-disabled", lockTime ? "true" : "false");
    el.querySelectorAll("button").forEach((b) => { b.disabled = lockTime; });
  });
  ["aggStackIo", "aggLayoutTime"].forEach((id) => {
    const wrap = $(id)?.closest(".unit-toggle");
    if (!wrap) return;
    wrap.classList.toggle("is-disabled", ganttOff);
    wrap.setAttribute("aria-disabled", ganttOff ? "true" : "false");
    wrap.querySelectorAll("button").forEach((b) => { b.disabled = ganttOff; });
  });
  if (grainWrap) {
    grainWrap.title = lockTime
      ? (ganttOff ? "Grain hidden in timeline" : "Grain applies to Time layout only")
      : "Bar grain";
  }
  const tfCum = $("aggTfCum");
  if (tfCum) {
    tfCum.title = lockTime
      ? "Timeframe / Cumulative apply to Time layout only"
      : "Bar values";
  }
}

export function beginViewLoad() {
  document.body.classList.add("is-loading");
  const el = $("viewLoader");
  if (el) el.hidden = false;
}

export function endViewLoad() {
  document.body.classList.remove("is-loading");
  const el = $("viewLoader");
  if (el) el.hidden = true;
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
      mode: _mode,
      stack: _stack,
      byLabel: _byLabel,
    };
  }
  beginViewLoad();
  setScope("session");
  switchSession(sid);
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
  _mode = snap.mode === "cumulative" ? "cumulative" : "timeframe";
  _stack = snap.stack || "io";
  _byLabel = !!snap.byLabel;
  persist();
  applyScopeChrome();
  if (_pollRef) _pollRef();
}

export function setScope(scope) {
  const next = PERIODS.has(scope) || scope === "session" ? scope : "session";
  if (next !== _scope) {
    beginViewLoad();
    if (PERIODS.has(next)) _periodReturn = null;
    _scope = next;
    _offset = 0;
    _grain = normalizeGrain(_scope, _grain);
    hideAllChartTips();
    if (next === "session") _ganttSel = new Set();
    syncPeriodBack();
  }
  persist();
  applyScopeChrome();
  if (_pollRef) _pollRef();
}

function setOffset(delta) {
  _offset += delta;
  if (_offset > 0) _offset = 0;
  applyScopeChrome();
  if (_pollRef) _pollRef();
}

function setGrain(g) {
  _grain = normalizeGrain(_scope, g);
  persist();
  applyScopeChrome();
  if (_pollRef) _pollRef();
}

function setMode(m) {
  _mode = m === "cumulative" ? "cumulative" : "timeframe";
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
  if (s1) { s1.textContent = fmtUsd(tot.cost_in_usd); s1.className = "sub"; }
  if (s2) { s2.textContent = fmtUsd(tot.cost_cached_usd); s2.className = "sub"; }
  if (s3) { s3.textContent = fmtUsd(tot.cost_out_usd); s3.className = "sub"; }
  if (s4) { s4.textContent = fmtUsd(tot.official_usd); s4.className = "sub"; }
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
}

function ganttGroupIds(sid, sessions) {
  const id = String(sid || "").toLowerCase();
  const s = (sessions || []).find((x) => String(x.session_id).toLowerCase() === id);
  if (!s) return [id];
  if (s.depth > 0 || s.session_kind === "subagent") return [id];
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
    const sub = (s.session_kind === "subagent" || Number(s.depth) > 0) ? " is-sub" : "";
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
      <span class="sess-price">${totalPrice(s.official_usd)}</span>
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
  $("sessionMeta").innerHTML = `<code title="${esc(agg.start || "")} → ${esc(agg.end || "")}">${esc(agg.label || "")}</code>`;
  const lab = $("periodLabel");
  if (lab) lab.textContent = agg.label || "—";
  const next = $("periodNext");
  if (next) next.disabled = _offset >= 0;
  paintCards(agg.totals || {});
  const unit = (window.__costChart && window.__costChart.unit) || "usd";
  if (_timeline) {
    if (!window.__aggChart) window.__aggChart = {};
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
    drawAggBars($("costChart"), agg.buckets || [], {
      unit,
      cumulative: _mode === "cumulative",
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

export async function fetchPeriod() {
  const badge = $("liveBadge");
  if (badge && !_lastAgg) {
    badge.textContent = "loading";
    badge.className = "badge idle";
  }
  const url = `/api/aggregate?period=${encodeURIComponent(_scope)}&offset=${_offset}&grain=${encodeURIComponent(_grain)}&stack=${encodeURIComponent(_stack)}&_=${Date.now()}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error("HTTP " + r.status);
  const agg = await r.json();
  if (!isPeriodScope()) return;
  paintPeriod(agg);
  endViewLoad();
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
  $("grainA")?.addEventListener("click", (ev) => setGrain(ev.currentTarget.dataset.grain));
  $("grainB")?.addEventListener("click", (ev) => setGrain(ev.currentTarget.dataset.grain));
  $("aggModeTf")?.addEventListener("click", () => setMode("timeframe"));
  $("aggModeCum")?.addEventListener("click", () => setMode("cumulative"));
  const setAggStack = (s) => {
    _stack = s;
    persist();
    applyScopeChrome();
    if (_pollRef) _pollRef();
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
    persist();
    applyScopeChrome();
    setCostUnit("usd");
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitTok")?.addEventListener("click", () => {
    _timeline = false;
    persist();
    applyScopeChrome();
    setCostUnit("tokens");
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("aggUnitTime")?.addEventListener("click", () => {
    _timeline = true;
    persist();
    applyScopeChrome();
    if (_lastAgg) paintPeriod(_lastAgg);
  });
  $("ganttReset")?.addEventListener("click", () => clearGanttSel());
  $("periodBack")?.addEventListener("click", () => restorePeriodReturn());
  onGanttSelect((sid) => toggleGanttSel(sid));
}
