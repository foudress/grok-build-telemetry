/** Period Gantt timeline (gated). Extracted as-is from charts.js. */
import { $, fmtTokens, fmtUsd, esc, isSubagentKind } from './fmt.js';
import {
  CHART_AXIS,
  COST_Y_LEFT,
  PLOT_PAD_L,
  GANTT_MIN_BAR_PX,
  GANTT_MIN_SPAN,
  fitCanvas,
  drawChartEmpty,
  drawXLabel,
  hideChartTip,
  placeCostTip,
  setCostTipOwner,
  clearCostPointerProps,
  pointerInXAxis,
  clampYViewZoom,
  clampYPan,
  setGanttChrome,
  layoutGanttCanvas,
  renderCostLegend,
} from './cost-draw.js';
import { clearRateHost } from './rate-chart.js';

function niceTimeStep(spanSec, targetTicks) {
  const want = spanSec / Math.max(targetTicks, 2);
  const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  for (const s of steps) {
    if (s >= want) return s;
  }
  return 604800;
}

function isLocalMidnight(ep) {
  const d = new Date(ep * 1000);
  return d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0;
}

function fmtGanttDate(ep) {
  const d = new Date(ep * 1000);
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
}

function fmtGanttHour(ep) {
  return new Date(ep * 1000).getHours() + "h";
}

function fmtGanttHm(ep) {
  const d = new Date(ep * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function buildGanttTicks(t0, t1) {
  const span = Math.max(1, t1 - t0);
  const ticks = [];
  const seen = new Set();
  const add = (t, kind, label) => {
    const key = Math.round(Number(t));
    if (seen.has(key) || t < t0 - 1 || t > t1 + 1) return;
    seen.add(key);
    ticks.push({ t: Number(t), kind, label });
  };
  for (const m of midnightEpochs(t0, t1)) add(m, "date", fmtGanttDate(m));

  const H2 = 2 * 3600;
  if (span <= 5 * 86400) {
    let t = Math.ceil(t0 / H2) * H2;
    while (t < t1 + 1) {
      if (!isLocalMidnight(t)) add(t, "h2", fmtGanttHour(t));
      t += H2;
    }
  }

  let fine = 0;
  if (span <= 10 * 3600) fine = 3600;
  if (span <= 5 * 3600) fine = 1800;
  if (span <= 2 * 3600) fine = 900;
  if (span <= 3600) fine = 300;
  if (fine) {
    let t = Math.ceil(t0 / fine) * fine;
    while (t < t1 + 1) {
      if (!isLocalMidnight(t) && Math.round(t) % H2 !== 0)
        add(t, "fine", fine >= 3600 ? fmtGanttHour(t) : fmtGanttHm(t));
      t += fine;
    }
  }
  ticks.sort((a, b) => a.t - b.t);
  return ticks;
}

function midnightEpochs(t0, t1) {
  const out = [];
  const d = new Date(t0 * 1000);
  d.setHours(0, 0, 0, 0);
  if (d.getTime() / 1000 < t0) d.setDate(d.getDate() + 1);
  const end = t1 + 1;
  let guard = 0;
  while (d.getTime() / 1000 < end && guard++ < 400) {
    out.push(d.getTime() / 1000);
    d.setDate(d.getDate() + 1);
  }
  return out;
}

function drawYSessionLabels(rows, padT, padB, h) {
  const yAxis = $("costYAxis");
  if (!yAxis) return;
  yAxis.hidden = false;
  const w = COST_Y_LEFT;
  yAxis.style.width = w + "px";
  yAxis.style.height = h + "px";
  const { ctx } = fitCanvas(yAxis, w, h);
  ctx.clearRect(0, 0, w, h);
  let bg = "#121a24";
  const plot = $("costChart");
  if (plot) {
    const cs = getComputedStyle(plot);
    if (cs.backgroundColor && !cs.backgroundColor.includes("0, 0, 0, 0") && cs.backgroundColor !== "transparent")
      bg = cs.backgroundColor;
  }
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, w, h);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, padT, w, Math.max(1, h - padT - padB));
  ctx.clip();
  ctx.font = "11px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  const plotH = Math.max(1, h - padT - padB);
  const plan = planGanttYLabels(rows, padT, plotH);
  plan.forEach((it) => {
    if (it.mode === "hide") return;
    const y = it.y;
    if (y < padT || y > padT + plotH) return;
    const r = it.r;
    const lab = it.mode === "dot"
      ? "·"
      : (r.child_n != null ? `${r.n}.${r.child_n}` : String(r.n));
    const picked = r._picked;
    ctx.fillStyle = picked ? "#7ec8ff" : (r.depth > 0 || it.mode === "dot" ? CHART_AXIS.labelDim : CHART_AXIS.label);
    ctx.font = (picked && it.mode === "num" ? "600 " : "") + "11px system-ui, Segoe UI, sans-serif";
    ctx.fillText(lab, w - 8, y);
  });
  ctx.restore();
}

