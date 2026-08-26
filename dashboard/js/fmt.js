/** Formatting helpers and DOM $ */
const $ = (id) => document.getElementById(id);

function isSubagentKind(kind) {
  const k = String(kind || "").toLowerCase();
  return k === "subagent" || k === "subagent_resume";
}

function fmtTokens(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtUsd(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n === 0) return "$0";
  if (Math.abs(n) < 0.01) return "$" + n.toFixed(4);
  return "$" + n.toFixed(3);
}
/** $/M token rate label. */
function fmtUsdPerM(n) {
  if (n == null || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 10) return "$" + n.toFixed(1);
  return "$" + n.toFixed(2);
}

/**
 * Published xAI $/M cards (Official snap check).
 * Grok 4.5: $2/$0.30/$6 or $2/$0.60/$6 (repricing ambiguous) — not $0.50.
 * Grok 4.6: $2/$0.50/$6 only. High tier doubles.
 */
const RATE_CARD_45 = [
  { in: 2, cached: 0.3, out: 6 },
  { in: 2, cached: 0.6, out: 6 },
  { in: 4, cached: 0.6, out: 12 },
  { in: 4, cached: 1.2, out: 12 },
];
const RATE_CARD_46 = [
  { in: 2, cached: 0.5, out: 6 },
  { in: 4, cached: 1.0, out: 12 },
];
const RATE_CARD_ALL = [...RATE_CARD_45, ...RATE_CARD_46];
/** Component $/M and absolute $ session match — do not force-snap beyond this. */
const RATE_SNAP_EPS = 0.02;

function normalizeModelFamily(raw) {
  const s = String(raw || "").toLowerCase().replace(/_/g, "-");
  if (!s) return null;
  if (s.includes("4.6") || s.includes("4-6")) return "grok-4.6";
  if (s.includes("4.5") || s.includes("4-5")) return "grok-4.5";
  return null;
}

/** Cards to try for a model (4.5 tries $0.30 and $0.60 cache). */
function rateCardsForModel(model) {
  const fam = normalizeModelFamily(model);
  if (fam === "grok-4.6") return RATE_CARD_46;
  if (fam === "grok-4.5") return RATE_CARD_45;
  return RATE_CARD_ALL;
}

function costAtRates(tok, rates) {
  return (tok.unc * rates.in + tok.cached * rates.cached + tok.out * rates.out) / 1e6;
}

function ratesNearCard(rates, card, eps = RATE_SNAP_EPS) {
  return Math.abs(rates.in - card.in) <= eps
    && Math.abs(rates.out - card.out) <= eps
    && Math.abs(rates.cached - card.cached) <= eps;
}

/**
 * Solve Official $ with a card's own In/Out and Cache/Out ratios.
 * e.g. 2/0.3/6 → Cache=Out/20; 2/0.6/6 → Cache=Out/10; 2/0.5/6 → Cache=Out/12.
 */
function solveRatesForCard(officialUsd, tok, card) {
  const outP = Number(card.out);
  if (!(outP > 0)) return null;
  const inRatio = Number(card.in) / outP;
  const cacheRatio = Number(card.cached) / outP;
  const weight = tok.unc * inRatio + tok.cached * cacheRatio + tok.out;
  if (!(Number(officialUsd) > 0) || !(weight > 0)) return null;
  const out = (Number(officialUsd) * 1e6) / weight;
  return { in: out * inRatio, cached: out * cacheRatio, out };
}

/**
 * Reverse-engineer In/Cached/Out $/M from Official $ + API tokens.
 * Picks the best matching published card for the model (4.5: $0.30 or $0.60
 * cache; 4.6: $0.50). Snap only when within ±$0.02 (cost or rates).
 */
