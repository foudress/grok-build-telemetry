/** Context line chart + cost bars / legend */
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
 * Set content first so offsetWidth/Height work while still hidden.
 */
function showChartTip(el, html, leftPx, topPx) {
  if (!el) return;
  el.innerHTML = html;
  el.style.display = "";
  el.style.left = Math.max(0, leftPx) + "px";
  el.style.top = Math.max(0, topPx) + "px";
  el.classList.add("is-visible");
}

/** Write tip HTML and return measured size (works while visibility:hidden). */
function measureChartTip(el, html) {
  if (!el) return { tw: 160, th: 40 };
  el.innerHTML = html;
  el.style.display = "";
  return {
    tw: el.offsetWidth || 160,
    th: el.offsetHeight || 40,
  };
}

/** Hide DOM chart tooltip. */
function hideChartTip(el) {
  if (!el) return;
  el.classList.remove("is-visible");
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

/**
 * Context chart points: one point per LLM call only.
 * X labels: "R1" on first call of round, then "1","2","3"… reset at next RX.
 */
function buildCtxPoints(rounds) {
  const pts = [];
  (rounds || []).forEach(r => {
    const ri = r.index ?? "?";
    const steps = r.model_steps || [];
    // System baseline (bootstrap) before first LLM call of the session
    const sp = r.system_prompt;
    if (sp && (sp.tokens_in || sp.logical_tokens)) {
      const sysTok = Number(sp.tokens_in ?? sp.logical_tokens) || 0;
      if (sysTok > 0) {
        pts.push({
          label: "Sys",
          v: sysTok,
          kind: "system",
          round: ri,
          call: 0,
          tokens_in: sysTok,
          tokens_cached: 0,
          tokens_out: null,
          cost_in_usd: sp.cost_in_usd,
          cost_cached_usd: 0,
          cost_out_usd: null,
          estimate_usd: sp.estimate_usd ?? sp.cost_in_usd,
          tool_definitions_tokens: sp.tool_definitions_tokens,
          parts: sp.parts,
        });
      }
    }
    steps.forEach((s, i) => {
      const li = s.index ?? (i + 1);
      const v = s.context_start;
      if (v == null || Number.isNaN(v)) return;
      const se = s.estimate || {};
      // Display In = caused (tree); cache/out from call bill
      const tokIn = s.tokens_in ?? se.uncached_input_tokens ?? se.logical_uncached_tokens;
      const tokCache = s.tokens_cached ?? se.cached_read_tokens ?? se.logical_cached_tokens;
      const tokOut = s.tokens_out ?? se.output_tokens;
      const usdIn = s.cost_in_usd ?? se.cost_in_usd;
      const usdCache = s.cost_cached_usd ?? se.cost_cached_usd;
      const usdOut = s.cost_out_usd ?? se.cost_out_usd;
      const th = se.output_thought_tokens ?? s.composition?.thought_out;
      pts.push({
        // First call of round → "R3", subsequent → "1","2",…
        label: i === 0 ? `R${ri}` : String(li),
        v: v,
        kind: "call",
        round: ri,
        call: li,
        context_end: s.context_end,
        tokens_in: tokIn,
        tokens_cached: tokCache,
        tokens_out: tokOut,
        cost_in_usd: usdIn,
        cost_cached_usd: usdCache,
        cost_out_usd: usdOut,
        estimate_usd: s.estimate_usd ?? se.api_call_usd ?? se.estimate_usd,
        thought_tokens: th,
        thought_chars: s.thought_chars,
        system_tokens: i === 0 && sp ? (sp.tokens_in ?? sp.logical_tokens) : null,
      });
    });
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
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight || 200;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // One point per LLM call only
  let pts = buildCtxPoints(rounds);
  if (pts.length < 1 && series && series.length >= 1) {
    // fallback stream: sample evenly, no fake call labels
    const step = Math.max(1, Math.floor(series.length / 24));
    pts = series.filter((_, i) => i % step === 0 || i === series.length - 1)
      .map((p, i) => ({ label: "", v: p.v, kind: "stream" }));
  }

  const left = 48, right = 10, top = 22, bottom = 28;
  const plotW = w - left - right;
  const plotH = h - top - bottom;

  if (!pts || pts.length < 1) {
    drawChartEmpty(ctx, w, h, "No LLM calls yet");
    canvas._ctxPts = null;
    hideChartTip($("ctxTip"));
    return;
  }

  const vals = pts.map(p => p.v).filter(v => v != null && !Number.isNaN(v));
  const { min, max, step } = niceCtxYRange(vals);
  const yOf = (v) => top + plotH - ((v - min) / (max - min)) * plotH;
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

  // X label density: always show Sys / R* anchors; call numbers every 2, then every 5 if cramped
  const minLabelPx = 22;
  const avgPx = pts.length > 1 ? plotW / (pts.length - 1) : plotW;
  const callStride = avgPx >= minLabelPx * 2 ? 1 : (avgPx >= minLabelPx ? 2 : 5);
  let callInRound = 0;
  pts.forEach((p, i) => {
    const lab = String(p.label || "");
    const isSys = p.kind === "system";
    const isRoundAnchor = lab.startsWith("R");
    if (isSys || isRoundAnchor) callInRound = 0;
    else callInRound += 1;
    // Keep Sys + first call of round (R*); subsequent call nums: every callStride
    p._showX = isSys || isRoundAnchor || (callInRound % callStride === 0)
      || i === pts.length - 1;
  });

  // Points + X labels (Sys, R1, 1, 2, 3, R2, 1, …) — skip when cramped
  pts.forEach((p, i) => {
    const x = xOf(i), y = yOf(p.v);
    p._x = x; p._y = y;
    const isSys = p.kind === "system";
    ctx.fillStyle = isSys ? COST_COLORS.system : color;
    ctx.beginPath();
    ctx.arc(x, y, isSys ? 4 : 3.2, 0, Math.PI * 2);
    ctx.fill();
    if (isSys) {
      ctx.strokeStyle = "#e6edf3";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    if (p.label && p._showX) {
      const isAnchor = isSys || String(p.label).startsWith("R");
      ctx.fillStyle = isAnchor ? "#e6edf3" : CHART_AXIS.labelDim;
      ctx.font = isAnchor
        ? "10px system-ui, Segoe UI, sans-serif"
        : "9px system-ui, Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(p.label, x, h - 8);
      ctx.textAlign = "left";
    }
  });

  // Hit targets for tooltips
  canvas._ctxPts = pts;
  canvas._ctxGeom = { left, right, top, bottom, plotW, plotH, min, max, xOf, yOf };

  if (!canvas._ctxTipBound) {
    canvas._ctxTipBound = true;
    const tip = () => $("ctxTip");
    canvas.addEventListener("mousemove", (ev) => {
      const list = canvas._ctxPts;
      const t = tip();
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
      const lines = best.kind === "system"
        ? [
            `<b>System</b> · R${esc(best.round)} bootstrap`,
            `<span class="muted">Y context</span> <b>${fmtTokens(yVal)}</b>`,
            joinParts([partIn(best.tokens_in, best.cost_in_usd)].filter(Boolean)) || "—",
            best.tool_definitions_tokens
              ? `<span class="muted">tool defs + message ${fmtTokens(best.tool_definitions_tokens)}</span>`
              : "",
          ]
        : [
            `<b>${esc(best.label || "call")}</b> · R${esc(best.round)} call ${esc(best.call)}`,
            `<span class="muted">Y context</span> <b>${fmtTokens(yVal)}</b>` +
              (best.context_end != null ? ` <span class="muted">→ ${fmtTokens(best.context_end)}</span>` : ""),
          ];
      if (best.kind === "call") {
        if (best.system_tokens) {
          lines.push(`<span class="muted">System card</span> ${fmtTokens(best.system_tokens)}`);
        }
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
      } else if (best.kind === "system" && best.estimate_usd != null) {
        lines.push(totalPrice(best.estimate_usd));
      }
      t.style.whiteSpace = "normal";
      t.style.maxWidth = "280px";
      // Position near cursor (right-biased); measure while still CSS-hidden
      const html = lines.filter(Boolean).join("<br>");
      const { tw } = measureChartTip(t, html);
      let leftPx = mx + 12;
      if (leftPx + tw > rect.width - 4) leftPx = mx - tw - 8;
      showChartTip(t, html, Math.max(4, leftPx), Math.max(4, my - 10));
    });
    canvas.addEventListener("mouseleave", () => {
      hideChartTip(tip());
      canvas.style.cursor = "default";
    });
    canvas.addEventListener("click", (ev) => {
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
}

// Cost chart state: overview | detail attribution | drill into one turn's LLM calls
window.__costChart = window.__costChart || {
  detail: false,
  drillTurn: null, // turn index when drilled
  rounds: [],
  turns: [],
  /** "usd" | "tokens" — Cost per round Y-axis */
  unit: "usd",
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
  if (seg.legendKey) return seg.legendKey;
  if (seg.k === "tool" || seg.k === "toolreq")
    return seg.k + ":" + normToolCatName(seg.label);
  return seg.label || seg.k || "";
}

function isCostSegHidden(seg, hidden) {
  if (!hidden || !hidden.size || !seg) return false;
  const key = costSegKey(seg);
  if (key && hidden.has(key)) return true;
  if (hidden.has(seg.label)) return true;
  if (seg.k && hidden.has(seg.k)) return true;
  // simple-mode capitalized legend names
  const cap = {
    system: "System", in: "In", cached: "Cached", out: "Out",
    official: "official",
  };
  if (seg.k && cap[seg.k] && hidden.has(cap[seg.k])) return true;
  return false;
}

const COST_COLORS = {
  in: "#3ecf8e",
  cached: "#f0b429",
  out: "#f07178",
  system: "#8ab4f8",
  user: "#5ccfe6",       /* same blue as message */
  harness: "#3ecf8e",     /* same green as In / tool results */
  residual: "#8fd3a8",
  thought: "#c4a5e8",
  reasoning: "#a78bfa",
  toolreq: "#e88a90",    /* light red — tool request */
  message: "#5ccfe6",
  tool: "#3ecf8e",
  hook: "#e8a0bf",
  late: "#b48ead",
  llm: "#1a6b3c",        /* dark green — LLM Out→In */
  compact: "#f0a070",
  official: "#3d9cf0",
  sub: "#8b7cf7",
};
const SUB_SHADES = ["#8b7cf7", "#a78bfa", "#6d5ae6", "#c4b5fd"];

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

  // R1 peeled total = tree In + Cached + Out (matches hierarchy white $).
  // Prefer round.estimate_usd when already peeled; never scale to API+System.
  let tot;
  const partsSum = cin + ccache + cout;
  if (peelSystem || bd.round_total_peeled_system) {
    tot = (round && round.estimate_usd != null && bd.round_total_peeled_system)
      ? Number(round.estimate_usd)
      : (partsSum > 0 ? partsSum : Math.max(0, Number(t.estimate_usd || bd.total_usd || 0) - sys));
    // Do not rescale — stack must match hierarchy In/Cached/Out
  } else {
    tot = Number(
      (round && round.estimate_usd != null ? round.estimate_usd : null)
      ?? t.estimate_usd
      ?? bd.total_usd
      ?? bd.total
      ?? 0
    ) || 0;
    // Warm rounds: light align only when parts are empty or tiny drift
    let sum = partsSum;
    if (sum <= 0 && tot > 0) { cout = tot; sum = tot; }
    else if (tot > 0 && Math.abs(sum - tot) > 1e-6 && sum > 0) {
      const restTarget = Math.max(0, tot - cin);
      const rest = ccache + cout;
      if (rest > 0 && restTarget > 0) {
        const s = restTarget / rest;
        ccache *= s;
        cout *= s;
      } else {
        const s = tot / sum;
        cin *= s; ccache *= s; cout *= s;
      }
    }
  }
  const sum = cin + ccache + cout;
  if (peelSystem || bd.round_total_peeled_system) {
    tot = sum > 0 ? sum : tot;
  }

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
  const thoughtTokN = Number(bd.llm_thought_summary_tokens) || 0;
  const reasonTokN = Number(bd.llm_reasoning_encrypted_tokens)
    || Number(bd.llm_reasoning_tokens) || 0;
  const emitTokN = Number(bd.llm_out_to_harness_tokens) || 0;
  const msgTokN = Number(bd.llm_out_to_user_tokens) || 0;

  const segs = [];

  if (detail) {
    const harness = Number(bd.harness_in_usd) || 0;
    // Thought = pure Out (summary); Reasoning enc separate; tools from reason budget
    const thought = Number(bd.llm_thought_summary_usd) || 0;
    const reasoningEnc = Number(bd.llm_reasoning_encrypted_usd)
      || (Number(bd.llm_reasoning_usd) || 0);
    const emit = Number(bd.llm_out_to_harness_usd) || 0;
    const msg = Number(bd.llm_out_to_user_usd) || 0;
    // System is a separate chart bar when peelSystem — omit from R1 stack
    if (!peelSystem && sys > 0) {
      segs.push({
        k: "system", label: "System", v: sys, tok: sysTok || 0,
        color: COST_COLORS.system, tokens: sysTok || null,
      });
    }
    let inParts = [
      { k: "user", label: "user", v: user, tok: userTokN, color: COST_COLORS.user },
      { k: "harness", label: "harness", v: harness, tok: harnessTokN, color: COST_COLORS.harness },
    ];
    const inSum = inParts.reduce((a, p) => a + p.v, 0);
    const inTokSum = inParts.reduce((a, p) => a + (p.tok || 0), 0);
    if (cin > 0 || tinTok > 0) {
      if (inSum > 0) {
        const scale = cin > 0 ? cin / inSum : 1;
        const tscale = inTokSum > 0 && tinTok > 0 ? tinTok / inTokSum : 1;
        inParts = inParts.map(p => ({
          ...p,
          v: p.v * scale,
          tok: (p.tok || 0) * tscale,
        }));
        const used = inParts.reduce((a, p) => a + p.v, 0);
        const usedT = inParts.reduce((a, p) => a + (p.tok || 0), 0);
        if (cin - used > 1e-9 || tinTok - usedT > 1)
          inParts.push({
            k: "residual", label: "in residual",
            v: Math.max(0, cin - used),
            tok: Math.max(0, tinTok - usedT),
            color: COST_COLORS.residual,
          });
      } else {
        inParts = [{ k: "in", label: "in", v: cin, tok: tinTok, color: COST_COLORS.in }];
      }
    } else {
      inParts = [];
    }
    let outParts = [
      { k: "thought", label: "thought", v: thought, tok: thoughtTokN, color: COST_COLORS.thought },
      { k: "reasoning", label: "reasoning", v: reasoningEnc, tok: reasonTokN, color: COST_COLORS.reasoning },
      { k: "toolreq", label: "tool req", v: emit, tok: emitTokN, color: COST_COLORS.toolreq },
      { k: "message", label: "message", v: msg, tok: msgTokN, color: COST_COLORS.message },
    ];
    const outSum = outParts.reduce((a, p) => a + p.v, 0);
    const outTokSum = outParts.reduce((a, p) => a + (p.tok || 0), 0);
    if (cout > 0 || toutTok > 0) {
      if (outSum > 0) {
        const scale = cout > 0 ? cout / outSum : 1;
        const tscale = outTokSum > 0 && toutTok > 0 ? toutTok / outTokSum : 1;
        outParts = outParts.map(p => ({
          ...p,
          v: p.v * scale,
          tok: (p.tok || 0) * tscale,
        }));
      } else {
        outParts = [{ k: "out", label: "out", v: cout, tok: toutTok, color: COST_COLORS.out }];
      }
    } else outParts = [];
    segs.push(...inParts.filter(p => p.v > 0 || p.tok > 0));
    if (ccache > 0 || tcacheTok > 0)
      segs.push({ k: "cached", label: "cached", v: ccache, tok: tcacheTok, color: COST_COLORS.cached });
    segs.push(...outParts.filter(p => p.v > 0 || p.tok > 0));
  } else {
    // Simple stack: In / Cached / Out only (User folded into global In).
    // System only inside this bar when NOT a separate chart bar.
    if (!peelSystem && sys > 0) {
      segs.push({ k: "system", label: "System", v: sys, tok: sysTok || 0, color: COST_COLORS.system });
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

function systemCostBar(round) {
  const sp = round && round.system_prompt;
  const bd = (round && round.breakdown) || {};
  const sys = Number(bd.system_in_usd) || Number(sp && sp.cost_in_usd) || 0;
  const sysTok = Number(bd.system_in_tokens) || Number(sp && (sp.tokens_in ?? sp.logical_tokens)) || 0;
  if (sys <= 0 && sysTok <= 0) return null;
  return {
    segs: [{
      k: "system",
      label: "System",
      v: sys || 0,
      tok: sysTok || 0,
      color: COST_COLORS.system,
      tokens: sysTok || null,
    }],
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

function callCostParts(step, callIndex, round) {
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
  // First LLM call: User paid@start only. System stays on the System card.
  const isFirstCall = Number(callIndex) === 1;
  const up = round && round.user_prompt;
  if (isFirstCall && up && (up.tokens_in || up.uncached_est || up.cost_in_usd)) {
    const userU = Number(up.cost_in_usd) || 0;
    const userT = Number(up.tokens_in ?? up.uncached_est) || 0;
    if (userU > 0 || userT > 0) {
      bottomExtra.push({
        k: "user",
        label: "User",
        v: userU || 0,
        tok: userT || 0,
        color: COST_COLORS.user,
        tokens: userT || null,
      });
    }
  }
  // Caused In (tree) — LLM Out→In + tools split by name (same rules as toolreqs)
  const causedIn = Number(step.cost_in_usd ?? se.cost_in_usd) || 0;
  const causedInTok = Number(step.uncached_input_tokens ?? se.uncached_input_tokens) || 0;
  let harnessUsd = 0;
  let harnessTok = 0;
  const toolAgg = new Map(); // normName → {v, tok, n}
  let llmOutUsd = 0;
  let llmOutTok = 0;
  for (const ch of step.children || []) {
    if (ch.kind !== "phase_harness") continue;
    for (const sub of ch.children || []) {
      if (sub.kind === "late_context") continue; // redistributed into tools
      if (sub.kind === "hook") continue; // Hook not on Cost per Round graph
      const u = Number(sub.cost_in_usd) || 0;
      const tk = Number(sub.tokens_in || sub.context_delta) || 0;
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

  if (causedIn > 0 || causedInTok > 0) {
    if (harnessUsd > 0 && harnessUsd <= causedIn + 1e-9) {
      const scale = harnessUsd > 0 ? Math.min(1, causedIn / harnessUsd) : 1;
      const tscale = harnessTok > 0 && causedInTok > 0
        ? Math.min(1, causedInTok / harnessTok) : 1;
      toolSegs.forEach(s => { s.v *= scale; s.tok = (s.tok || 0) * tscale; });
      llmOutSegs.forEach(s => { s.v *= scale; s.tok = (s.tok || 0) * tscale; });
      const used =
        toolSegs.reduce((a, s) => a + s.v, 0) +
        llmOutSegs.reduce((a, s) => a + s.v, 0);
      const usedT =
        toolSegs.reduce((a, s) => a + (s.tok || 0), 0) +
        llmOutSegs.reduce((a, s) => a + (s.tok || 0), 0);
      if (causedIn - used > 1e-9 || causedInTok - usedT > 1)
        bottomExtra.push({
          k: "residual",
          label: "in residual",
          legendKey: "in residual",
          v: Math.max(0, causedIn - used),
          tok: Math.max(0, causedInTok - usedT),
          color: COST_COLORS.residual,
        });
    } else if (!(toolSegs.length || llmOutSegs.length)) {
      bottomExtra.push({
        k: "in", label: "in", legendKey: "in",
        v: causedIn, tok: causedInTok, color: COST_COLORS.in,
      });
    }
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
  if (!(thoughtU || encU || msgU || reqAgg.size || thoughtT || encT || msgT)) {
    const cout = Number(step.cost_out_usd ?? se.cost_out_usd) || 0;
    const coutT = Number(step.output_tokens ?? se.output_tokens) || 0;
    if (cout > 0 || coutT > 0)
      thoughtSegs.push({
        k: "out", label: "out", legendKey: "out",
        v: cout, tok: coutT, color: COST_COLORS.out,
      });
  } else {
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
  const cin = Number(sa.cost_in_usd) || 0;
  const ccache = Number(sa.cost_cached_usd) || 0;
  const cout = Number(sa.cost_out_usd) || 0;
  const official = sa.official_usd != null ? Number(sa.official_usd) : null;
  const est = Number(sa.estimate_usd) || (cin + ccache + cout);
  const total = (official != null && official > 0) ? official : est;
  const totalTok = Math.max(0, inTok) + Math.max(0, cacheTok) + Math.max(0, outTok);
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
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const bar = subagentCostBar(sa);
      if (bar) out.push(bar);
    }
  }
  return out;
}

/** Rounds overview: fold each child bill into that turn's stack (not its own X slot). */
function attachSubagentSegsToTurn(turnBar, round) {
  if (!turnBar) return turnBar;
  const kids = collectRoundSubagentBars(round);
  if (!kids.length) return turnBar;
  if (!turnBar.segs) turnBar.segs = [];
  kids.forEach((sb, i) => {
    const n = String(sb.label || "").replace(/^S/, "") || String(i + 1);
    turnBar.segs.push({
      k: "sub",
      label: "S" + n,
      legendKey: "sub:" + (sb.session_id || n),
      v: sb.total || 0,
      tok: sb.total_tok || 0,
      color: SUB_SHADES[i % SUB_SHADES.length],
      session_id: sb.session_id,
      title: sb.title,
    });
    turnBar.total = (Number(turnBar.total) || 0) + (sb.total || 0);
    turnBar.total_tok = (Number(turnBar.total_tok) || 0) + (sb.total_tok || 0);
  });
  turnBar.has_subagents = true;
  return turnBar;
}

function compactCostBar(c, compactIndex) {
  if (!c || c.kind !== "compaction") return null;
  const segs = [];
  // Same order as Round stack: In / Cached / (Out n/a) — reload folds into In
  const preUnc = Number(c.pre_read_uncached_usd) || 0;
  const preCache = Number(c.pre_read_cached_usd) || Number(c.pre_read_usd) || 0;
  const reload = Number(c.deferred_reload_usd) || 0;
  const preUncT = Number(c.pre_read_uncached_tokens) || 0;
  const preCacheT = Number(c.pre_read_cached_tokens) || Number(c.pre_read_tokens) || Number(c.tokens_before) || 0;
  const reloadT = Number(c.deferred_reload_tokens) || 0;
  const inU = preUnc + reload;
  const inT = preUncT + reloadT;
  if (inU > 0 || inT > 0)
    segs.push({ k: "in", label: "In", v: inU, tok: inT, color: COST_COLORS.in });
  if (preCache > 0 || preCacheT > 0)
    segs.push({ k: "cached", label: "Cached", v: preCache, tok: preCacheT, color: COST_COLORS.cached });
  const total = Number(c.cost_usd) != null && Number(c.cost_usd) > 0
    ? Number(c.cost_usd)
    : segs.reduce((a, s) => a + s.v, 0);
  const totalTok = segs.reduce((a, s) => a + (Number(s.tok) || 0), 0);
  if (!(total > 0) && !(totalTok > 0) && !segs.length) {
    const before = Number(c.tokens_before) || 0;
    if (before <= 0) return null;
    segs.push({ k: "cached", label: "pre-read", v: 0.000001, tok: before, color: COST_COLORS.cached });
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

const COST_Y_LEFT = 56;
const PLOT_PAD_L = 6;
const COST_CHART_H = 240;
const COST_MIN_SLOT = 36;
const COST_MAX_SLOT = 96;

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

function slotRange(viewW, barCount) {
  const nSlots = Math.max(barCount, 5);
  const plotAvail = Math.max(10, viewW - PLOT_PAD_L - 12);
  const minSlot = plotAvail / nSlots;
  return { nSlots, minSlot, maxSlot: COST_MAX_SLOT, plotAvail };
}

function layoutCostCanvas(canvas, barCount, { forceFit } = {}) {
  const yAxis = $("costYAxis");
  if (yAxis && barCount > 0) yAxis.hidden = false;
  const scroller = $("costChartScroll");
  let viewW = (scroller && scroller.clientWidth) || 0;
  if (!viewW) viewW = (canvas.parentElement && canvas.parentElement.clientWidth) || 600;
  let h = canvas.clientHeight;
  if (!h || h < 80) h = COST_CHART_H;
  const store = zoomStore();
  const keepScroll = store._costUserZoom || store._costStickEnd === false;
  const prevScroll = scroller
    ? (store._scrollLeft != null ? store._scrollLeft : scroller.scrollLeft)
    : 0;
  const { nSlots, minSlot, maxSlot } = slotRange(viewW, barCount);
  let slot = store.slotPx;
  if (!(slot > 0)) slot = forceFit ? minSlot : COST_MIN_SLOT;
  slot = Math.min(maxSlot, Math.max(minSlot, slot));
  store.slotPx = slot;
  const w = PLOT_PAD_L + 12 + nSlots * slot;
  const overflow = w > viewW + 1;
  if (overflow) canvas.style.width = w + "px";
  else canvas.style.width = "100%";
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
    if (ag) drawAggBars(canvas, ag.buckets, ag);
    return;
  }
  const st = window.__costChart;
  if (st) drawBars(canvas, st.turns, st.rounds);
}

function bindCostZoom() {
  const wrap = $("costChartWrap");
  if (!wrap || wrap._zoomBound) return;
  wrap._zoomBound = true;
  wrap.addEventListener("wheel", (ev) => {
    if (ev.shiftKey) return;
    if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;
    const canvas = $("costChart");
    const scroller = $("costChartScroll");
    if (!canvas) return;
    ev.preventDefault();
    const barCount = canvas._zoomBars || 1;
    const viewW = (scroller && scroller.clientWidth) || wrap.clientWidth || 600;
    const { nSlots, minSlot, maxSlot } = slotRange(viewW, barCount);
    const store = zoomStore();
    const slot0 = store.slotPx > 0 ? store.slotPx : COST_MIN_SLOT;
    const notches = ev.deltaY / (ev.deltaMode === 1 ? 3 : 80);
    let next = slot0 * Math.pow(1.16, -notches);
    next = Math.min(maxSlot, Math.max(minSlot, next));
    if (next <= minSlot * 1.03) next = minSlot;
    if (Math.abs(next - slot0) < 0.05) return;
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

function drawBars(canvas, turns, rounds) {
  const st = window.__costChart;
  st.turns = turns || [];
  st.rounds = rounds || [];
  const tip = $("costTip");
  const backBtn = $("costDrillBack");
  const modeLabel = $("costModeLabel");
  if (backBtn) backBtn.hidden = st.drillTurn == null;
  if (modeLabel) {
    const u = st.unit === "tokens" ? "Tok" : "$";
    modeLabel.textContent = st.drillTurn != null
      ? `Drill · Round ${st.drillTurn} · ${u}`
      : (st.detail ? `Detailed · ${u}` : `In / Cached / Out · ${u}`);
  }

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
    steps.forEach((s, i) => {
      const b = callCostParts(s, s.index ?? i + 1, round);
      b.turnIndex = st.drillTurn;
      bars.push(b);
      for (const sa of s.subagents_after || []) {
        const sb = subagentCostBar(sa);
        if (sb) {
          sb.turnIndex = st.drillTurn;
          bars.push(sb);
        }
      }
    });
  } else {
    const slice = turns || [];
    // Separate System bar before Round 1 when bootstrap system exists
    const r1 = findRound(st.rounds, 1)
      || (st.rounds || []).find(r => r && r.system_prompt)
      || null;
    const hasR1InSlice = slice.some(t => Number(t.index) === 1)
      || (r1 && slice.length && Number(slice[0].index) === Number(r1.index));
    const sysBar = (r1 && hasR1InSlice) ? systemCostBar(r1) : null;
    bars = [];
    if (sysBar) bars.push(sysBar);

    // Compaction counter is session-global (C1, C2, … chronological)
    const sliceIdx = new Set(slice.map(t => Number(t.index)));
    let compactN = 0;
    const minSlice = slice.length
      ? Math.min(...slice.map(t => Number(t.index)))
      : Infinity;
    for (const r of (st.rounds || [])) {
      if (!r || Number(r.index) >= minSlice) break;
      if (r.compact_after && r.compact_after.kind === "compaction") compactN += 1;
    }
    for (const t of slice) {
      const round = findRound(st.rounds, t.index);
      const peelSystem = !!(sysBar && Number(t.index) === Number(r1 && r1.index || 1));
      // Compact *before* this round when previous round is outside the slice
      // (same object as previous.compact_after — avoid double-draw / double-count).
      const prevInSlice = sliceIdx.has(Number(t.index) - 1);
      if (!prevInSlice && round && round.compact_before && round.compact_before.kind === "compaction") {
        const prevRound = findRound(st.rounds, Number(t.index) - 1);
        if (!(prevRound && prevRound.compact_after && prevRound.compact_after.kind === "compaction")) {
          compactN += 1; // not already counted in the pre-slice loop
        }
        const cbBefore = compactCostBar(round.compact_before, compactN);
        if (cbBefore) bars.push(cbBefore);
      }
      bars.push(attachSubagentSegsToTurn(
        turnCostParts(t, round, { detail: st.detail, peelSystem }),
        round
      ));
      // Compact after this round (between R[n] and R[n+1])
      const c = round && round.compact_after;
      if (c && c.kind === "compaction") {
        compactN += 1;
        const cb = compactCostBar(c, compactN);
        if (cb) bars.push(cb);
      }
    }
  }

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

  if (!(st.hiddenLegend instanceof Set)) st.hiddenLegend = new Set();
  const unit = st.unit === "tokens" ? "tokens" : "usd";
  const isDrill = st.drillTurn != null;

  // Rounds ↔ drill: drop every hidden chip (Cached stay-off was leaking).
  if (isDrill !== !!st._wasDrill) {
    st.hiddenLegend = new Set();
  }
  st._wasDrill = isDrill;

  const hidden = st.hiddenLegend;
  if (hidden.has("sub") || hidden.has("Sub")) {
    bars = bars.filter((b) => b.kind !== "subagent");
  }

  // Visible stack per bar (legend click hides categories for granularity)
  const visStacks = bars.map(p => {
    const raw = p.segs && p.segs.length
      ? p.segs
      : [{ k: "out", label: "—", v: p.total || 0, tok: p.total_tok || 0, color: COST_COLORS.out }];
    return raw.filter(s => {
      const m = costSegMetric(s, unit);
      return m > 0 && !isCostSegHidden(s, hidden);
    });
  });
  let rawMax = 0;
  bars.forEach((p, i) => {
    const visTot = visStacks[i].reduce((a, s) => a + costSegMetric(s, unit), 0);
    const off = (!hidden.has("official") && unit === "usd" && p.official != null)
      ? p.official : 0;
    rawMax = Math.max(rawMax, visTot, off, 0);
  });
  if (rawMax <= 0) rawMax = unit === "tokens" ? 1000 : 0.0001;
  const { min: yMin, max, step: yStep } = niceCostYMaxForUnit(rawMax, unit);

  // Legend lives in HTML below the canvas (wrap + scroll) — free plot top
  const left = PLOT_PAD_L, right = 12, top = 12, bottom = 28;
  const plotW = w - left - right;
  const plotH = h - top - bottom;
  // Pad X to ≥5 slots so 1–2 early bars stay narrow; empty slots left blank
  const MIN_COST_SLOTS = 5;
  const nSlots = Math.max(bars.length, MIN_COST_SLOTS);
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
        legendMap.set(key, { label: seg.label || key, color: seg.color, k: seg.k });
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
    ctx.fillStyle = p.kind === "subagent" ? COST_COLORS.sub : CHART_AXIS.labelDim;
    ctx.font = "10px system-ui, Segoe UI, sans-serif";
    ctx.textAlign = "center";
    const xEvery = groupW < 8 ? 8 : groupW < 14 ? 4 : groupW < 22 ? 2 : 1;
    if (i % xEvery === 0)
      ctx.fillText(p.label || String(p.index), x0 + bw / 2, h - 8);
    ctx.textAlign = "left";
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
  if (st.drillTurn == null && !st.detail) {
    legendItems = [
      ["System", { label: "System", color: COST_COLORS.system }],
      ["In", { label: "In", color: COST_COLORS.in }],
      ["Cached", { label: "Cached", color: COST_COLORS.cached }],
      ["Out", { label: "Out", color: COST_COLORS.out }],
    ];
    if (bars.some((b) => b.kind === "subagent" || (b.segs || []).some((s) => s.k === "sub")))
      legendItems.push(["sub", { label: "Sub", color: COST_COLORS.sub, k: "sub" }]);
    if (unit === "usd")
      legendItems.push(["official", { label: "official", color: COST_COLORS.official }]);
  } else if (st.drillTurn == null && unit === "usd") {
    if (!legendItems.some(([key]) => key === "official"))
      legendItems.unshift(["official", { label: "official", color: COST_COLORS.official }]);
  }
  // Re-show hidden chips only if that category exists in *current* legendMap
  for (const name of hidden) {
    if (legendMap.has(name)) continue; // already from segs
    if (!legendItems.some(([key]) => key === name)) {
      // only if it was a fixed overview key still relevant
      const fixedOk = ["System", "In", "Cached", "Out", "official", "in", "cached", "out"].includes(name);
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
  // Order: fixed cats → toolreqs (alpha) → LLM Out→In → tools (alpha) → rest
  const rankKey = (key, meta) => {
    const lab = (meta && meta.label) || key;
    const k = (meta && meta.k) || "";
    const fixed = [
      "System", "User", "user", "In", "in", "Cached", "cached",
      "thought", "reasoning", "message", "LLM Out→In", "llm_out_in",
      "Out", "out", "Sub", "sub", "official", "in residual", "reload", "pre-read", "pre-read In",
    ];
    const fi = fixed.indexOf(key) >= 0 ? fixed.indexOf(key) : fixed.indexOf(lab);
    if (fi >= 0) return fi;
    if (k === "toolreq" || String(key).startsWith("toolreq:")) return 50 + String(lab).localeCompare("");
    if (k === "llm_out_in" || key === "llm_out_in" || lab === "LLM Out→In") return 80;
    if (k === "tool" || String(key).startsWith("tool:")) return 90 + String(lab).localeCompare("");
    if (k === "hook" || String(key).startsWith("hook:")) return 999; // hidden from chart
    return 100 + String(lab).localeCompare("");
  };
  legendItems.sort((a, b) => {
    const ra = rankKey(a[0], a[1]), rb = rankKey(b[0], b[1]);
    if (ra !== rb) return ra - rb;
    return String((a[1] && a[1].label) || a[0]).localeCompare(String((b[1] && b[1].label) || b[0]));
  });
  renderCostLegend(legendItems, hidden);

  canvas._barHit = hit;
  canvas._legendHit = []; // legend is HTML now

  if (!canvas._tipBound) {
    canvas._tipBound = true;
    canvas.addEventListener("mousemove", (ev) => {
      const tipEl = $("costTip");
      if (!tipEl || !canvas._barHit) return;
      const rect = canvas.getBoundingClientRect();
      const wrap = $("costChartWrap");
      const wrapRect = wrap ? wrap.getBoundingClientRect() : rect;
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const found = canvas._barHit.find(b => mx >= b.x0 && mx <= b.x1 && my >= b.y0 && my <= b.y1);
      if (!found) {
        canvas.style.cursor = "default";
        hideChartTip(tipEl);
        return;
      }
      canvas.style.cursor = (found.p.kind === "turn" || found.p.kind === "subagent")
        ? "pointer" : "default";
      const p = found.p;
      tipEl.style.whiteSpace = "normal";
      tipEl.style.maxWidth = "300px";
      const head = p.kind === "call"
        ? ("LLM call " + p.index)
        : (p.kind === "system" ? "System"
          : (p.kind === "compact" ? ("Compact " + p.label)
            : (p.kind === "subagent"
              ? ("Sub Agent " + String(p.label || "").replace(/^S/, "") + (p.title ? " · " + p.title : ""))
              : ("Round " + p.index))));
      const lines = [`<b>${esc(head)}</b>`];
      if (p.kind === "compact" && (p.tokens_before != null || p.tokens_after != null)) {
        lines.push(
          `<span class="muted">${fmtTokens(p.tokens_before)}${AR}${fmtTokens(p.tokens_after)}</span>`
        );
      }
      const u = p._unit || window.__costChart.unit || "usd";
      if (p.kind === "subagent") {
        if (p.uncached_tokens || p.in)
          lines.push(`<span style="color:${COST_COLORS.in}">●</span> In ${fmtTokens(p.uncached_tokens)} · ${fmtUsd(p.in)}`);
        if (p.cached_tokens || p.cached)
          lines.push(`<span style="color:${COST_COLORS.cached}">●</span> Cached ${fmtTokens(p.cached_tokens)} · ${fmtUsd(p.cached)}`);
        if (p.out_tokens || p.out)
          lines.push(`<span style="color:${COST_COLORS.out}">●</span> Out ${fmtTokens(p.out_tokens)} · ${fmtUsd(p.out)}`);
      } else {
        (p.segs || []).forEach(s => {
          const mv = costSegMetric(s, u);
          if (!(mv > 0)) return;
          const cnt = s.n > 1 ? ` ×${s.n}` : "";
          const extra = s.title ? " · " + s.title : "";
          lines.push(
            `<span style="color:${s.color}">●</span> ${esc(s.label)}${esc(extra)}${cnt} ${fmtCostAxis(mv, u)}`
          );
        });
      }
      lines.push(`<b>→ ${fmtCostAxis(p.total || 0, u)}</b>`);
      if (u === "usd" && p.official != null && !window.__costChart.hiddenLegend?.has("official"))
        lines.push(`<span class="muted">official ${fmtUsd(p.official)}</span>`);
      if (p.kind === "turn") lines.push(`<span class="muted">click: tree + drill calls</span>`);
      if (p.kind === "subagent") lines.push(`<span class="muted">click: open this sub-agent tab</span>`);
      const html = lines.join("<br>");
      // Tooltip top-right of cursor (above + to the right) — keep existing placement
      const { tw, th } = measureChartTip(tipEl, html);
      let leftPx = ev.clientX - wrapRect.left + 12;
      if (leftPx + tw > wrapRect.width - 4) leftPx = Math.max(4, ev.clientX - wrapRect.left - tw - 8);
      let topPx = ev.clientY - wrapRect.top - th - 8;
      if (topPx < 4) topPx = Math.min(wrapRect.height - th - 4, ev.clientY - wrapRect.top + 12);
      showChartTip(tipEl, html, leftPx, Math.max(4, topPx));
    });
    canvas.addEventListener("mouseleave", () => {
      hideChartTip($("costTip"));
      canvas.style.cursor = "default";
    });
    canvas.addEventListener("click", (ev) => {
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
      if (found.p.kind === "turn") {
        window.__costChart.drillTurn = found.p.index;
        drawBars(canvas, window.__costChart.turns, window.__costChart.rounds);
        focusRound(found.p.index);
      }
    });
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
    // Distinguish toolreq vs tool when same display name (color already differs)
    let show = label;
    if (String(key).startsWith("toolreq:") && !/req$/i.test(show))
      show = show; // orange swatch = request; keep clean name
    else if (String(key).startsWith("tool:") && !/in$/i.test(show))
      show = show; // green swatch = result
    const hid = hidden.has(key) || hidden.has(label);
    return `<button type="button" class="leg-chip${hid ? " hid" : ""}" data-leg="${esc(key)}" aria-pressed="${hid ? "false" : "true"}" title="${hid ? "Show" : "Hide"} ${esc(show)}">
      <span class="leg-sw" style="background:${color}"></span>${esc(show)}
    </button>`;
  }).join("");
  el.innerHTML = chips;
  if (!el._bound) {
    el._bound = true;
    el.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".leg-chip");
      if (!btn) return;
      const name = btn.getAttribute("data-leg");
      if (!name) return;
      const canvas = $("costChart");
      if (document.body.classList.contains("scope-period")) {
        const ag = window.__aggChart || {};
        if (!(ag.hiddenLegend instanceof Set)) ag.hiddenLegend = new Set();
        if (ag.hiddenLegend.has(name)) ag.hiddenLegend.delete(name);
        else ag.hiddenLegend.add(name);
        window.__aggChart = ag;
        if (canvas) drawAggBars(canvas, ag.buckets, ag);
        return;
      }
      const st = window.__costChart;
      if (!(st.hiddenLegend instanceof Set)) st.hiddenLegend = new Set();
      if (st.hiddenLegend.has(name)) st.hiddenLegend.delete(name);
      else st.hiddenLegend.add(name);
      if (canvas) drawBars(canvas, st.turns, st.rounds);
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
  const aggUsd = $("aggUnitUsd");
  const aggTok = $("aggUnitTok");
  if (aggUsd) {
    aggUsd.classList.toggle("active", isUsd);
    aggUsd.setAttribute("aria-pressed", isUsd ? "true" : "false");
  }
  if (aggTok) {
    aggTok.classList.toggle("active", !isUsd);
    aggTok.setAttribute("aria-pressed", !isUsd ? "true" : "false");
  }
  if (document.body.classList.contains("scope-period")) {
    const ag = window.__aggChart;
    if (ag) drawAggBars($("costChart"), ag.buckets, ag);
    return;
  }
  const st = window.__costChart;
  if (st) drawBars($("costChart"), st.turns, st.rounds);
}

function _cumBuckets(buckets) {
  const out = [];
  let tin = 0, tc = 0, tout = 0, ci = 0, cc = 0, co = 0, tot = 0;
  for (const b of buckets || []) {
    tin += Number(b.tokens_in) || 0;
    tc += Number(b.tokens_cached) || 0;
    tout += Number(b.tokens_out) || 0;
    ci += Number(b.cost_in_usd) || 0;
    cc += Number(b.cost_cached_usd) || 0;
    co += Number(b.cost_out_usd) || 0;
    tot += Number(b.official_usd) || 0;
    out.push({
      ...b,
      tokens_in: tin,
      tokens_cached: tc,
      tokens_out: tout,
      tokens_all: tin + tc + tout,
      cost_in_usd: ci,
      cost_cached_usd: cc,
      cost_out_usd: co,
      official_usd: tot,
    });
  }
  return out;
}

/** Stacked In / Cached / Out histogram for period views. */
function drawAggBars(canvas, buckets, opts) {
  if (!canvas) return;
  const unit = (opts && opts.unit) || (window.__costChart && window.__costChart.unit) || "usd";
  const cumulative = !!(opts && opts.cumulative);
  const prev = window.__aggChart || {};
  const hidden = prev.hiddenLegend instanceof Set ? prev.hiddenLegend : new Set();
  const src = cumulative ? _cumBuckets(buckets) : (buckets || []);
  window.__aggChart = {
    buckets: buckets || [],
    unit,
    cumulative,
    hiddenLegend: hidden,
    slotPx: prev.slotPx,
    _costUserZoom: prev._costUserZoom,
    _costStickEnd: prev._costStickEnd,
    _scrollLeft: prev._scrollLeft,
  };

  const tip = $("costTip");
  const laid = layoutCostCanvas(canvas, Math.max(src.length, 1), {
    forceFit: !prev._costUserZoom && src.length <= 36,
  });
  const { w, h, ctx } = laid;
  const padL = PLOT_PAD_L;
  const padR = 12;
  const padT = 16;
  const padB = 28;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  const legendItems = [
    ["In", { label: "In", color: COST_COLORS.in, k: "in" }],
    ["Cached", { label: "Cached", color: COST_COLORS.cached, k: "cached" }],
    ["Out", { label: "Out", color: COST_COLORS.out, k: "out" }],
  ];
  renderCostLegend(legendItems, hidden);

  if (!src.length) {
    const yAxis = $("costYAxis");
    if (yAxis) yAxis.hidden = true;
    drawChartEmpty(ctx, w, h, "No usage in this period");
    hideChartTip(tip);
    canvas.onmousemove = null;
    canvas.onmouseleave = null;
    return;
  }

  const segsOf = (b) => {
    if (unit === "tokens") {
      return [
        { k: "in", label: "In", legendKey: "In", v: Number(b.tokens_in) || 0, color: COST_COLORS.in },
        { k: "cached", label: "Cached", legendKey: "Cached", v: Number(b.tokens_cached) || 0, color: COST_COLORS.cached },
        { k: "out", label: "Out", legendKey: "Out", v: Number(b.tokens_out) || 0, color: COST_COLORS.out },
      ];
    }
    return [
      { k: "in", label: "In", legendKey: "In", v: Number(b.cost_in_usd) || 0, color: COST_COLORS.in },
      { k: "cached", label: "Cached", legendKey: "Cached", v: Number(b.cost_cached_usd) || 0, color: COST_COLORS.cached },
      { k: "out", label: "Out", legendKey: "Out", v: Number(b.cost_out_usd) || 0, color: COST_COLORS.out },
    ];
  };
  const visSegs = (b) => segsOf(b).filter((s) => {
    if (!(s.v > 0)) return false;
    return !hidden.has(s.legendKey) && !hidden.has(s.label) && !hidden.has(s.k);
  });

  let yMax = 0;
  const bars = src.map((b) => {
    const segs = visSegs(b);
    const total = segs.reduce((a, s) => a + s.v, 0);
    if (total > yMax) yMax = total;
    return { b, segs, total };
  });
  if (yMax <= 0) yMax = unit === "tokens" ? 1000 : 0.01;
  const y = niceCostYMaxForUnit(yMax, unit);
  const max = y.max || 1;

  ctx.strokeStyle = CHART_AXIS.grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = CHART_AXIS.labelDim;
  ctx.font = "10px system-ui, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  eachCostYTick(0, max, y.step, unit, (v) => {
    const yy = padT + plotH - (v / max) * plotH;
    ctx.beginPath();
    ctx.moveTo(0, yy);
    ctx.lineTo(w - padR, yy);
    ctx.stroke();
  });
  drawCostYOverlay({ min: 0, max, step: y.step, unit, top: padT, bottom: padB, h });

  const n = bars.length;
  const nSlots = Math.max(n, 1);
  const groupW = plotW / nSlots;
  const gap = Math.max(0, Math.min(6, groupW * 0.18));
  const bw = Math.max(1, groupW - gap);
  const hit = [];
  const labelEvery = groupW < 8 ? 8 : groupW < 14 ? 4 : groupW < 22 ? 2 : 1;

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
    ctx.fillStyle = CHART_AXIS.label;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const lab = bar.b.label || "";
    const show = i % labelEvery === 0;
    if (show) {
      ctx.save();
      if (n > 12 && lab.length > 6) {
        ctx.translate(x + bw / 2, h - padB + 4);
        ctx.rotate(-0.45);
        ctx.textAlign = "right";
        ctx.fillText(lab, 0, 0);
        ctx.restore();
      } else {
        ctx.fillText(lab, x + bw / 2, h - padB + 6);
      }
    }
  });

  canvas.onmousemove = (ev) => {
    const r = canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left;
    const my = ev.clientY - r.top;
    const found = hit.find((h0) => mx >= h0.x && mx <= h0.x + h0.w && my >= h0.y && my <= h0.y + h0.h);
    if (!found) {
      hideChartTip(tip);
      return;
    }
    const b = found.bar.b;
    const html = `<b>${esc(b.label || "")}</b><br>
      <span class="tok-in">In</span> ${fmtTokens(b.tokens_in)} · ${fmtUsd(b.cost_in_usd)}<br>
      <span class="tok-cached">Cached</span> ${fmtTokens(b.tokens_cached)} · ${fmtUsd(b.cost_cached_usd)}<br>
      <span class="tok-out">Out</span> ${fmtTokens(b.tokens_out)} · ${fmtUsd(b.cost_out_usd)}<br>
      ${fmtUsd(b.official_usd)}`;
    const tw = measureChartTip(tip, html);
    let left = ev.clientX - r.left + 12;
    let top = ev.clientY - r.top - tw.th - 8;
    if (left + tw.tw > w) left = w - tw.tw - 4;
    if (top < 0) top = ev.clientY - r.top + 16;
    showChartTip(tip, html, left, top);
  };
  canvas.onmouseleave = () => hideChartTip(tip);
}

export {
  buildCtxPoints,
  drawLineChart,
  drawBars,
  drawAggBars,
  renderCostLegend,
  setCostUnit,
  findRound,
};
