/** Cost/context canvas — bars, zoom, resize. */
import {
  $,
  fmtTokens,
  fmtUsd,
  esc,
  AR,
  partIn,
  partCached,
  partOut,
  joinParts,
  totalPrice,
} from './fmt.js';
import { focusRound, revealRound } from './tree.js';
import {
  COST_COLORS,
  costSegKey,
  costDisplayLabel,
  isCostSegHidden,
  costSegMetric,
  fmtCostAxis,
  niceCostYMaxForUnit,
  findRound,
  turnCostParts,
  turnCostPartsTools,
  systemCostBar,
  userCostBar,
  callCostParts,
  applyDrillStack,
  collectRoundSubagentBars,
  attachSubagentSegsToTurn,
  compactCostBar,
  recapCostBar,
  eventMs,
  collectLabelLegend,
  currentCostStack,
  loadLegendOrder,
  saveLegendOrder,
  clearLegendOrder,
  defaultStackRank,
  orderCostSegs,
  sortLegendItems,
  tipOrderedSegs,
  formatTipSegLine,
  periodSessionTipHead,
  aggregateBarsByLabel,
  _cumBuckets,
} from './cost-model.js';
import { drawTimeline, clampGanttWindow } from './gantt.js';
import { drawRateChart, clearRateHost } from './rate-chart.js';
import { drawIoStepChart } from './io-step.js';

/* Canvas axis / empty-state palette — slightly higher contrast than raw muted */
export const CHART_AXIS = {
  grid: "#243040",
  label: "#b6c2d1",
  labelDim: "#8b9cb3",
  empty: "#6b7c92",
  placeholder: "#4a5568",
};

const DPR_CAP = 2;
let _measureCtx = null;

/** Reuse one offscreen 2d context for label measurement (avoid detached canvases each paint). */
export function measureCtx() {
  if (!_measureCtx) {
    _measureCtx = document.createElement("canvas").getContext("2d");
  }
  return _measureCtx;
}

/**
 * Size a canvas backing store only when CSS px / DPR change.
 * Reassigning width/height every poll reallocates bitmaps and drives browser RSS up.
 */
export function fitCanvas(canvas, cssW, cssH, dprCap = DPR_CAP) {
  const dpr = Math.min(dprCap, window.devicePixelRatio || 1);
  const w = Math.max(1, cssW | 0);
  const h = Math.max(1, cssH | 0);
  const bw = Math.max(1, Math.floor(w * dpr));
  const bh = Math.max(1, Math.floor(h * dpr));
  if (canvas.width !== bw || canvas.height !== bh) {
    canvas.width = bw;
    canvas.height = bh;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, dpr, w, h };
}

/**
 * Show DOM chart tooltip (fade/translate via CSS .is-visible).
 * Uses fixed viewport coords so #costChartWrap overflow cannot clip it.
 */
function showChartTip(el, html, leftPx, topPx) {
  if (!el) return;
  el.innerHTML = html;
  el.style.display = "";
  el.style.position = "fixed";
  el.style.left = Math.max(0, leftPx) + "px";
  el.style.top = Math.max(0, topPx) + "px";
  el.classList.add("is-visible");
}

/** Position tip in the viewport (not clipped by the graph window). Prefer below cursor. */
export function placeCostTip(ev, tipEl, html) {
  if (!tipEl) return;
  tipEl.style.maxHeight = "";
  tipEl.style.overflowY = "";
  const margin = 6;
  const maxH = Math.max(96, window.innerHeight - margin * 2);
  tipEl.style.maxHeight = maxH + "px";
  const measured = measureChartTip(tipEl, html);
  const th = Math.min(measured.th, maxH);
  const tw = measured.tw;
  if (measured.th > maxH - 1) tipEl.style.overflowY = "auto";
  let left = ev.clientX + 14;
  let top = ev.clientY + 16;
  if (left + tw > window.innerWidth - margin) left = ev.clientX - tw - 14;
  if (left < margin) left = margin;
  if (top + th > window.innerHeight - margin) top = ev.clientY - th - 10;
  if (top < margin) top = margin;
  if (top + th > window.innerHeight - margin)
    top = Math.max(margin, window.innerHeight - th - margin);
  showChartTip(tipEl, html, left, top);
}

/** Write tip HTML and return measured size (works while visibility:hidden). */
function measureChartTip(el, html) {
  if (!el) return { tw: 160, th: 40 };
  el.innerHTML = html;
  el.style.display = "";
  el.style.position = "fixed";
  el.style.left = "-9999px";
  el.style.top = "0px";
  el.classList.remove("is-visible");
  return {
    tw: el.offsetWidth || 160,
    th: el.offsetHeight || 40,
  };
}

/** Hide DOM chart tooltip. */
export function hideChartTip(el) {
  if (!el) return;
  el.classList.remove("is-visible");
  el.innerHTML = "";
}

export function hideAllChartTips() {
  hideChartTip($("costTip"));
  hideChartTip($("ctxTip"));
}

/** Drop property-style hover handlers (period used these; they stack with addEventListener). */
export function clearCostPointerProps(canvas) {
  if (!canvas) return;
  canvas.onmousemove = null;
  canvas.onmouseleave = null;
  canvas.onclick = null;
  canvas._aggHit = null;
}

export function setCostTipOwner(canvas, owner) {
  if (!canvas) return;
  if (canvas._costTipOwner !== owner) {
    hideChartTip($("costTip"));
    canvas._costTipOwner = owner;
  }
}

/** Calm centered empty-state message inside a chart canvas. */
export function drawChartEmpty(ctx, w, h, message) {
  ctx.fillStyle = CHART_AXIS.empty;
  ctx.font = "12px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(message || "No data yet", w / 2, h / 2);
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
}

/** Call In/Cached/Out — same priorities as round hierarchy (estimate, not harness tt). */
function callHierarchyMetrics(s) {
  const se = (s && s.estimate) || {};
  let tokIn = s.tokens_in != null ? s.tokens_in
    : (se.uncached_input_tokens != null ? se.uncached_input_tokens : se.logical_uncached_tokens);
  let usdIn = s.cost_in_usd != null ? s.cost_in_usd
    : (se.cost_in_usd != null ? se.cost_in_usd : se.cost_in_logical_usd);
  const harnessIn = (s.children || [])
    .filter((ch) => ch && ch.kind === "phase_harness")
    .reduce((a, ch) => a + (Number(ch.tokens_in) || 0), 0);
  if ((!tokIn || tokIn <= 0) && harnessIn > 0) {
    tokIn = harnessIn;
    usdIn = (s.children || [])
      .filter((ch) => ch && ch.kind === "phase_harness")
      .reduce((a, ch) => a + (Number(ch.cost_in_usd) || 0), 0);
  }
  const tokCache = s.tokens_cached
    ?? s.display_cached_tokens
    ?? se.display_cached_tokens
    ?? se.logical_cached_tokens
    ?? se.cached_read_tokens;
  const usdCache = s.cost_cached_usd
    ?? s.display_cached_usd
    ?? se.display_cached_usd
    ?? se.cost_cached_logical_usd
    ?? se.cost_cached_usd;
  const tokOut = s.tokens_out ?? se.output_tokens;
  const usdOut = s.cost_out_usd ?? se.cost_out_usd;
  return {
    tokIn: Number(tokIn) || 0,
    usdIn: Number(usdIn) || 0,
    tokCache: Number(tokCache) || 0,
    usdCache: Number(usdCache) || 0,
    tokOut: tokOut != null ? Number(tokOut) || 0 : null,
    usdOut: usdOut != null ? Number(usdOut) || 0 : null,
    estimate_usd: s.estimate_usd ?? se.api_call_usd ?? se.estimate_usd,
    thought_tokens: se.output_thought_tokens ?? s.composition?.thought_out,
    thought_chars: s.thought_chars,
  };
}

/**
 * Context chart: User(R{n} X-label) → each call@Cached(est) → round_end (no X label).
 * No Sys. No Round Start (end already marks the round; start==prev end).
 */