function implyOfficialRatesPerM(officialUsd, tok, opts) {
  const off = Number(officialUsd);
  const unc = Number(tok && tok.unc) || 0;
  const cached = Number(tok && tok.cached) || 0;
  const outTok = Number(tok && tok.out) || 0;
  if (!(off > 0) || !(unc + cached + outTok > 0)) return null;
  const t = { unc, cached, out: outTok };
  const cards = rateCardsForModel(opts && opts.model);

  let bestCard = null;
  let bestCostD = Infinity;
  for (const card of cards) {
    const d = Math.abs(costAtRates(t, card) - off);
    if (d < bestCostD) { bestCostD = d; bestCard = card; }
  }
  if (bestCard && bestCostD <= RATE_SNAP_EPS) {
    return { ...bestCard, snapped: true, note: "official $ ≈ published card" };
  }

  let bestSolve = null;
  let bestSolveD = Infinity;
  for (const card of cards) {
    const rates = solveRatesForCard(off, t, card);
    if (!rates) continue;
    if (ratesNearCard(rates, card)) {
      return { ...card, snapped: true, note: "ratio solve ≈ published" };
    }
    const d = Math.abs(rates.in - card.in)
      + Math.abs(rates.cached - card.cached)
      + Math.abs(rates.out - card.out);
    if (d < bestSolveD) {
      bestSolveD = d;
      bestSolve = { ...rates, snapped: false, note: `ratio ${card.in}/${card.cached}/${card.out}` };
    }
  }
  return bestSolve;
}

/**
 * Period I/O $/M point: Official ÷ API tokens from rounds ≤190k ctx
 * (`rate_*` fields — same pool as Session Official card).
 */
function ratesPerMFromIoCosts(row) {
  const tin = Number(row && (row.rate_tokens_in ?? row.api_tokens_in ?? row.tokens_in)) || 0;
  const tc = Number(row && (row.rate_tokens_cached ?? row.api_tokens_cached ?? row.tokens_cached)) || 0;
  const tout = Number(row && (row.rate_tokens_out ?? row.api_tokens_out ?? row.tokens_out)) || 0;
  const off = Number(row && (row.rate_official_usd ?? row.official_usd)) || 0;
  const model = (row && (row.model_family || row.model_id || row.model)) || null;
  const implied = implyOfficialRatesPerM(off, { unc: tin, cached: tc, out: tout }, { model });
  if (implied) return implied;
  // No official bill — fall back to estimate cost÷tokens.
  const cin = Number(row && row.cost_in_usd) || 0;
  const cc = Number(row && row.cost_cached_usd) || 0;
  const cout = Number(row && row.cost_out_usd) || 0;
  const rates = {
    in: tin > 0 ? (cin * 1e6) / tin : 0,
    cached: tc > 0 ? (cc * 1e6) / tc : 0,
    out: tout > 0 ? (cout * 1e6) / tout : 0,
  };
  for (const card of rateCardsForModel(model)) {
    if (ratesNearCard(rates, card)) return { ...card, snapped: true };
  }
  return { ...rates, snapped: false };
}
function fmtMs(n) {
  if (n == null) return "—";
  if (n < 1000) return Math.round(n) + " ms";
  return (n / 1000).toFixed(1) + " s";
}
function fmtToksPerSec(n) {
  if (n == null || Number.isNaN(n)) return "—";
  const v = Math.abs(n);
  if (v >= 1000) return fmtTokens(n) + "/s";
  if (v >= 100) return n.toFixed(0) + "/s";
  if (v >= 10) return n.toFixed(1) + "/s";
  return n.toFixed(2) + "/s";
}
/** Idle gap between rounds (ms) — prefer minutes/hours when long. */
function fmtIdleGap(ms) {
  if (ms == null || Number.isNaN(ms) || ms < 0) return null;
  if (ms < 1000) return Math.round(ms) + "ms";
  const s = ms / 1000;
  if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + "s";
  const m = s / 60;
  if (m < 60) return (m < 10 ? m.toFixed(1) : Math.round(m)) + "m";
  const h = m / 60;
  if (h < 48) return (h < 10 ? h.toFixed(1) : Math.round(h)) + "h";
  return Math.round(h / 24) + "d";
}
function fmtDelta(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n === 0) return "±0";
  const sign = n > 0 ? "+" : "";
  return sign + fmtTokens(n);
}
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
const AR = `<span class="arrow">→</span>`;

