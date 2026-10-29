/** Cost bar model — turnCostParts, segs, legend ranking. Do not change formulas. */
import {
  fmtTokens,
  esc,
} from './fmt.js';

/** Present ledger number. 0 is valid; null / "" / NaN → missing. */
function ledgerNum(v) {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Cost Y ($): ~5–8 nice ticks (1-2-5). Old 0.5 steps explode on period totals. */
export function niceCostYMax(rawMax) {
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

export function costSegKey(seg) {
  if (!seg) return "";
  const k = String(seg.k || "");
  // Stable keys (by-label folds Compact N / LLM Answer RN → one chip).
  if (k === "in" || k === "cached" || k === "out" || k === "system"
      || k === "recap" || k === "compact" || k === "official"
      || k === "cache_miss" || k === "prompt" || k === "llm_answer"
      || k === "compact_out" || k === "user")
    return k;
  if (k === "sys_prompt" || k === "user_info" || k === "reminders" || k === "tool_def" || k === "mcp" || k === "unknown")
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
export function costDisplayLabel(seg) {
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
  else if (k === "tool_def") raw = "tool definitions";
  else if (k === "mcp") raw = "mcp";
  else if (k === "unknown") raw = "unknown";
  else if (k === "sub") raw = String(seg.label || "sub agent");
  else raw = String(seg.label || k || "");
  if (raw === "LLM Out→In" || raw === "LLM Out->In") raw = "llm out→in";
  raw = String(raw).toLowerCase();
  if (!io) return raw;
  return raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : raw;
}

export function isCostSegHidden(seg, hidden) {
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

export const COST_COLORS = {
  in: "#3ecf8e",
  cached: "#f0b429",
  out: "#f07178",
  system: "#8ab4f8",
  sys_prompt: "#8ab4f8",
  user_info: "#7aa2f7",
  reminders: "#9d7cd8",
  tool_def: "#6d8fd6",
  mcp: "#5b8def",
  unknown: "#9aa0a6",
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

export function costSegMetric(seg, unit) {
  if (!seg) return 0;
  if (unit === "tokens") {
    const t = Number(seg.tok);
    if (t > 0) return t;
    // fallback: no tok stamp
    return 0;
  }
  return Number(seg.v) || 0;
}

export function fmtCostAxis(v, unit) {
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
export function niceCostYMaxForUnit(m, unit) {
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

export function findRound(rounds, turnIndex) {
  return (rounds || []).find(r => Number(r.index) === Number(turnIndex)) || null;
}

export function turnCostParts(t, round, { detail, peelSystem } = {}) {
  // peelSystem=true when System is drawn as its own chart bar (R1 only)
  const bd = (round && round.breakdown) || t.estimate_breakdown || t.cost_usd || {};
  const sp = round && round.system_prompt;
  const up = round && round.user_prompt;
  let sys = ledgerNum(bd.system_in_usd) ?? ledgerNum(sp && sp.cost_in_usd) ?? 0;
  let user = ledgerNum(bd.user_in_usd) ?? ledgerNum(up && up.cost_in_usd) ?? 0;
  const sysTok = ledgerNum(bd.system_in_tokens)
    ?? ledgerNum(sp && (sp.tokens_in ?? sp.logical_tokens))
    ?? 0;
  const userTok = ledgerNum(bd.user_in_tokens)
    ?? ledgerNum(up && (up.tokens_in ?? up.uncached_est))
    ?? 0;

  // I/O In = breakdown.tree_in (never R1 uncached_in — recounts System).
  // Prefer round fields (hierarchy) over turn snapshot (may be stale API bill).
  let cin = ledgerNum(bd.tree_in_usd)
    ?? ledgerNum(round && round.cost_tree_in_usd)
    ?? ledgerNum(t.cost_tree_in_usd);
  if (cin == null) {
    // legacy no-snap: payload without breakdown.tree_in
    cin = ledgerNum(t.cost_in_usd)
      ?? ledgerNum(bd.uncached_in_usd)
      ?? ledgerNum(bd.uncached_input)
      ?? ledgerNum(bd.input)
      ?? 0;
  }
  let ccache = ledgerNum(bd.cached_usd)
    ?? ledgerNum(round && round.cost_cached_usd)
    ?? ledgerNum(t.cost_cached_usd)
    ?? ledgerNum(bd.cached_input)
    ?? 0;
  let cout = ledgerNum(bd.output_usd)
    ?? ledgerNum(round && round.cost_out_usd)
    ?? ledgerNum(t.cost_out_usd)
    ?? ledgerNum(bd.output)
    ?? ledgerNum(bd.output_incl_reasoning)
    ?? 0;

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
  let tinTok = ledgerNum(bd.tree_in_tokens) ?? ledgerNum(round && round.tree_in_tokens);
  if (tinTok == null) {
    // legacy no-snap
    tinTok = ledgerNum(t.uncached_input_tokens) ?? 0;
  }
  const tcacheTok = ledgerNum(bd.cached_tokens)
    ?? ledgerNum(t.cached_read_tokens)
    ?? 0;
  const toutTok = ledgerNum(bd.output_tokens)
    ?? ledgerNum(t.output_tokens)
    ?? 0;
  const userTokN = userTok || 0;
  const harnessTokN = ledgerNum(bd.harness_in_tokens) ?? 0;
  const missTokN = ledgerNum(bd.cache_miss_in_tokens)
    ?? ledgerNum(round && round.cache_miss_in_tokens)
    ?? 0;
  let thoughtTokN = Number(bd.llm_thought_summary_tokens) || 0;
  let reasonTokN = Number(bd.llm_reasoning_tokens ?? bd.llm_reasoning_encrypted_tokens) || 0;
  const emitTokN = Number(bd.llm_out_to_harness_tokens) || 0;
  const msgTokN = Number(bd.llm_out_to_user_tokens) || 0;

  const segs = [];

  if (detail) {
    let harness = ledgerNum(bd.harness_in_usd) ?? 0;
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
    const missUsd = ledgerNum(bd.cache_miss_in_usd)
      ?? ledgerNum(round && round.cache_miss_in_usd)
      ?? 0;
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
export function turnCostPartsTools(t, round, { peelSystem } = {}) {
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
  const missUsd = ledgerNum(bd.cache_miss_in_usd)
    ?? ledgerNum(round && round.cache_miss_in_usd)
    ?? 0;
  const missTok = ledgerNum(bd.cache_miss_in_tokens)
    ?? ledgerNum(round && round.cache_miss_in_tokens)
    ?? 0;
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
  const cacheUsd = ledgerNum(bd.cached_usd)
    ?? ledgerNum(round && round.cost_cached_usd)
    ?? ledgerNum(base.cached)
    ?? 0;
  const cacheTok = ledgerNum(bd.cached_tokens)
    ?? ledgerNum(round && round.cached_read_tokens)
    ?? ledgerNum(base.cached_tokens)
    ?? 0;
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
  const co = up.compact_out || {};
  const coT = Number(co.tokens_in) || 0;
  const coU = Number(co.cost_in_usd) || 0;
  const rawUser = Number(up.tokens_in ?? up.uncached_est) || 0;
  const foldedCo = !!up.compact_out_in_user_in;
  let promptT;
  if (up.prompt_tokens_in != null) promptT = Number(up.prompt_tokens_in) || 0;
  else if (foldedCo) promptT = Math.max(0, rawUser - coT - ansT);
  else promptT = rawUser;
  let promptU;
  if (up.prompt_cost_in_usd != null) promptU = Number(up.prompt_cost_in_usd) || 0;
  else {
    const rawUsd = Number(up.cost_in_usd) || 0;
    if (foldedCo && rawUser > 0)
      promptU = rawUsd * (promptT / rawUser);
    else promptU = rawUsd;
  }
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
    if (k === "tool_definitions" || k === "message" || k === "tool_defs_message")
      return { k: "tool_def", label: "Tool Definitions", color: COST_COLORS.tool_def };
    if (k === "mcp") return { k: "mcp", label: "MCP", color: COST_COLORS.mcp || COST_COLORS.reminders };
    if (k === "unknown")
      return { k: "unknown", label: "Unknown", color: COST_COLORS.unknown || "#a8b4c8" };
    return { k: "sys_prompt", label: label || k || "System Prompt", color: COST_COLORS.sys_prompt };
  };
  const segs = [];
  let sumT = 0;
  let sumU = 0;
  for (const p of raw) {
    if (!p || p.kind === "hooks") continue;
    const tok = Number(p.tokenizer_tokens ?? p.tokens ?? p.tokens_in) || 0;
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

export function systemCostBar(round, { stack } = {}) {
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
export function userCostBar(round, { stack } = {}) {
  const up = round && round.user_prompt;
  if (!up || up.kind !== "user_prompt") return null;
  const bd = (round && round.breakdown) || {};
  const st = (stack || (window.__costChart && window.__costChart.stack) || "io");
  const isR1 = Number(round && round.index) === 1;

  const co = up.compact_out || {};
  const coTok = Number(co.tokens_in) || 0;
  const coUsd = Number(co.cost_in_usd) || 0;
  let inTok = ledgerNum(bd.user_in_tokens)
    ?? ledgerNum(up.tokens_in ?? up.uncached_est)
    ?? ledgerNum(up.display_in_tokens)
    ?? 0;
  let inUsd = ledgerNum(bd.user_in_usd)
    ?? ledgerNum(up.cost_in_usd)
    ?? ledgerNum(up.display_in_usd)
    ?? 0;
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

export function callCostParts(step, callIndex, round, opts) {
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
  const ccacheTok = Number(step.cached_read_tokens ?? step.tokens_cached ?? se.cached_read_tokens) || 0;
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
  const missTok = Number(step.cache_miss_in_tokens) || 0;
  const missUsd = Number(step.cache_miss_in_usd) || 0;
  if (missTok > 0 || missUsd > 0) {
    bottomExtra.push({
      k: "cache_miss",
      label: "cache miss",
      legendKey: "cache_miss",
      v: missUsd,
      tok: missTok,
      color: COST_COLORS.cache_miss,
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
        || s.k === "tool_def" || s.k === "mcp" || s.k === "unknown" || s.k === "system") {
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
    } else if (s.k === "sys_prompt" || s.k === "user_info" || s.k === "reminders" || s.k === "tool_def" || s.k === "mcp" || s.k === "unknown") {
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

export function applyDrillStack(bar, stack) {
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

export function collectRoundSubagentBars(round) {
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
export function attachSubagentSegsToTurn(turnBar, round, stack) {
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
    // Distinct Sub Agent slices (not folded into In/Cached/Out) so they
    // stay visible + clickable on Cost Per Turn.
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
  } else if (mode === "parts") {
    for (const a of agents) {
      turnBar.segs.push({
        k: "sub",
        label: "Sub Agent " + a.n,
        legendKey: "sub:" + a.n,
        v: a.total || 0,
        tok: a.total_tok || 0,
        color: COST_COLORS.sub,
        session_id: a.session_id,
        title: a.title,
      });
    }
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

export function eventMs(e) {
  const n = Number(e && e.agent_ms);
  return Number.isFinite(n) ? n : 0;
}

export function compactCostBar(c, compactIndex, { detail } = {}) {
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

export function recapCostBar(c, recapIndex, { detail } = {}) {
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

export function collectLabelLegend(bars, unit) {
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

const PARTS_STACK_ORDER = [
  "cached", "cache_miss", "harness", "reasoning", "toolreq",
  "message", "thought", "user", "compact", "recap",
  "in", "out", "system", "sys_prompt", "user_info", "reminders", "mcp", "tool_def", "unknown", "mcp", "unknown",
  "llm_out_in", "official", "sub",
];

/** User Tools breakdown keys (after toolreq:* and tool:* in Tools mode). */
const USER_BREAKDOWN_ORDER = ["prompt", "llm_answer", "compact_out"];

export function currentCostStack() {
  if (document.body.classList.contains("scope-period"))
    return (window.__aggChart && window.__aggChart.stack) || "io";
  return (window.__costChart && window.__costChart.stack) || "io";
}

export function loadLegendOrder(stack) {
  try {
    const raw = localStorage.getItem("tt-leg-order-" + (stack || "io"));
    if (!raw) return null;
    const arr = JSON.parse(raw);
    return Array.isArray(arr) && arr.length ? arr.map(String) : null;
  } catch {
    return null;
  }
}

export function saveLegendOrder(stack, keys) {
  try {
    localStorage.setItem("tt-leg-order-" + (stack || "io"), JSON.stringify(keys || []));
  } catch { /* ignore */ }
}

export function clearLegendOrder(stack) {
  try {
    localStorage.removeItem("tt-leg-order-" + (stack || "io"));
  } catch { /* ignore */ }
}

/** Default rank: bottom of bar = low. Tools: fixed → toolreq:* → tool:* → user breakdown. */
export function defaultStackRank(key, meta, stack) {
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

export function orderCostSegs(segs, stack) {
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

export function sortLegendItems(items, stack) {
  const st = stack || currentCostStack();
  return (items || []).slice().sort((a, b) => {
    const ra = _legendRank(a[0], a[1]), rb = _legendRank(b[0], b[1]);
    if (ra !== rb) return ra - rb;
    return String((a[1] && a[1].label) || a[0]).localeCompare(String((b[1] && b[1].label) || b[0]));
  });
}

/** Tooltip rows: I/O keeps stack order; Parts/Tools sort small→large. */
export function tipOrderedSegs(segs, stack, unit) {
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

export function formatTipSegLine(seg, unit, opts) {
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

export function periodSessionTipHead(b) {
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
export function aggregateBarsByLabel(bars, hidden, unit) {
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

export function _cumBuckets(buckets) {
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