export function buildCtxPoints(rounds) {
  const pts = [];
  (rounds || []).forEach((r) => {
    const ri = r.index ?? "?";
    const steps = r.model_steps || [];
    const sp = r.system_prompt;
    const sysTok = (sp && (sp.tokens_in || sp.logical_tokens))
      ? (Number(sp.tokens_in ?? sp.logical_tokens) || 0)
      : 0;

    const plottable = [];
    steps.forEach((s, i) => {
      if (s.skip_context) return;
      const ctxA = s.display_context_start ?? s.context_start;
      const ctxB = s.display_context_end ?? s.context_end;
      // Keep calls even when start is 0 (cold R1).
      if (ctxA == null || Number.isNaN(Number(ctxA))) return;
      plottable.push({
        s, i, li: s.index ?? (i + 1),
        ctxA: Number(ctxA),
        ctxB: ctxB != null ? Number(ctxB) : null,
        ownStart: Number(s.context_start),
      });
    });

    const roundCtxA = Number(r.context_start);
    const roundCtxB = Number(r.context_end);
    const hasRoundA = Number.isFinite(roundCtxA);

    const up = r.user_prompt || {};
    const bd = r.breakdown || {};
    const priorCache = r.cache_baseline_at_start;
    const isR1 = Number(r.index) === 1;
    const promptIn = Number(
      up.prompt_tokens_in != null ? up.prompt_tokens_in : up.tokens_in ?? up.uncached_est ?? bd.user_in_tokens
    ) || 0;
    const prevAns = up.prev_llm_answer || {};
    const prevInTok = Math.round(Number(prevAns.tokens_in || prevAns.tokenizer_tokens || 0)) || 0;
    const coTok = Math.round(Number((up.compact_out || {}).tokens_in) || 0);
    const userHeadIn = Number(up.display_in_tokens) || (promptIn + prevInTok + coTok) || promptIn;
    const userHeadUsd = Number(up.display_in_usd)
      || (Number(up.prompt_cost_in_usd ?? up.cost_in_usd) || 0)
      || 0;
    let upCache = isR1 ? 0 : (Number(up.tokens_cached ?? up.cached_est ?? priorCache) || 0);
    let upCacheUsd = isR1 ? 0 : (Number(up.cost_cached_usd) || 0);
    if (prevInTok > 0 && upCache >= prevInTok && !prevAns.from_user_pool) {
      const fullC = Number(up.tokens_cached ?? up.cached_est ?? priorCache) || 1;
      upCache = Math.max(0, upCache - prevInTok);
      if (upCacheUsd > 0 && fullC > 0) upCacheUsd = upCacheUsd * (upCache / fullC);
    }
    // Absolute context after User In (baseline = hierarchy round.context_start).
    let userV = null;
    if (hasRoundA) userV = roundCtxA + userHeadIn;
    else if (plottable.length && Number.isFinite(plottable[0].ownStart))
      userV = plottable[0].ownStart;
    else if (isR1 && sysTok > 0) userV = sysTok + userHeadIn;
    else if (upCache > 0 || userHeadIn > 0) userV = upCache + userHeadIn;
    // Skip User@0 (live mid-calc / empty) — avoids a floor spike until cache lands.
    // X label R{n} sits under the User point (not round-end).
    if (userV != null && userV > 0) {
      pts.push({
        label: `R${ri}`,
        v: userV,
        kind: "user",
        round: ri,
        call: 0,
        tokens_in: userHeadIn,
        tokens_cached: upCache,
        tokens_out: null,
        cost_in_usd: userHeadUsd,
        cost_cached_usd: upCacheUsd,
        cost_out_usd: null,
        estimate_usd: userHeadUsd + upCacheUsd,
      });
    }

    if (!plottable.length) {
      if (Number.isFinite(roundCtxB) && roundCtxB > 0) {
        pts.push({
          label: "",
          v: roundCtxB,
          kind: "round_end",
          round: ri,
          call: 0,
          context_start: hasRoundA ? roundCtxA : null,
          context_end: roundCtxB,
        });
      }
      return;
    }

    plottable.forEach((p) => {
      const m = callHierarchyMetrics(p.s);
      // Calls Y = Cached, unless the connector stamped the real prompt size.
      // A cache miss must not draw a context drop: hit-count ≠ window.
      const plotTok = Number(p.s.context_plot_tokens);
      const y = Number.isFinite(plotTok) && plotTok > 0 ? plotTok : m.tokCache;
      if (!(y > 0)) return;
      pts.push({
        label: String(p.li),
        v: y,
        kind: "call",
        round: ri,
        call: p.li,
        context_start: p.ctxA,
        context_end: p.ctxB,
        tokens_in: m.tokIn,
        tokens_cached: m.tokCache,
        tokens_out: m.tokOut,
        cost_in_usd: m.usdIn,
        cost_cached_usd: m.usdCache,
        cost_out_usd: m.usdOut,
        estimate_usd: m.estimate_usd,
        thought_tokens: m.thought_tokens,
        thought_chars: m.thought_chars,
      });
    });

    const last = plottable[plottable.length - 1];
    const endV = Number.isFinite(roundCtxB)
      ? roundCtxB
      : (last.ctxB != null && !Number.isNaN(last.ctxB) ? last.ctxB : null);
    if (endV != null && !Number.isNaN(endV) && endV > 0) {
      const mL = callHierarchyMetrics(last.s);
      pts.push({
        label: "",
        v: endV,
        kind: "round_end",
        round: ri,
        call: last.li,
        context_start: hasRoundA ? roundCtxA : last.ctxA,
        context_end: endV,
        tokens_in: mL.tokIn,
        tokens_cached: mL.tokCache,
        tokens_out: mL.tokOut,
        cost_in_usd: mL.usdIn,
        cost_cached_usd: mL.usdCache,
        cost_out_usd: mL.usdOut,
        estimate_usd: mL.estimate_usd,
      });
    }
  });
  return pts;
}

/** Context Y: step 50k, nice floor/ceil */
function niceCtxYRange(vals) {
  const STEP = 50_000;
  const rawMax = Math.max(...vals, 0);
  const rawMin = Math.min(...vals, 0);
  let min = Math.floor(rawMin / STEP) * STEP;
  let max = Math.ceil(rawMax / STEP) * STEP;
  if (max <= min) max = min + STEP;
  // Prefer starting at 0 when data is small
  if (rawMin >= 0 && min > 0 && rawMin < STEP) min = 0;
  if (min < 0) min = 0;
  return { min, max, step: STEP };
}

export function drawLineChart(canvas, series, color, rounds) {
  if (canvas) canvas._ratePts = null;

  // One point per LLM call only
  let pts = buildCtxPoints(rounds);
  if (pts.length < 1 && series && series.length >= 1) {
    // fallback stream: sample evenly, no fake call labels
    const step = Math.max(1, Math.floor(series.length / 24));
    pts = series.filter((_, i) => i % step === 0 || i === series.length - 1)
      .map((p, i) => ({ label: "", v: p.v, kind: "stream" }));
  }

  const left = 48, right = 10, top = 22;
  const baseH = storedCtxChartH();
  const wGuess = canvas.clientWidth || 600;
  const plotWGuess = Math.max(1, wGuess - left - right);
  const avgPxGuess = pts.length > 1 ? plotWGuess / (pts.length - 1) : plotWGuess;
  // Same X-label planner as Cost / tok/s — rotate + grow bottom pad when cramped.
  const xPlan = planXLabels(
    measureCtx(),
    (pts || []).map((p) => p.label || ""),
    avgPxGuess,
    { temporal: false }
  );
  const bottom = Math.max(28, xPlan.padB || 28);
  canvas.style.height = (baseH + Math.max(0, bottom - 28)) + "px";

  const w = canvas.clientWidth || wGuess;
  const h = canvas.clientHeight || baseH;
  const { ctx } = fitCanvas(canvas, w, h);
  ctx.clearRect(0, 0, w, h);

  const plotW = w - left - right;
  const plotH = Math.max(10, h - top - bottom);

  if (!pts || pts.length < 1) {
    drawChartEmpty(ctx, w, h, "No LLM calls yet");
    canvas._ctxPts = null;
    hideChartTip($("ctxTip"));
    return;
  }

  const vals = pts.map(p => p.v).filter(v => v != null && !Number.isNaN(v));
  const { min, max, step } = niceCtxYRange(vals);
  const yOf = (v) => top + plotH - ((v - min) / (max - min || 1)) * plotH;
  const xOf = (i) => left + (pts.length === 1 ? plotW / 2 : (i / (pts.length - 1)) * plotW);

  // Grid + Y labels at 50k steps
  ctx.strokeStyle = CHART_AXIS.grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = CHART_AXIS.label;
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  for (let v = min; v <= max + 1; v += step) {
    const y = yOf(v);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
    ctx.fillText(fmtTokens(v), left - 4, y + 3);
  }
  ctx.textAlign = "left";

  const tierCliff = document.documentElement.dataset.source === "codex" ? 272000 : 200000;
  if (tierCliff > min && tierCliff < max) {
    const yCliff = yOf(tierCliff);
    ctx.save();
    ctx.strokeStyle = "rgba(240, 180, 41, 0.55)";
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, yCliff);
    ctx.lineTo(w - right, yCliff);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#f0b429";
    ctx.font = "9px system-ui, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(tierCliff >= 272000 ? "272k" : "200k", left + 4, yCliff - 4);
    ctx.restore();
  }

  // Line through call points only
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = xOf(i), y = yOf(p.v);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.lineTo(xOf(pts.length - 1), top + plotH);
  ctx.lineTo(xOf(0), top + plotH);
  ctx.closePath();
  ctx.fillStyle = color + "22";
  ctx.fill();

  // X label density: always R{n} under User point; thin call nums via planXLabels.every + stride
  const avgPx = pts.length > 1 ? plotW / (pts.length - 1) : plotW;
  const callStride = avgPx >= 22 ? 1 : (avgPx >= 14 ? 2 : 5);
  const every = Math.max(1, xPlan.every || 1, callStride);
  let callInRound = 0;
  pts.forEach((p, i) => {
    const isUser = p.kind === "user";
    const isRoundAnchor = p.kind === "round_end" || p.kind === "round_start";
    if (isUser || isRoundAnchor) callInRound = 0;
    else callInRound += 1;
    // Round-end has no X label (R{n} is on the User point).
    p._showX = !!(p.label) && !isRoundAnchor && (isUser
      || (callInRound % every === 0) || i === pts.length - 1);
  });

  // Points + X labels — y sits in bottom pad (same as Cost / tok/s: h - padB + 6)
  // Round End = same dot as calls (no larger stroked "anchor" ball). User keeps a ring.
  const labelY = h - bottom + 6;
  pts.forEach((p, i) => {
    const x = xOf(i), y = yOf(p.v);
    p._x = x; p._y = y;
    const isUser = p.kind === "user";
    const isRoundAnchor = p.kind === "round_end" || p.kind === "round_start";
    ctx.fillStyle = isUser ? COST_COLORS.user : color;
    ctx.beginPath();
    ctx.arc(x, y, isUser ? 4 : 3.2, 0, Math.PI * 2);
    ctx.fill();
    if (isUser) {
      ctx.strokeStyle = "#e6edf3";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    if (p.label && p._showX) {
      // User / R{n} must tilt too — excluding them caused collisions while call nums tilted alone.
      drawXLabel(ctx, p.label, x, labelY, !!xPlan.rotate);
    }
  });

  // Hit targets for tooltips
  canvas._ctxPts = pts;
  canvas._ctxGeom = { left, right, top, bottom, plotW, plotH, min, max, xOf, yOf };

  if (!canvas._ctxTipBound) {
    canvas._ctxTipBound = true;
    const tip = () => $("ctxTip");
    canvas.addEventListener("mousemove", (ev) => {
      canvas._ctxPtr = { clientX: ev.clientX, clientY: ev.clientY };
      const t = tip();
      if (document.body.classList.contains("scope-period") || canvas._ratePts) {
        return;
      }
      const list = canvas._ctxPts;
      if (!list || !list.length) return hideChartTip(t);
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      let best = null, bestD = 14;
      for (const p of list) {
        if (p._x == null) continue;
        const d = Math.hypot(mx - p._x, my - p._y);
        if (d < bestD) { bestD = d; best = p; }
      }
      if (!t || !best) return hideChartTip(t);
      const yVal = best.v;
      const ctxY = `<span class="muted">Context</span> <b>${fmtTokens(yVal)}</b>`;
      let lines;
      if (best.kind === "system") {
        lines = [
          `<b>System</b> · R${esc(best.round)} bootstrap`,
          ctxY,
          joinParts([partIn(best.tokens_in, best.cost_in_usd)].filter(Boolean)) || "—",
          Number(best.message_residual_tokens ?? best.tool_definitions_tokens) > 0
            ? `<span class="muted">tool definitions ${fmtTokens(Number(best.tool_definitions_tokens ?? best.message_residual_tokens))}</span>`
            : "",
        ];
        if (best.estimate_usd != null) lines.push(totalPrice(best.estimate_usd));
      } else if (best.kind === "user") {
        lines = [
          `<b>User</b> · R${esc(best.round)}`,
          ctxY,
          joinParts([
            partIn(best.tokens_in, best.cost_in_usd),
            partCached(best.tokens_cached, best.cost_cached_usd),
          ].filter(Boolean)) || "—",
        ];
        if (best.estimate_usd != null) lines.push(totalPrice(best.estimate_usd));
      } else if (best.kind === "round_start" || best.kind === "round_end") {
        lines = [
          `<b>R${esc(best.round)}</b> · end`,
          ctxY + (best.context_start != null
            ? ` <span class="muted">(${fmtTokens(best.context_start)}→${fmtTokens(best.context_end ?? yVal)})</span>`
            : ""),
          joinParts([
            partIn(best.tokens_in, best.cost_in_usd),
            partCached(best.tokens_cached, best.cost_cached_usd),
            partOut(best.tokens_out, best.cost_out_usd),
          ].filter(Boolean)) || "—",
        ];
        if (best.estimate_usd != null) lines.push(totalPrice(best.estimate_usd));
      } else {
        lines = [
          `<b>${esc(best.label || "call")}</b> · R${esc(best.round)} call ${esc(best.call)}`,
          ctxY + (best.context_start != null && best.context_end != null
            ? ` <span class="muted">(${fmtTokens(best.context_start)}→${fmtTokens(best.context_end)})</span>`
            : (best.context_end != null
              ? ` <span class="muted">→ ${fmtTokens(best.context_end)}</span>` : "")),
        ];
        lines.push(joinParts([
          partIn(best.tokens_in, best.cost_in_usd),
          partCached(best.tokens_cached, best.cost_cached_usd),
          partOut(best.tokens_out, best.cost_out_usd),
        ].filter(Boolean)) || "—");
        if (best.thought_tokens || best.thought_chars) {
          const ttok = best.thought_tokens;
          const tch = best.thought_chars;
          let meta = ttok != null ? `${fmtTokens(ttok)} tok` : "";
          if (ttok > 0 && tch > 0) meta += ` · ${(tch / ttok).toFixed(1)} ch/tok`;
          else if (!ttok && tch > 0) meta = `~${fmtTokens(Math.round(tch / 4))} tok`;
          lines.push(`<span class="lbl-thought">thought</span> <span class="muted">${meta}</span>`);
        }
        if (best.estimate_usd != null) lines.push(totalPrice(best.estimate_usd));
      }
      t.style.whiteSpace = "normal";
      t.style.maxWidth = "280px";
      // Viewport-fixed tip (canvas-relative + fixed was too high / clipped).
      placeCostTip(ev, t, lines.filter(Boolean).join("<br>"));
    });
    canvas.addEventListener("mouseleave", () => {
      canvas._ctxPtr = null;
      if (!canvas._ratePts) hideChartTip(tip());
      canvas.style.cursor = "default";
    });
    canvas.addEventListener("click", (ev) => {
      if (canvas._ratePts) return;
      const list = canvas._ctxPts;
      if (!list || !list.length) return;
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      let best = null, bestD = 14;
      for (const p of list) {
        if (p._x == null) continue;
        const d = Math.hypot(mx - p._x, my - p._y);
        if (d < bestD) { bestD = d; best = p; }
      }
      if (best && best.round != null) revealRound(best.round);
    });
  }
  if (canvas._ctxPtr && !document.body.classList.contains("scope-period")) {
    canvas.dispatchEvent(new MouseEvent("mousemove", {
      clientX: canvas._ctxPtr.clientX,
      clientY: canvas._ctxPtr.clientY,
      bubbles: false,
    }));
  }
}


