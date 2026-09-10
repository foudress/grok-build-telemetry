/** tok/s charts (gated). Extracted as-is from charts.js. */
import {
  $,
  fmtTokens,
  fmtToksPerSec,
  fmtMs,
  esc,
} from './fmt.js';
import {
  CHART_AXIS,
  COST_Y_LEFT,
  PLOT_PAD_L,
  COST_MAX_SLOT,
  fitCanvas,
  drawChartEmpty,
  drawXLabel,
  planXLabels,
  guessXPlan,
  applyXPadHeight,
  storedCtxChartH,
  ensureCostChartH,
  bindChartResize,
  zoomStore,
  slotRange,
  clampYViewZoom,
  pointerInXAxis,
  chartViewKey,
  hideChartTip,
  placeCostTip,
  setCostTipOwner,
  bindCostZoom,
} from './cost-draw.js';

function ctxRateStore() {
  if (!window.__ctxRateChart) window.__ctxRateChart = {};
  return window.__ctxRateChart;
}

export function rateHostIds(host) {
  if (host === "ctx") {
    return {
      wrap: "ctxChartWrap",
      scroll: "ctxChartScroll",
      yAxis: "ctxYAxis",
      tip: "ctxTip",
      store: ctxRateStore(),
    };
  }
  return {
    wrap: "costChartWrap",
    scroll: "costChartScroll",
    yAxis: "costYAxis",
    tip: "costTip",
    store: zoomStore(),
  };
}

export function niceRateYMax(rawMax) {
  const m = Math.max(Number(rawMax) || 0, 0.01);
  const targetTicks = 6;
  const rough = m / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(rough, 1e-6))));
  const r = rough / mag;
  let nice = 1;
  if (r <= 1) nice = 1;
  else if (r <= 2) nice = 2;
  else if (r <= 5) nice = 5;
  else nice = 10;
  let step = nice * mag;
  if (step < 0.01) step = 0.01;
  let max = Math.max(step, Math.ceil(m / step) * step);
  let ticks = Math.round(max / step);
  if (ticks > 8) {
    step = (Math.ceil(max / 6 / step) || 1) * step;
    max = Math.max(step, Math.ceil(m / step) * step);
  }
  return { min: 0, max, step };
}

export function buildSessionRatePoints(rounds, grain) {
  const pts = [];
  (rounds || []).forEach((r) => {
    const ri = r.index ?? "?";
    if (grain === "round") {
      if (r.gen_tokens_per_sec == null) return;
      pts.push({
        label: `R${ri}`,
        v: Number(r.gen_tokens_per_sec),
        kind: "round",
        round: ri,
        gen_ms: r.gen_ms,
        tokens_out: r.gen_out_tokens,
        n: r.gen_rate_n,
      });
      return;
    }
    (r.model_steps || []).forEach((s, i) => {
      if (s.gen_tokens_per_sec == null) return;
      const li = s.index ?? (i + 1);
      pts.push({
        label: `R${ri}·${li}`,
        v: Number(s.gen_tokens_per_sec),
        kind: "call",
        round: ri,
        call: li,
        gen_ms: s.gen_ms,
        tokens_out: s.gen_out_tokens,
      });
    });
  });
  return pts;
}