function keepYNumber(n, step) {
  if (!(n > 0)) return false;
  if (step <= 1) return true;
  if (step === 2) return n % 2 === 1;
  return n === 1 || n % step === 0;
}

function planGanttYLabels(rows, padT, plotH) {
  const MIN = 12;
  const items = (rows || []).map((r, i) => ({
    r,
    i,
    y: r._cy != null ? r._cy : (padT + (i + 0.5) * (plotH / Math.max(rows.length, 1))),
    isSub: Number(r.depth) > 0 || isSubagentKind(r.session_kind),
    n: Number(r.n) || 0,
    mode: "num",
  }));
  items.sort((a, b) => a.y - b.y);
  const collides = (a, b) => Math.abs(a.y - b.y) < MIN;

  for (const it of items) {
    if (!it.isSub) continue;
    if (items.some((o) => !o.isSub && collides(it, o))) it.mode = "hide";
  }
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (!it.isSub || it.mode === "hide") continue;
    for (let j = i + 1; j < items.length; j++) {
      const o = items[j];
      if (o.mode === "hide") continue;
      if (!collides(it, o)) break;
      if (o.isSub) o.mode = "hide";
    }
  }

  const parents = items.filter((it) => !it.isSub);
  const steps = [1, 2, 5, 10, 20, 50];
  let step = 1;
  for (const s of steps) {
    let prev = -1e9;
    let ok = true;
    for (const it of parents) {
      if (!keepYNumber(it.n, s)) continue;
      if (it.y - prev < MIN) { ok = false; break; }
      prev = it.y;
    }
    step = s;
    if (ok) break;
  }
  for (const it of parents) {
    it.mode = keepYNumber(it.n, step) ? "num" : "dot";
  }
  for (const it of items) {
    if (it.mode !== "dot") continue;
    if (items.some((o) => o.mode === "num" && collides(it, o))) it.mode = "hide";
  }
  return items;
}

function sessionOverlapsWindow(s, t0, t1) {
  const a = s.first_epoch != null ? Number(s.first_epoch) : null;
  const b = s.last_epoch != null ? Number(s.last_epoch) : null;
  if (a != null && b != null && a < t1 && b > t0) return true;
  for (const sp of s.spans || []) {
    const x = Number(sp.start);
    const y = Number(sp.end);
    if (Number.isFinite(x) && Number.isFinite(y) && x < t1 && y > t0) return true;
  }
  return false;
}

function sessionsVisibleInWindow(all, t0, t1) {
  if (!all.length) return [];
  const n = all.length;
  const hit = new Set();
  all.forEach((s, i) => {
    if (!sessionOverlapsWindow(s, t0, t1)) return;
    hit.add(i);
    const pid = String(s.parent_id || "").toLowerCase();
    if (pid) {
      const pi = all.findIndex((p) => String(p.session_id).toLowerCase() === pid);
      if (pi >= 0) hit.add(pi);
    }
  });
  if (!hit.size) return all;
  const extra = new Set();
  for (const i of hit) {
    if (i > 0) extra.add(i - 1);
    if (i + 1 < n) extra.add(i + 1);
  }
  extra.forEach((i) => hit.add(i));
  return all.filter((_, i) => hit.has(i));
}