export const COST_Y_LEFT = 56;
export const PLOT_PAD_L = 6;
export const COST_CHART_H = 240;
export const CTX_CHART_H = 260;
const TREE_FLOOR_PX = 120;
const COST_MIN_SLOT = 36;
export const COST_MAX_SLOT = 420;
export const MIN_COST_SLOTS = 8;
export const GANTT_MIN_BAR_PX = 4;
export const GANTT_MIN_SPAN = 5 * 60;
export const GANTT_MAX_H = 920;
export const GANTT_MIN_H = 160;
export const X_AXIS_BAND = 40;

/** Max chart height so cards + charts + Sessions/tree still fit in one viewport. */
function viewportChartCap(resizingPanelId) {
  const main = document.querySelector("main");
  if (!main) return GANTT_MAX_H;
  const mainH = main.clientHeight || (window.innerHeight - 64);
  let used = 0;
  let visible = 0;
  for (const el of main.children) {
    if (!el || el.id === resizingPanelId) continue;
    if (el.classList && el.classList.contains("tree-panel")) continue;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || el.hidden) continue;
    used += el.offsetHeight || 0;
    visible += 1;
  }
  const gap = parseFloat(window.getComputedStyle(main).rowGap || main.style.gap) || 12;
  // +1 gap for the resized panel itself among visible rows
  used += gap * Math.max(0, visible);
  const headSlop = 72; // panel head + toolbar/legend approx inside resized panel
  return Math.max(GANTT_MIN_H, Math.floor(mainH - used - TREE_FLOOR_PX - headSlop));
}

function clampChartH(h, fallback, resizingPanelId) {
  const n = Number(h);
  const base = Number.isFinite(n) ? n : fallback;
  const cap = Math.min(GANTT_MAX_H, viewportChartCap(resizingPanelId));
  return Math.min(cap, Math.max(GANTT_MIN_H, base));
}

export function storedCostChartH() {
  try {
    const n = parseInt(localStorage.getItem("tt-cost-chart-h") || "", 10);
    if (Number.isFinite(n)) return clampChartH(n, COST_CHART_H, "costPanel");
  } catch { /* ignore */ }
  return clampChartH(COST_CHART_H, COST_CHART_H, "costPanel");
}

export function storedCtxChartH() {
  try {
    const n = parseInt(localStorage.getItem("tt-ctx-chart-h") || "", 10);
    if (Number.isFinite(n)) return clampChartH(n, CTX_CHART_H, "ctxPanel");
  } catch { /* ignore */ }
  return clampChartH(CTX_CHART_H, CTX_CHART_H, "ctxPanel");
}

export function ensureCostChartH(store) {
  const st = store || zoomStore() || (window.__costChart = window.__costChart || {});
  if (!st._chartH) st._chartH = storedCostChartH();
  else st._chartH = clampChartH(st._chartH, COST_CHART_H, "costPanel");
  return st._chartH;
}

function resetChartZoom(store) {
  const st = store || zoomStore();
  if (!st) return;
  st._costUserZoom = false;
  st.slotPx = 0;
  st._scrollLeft = 0;
  st._costStickEnd = false;
}

export function chartViewKey() {
  if (document.body.classList.contains("scope-period")) {
    const ag = window.__aggChart || {};
    return ["p", ag.stack || "io", ag.byLabel ? 1 : 0, ag.cumulative ? 1 : 0, ag.normalized ? 1 : 0, ag.timeline ? 1 : 0, ag.rate ? 1 : 0, ag.ioStep ? 1 : 0, ag.rateGrain || "-"].join(":");
  }
  const st = window.__costChart || {};
  return ["s", st.stack || "io", st.byLabel ? 1 : 0, st.drillTurn == null ? "-" : String(st.drillTurn)].join(":");
}

export function planXLabels(ctx, labels, groupW, { temporal } = {}) {
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  let maxW = 0;
  for (const lab of labels || []) {
    const w = ctx.measureText(String(lab || "")).width;
    if (w > maxW) maxW = w;
  }
  // Center-aligned labels collide when half-widths overlap.
  // Require a little slack so we do not tilt on near-miss spacing.
  const collide = maxW > Math.max(4, groupW + 2);
  if (temporal) {
    let every = 1;
    if (groupW < 8) every = 8;
    else if (groupW < 14) every = 4;
    else if (groupW < 22) every = 2;
    if (collide && groupW > 0)
      every = Math.max(every, Math.ceil((maxW + 8) / groupW));
    return { rotate: false, every, padB: 28 };
  }
  if (collide) {
    const padB = Math.max(44, Math.ceil(16 + Math.sin(0.65) * maxW));
    return { rotate: true, every: 1, padB };
  }
  return { rotate: false, every: 1, padB: 28 };
}

export function drawXLabel(ctx, text, x, y, rotate) {
  const s = String(text || "");
  if (!s) return;
  ctx.save();
  ctx.fillStyle = CHART_AXIS.label;
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  if (rotate) {
    ctx.translate(x, y);
    ctx.rotate(-0.65);
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(s, 0, 0);
  } else {
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(s, x, y);
  }
  ctx.restore();
}

export function guessXPlan(labels, barCount, temporal) {
  const scroller = $("costChartScroll");
  const viewW = (scroller && scroller.clientWidth) || 600;
  const nSlots = Math.max(barCount || 1, temporal ? MIN_COST_SLOTS : 1);
  const groupW = Math.max(1, (viewW - PLOT_PAD_L - 12) / nSlots);
  return planXLabels(measureCtx(), labels, groupW, { temporal });
}

export function applyXPadHeight(canvas, padB) {
  if (!canvas) return;
  const store = zoomStore() || {};
  const base = ensureCostChartH(store);
  const extra = Math.max(0, (padB || 28) - 28);
  canvas.style.height = (base + extra) + "px";
}

export function pointerInXAxis(ev, wrap) {
  if (!wrap) return false;
  const r = wrap.getBoundingClientRect();
  const sb = 16;
  const top = r.bottom - X_AXIS_BAND;
  const bot = r.bottom - sb;
  return ev.clientY >= top && ev.clientY <= bot;
}

export function zoomStore() {
  if (document.body.classList.contains("scope-period")) {
    if (!window.__aggChart) window.__aggChart = {};
    return window.__aggChart;
  }
  return window.__costChart;
}

function resetCostCanvasFit(canvas) {
  if (!canvas) return;
  canvas.style.width = "100%";
  const scroller = $("costChartScroll");
  if (scroller) scroller.classList.remove("is-overflow");
}

export function slotRange(viewW, barCount, maxSlot) {
  const nSlots = Math.max(barCount, MIN_COST_SLOTS);
  const plotAvail = Math.max(10, viewW - PLOT_PAD_L - 12);
  const minSlot = plotAvail / nSlots;
  const cap = maxSlot || COST_MAX_SLOT;
  return { nSlots, minSlot, maxSlot: Math.max(cap, minSlot), plotAvail };
}

export function clampYViewZoom(z) {
  return Math.min(12, Math.max(1, z));
}

function applyRateYZoom(store, next) {
  if (!store) return;
  const z1 = clampYViewZoom(next);
  if (Math.abs(z1 - (store._rateYZoom || 1)) < 0.001) return;
  store._rateYZoom = z1;
  const canvas = $("costChart");
  if (canvas && canvas._rateRedraw) canvas._rateRedraw();
  else redrawCostChart();
}

function applyYViewZoom(store, next, anchorClientY) {
  if (!store) return;
  const n = ganttRowCount(store);
  const z0 = store._yViewZoom || 1;
  const z1 = clampYViewZoom(next);
  const g = store._ganttGeom;
  if (n > 0 && g && anchorClientY != null) {
    const focus = yIndexAt(anchorClientY, store);
    const vis1 = Math.max(1, n / z1);
    const rowH1 = g.plotH / vis1;
    const yEl = $("costYAxis");
    const top = yEl ? yEl.getBoundingClientRect().top : 0;
    store._yViewPan = clampYPan(focus - (anchorClientY - top - g.padT) / rowH1, n, vis1);
  }
  store._yViewZoom = z1;
  if (Math.abs(z1 - z0) < 0.001 && anchorClientY == null) return;
  redrawCostChart();
}