export function layoutRateCanvas(canvas, barCount, ids) {
  const yAxis = $(ids.yAxis);
  if (yAxis && barCount > 0) yAxis.hidden = false;
  const scroller = $(ids.scroll);
  let viewW = (scroller && scroller.clientWidth) || 0;
  if (!viewW) viewW = (canvas.parentElement && canvas.parentElement.clientWidth) || 600;
  const store = ids.store;
  let h = ids.host === "ctx"
    ? storedCtxChartH()
    : ensureCostChartH(store);
  canvas.style.height = h + "px";
  if (yAxis) yAxis.style.height = h + "px";
  if (ids.host === "cost") bindChartResize();
  const vk = (ids.host === "ctx" ? "ctx:" : "") + (store._rateKey || chartViewKey());
  if (store._rateZoomKey !== vk) {
    store._costUserZoom = false;
    store.slotPx = 0;
    store._scrollLeft = 0;
    store._rateYZoom = 1;
    store._rateZoomKey = vk;
  }
  const keepScroll = store._costUserZoom || store._costStickEnd === false;
  const prevScroll = scroller
    ? (store._scrollLeft != null ? store._scrollLeft : scroller.scrollLeft)
    : 0;
  const { nSlots, minSlot, maxSlot: slotCap } = slotRange(viewW, barCount, COST_MAX_SLOT);
  let slot = store.slotPx;
  if (store._costUserZoom && slot > 0) {
    slot = Math.min(slotCap, Math.max(minSlot, slot));
  } else {
    slot = minSlot;
  }
  store.slotPx = slot;
  const w = PLOT_PAD_L + 12 + nSlots * slot;
  const overflow = w > viewW + 1;
  canvas.style.width = w + "px";
  if (scroller) scroller.classList.toggle("is-overflow", overflow);
  const { ctx } = fitCanvas(canvas, w, h);
  ctx.clearRect(0, 0, w, h);
  if (scroller) {
    const max = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
    if (overflow && !keepScroll && store._costStickEnd !== false) {
      scroller.scrollLeft = max;
      store._scrollLeft = scroller.scrollLeft;
    } else if (keepScroll) {
      scroller.scrollLeft = Math.min(max, Math.max(0, prevScroll));
      store._scrollLeft = scroller.scrollLeft;
    }
  }
  if (scroller && !scroller._rateStickBound) {
    scroller._rateStickBound = true;
    scroller.addEventListener("scroll", () => {
      const st = ids.store;
      st._scrollLeft = scroller.scrollLeft;
      const max = scroller.scrollWidth - scroller.clientWidth;
      st._costStickEnd = max > 0 && scroller.scrollLeft >= max - 12;
    });
  }
  canvas._zoomBars = barCount;
  canvas._zoomMaxSlot = COST_MAX_SLOT;
  return { w, h, ctx, overflow, slot, nSlots, minSlot, viewW };
}

export function drawRateYOverlay(ids, { min, max, step, top, h, padB, format }) {
  const yAxis = $(ids.yAxis);
  if (!yAxis) return;
  yAxis.hidden = false;
  const w = COST_Y_LEFT;
  yAxis.style.width = w + "px";
  yAxis.style.height = h + "px";
  const { ctx } = fitCanvas(yAxis, w, h);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = CHART_AXIS.label;
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  const span = (max - min) || 1;
  const plotH = Math.max(10, h - top - (padB || 28));
  const fmt = typeof format === "function" ? format : fmtToksPerSec;
  for (let v = min; v <= max + step / 2; v += step) {
    const y = top + ((max - v) / span) * plotH;
    ctx.fillText(fmt(v), w - 8, y + 3);
  }
}

/** Square step path: horizontal then vertical jump (no linear interpolation). */
export function strokeStepLine(ctx, xs, ys) {
  if (!xs.length) return;
  ctx.beginPath();
  ctx.moveTo(xs[0], ys[0]);
  for (let i = 1; i < xs.length; i++) {
    ctx.lineTo(xs[i], ys[i - 1]);
    ctx.lineTo(xs[i], ys[i]);
  }
  ctx.stroke();
}

export function bindRatePan(ids) {
  const scroller = $(ids.scroll);
  const wrap = $(ids.wrap);
  if (!scroller || scroller._ratePanBound) return;
  scroller._ratePanBound = true;
  scroller.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    if (ev.target && ev.target.closest && ev.target.closest(".chart-resize")) return;
    scroller._rpan = { x0: ev.clientX, sl: scroller.scrollLeft, moved: false };
    try { scroller.setPointerCapture(ev.pointerId); } catch { /* ignore */ }
  });
  scroller.addEventListener("pointermove", (ev) => {
    const d = scroller._rpan;
    if (!d) return;
    const dx = ev.clientX - d.x0;
    if (Math.abs(dx) > 3) d.moved = true;
    if (!d.moved) return;
    ev.preventDefault();
    if (wrap) wrap.classList.add("is-panning");
    scroller.scrollLeft = d.sl - dx;
    ids.store._scrollLeft = scroller.scrollLeft;
  });
  const end = () => {
    scroller._rpan = null;
    if (wrap) wrap.classList.remove("is-panning");
  };
  scroller.addEventListener("pointerup", end);
  scroller.addEventListener("pointercancel", end);
}