export function clampGanttWindow(t0, t1, p0, p1) {
  const minS = GANTT_MIN_SPAN;
  const maxS = Math.max(minS, p1 - p0);
  let span = Math.min(maxS, Math.max(minS, t1 - t0));
  let a = t0;
  let b = a + span;
  if (b > p1) {
    b = p1;
    a = b - span;
  }
  if (a < p0) {
    a = p0;
    b = Math.min(p1, a + span);
  }
  return { t0: a, t1: b };
}

function bindGanttInteract(canvas) {
  if (!canvas || canvas._ganttIx) return;
  canvas._ganttIx = true;
  const wrap = $("costChartWrap");
  canvas.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    const pack = canvas._aggHit;
    if (!pack || pack.kind !== "timeline") return;
    if (pointerInXAxis(ev, wrap)) return;
    canvas.setPointerCapture(ev.pointerId);
    const store = window.__aggChart || {};
    canvas._gpan = {
      x0: ev.clientX,
      y0: ev.clientY,
      t0: store._gt0,
      t1: store._gt1,
      yPan0: store._yViewPan || 0,
      moved: false,
    };
    if (wrap) wrap.classList.add("is-panning");
  });
  canvas.addEventListener("pointermove", (ev) => {
    const pan = canvas._gpan;
    const pack = canvas._aggHit;
    if (pan && pack) {
      const dx = ev.clientX - pan.x0;
      const dy = ev.clientY - pan.y0;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) pan.moved = true;
      if (pan.moved) {
        hideChartTip($("costTip"));
        const dt = -dx / pack.plotW * (pan.t1 - pan.t0);
        const nxt = clampGanttWindow(pan.t0 + dt, pan.t1 + dt, pack.p0, pack.p1);
        const store = window.__aggChart;
        if (store) {
          store._gt0 = nxt.t0;
          store._gt1 = nxt.t1;
          const n = pack.n || ((store._ganttRows || []).length);
          const visN = pack.visN || Math.max(1, n / (store._yViewZoom || 1));
          const rowH = pack.rowH || ((pack.plotH || 1) / visN);
          store._yViewPan = clampYPan(pan.yPan0 - dy / rowH, n, visN);
        }
        drawTimeline(canvas, pack.agg);
      }
      return;
    }
    if (!pack || pack.kind !== "timeline") return;
    if (canvas._costTipOwner !== "period") return;
    const tipEl = $("costTip");
    if (!tipEl) return;
    const r = canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left;
    const my = ev.clientY - r.top;
    const found = pack.hit.find((h0) => mx >= h0.x && mx <= h0.x + h0.w && my >= h0.y && my <= h0.y + h0.h);
    if (!found) {
      hideChartTip(tipEl);
      return;
    }
    const s = found.s;
    const name = s.label || (s.depth > 0 ? `Sub Agent ${s.child_n}` : `Session ${s.n}`);
    const kind = found.seg && found.seg.kind === "wait" ? "Wait · user" : "LLM / harness";
    const dur = found.seg ? Math.max(0, found.seg.end - found.seg.start) : 0;
    const html = `<b>${esc(name)}</b><br><span class="tip-title">${esc(s.title || "")}</span><br>${esc(kind)} · ${Math.round(dur)}s<br>${fmtUsd(s.official_usd)} · ${fmtTokens(s.tokens_all || 0)}`;
    placeCostTip(ev, tipEl, html);
  });
  canvas.addEventListener("pointerup", () => {
    canvas._gpan = null;
    if (wrap) wrap.classList.remove("is-panning");
  });
  canvas.addEventListener("pointercancel", () => {
    canvas._gpan = null;
    if (wrap) wrap.classList.remove("is-panning");
  });
  canvas.addEventListener("mouseleave", () => {
    if (!canvas._gpan) hideChartTip($("costTip"));
  });
}