function ganttRowCount(store) {
  const rows = (store && store._ganttRows) || (store && store.agg && store.agg.sessions) || [];
  return rows.length;
}

export function clampYPan(pan, n, visN) {
  return Math.min(Math.max(0, n - visN), Math.max(0, pan));
}

function yIndexAt(clientY, store) {
  const g = store._ganttGeom;
  const n = ganttRowCount(store);
  if (!g || n <= 0) return 0;
  const yEl = $("costYAxis");
  const top = yEl ? yEl.getBoundingClientRect().top : 0;
  const visN = Math.max(1, n / (store._yViewZoom || 1));
  const rowH = g.plotH / visN;
  return (store._yViewPan || 0) + (clientY - top - g.padT) / rowH;
}

let _ganttSelectFn = null;
export function onGanttSelect(fn) {
  _ganttSelectFn = fn;
}

export function bindYStretch() {
  const y = $("costYAxis");
  if (!y || y._yStretch) return;
  y._yStretch = true;
  y.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const store = zoomStore();
    if (!store) return;
    if (store.rate || store.ioStep) {
      applyRateYZoom(store, (store._rateYZoom || 1) * Math.pow(1.12, -ev.deltaY / 80));
      return;
    }
    const cur = store._yViewZoom || 1;
    applyYViewZoom(store, cur * Math.pow(1.12, -ev.deltaY / 80), ev.clientY);
  }, { passive: false });
  y.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    const store = zoomStore();
    if (!store) return;
    y.setPointerCapture(ev.pointerId);
    y._ydrag = {
      y0: ev.clientY,
      z0: (store.rate || store.ioStep) ? (store._rateYZoom || 1) : (store._yViewZoom || 1),
      rate: !!(store.rate || store.ioStep),
      moved: false,
    };
  });
  y.addEventListener("pointermove", (ev) => {
    const drag = y._ydrag;
    if (!drag) return;
    if (Math.abs(ev.clientY - drag.y0) > 3) drag.moved = true;
    if (!drag.moved) return;
    const store = zoomStore();
    if (!store) return;
    const dy = drag.y0 - ev.clientY;
    const next = drag.z0 * Math.pow(1.02, dy / 4);
    if (drag.rate || store.rate || store.ioStep) applyRateYZoom(store, next);
    else applyYViewZoom(store, next, drag.y0);
  });
  const endY = (ev) => {
    const drag = y._ydrag;
    y._ydrag = null;
    if (!drag || drag.moved) return;
    const store = zoomStore();
    if (!store || !store.timeline) return;
    const idx = Math.floor(yIndexAt(ev.clientY, store));
    const rows = store._ganttRows || [];
    const hit = rows[idx];
    if (hit && _ganttSelectFn) _ganttSelectFn(hit.session_id);
  };
  y.addEventListener("pointerup", endY);
  y.addEventListener("pointercancel", () => { y._ydrag = null; });
}

export function bindChartResize() {
  const btn = $("chartResize");
  const wrap = $("costChartWrap");
  if (!btn || !wrap || btn._bound) return;
  btn._bound = true;
  btn.hidden = false;
  btn.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    btn.setPointerCapture(ev.pointerId);
    const store = zoomStore() || window.__costChart || (window.__costChart = {});
    btn._rd = {
      y0: ev.clientY,
      h0: ensureCostChartH(store),
    };
  });
  btn.addEventListener("pointermove", (ev) => {
    const d = btn._rd;
    if (!d) return;
    const store = zoomStore() || window.__costChart || (window.__costChart = {});
    const next = clampChartH(d.h0 + (ev.clientY - d.y0), COST_CHART_H, "costPanel");
    store._chartH = next;
    if (window.__aggChart) window.__aggChart._chartH = next;
    if (window.__costChart) window.__costChart._chartH = next;
    const canvas = $("costChart");
    if (canvas) canvas.style.height = next + "px";
    const y = $("costYAxis");
    if (y) y.style.height = next + "px";
    redrawCostChart();
  });
  const end = () => {
    if (btn._rd) {
      try {
        const store = zoomStore() || window.__costChart;
        if (store && store._chartH)
          localStorage.setItem("tt-cost-chart-h", String(Math.round(store._chartH)));
      } catch { /* ignore */ }
    }
    btn._rd = null;
  };
  btn.addEventListener("pointerup", end);
  btn.addEventListener("pointercancel", end);
}

export function bindCtxChartResize(redrawFn) {
  const btn = $("ctxChartResize");
  const wrap = $("ctxChartWrap");
  if (!btn || !wrap || btn._bound) return;
  btn._bound = true;
  btn.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    btn.setPointerCapture(ev.pointerId);
    btn._rd = { y0: ev.clientY, h0: storedCtxChartH() };
  });
  btn.addEventListener("pointermove", (ev) => {
    const d = btn._rd;
    if (!d) return;
    const next = clampChartH(d.h0 + (ev.clientY - d.y0), CTX_CHART_H, "ctxPanel");
    try { localStorage.setItem("tt-ctx-chart-h", String(Math.round(next))); } catch { /* ignore */ }
    const canvas = $("ctxChart");
    if (canvas) canvas.style.height = next + "px";
    const y = $("ctxYAxis");
    if (y) y.style.height = next + "px";
    if (typeof redrawFn === "function") redrawFn();
  });
  const end = () => { btn._rd = null; };
  btn.addEventListener("pointerup", end);
  btn.addEventListener("pointercancel", end);
}

export function setGanttChrome(on) {
  const wrap = $("costChartWrap");
  if (wrap) wrap.classList.toggle("is-gantt", !!on);
  const btn = $("chartResize");
  if (btn) btn.hidden = false;
  bindChartResize();
}

export function layoutGanttCanvas(canvas) {
  const yAxis = $("costYAxis");
  if (yAxis) yAxis.hidden = false;
  const scroller = $("costChartScroll");
  let viewW = (scroller && scroller.clientWidth) || 0;
  if (!viewW) viewW = (canvas.parentElement && canvas.parentElement.clientWidth) || 600;
  const store = zoomStore();
  const h = ensureCostChartH(store);
  canvas.style.width = viewW + "px";
  canvas.style.height = h + "px";
  if (yAxis) yAxis.style.height = h + "px";
  if (scroller) scroller.classList.remove("is-overflow");
  const { ctx } = fitCanvas(canvas, viewW, h);
  ctx.clearRect(0, 0, viewW, h);
  bindYStretch();
  bindChartResize();
  return { w: viewW, h, ctx };
}

export function layoutCostCanvas(canvas, barCount, { forceFit, maxSlot } = {}) {
  const yAxis = $("costYAxis");
  if (yAxis && barCount > 0) yAxis.hidden = false;
  const scroller = $("costChartScroll");
  let viewW = (scroller && scroller.clientWidth) || 0;
  if (!viewW) viewW = (canvas.parentElement && canvas.parentElement.clientWidth) || 600;
  const store = zoomStore();
  let h = ensureCostChartH(store);
  canvas.style.height = h + "px";
  if (yAxis) yAxis.style.height = h + "px";
  bindChartResize();
  const vk = chartViewKey();
  if (store._zoomViewKey !== vk) {
    resetChartZoom(store);
    store._zoomViewKey = vk;
  }
  const keepScroll = store._costUserZoom || store._costStickEnd === false;
  const prevScroll = scroller
    ? (store._scrollLeft != null ? store._scrollLeft : scroller.scrollLeft)
    : 0;
  bindYStretch();
  const zoomCap = maxSlot || canvas._zoomMaxSlot || COST_MAX_SLOT;
  canvas._zoomMaxSlot = zoomCap;
  const { nSlots, minSlot, maxSlot: slotCap } = slotRange(viewW, barCount, zoomCap);
  let slot = store.slotPx;
  // Default + mode switches: fit the full range (fully zoomed out).
  // Only keep a custom slot after the user wheel-zooms.
  if (store._costUserZoom && slot > 0) {
    slot = Math.min(slotCap, Math.max(minSlot, slot));
  } else {
    slot = minSlot;
  }
  store.slotPx = slot;
  const w = PLOT_PAD_L + 12 + nSlots * slot;
  const overflow = w > viewW + 1;
  // Never CSS-stretch a narrower bitmap — that desyncs hit-test vs bars.
  canvas.style.width = w + "px";
  if (scroller) scroller.classList.toggle("is-overflow", overflow);
  const { ctx } = fitCanvas(canvas, w, h);
  ctx.clearRect(0, 0, w, h);
  if (scroller) {
    const max = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
    if (overflow && !keepScroll && store._costStickEnd !== false && !store.timeline) {
      scroller.scrollLeft = max;
      store._scrollLeft = scroller.scrollLeft;
    } else if (keepScroll) {
      scroller.scrollLeft = Math.min(max, Math.max(0, prevScroll));
      store._scrollLeft = scroller.scrollLeft;
    }
  }
  if (scroller && !scroller._stickBound) {
    scroller._stickBound = true;
    scroller.addEventListener("scroll", () => {
      const st = zoomStore();
      st._scrollLeft = scroller.scrollLeft;
      const max = scroller.scrollWidth - scroller.clientWidth;
      st._costStickEnd = max > 0 && scroller.scrollLeft >= max - 12;
    });
  }
  canvas._zoomBars = barCount;
  bindCostZoom();
  return { w, h, ctx, overflow, slot, nSlots, minSlot };
}

export function redrawCostChart() {
  const canvas = $("costChart");
  if (!canvas) return;
  if (document.body.classList.contains("scope-period")) {
    const ag = window.__aggChart;
    if (ag && ag.ioStep && ag.ioStepPts) {
      drawIoStepChart(canvas, ag.ioStepPts, ag.ioStepOpts || {});
      return;
    }
    if (ag && ag.rate && ag.ratePts) {
      drawRateChart(canvas, ag.ratePts, ag.rateOpts || { host: "cost" });
      return;
    }
    if (ag && ag.timeline && ag.agg) drawTimeline(canvas, ag.agg);
    else if (ag) drawAggBars(canvas, ag.buckets, ag);
    return;
  }
  const st = window.__costChart;
  if (st) drawBars(canvas, st.turns, st.rounds);
}