export function bindRateZoom(ids) {
  const wrap = $(ids.wrap);
  if (!wrap || wrap._rateZoomBound) return;
  wrap._rateZoomBound = true;
  wrap.addEventListener("wheel", (ev) => {
    if (ev.shiftKey) return;
    if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;
    if (ev.target && (ev.target.id === ids.yAxis || (ev.target.classList && ev.target.classList.contains("cost-yaxis"))))
      return;
    if (ids.host === "cost" && wrap._zoomBound && pointerInXAxis(ev, wrap)) return;
    const canvas = ids.host === "ctx" ? $("ctxChart") : $("costChart");
    const scroller = $(ids.scroll);
    if (!canvas || !canvas._ratePts) return;
    const store = ids.store;
    const barCount = canvas._zoomBars || 1;
    const viewW = (scroller && scroller.clientWidth) || wrap.clientWidth || 600;
    const { nSlots, minSlot, maxSlot } = slotRange(viewW, barCount, canvas._zoomMaxSlot);
    const slot0 = store.slotPx > 0 ? store.slotPx : minSlot;
    const notches = ev.deltaY / (ev.deltaMode === 1 ? 3 : 80);
    let next = slot0 * Math.pow(1.16, -notches);
    next = Math.min(maxSlot, Math.max(minSlot, next));
    if (next <= minSlot * 1.03) next = minSlot;
    if (Math.abs(next - slot0) < 0.05) return;
    ev.preventDefault();
    const sl = scroller ? scroller.scrollLeft : 0;
    const rect = (scroller || wrap).getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const oldW = PLOT_PAD_L + 12 + nSlots * slot0;
    const newW = PLOT_PAD_L + 12 + nSlots * next;
    const t = oldW > 0 ? (sl + mx) / oldW : 0;
    store.slotPx = next;
    store._costUserZoom = true;
    store._costStickEnd = false;
    if (canvas._rateRedraw) canvas._rateRedraw();
    if (scroller) {
      const max = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
      scroller.scrollLeft = Math.min(max, Math.max(0, t * newW - mx));
      store._scrollLeft = scroller.scrollLeft;
    }
  }, { passive: false });
}

export function placeRateTip(ev, tipEl, html, _wrapId) {
  // Same fixed viewport placement as cost tips (wrap-relative + fixed was too high).
  placeCostTip(ev, tipEl, html);
}