/** Hide zero parts. Colors: in green · cached yellow · out red */
function partIn(tok, usd, label) {
  if (!tok && !(usd > 0)) return "";
  const lab = label || "In";
  return `<span class="tok-in">${lab} +${fmtTokens(tok || 0)}</span> <span class="cost-in">${fmtUsd(usd || 0)}</span>`;
}
function partCached(tok, usd) {
  if (!tok && !(usd > 0)) return "";
  return `<span class="tok-cached">Cached ${fmtTokens(tok || 0)}</span> <span class="cost-cached">${fmtUsd(usd || 0)}</span>`;
}
function partOut(tok, usd) {
  if (!tok && !(usd > 0)) return "";
  return `<span class="tok-out">Out +${fmtTokens(tok || 0)}</span> <span class="cost-out">${fmtUsd(usd || 0)}</span>`;
}
function joinParts(parts) {
  return parts.filter(Boolean).join(` <span class="sep">/</span> `);
}
function totalPrice(usd) {
  if (usd == null || Number.isNaN(usd)) return "";
  return `<span class="cost-total">${AR} ${fmtUsd(usd)}</span>`;
}
function moneyFromNode(n) {
  if (!n) return { ti: 0, tc: 0, to: 0, ci: 0, cc: 0, co: 0, tot: 0 };
  const ci = n.cost_in_usd || 0;
  const cc = n.cost_cached_usd || 0;
  const co = n.cost_out_usd || 0;
  // Prefer explicit estimate_usd (Harness keeps cached display but total = In only)
  const tot = n.estimate_usd != null ? n.estimate_usd : (ci + cc + co);
  return {
    ti: n.tokens_in || 0,
    tc: n.tokens_cached || n.display_cached_tokens || 0,
    to: n.tokens_out || 0,
    ci, cc, co, tot,
  };
}

/** Compress long filesystem paths / cwd (keep last 2 segments, ~ prefix). */
function shortPath(p) {
  if (!p) return "";
  let s = String(p).replace(/\\/g, "/");
  // Collapse home roots
  s = s.replace(/^\/Users\/[^/]+/i, "~");
  s = s.replace(/^[A-Za-z]:\/Users\/[^/]+/i, "~");
  s = s.replace(/^\/home\/[^/]+/i, "~");
  const parts = s.split("/").filter(Boolean);
  if (parts.length <= 2) return parts.join("/");
  return "…/" + parts.slice(-2).join("/");
}