export function bindCostZoom() {
  const wrap = $("costChartWrap");
  if (!wrap || wrap._zoomBound) return;
  wrap._zoomBound = true;
  wrap.addEventListener("mousemove", (ev) => {
    const st = zoomStore();
    wrap.title = (st && st.timeline) || pointerInXAxis(ev, wrap)
      ? "Scroll to zoom horizontal scale"
      : "";
  });
  wrap.addEventListener("mouseleave", () => { wrap.title = ""; });
  wrap.addEventListener("wheel", (ev) => {
    if (ev.shiftKey) return;
    if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;
    if (ev.target && ev.target.id === "costYAxis") return;
    const canvas = $("costChart");
    const scroller = $("costChartScroll");
    if (!canvas) return;
    const store = zoomStore();
    if (store && store.timeline && store._gt0 != null) {
      if (ev.target && ev.target.id === "costYAxis") return;
      const pack = canvas._aggHit || {};
      const p0 = pack.p0;
      const p1 = pack.p1;
      if (!(p1 > p0)) return;
      ev.preventDefault();
      const r = canvas.getBoundingClientRect();
      const padL = PLOT_PAD_L;
      const plotW = pack.plotW || Math.max(10, r.width - padL - 18);
      const frac = Math.min(1, Math.max(0, (ev.clientX - r.left - padL) / plotW));
      const span0 = store._gt1 - store._gt0;
      const tAt = store._gt0 + frac * span0;
      const nextSpan = Math.min(p1 - p0, Math.max(GANTT_MIN_SPAN, span0 * Math.pow(1.18, ev.deltaY / 80)));
      const nxt = clampGanttWindow(tAt - frac * nextSpan, tAt - frac * nextSpan + nextSpan, p0, p1);
      store._gt0 = nxt.t0;
      store._gt1 = nxt.t1;
      if (store.agg) drawTimeline(canvas, store.agg);
      return;
    }
    if (!pointerInXAxis(ev, wrap) && !(store && (store.rate || store.ioStep))) return;
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
    redrawCostChart();
    if (scroller) {
      const max = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
      scroller.scrollLeft = Math.min(max, Math.max(0, t * newW - mx));
      store._scrollLeft = scroller.scrollLeft;
    }
  }, { passive: false });
}

function drawCostYOverlay({ min, max, step, unit, top, bottom, h }) {
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
  const plotH = Math.max(1, h - top - bottom);
  const span = (max - min) || 1;
  const yOf = (v) => top + plotH - ((v - min) / span) * plotH;
  ctx.fillStyle = CHART_AXIS.label;
  ctx.font = "11px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  eachCostYTick(min, max, step, unit, (v) => {
    ctx.fillText(fmtCostAxis(v, unit), w - 8, yOf(v));
  });
}

function eachCostYTick(yMin, max, step, unit, fn) {
  if (!(step > 0) || !Number.isFinite(step) || !Number.isFinite(max)) {
    fn(0);
    return;
  }
  const span = Math.max(0, max - yMin);
  const n = Math.max(1, Math.round(span / step));
  const count = Math.min(8, n);
  const seen = new Set();
  for (let i = 0; i <= count; i++) {
    let v = yMin + (span * i) / count;
    v = Math.round(v / step) * step;
    if (v < yMin - step * 0.01 || v > max + step * 0.01) continue;
    if (unit === "tokens") v = Math.round(v);
    const key = unit === "tokens" ? String(v) : v.toFixed(6);
    if (seen.has(key)) continue;
    seen.add(key);
    fn(v);
  }
  if (!seen.size) fn(yMin);
}

/** Official line only when the bill is non-zero. Codex list-price estimate stays. */
function officialAmount(v) {
  const n = Number(v);
  return Number.isFinite(n) && n !== 0 ? n : null;
}