export function drawRateChart(canvas, pts, opts) {
  const host = (opts && opts.host) || "cost";
  const ids = rateHostIds(host);
  ids.host = host;
  if (host === "ctx") ids.store._rateKey = "ctx:" + ((opts && opts.grain) || "call");
  else ids.store._rateKey = "p-rate:" + ((opts && opts.grain) || "session");
  const color = (opts && opts.color) || "#7ec8ff";
  const wrap = $(ids.wrap);
  if (wrap) wrap.classList.toggle("is-rate", true);
  canvas._ioStepTip = false;
  if (host === "cost") {
    if (wrap) wrap.classList.remove("is-gantt");
    if (window.__aggChart) {
      window.__aggChart.ratePts = pts;
      window.__aggChart.rateOpts = { ...(opts || {}), host: "cost" };
      window.__aggChart.rate = true;
      window.__aggChart.ioStep = false;
      window.__aggChart.ioStepPts = null;
    }
  }
  canvas._rateRedraw = () => drawRateChart(canvas, pts, opts);
  bindRatePan(ids);
  if (host === "ctx") {
    bindRateZoom(ids);
    bindRateYStretch(ids);
  }
  if (host === "cost") {
    bindCostZoom();
    setCostTipOwner(canvas, "rate");
  }
  if (host === "ctx") canvas._ctxPts = null;

  const list = (pts || []).filter((p) => p && p.v != null && !Number.isNaN(p.v));
  const yAxis = $(ids.yAxis);
  if (!list.length) {
    if (yAxis) yAxis.hidden = true;
    const scroller = $(ids.scroll);
    if (scroller) {
      canvas.style.width = "100%";
      const h = host === "ctx" ? storedCtxChartH() : ensureCostChartH(ids.store);
      canvas.style.height = h + "px";
    }
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 240;
    const { ctx } = fitCanvas(canvas, w, h);
    ctx.clearRect(0, 0, w, h);
    drawChartEmpty(ctx, w, h, "No tok/s samples");
    canvas._ratePts = null;
    hideChartTip($(ids.tip));
    if (host === "cost") {
      const legend = $("costLegend");
      if (legend) legend.innerHTML = "";
    }
    return;
  }

  const labels = list.map((p) => p.label || "");
  // Pad from fit-view first; final X plan uses real slot after zoom (same as Session bars).
  const padGuess = guessXPlan(labels, list.length, false);
  if (host === "cost") applyXPadHeight(canvas, padGuess.padB);
  else {
    const base = storedCtxChartH();
    canvas.style.height = (base + Math.max(0, (padGuess.padB || 28) - 28)) + "px";
  }
  const laid = layoutRateCanvas(canvas, list.length, ids);
  const { w, h, ctx, slot } = laid;
  const xPlan = planXLabels(ctx, labels, slot, { temporal: false });
  const padL = PLOT_PAD_L;
  const padT = 18;
  const padB = Math.max(padGuess.padB || 28, xPlan.padB || 28);
  const plotH = Math.max(10, h - padT - padB);
  const vals = list.map((p) => p.v);
  const yz = clampYViewZoom(ids.store._rateYZoom || 1);
  const { min, max, step } = niceRateYMax(Math.max(...vals, 0) / yz);
  const yOf = (v) => padT + plotH - ((v - min) / ((max - min) || 1)) * plotH;
  const xOf = (i) => padL + slot / 2 + i * slot;
  // Shrink dots when zoomed out so dense Session/Round grains stay readable.
  const baseR = 3.2;
  const dotR = Math.max(1.15, Math.min(baseR, slot * 0.28));
  const lineW = Math.max(1, Math.min(2, slot * 0.12));

  ctx.strokeStyle = CHART_AXIS.grid;
  ctx.lineWidth = 1;
  for (let v = min; v <= max + step / 2; v += step) {
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - 8, y);
    ctx.stroke();
  }
  drawRateYOverlay(ids, { min, max, step, top: padT, h, padB });

  ctx.strokeStyle = color;
  ctx.lineWidth = lineW;
  ctx.beginPath();
  list.forEach((p, i) => {
    const x = xOf(i);
    const y = yOf(p.v);
    p._x = x;
    p._y = y;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.lineTo(xOf(list.length - 1), padT + plotH);
  ctx.lineTo(xOf(0), padT + plotH);
  ctx.closePath();
  ctx.fillStyle = color + "22";
  ctx.fill();

  list.forEach((p, i) => {
    const x = xOf(i);
    const y = yOf(p.v);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, dotR, 0, Math.PI * 2);
    ctx.fill();
    if (p.label && (xPlan.every <= 1 || i % xPlan.every === 0 || i === list.length - 1)) {
      drawXLabel(ctx, p.label, x, h - padB + 6, xPlan.rotate);
    }
  });

  canvas._ratePts = list;
  canvas._rateGeom = { padL, padT, padB, plotH, slot, min, max };
  if (host === "cost") {
    const legend = $("costLegend");
    if (legend) legend.innerHTML = "";
  }

  if (!canvas._rateTipBound) {
    canvas._rateTipBound = true;
    canvas.addEventListener("mousemove", (ev) => {
      if (canvas._ioStepTip) return;
      const pack = canvas._ratePts;
      const tip = $(canvas._rateTipId || ids.tip);
      if (!pack || !pack.length) return;
      if (canvas._costTipOwner && canvas._costTipOwner !== "rate" && canvas._rateWrapId === "costChartWrap")
        return;
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      let best = null;
      let bestD = Math.max(18, (canvas._rateGeom && canvas._rateGeom.slot) ? canvas._rateGeom.slot / 2 : 18);
      for (const p of pack) {
        if (p._x == null) continue;
        const d = Math.hypot(mx - p._x, my - p._y);
        if (d < bestD) {
          bestD = d;
          best = p;
        }
      }
      if (!best) return hideChartTip(tip);
      // Period Session/Round: numeric labels (29 / 29.1 / 29 R2). Call grain keeps R·c.
      const title = (best.kind === "session" || best.kind === "round")
        ? `<b>${esc(best.label || (best.round != null ? ("R" + best.round) : ""))}</b>`
        : `<b>R${esc(best.round)}·${esc(best.call != null ? best.call : best.label)}</b>`;
      // Call-count avg only for session-local Round/Call points (not period session index).
      const showCallAvg = best.n != null && (
        best.kind === "call"
        || (best.kind === "round" && !best.session_id)
      );
      const lines = [
        title,
        `<span class="muted">Y tok/s</span> <b>${fmtToksPerSec(best.v)}</b>`,
        best.tokens_out != null ? `<span class="muted">TokF</span> ${fmtTokens(best.tokens_out)}` : "",
        best.gen_ms != null ? `<span class="muted">window</span> ${fmtMs(best.gen_ms)}` : "",
        showCallAvg
          ? `<span class="muted">${best.n} call${best.n === 1 ? "" : "s"} avg</span>`
          : "",
      ];
      placeRateTip(ev, tip, lines.filter(Boolean).join("<br>"), canvas._rateWrapId || ids.wrap);
    });
    canvas.addEventListener("mouseleave", () => {
      if (canvas._ioStepTip) return;
      if (!canvas._ratePts) return;
      hideChartTip($(canvas._rateTipId || ids.tip));
    });
    canvas.addEventListener("click", (ev) => {
      if (canvas._ioStepTip) return;
      const pack = canvas._ratePts;
      if (!pack || !pack.length) return;
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      let best = null;
      let bestD = 16;
      for (const p of pack) {
        if (p._x == null) continue;
        const d = Math.hypot(mx - p._x, my - p._y);
        if (d < bestD) {
          bestD = d;
          best = p;
        }
      }
      const fn = canvas._rateOnClick;
      if (best && fn) fn(best);
    });
  }
  canvas._rateTipId = ids.tip;
  canvas._rateWrapId = ids.wrap;
  canvas._rateOnClick = opts && opts.onClick;
}