/** Compress cwd-like paths embedded in command / title strings. */
function compressCwdText(text) {
  if (!text) return "";
  let s = String(text);
  // Windows + POSIX absolute paths → short form
  s = s.replace(
    /([A-Za-z]:)?[\\/](?:Users|home)[\\/][^\\/\s"']+[\\/][^\s"']*/gi,
    (m) => shortPath(m)
  );
  s = s.replace(
    /[A-Za-z]:[\\/][^\s"']{24,}/g,
    (m) => shortPath(m)
  );
  s = s.replace(
    /(?:^|[\s"'=])(\/[^\s"']{24,})/g,
    (full, p) => full.replace(p, shortPath(p))
  );
  if (s.length > 56) s = s.slice(0, 24) + "…" + s.slice(-22);
  return s;
}

/**
 * Shell / subagent: keep only the interesting command tail
 * (strip long cwd, cd prefixes, wrapper noise).
 */
function shellInteresting(text) {
  if (!text) return "";
  let s = String(text).replace(/\r?\n/g, " ").trim();
  // Drop common wrappers
  s = s.replace(/^(?:run_terminal_command|get_command_or_subagent_output|spawn_subagent)\s*[:=]?\s*/i, "");
  // cd X; cmd  /  cd X && cmd
  s = s.replace(/^(?:cd|Set-Location|pushd)\s+("[^"]+"|'[^']+'|\S+)\s*[;&]+\s*/i, "");
  s = s.replace(/^(?:cd|Set-Location|pushd)\s+("[^"]+"|'[^']+'|\S+)\s+/i, "");
  // powershell -Command "..." / -c "..."
  const mPs = s.match(/(?:powershell|pwsh)(?:\.exe)?\s+(?:-\w+\s+)*-(?:Command|c)\s+("([^"]*)"|'([^']*)'|(.+)$)/i);
  if (mPs) s = mPs[2] || mPs[3] || mPs[4] || s;
  // Last segment of && / ;
  const segs = s.split(/\s*(?:&&|\|\||;)\s*/).filter(Boolean);
  if (segs.length > 1) s = segs[segs.length - 1];
  s = compressCwdText(s.trim());
  // Prefer python script / verb + short arg
  const mPy = s.match(/\bpython(?:\d)?(?:\.exe)?\s+(.+)$/i);
  if (mPy) s = "python " + compressCwdText(mPy[1]);
  if (s.length > 42) s = s.slice(0, 18) + "…" + s.slice(-18);
  return s;
}

/** Tokenizer token weight stamped on the node (prefer real count over chars//4). */
function pickTokenizerTokens(c) {
  if (!c) return null;
  const keys = [
    "tokenizer_tokens",
    "encrypted_tokens",
    "summary_tokens",
    "message_tokens",
    "ch_result_tokens",
    "result_tokens_est",
    "arg_tokens_est",
    "tokens_est",
  ];
  for (const k of keys) {
    const v = c[k];
    if (v != null && Number(v) > 0) return Number(v);
  }
  return null;
}

/** Raw char length only for deriving ch/tok ratio (not displayed alone). */
function pickMetaChars(c) {
  if (!c) return 0;
  const keys = [
    "summary_chars",
    "message_chars",
    "ch_result_chars",
    "result_chars",
    "arg_chars",
    "encrypted_chars",
    "chars",
  ];
  for (const k of keys) {
    const v = c[k];
    if (v != null && Number(v) > 0) return Number(v);
  }
  return 0;
}

/**
 * Gray end-of-line for LLM/Harness children.
 *
 * tokZ = raw tokenizer weight (exact int; Grok-2 / stamp on the node)
 * tokF = In/Out after pro-rata (exact int; green/red figure on the line)
 *
 * Ratio uses integer tokens only: tokZ / tokF (no fmtTokens, no chars//4
 * when a real stamp exists).
 *
 * Display: "1.2k tokZ · 0.80 tokZ/tokF"
 *
 * @param {object} c node
 * @param {number|null|undefined} tokF final billed In or Out tokens for this row
 */
function eolTokenizerMeta(c, tokF) {
  let zRaw = pickTokenizerTokens(c);
  let approx = false;
  if (!(zRaw != null && Number(zRaw) > 0)) {
    // Last resort only: chars//4 proxy when no tokenizer stamp at all
    const ch = pickMetaChars(c);
    if (ch > 0) {
      zRaw = Math.max(1, Math.round(Number(ch) / 4));
      approx = true;
    } else {
      zRaw = null;
    }
  }
  // Exact integer tokens for ratio (never use fmt'd / float display values)
  const z = zRaw != null && Number(zRaw) > 0 ? Math.round(Number(zRaw)) : null;
  const f =
    tokF != null && Number(tokF) > 0 ? Math.round(Number(tokF)) : null;
  const bits = [];
  if (z != null && z > 0) {
    bits.push(`${approx ? "~" : ""}${fmtTokens(z)} tokZ`);
    if (f != null && f > 0) {
      bits.push(`${(z / f).toFixed(2)} tokZ/tokF`);
    }
  } else if (f != null && f > 0) {
    bits.push(`${fmtTokens(f)} tokF`);
  }
  if (!bits.length) return "";
  const tip = [
    z != null ? `tokZ=${z} (tokenizer exact${approx ? " ~chars/4" : ""})` : "tokZ=—",
    f != null ? `tokF=${f} (billed In/Out exact)` : "tokF=—",
    z != null && f != null && f > 0
      ? `ratio=${(z / f).toFixed(4)}`
      : null,
  ].filter(Boolean).join(" · ");
  return `<span class="eol-meta muted" title="${esc(tip)}">${bits.join(" · ")}</span>`;
}

export {
  $,
  isSubagentKind,
  fmtTokens,
  fmtUsd,
  fmtUsdPerM,
  fmtMs,
  fmtToksPerSec,
  fmtIdleGap,
  fmtDelta,
  esc,
  AR,
  partIn,
  partCached,
  partOut,
  joinParts,
  totalPrice,
  moneyFromNode,
  ratesPerMFromIoCosts,
  implyOfficialRatesPerM,
  normalizeModelFamily,
  rateCardsForModel,
  shortPath,
  compressCwdText,
  shellInteresting,
  pickTokenizerTokens,
  pickMetaChars,
  eolTokenizerMeta,
};