export function drawBars(canvas, turns, rounds, opts) {
  if (canvas) {
    clearCostPointerProps(canvas);
    setCostTipOwner(canvas, "session");
  }
  clearRateHost("cost");
  const st = window.__costChart;
  st.turns = turns || [];
  st.rounds = rounds || [];
  if (opts && opts.superAgent != null) st.superAgent = !!opts.superAgent;
  const tip = $("costTip");
  const backBtn = $("costDrillBack");
  if (backBtn) backBtn.hidden = st.drillTurn == null;

  let bars = [];
  if (st.drillTurn != null) {
    if (st.drillTurn === "sys" || String(st.drillTurn).startsWith("C")) {
      // System / Compact bars are not drillable into LLM calls
      st.drillTurn = null;
    }
  }
  if (st.drillTurn != null) {
    const round = findRound(st.rounds, st.drillTurn);
    const steps = (round && round.model_steps) || [];
    bars = [];
    const userBar = userCostBar(round, { stack: st.stack || "io" });
    if (userBar) bars.push(userBar);
    steps.forEach((s, i) => {
      const raw = callCostParts(s, s.index ?? i + 1, round, { omitUser: !!userBar });
      const b = applyDrillStack(raw, st.stack || "io");
      b.turnIndex = st.drillTurn;
      attachSubagentSegsToTurn(b, { model_steps: [s] }, st.stack);
      bars.push(b);
    });
  } else {
    const slice = turns || [];
    // Separate System bar before Round 1 when bootstrap system exists
    const r1 = findRound(st.rounds, 1)
      || (st.rounds || []).find(r => r && r.system_prompt)
      || null;
    const hasR1InSlice = slice.some(t => Number(t.index) === 1)
      || (r1 && slice.length && Number(slice[0].index) === Number(r1.index));
    const sysBar = (r1 && hasR1InSlice)
      ? systemCostBar(r1, { stack: st.stack || "io" })
      : null;
    bars = [];
    if (sysBar) bars.push(sysBar);

    // Compaction / recap counters are session-global (chronological)
    const sliceIdx = new Set(slice.map(t => Number(t.index)));
    let compactN = 0;
    let recapN = 0;
    const minSlice = slice.length
      ? Math.min(...slice.map(t => Number(t.index)))
      : Infinity;
    for (const r of (st.rounds || [])) {
      if (!r || Number(r.index) >= minSlice) break;
      if (r.compact_after && r.compact_after.kind === "compaction") compactN += 1;
      recapN += (r.recaps_after || []).length;
    }
    const pushBetween = (items) => {
      items.sort((a, b) => eventMs(a.ev) - eventMs(b.ev));
      for (const it of items) {
        if (it.kind === "compact") {
          compactN += 1;
          if (it.ev && it.ev.compact_index == null) it.ev.compact_index = compactN;
          // Stamp N onto following User.compact_out when this compact is between-rounds.
          const unify = st.stack === "parts" || st.stack === "tools";
          const cb = compactCostBar(it.ev, compactN, { detail: unify });
          if (cb) bars.push(cb);
        } else {
          recapN += 1;
          const rb = recapCostBar(it.ev, recapN, { detail: st.stack === "parts" || st.stack === "tools" });
          if (rb) bars.push(rb);
        }
      }
    };
    for (const t of slice) {
      const round = findRound(st.rounds, t.index);
      const peelSystem = !!(sysBar && Number(t.index) === Number(r1 && r1.index || 1));
      // Events *before* this round when previous round is outside the slice
      // (same objects as previous.*_after — avoid double-draw).
      const prevInSlice = sliceIdx.has(Number(t.index) - 1);
      if (!prevInSlice && round) {
        const prevRound = findRound(st.rounds, Number(t.index) - 1);
        const before = [];
        const prevHasCompactAfter = !!(
          prevRound && prevRound.compact_after && prevRound.compact_after.kind === "compaction"
        );
        if (round.compact_before && round.compact_before.kind === "compaction" && !prevHasCompactAfter) {
          before.push({ kind: "compact", ev: round.compact_before });
        }
        const prevAfterRecaps = new Set(prevRound && prevRound.recaps_after ? prevRound.recaps_after : []);
        for (const rec of (round.recaps_before || [])) {
          if (!prevAfterRecaps.has(rec)) before.push({ kind: "recap", ev: rec });
        }
        pushBetween(before);
      }
      const stack = st.stack || "io";
      // Propagate Compact N onto User.compact_out for Tools labels.
      if (round && round.compact_before && round.compact_before.kind === "compaction") {
        const cn = round.compact_before.compact_index;
        const up = round.user_prompt;
        if (cn != null && up && up.compact_out && up.compact_out.compact_index == null)
          up.compact_out.compact_index = cn;
      }
      const parts = stack === "tools"
        ? turnCostPartsTools(t, round, { peelSystem })
        : turnCostParts(t, round, { detail: stack === "parts", peelSystem });
      bars.push(attachSubagentSegsToTurn(parts, round, stack));
      // Events after this round (between R[n] and R[n+1]), chronological
      const after = [];
      const seenCompactMs = new Set();
      if (round && round.compact_after && round.compact_after.kind === "compaction") {
        after.push({ kind: "compact", ev: round.compact_after });
        if (round.compact_after.agent_ms != null) seenCompactMs.add(round.compact_after.agent_ms);
      }
      for (const s of (round && round.model_steps) || []) {
        for (const c of s.compacts_after || []) {
          if (!c || c.kind !== "compaction") continue;
          if (c.agent_ms != null && seenCompactMs.has(c.agent_ms)) continue;
          after.push({ kind: "compact", ev: c });
          if (c.agent_ms != null) seenCompactMs.add(c.agent_ms);
        }
      }
      for (const rec of (round && round.recaps_after) || []) {
        after.push({ kind: "recap", ev: rec });
      }
      pushBetween(after);
    }
  }

  if (!(st.hiddenLegend instanceof Set)) st.hiddenLegend = new Set();
  const isDrill0 = st.drillTurn != null;
  if (isDrill0 !== !!st._wasDrill) {
    st.hiddenLegend = new Set();
  }
  st._wasDrill = isDrill0;
  const unit0 = st.unit === "tokens" ? "tokens" : "usd";
  const labelLegend = st.byLabel ? collectLabelLegend(bars, unit0) : [];
  if (st.byLabel && bars.length) {
    bars = aggregateBarsByLabel(bars, st.hiddenLegend, unit0);
    const customOrd = loadLegendOrder(st.stack);
    if (customOrd && customOrd.length) {
      bars = bars.slice().sort((a, b) => {
        const ka = costSegKey((a.segs || [])[0]) || a.label;
        const kb = costSegKey((b.segs || [])[0]) || b.label;
        const ia = customOrd.indexOf(ka);
        const ib = customOrd.indexOf(kb);
        const ra = ia >= 0 ? ia : customOrd.length + defaultStackRank(ka, (a.segs || [])[0], st.stack);
        const rb = ib >= 0 ? ib : customOrd.length + defaultStackRank(kb, (b.segs || [])[0], st.stack);
        return ra - rb;
      });
    } else if (st.stack === "parts" || st.stack === "tools") {
      const metric = (b) => {
        const vis = (b.segs || []).reduce((a, s) => a + costSegMetric(s, unit0), 0);
        if (vis > 0) return vis;
        return unit0 === "tokens" ? (Number(b.total_tok) || 0) : (Number(b.total) || 0);
      };
      bars = bars.slice().sort((a, b) => metric(a) - metric(b));
    }
  }
  applyXPadHeight(canvas, guessXPlan(
    bars.map((p) => p.label || String(p.index)),
    bars.length,
    false
  ).padB);

  if (!bars.length) {
    resetCostCanvasFit(canvas);
    const yAxis = $("costYAxis");
    if (yAxis) yAxis.hidden = true;
    const empty = layoutCostCanvas(canvas, 0, { forceFit: true });
    drawChartEmpty(
      empty.ctx, empty.w, empty.h,
      st.drillTurn != null ? "No LLM calls in this round" : "No completed rounds yet"
    );
    canvas._barHit = [];
    canvas._legendHit = [];
    renderCostLegend([], st.hiddenLegend);
    hideChartTip(tip);
    return;
  }

  const { w, h, ctx, overflow } = layoutCostCanvas(canvas, bars.length);

  const unit = st.unit === "tokens" ? "tokens" : "usd";
  const isDrill = st.drillTurn != null;
  const hidden = st.hiddenLegend;
  if (hidden.has("sub") || hidden.has("Sub")) {
    bars = bars.filter((b) => b.kind !== "subagent");
  }

  // Visible stack per bar (legend click hides categories for granularity)
  const visStacks = bars.map(p => {
    const raw = p.segs && p.segs.length
      ? p.segs
      : [{ k: "out", label: "—", v: p.total || 0, tok: p.total_tok || 0, color: COST_COLORS.out }];
    const filtered = raw.filter(s => {
      const m = costSegMetric(s, unit);
      return m > 0 && !isCostSegHidden(s, hidden);
    });
    return orderCostSegs(filtered, st.stack);
  });
  let rawMax = 0;
  bars.forEach((p, i) => {
    const visTot = visStacks[i].reduce((a, s) => a + costSegMetric(s, unit), 0);
    const offAmt = officialAmount(p.official);
    const off = (!hidden.has("official") && unit === "usd" && offAmt != null)
      ? offAmt : 0;
    rawMax = Math.max(rawMax, visTot, off, 0);
  });
  if (rawMax <= 0) rawMax = unit === "tokens" ? 1000 : 0.0001;
  rawMax = rawMax / clampYViewZoom(st._yViewZoom || 1);
  const { min: yMin, max, step: yStep } = niceCostYMaxForUnit(rawMax, unit);

  const left = PLOT_PAD_L, right = 12, top = 12;
  const nSlots0 = Math.max(bars.length, MIN_COST_SLOTS);
  const groupW0 = Math.max(1, (w - left - right) / nSlots0);
  const xPlan = planXLabels(
    ctx,
    bars.map((p) => p.label || String(p.index)),
    groupW0,
    { temporal: false }
  );
  const bottom = xPlan.padB;
  const plotW = w - left - right;
  const plotH = h - top - bottom;
  const nSlots = nSlots0;
  const groupW = plotW / nSlots;
  const bw = Math.max(1, Math.min(44, groupW * (groupW < 8 ? 0.9 : groupW < 18 ? 0.72 : 0.58)));
  const yOf = (v) => top + plotH - ((v - yMin) / (max - yMin)) * plotH;

  ctx.strokeStyle = CHART_AXIS.grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = CHART_AXIS.label;
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  eachCostYTick(yMin, max, yStep, unit, (v) => {
    const y = yOf(v);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w - right, y); ctx.stroke();
  });
  ctx.textAlign = "left";
  drawCostYOverlay({ min: yMin, max, step: yStep, unit, top, bottom, h });

  const hit = [];
  const legendMap = new Map();
  // Draw empty X placeholders first (no bars)
  for (let i = bars.length; i < nSlots; i++) {
    const x0 = left + i * groupW + (groupW - bw) / 2;
    ctx.fillStyle = CHART_AXIS.placeholder;
    ctx.font = "10px system-ui, Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("—", x0 + bw / 2, h - 8);
    ctx.textAlign = "left";
  }
  bars.forEach((p, i) => {
    const x0 = left + i * groupW + (groupW - bw) / 2;
    let yBase = top + plotH;
    const stack = visStacks[i];
    // Full segs still feed the legend (so hidden cats remain clickable)
    // Only categories present in *this* view (drill or overview).
    (p.segs || stack).forEach(seg => {
      if (!(costSegMetric(seg, unit) > 0 || seg.v > 0 || seg.tok > 0)) return;
      const key = costSegKey(seg);
      if (!key) return;
      if (!legendMap.has(key))
        legendMap.set(key, { label: costDisplayLabel(seg) || key, color: seg.color, k: seg.k });
    });
    const segHits = [];
    stack.forEach(seg => {
      const mv = costSegMetric(seg, unit);
      const segH = ((mv - 0) / (max - yMin)) * plotH;
      yBase -= segH;
      if (segH > 0.4) {
        ctx.fillStyle = seg.color;
        ctx.fillRect(x0, yBase, bw, segH);
      }
      segHits.push({ y0: yBase, y1: yBase + segH, seg });
    });
    if (unit === "usd" && officialAmount(p.official) != null && !hidden.has("official")) {
      const oy = yOf(p.official);
      ctx.strokeStyle = COST_COLORS.official;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x0 - 2, oy);
      ctx.lineTo(x0 + bw + 2, oy);
      ctx.stroke();
    }
    if (p.kind === "subagent") {
      ctx.strokeStyle = COST_COLORS.sub;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x0 + 0.5, yBase + 0.5, bw - 1, (top + plotH) - yBase - 1);
    }
    if (i % xPlan.every === 0)
      drawXLabel(ctx, p.label || String(p.index), x0 + bw / 2, h - bottom + 6, xPlan.rotate);
    // attach visible segs for tooltip
    const visTot = stack.reduce((a, s) => a + costSegMetric(s, unit), 0);
    hit.push({
      x0, x1: x0 + bw, y0: top, y1: top + plotH,
      p: { ...p, segs: stack, total: visTot, _unit: unit },
      segs: segHits,
    });
  });

  // HTML chip legend — all categories present in this view only
  // (no stale hidden tools from previous mode)
  let legendItems = [...legendMap.entries()].map(([key, meta]) => [key, meta]);
  if (st.byLabel && labelLegend.length) {
    legendItems = labelLegend.slice();
  } else if (st.drillTurn == null && st.stack === "io") {
    legendItems = [
      ["system", { label: "System", color: COST_COLORS.system, k: "system" }],
      ["in", { label: "In", color: COST_COLORS.in, k: "in" }],
      ["cached", { label: "Cached", color: COST_COLORS.cached, k: "cached" }],
      ["out", { label: "Out", color: COST_COLORS.out, k: "out" }],
    ];
    if (bars.some((b) => b.kind === "subagent" || (b.segs || []).some((s) => s.k === "sub")))
      legendItems.push(["sub", { label: "Sub agent", color: COST_COLORS.sub, k: "sub" }]);
    if (unit === "usd" && bars.some((b) => officialAmount(b.official) != null))
      legendItems.push(["official", { label: "Official", color: COST_COLORS.official }]);
  } else if (st.drillTurn == null && unit === "usd") {
    if (
      bars.some((b) => officialAmount(b.official) != null)
      && !legendItems.some(([key]) => key === "official")
    )
      legendItems.unshift(["official", { label: "official", color: COST_COLORS.official }]);
  }
  // Re-show hidden chips only if that category exists in *current* legendMap
  for (const name of hidden) {
    if (legendMap.has(name)) continue; // already from segs
    if (!legendItems.some(([key]) => key === name)) {
      // only if it was a fixed overview key still relevant
      const fixedOk = ["System", "In", "Cached", "Out", "official", "in", "cached", "out", "system"].includes(name);
      if (!fixedOk) continue; // drop stale tool hides from UI
      if (name === "official" && !bars.some((b) => officialAmount(b.official) != null)) continue;
      const fallback =
        name === "System" || name === "system" ? COST_COLORS.system
        : name === "In" || name === "in" ? COST_COLORS.in
        : name === "Cached" || name === "cached" ? COST_COLORS.cached
        : name === "Out" || name === "out" ? COST_COLORS.out
        : name === "official" ? COST_COLORS.official
        : COST_COLORS.residual;
      legendItems.push([name, { label: name, color: fallback }]);
    }
  }
  legendItems = sortLegendItems(legendItems, st.stack);
  renderCostLegend(legendItems, hidden);

  canvas._barHit = hit;
  canvas._legendHit = []; // legend is HTML now

  if (!canvas._tipBound) {
    canvas._tipBound = true;
    canvas.addEventListener("mousemove", (ev) => {
      canvas._ptr = { clientX: ev.clientX, clientY: ev.clientY };
      if (canvas._costTipOwner !== "session") return;
      const tipEl = $("costTip");
      if (!tipEl || !canvas._barHit) return;
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const found = canvas._barHit.find(b => mx >= b.x0 && mx <= b.x1 && my >= b.y0 && my <= b.y1);
      if (!found) {
        canvas.style.cursor = "default";
        hideChartTip(tipEl);
        return;
      }
      const overSub = (found.segs || []).some((h) =>
        h && h.seg && h.seg.session_id && my >= h.y0 && my <= h.y1
      );
      canvas.style.cursor = (!st.byLabel && (found.p.kind === "turn" || found.p.kind === "subagent" || overSub))
        ? "pointer" : "default";
      const p = found.p;
      tipEl.style.whiteSpace = "normal";
      tipEl.style.maxWidth = "300px";
      const head = p.kind === "call"
        ? ("LLM call " + p.index)
        : (p.kind === "system" ? "System"
          : (p.kind === "user" ? "User"
            : (p.kind === "compact" ? ("Compact " + p.label)
              : (p.kind === "recap" ? ("Recap " + p.label)
                : (p.kind === "agg-label" ? String(p.label || "")
                : (p.kind === "subagent"
                  ? ("Sub Agent " + String(p.label || "").replace(/^S/, "") + (p.title ? " · " + p.title : ""))
                  : ("Round " + p.index)))))));
      const lines = [`<b>${esc(head)}</b>`];
      if (p.kind === "compact" && (p.tokens_before != null || p.tokens_after != null)) {
        lines.push(
          `<span class="muted">${fmtTokens(p.tokens_before)}${AR}${fmtTokens(p.tokens_after)}</span>`
        );
      }
      const u = p._unit || window.__costChart.unit || "usd";
      const stackMode = (window.__costChart && window.__costChart.stack) || "io";
      if (p.kind === "subagent") {
        if (p.uncached_tokens || p.in)
          lines.push(`<span style="color:${COST_COLORS.in}">●</span> In ${fmtTokens(p.uncached_tokens)} · ${fmtUsd(p.in)}`);
        if (p.cached_tokens || p.cached)
          lines.push(`<span style="color:${COST_COLORS.cached}">●</span> Cached ${fmtTokens(p.cached_tokens)} · ${fmtUsd(p.cached)}`);
        if (p.out_tokens || p.out)
          lines.push(`<span style="color:${COST_COLORS.out}">●</span> Out ${fmtTokens(p.out_tokens)} · ${fmtUsd(p.out)}`);
      } else {
        tipOrderedSegs(p.segs || [], stackMode, u).forEach((s) => {
          lines.push(formatTipSegLine(s, u, null));
        });
      }
      lines.push(`<b>→ ${fmtCostAxis(p.total || 0, u)}</b>`);
      if (u === "usd" && officialAmount(p.official) != null && !window.__costChart.hiddenLegend?.has("official"))
        lines.push(`<span class="muted">official ${fmtUsd(p.official)}</span>`);
      if (p.kind === "turn") lines.push(`<span class="muted">click: tree + drill calls</span>`);
      if (p.kind === "subagent" || (p.segs || []).some((s) => s && s.session_id))
        lines.push(`<span class="muted">click: open this sub-agent tab</span>`);
      placeCostTip(ev, tipEl, lines.join("<br>"));
    });
    canvas.addEventListener("mouseleave", () => {
      canvas._ptr = null;
      if (canvas._costTipOwner !== "session") return;
      hideChartTip($("costTip"));
      canvas.style.cursor = "default";
    });
    canvas.addEventListener("click", (ev) => {
      if (canvas._costTipOwner !== "session") return;
      if (!canvas._barHit) return;
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const found = canvas._barHit.find(b => mx >= b.x0 && mx <= b.x1 && my >= b.y0 && my <= b.y1);
      if (!found || !found.p) return;
      if (found.p.kind === "subagent" && found.p.session_id) {
        if (typeof window.__switchTaskTab === "function")
          window.__switchTaskTab(found.p.session_id);
        return;
      }
      const subHit = (found.segs || []).slice().reverse().find((h) =>
        h && h.seg && h.seg.session_id && my >= h.y0 && my <= h.y1
      );
      if (subHit && subHit.seg.session_id) {
        if (typeof window.__switchTaskTab === "function")
          window.__switchTaskTab(subHit.seg.session_id);
        return;
      }
      if (found.p.kind === "agg-label") return;
      if (found.p.kind === "turn") {
        window.__costChart.drillTurn = found.p.index;
        drawBars(canvas, window.__costChart.turns, window.__costChart.rounds);
        focusRound(found.p.index);
      }
    });
  }
  if (canvas._ptr && canvas._costTipOwner === "session") {
    canvas.dispatchEvent(new MouseEvent("mousemove", {
      clientX: canvas._ptr.clientX,
      clientY: canvas._ptr.clientY,
      bubbles: false,
    }));
  }
}