export function clearRateHost(host) {
  const ids = rateHostIds(host || "cost");
  const wrap = $(ids.wrap);
  if (wrap) wrap.classList.remove("is-rate");
  const y = $(ids.yAxis);
  if (y && host === "ctx") y.hidden = true;
  const canvas = host === "ctx" ? $("ctxChart") : $("costChart");
  if (canvas) {
    canvas._ratePts = null;
    canvas._rateGeom = null;
    canvas._rateRedraw = null;
    canvas._ioStepTip = false;
  }
  if (host === "cost" && window.__aggChart) {
    window.__aggChart.ioStep = false;
    window.__aggChart.ioStepPts = null;
  }
  hideChartTip($(ids.tip));
}

function bindRateYStretch(ids) {
  const y = $(ids.yAxis);
  if (!y || y._rateYStretch) return;
  y._rateYStretch = true;
  y.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const store = ids.store;
    if (!store) return;
    applyCtxRateYZoom(store, (store._rateYZoom || 1) * Math.pow(1.12, -ev.deltaY / 80), ids);
  }, { passive: false });
  y.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    const store = ids.store;
    if (!store) return;
    try { y.setPointerCapture(ev.pointerId); } catch { /* ignore */ }
    y._rateYDrag = { y0: ev.clientY, z0: store._rateYZoom || 1 };
  });
  y.addEventListener("pointermove", (ev) => {
    const drag = y._rateYDrag;
    if (!drag) return;
    if (Math.abs(ev.clientY - drag.y0) < 3) return;
    const store = ids.store;
    if (!store) return;
    applyCtxRateYZoom(store, drag.z0 * Math.pow(1.02, (drag.y0 - ev.clientY) / 4), ids);
  });
  const end = () => { y._rateYDrag = null; };
  y.addEventListener("pointerup", end);
  y.addEventListener("pointercancel", end);
}

function applyCtxRateYZoom(store, next, ids) {
  const z1 = clampYViewZoom(next);
  if (Math.abs(z1 - (store._rateYZoom || 1)) < 0.001) return;
  store._rateYZoom = z1;
  const canvas = ids && ids.host === "cost" ? $("costChart") : $("ctxChart");
  if (canvas && canvas._rateRedraw) canvas._rateRedraw();
}
