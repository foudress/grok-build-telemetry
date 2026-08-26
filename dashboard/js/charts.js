/** Context line chart + cost bars / legend */
import {
  $,
  fmtTokens,
  fmtUsd,
  fmtUsdPerM,
  fmtToksPerSec,
  fmtMs,
  esc,
  AR,
  partIn,
  partCached,
  partOut,
  joinParts,
  totalPrice,
  isSubagentKind,
} from './fmt.js';
import { focusRound, revealRound } from './tree.js';

/* Canvas axis / empty-state palette — slightly higher contrast than raw muted */
const CHART_AXIS = {
  grid: "#243040",
  label: "#b6c2d1",
  labelDim: "#8b9cb3",
  empty: "#6b7c92",
  placeholder: "#4a5568",
};

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
function placeCostTip(ev, tipEl, html) {
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
function hideChartTip(el) {
  if (!el) return;
  el.classList.remove("is-visible");
  el.innerHTML = "";
}

function hideAllChartTips() {
  hideChartTip($("costTip"));
  hideChartTip($("ctxTip"));
}

/** Drop property-style hover handlers (period used these; they stack with addEventListener). */
function clearCostPointerProps(canvas) {
  if (!canvas) return;
  canvas.onmousemove = null;
  canvas.onmouseleave = null;
  canvas.onclick = null;
  canvas._aggHit = null;
}

function setCostTipOwner(canvas, owner) {
  if (!canvas) return;
  if (canvas._costTipOwner !== owner) {
    hideChartTip($("costTip"));
    canvas._costTipOwner = owner;
  }
}

/** Calm centered empty-state message inside a chart canvas. */
function drawChartEmpty(ctx, w, h, message) {
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
function buildCtxPoints(rounds) {
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
      // Calls Y = Cached. Last call / reminder-only LLM (no harness) is 0 —
      // R{n} end already marks the round; plotting Call@0 is noise.
      if (!(m.tokCache > 0)) return;
      pts.push({
        label: String(p.li),
        v: m.tokCache,
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

/** Cost Y ($): ~5–8 nice ticks (1-2-5). Old 0.5 steps explode on period totals. */
function niceCostYMax(rawMax) {
  const m = Math.max(Number(rawMax) || 0, 0.01);
  const targetTicks = 6;
  const rough = m / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(rough, 1e-6))));
  const r = rough / mag;
  let nice = 1;
  if (r <= 1) nice = 1;
  else if (r <= 2) nice = 2;
  else if (r <= 2.5) nice = 2.5;
  else if (r <= 5) nice = 5;
  else nice = 10;
  let step = nice * mag;
  if (step < 0.005) step = 0.005;
  let max = Math.max(0.01, Math.ceil(m / step) * step);
  let ticks = Math.round(max / step);
  if (ticks > 8) {
    step = (Math.ceil(max / 6 / step) || 1) * step;
    max = Math.max(step, Math.ceil(m / step) * step);
  }
  return { min: 0, max: max || step, step };
}

function drawLineChart(canvas, series, color, rounds) {
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
  const measure = document.createElement("canvas").getContext("2d");
  const xPlan = planXLabels(
    measure,
    (pts || []).map((p) => p.label || ""),
    avgPxGuess,
    { temporal: false }
  );
  const bottom = Math.max(28, xPlan.padB || 28);
  canvas.style.height = (baseH + Math.max(0, bottom - 28)) + "px";

  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || wGuess;
  const h = canvas.clientHeight || baseH;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

  const TIER_CLIFF = 200000;
  if (TIER_CLIFF > min && TIER_CLIFF < max) {
    const yCliff = yOf(TIER_CLIFF);
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
    ctx.fillText("200k", left + 4, yCliff - 4);
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
            ? `<span class="muted">tool defs + message ${fmtTokens(Number(best.message_residual_tokens ?? best.tool_definitions_tokens))}</span>`
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

// Cost chart state: overview | detail attribution | drill into one turn's LLM calls
window.__costChart = window.__costChart || {
  stack: "io", // io | parts | tools
  drillTurn: null, // turn index when drilled
  rounds: [],
  turns: [],
  /** "usd" | "tokens" — Cost per round Y-axis */
  unit: "usd",
  byLabel: false,
  /** Legend labels (as shown) currently hidden from the bar stack */
  hiddenLegend: new Set(),
  /** previous drill state — prune hide list when entering drill */
  _wasDrill: false,
};

/** Normalize tool / tool-request names so "grep", "grep x2", "grep×2" share one category. */
function normToolCatName(raw) {
  let s = String(raw == null ? "tool" : raw).trim();
  if (!s) s = "tool";
  // "grep x2" / "grep ×2" / "grep x 12"
  s = s.replace(/\s*[x×]\s*\d+\s*$/i, "").trim();
  // "grep×2" (no space) leftover
  s = s.replace(/[x×]\d+\s*$/i, "").trim();
  return s || "tool";
}

function costSegKey(seg) {
  if (!seg) return "";
  const k = String(seg.k || "");
  // Stable keys (by-label folds Compact N / LLM Answer RN → one chip).
  if (k === "in" || k === "cached" || k === "out" || k === "system"
      || k === "recap" || k === "compact" || k === "official"
      || k === "cache_miss" || k === "prompt" || k === "llm_answer"
      || k === "compact_out" || k === "user")
    return k;
  if (k === "sys_prompt" || k === "user_info" || k === "reminders" || k === "tool_def")
    return k;
  if (seg.legendKey) {
    const lk = String(seg.legendKey);
    const low = lk.toLowerCase();
    if (low === "in" || low === "cached" || low === "out" || low === "system"
        || low === "prompt" || low === "llm answer" || low === "llm_answer"
        || low === "compact out" || low === "compact_out")
      return low === "llm answer" ? "llm_answer"
        : low === "compact out" ? "compact_out"
        : low;
    if (low.startsWith("llm answer")) return "llm_answer";
    if (low.startsWith("compact ") && low.endsWith(" out")) return "compact_out";
    if (low === "compact out") return "compact_out";
    return lk;
  }
  if (k === "tool" || k === "toolreq")
    return k + ":" + normToolCatName(seg.label);
  const lab = String(seg.label || "");
  const low = lab.toLowerCase();
  if (low === "in" || low === "cached" || low === "out" || low === "system")
    return low;
  if (low.startsWith("llm answer")) return "llm_answer";
  if (low === "compact out" || (low.startsWith("compact ") && low.endsWith(" out")))
    return "compact_out";
  if (low === "prompt") return "prompt";
  return lab || k || "";
}

/** I/O: first letter capital. Other stacks: all lowercase. */
function costDisplayLabel(seg) {
  if (!seg) return "";
  const io = currentCostStack() === "io";
  const k = String(seg.k || "");
  let raw;
  if (k === "llm_out_in" || seg.legendKey === "llm_out_in") raw = "llm out→in";
  else if (k === "in") raw = "in";
  else if (k === "cached") raw = "cached";
  else if (k === "out") raw = "out";
  else if (k === "cache_miss") raw = "cache miss";
  else if (k === "prompt") raw = "prompt";
  else if (k === "llm_answer") raw = "llm answer";
  else if (k === "compact_out") raw = "compact out";
  else if (k === "sys_prompt") raw = "system prompt";
  else if (k === "user_info") raw = "user info";
  else if (k === "reminders") raw = "reminders & skills";
  else if (k === "tool_def") raw = "tool def";
  else if (k === "sub") raw = String(seg.label || "sub agent");
  else raw = String(seg.label || k || "");
  if (raw === "LLM Out→In" || raw === "LLM Out->In") raw = "llm out→in";
  raw = String(raw).toLowerCase();
  if (!io) return raw;
  return raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : raw;
}

function isCostSegHidden(seg, hidden) {
  if (!hidden || !hidden.size || !seg) return false;
  const key = costSegKey(seg);
  if (key && hidden.has(key)) return true;
  if (seg.label && hidden.has(seg.label)) return true;
  if (seg.k && hidden.has(seg.k)) return true;
  const aliases = {
    in: ["In", "in"],
    cached: ["Cached", "cached"],
    out: ["Out", "out"],
    system: ["System", "system"],
  };
  for (const a of (aliases[key] || [])) {
    if (hidden.has(a)) return true;
  }
  return false;
}

const COST_COLORS = {
  in: "#3ecf8e",
  cached: "#f0b429",
  out: "#f07178",
  system: "#8ab4f8",
  sys_prompt: "#8ab4f8",
  user_info: "#7aa2f7",
  reminders: "#9d7cd8",
  tool_def: "#6d8fd6",
  user: "#5ccfe6",       /* same blue as message */
  prompt: "#5ccfe6",
  llm_answer: "#7dcfff",
  compact_out: "#e0b060",
  harness: "#3ecf8e",     /* same green as In / tool results */
  residual: "#8fd3a8",
  cache_miss: "#d97757",  /* leftover uncached KV reread — not user, not residual */
  thought: "#c4a5e8",
  reasoning: "#a78bfa",
  toolreq: "#e88a90",    /* light red — tool request */
  message: "#5ccfe6",
  tool: "#3ecf8e",
  hook: "#e8a0bf",
  late: "#b48ead",
  llm: "#1a6b3c",        /* dark green — LLM Out→In */
  compact: "#f0a070",
  recap: "#e0b060",
  official: "#3d9cf0",
  sub: "#8b7cf7",
};
const SUB_SHADES = ["#c4b5fd", "#a78bfa", "#8b7cf7", "#6d5ae6", "#5b21b6", "#4c1d95"];

function subShade(n) {
  const i = Math.max(1, Number(n) || 1);
  return SUB_SHADES[(i - 1) % SUB_SHADES.length];
}

function costSegMetric(seg, unit) {
  if (!seg) return 0;
  if (unit === "tokens") {
    const t = Number(seg.tok);
    if (t > 0) return t;
    // fallback: no tok stamp
    return 0;
  }
  return Number(seg.v) || 0;
}

function fmtCostAxis(v, unit) {
  if (unit === "pct") {
    const n = Number(v) || 0;
    if (Math.abs(n - Math.round(n)) < 1e-6) return `${Math.round(n)}%`;
    return `${n.toFixed(1)}%`;
  }
  if (unit === "tokens") return fmtTokens(v);
  const n = Number(v) || 0;
  if (n === 0) return "$0";
  const a = Math.abs(n);
  if (a >= 100) return "$" + Math.round(n);
  if (a >= 10) return "$" + n.toFixed(1);
  if (a >= 1) return "$" + n.toFixed(2);
  if (a >= 0.1) return "$" + n.toFixed(2);
  return "$" + n.toFixed(3);
}

/** Token Y-axis: ~5–8 ticks (same Round overview + Drill — no dense 500-step grids). */
function niceCostYMaxForUnit(m, unit) {
  if (unit === "pct") {
    return { min: 0, max: 100, step: 20 };
  }
  if (unit === "tokens") {
    m = Math.max(0, Number(m) || 0);
    if (m <= 0) return { min: 0, max: 1000, step: 200 };
    const targetTicks = 6;
    const rough = m / targetTicks;
    const mag = Math.pow(10, Math.floor(Math.log10(Math.max(rough, 1))));
    const r = rough / mag;
    let nice = 1;
    if (r <= 1) nice = 1;
    else if (r <= 2) nice = 2;
    else if (r <= 2.5) nice = 2.5;
    else if (r <= 5) nice = 5;
    else nice = 10;
    let step = nice * mag;
    // Prefer whole-token steps (no fractional token ticks)
    if (step < 1) step = 1;
    else if (step >= 10 && step !== Math.floor(step))
      step = Math.ceil(step);
    // Snap common large steps to round k/M for readability
    if (step >= 1000) {
      const k = Math.round(step / 1000) * 1000;
      if (k > 0) step = k;
    }
    const max = Math.max(step, Math.ceil(m / step) * step);
    // Cap tick count if still dense (e.g. odd max)
    let ticks = Math.round(max / step);
    if (ticks > 10) {
      step = Math.ceil(max / 8 / step) * step || step;
      // re-ceil max
      const max2 = Math.max(step, Math.ceil(m / step) * step);
      return { min: 0, max: max2, step };
    }
    return { min: 0, max, step };
  }
  return niceCostYMax(m);
}

function findRound(rounds, turnIndex) {
  return (rounds || []).find(r => Number(r.index) === Number(turnIndex)) || null;
}

function turnCostParts(t, round, { detail, peelSystem } = {}) {
  // peelSystem=true when System is drawn as its own chart bar (R1 only)
  const bd = (round && round.breakdown) || t.estimate_breakdown || t.cost_usd || {};
  const sp = round && round.system_prompt;
  const up = round && round.user_prompt;
  let sys = Number(bd.system_in_usd) || Number(sp && sp.cost_in_usd) || 0;
  let user = Number(bd.user_in_usd) || Number(up && up.cost_in_usd) || 0;
  const sysTok = Number(bd.system_in_tokens) || Number(sp && (sp.tokens_in ?? sp.logical_tokens)) || 0;
  const userTok = Number(bd.user_in_tokens) || Number(up && (up.tokens_in ?? up.uncached_est)) || 0;

  // Prefer tree In (R1: user+harness ~4.5k; R2+: off_unc). Never use full
  // uncached_in on R1 — that double-counts System bootstrap.
  // Prefer round fields (hierarchy) over turn snapshot (may be stale API bill).
  let cin = bd.tree_in_usd
    ?? round?.cost_tree_in_usd
    ?? t.cost_tree_in_usd
    ?? t.cost_in_usd
    ?? bd.uncached_in_usd
    ?? bd.uncached_input
    ?? bd.input;
  let ccache = bd.cached_usd
    ?? round?.cost_cached_usd
    ?? t.cost_cached_usd
    ?? bd.cached_input;
  let cout = bd.output_usd
    ?? round?.cost_out_usd
    ?? t.cost_out_usd
    ?? bd.output
    ?? bd.output_incl_reasoning;
  cin = Number(cin) || 0;
  ccache = Number(ccache) || 0;
  cout = Number(cout) || 0;

  // Stack totals = raw hierarchy In/Cached/Out. Never scale modes to match each other.
  const partsSum = cin + ccache + cout;
  let tot = partsSum;
  if (peelSystem || bd.round_total_peeled_system) {
    if (round && round.estimate_usd != null && bd.round_total_peeled_system)
      tot = Number(round.estimate_usd) || partsSum;
    else if (!(partsSum > 0))
      tot = Math.max(0, Number(t.estimate_usd || bd.total_usd || 0) - sys);
  } else if (!(partsSum > 0)) {
    tot = Number(
      (round && round.estimate_usd != null ? round.estimate_usd : null)
      ?? t.estimate_usd
      ?? bd.total_usd
      ?? bd.total
      ?? 0
    ) || 0;
  }
  const sum = partsSum > 0 ? partsSum : tot;

  // Token counts for Tok unit mode
  const tinTok = Number(bd.tree_in_tokens)
    ?? Number(round?.tree_in_tokens)
    ?? Number(t.uncached_input_tokens)
    ?? 0;
  const tcacheTok = Number(bd.cached_tokens)
    ?? Number(t.cached_read_tokens)
    ?? 0;
  const toutTok = Number(bd.output_tokens)
    ?? Number(t.output_tokens)
    ?? 0;
  const userTokN = userTok || 0;
  const harnessTokN = Number(bd.harness_in_tokens) || 0;
  const missTokN = Number(bd.cache_miss_in_tokens)
    || Number(round && round.cache_miss_in_tokens) || 0;
  let thoughtTokN = Number(bd.llm_thought_summary_tokens) || 0;
  let reasonTokN = Number(bd.llm_reasoning_tokens ?? bd.llm_reasoning_encrypted_tokens) || 0;
  const emitTokN = Number(bd.llm_out_to_harness_tokens) || 0;
  const msgTokN = Number(bd.llm_out_to_user_tokens) || 0;

  const segs = [];

  if (detail) {
    let harness = Number(bd.harness_in_usd) || 0;
    let harnessTok = harnessTokN;
    // Compact Out → User in Parts. Between-rounds is already off harness_in
    // (owned by user.compact_out); only mid-round compact still sits in harness.
    let coUsd = 0;
    let coTok = 0;
    const upCo = (round && round.user_prompt && round.user_prompt.compact_out) || {};
    const coUserUsd = Number(upCo.cost_in_usd) || 0;
    const coUserTok = Number(upCo.tokens_in) || 0;
    let coMidUsd = 0;
    let coMidTok = 0;
    for (const step of (round && round.model_steps) || []) {
      for (const ch of step.children || []) {
        if (!ch || ch.kind !== "phase_harness") continue;
        for (const sub of ch.children || []) {
          if (!sub || sub.kind !== "compact_out_in") continue;
          if (String(sub.attribution || "") === "user") continue;
          coMidUsd += Number(sub.cost_in_usd) || 0;
          coMidTok += Number(sub.tokens_in || sub.context_delta) || 0;
        }
      }
    }
    coUsd = coUserUsd + coMidUsd;
    coTok = coUserTok + coMidTok;
    if (coMidUsd > 0 || coMidTok > 0) {
      harness = Math.max(0, harness - coMidUsd);
      harnessTok = Math.max(0, harnessTok - coMidTok);
    }
    const missUsd = Number(bd.cache_miss_in_usd)
      || Number(round && round.cache_miss_in_usd) || 0;
    let thought = Number(bd.llm_thought_summary_usd) || 0;
    if (!(thought > 0 || thoughtTokN > 0)) {
      const fromSteps = _sumThoughtFromSteps(round);
      thought = fromSteps.usd;
      thoughtTokN = fromSteps.tok;
    }
    const reasoningEnc = Number(bd.llm_reasoning_usd ?? bd.llm_reasoning_encrypted_usd) || 0;
    const emit = Number(bd.llm_out_to_harness_usd) || 0;
    const msg = Number(bd.llm_out_to_user_usd) || 0;
    // System is a separate chart bar when peelSystem — omit from R1 stack
    if (!peelSystem && sys > 0) {
      segs.push({
        k: "system", label: "system", v: sys, tok: sysTok || 0,
        color: COST_COLORS.system, tokens: sysTok || null,
      });
    }
    // Parts = tree aggregation only (same User fold as Tools: prompt + answer + compact).
    const uFold = userToolsSegs(up, round && round.index);
    let userPartsUsd = uFold.reduce((a, s) => a + (Number(s.v) || 0), 0);
    let userPartsTok = uFold.reduce((a, s) => a + (Number(s.tok) || 0), 0);
    if (!(userPartsUsd > 0 || userPartsTok > 0)) {
      userPartsUsd = user + coUserUsd;
      userPartsTok = userTokN + coUserTok;
    }
    userPartsUsd += coMidUsd;
    userPartsTok += coMidTok;
    let inParts = [
      { k: "user", label: "user", v: userPartsUsd, tok: userPartsTok, color: COST_COLORS.user },
      { k: "harness", label: "harness", v: harness, tok: harnessTok, color: COST_COLORS.harness },
      { k: "cache_miss", label: "cache miss", legendKey: "cache_miss",
        v: missUsd, tok: missTokN, color: COST_COLORS.cache_miss },
    ].filter((p) => p.v > 0 || p.tok > 0);
    if (!inParts.length && (cin > 0 || tinTok > 0)) {
      inParts = [{ k: "in", label: "in", v: cin, tok: tinTok, color: COST_COLORS.in }];
    }
    let outParts = [
      { k: "thought", label: "thought", v: thought, tok: thoughtTokN, color: COST_COLORS.thought },
      { k: "reasoning", label: "reasoning", v: reasoningEnc, tok: reasonTokN, color: COST_COLORS.reasoning },
      { k: "toolreq", label: "tool req", v: emit, tok: emitTokN, color: COST_COLORS.toolreq },
      { k: "message", label: "message", v: msg, tok: msgTokN, color: COST_COLORS.message },
    ].filter((p) => p.v > 0 || p.tok > 0);
    if (!outParts.length && (cout > 0 || toutTok > 0)) {
      outParts = [{ k: "out", label: "out", v: cout, tok: toutTok, color: COST_COLORS.out }];
    }
    segs.push(...inParts);
    if (ccache > 0 || tcacheTok > 0)
      segs.push({ k: "cached", label: "cached", v: ccache, tok: tcacheTok, color: COST_COLORS.cached });
    segs.push(...outParts);
  } else {
    // Simple stack: In / Cached / Out only (User folded into global In).
    // System only inside this bar when NOT a separate chart bar.
    if (!peelSystem && sys > 0) {
      segs.push({ k: "system", label: "system", v: sys, tok: sysTok || 0, color: COST_COLORS.system });
    }
    if (cin > 1e-9 || tinTok > 0)
      segs.push({ k: "in", label: "in", v: cin, tok: tinTok, color: COST_COLORS.in });
    if (ccache > 0 || tcacheTok > 0)
      segs.push({ k: "cached", label: "cached", v: ccache, tok: tcacheTok, color: COST_COLORS.cached });
    if (cout > 0 || toutTok > 0)
      segs.push({ k: "out", label: "out", v: cout, tok: toutTok, color: COST_COLORS.out });
  }

  const stackTotal = segs.reduce((a, s) => a + s.v, 0);
  const stackTok = segs.reduce((a, s) => a + (Number(s.tok) || 0), 0);
  return {
    segs,
    in: cin,
    cached: ccache,
    out: cout,
    total: stackTotal || sum || tot || 0,
    total_tok: stackTok || tinTok + tcacheTok + toutTok,
    official: peelSystem ? null : t.official_usd,
    index: t.index,
    uncached_tokens: t.uncached_input_tokens ?? tinTok,
    cached_tokens: t.cached_read_tokens ?? tcacheTok,
    out_tokens: t.output_tokens ?? toutTok,
    label: String(t.index),
    kind: "turn",
  };
}

function mergeCostSegs(a, b) {
  const map = new Map();
  for (const s of [...(a || []), ...(b || [])]) {
    const key = costSegKey(s);
    if (!key) continue;
    const prev = map.get(key);
    if (!prev) {
      map.set(key, { ...s });
      continue;
    }
    prev.v = (Number(prev.v) || 0) + (Number(s.v) || 0);
    prev.tok = (Number(prev.tok) || 0) + (Number(s.tok) || 0);
  }
  return [...map.values()];
}

/** Drill-level cats folded onto one round bar (tools, LLM Out→In, …). */
function turnCostPartsTools(t, round, { peelSystem } = {}) {
  const base = turnCostParts(t, round, { detail: false, peelSystem });
  const steps = (round && round.model_steps) || [];
  let segs = [];
  steps.forEach((s, i) => {
    const b = callCostParts(s, s.index ?? i + 1, round);
    // Drop per-call cached — Σ call prefixes ≠ official round when cache miss.
    const withoutCache = (b.segs || []).filter((x) => x.k !== "cached");
    segs = mergeCostSegs(segs, withoutCache);
  });
  if (!segs.length)
    return turnCostParts(t, round, { detail: true, peelSystem });
  // Round-level KV miss + official Cached (same as Parts) — not Σ calls.
  const bd = (round && round.breakdown) || {};
  const missUsd = Number(bd.cache_miss_in_usd)
    || Number(round && round.cache_miss_in_usd) || 0;
  const missTok = Number(bd.cache_miss_in_tokens)
    || Number(round && round.cache_miss_in_tokens) || 0;
  if (missUsd > 0 || missTok > 0) {
    segs = mergeCostSegs(segs, [{
      k: "cache_miss",
      label: "cache miss",
      legendKey: "cache_miss",
      v: missUsd,
      tok: missTok,
      color: COST_COLORS.cache_miss,
    }]);
  }
  const cacheUsd = Number(bd.cached_usd)
    || Number(round && round.cost_cached_usd)
    || Number(base.cached) || 0;
  const cacheTok = Number(bd.cached_tokens)
    || Number(round && round.cached_read_tokens)
    || Number(base.cached_tokens) || 0;
  if (cacheUsd > 0 || cacheTok > 0) {
    segs = mergeCostSegs(segs, [{
      k: "cached",
      label: "cached",
      legendKey: "cached",
      v: cacheUsd,
      tok: cacheTok,
      color: COST_COLORS.cached,
    }]);
  }
  return { ...base, segs };
}

/** User Tools cats: prompt / LLM Answer RN-1 / Compact [N] Out. */
function userToolsSegs(up, roundIndex) {
  if (!up) return [];
  const out = [];
  const prev = up.prev_llm_answer || {};
  const ansT = Number(prev.tokens_in) || 0;
  const ansU = Number(prev.cost_in_usd) || 0;
  let promptT = Number(
    up.prompt_tokens_in != null ? up.prompt_tokens_in : (up.tokens_in ?? up.uncached_est)
  ) || 0;
  let promptU = Number(
    up.prompt_cost_in_usd != null ? up.prompt_cost_in_usd : up.cost_in_usd
  ) || 0;
  const co = up.compact_out || {};
  const coT = Number(co.tokens_in) || 0;
  const coU = Number(co.cost_in_usd) || 0;
  if (promptT > 0 || promptU > 0) {
    out.push({
      k: "prompt",
      label: "prompt",
      legendKey: "prompt",
      v: promptU || 0,
      tok: promptT || 0,
      color: COST_COLORS.prompt,
    });
  }
  if (ansT > 0 || ansU > 0) {
    const rn = Number(prev.round_index) || Math.max(0, Number(roundIndex || 1) - 1);
    out.push({
      k: "llm_answer",
      label: rn > 0 ? ("LLM Answer R" + rn) : "LLM Answer",
      legendKey: "llm_answer",
      v: ansU || 0,
      tok: ansT || 0,
      color: COST_COLORS.llm_answer,
    });
  }
  if (coT > 0 || coU > 0) {
    const cn = co.compact_index;
    out.push({
      k: "compact_out",
      label: (cn != null && Number(cn) > 0) ? ("Compact " + Number(cn) + " Out") : "Compact Out",
      legendKey: "compact_out",
      v: coU || 0,
      tok: coT || 0,
      color: COST_COLORS.compact_out,
    });
  }
  if (!out.length) {
    const userU = Number(up.cost_in_usd) || 0;
    const userT = Number(up.tokens_in ?? up.uncached_est) || 0;
    if (userU > 0 || userT > 0) {
      out.push({
        k: "user",
        label: "user",
        legendKey: "user",
        v: userU || 0,
        tok: userT || 0,
        color: COST_COLORS.user,
      });
    }
  }
  return out;
}

/** System Tools cats from bootstrap parts. */
function systemToolsSegs(sp, sysUsd, sysTok) {
  const raw = (sp && sp.parts) || [];
  const mapKind = (kind, label) => {
    const k = String(kind || "");
    if (k === "system") return { k: "sys_prompt", label: "System Prompt", color: COST_COLORS.sys_prompt };
    if (k === "user_info") return { k: "user_info", label: "User info", color: COST_COLORS.user_info };
    if (k === "reminders") return { k: "reminders", label: "Reminders & Skills", color: COST_COLORS.reminders };
    if (k === "tool_definitions" || k === "tool_defs_message")
      return { k: "tool_def", label: "Tool Def", color: COST_COLORS.tool_def };
    if (k === "mcp") return { k: "tool_def", label: "Tool Def", color: COST_COLORS.tool_def };
    return { k: "sys_prompt", label: label || k || "System Prompt", color: COST_COLORS.sys_prompt };
  };
  const segs = [];
  let sumT = 0;
  let sumU = 0;
  for (const p of raw) {
    if (!p || p.kind === "hooks") continue;
    const tok = Number(p.tokens ?? p.tokens_in) || 0;
    const usd = Number(p.cost_in_usd);
    const m = mapKind(p.kind, p.label);
    if (!(tok > 0 || (Number.isFinite(usd) && usd > 0))) continue;
    sumT += tok;
    const v = Number.isFinite(usd) ? usd : 0;
    sumU += v;
    segs.push({
      k: m.k,
      label: m.label,
      legendKey: m.k,
      v,
      tok,
      color: m.color,
    });
  }
  // Only fill missing $ from tokens (tree parts often have tokens, no cost_in_usd).
  // Never rescale priced parts to force Σ = System card total.
  if (segs.length && sysUsd > 0 && !(sumU > 0) && sumT > 0) {
    for (const s of segs) s.v = sysUsd * ((Number(s.tok) || 0) / sumT);
  }
  if (!segs.length && (sysUsd > 0 || sysTok > 0)) {
    segs.push({
      k: "system",
      label: "system",
      legendKey: "system",
      v: sysUsd || 0,
      tok: sysTok || 0,
      color: COST_COLORS.system,
    });
  }
  return segs.filter((s) => s.v > 0 || s.tok > 0);
}

function systemCostBar(round, { stack } = {}) {
  const sp = round && round.system_prompt;
  const bd = (round && round.breakdown) || {};
  const sys = Number(bd.system_in_usd) || Number(sp && sp.cost_in_usd) || 0;
  const sysTok = Number(bd.system_in_tokens) || Number(sp && (sp.tokens_in ?? sp.logical_tokens)) || 0;
  if (sys <= 0 && sysTok <= 0) return null;
  const st = (stack || (window.__costChart && window.__costChart.stack) || "io");
  let segs;
  if (st === "io") {
    // Rounds I/O: Sys bar = green In only.
    segs = [{
      k: "in",
      label: "In",
      legendKey: "in",
      v: sys || 0,
      tok: sysTok || 0,
      color: COST_COLORS.in,
      tokens: sysTok || null,
    }];
  } else if (st === "tools") {
    segs = systemToolsSegs(sp, sys, sysTok);
  } else {
    // Parts: single system (blue).
    segs = [{
      k: "system",
      label: "system",
      legendKey: "system",
      v: sys || 0,
      tok: sysTok || 0,
      color: COST_COLORS.system,
      tokens: sysTok || null,
    }];
  }
  return {
    segs,
    in: sys,
    cached: 0,
    out: 0,
    total: sys || 0,
    total_tok: sysTok || 0,
    official: null,
    index: "sys",
    label: "Sys",
    kind: "system",
    uncached_tokens: sysTok,
    cached_tokens: 0,
    out_tokens: 0,
  };
}

/** Separate User bar for Cost drill (like Sys on overview). */
function userCostBar(round, { stack } = {}) {
  const up = round && round.user_prompt;
  if (!up || up.kind !== "user_prompt") return null;
  const bd = (round && round.breakdown) || {};
  const st = (stack || (window.__costChart && window.__costChart.stack) || "io");
  const isR1 = Number(round && round.index) === 1;

  const co = up.compact_out || {};
  const coTok = Number(co.tokens_in) || 0;
  const coUsd = Number(co.cost_in_usd) || 0;
  let inTok = Number(up.display_in_tokens)
    || Number(bd.user_in_tokens)
    || Number(up.tokens_in ?? up.uncached_est)
    || 0;
  let inUsd = Number(up.display_in_usd)
    || Number(bd.user_in_usd)
    || Number(up.cost_in_usd)
    || 0;
  if (!(inTok > 0) && coTok > 0) inTok = coTok;
  if (!(inUsd > 0) && coUsd > 0) inUsd = coUsd;

  // Keep User Cached even on round KV miss (Σ User+calls may exceed round Cached).
  // R1 User never has Cached — leave at 0.
  let cacheTok = isR1
    ? 0
    : (Number(up.tokens_cached ?? up.cached_est ?? bd.user_cached_tokens) || 0);
  let cacheUsd = isR1
    ? 0
    : (Number(up.cost_cached_usd ?? bd.user_cached_usd) || 0);

  let segs = [];
  if (st === "tools") {
    segs = userToolsSegs(up, round && round.index).slice();
    if (cacheTok > 0 || cacheUsd > 0) {
      segs.push({
        k: "cached",
        label: "cached",
        legendKey: "cached",
        v: cacheUsd || 0,
        tok: cacheTok || 0,
        color: COST_COLORS.cached,
      });
    }
  } else if (st === "parts") {
    const fold = userToolsSegs(up, round && round.index);
    let userUsd = fold.reduce((a, s) => a + (Number(s.v) || 0), 0);
    let userTok = fold.reduce((a, s) => a + (Number(s.tok) || 0), 0);
    if (!(userUsd > 0 || userTok > 0)) {
      userUsd = inUsd;
      userTok = inTok;
    }
    if (userUsd > 0 || userTok > 0) {
      segs.push({
        k: "user",
        label: "user",
        legendKey: "user",
        v: userUsd || 0,
        tok: userTok || 0,
        color: COST_COLORS.user,
      });
    }
    if (cacheTok > 0 || cacheUsd > 0) {
      segs.push({
        k: "cached",
        label: "cached",
        legendKey: "cached",
        v: cacheUsd || 0,
        tok: cacheTok || 0,
        color: COST_COLORS.cached,
      });
    }
  } else {
    if (inTok > 0 || inUsd > 0) {
      segs.push({
        k: "in",
        label: "In",
        legendKey: "in",
        v: inUsd || 0,
        tok: inTok || 0,
        color: COST_COLORS.in,
      });
    }
    if (cacheTok > 0 || cacheUsd > 0) {
      segs.push({
        k: "cached",
        label: "Cached",
        legendKey: "cached",
        v: cacheUsd || 0,
        tok: cacheTok || 0,
        color: COST_COLORS.cached,
      });
    }
  }
  segs = segs.filter((s) => (Number(s.v) || 0) > 0 || (Number(s.tok) || 0) > 0);
  if (!segs.length) return null;
  const total = segs.reduce((a, s) => a + (Number(s.v) || 0), 0);
  const totalTok = segs.reduce((a, s) => a + (Number(s.tok) || 0), 0);
  return {
    segs,
    in: inUsd || 0,
    cached: cacheUsd || 0,
    out: 0,
    total: total || 0,
    total_tok: totalTok || 0,
    official: null,
    index: "user",
    label: "User",
    kind: "user",
    uncached_tokens: inTok || 0,
    cached_tokens: cacheTok || 0,
    out_tokens: 0,
  };
}

function callCostParts(step, callIndex, round, opts) {
  const se = step.estimate || {};
  // Stack draw order: first segment = bottom of bar.
  // Desired top→bottom (hierarchy colors): thought · reasoning · message ·
  // toolreqs(by name) · LLM Out · tools(by name). So push bottom-first.
  const bottomExtra = []; // user / residual / cached (below hierarchy cats)
  const toolSegs = [];    // harness tools (bottom of hierarchy stack)
  const llmOutSegs = [];
  const reqSegs = [];
  const msgSegs = [];
  const reasonSegs = [];
  const thoughtSegs = [];

  // Cached (this call prompt prefix)
  const ccache = Number(step.cost_cached_usd ?? se.cost_cached_usd) || 0;
  // First LLM call: User Tools = prompt / LLM Answer RN-1 / Compact Out.
  // Skip when drill draws a separate User bar (omitUser).
  const isFirstCall = Number(callIndex) === 1;
  const up = round && round.user_prompt;
  if (isFirstCall && up && !(opts && opts.omitUser)) {
    for (const s of userToolsSegs(up, round && round.index)) {
      bottomExtra.push(s);
    }
  }
  // Caused In (tree) — LLM Out→In + tools split by name (same rules as toolreqs)
  let causedIn = Number(step.cost_in_usd ?? se.cost_in_usd) || 0;
  let causedInTok = Number(step.uncached_input_tokens ?? se.uncached_input_tokens) || 0;
  let harnessUsd = 0;
  let harnessTok = 0;
  const toolAgg = new Map(); // normName → {v, tok, n}
  let llmOutUsd = 0;
  let llmOutTok = 0;
  const compactOutSegs = [];
  for (const ch of step.children || []) {
    if (ch.kind !== "phase_harness") continue;
    for (const sub of ch.children || []) {
      if (sub.kind === "late_context") continue; // redistributed into tools
      if (sub.kind === "hook") continue; // Hook not on Cost per Round graph
      const u = Number(sub.cost_in_usd) || 0;
      const tk = Number(sub.tokens_in || sub.context_delta) || 0;
      if (sub.kind === "compact_out_in") {
        // Between-rounds: User owns it (already in bottomExtra). Mid-round: own cat.
        if (String(sub.attribution || "") === "user") {
          // Accounted under User — peel out of Call In so it is not "in residual".
          causedIn = Math.max(0, causedIn - u);
          causedInTok = Math.max(0, causedInTok - tk);
          continue;
        }
        if (u > 0 || tk > 0) {
          const cn = sub.compact_index;
          const lab = (cn != null && Number(cn) > 0)
            ? ("Compact " + Number(cn) + " Out")
            : "Compact Out";
          compactOutSegs.push({
            k: "compact_out",
            label: lab,
            legendKey: "compact_out",
            v: u || 0,
            tok: tk || 0,
            color: COST_COLORS.compact_out,
          });
          harnessUsd += u;
          harnessTok += tk;
        }
        continue;
      }
      if (u <= 0 && tk <= 0) continue;
      harnessUsd += u;
      harnessTok += tk;
      if (sub.kind === "llm_to_in") {
        llmOutUsd += u || 0;
        llmOutTok += tk || 0;
        continue;
      }
      // tool / any other harness child → per-name category (never "tool x2" as a key)
      const name = normToolCatName(sub.name || sub.title || "tool");
      const prev = toolAgg.get(name) || { v: 0, tok: 0, n: 0 };
      prev.v += u || 0;
      prev.tok += tk || 0;
      prev.n += 1;
      toolAgg.set(name, prev);
    }
  }
  for (const s of compactOutSegs) toolSegs.push(s);
  // One segment per tool name (label stable → legend consolidates grep / grep x2)
  for (const [name, g] of toolAgg) {
    if (!(g.v > 0 || g.tok > 0)) continue;
    toolSegs.push({
      k: "tool",
      label: name,
      legendKey: "tool:" + name,
      n: g.n,
      v: g.v,
      tok: g.tok,
      color: COST_COLORS.tool,
    });
  }
  if (llmOutUsd > 0 || llmOutTok > 0) {
    llmOutSegs.push({
      k: "llm_out_in",
      label: "LLM Out→In",
      legendKey: "llm_out_in",
      v: llmOutUsd,
      tok: llmOutTok,
      color: COST_COLORS.llm,
    });
  }

  // Tools In = harness children as in the tree. No scale / residual to Call In.
  if (!(toolSegs.length || llmOutSegs.length || compactOutSegs.length)
    && (causedIn > 0 || causedInTok > 0)) {
    bottomExtra.push({
      k: "in", label: "in", legendKey: "in",
      v: causedIn, tok: causedInTok, color: COST_COLORS.in,
    });
  }
  const ccacheTok = Number(step.cached_read_tokens ?? se.cached_read_tokens) || 0;
  if (ccache > 0 || ccacheTok > 0) {
    bottomExtra.push({
      k: "cached",
      label: "cached",
      legendKey: "cached",
      v: ccache,
      tok: ccacheTok,
      color: COST_COLORS.cached,
    });
  }

  // LLM out — tool requests split by name (same norm as tools: no "xN" categories)
  let thoughtU = 0, encU = 0, msgU = 0;
  let thoughtT = 0, encT = 0, msgT = 0;
  const reqAgg = new Map(); // normName → {v, tok, n}
  for (const ch of step.children || []) {
    if (ch.kind !== "phase_llm") continue;
    for (const sub of ch.children || []) {
      const u = Number(sub.cost_out_usd) || 0;
      const tk = Number(
        sub.tokens_out || sub.tokenizer_tokens || sub.tokens || sub.output_tokens
      ) || 0;
      if (sub.kind === "thought") { thoughtU += u; thoughtT += tk; }
      else if (sub.kind === "reasoning") { encU += u; encT += tk; }
      else if (sub.kind === "message") { msgU += u; msgT += tk; }
      else if (sub.kind === "tool_request") {
        const name = normToolCatName(sub.name || sub.title || "tool request");
        const prev = reqAgg.get(name) || { v: 0, tok: 0, n: 0 };
        prev.v += u;
        prev.tok += tk;
        prev.n += 1;
        reqAgg.set(name, prev);
      } else if (sub.kind === "tool_requests") {
        // Expand children first; else declared_tools; else parent lump
        const kids = (sub.children || []).filter(
          k => k && (k.kind === "tool_request" || k.kind === "tool_requests")
        );
        if (kids.length) {
          for (const kid of kids) {
            const ku = Number(kid.cost_out_usd) || 0;
            const kt = Number(
              kid.tokens_out || kid.tokenizer_tokens || kid.tokens || 0
            ) || 0;
            const name = normToolCatName(kid.name || kid.title || "tool request");
            const nExtra = Math.max(1, Number(kid.count) || 1);
            const prev = reqAgg.get(name) || { v: 0, tok: 0, n: 0 };
            prev.v += ku;
            prev.tok += kt;
            prev.n += nExtra;
            reqAgg.set(name, prev);
          }
          // If parent holds the $ and kids don't, split parent $ by child counts
          const kidsSum = kids.reduce((a, k) => a + (Number(k.cost_out_usd) || 0), 0);
          const kidsTok = kids.reduce((a, k) => a + (Number(k.tokens_out || k.tokenizer_tokens) || 0), 0);
          const parentU = Number(sub.cost_out_usd) || 0;
          const parentT = Number(sub.tokens_out || sub.tokenizer_tokens) || 0;
          if ((parentU > 0 && kidsSum <= 0) || (parentT > 0 && kidsTok <= 0)) {
            const weights = kids.map(k => Math.max(1, Number(k.count) || 1));
            const wSum = weights.reduce((a, b) => a + b, 0) || 1;
            kids.forEach((kid, i) => {
              const name = normToolCatName(kid.name || kid.title || "tool request");
              const prev = reqAgg.get(name) || { v: 0, tok: 0, n: 0 };
              if (parentU > 0 && kidsSum <= 0)
                prev.v += parentU * (weights[i] / wSum);
              if (parentT > 0 && kidsTok <= 0)
                prev.tok += parentT * (weights[i] / wSum);
              reqAgg.set(name, prev);
            });
          }
        } else {
          const declared = sub.declared_tools || [];
          const parentU = Number(sub.cost_out_usd) || 0;
          const parentT = Number(sub.tokens_out || sub.tokenizer_tokens) || 0;
          if (declared.length) {
            const parts = declared.map(d => {
              const raw = typeof d === "string" ? d : (d && d.name) || "tool request";
              // declared may be "grep×2" — peel count
              const m = String(raw).match(/^(.*?)(?:\s*[x×]\s*(\d+)\s*)$/i);
              const name = normToolCatName(m ? m[1] : raw);
              const n = m && m[2] ? Math.max(1, parseInt(m[2], 10)) : 1;
              return { name, n };
            });
            const wSum = parts.reduce((a, p) => a + p.n, 0) || 1;
            for (const p of parts) {
              const prev = reqAgg.get(p.name) || { v: 0, tok: 0, n: 0 };
              prev.n += p.n;
              prev.v += parentU > 0 ? parentU * (p.n / wSum) : 0;
              prev.tok += parentT > 0 ? parentT * (p.n / wSum) : 0;
              reqAgg.set(p.name, prev);
            }
          } else if (parentU > 0 || parentT > 0) {
            const name = normToolCatName(sub.name || sub.title || "tool request");
            const prev = reqAgg.get(name) || { v: 0, tok: 0, n: 0 };
            prev.v += parentU;
            prev.tok += parentT;
            prev.n += 1;
            reqAgg.set(name, prev);
          }
        }
      }
    }
  }
  const cout = Number(step.cost_out_usd ?? se.cost_out_usd) || 0;
  const coutT = Number(step.output_tokens ?? se.output_tokens) || 0;
  if (!(thoughtU || encU || msgU || reqAgg.size || thoughtT || encT || msgT)) {
    if (cout > 0 || coutT > 0)
      thoughtSegs.push({
        k: "out", label: "out", legendKey: "out",
        v: cout, tok: coutT, color: COST_COLORS.out,
      });
  } else {
    // Out cats = tree LLM children as priced. No scale to official Out.
    const estReT = Number(
      se.output_reasoning_tokens
      ?? (step.composition && step.composition.reasoning_encrypted_out)
    ) || 0;
    if (estReT > 0 && !(encT > 0)) encT = estReT;
    if (thoughtU > 0 || thoughtT > 0)
      thoughtSegs.push({
        k: "thought", label: "thought", legendKey: "thought",
        v: thoughtU, tok: thoughtT, color: COST_COLORS.thought,
      });
    if (encU > 0 || encT > 0)
      reasonSegs.push({
        k: "reasoning",
        label: "reasoning",
        legendKey: "reasoning",
        v: encU,
        tok: encT,
        color: COST_COLORS.reasoning,
      });
    if (msgU > 0 || msgT > 0)
      msgSegs.push({
        k: "message", label: "message", legendKey: "message",
        v: msgU, tok: msgT, color: COST_COLORS.message,
      });
    for (const [name, g] of reqAgg) {
      if (!(g.v > 0 || g.tok > 0)) continue;
      reqSegs.push({
        k: "toolreq",
        label: name,
        legendKey: "toolreq:" + name,
        n: g.n,
        v: g.v,
        tok: g.tok,
        color: COST_COLORS.toolreq,
      });
    }
  }

  // bottom → top: extras · tools · LLM Out · toolreqs · message · reasoning · thought
  const segs = [
    ...bottomExtra,
    ...toolSegs,
    ...llmOutSegs,
    ...reqSegs,
    ...msgSegs,
    ...reasonSegs,
    ...thoughtSegs,
  ].filter(s => (s.v > 0) || (s.tok > 0));

  const total = segs.reduce((a, s) => a + s.v, 0);
  const totalTok = segs.reduce((a, s) => a + (Number(s.tok) || 0), 0);
  return {
    segs,
    total,
    total_tok: totalTok,
    index: callIndex,
    label: "C" + callIndex,
    kind: "call",
    official: null,
    paid_start: step.paid_at_start_usd,
    estimate_usd: step.estimate_usd ?? se.api_call_usd,
  };
}

function _sumThoughtFromSteps(round) {
  let usd = 0, tok = 0;
  for (const step of (round && round.model_steps) || []) {
    for (const ch of step.children || []) {
      if (ch.kind !== "phase_llm") continue;
      for (const sub of ch.children || []) {
        if (sub.kind !== "thought") continue;
        usd += Number(sub.cost_out_usd) || 0;
        tok += Number(sub.tokens_out || sub.tokenizer_tokens || sub.tokens) || 0;
      }
    }
  }
  return { usd, tok };
}

function foldCallToIo(bar) {
  let inn = 0, cache = 0, out = 0, innT = 0, cacheT = 0, outT = 0;
  for (const s of bar.segs || []) {
    const v = Number(s.v) || 0;
    const t = Number(s.tok) || 0;
    if (s.k === "cached") { cache += v; cacheT += t; }
    else if (s.k === "in" || s.k === "user" || s.k === "harness" || s.k === "tool"
        || s.k === "llm_out_in" || s.k === "residual" || s.k === "cache_miss"
        || s.k === "prompt" || s.k === "llm_answer" || s.k === "compact_out"
        || s.k === "sys_prompt" || s.k === "user_info" || s.k === "reminders"
        || s.k === "tool_def" || s.k === "system") {
      inn += v; innT += t;
    } else if (s.k === "sub") {
      /* I/O folds child In/Cached/Out before this; ignore leftover sub segs */
    } else {
      out += v; outT += t;
    }
  }
  const segs = [];
  if (inn > 0 || innT > 0)
    segs.push({ k: "in", label: "In", legendKey: "in", v: inn, tok: innT, color: COST_COLORS.in });
  if (cache > 0 || cacheT > 0)
    segs.push({ k: "cached", label: "Cached", legendKey: "cached", v: cache, tok: cacheT, color: COST_COLORS.cached });
  if (out > 0 || outT > 0)
    segs.push({ k: "out", label: "Out", legendKey: "out", v: out, tok: outT, color: COST_COLORS.out });
  return { ...bar, segs, total: inn + cache + out, total_tok: innT + cacheT + outT };
}

function foldCallToParts(bar) {
  const map = new Map();
  for (const s of bar.segs || []) {
    let k = s.k, lab = s.label, key = costSegKey(s);
    if (s.k === "tool" || s.k === "llm_out_in") {
      k = "harness"; lab = "harness"; key = "harness";
    } else if (s.k === "prompt" || s.k === "llm_answer" || s.k === "compact_out" || s.k === "user") {
      k = "user"; lab = "user"; key = "user";
    } else if (s.k === "sys_prompt" || s.k === "user_info" || s.k === "reminders" || s.k === "tool_def") {
      k = "system"; lab = "system"; key = "system";
    } else if (s.k === "toolreq") {
      k = "toolreq"; lab = "tool req"; key = "toolreq";
    } else if (s.k === "sub") {
      k = "sub"; lab = "Sub Agent"; key = "sub";
    }
    const prev = map.get(key);
    if (!prev) {
      map.set(key, {
        k, label: lab, legendKey: key,
        v: Number(s.v) || 0, tok: Number(s.tok) || 0,
        color: k === "harness" ? COST_COLORS.harness
          : k === "toolreq" ? COST_COLORS.toolreq
          : s.color,
      });
    } else {
      prev.v += Number(s.v) || 0;
      prev.tok += Number(s.tok) || 0;
    }
  }
  const segs = [...map.values()].filter((s) => s.v > 0 || s.tok > 0);
  return {
    ...bar,
    segs,
    total: segs.reduce((a, s) => a + s.v, 0),
    total_tok: segs.reduce((a, s) => a + (s.tok || 0), 0),
  };
}

function applyDrillStack(bar, stack) {
  if (!bar) return bar;
  if (stack === "io") return foldCallToIo(bar);
  if (stack === "parts") return foldCallToParts(bar);
  return bar;
}

function subagentCostBar(sa) {
  if (!sa || !sa.session_id) return null;
  const u = sa.usage || {};
  const tin = Number(sa.tokens_in);
  const tcache = Number(sa.tokens_cached);
  const tout = Number(sa.tokens_out);
  const inTok = Number.isFinite(tin)
    ? tin
    : Math.max(0, Number(u.inputTokens || 0) - Number(u.cachedReadTokens || 0));
  const cacheTok = Number.isFinite(tcache) ? tcache : Number(u.cachedReadTokens || 0);
  const outTok = Number.isFinite(tout) ? tout : Number(u.outputTokens || 0);
  const official = sa.official_usd != null ? Number(sa.official_usd) : null;
  const cin = Number(sa.cost_in_usd) || 0;
  const ccache = Number(sa.cost_cached_usd) || 0;
  const cout = Number(sa.cost_out_usd) || 0;
  const est = Number(sa.estimate_usd) || (cin + ccache + cout);
  const sumT = Math.max(0, inTok) + Math.max(0, cacheTok) + Math.max(0, outTok);
  const total = est;
  const totalTok = sumT;
  if (!(total > 0) && !(totalTok > 0)) return null;
  const n = sa.n != null ? sa.n : "";
  const title = sa.title || sa.label || sa.agent_name || "Sub Agent";
  return {
    segs: [{
      k: "sub",
      label: "Sub",
      legendKey: "sub",
      v: total || 0,
      tok: totalTok || 0,
      color: COST_COLORS.sub,
    }],
    in: cin,
    cached: ccache,
    out: cout,
    total: total || 0,
    total_tok: totalTok || 0,
    official: official,
    index: "S" + n,
    label: "S" + n,
    kind: "subagent",
    session_id: sa.session_id,
    title,
    agent_name: sa.agent_name,
    uncached_tokens: inTok,
    cached_tokens: cacheTok,
    out_tokens: outTok,
    n: Number(n) || 0,
  };
}

function collectRoundSubagentBars(round) {
  if (!round) return [];
  const seen = new Set();
  const out = [];
  for (const step of round.model_steps || []) {
    if (!step) continue;
    for (const sa of step.subagents_after || []) {
      const id = sa && sa.session_id;
      if (!id) continue;
      const key = `${id}:${sa.is_sys ? "sys" : (sa.resume_index || 0)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const bar = subagentCostBar(sa);
      if (bar) out.push(bar);
    }
  }
  return out;
}

/** Fold child bills into the parent round bar according to stack mode. */
function attachSubagentSegsToTurn(turnBar, round, stack) {
  if (!turnBar) return turnBar;
  const kids = collectRoundSubagentBars(round);
  if (!kids.length) return turnBar;
  if (!turnBar.segs) turnBar.segs = [];
  const mode = stack || (window.__costChart && window.__costChart.stack) || "io";
  const byN = new Map();
  for (const sb of kids) {
    const n = String(sb.label || "").replace(/^S/, "") || "1";
    const prev = byN.get(n);
    if (!prev) {
      byN.set(n, {
        n,
        in: Number(sb.in) || 0,
        cached: Number(sb.cached) || 0,
        out: Number(sb.out) || 0,
        total: Number(sb.total) || 0,
        total_tok: Number(sb.total_tok) || 0,
        uncached_tokens: Number(sb.uncached_tokens) || 0,
        cached_tokens: Number(sb.cached_tokens) || 0,
        out_tokens: Number(sb.out_tokens) || 0,
        session_id: sb.session_id,
        title: sb.title,
      });
    } else {
      prev.in += Number(sb.in) || 0;
      prev.cached += Number(sb.cached) || 0;
      prev.out += Number(sb.out) || 0;
      prev.total += Number(sb.total) || 0;
      prev.total_tok += Number(sb.total_tok) || 0;
      prev.uncached_tokens += Number(sb.uncached_tokens) || 0;
      prev.cached_tokens += Number(sb.cached_tokens) || 0;
      prev.out_tokens += Number(sb.out_tokens) || 0;
    }
  }
  const agents = [...byN.values()].sort((a, b) => Number(a.n) - Number(b.n));
  if (mode === "io") {
    let inn = 0, cache = 0, out = 0, innT = 0, cacheT = 0, outT = 0;
    for (const a of agents) {
      inn += a.in; cache += a.cached; out += a.out;
      innT += a.uncached_tokens; cacheT += a.cached_tokens; outT += a.out_tokens;
    }
    const add = [];
    if (inn > 0 || innT > 0) add.push({ k: "in", label: "In", legendKey: "in", v: inn, tok: innT, color: COST_COLORS.in });
    if (cache > 0 || cacheT > 0) add.push({ k: "cached", label: "Cached", legendKey: "cached", v: cache, tok: cacheT, color: COST_COLORS.cached });
    if (out > 0 || outT > 0) add.push({ k: "out", label: "Out", legendKey: "out", v: out, tok: outT, color: COST_COLORS.out });
    turnBar.segs = mergeCostSegs(turnBar.segs, add);
  } else if (mode === "parts") {
    let tot = 0, tok = 0;
    for (const a of agents) { tot += a.total; tok += a.total_tok; }
    turnBar.segs = mergeCostSegs(turnBar.segs, [{
      k: "sub", label: "Sub Agent", legendKey: "sub",
      v: tot, tok, color: COST_COLORS.sub,
    }]);
  } else {
    for (const a of agents) {
      turnBar.segs.push({
        k: "sub",
        label: "Sub Agent " + a.n,
        legendKey: "sub:" + a.n,
        v: a.total || 0,
        tok: a.total_tok || 0,
        color: subShade(a.n),
        session_id: a.session_id,
        title: a.title,
      });
    }
  }
  for (const a of agents) {
    turnBar.total = (Number(turnBar.total) || 0) + (a.total || 0);
    turnBar.total_tok = (Number(turnBar.total_tok) || 0) + (a.total_tok || 0);
    turnBar.in = (Number(turnBar.in) || 0) + (a.in || 0);
    turnBar.cached = (Number(turnBar.cached) || 0) + (a.cached || 0);
    turnBar.out = (Number(turnBar.out) || 0) + (a.out || 0);
  }
  turnBar.has_subagents = true;
  return turnBar;
}

function eventMs(e) {
  const n = Number(e && e.agent_ms);
  return Number.isFinite(n) ? n : 0;
}

function compactCostBar(c, compactIndex, { detail } = {}) {
  if (!c || c.kind !== "compaction") return null;
  const segs = [];
  // In XOR Cached (miss vs hit) + Out (compressed history). Never both.
  const miss = !!c.pre_read_cache_miss;
  const preTok = Number(c.pre_read_tokens) || Number(c.tokens_before) || 0;
  const preUnc = Number(c.pre_read_uncached_usd) || 0;
  const preCache = Number(c.pre_read_cached_usd) || 0;
  const preUncT = Number(c.pre_read_uncached_tokens) || 0;
  const preCacheT = Number(c.pre_read_cached_tokens) || 0;
  const outU = Number(c.out_usd) || 0;
  const outT = Number(c.out_tokens) || 0;
  let inU = 0, inT = 0, cacheU = 0, cacheT = 0;
  if (miss || (preUncT > 0 && !(preCacheT > 0))) {
    inU = preUnc || Number(c.pre_read_usd) || 0;
    inT = preUncT || preTok;
  } else {
    cacheU = preCache || Number(c.pre_read_usd) || 0;
    cacheT = preCacheT || preTok;
  }
  if (detail) {
    const u = (inU || 0) + (cacheU || 0) + (outU || 0);
    const t = (inT || 0) + (cacheT || 0) + (outT || 0);
    if (u > 0 || t > 0)
      segs.push({ k: "compact", label: "compact", legendKey: "compact", v: u, tok: t, color: COST_COLORS.compact });
  } else {
    if (inU > 0 || inT > 0)
      segs.push({ k: "in", label: "In", legendKey: "in", v: inU, tok: inT, color: COST_COLORS.in });
    if (cacheU > 0 || cacheT > 0)
      segs.push({ k: "cached", label: "Cached", legendKey: "cached", v: cacheU, tok: cacheT, color: COST_COLORS.cached });
    if (outU > 0 || outT > 0)
      segs.push({ k: "out", label: "Out", legendKey: "out", v: outU, tok: outT, color: COST_COLORS.out });
  }
  const total = Number(c.cost_usd) != null && Number(c.cost_usd) > 0
    ? Number(c.cost_usd)
    : segs.reduce((a, s) => a + s.v, 0);
  const totalTok = segs.reduce((a, s) => a + (Number(s.tok) || 0), 0);
  if (!(total > 0) && !(totalTok > 0) && !segs.length) {
    const before = Number(c.tokens_before) || 0;
    if (before <= 0) return null;
    segs.push({
      k: detail ? "compact" : "cached",
      label: detail ? "compact" : "pre-read",
      legendKey: detail ? "compact" : "cached",
      v: 0.000001,
      tok: before,
      color: detail ? COST_COLORS.compact : COST_COLORS.cached,
    });
  }
  return {
    segs: segs.filter(s => s.v > 0 || s.tok > 0),
    total: total || segs.reduce((a, s) => a + s.v, 0),
    total_tok: totalTok,
    index: "C" + compactIndex,
    label: "C" + compactIndex,
    kind: "compact",
    official: null,
    tokens_before: c.tokens_before,
    tokens_after: c.tokens_after,
    estimate_usd: total,
  };
}

function recapCostBar(c, recapIndex, { detail } = {}) {
  if (!c || c.kind !== "session_recap") return null;
  const segs = [];
  const inU = Number(c.prompt_in_usd) || 0;
  const inT = Number(c.prompt_tokens) || 0;
  const cacheU = Number(c.pre_read_cached_usd) || 0;
  const cacheT = Number(c.context_tokens ?? c.context_cached_tokens) || 0;
  const outU = Number(c.out_usd) || 0;
  const outT = Number(c.out_tokens) || 0;
  if (detail) {
    const u = (inU || 0) + (cacheU || 0) + (outU || 0);
    const t = (inT || 0) + (cacheT || 0) + (outT || 0);
    if (u > 0 || t > 0)
      segs.push({ k: "recap", label: "recap", legendKey: "recap", v: u, tok: t, color: COST_COLORS.recap });
  } else {
    if (inU > 0 || inT > 0)
      segs.push({ k: "in", label: "In", legendKey: "in", v: inU, tok: inT, color: COST_COLORS.in });
    if (cacheU > 0 || cacheT > 0)
      segs.push({ k: "cached", label: "Cached", legendKey: "cached", v: cacheU, tok: cacheT, color: COST_COLORS.cached });
    if (outU > 0 || outT > 0)
      segs.push({ k: "out", label: "Out", legendKey: "out", v: outU, tok: outT, color: COST_COLORS.out });
  }
  const total = Number(c.cost_usd) != null && Number(c.cost_usd) > 0
    ? Number(c.cost_usd)
    : segs.reduce((a, s) => a + s.v, 0);
  const totalTok = segs.reduce((a, s) => a + (Number(s.tok) || 0), 0);
  if (!(total > 0) && !(totalTok > 0) && !segs.length) return null;
  return {
    segs: segs.filter(s => s.v > 0 || s.tok > 0),
    total: total || 0,
    total_tok: totalTok,
    index: "R" + recapIndex,
    label: "Rec" + recapIndex,
    kind: "recap",
    official: null,
    estimate_usd: total,
  };
}

const COST_Y_LEFT = 56;
const PLOT_PAD_L = 6;
const COST_CHART_H = 240;
const CTX_CHART_H = 260;
const TREE_FLOOR_PX = 120;
const COST_MIN_SLOT = 36;
const COST_MAX_SLOT = 420;
const MIN_COST_SLOTS = 8;
const GANTT_MIN_BAR_PX = 4;
const GANTT_MIN_SPAN = 5 * 60;
const GANTT_MAX_H = 920;
const GANTT_MIN_H = 160;
const X_AXIS_BAND = 40;

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

function storedCostChartH() {
  try {
    const n = parseInt(localStorage.getItem("tt-cost-chart-h") || "", 10);
    if (Number.isFinite(n)) return clampChartH(n, COST_CHART_H, "costPanel");
  } catch { /* ignore */ }
  return clampChartH(COST_CHART_H, COST_CHART_H, "costPanel");
}

function storedCtxChartH() {
  try {
    const n = parseInt(localStorage.getItem("tt-ctx-chart-h") || "", 10);
    if (Number.isFinite(n)) return clampChartH(n, CTX_CHART_H, "ctxPanel");
  } catch { /* ignore */ }
  return clampChartH(CTX_CHART_H, CTX_CHART_H, "ctxPanel");
}

function ensureCostChartH(store) {
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

function chartViewKey() {
  if (document.body.classList.contains("scope-period")) {
    const ag = window.__aggChart || {};
    return ["p", ag.stack || "io", ag.byLabel ? 1 : 0, ag.cumulative ? 1 : 0, ag.normalized ? 1 : 0, ag.timeline ? 1 : 0, ag.rate ? 1 : 0, ag.ioStep ? 1 : 0, ag.rateGrain || "-"].join(":");
  }
  const st = window.__costChart || {};
  return ["s", st.stack || "io", st.byLabel ? 1 : 0, st.drillTurn == null ? "-" : String(st.drillTurn)].join(":");
}

function planXLabels(ctx, labels, groupW, { temporal } = {}) {
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

function drawXLabel(ctx, text, x, y, rotate) {
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

function guessXPlan(labels, barCount, temporal) {
  const scroller = $("costChartScroll");
  const viewW = (scroller && scroller.clientWidth) || 600;
  const nSlots = Math.max(barCount || 1, temporal ? MIN_COST_SLOTS : 1);
  const groupW = Math.max(1, (viewW - PLOT_PAD_L - 12) / nSlots);
  const ctx = document.createElement("canvas").getContext("2d");
  return planXLabels(ctx, labels, groupW, { temporal });
}

function applyXPadHeight(canvas, padB) {
  if (!canvas) return;
  const store = zoomStore() || {};
  const base = ensureCostChartH(store);
  const extra = Math.max(0, (padB || 28) - 28);
  canvas.style.height = (base + extra) + "px";
}

function collectLabelLegend(bars, unit) {
  const map = new Map();
  for (const b of bars || []) {
    for (const seg of b.segs || []) {
      if (!(costSegMetric(seg, unit) > 0)) continue;
      const key = costSegKey(seg);
      if (!key || key === "official") continue;
      if (!map.has(key))
        map.set(key, { label: costDisplayLabel(seg), color: seg.color, k: seg.k });
    }
  }
  return [...map.entries()];
}

function pointerInXAxis(ev, wrap) {
  if (!wrap) return false;
  const r = wrap.getBoundingClientRect();
  const sb = 16;
  const top = r.bottom - X_AXIS_BAND;
  const bot = r.bottom - sb;
  return ev.clientY >= top && ev.clientY <= bot;
}

function zoomStore() {
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

function slotRange(viewW, barCount, maxSlot) {
  const nSlots = Math.max(barCount, MIN_COST_SLOTS);
  const plotAvail = Math.max(10, viewW - PLOT_PAD_L - 12);
  const minSlot = plotAvail / nSlots;
  const cap = maxSlot || COST_MAX_SLOT;
  return { nSlots, minSlot, maxSlot: Math.max(cap, minSlot), plotAvail };
}

function clampYViewZoom(z) {
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

function clampYPan(pan, n, visN) {
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
function onGanttSelect(fn) {
  _ganttSelectFn = fn;
}

function bindYStretch() {
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

function bindChartResize() {
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

function bindCtxChartResize(redrawFn) {
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

function setGanttChrome(on) {
  const wrap = $("costChartWrap");
  if (wrap) wrap.classList.toggle("is-gantt", !!on);
  const btn = $("chartResize");
  if (btn) btn.hidden = false;
  bindChartResize();
}

function layoutGanttCanvas(canvas) {
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
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.floor(viewW * dpr));
  canvas.height = Math.max(1, Math.floor(h * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, viewW, h);
  bindYStretch();
  bindChartResize();
  return { w: viewW, h, ctx };
}

function layoutCostCanvas(canvas, barCount, { forceFit, maxSlot } = {}) {
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
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(w * dpr));
  canvas.height = Math.max(1, Math.floor(h * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

function redrawCostChart() {
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

function bindCostZoom() {
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
  const dpr = window.devicePixelRatio || 1;
  const w = COST_Y_LEFT;
  yAxis.width = Math.max(1, Math.floor(w * dpr));
  yAxis.height = Math.max(1, Math.floor(h * dpr));
  yAxis.style.width = w + "px";
  yAxis.style.height = h + "px";
  const ctx = yAxis.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

/** Parts stack bottom→top (legend: bottom first, top last). */
const PARTS_STACK_ORDER = [
  "cached", "cache_miss", "harness", "reasoning", "toolreq",
  "message", "thought", "user", "compact", "recap",
  "in", "out", "system", "sys_prompt", "user_info", "reminders", "tool_def",
  "llm_out_in", "official", "sub",
];

/** User Tools breakdown keys (after toolreq:* and tool:* in Tools mode). */
const USER_BREAKDOWN_ORDER = ["prompt", "llm_answer", "compact_out"];

function currentCostStack() {
  if (document.body.classList.contains("scope-period"))
    return (window.__aggChart && window.__aggChart.stack) || "io";
  return (window.__costChart && window.__costChart.stack) || "io";
}

function loadLegendOrder(stack) {
  try {
    const raw = localStorage.getItem("tt-leg-order-" + (stack || "io"));
    if (!raw) return null;
    const arr = JSON.parse(raw);
    return Array.isArray(arr) && arr.length ? arr.map(String) : null;
  } catch {
    return null;
  }
}

function saveLegendOrder(stack, keys) {
  try {
    localStorage.setItem("tt-leg-order-" + (stack || "io"), JSON.stringify(keys || []));
  } catch { /* ignore */ }
}

function clearLegendOrder(stack) {
  try {
    localStorage.removeItem("tt-leg-order-" + (stack || "io"));
  } catch { /* ignore */ }
}

/** Default rank: bottom of bar = low. Tools: fixed → toolreq:* → tool:* → user breakdown. */
function defaultStackRank(key, meta, stack) {
  const k = String((meta && meta.k) || key || "");
  const lab = String((meta && meta.label) || key || "");
  const sk = String(key || "");
  const st = stack || "io";

  if (st === "io") {
    const ioOrder = ["system", "in", "cached", "out", "sub", "official"];
    let fi = ioOrder.indexOf(sk);
    if (fi < 0) fi = ioOrder.indexOf(k);
    if (fi < 0) fi = ioOrder.indexOf(lab.toLowerCase());
    if (fi >= 0) return fi;
    if (sk.startsWith("sub:")) return 10 + (Number(sk.slice(4)) || 0);
    return 50;
  }

  if (st === "tools") {
    // Non-breakdown cats use PARTS order below; then toolreq:* · tool:* · user breakdown.
    if (sk.startsWith("toolreq:")) return 1000;
    if (k === "toolreq") return 999;
    if (sk.startsWith("tool:")) return 2000;
    if (k === "tool") return 1999;
    const ub = USER_BREAKDOWN_ORDER.indexOf(k);
    if (ub >= 0) return 3000 + ub;
    if (k === "llm_out_in" || sk === "llm_out_in") return 1950;
  }

  let fi = PARTS_STACK_ORDER.indexOf(sk);
  if (fi < 0) fi = PARTS_STACK_ORDER.indexOf(k);
  if (fi < 0) {
    const low = lab.toLowerCase();
    fi = PARTS_STACK_ORDER.indexOf(low);
    if (fi < 0 && low === "cache miss") fi = PARTS_STACK_ORDER.indexOf("cache_miss");
    if (fi < 0 && low === "tool req") fi = PARTS_STACK_ORDER.indexOf("toolreq");
  }
  if (fi >= 0) return fi;
  if (sk.startsWith("sub:")) return 400 + (Number(sk.slice(4)) || 0);
  if (k === "sub") return 400;
  if (k === "hook" || sk.startsWith("hook:")) return 9000;
  return 500 + lab.toLowerCase().charCodeAt(0) / 1000;
}

function _legendRank(key, meta) {
  const custom = loadLegendOrder(currentCostStack());
  if (custom && custom.length) {
    const i = custom.indexOf(String(key));
    if (i >= 0) return i;
    return custom.length + 1 + defaultStackRank(key, meta, currentCostStack());
  }
  return defaultStackRank(key, meta, currentCostStack());
}

function orderCostSegs(segs, stack) {
  const st = stack || currentCostStack();
  const custom = loadLegendOrder(st);
  return (segs || []).slice().sort((a, b) => {
    const ka = costSegKey(a), kb = costSegKey(b);
    const ra = custom && custom.length && custom.indexOf(ka) >= 0
      ? custom.indexOf(ka)
      : (custom && custom.length ? custom.length + 1 : 0) + defaultStackRank(ka, a, st);
    const rb = custom && custom.length && custom.indexOf(kb) >= 0
      ? custom.indexOf(kb)
      : (custom && custom.length ? custom.length + 1 : 0) + defaultStackRank(kb, b, st);
    if (ra !== rb) return ra - rb;
    return String(costDisplayLabel(a)).localeCompare(String(costDisplayLabel(b)));
  });
}

function sortLegendItems(items, stack) {
  const st = stack || currentCostStack();
  return (items || []).slice().sort((a, b) => {
    const ra = _legendRank(a[0], a[1]), rb = _legendRank(b[0], b[1]);
    if (ra !== rb) return ra - rb;
    return String((a[1] && a[1].label) || a[0]).localeCompare(String((b[1] && b[1].label) || b[0]));
  });
}

/** Tooltip rows: I/O keeps stack order; Parts/Tools sort small→large. */
function tipOrderedSegs(segs, stack, unit) {
  const list = (segs || []).filter((s) => {
    if (s && s._raw != null) return Number(s._raw) > 0 || Number(s.v) > 0;
    return costSegMetric(s, unit) > 0 || Number(s && s.v) > 0;
  });
  if ((stack || "io") === "io") return orderCostSegs(list, "io");
  return list.slice().sort((a, b) => {
    const va = a._raw != null ? Number(a._raw) : costSegMetric(a, unit);
    const vb = b._raw != null ? Number(b._raw) : costSegMetric(b, unit);
    if (va !== vb) return va - vb;
    return String(costDisplayLabel(a)).localeCompare(String(costDisplayLabel(b)));
  });
}

function formatTipSegLine(seg, unit, opts) {
  const color = (seg && seg.color) || "#aaa";
  const label = costDisplayLabel(seg);
  const cnt = seg && seg.n > 1 ? ` ×${seg.n}` : "";
  const extra = seg && seg.title ? " · " + seg.title : "";
  const raw = seg && seg._raw != null ? Number(seg._raw) : costSegMetric(seg, unit);
  if (opts && opts.normalized) {
    const pct = Number(seg.v) || 0;
    const pctTxt = fmtCostAxis(pct, "pct");
    return `<span style="color:${color}">${esc(pctTxt)}</span> `
      + `<span style="color:${color}">●</span> ${esc(label)}${esc(extra)}${cnt} `
      + `${fmtCostAxis(raw, unit)}`;
  }
  return `<span style="color:${color}">●</span> ${esc(label)}${esc(extra)}${cnt} ${fmtCostAxis(raw, unit)}`;
}

function periodSessionTipHead(b) {
  if (!b) return "";
  const sl = String(b.session_label || "");
  if (/^Session\s+/i.test(sl)) return sl;
  if (/^Sub Agent\s+/i.test(sl)) {
    const parent = String(b.label || "").split(".")[0];
    return parent ? `${parent} ${sl}` : sl;
  }
  return sl || String(b.label || "");
}

/** One bar per legend category — totals for the current view. */
function aggregateBarsByLabel(bars, hidden, unit) {
  const map = new Map();
  for (const b of bars || []) {
    for (const seg of b.segs || []) {
      if (isCostSegHidden(seg, hidden)) continue;
      const mv = costSegMetric(seg, unit);
      if (!(mv > 0) && !(Number(seg.v) > 0) && !(Number(seg.tok) > 0)) continue;
      const key = costSegKey(seg);
      if (!key || key === "official") continue;
      const label = costDisplayLabel(seg);
      const prev = map.get(key) || {
        key, label, color: seg.color, k: seg.k, v: 0, tok: 0,
      };
      prev.v += Number(seg.v) || 0;
      prev.tok += Number(seg.tok) || 0;
      if (!prev.color) prev.color = seg.color;
      map.set(key, prev);
    }
  }
  const rows = [...map.values()].sort((a, b) => {
    const ra = _legendRank(a.key, a), rb = _legendRank(b.key, b);
    if (ra !== rb) return ra - rb;
    return String(a.label).localeCompare(String(b.label));
  });
  return rows.map((g) => ({
    segs: [{
      k: g.k || g.key,
      label: g.label,
      legendKey: g.key,
      v: g.v,
      tok: g.tok,
      color: g.color,
    }],
    total: g.v,
    total_tok: g.tok,
    index: g.label,
    label: g.label,
    kind: "agg-label",
    official: null,
  }));
}

function drawBars(canvas, turns, rounds, opts) {
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
    const off = (!hidden.has("official") && unit === "usd" && p.official != null)
      ? p.official : 0;
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
    stack.forEach(seg => {
      const mv = costSegMetric(seg, unit);
      const segH = ((mv - 0) / (max - yMin)) * plotH;
      yBase -= segH;
      if (segH > 0.4) {
        ctx.fillStyle = seg.color;
        ctx.fillRect(x0, yBase, bw, segH);
      }
    });
    if (unit === "usd" && p.official != null && !hidden.has("official")) {
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
    if (unit === "usd")
      legendItems.push(["official", { label: "Official", color: COST_COLORS.official }]);
  } else if (st.drillTurn == null && unit === "usd") {
    if (!legendItems.some(([key]) => key === "official"))
      legendItems.unshift(["official", { label: "official", color: COST_COLORS.official }]);
  }
  // Re-show hidden chips only if that category exists in *current* legendMap
  for (const name of hidden) {
    if (legendMap.has(name)) continue; // already from segs
    if (!legendItems.some(([key]) => key === name)) {
      // only if it was a fixed overview key still relevant
      const fixedOk = ["System", "In", "Cached", "Out", "official", "in", "cached", "out", "system"].includes(name);
      if (!fixedOk) continue; // drop stale tool hides from UI
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
      canvas.style.cursor = (!st.byLabel && (found.p.kind === "turn" || found.p.kind === "subagent"))
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
      if (u === "usd" && p.official != null && !window.__costChart.hiddenLegend?.has("official"))
        lines.push(`<span class="muted">official ${fmtUsd(p.official)}</span>`);
      if (p.kind === "turn") lines.push(`<span class="muted">click: tree + drill calls</span>`);
      if (p.kind === "subagent") lines.push(`<span class="muted">click: open this sub-agent tab</span>`);
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

function renderCostLegend(legendItems, hidden) {
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

function setCostUnit(unit) {
  window.__costChart.unit = unit === "tokens" ? "tokens" : "usd";
  try { localStorage.setItem("tt-cost-unit", window.__costChart.unit); } catch { /* ignore */ }
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

function _mergeCatLists(into, extra) {
  const map = new Map();
  for (const s of into || []) map.set(s.key, { ...s });
  for (const s of extra || []) {
    const prev = map.get(s.key);
    if (prev) {
      prev.usd = (Number(prev.usd) || 0) + (Number(s.usd) || 0);
      prev.tok = (Number(prev.tok) || 0) + (Number(s.tok) || 0);
    } else map.set(s.key, { ...s });
  }
  return [...map.values()];
}

function _cumBuckets(buckets) {
  const out = [];
  let tin = 0, tc = 0, tout = 0, tr = 0, ci = 0, cc = 0, co = 0, cr = 0, tot = 0, est = 0;
  let parts = [], tools = [];
  for (const b of buckets || []) {
    tin += Number(b.tokens_in) || 0;
    tc += Number(b.tokens_cached) || 0;
    tout += Number(b.tokens_out) || 0;
    tr += Number(b.tokens_reason) || 0;
    ci += Number(b.cost_in_usd) || 0;
    cc += Number(b.cost_cached_usd) || 0;
    co += Number(b.cost_out_usd) || 0;
    cr += Number(b.cost_reason_usd) || 0;
    tot += Number(b.official_usd) || 0;
    est += Number(b.estimate_usd) || ((Number(b.cost_in_usd) || 0)
      + (Number(b.cost_cached_usd) || 0)
      + (Number(b.cost_out_usd) || 0));
    parts = _mergeCatLists(parts, b.parts);
    tools = _mergeCatLists(tools, b.tools);
    out.push({
      ...b,
      tokens_in: tin,
      tokens_cached: tc,
      tokens_out: tout,
      tokens_reason: tr,
      tokens_all: tin + tc + tout,
      cost_in_usd: ci,
      cost_cached_usd: cc,
      cost_out_usd: co,
      cost_reason_usd: cr,
      official_usd: tot,
      estimate_usd: est,
      parts: parts.map((s) => ({ ...s })),
      tools: tools.map((s) => ({ ...s })),
    });
  }
  return out;
}

/** Stacked In / Cached / Out histogram for period views. */
function drawAggBars(canvas, buckets, opts) {
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
  let src = cumulative ? _cumBuckets(buckets) : (buckets || []).slice();
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
        if (unit === "usd" && b.official_usd != null)
          lines.push(`<span class="muted">official ${fmtUsd(b.official_usd)}</span>`);
        html = lines.join("<br>");
      } else {
        const lines = [`<b>${esc(head)}</b>`];
        tipOrderedSegs(found.bar.segs || [], stackMode, unit).forEach((s) => {
          lines.push(formatTipSegLine(s, unit, { normalized: isNorm }));
        });
        if (!isNorm)
          lines.push(`<b>→ ${fmtCostAxis(tipTotal, unit)}</b>`);
        if (!isNorm && unit === "usd" && b.official_usd != null)
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
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = COST_Y_LEFT;
  yAxis.width = Math.max(1, Math.floor(w * dpr));
  yAxis.height = Math.max(1, Math.floor(h * dpr));
  yAxis.style.width = w + "px";
  yAxis.style.height = h + "px";
  const ctx = yAxis.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

function clampGanttWindow(t0, t1, p0, p1) {
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
function drawTimeline(canvas, agg) {
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

function fitGanttToSessions(rows) {
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

function ctxRateStore() {
  if (!window.__ctxRateChart) window.__ctxRateChart = {};
  return window.__ctxRateChart;
}

function rateHostIds(host) {
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

function niceRateYMax(rawMax) {
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

function layoutRateCanvas(canvas, barCount, ids) {
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
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(w * dpr));
  canvas.height = Math.max(1, Math.floor(h * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

function drawRateYOverlay(ids, { min, max, step, top, h, padB, format }) {
  const yAxis = $(ids.yAxis);
  if (!yAxis) return;
  yAxis.hidden = false;
  const dpr = window.devicePixelRatio || 1;
  const w = COST_Y_LEFT;
  yAxis.width = Math.max(1, Math.floor(w * dpr));
  yAxis.height = Math.max(1, Math.floor(h * dpr));
  yAxis.style.width = w + "px";
  yAxis.style.height = h + "px";
  const ctx = yAxis.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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
function strokeStepLine(ctx, xs, ys) {
  if (!xs.length) return;
  ctx.beginPath();
  ctx.moveTo(xs[0], ys[0]);
  for (let i = 1; i < xs.length; i++) {
    ctx.lineTo(xs[i], ys[i - 1]);
    ctx.lineTo(xs[i], ys[i]);
  }
  ctx.stroke();
}

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
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 240;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

function bindRatePan(ids) {
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

function bindRateZoom(ids) {
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

function placeRateTip(ev, tipEl, html, _wrapId) {
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
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 240;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

export {
  buildCtxPoints,
  drawLineChart,
  drawBars,
  drawAggBars,
  drawTimeline,
  onGanttSelect,
  fitGanttToSessions,
  renderCostLegend,
  setCostUnit,
  findRound,
  hideAllChartTips,
  hideChartTip,
  storedCtxChartH,
  bindCtxChartResize,
};