function _redrawCostAfterLegend() {
  const canvas = $("costChart");
  if (!canvas) return;
  if (document.body.classList.contains("scope-period")) redrawCostChart();
  else {
    const st = window.__costChart;
    if (st) drawBars(canvas, st.turns, st.rounds);
  }
}

export function renderCostLegend(legendItems, hidden) {
  const el = $("costLegend");
  if (!el) return;
  if (!(hidden instanceof Set)) hidden = new Set();
  // legendItems: [key, {label, color}] or legacy [label, color]
  const chips = (legendItems || []).map((entry) => {
    let key, label, color;
    if (Array.isArray(entry) && entry[1] && typeof entry[1] === "object" && entry[1].color) {
      key = entry[0];
      label = entry[1].label || key;
      color = entry[1].color;
    } else {
      key = entry[0];
      label = entry[0];
      color = entry[1];
    }
    const show = label;
    const hid = hidden.has(key) || hidden.has(label);
    return `<button type="button" class="leg-chip${hid ? " hid" : ""}" draggable="true" data-leg="${esc(key)}" aria-pressed="${hid ? "false" : "true"}" title="${hid ? "Show" : "Hide"} ${esc(show)} · drag to reorder stack">
      <span class="leg-sw" style="background:${color}"></span>${esc(show)}
    </button>`;
  }).join("");
  const hasCustom = !!(loadLegendOrder(currentCostStack()) || []).length;
  el.innerHTML = chips + (chips
    ? `<button type="button" class="leg-chip leg-reset" data-leg-reset="1" title="Reset label order to default" ${hasCustom ? "" : "hidden"}>Reset</button>`
    : "");
  if (!el._bound) {
    el._bound = true;
    el.addEventListener("click", (ev) => {
      if (el._legDragMoved) {
        el._legDragMoved = false;
        return;
      }
      const resetBtn = ev.target.closest("[data-leg-reset]");
      if (resetBtn) {
        clearLegendOrder(currentCostStack());
        _redrawCostAfterLegend();
        return;
      }
      const btn = ev.target.closest(".leg-chip");
      if (!btn || btn.hasAttribute("data-leg-reset")) return;
      const name = btn.getAttribute("data-leg");
      if (!name) return;
      if (document.body.classList.contains("scope-period")) {
        const ag = window.__aggChart || {};
        if (!(ag.hiddenLegend instanceof Set)) ag.hiddenLegend = new Set();
        if (ag.hiddenLegend.has(name)) ag.hiddenLegend.delete(name);
        else ag.hiddenLegend.add(name);
        window.__aggChart = ag;
        _redrawCostAfterLegend();
        return;
      }
      const st = window.__costChart;
      if (!(st.hiddenLegend instanceof Set)) st.hiddenLegend = new Set();
      if (st.hiddenLegend.has(name)) st.hiddenLegend.delete(name);
      else st.hiddenLegend.add(name);
      _redrawCostAfterLegend();
    });
    el.addEventListener("dragstart", (ev) => {
      const btn = ev.target.closest(".leg-chip");
      if (!btn || btn.hasAttribute("data-leg-reset")) return;
      el._legDragKey = btn.getAttribute("data-leg");
      el._legDragMoved = false;
      btn.classList.add("leg-dragging");
      try {
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", el._legDragKey || "");
      } catch { /* ignore */ }
    });
    el.addEventListener("dragover", (ev) => {
      const over = ev.target.closest(".leg-chip:not([data-leg-reset])");
      if (!over || !el._legDragKey) return;
      ev.preventDefault();
      el.querySelectorAll(".leg-chip.leg-drag-over").forEach((n) => n.classList.remove("leg-drag-over"));
      over.classList.add("leg-drag-over");
      try { ev.dataTransfer.dropEffect = "move"; } catch { /* ignore */ }
    });
    el.addEventListener("dragleave", (ev) => {
      const over = ev.target.closest(".leg-chip");
      if (over) over.classList.remove("leg-drag-over");
    });
    el.addEventListener("drop", (ev) => {
      ev.preventDefault();
      const over = ev.target.closest(".leg-chip:not([data-leg-reset])");
      const fromKey = el._legDragKey;
      el.querySelectorAll(".leg-chip.leg-drag-over").forEach((n) => n.classList.remove("leg-drag-over"));
      if (!over || !fromKey) return;
      const toKey = over.getAttribute("data-leg");
      if (!toKey || toKey === fromKey) return;
      const keys = [...el.querySelectorAll(".leg-chip:not([data-leg-reset])")]
        .map((b) => b.getAttribute("data-leg")).filter(Boolean);
      const from = keys.indexOf(fromKey);
      const to = keys.indexOf(toKey);
      if (from < 0 || to < 0) return;
      keys.splice(from, 1);
      keys.splice(to, 0, fromKey);
      el._legDragMoved = true;
      saveLegendOrder(currentCostStack(), keys);
      _redrawCostAfterLegend();
    });
    el.addEventListener("dragend", () => {
      el._legDragKey = null;
      el.querySelectorAll(".leg-chip.leg-dragging, .leg-chip.leg-drag-over").forEach((n) => {
        n.classList.remove("leg-dragging", "leg-drag-over");
      });
    });
  }
}

export function setCostUnit(unit) {
  window.__costChart.unit = unit === "tokens" ? "tokens" : "usd";
  const unitKey = document.documentElement.dataset.source === "codex"
    ? "tt-cost-unit-codex"
    : "tt-cost-unit";
  try { localStorage.setItem(unitKey, window.__costChart.unit); } catch { /* ignore */ }
  const usdBtn = $("costUnitUsd");
  const tokBtn = $("costUnitTok");
  const isUsd = window.__costChart.unit === "usd";
  if (usdBtn) {
    usdBtn.classList.toggle("active", isUsd);
    usdBtn.setAttribute("aria-pressed", isUsd ? "true" : "false");
  }
  if (tokBtn) {
    tokBtn.classList.toggle("active", !isUsd);
    tokBtn.setAttribute("aria-pressed", !isUsd ? "true" : "false");
  }
  if (document.body.classList.contains("scope-period")) {
    redrawCostChart();
    return;
  }
  const st = window.__costChart;
  if (st) drawBars($("costChart"), st.turns, st.rounds);
}

