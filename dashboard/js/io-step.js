/** Period I/O $/M step chart (gated). Extracted as-is from charts.js. */
import {
  $,
  esc,
  fmtUsdPerM,
  partIn,
  partCached,
  partOut,
  joinParts,
} from './fmt.js';
import { COST_COLORS, niceCostYMax } from './cost-model.js';
import {
  PLOT_PAD_L,
  CHART_AXIS,
  fitCanvas,
  drawChartEmpty,
  hideChartTip,
  ensureCostChartH,
  guessXPlan,
  applyXPadHeight,
  planXLabels,
  clampYViewZoom,
  drawXLabel,
  renderCostLegend,
  bindCostZoom,
  bindYStretch,
  setCostTipOwner,
} from './cost-draw.js';
import {
  rateHostIds,
  layoutRateCanvas,
  drawRateYOverlay,
  strokeStepLine,
  bindRatePan,
  placeRateTip,
} from './rate-chart.js';

/**
 * Period D/W/M: estimated In / Cached / Out $ per session as step lines.
 * Reuses cost zoom / resize / X-label collision / Y overlay from rate charts.
 */
export function drawIoStepChart(canvas, pts, opts) {
  const ids = rateHostIds("cost");
  ids.host = "cost";
  ids.store._rateKey = "p-io-step";
  const wrap = $(ids.wrap);
  if (wrap) {
    wrap.classList.toggle("is-rate", true);
    wrap.classList.remove("is-gantt");
  }
  if (!window.__aggChart) window.__aggChart = {};
  window.__aggChart.ioStep = true;
  window.__aggChart.rate = false;
  window.__aggChart.timeline = false;
  window.__aggChart.ioStepPts = pts;
  window.__aggChart.ioStepOpts = { ...(opts || {}) };
  canvas._rateRedraw = () => drawIoStepChart(canvas, pts, opts);
  bindRatePan(ids);
  bindCostZoom();
  bindYStretch();
  setCostTipOwner(canvas, "rate");

  const list = (pts || []).filter((p) => p && p.session_id);
  const yAxis = $(ids.yAxis);
  const hidden = (window.__aggChart.hiddenLegend instanceof Set)
    ? window.__aggChart.hiddenLegend
    : new Set();
  const series = [
    { key: "in", label: "In $/M", color: COST_COLORS.in, pick: (p) => Number(p.in) || 0 },
    { key: "cached", label: "Cached $/M", color: COST_COLORS.cached, pick: (p) => Number(p.cached) || 0 },
    { key: "out", label: "Out $/M", color: COST_COLORS.out, pick: (p) => Number(p.out) || 0 },
  ];
  const vis = series.filter((s) => !hidden.has(s.key));

  if (!list.length) {
    if (yAxis) yAxis.hidden = true;
    const scroller = $(ids.scroll);
    if (scroller) {
      canvas.style.width = "100%";
      canvas.style.height = ensureCostChartH(ids.store) + "px";
    }
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 240;
    const { ctx } = fitCanvas(canvas, w, h);
    ctx.clearRect(0, 0, w, h);
    drawChartEmpty(ctx, w, h, "No sessions");
    canvas._ratePts = null;
    hideChartTip($(ids.tip));
    const legend = $("costLegend");
    if (legend) legend.innerHTML = "";
    return;
  }

  const labels = list.map((p) => p.label || "");
  const padGuess = guessXPlan(labels, list.length, false);
  applyXPadHeight(canvas, padGuess.padB);
  const laid = layoutRateCanvas(canvas, list.length, ids);
  const { w, h, ctx, slot } = laid;
  const xPlan = planXLabels(ctx, labels, slot, { temporal: false });
  const padL = PLOT_PAD_L;
  const padT = 18;
  const padB = Math.max(padGuess.padB || 28, xPlan.padB || 28);
  const plotH = Math.max(10, h - padT - padB);
  let rawMax = 0;
  for (const p of list) {
    for (const s of vis) rawMax = Math.max(rawMax, s.pick(p));
  }
  const yz = clampYViewZoom(ids.store._rateYZoom || 1);
  const { min, max, step } = niceCostYMax(Math.max(rawMax, 0.01) / yz);
  const yOf = (v) => padT + plotH - ((v - min) / ((max - min) || 1)) * plotH;
  const xOf = (i) => padL + slot / 2 + i * slot;
  const baseR = 3.2;
  const dotR = Math.max(1.15, Math.min(baseR, slot * 0.28));
  const lineW = Math.max(1.2, Math.min(2.2, slot * 0.12));

  ctx.strokeStyle = CHART_AXIS.grid;
  ctx.lineWidth = 1;
  for (let v = min; v <= max + step / 2; v += step) {
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - 8, y);
    ctx.stroke();
  }
  drawRateYOverlay(ids, { min, max, step, top: padT, h, padB, format: fmtUsdPerM });

  const xs = list.map((_, i) => xOf(i));
  for (const s of vis) {
    const ys = list.map((p) => yOf(s.pick(p)));
    ctx.strokeStyle = s.color;
    ctx.lineWidth = lineW;
    strokeStepLine(ctx, xs, ys);
    list.forEach((p, i) => {
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.arc(xs[i], ys[i], dotR, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  list.forEach((p, i) => {
    p._x = xs[i];
    // Hit mid of visible series Y (or plot mid if all hidden).
    let yHit = padT + plotH / 2;
    if (vis.length) {
      let sum = 0;
      for (const s of vis) sum += yOf(s.pick(p));
      yHit = sum / vis.length;
    }
    p._y = yHit;
    if (p.label && (xPlan.every <= 1 || i % xPlan.every === 0 || i === list.length - 1)) {
      drawXLabel(ctx, p.label, xs[i], h - padB + 6, xPlan.rotate);
    }
  });

  canvas._ratePts = list;
  canvas._rateGeom = { padL, padT, padB, plotH, slot, min, max };
  canvas._ioStepTip = true;

  renderCostLegend(
    series.map((s) => [s.key, { label: s.label, color: s.color, k: s.key }]),
    hidden
  );

  if (!canvas._ioStepTipBound) {
    canvas._ioStepTipBound = true;
    canvas.addEventListener("mousemove", (ev) => {
      if (!canvas._ioStepTip || !canvas._ratePts) return;
      if (canvas._costTipOwner && canvas._costTipOwner !== "rate") return;
      const pack = canvas._ratePts;
      const tip = $(ids.tip);
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      let best = null;
      let bestD = Math.max(18, (canvas._rateGeom && canvas._rateGeom.slot) ? canvas._rateGeom.slot / 2 : 18);
      for (const p of pack) {
        if (p._x == null) continue;
        const d = Math.abs(mx - p._x);
        if (d < bestD) { bestD = d; best = p; }
      }
      if (!best) return hideChartTip(tip);
      const lines = [
        `<b>${esc(best.title || best.label || "Session")}</b>`,
        `<span class="tok-in">In ${fmtUsdPerM(best.in)}</span> · `
          + `<span class="tok-cached">Cached ${fmtUsdPerM(best.cached)}</span> · `
          + `<span class="tok-out">Out ${fmtUsdPerM(best.out)}</span> <span class="muted">/M</span>`,
        joinParts([
          partIn(best.tokens_in, best.cost_in_usd),
          partCached(best.tokens_cached, best.cost_cached_usd),
          partOut(best.tokens_out, best.cost_out_usd),
        ].filter(Boolean)) || "",
        best.snapped ? `<span class="muted">snapped to published rates</span>` : "",
      ];
      placeRateTip(ev, tip, lines.filter(Boolean).join("<br>"), ids.wrap);
    });
    canvas.addEventListener("mouseleave", () => {
      if (!canvas._ioStepTip) return;
      hideChartTip($(ids.tip));
    });
    canvas.addEventListener("click", (ev) => {
      if (!canvas._ioStepTip || !canvas._ratePts) return;
      const pack = canvas._ratePts;
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      let best = null;
      let bestD = Math.max(16, (canvas._rateGeom && canvas._rateGeom.slot) ? canvas._rateGeom.slot / 2 : 16);
      for (const p of pack) {
        if (p._x == null) continue;
        const d = Math.abs(mx - p._x);
        if (d < bestD) { bestD = d; best = p; }
      }
      const fn = canvas._rateOnClick;
      if (best && fn) fn(best);
    });
  }
  canvas._rateTipId = ids.tip;
  canvas._rateWrapId = ids.wrap;
  canvas._rateOnClick = opts && opts.onClick;
}