/** Gantt-style session durations (parent / child lanes). */
export function drawTimeline(canvas, agg) {
  if (!canvas) return;
  if (!document.body.classList.contains("scope-period")) {
    clearCostPointerProps(canvas);
    hideChartTip($("costTip"));
    setGanttChrome(false);
    return;
  }
  setCostTipOwner(canvas, "period");
  setGanttChrome(true);
  clearRateHost("cost");
  const prev = window.__aggChart || {};
  const all = (agg && agg.sessions) || [];
  const selected = prev.selected instanceof Set ? prev.selected : new Set();
  const drill = selected.size > 0;
  const picked = drill
    ? all.filter((s) => selected.has(String(s.session_id).toLowerCase()))
    : all;
  window.__aggChart = {
    ...prev,
    buckets: (agg && agg.buckets) || [],
    timeline: true,
    agg,
    selected,
    _ganttRows: picked,
  };

  const p0 = Date.parse(agg.start) / 1000;
  const p1 = Date.parse(agg.end) / 1000;
  if (!Number.isFinite(p0) || !Number.isFinite(p1) || p1 <= p0) {
    const laid0 = layoutGanttCanvas(canvas);
    drawChartEmpty(laid0.ctx, laid0.w, laid0.h, "No period");
    return;
  }
  let win = clampGanttWindow(
    prev._gt0 != null ? prev._gt0 : p0,
    prev._gt1 != null ? prev._gt1 : p1,
    p0,
    p1
  );
  window.__aggChart._gt0 = win.t0;
  window.__aggChart._gt1 = win.t1;
  const sessions = sessionsVisibleInWindow(picked, win.t0, win.t1);
  window.__aggChart._ganttRows = sessions;
  const span = Math.max(1, win.t1 - win.t0);

  const laid = layoutGanttCanvas(canvas);
  const { w, h, ctx } = laid;
  const padL = PLOT_PAD_L;
  const padR = 18;
  const padT = 16;
  let padB = 28;
  const plotW = Math.max(10, w - padL - padR);
  const xOf = (ep) => padL + ((Number(ep) - win.t0) / span) * plotW;
  const ticks = buildGanttTicks(win.t0, win.t1);
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  let collide = false;
  for (let i = 1; i < ticks.length; i++) {
    const gap = xOf(ticks[i].t) - xOf(ticks[i - 1].t);
    const need = (ctx.measureText(ticks[i - 1].label).width + ctx.measureText(ticks[i].label).width) / 2 + 8;
    if (gap < need) { collide = true; break; }
  }
  if (collide) padB = 52;
  const plotH = Math.max(10, h - padT - padB);
  window.__aggChart._ganttGeom = { padT, padB, plotH, plotW, padL };

  const workColor = "#3ecf8e";
  const waitColor = "#9aa3ad";
  const hidden = prev.hiddenLegend instanceof Set ? prev.hiddenLegend : new Set();
  window.__aggChart.hiddenLegend = hidden;
  renderCostLegend([
    ["work", { label: "LLM / harness", color: workColor, k: "in" }],
    ["wait", { label: "Wait · user", color: waitColor, k: "cached" }],
  ], hidden);

  for (const tk of ticks) {
    const x = xOf(tk.t);
    const mid = tk.kind === "date";
    ctx.strokeStyle = mid
      ? "rgba(126, 200, 255, 0.38)"
      : (tk.kind === "h2" ? "rgba(36, 48, 64, 0.95)" : CHART_AXIS.grid);
    ctx.lineWidth = mid ? 1.4 : 1;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + plotH);
    ctx.stroke();
    ctx.fillStyle = mid ? CHART_AXIS.label : CHART_AXIS.labelDim;
    drawXLabel(ctx, tk.label, x, h - padB + 6, collide);
  }

  if (!sessions.length) {
    drawYSessionLabels([], padT, padB, h);
    drawChartEmpty(ctx, w, h, "No sessions in this period");
    canvas._aggHit = { hit: [], w, kind: "timeline", plotW, p0, p1, agg };
    bindGanttInteract(canvas);
    return;
  }

  const yz = clampYViewZoom(prev._yViewZoom || 1);
  const visN = Math.max(1, sessions.length / yz);
  const yPan = clampYPan(Number(prev._yViewPan) || 0, sessions.length, visN);
  window.__aggChart._yViewPan = yPan;
  const rowH = plotH / visN;
  const barH = Math.max(5, Math.min(16, rowH * 0.42));
  const waitH = Math.max(3, barH * 0.48);
  const hit = [];
  const rowMid = [];
  const labelRows = [];

  ctx.save();
  ctx.beginPath();
  ctx.rect(padL, padT, plotW, plotH);
  ctx.clip();

  // Parent → child elbows (behind bars)
  sessions.forEach((s, i) => {
    rowMid[i] = padT + (i - yPan + 0.5) * rowH;
  });
  sessions.forEach((s, i) => {
    if (!(s.depth > 0) && !isSubagentKind(s.session_kind)) return;
    const pid = (s.parent_id || "").toLowerCase();
    if (!pid) return;
    const pi = sessions.findIndex((p) => String(p.session_id).toLowerCase() === pid);
    if (pi < 0) return;
    const childStart = s.first_epoch || (s.spans && s.spans[0] && s.spans[0].start);
    if (childStart == null) return;
    const x = xOf(childStart);
    const yP = rowMid[pi];
    const yC = rowMid[i];
    if (!Number.isFinite(yP) || !Number.isFinite(yC)) return;
    ctx.strokeStyle = "rgba(126, 200, 255, 0.75)";
    ctx.lineWidth = 1.6;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(x, yP);
    ctx.lineTo(x, yC);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(126, 200, 255, 0.9)";
    ctx.beginPath();
    ctx.arc(x, yP, 2.4, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, yC, 2.2, 0, Math.PI * 2);
    ctx.fill();
  });

  sessions.forEach((s, i) => {
    const cy = rowMid[i];
    if (cy < padT - rowH || cy > padT + plotH + rowH) return;
    labelRows.push({
      ...s,
      _cy: cy,
      _picked: drill && selected.has(String(s.session_id).toLowerCase()),
    });
    const segs = (s.spans && s.spans.length)
      ? s.spans
      : (s.first_epoch != null
        ? [{ start: s.first_epoch, end: s.last_epoch || s.first_epoch, kind: "work" }]
        : []);
    const vis = segs.filter((seg) => {
      const key = seg.kind === "wait" ? "wait" : "work";
      return !hidden.has(key);
    });
    vis.forEach((seg, si) => {
      const isWait = seg.kind === "wait";
      let x0 = xOf(seg.start);
      let x1 = xOf(seg.end);
      if (x1 < padL || x0 > w - padR) return;
      x0 = Math.max(padL, x0);
      x1 = Math.min(w - padR, x1);
      let bw = Math.max(GANTT_MIN_BAR_PX, x1 - x0);
      const nxt = vis[si + 1];
      if (nxt) {
        const nx = xOf(nxt.start);
        if (x0 + bw > nx - 1) bw = Math.max(1, nx - x0 - 1);
      }
      const bh = isWait ? waitH : barH;
      const y = cy - bh / 2;
      ctx.fillStyle = isWait ? waitColor : workColor;
      ctx.fillRect(x0, y, bw, bh);
      hit.push({ x: x0, y, w: bw, h: bh, s, seg });
    });
  });
  ctx.restore();

  drawYSessionLabels(labelRows, padT, padB, h);

  canvas._aggHit = {
    hit, w, kind: "timeline", plotW, plotH, p0, p1, agg,
    n: sessions.length, visN, rowH,
  };
  bindGanttInteract(canvas);
}

export function fitGanttToSessions(rows) {
  const store = window.__aggChart;
  if (!store || !store.agg) return;
  const p0 = Date.parse(store.agg.start) / 1000;
  const p1 = Date.parse(store.agg.end) / 1000;
  if (!(p1 > p0)) return;
  let a = Infinity;
  let b = -Infinity;
  for (const s of rows || []) {
    if (s.first_epoch != null) a = Math.min(a, Number(s.first_epoch));
    if (s.last_epoch != null) b = Math.max(b, Number(s.last_epoch));
    for (const sp of s.spans || []) {
      if (sp.start != null) a = Math.min(a, Number(sp.start));
      if (sp.end != null) b = Math.max(b, Number(sp.end));
    }
  }
  if (!Number.isFinite(a) || !Number.isFinite(b)) return;
  if (b < a) b = a;
  const pad = Math.max(45, (b - a) * 0.1);
  const win = clampGanttWindow(a - pad, b + pad, p0, p1);
  store._gt0 = win.t0;
  store._gt1 = win.t1;
}