export function drawAggBars(canvas, buckets, opts) {
  if (!canvas) return;
  if (!document.body.classList.contains("scope-period")) {
    clearCostPointerProps(canvas);
    hideChartTip($("costTip"));
    setGanttChrome(false);
    return;
  }
  setGanttChrome(false);
  clearRateHost("cost");
  setCostTipOwner(canvas, "period");
  const unit = (opts && opts.unit) || (window.__costChart && window.__costChart.unit) || "usd";
  const cumulative = !!(opts && opts.cumulative);
  // Normalized = 100% stacked composition; mutually exclusive with cumulative.
  const normalized = !!(opts && opts.normalized) && !cumulative;
  const byLabel = !!(opts && opts.byLabel);
  const stack = (opts && opts.stack) || "io";
  const prev = window.__aggChart || {};
  const hidden = prev.hiddenLegend instanceof Set ? prev.hiddenLegend : new Set();
  // By-label must use observation-window buckets only. Cumulative prefixes would
  // be re-summed across bars and massively overestimate every label.
  let src = (cumulative && !byLabel) ? _cumBuckets(buckets) : (buckets || []).slice();
  // By-label collapses to one segment per bar — skip 100% stack (all bars = 100%).
  const doNorm = normalized && !byLabel;
  const segsFromCats = (list) => (list || []).map((s) => ({
    k: s.k,
    label: s.label,
    legendKey: s.key,
    v: unit === "tokens" ? Number(s.tok) || 0 : Number(s.usd) || 0,
    tok: Number(s.tok) || 0,
    color: COST_COLORS[s.k] || (s.k === "toolreq" ? COST_COLORS.toolreq : COST_COLORS.tool),
  })).filter((s) => s.v > 0);
  const segsOfBucket = (b) => {
    if (b && b._oneSeg) return [b._oneSeg];
    if (stack === "parts" && Array.isArray(b.parts) && b.parts.length)
      return segsFromCats(b.parts);
    if (stack === "tools" && Array.isArray(b.tools) && b.tools.length)
      return segsFromCats(b.tools);
    const tok = unit === "tokens";
    return [
      { k: "in", label: "In", legendKey: "in", v: tok ? Number(b.tokens_in) || 0 : Number(b.cost_in_usd) || 0, color: COST_COLORS.in },
      { k: "cached", label: "Cached", legendKey: "cached", v: tok ? Number(b.tokens_cached) || 0 : Number(b.cost_cached_usd) || 0, color: COST_COLORS.cached },
      { k: "out", label: "Out", legendKey: "out", v: tok ? Number(b.tokens_out) || 0 : Number(b.cost_out_usd) || 0, color: COST_COLORS.out },
    ];
  };
  let periodLabelLegend = [];
  if (byLabel && src.length) {
    const accAll = new Map();
    const accVis = new Map();
    for (const b of src) {
      for (const s of segsOfBucket(b)) {
        if (!(s.v > 0)) continue;
        const key = costSegKey(s);
        const add = (map) => {
          const prevS = map.get(key);
          if (!prevS) map.set(key, { ...s });
          else prevS.v += s.v;
        };
        add(accAll);
        if (!isCostSegHidden(s, hidden)) add(accVis);
      }
    }
    periodLabelLegend = sortLegendItems([...accAll.entries()].map(([key, s]) => [key, {
      label: costDisplayLabel(s), color: s.color, k: s.k,
    }]), stack);
    src = [...accVis.values()].map((s) => ({
      label: costDisplayLabel(s),
      _oneSeg: s,
      official_usd: s.v,
    }));
    const customOrd = loadLegendOrder(stack);
    if (customOrd && customOrd.length) {
      src.sort((a, b) => {
        const ka = costSegKey(a._oneSeg), kb = costSegKey(b._oneSeg);
        const ia = customOrd.indexOf(ka), ib = customOrd.indexOf(kb);
        const ra = ia >= 0 ? ia : customOrd.length + defaultStackRank(ka, a._oneSeg, stack);
        const rb = ib >= 0 ? ib : customOrd.length + defaultStackRank(kb, b._oneSeg, stack);
        return ra - rb;
      });
    } else if (stack === "parts" || stack === "tools") {
      src.sort((a, b) => (Number(a._oneSeg && a._oneSeg.v) || 0) - (Number(b._oneSeg && b._oneSeg.v) || 0));
    }
  }
  window.__aggChart = {
    ...prev,
    buckets: buckets || [],
    unit,
    cumulative,
    normalized: doNorm,
    byLabel,
    stack,
    timeline: false,
    rate: false,
    ratePts: null,
    hiddenLegend: hidden,
    slotPx: prev.slotPx,
    _costUserZoom: prev._costUserZoom,
    _costStickEnd: prev._costStickEnd,
    _scrollLeft: prev._scrollLeft,
    _yViewZoom: prev._yViewZoom,
    _yViewPan: prev._yViewPan,
    _zoomViewKey: prev._zoomViewKey,
    _ganttGeom: null,
    _ganttRows: null,
  };

  const tip = $("costTip");
  applyXPadHeight(canvas, guessXPlan(
    src.map((b) => b.label || ""),
    Math.max(src.length, 1),
    !byLabel
  ).padB);

  const laid = layoutCostCanvas(canvas, Math.max(src.length, 1), {
    forceFit: !prev._costUserZoom,
  });
  const { w, h, ctx } = laid;
  const padL = PLOT_PAD_L;
  const padR = 12;
  const padT = 16;
  const groupW0 = Math.max(1, (w - padL - padR) / Math.max(src.length, 1));
  const xPlan = planXLabels(
    ctx,
    src.map((b) => b.label || ""),
    groupW0,
    { temporal: !byLabel }
  );
  const padB = xPlan.padB;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  let legendItems = periodLabelLegend.length
    ? periodLabelLegend
    : stack === "io"
    ? [
        ["in", { label: "In", color: COST_COLORS.in, k: "in" }],
        ["cached", { label: "Cached", color: COST_COLORS.cached, k: "cached" }],
        ["out", { label: "Out", color: COST_COLORS.out, k: "out" }],
      ]
    : null;
  if (!legendItems) {
    const live = new Map();
    for (const b of src) {
      for (const s of segsOfBucket(b)) {
        const key = costSegKey(s);
        if (key && !live.has(key))
          live.set(key, { label: costDisplayLabel(s), color: s.color, k: s.k });
      }
    }
    legendItems = [...live.entries()];
  }
  renderCostLegend(sortLegendItems(legendItems, stack), hidden);

  if (!src.length) {
    const yAxis = $("costYAxis");
    if (yAxis) yAxis.hidden = true;
    drawChartEmpty(ctx, w, h, "No usage in this period");
    hideChartTip(tip);
    canvas._aggHit = null;
    return;
  }

  const visSegs = (b) => orderCostSegs(segsOfBucket(b).filter((s) => {
    if (!(s.v > 0)) return false;
    return !hidden.has(s.legendKey) && !hidden.has(s.label) && !hidden.has(s.k);
  }), stack);

  let yMax = 0;
  let bars = src.map((b) => {
    const segs = visSegs(b);
    const total = segs.reduce((a, s) => a + s.v, 0);
    if (total > yMax) yMax = total;
    return { b, segs, total };
  });
  const axisUnit = doNorm ? "pct" : unit;
  if (doNorm) {
    bars = bars.map((bar) => {
      const t = bar.total;
      if (!(t > 0)) return { ...bar, segs: [], total: 0 };
      return {
        ...bar,
        segs: bar.segs.map((s) => ({ ...s, v: (s.v / t) * 100, _raw: s.v })),
        total: 100,
      };
    });
    yMax = 100;
  } else if (yMax <= 0) {
    yMax = unit === "tokens" ? 1000 : 0.01;
  }
  if (!doNorm)
    yMax = yMax / clampYViewZoom((window.__aggChart && window.__aggChart._yViewZoom) || 1);
  const y = niceCostYMaxForUnit(yMax, axisUnit);
  const max = y.max || 1;

  ctx.strokeStyle = CHART_AXIS.grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = CHART_AXIS.labelDim;
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  eachCostYTick(0, max, y.step, axisUnit, (v) => {
    const yy = padT + plotH - (v / max) * plotH;
    ctx.beginPath();
    ctx.moveTo(0, yy);
    ctx.lineTo(w - padR, yy);
    ctx.stroke();
  });
  drawCostYOverlay({ min: 0, max, step: y.step, unit: axisUnit, top: padT, bottom: padB, h });

  const n = bars.length;
  const nSlots = Math.max(n, 1);
  const groupW = plotW / nSlots;
  const gap = Math.max(0, Math.min(6, groupW * 0.18));
  const bw = Math.max(1, groupW - gap);
  const hit = [];

  bars.forEach((bar, i) => {
    const x = padL + i * groupW + (groupW - bw) / 2;
    let y0 = padT + plotH;
    bar.segs.forEach((s) => {
      if (!(s.v > 0)) return;
      const bh = (s.v / max) * plotH;
      y0 -= bh;
      ctx.fillStyle = s.color;
      ctx.fillRect(x, y0, bw, Math.max(1, bh));
    });
    hit.push({ x, y: padT, w: bw, h: plotH, bar });
    if (i % xPlan.every === 0)
      drawXLabel(ctx, bar.b.label || "", x + bw / 2, h - padB + 6, xPlan.rotate);
  });

  canvas._aggHit = { hit, w };
  if (!canvas._aggTipBound) {
    canvas._aggTipBound = true;
    canvas.addEventListener("mousemove", (ev) => {
      canvas._ptr = { clientX: ev.clientX, clientY: ev.clientY };
      if (canvas._costTipOwner !== "period") return;
      const pack = canvas._aggHit;
      const tipEl = $("costTip");
      if (!pack || !tipEl) return;
      const r = canvas.getBoundingClientRect();
      const mx = ev.clientX - r.left;
      const my = ev.clientY - r.top;
      const found = pack.hit.find((h0) => mx >= h0.x && mx <= h0.x + h0.w && my >= h0.y && my <= h0.y + h0.h);
      if (!found) {
        hideChartTip(tipEl);
        return;
      }
      const b = found.bar.b;
      const ag = window.__aggChart || {};
      const unit = ag.unit || "usd";
      const stackMode = ag.stack || "io";
      const isNorm = !!ag.normalized;
      const head = b.session_label
        ? periodSessionTipHead(b)
        : (b.label || "");
      let html;
      const estUsd = Number(b.estimate_usd) > 0
        ? Number(b.estimate_usd)
        : ((Number(b.cost_in_usd) || 0)
          + (Number(b.cost_cached_usd) || 0)
          + (Number(b.cost_out_usd) || 0));
      const tokTot = Number(b.tokens_all) > 0
        ? Number(b.tokens_all)
        : ((Number(b.tokens_in) || 0)
          + (Number(b.tokens_cached) || 0)
          + (Number(b.tokens_out) || 0));
      const tipTotal = isNorm
        ? found.bar.total
        : (found.bar.total > 0
          ? found.bar.total
          : (unit === "tokens" ? tokTot : estUsd));
      if (b._oneSeg) {
        html = `<b>${esc(head)}</b><br>${fmtCostAxis(tipTotal, unit)}`;
      } else if (stackMode === "io" && !isNorm) {
        const lines = [
          `<b>${esc(head)}</b>`,
          `<span class="tok-in">In</span> ${fmtTokens(b.tokens_in)} · ${fmtUsd(b.cost_in_usd)}`,
          `<span class="tok-cached">Cached</span> ${fmtTokens(b.tokens_cached)} · ${fmtUsd(b.cost_cached_usd)}`,
          `<span class="tok-out">Out</span> ${fmtTokens(b.tokens_out)} · ${fmtUsd(b.cost_out_usd)}`,
          `<b>→ ${fmtCostAxis(tipTotal, unit)}</b>`,
        ];
        if (unit === "usd" && officialAmount(b.official_usd) != null)
          lines.push(`<span class="muted">official ${fmtUsd(b.official_usd)}</span>`);
        html = lines.join("<br>");
      } else {
        const lines = [`<b>${esc(head)}</b>`];
        tipOrderedSegs(found.bar.segs || [], stackMode, unit).forEach((s) => {
          lines.push(formatTipSegLine(s, unit, { normalized: isNorm }));
        });
        if (!isNorm)
          lines.push(`<b>→ ${fmtCostAxis(tipTotal, unit)}</b>`);
        if (!isNorm && unit === "usd" && officialAmount(b.official_usd) != null)
          lines.push(`<span class="muted">official ${fmtUsd(b.official_usd)}</span>`);
        html = lines.join("<br>");
      }
      placeCostTip(ev, tipEl, html);
    });
    canvas.addEventListener("mouseleave", () => {
      canvas._ptr = null;
      if (canvas._costTipOwner !== "period") return;
      hideChartTip($("costTip"));
    });
  }
  if (canvas._ptr) {
    canvas.dispatchEvent(new MouseEvent("mousemove", {
      clientX: canvas._ptr.clientX,
      clientY: canvas._ptr.clientY,
      bubbles: false,
    }));
  }
}
