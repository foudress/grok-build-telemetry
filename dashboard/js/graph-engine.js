/**
 * Call-graph canvas engine (d3-force + 2d).
 * Expects globalThis.d3 (v7) from <script src="/vendor/d3.min.js"></script>
 * Vendor: copy of https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js
 */
const TAU = Math.PI * 2;
const DPR_CAP = 2;
const FPS = 30;
const FRAME_MS = 1000 / FPS;
const ALPHA_EPS = 0.008;
const IDLE_MS = 8000;
const FADE_MS = 900;
const HEAT_MS = 20000;
const WALK_OMEGA = 10; // ~0.4s settle, ζ = 1
const POP_OMEGA = 15;
const MIN_HIT_PX = 7;
const MAX_FX = 32;
const MAX_PARTICLES = 28;

const DEFAULT_BOT_SRCS = [
  "/img/bots/bot-blue.jpg",
  "/img/bots/bot-green.jpg",
  "/img/bots/bot-amber.jpg",
  "/img/bots/bot-rose.jpg",
  "/img/bots/bot-violet.jpg",
  "/img/bots/bot-cyan.jpg",
];
const DEFAULT_BOT_COLORS = [
  "#3d9cf0",
  "#3ecf8e",
  "#f0b429",
  "#f07178",
  "#b48ead",
  "#5ccfe6",
];

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const FONT_MONO =
  'ui-monospace, "SF Mono", "Cascadia Code", Consolas, "Liberation Mono", Menlo, monospace';

function posix(p) {
  return String(p || "")
    .replace(/\\/g, "/")
    .replace(/\/{2,}/g, "/")
    .replace(/\/$/, "");
}

function hash32(s) {
  let h = 2166136261;
  const str = String(s ?? "");
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function clamp(v, a, b) {
  return v < a ? a : v > b ? b : v;
}

function baseName(p) {
  const s = posix(p);
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s || p;
}

function parentDir(p) {
  const s = posix(p);
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(0, i) : "";
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function prefersReduce() {
  try {
    return matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

function parseColor(s) {
  const t = String(s || "").trim();
  let m = t.match(/^#([0-9a-f]{6})$/i);
  if (m) {
    const n = parseInt(m[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  m = t.match(/^#([0-9a-f]{3})$/i);
  if (m) {
    const a = m[1];
    return {
      r: parseInt(a[0] + a[0], 16),
      g: parseInt(a[1] + a[1], 16),
      b: parseInt(a[2] + a[2], 16),
    };
  }
  m = t.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i);
  if (m) return { r: +m[1], g: +m[2], b: +m[3] };
  return { r: 61, g: 156, b: 240 };
}

function rgba(col, a) {
  const c = typeof col === "string" ? parseColor(col) : col;
  return `rgba(${c.r},${c.g},${c.b},${a})`;
}

function cssVar(cs, name, fallback) {
  if (!cs) return fallback;
  const key = name.startsWith("--") ? name : "--" + name;
  const v = cs.getPropertyValue(key).trim();
  return v || fallback;
}

function readTokens(el, override) {
  const cs = el ? getComputedStyle(el) : null;
  const o = override && typeof override === "object" ? override : null;
  const grab = (name, fb) => {
    if (o) {
      if (o[name]) return o[name];
      if (o["--" + name]) return o["--" + name];
    }
    return cssVar(cs, name, fb);
  };
  return {
    bg: grab("bg", "#0b0f14"),
    panel: grab("panel", "#121820"),
    text: grab("text", "#e6edf3"),
    muted: grab("muted", "#8b9cb3"),
    muted2: grab("muted-2", "#5c6b80"),
    accent: grab("accent", "#3d9cf0"),
    green: grab("green", "#3ecf8e"),
    amber: grab("amber", "#f0b429"),
    rose: grab("rose", "#f07178"),
    violet: grab("violet", "#b48ead"),
    cyan: grab("cyan", "#5ccfe6"),
    border: grab("border", "#1e2a38"),
    font: cssVar(cs, "font-sans", FONT),
  };
}

function spring(p, v, target, dt, omega, zeta) {
  const acc = omega * omega * (target - p) - 2 * zeta * omega * v;
  v += acc * dt;
  p += v * dt;
  return [p, v];
}

/** JPEG sprites ship a checkerboard; flood-fill from edges so the bot has alpha. */
function punchSpriteBg(img) {
  const w = img.naturalWidth || img.width;
  const h = img.naturalHeight || img.height;
  if (!w || !h) return img;
  const cnv = document.createElement("canvas");
  cnv.width = w;
  cnv.height = h;
  const ctx = cnv.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  let im;
  try {
    im = ctx.getImageData(0, 0, w, h);
  } catch {
    return img;
  }
  const d = im.data;
  const n = w * h;
  const mark = new Uint8Array(n);
  const chroma = (i) => {
    const o = i * 4;
    const r = d[o],
      g = d[o + 1],
      b = d[o + 2];
    return Math.max(r, g, b) - Math.min(r, g, b);
  };
  const lum = (i) => {
    const o = i * 4;
    return (d[o] + d[o + 1] + d[o + 2]) / 3;
  };
  const bgLike = (i) => chroma(i) < 24 && lum(i) > 135;
  const stack = [];
  const push = (i) => {
    if (i < 0 || i >= n || mark[i] || !bgLike(i)) return;
    mark[i] = 1;
    stack.push(i);
  };
  for (let x = 0; x < w; x++) {
    push(x);
    push((h - 1) * w + x);
  }
  for (let y = 0; y < h; y++) {
    push(y * w);
    push(y * w + w - 1);
  }
  while (stack.length) {
    const i = stack.pop();
    const x = i % w;
    if (x > 0) push(i - 1);
    if (x + 1 < w) push(i + 1);
    if (i >= w) push(i - w);
    if (i + w < n) push(i + w);
  }
  for (let i = 0; i < n; i++) if (mark[i]) d[i * 4 + 3] = 0;
  ctx.putImageData(im, 0, 0);
  return cnv;
}

class SpatialGrid {
  constructor(cell) {
    this.cell = cell;
    this.map = new Map();
  }
  clear() {
    this.map.clear();
  }
  key(ix, iy) {
    return ix + "," + iy;
  }
  insert(node) {
    const c = this.cell;
    const ix = Math.floor(node.x / c);
    const iy = Math.floor(node.y / c);
    const k = this.key(ix, iy);
    let b = this.map.get(k);
    if (!b) {
      b = [];
      this.map.set(k, b);
    }
    b.push(node);
  }
  query(x, y, r) {
    const c = this.cell;
    const ix0 = Math.floor((x - r) / c);
    const ix1 = Math.floor((x + r) / c);
    const iy0 = Math.floor((y - r) / c);
    const iy1 = Math.floor((y + r) / c);
    const out = [];
    for (let ix = ix0; ix <= ix1; ix++) {
      for (let iy = iy0; iy <= iy1; iy++) {
        const b = this.map.get(this.key(ix, iy));
        if (b) for (let i = 0; i < b.length; i++) out.push(b[i]);
      }
    }
    return out;
  }
}

export function createGraph(canvasEl, opts = {}) {
  const d3 = globalThis.d3;
  if (!d3) {
    throw new Error(
      "createGraph: globalThis.d3 missing — load /vendor/d3.min.js (d3 v7)"
    );
  }
  if (!canvasEl || canvasEl.tagName !== "CANVAS") {
    throw new Error("createGraph: canvas element required");
  }

  const canvas = canvasEl;
  canvas.classList.add("graph-canvas");
  const host = canvas.parentElement;
  if (host) host.classList.add("graph-host");

  const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
  if (!ctx) throw new Error("createGraph: 2d context unavailable");

  let colors = readTokens(host || canvas, opts.tokens);
  let reduceMotion = prefersReduce();
  const botSrcs = Array.isArray(opts.botSrcs) && opts.botSrcs.length
    ? opts.botSrcs
    : DEFAULT_BOT_SRCS.slice();
  const botColors = DEFAULT_BOT_COLORS;

  const punched = new Array(botSrcs.length).fill(null);
  const rawImgs = botSrcs.map((src, i) => {
    const im = new Image();
    im.decoding = "async";
    im.onload = () => {
      punched[i] = punchSpriteBg(im);
      scheduleDraw();
    };
    im.src = src;
    return im;
  });

  const tip = document.createElement("div");
  tip.className = "graph-tip";
  tip.setAttribute("role", "tooltip");
  (host || document.body).appendChild(tip);

  let width = 1;
  let height = 1;
  let xf = d3.zoomIdentity;
  let destroyed = false;
  let paused = false;
  let userView = false;
  let didAutoFit = false;
  let focusSid = null;
  let hover = null; // {type:'node'|'bot', node?, bot?}
  let dragNode = null;
  let lastExpanded = null;

  let nodes = [];
  let rawEdges = [];
  let links = [];
  const byId = new Map();
  const byPath = new Map();
  const collapsed = new Set();
  const posMemo = new Map();

  const bots = new Map();
  const seenEvents = new Set();
  let fx = [];
  let particles = [];

  const grid = new SpatialGrid(48);

  let raf = 0;
  let lastPaint = 0;
  let lastStep = 0;

  const sim = d3
    .forceSimulation([])
    .force(
      "link",
      d3
        .forceLink([])
        .id((d) => d.id)
        .distance((d) => (d.kind === "import" ? 78 : 54))
        .strength((d) => (d.kind === "call" ? 0.32 : 0.11))
    )
    .force(
      "charge",
      d3.forceManyBody().strength((d) =>
        d.kind === "cluster" ? -240 : -48 - (d.deg || 1) * 5
      )
    )
    .force("x", d3.forceX(0).strength(0.03))
    .force("y", d3.forceY(0).strength(0.03))
    .force(
      "collide",
      d3
        .forceCollide()
        .radius((d) => d.r + (d.kind === "cluster" ? 10 : 4))
        .iterations(2)
    )
    .alphaDecay(0.022)
    .velocityDecay(0.38)
    .on("tick", onTick);

  const zoom = d3
    .zoom()
    .scaleExtent([0.12, 8])
    .filter((ev) => {
      if (ev.type === "wheel") return true;
      if (ev.button && ev.button !== 0) return false;
      if (ev.type === "mousedown" || ev.type === "pointerdown" || ev.type === "touchstart") {
        if (hitFromEvent(ev)) return false;
      }
      return true;
    })
    .on("start", (ev) => {
      if (ev.sourceEvent) {
        userView = true;
        canvas.classList.add("is-panning");
      }
    })
    .on("zoom", (ev) => {
      xf = ev.transform;
      scheduleDraw();
    })
    .on("end", () => canvas.classList.remove("is-panning"));

  d3.select(canvas).call(zoom).on("dblclick.zoom", null);

  function onTick() {
    if (sim.alpha() < ALPHA_EPS) sim.stop();
    scheduleDraw();
  }

  function wake(alpha = 0.25) {
    if (destroyed) return;
    if (!paused && alpha > 0) sim.alpha(Math.max(sim.alpha(), alpha)).restart();
    scheduleDraw();
  }

  function remapVisible(n) {
    if (!n) return null;
    let hiddenBy = null;
    let a = n.parentId ? byId.get(n.parentId) : null;
    while (a) {
      if (collapsed.has(a.id)) hiddenBy = a;
      a = a.parentId ? byId.get(a.parentId) : null;
    }
    return hiddenBy || n;
  }

  function isVisible(n) {
    return remapVisible(n) === n;
  }

  function displayLinks() {
    const map = new Map();
    for (let i = 0; i < rawEdges.length; i++) {
      const e = rawEdges[i];
      const s = remapVisible(byId.get(e.src));
      const t = remapVisible(byId.get(e.dst));
      if (!s || !t || s.id === t.id) continue;
      const dkey = s.id + "\t" + t.id + "\t" + e.kind;
      const prev = map.get(dkey);
      if (prev) prev.w += e.w || 1;
      else map.set(dkey, { source: s, target: t, kind: e.kind || "call", w: e.w || 1 });
    }
    return [...map.values()];
  }

  function childCount(id) {
    let n = 0;
    for (let i = 0; i < nodes.length; i++) if (nodes[i].parentId === id) n++;
    return n;
  }

  function radiusFor(n) {
    if (n.kind === "cluster") {
      return 12 + Math.min(18, Math.log1p(childCount(n.id)) * 4);
    }
    const loc = n.loc > 0 ? n.loc : 40;
    const deg = n.deg > 0 ? n.deg : 1;
    return Math.min(16, 4.6 + Math.sqrt(loc) * 0.22 + Math.log1p(deg) * 1.35);
  }

  function linkParents() {
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      n.parentId = null;
      if (n.kind === "cluster") {
        const pd = parentDir(n.path);
        if (pd) {
          const p = byPath.get(pd.toLowerCase());
          if (p && p.kind === "cluster" && p !== n) n.parentId = p.id;
        }
      } else {
        const dir = posix(n.dir || parentDir(n.path)).toLowerCase();
        const p = byPath.get(dir);
        if (p && p.kind === "cluster") n.parentId = p.id;
      }
    }
  }

  function rebuildForces() {
    const vis = nodes.filter(isVisible);
    links = displayLinks();
    for (let i = 0; i < vis.length; i++) vis[i].r = radiusFor(vis[i]);
    sim.nodes(vis);
    sim.force("link").links(links);
  }

  function ingest(ns, es, keepPos) {
    const prev = keepPos || new Map(nodes.map((n) => [n.id, n]));
    nodes = [];
    byId.clear();
    byPath.clear();
    const list = Array.isArray(ns) ? ns : [];
    for (let i = 0; i < list.length; i++) {
      const src = list[i] || {};
      const id = String(src.id ?? src.path ?? i);
      const old = prev.get(id) || posMemo.get(id);
      const n = {
        id,
        path: src.path || id,
        dir: src.dir || parentDir(src.path || ""),
        loc: +src.loc || 0,
        deg: +src.deg || 0,
        kind: src.kind === "cluster" ? "cluster" : "file",
        x: old && Number.isFinite(old.x) ? old.x : src.x,
        y: old && Number.isFinite(old.y) ? old.y : src.y,
        vx: 0,
        vy: 0,
        s: 1,
        sv: 0,
        heats: (old && old.heats) || [],
        r: 6,
      };
      if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) {
        const h = hash32(id);
        const rad = 70 + (h % 220);
        n.x = Math.cos(h) * rad;
        n.y = Math.sin(h * 1.31) * rad;
      }
      nodes.push(n);
      byId.set(id, n);
      if (n.path) byPath.set(posix(n.path).toLowerCase(), n);
    }
    rawEdges = [];
    const elist = Array.isArray(es) ? es : [];
    const degBoost = new Map();
    for (let i = 0; i < elist.length; i++) {
      const e = elist[i] || {};
      const src = String(e.src ?? e.source ?? "");
      const dst = String(e.dst ?? e.target ?? "");
      if (!src || !dst || src === dst) continue;
      if (!byId.has(src) || !byId.has(dst)) continue;
      rawEdges.push({
        src,
        dst,
        kind: e.kind === "import" ? "import" : "call",
        w: +e.w > 0 ? +e.w : 1,
      });
      degBoost.set(src, (degBoost.get(src) || 0) + 1);
      degBoost.set(dst, (degBoost.get(dst) || 0) + 1);
    }
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (!n.deg) n.deg = degBoost.get(n.id) || 0;
    }
    linkParents();
    for (let i = 0; i < nodes.length; i++) nodes[i].r = radiusFor(nodes[i]);
    rebuildForces();
  }

  function findNodeForPath(path) {
    const p = posix(path);
    if (!p) return null;
    const lower = p.toLowerCase();
    const exact = byPath.get(lower) || byId.get(path) || byId.get(p);
    if (exact) return remapVisible(exact);
    let best = null;
    let bestScore = -1;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const np = posix(n.path).toLowerCase();
      if (!np) continue;
      let score = -1;
      if (np === lower) score = 1e9;
      else if (lower.endsWith("/" + np)) score = 1000 + np.length;
      else if (np.endsWith("/" + lower)) score = 800 + lower.length;
      else if (n.kind === "cluster" && (lower.startsWith(np + "/") || np.startsWith(lower + "/"))) {
        score = 400 + np.length;
      }
      if (score > bestScore) {
        bestScore = score;
        best = n;
      }
    }
    return best ? remapVisible(best) : null;
  }

  function hasDirectEdge(a, b) {
    if (!a || !b) return false;
    for (let i = 0; i < rawEdges.length; i++) {
      const e = rawEdges[i];
      if (
        (e.src === a.id && e.dst === b.id) ||
        (e.src === b.id && e.dst === a.id)
      ) {
        return true;
      }
    }
    return false;
  }

  function botIndex(sid) {
    return hash32(sid) % botSrcs.length;
  }

  function ensureBot(sid, name) {
    let b = bots.get(sid);
    if (b) {
      if (name) b.name = name;
      return b;
    }
    const idx = botIndex(sid);
    const h = hash32(sid);
    b = {
      sid,
      name: name || "",
      idx,
      hash: h,
      color: botColors[idx] || colors.accent,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
      placed: false,
      alpha: 0,
      lastAt: 0,
      lastKind: "",
      node: null,
      bobAmp: 0,
      bobPhase: 0,
      bobY: 0,
    };
    bots.set(sid, b);
    return b;
  }

  function spawnParticle(fromNode, toNode, color) {
    const a = remapVisible(fromNode);
    const b = remapVisible(toNode);
    if (!a || !b || a === b) return;
    particles.push({ a, b, color, t0: performance.now(), dur: reduceMotion ? 1 : 480 });
    if (particles.length > MAX_PARTICLES) particles.shift();
  }

  function pushFx(item) {
    fx.push(item);
    if (fx.length > MAX_FX) fx.shift();
  }

  function juice(node, kind, bot) {
    const now = performance.now();
    const color = bot.color;
    if (kind === "write") {
      if (!reduceMotion) {
        node.sv = (node.sv || 0) + 3.6;
        node.s = Math.max(node.s || 1, 1.04);
        bot.bobAmp = 3.2;
        bot.bobPhase = 0;
      }
      node.heats = node.heats || [];
      node.heats.push({ at: now, color, sid: bot.sid });
      if (node.heats.length > 4) node.heats.shift();
      pushFx({ kind: "pulse", node, at: now, color, life: 520 });
    } else if (kind === "read") {
      pushFx({ kind: "pulse", node, at: now, color, life: 700 });
    } else if (kind === "search") {
      pushFx({ kind: "ripple", node, at: now, color, life: 1100 });
    } else if (kind === "list") {
      pushFx({ kind: "pulse", node, at: now, color, life: 420, quiet: 1 });
    } else if (kind === "exec") {
      pushFx({ kind: "pulse", node, at: now, color, life: 360, quiet: 1 });
    }
  }

  function eventKey(ev) {
    return (
      (ev.t ?? "") +
      "\t" +
      (ev.sid ?? "") +
      "\t" +
      (ev.path ?? "") +
      "\t" +
      (ev.kind ?? "") +
      "\t" +
      (ev.tool ?? "")
    );
  }

  function applyEvent(ev, now, forceJuice) {
    if (!ev) return;
    const key = eventKey(ev);
    const fresh = !seenEvents.has(key);
    seenEvents.add(key);
    if (seenEvents.size > 5000) seenEvents.delete(seenEvents.values().next().value);
    const sid = String(ev.sid || ev.name || "anon");
    const bot = ensureBot(sid, ev.name);
    const wall = +ev.t;
    bot.lastAt = wall > 1e12 ? now - Math.max(0, Date.now() - wall) : now;
    bot.lastKind = ev.kind || "";
    if (ev.name) bot.name = ev.name;
    const node = findNodeForPath(ev.path);
    if (node) {
      const prev = bot.node;
      bot.node = node;
      if (!bot.placed) {
        const spawn = node.parentId ? byId.get(node.parentId) || node : node;
        const ang = ((bot.hash >>> 5) % 7) * (TAU / 7);
        bot.x = (spawn.x || 0) + Math.cos(ang) * 22;
        bot.y = (spawn.y || 0) + Math.sin(ang) * 22;
        bot.vx = 0;
        bot.vy = 0;
        bot.placed = true;
        if (reduceMotion) {
          bot.x = node.x;
          bot.y = node.y;
        }
      }
      if ((fresh || forceJuice) && prev && prev !== node && hasDirectEdge(prev, node)) {
        spawnParticle(prev, node, bot.color);
      }
      if (fresh || forceJuice) juice(node, ev.kind, bot);
    }
    bot.alpha = Math.max(bot.alpha, 0.02);
    wake(0.06);
  }

  function toggleCluster(cluster) {
    if (!cluster || cluster.kind !== "cluster") return;
    if (collapsed.has(cluster.id)) {
      collapsed.delete(cluster.id);
      lastExpanded = cluster.id;
      const kids = nodes.filter((n) => n.parentId === cluster.id);
      for (let i = 0; i < kids.length; i++) {
        const k = kids[i];
        if (!Number.isFinite(k.x) || Math.hypot(k.x - cluster.x, k.y - cluster.y) < 2) {
          const ang = (i / Math.max(1, kids.length)) * TAU;
          k.x = cluster.x + Math.cos(ang) * (cluster.r + 28);
          k.y = cluster.y + Math.sin(ang) * (cluster.r + 28);
        }
      }
      rebuildForces();
      wake(0.45);
    } else {
      collapsed.add(cluster.id);
      rebuildForces();
      wake(0.28);
    }
  }

  function eventCssXY(ev) {
    const r = canvas.getBoundingClientRect();
    const src = ev.touches && ev.touches[0] ? ev.touches[0] : ev;
    return [src.clientX - r.left, src.clientY - r.top];
  }

  function worldFromEvent(ev) {
    const [x, y] = eventCssXY(ev);
    return xf.invert([x, y]);
  }

  function rebuildGrid() {
    grid.clear();
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (!isVisible(n)) continue;
      grid.insert(n);
    }
  }

  function hitFromEvent(ev) {
    rebuildGrid();
    const [sx, sy] = eventCssXY(ev);
    const [wx, wy] = xf.invert([sx, sy]);
    const k = xf.k;
    let best = null;
    let bestD = Infinity;
    for (const b of bots.values()) {
      if (!b.placed || b.alpha < 0.08) continue;
      const dim = focusSid && b.sid !== focusSid ? 0.22 : 1;
      if (b.alpha * dim < 0.06) continue;
      const bx = b.x * k + xf.x;
      const by = (b.y + (b.bobY || 0)) * k + xf.y;
      const br = Math.max(12, botScreenR(k) * 0.55);
      const d = Math.hypot(sx - bx, sy - by);
      if (d <= br && d < bestD) {
        bestD = d;
        best = { type: "bot", bot: b, node: remapVisible(b.node) };
      }
    }
    if (best) return best;
    const visR = 80 / k;
    const cand = grid.query(wx, wy, visR);
    for (let i = 0; i < cand.length; i++) {
      const n = cand[i];
      const screenR = n.r * (n.s || 1) * k;
      if (screenR < 3.5) continue;
      const hitR = Math.max(screenR, MIN_HIT_PX) / k;
      const d = Math.hypot(wx - n.x, wy - n.y);
      if (d <= hitR && d < bestD) {
        bestD = d;
        best = { type: "node", node: n };
      }
    }
    return best;
  }

  function botScreenR(k) {
    return clamp(22 * Math.pow(k, 0.22), 16, 34);
  }

  function showTip(hit, ev) {
    if (!hit) {
      tip.classList.remove("is-visible");
      return;
    }
    const r = (host || canvas).getBoundingClientRect();
    const [sx, sy] = eventCssXY(ev);
    if (hit.type === "bot") {
      const b = hit.bot;
      const n = hit.node;
      tip.innerHTML =
        `<div class="path">${esc(b.name || b.sid)}</div>` +
        `<div class="meta">${esc(b.lastKind || "agent")}${n ? " · " + esc(n.path) : ""}</div>`;
    } else {
      const n = hit.node;
      const kind = n.kind === "cluster" ? "cluster" : "file";
      const extra =
        n.kind === "cluster"
          ? `${childCount(n.id)} inside`
          : `${n.loc || "—"} loc · deg ${n.deg || 0}`;
      tip.innerHTML =
        `<div class="path">${esc(n.path)}</div>` +
        `<div class="meta">${kind} · ${esc(extra)}</div>`;
    }
    tip.style.display = "block";
    let left = sx + 12;
    let top = sy + 12;
    const tw = tip.offsetWidth || 160;
    const th = tip.offsetHeight || 40;
    if (left + tw > r.width - 6) left = sx - tw - 12;
    if (top + th > r.height - 6) top = sy - th - 12;
    tip.style.left = Math.max(4, left) + "px";
    tip.style.top = Math.max(4, top) + "px";
    tip.classList.add("is-visible");
  }

  function setHover(hit, ev) {
    const prev = hover;
    hover = hit;
    canvas.classList.toggle("is-over-node", !!hit);
    if (!hit) {
      showTip(null);
      if (prev) scheduleDraw();
      return;
    }
    showTip(hit, ev);
    scheduleDraw();
  }

  function onPointerDown(ev) {
    if (ev.button && ev.button !== 0) return;
    rebuildGrid();
    const hit = hitFromEvent(ev);
    if (!hit || hit.type !== "node") return;
    dragNode = hit.node;
    dragNode.fx = dragNode.x;
    dragNode.fy = dragNode.y;
    canvas.classList.add("is-dragging");
    try {
      canvas.setPointerCapture(ev.pointerId);
    } catch {
      /* older browsers */
    }
    ev.preventDefault();
    wake(0.22);
  }

  function onPointerMove(ev) {
    if (dragNode) {
      const [wx, wy] = worldFromEvent(ev);
      dragNode.fx = dragNode.x = wx;
      dragNode.fy = dragNode.y = wy;
      wake(0.12);
      return;
    }
    rebuildGrid();
    setHover(hitFromEvent(ev), ev);
  }

  function onPointerUp() {
    if (!dragNode) return;
    dragNode.x = dragNode.fx;
    dragNode.y = dragNode.fy;
    dragNode.fx = null;
    dragNode.fy = null;
    posMemo.set(dragNode.id, { x: dragNode.x, y: dragNode.y });
    dragNode = null;
    canvas.classList.remove("is-dragging");
    wake(0.16);
  }

  function onPointerLeave() {
    if (!dragNode) setHover(null);
  }

  function onDblClick(ev) {
    rebuildGrid();
    const hit = hitFromEvent(ev);
    if (hit && hit.node && hit.node.kind === "cluster") {
      toggleCluster(hit.node);
      ev.preventDefault();
    }
  }

  function graphKeysWanted() {
    try {
      if (canvas.matches(":hover")) return true;
    } catch {
      /* :hover on detached node */
    }
    const ae = document.activeElement;
    return ae === canvas || (host && ae && host.contains(ae));
  }

  function onKey(ev) {
    if (destroyed) return;
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (!graphKeysWanted()) return;
    if (ev.key === "Escape") {
      if (hover) {
        setHover(null);
        ev.preventDefault();
        return;
      }
      if (focusSid) {
        focusSid = null;
        scheduleDraw();
        ev.preventDefault();
        return;
      }
      if (lastExpanded && !collapsed.has(lastExpanded)) {
        const n = byId.get(lastExpanded);
        if (n) toggleCluster(n);
        ev.preventDefault();
      }
    } else if (ev.key === "f" || ev.key === "F") {
      fit();
      ev.preventDefault();
    } else if (ev.key === " " && tag !== "BUTTON") {
      setPaused(!paused);
      ev.preventDefault();
    }
  }

  function onMq(e) {
    reduceMotion = e.matches;
    scheduleDraw();
  }

  const mq = matchMedia("(prefers-reduced-motion: reduce)");
  if (mq.addEventListener) mq.addEventListener("change", onMq);
  else if (mq.addListener) mq.addListener(onMq);

  if (canvas.tabIndex < 0) canvas.tabIndex = 0;
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("pointerleave", onPointerLeave);
  canvas.addEventListener("dblclick", onDblClick);
  window.addEventListener("keydown", onKey);

  const ro =
    typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => resize())
      : null;
  if (ro) ro.observe(host || canvas);

  function persistPositions() {
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      posMemo.set(n.id, { x: n.x, y: n.y });
    }
  }

  function botTarget(bot, now) {
    const n = remapVisible(bot.node);
    if (!n) return { x: bot.x, y: bot.y, node: null };
    const h = bot.hash;
    const ang =
      ((h >>> 5) % 7) * (TAU / 7) +
      ((h >>> 17) & 255) / 255 * 0.45 +
      (reduceMotion ? 0 : now * 0.00028);
    const dist = n.r + 16 + ((h >>> 11) % 7);
    const ox = Math.cos(ang) * dist;
    const oy = Math.sin(ang) * dist;
    const d = Math.hypot(bot.x - n.x, bot.y - n.y);
    const mix = d < 90 ? clamp((90 - Math.max(0, d - dist)) / 90, 0.18, 1) : 0.1;
    return { x: n.x + ox * mix, y: n.y + oy * mix, node: n };
  }

  function stepMotion(dt, now) {
    fx = fx.filter((f) => now - f.at < f.life);
    particles = particles.filter((p) => now - p.t0 < p.dur);
    if (paused) return;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (n.heats && n.heats.length) {
        n.heats = n.heats.filter((h) => now - h.at < HEAT_MS);
      }
      if (Math.abs((n.s || 1) - 1) < 0.003 && Math.abs(n.sv || 0) < 0.01) {
        n.s = 1;
        n.sv = 0;
        continue;
      }
      const zeta = reduceMotion ? 1 : 0.55;
      const pair = spring(n.s || 1, n.sv || 0, 1, dt, POP_OMEGA, zeta);
      n.s = pair[0];
      n.sv = pair[1];
      if (reduceMotion) {
        n.s = 1;
        n.sv = 0;
      }
    }
    for (const bot of bots.values()) {
      if (!bot.placed) continue;
      const tgt = botTarget(bot, now);
      if (reduceMotion) {
        bot.x = tgt.x;
        bot.y = tgt.y;
        bot.vx = 0;
        bot.vy = 0;
      } else {
        const x = spring(bot.x, bot.vx, tgt.x, dt, WALK_OMEGA, 1);
        const y = spring(bot.y, bot.vy, tgt.y, dt, WALK_OMEGA, 1);
        bot.x = x[0];
        bot.vx = x[1];
        bot.y = y[0];
        bot.vy = y[1];
        if (Math.abs(bot.x - tgt.x) < 0.2 && Math.abs(bot.vx) < 0.3) {
          bot.x = tgt.x;
          bot.vx = 0;
        }
        if (Math.abs(bot.y - tgt.y) < 0.2 && Math.abs(bot.vy) < 0.3) {
          bot.y = tgt.y;
          bot.vy = 0;
        }
      }
      const age = now - bot.lastAt;
      const want = age > IDLE_MS ? clamp(1 - (age - IDLE_MS) / FADE_MS, 0, 1) : 1;
      if (reduceMotion) bot.alpha = want;
      else bot.alpha += (want - bot.alpha) * (1 - Math.exp(-dt * 10));
      if (bot.bobAmp > 0.04 && !reduceMotion) {
        bot.bobPhase += dt * 22;
        bot.bobY = Math.sin(bot.bobPhase) * bot.bobAmp;
        bot.bobAmp *= Math.exp(-dt * 5.5);
      } else {
        bot.bobAmp = 0;
        bot.bobY = 0;
      }
    }
  }

  function needsMotion(now) {
    if (fx.length || particles.length) return true;
    if (dragNode) return true;
    if (paused) return false;
    if (sim.alpha() >= ALPHA_EPS) return true;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (Math.abs((n.s || 1) - 1) > 0.004 || Math.abs(n.sv || 0) > 0.01) return true;
      const heats = n.heats;
      if (heats) {
        for (let j = 0; j < heats.length; j++) {
          if (now - heats[j].at < HEAT_MS) return true;
        }
      }
    }
    for (const b of bots.values()) {
      if (!b.placed) continue;
      if (b.alpha > 0.01 && now - b.lastAt < IDLE_MS + FADE_MS) return true;
      if (b.alpha > 0.01 && b.alpha < 0.995) return true;
      if (Math.abs(b.vx) > 0.05 || Math.abs(b.vy) > 0.05) return true;
      if (b.bobAmp > 0.05) return true;
    }
    return false;
  }

  function scheduleDraw() {
    if (raf || destroyed) return;
    raf = requestAnimationFrame(frame);
  }

  function frame(now) {
    raf = 0;
    if (destroyed) return;
    if (now - lastPaint < FRAME_MS - 2) {
      raf = requestAnimationFrame(frame);
      return;
    }
    const dt = Math.min(0.05, Math.max(0.001, (now - (lastStep || now)) / 1000));
    lastStep = now;
    lastPaint = now;
    stepMotion(dt, now);
    paint(now);
    if (sim.alpha() < ALPHA_EPS) persistPositions();
    if (needsMotion(now)) scheduleDraw();
  }

  function paint(now) {
    const dpr = Math.min(DPR_CAP, window.devicePixelRatio || 1);
    colors = readTokens(host || canvas, opts.tokens);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = colors.bg;
    ctx.fillRect(0, 0, width, height);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";

    if (!nodes.length) {
      ctx.fillStyle = colors.muted;
      ctx.font = `12px ${colors.font || FONT}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("no files", width / 2, height / 2);
      return;
    }

    const k = xf.k;
    rebuildGrid();

    ctx.save();
    ctx.translate(xf.x, xf.y);
    ctx.scale(k, k);

    const hoverNode = hover && hover.node;
    const hoverId = hoverNode ? hoverNode.id : null;

    for (let i = 0; i < links.length; i++) {
      const e = links[i];
      const a = e.source;
      const b = e.target;
      if (!a || !b) continue;
      const hot = hoverId && (a.id === hoverId || b.id === hoverId);
      const wlog = Math.log1p(e.w || 1);
      const alpha =
        (e.kind === "import" ? 0.16 : 0.22) + Math.min(0.28, wlog * 0.05) + (hot ? 0.28 : 0);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = rgba(hot ? colors.accent : e.kind === "call" ? colors.text : colors.muted, alpha);
      ctx.lineWidth = (0.9 + wlog * 0.55) / k;
      if (e.kind === "import") ctx.setLineDash([5 / k, 4 / k]);
      else ctx.setLineDash([]);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    const vis = sim.nodes();
    for (let i = 0; i < vis.length; i++) {
      const n = vis[i];
      const heats = n.heats;
      if (!heats || !heats.length) continue;
      for (let j = 0; j < heats.length; j++) {
        const h = heats[j];
        const u = 1 - (now - h.at) / HEAT_MS;
        if (u <= 0) continue;
        let a = 0.42 * u * u;
        if (focusSid && h.sid !== focusSid) a *= 0.18;
        const rad = n.r * (n.s || 1) * (3.2 + u);
        const g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, rad);
        g.addColorStop(0, rgba(h.color, a));
        g.addColorStop(1, rgba(h.color, 0));
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(n.x, n.y, rad, 0, TAU);
        ctx.fill();
      }
    }

    for (let i = 0; i < fx.length; i++) {
      const f = fx[i];
      const visN = f.node && remapVisible(f.node);
      if (!visN) continue;
      drawRing(ctx, visN.x, visN.y, visN.r * (visN.s || 1), f, now, k);
    }

    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];
      const u = clamp((now - p.t0) / p.dur, 0, 1);
      const s = u * u * (3 - 2 * u);
      const x = p.a.x + (p.b.x - p.a.x) * s;
      const y = p.a.y + (p.b.y - p.a.y) * s;
      ctx.beginPath();
      ctx.arc(x, y, 2.4 / k, 0, TAU);
      ctx.fillStyle = rgba(p.color, 0.85 * (1 - u * 0.35));
      ctx.fill();
    }

    for (let i = 0; i < vis.length; i++) {
      const n = vis[i];
      const r = n.r * (n.s || 1);
      const hot = hoverId === n.id;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, TAU);
      if (n.kind === "cluster") {
        const tint = clusterTint(n, colors);
        ctx.fillStyle = rgba(tint, hot ? 0.28 : 0.16);
        ctx.strokeStyle = rgba(tint, hot ? 0.85 : 0.5);
        ctx.lineWidth = (hot ? 1.6 : 1.15) / k;
      } else {
        const hub = (n.deg || 0) >= 4;
        ctx.fillStyle = rgba(hub ? colors.accent : colors.text, hot ? 0.28 : hub ? 0.16 : 0.1);
        ctx.strokeStyle = rgba(hot ? colors.accent : colors.muted, hot ? 0.9 : 0.45);
        ctx.lineWidth = (hot ? 1.5 : 1) / k;
      }
      ctx.fill();
      ctx.stroke();
    }

    ctx.restore();

    drawLabels(vis, hoverNode, k);
    drawBots(now, k);
  }

  function clusterTint(n, c) {
    const pal = [c.accent, c.cyan, c.violet, c.green];
    return pal[hash32(n.path) % pal.length];
  }

  function drawRing(ctx2, x, y, r, f, now, k) {
    const u = clamp((now - f.at) / f.life, 0, 1);
    const ripple = f.kind === "ripple";
    const rad = r + 3 + u * (ripple ? 58 : 16);
    const a = (f.quiet ? 0.2 : ripple ? 0.32 : 0.42) * Math.pow(1 - u, 1.25);
    ctx2.beginPath();
    ctx2.arc(x, y, rad, 0, TAU);
    ctx2.strokeStyle = rgba(f.color, a);
    ctx2.lineWidth = (ripple ? 1.4 : 1.8) / k;
    ctx2.stroke();
  }

  function shouldLabel(n, k, hoverNode) {
    if (hoverNode && hoverNode.id === n.id) return true;
    if (n.kind === "cluster") return k > 0.55 || childCount(n.id) >= 3;
    if ((n.deg || 0) >= 5) return true;
    if (k > 1.35 && (n.deg || 0) >= 2) return true;
    if (k > 2.1) return true;
    return false;
  }

  function drawLabels(vis, hoverNode, k) {
    ctx.font = `500 11px ${colors.font || FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineJoin = "round";
    for (let i = 0; i < vis.length; i++) {
      const n = vis[i];
      if (!shouldLabel(n, k, hoverNode)) continue;
      const sx = n.x * k + xf.x;
      const sy = n.y * k + xf.y + n.r * (n.s || 1) * k + 4;
      const label = baseName(n.path) || n.id;
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = rgba(colors.bg, 0.88);
      ctx.strokeText(label, sx, sy);
      ctx.fillStyle = n.kind === "cluster" ? colors.muted : colors.text;
      ctx.fillText(label, sx, sy);
    }
  }

  function drawBots(now, k) {
    const br = botScreenR(k);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.font = `500 10px ${colors.font || FONT}`;
    for (const bot of bots.values()) {
      if (!bot.placed || bot.alpha < 0.02) continue;
      let dim = 1;
      if (focusSid && bot.sid !== focusSid) dim = 0.2;
      const a = bot.alpha * dim;
      if (a < 0.02) continue;
      const sx = bot.x * k + xf.x;
      const sy = (bot.y + (bot.bobY || 0)) * k + xf.y;
      ctx.save();
      ctx.globalAlpha = a;
      ctx.beginPath();
      ctx.ellipse(sx, sy + br * 0.42, br * 0.28, br * 0.08, 0, 0, TAU);
      ctx.fillStyle = "rgba(0,0,0,0.38)";
      ctx.fill();
      const sprite = punched[bot.idx] || rawImgs[bot.idx];
      const bw = br;
      const bh = br;
      const dx = sx - bw / 2;
      const dy = sy - bh * 0.72;
      if (sprite && (sprite.width || sprite.complete)) {
        ctx.drawImage(sprite, dx, dy, bw, bh);
      } else {
        ctx.beginPath();
        ctx.arc(sx, sy - 4, br * 0.36, 0, TAU);
        ctx.fillStyle = bot.color;
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(sx, sy - bh * 0.22, br * 0.42, 0, TAU);
      ctx.strokeStyle = rgba(bot.color, 0.85);
      ctx.lineWidth = 1.4;
      ctx.stroke();
      const showName =
        (bot.name && (k > 1.15 || (hover && hover.bot === bot))) ||
        (hover && hover.bot === bot);
      if (showName && bot.name) {
        ctx.globalAlpha = a;
        ctx.lineWidth = 3;
        ctx.strokeStyle = rgba(colors.bg, 0.9);
        ctx.strokeText(bot.name, sx, sy + br * 0.42);
        ctx.fillStyle = colors.text;
        ctx.fillText(bot.name, sx, sy + br * 0.42);
      }
      ctx.restore();
    }
  }

  function resize() {
    if (destroyed) return;
    const prevW = width;
    const prevH = height;
    const cssW = Math.max(1, canvas.clientWidth || (host && host.clientWidth) || 1);
    const cssH = Math.max(1, canvas.clientHeight || (host && host.clientHeight) || 1);
    const dpr = Math.min(DPR_CAP, window.devicePixelRatio || 1);
    const bw = Math.max(1, Math.floor(cssW * dpr));
    const bh = Math.max(1, Math.floor(cssH * dpr));
    width = cssW;
    height = cssH;
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw;
      canvas.height = bh;
    }
    if (!userView && didAutoFit && width > 40 && height > 40 && (prevW !== width || prevH !== height)) {
      fit();
    }
    scheduleDraw();
  }

  function fit(pad = 52) {
    const vis = nodes.filter(isVisible);
    if (!vis.length) return;
    let x0 = Infinity,
      y0 = Infinity,
      x1 = -Infinity,
      y1 = -Infinity;
    for (let i = 0; i < vis.length; i++) {
      const n = vis[i];
      x0 = Math.min(x0, n.x - n.r);
      y0 = Math.min(y0, n.y - n.r);
      x1 = Math.max(x1, n.x + n.r);
      y1 = Math.max(y1, n.y + n.r);
    }
    const bw = Math.max(1, x1 - x0);
    const bh = Math.max(1, y1 - y0);
    const k = clamp(
      Math.min((width - pad * 2) / bw, (height - pad * 2) / bh),
      0.12,
      8
    );
    const tx = width / 2 - k * ((x0 + x1) / 2);
    const ty = height / 2 - k * ((y0 + y1) / 2);
    const t = d3.zoomIdentity.translate(tx, ty).scale(k);
    const sel = d3.select(canvas);
    if (reduceMotion) sel.call(zoom.transform, t);
    else sel.transition().duration(280).call(zoom.transform, t);
  }

  function maybeAutoFit() {
    if (didAutoFit || userView || destroyed) return;
    if (sim.alpha() > 0.04) return;
    if (width < 40 || height < 40) return;
    didAutoFit = true;
    fit();
  }

  const origTick = onTick;
  sim.on("tick", () => {
    origTick();
    maybeAutoFit();
  });

  function setPaused(p) {
    paused = !!p;
    if (paused) sim.stop();
    else wake(0.14);
    scheduleDraw();
  }

  ingest(opts.nodes || [], opts.edges || [], new Map());
  resize();
  wake(1);

  return {
    setData({ nodes: ns, edges: es } = {}) {
      ingest(ns || [], es || [], new Map(nodes.map((n) => [n.id, n])));
      fx = [];
      particles = [];
      didAutoFit = false;
      wake(1);
    },
    clearActivity() {
      bots.clear();
      fx = [];
      particles = [];
      seenEvents.clear();
      for (let i = 0; i < nodes.length; i++) nodes[i].heats = [];
      scheduleDraw();
    },
    setActivity(events) {
      const list = Array.isArray(events) ? events.slice() : [];
      list.sort((a, b) => (a.t || 0) - (b.t || 0));
      bots.clear();
      fx = [];
      particles = [];
      seenEvents.clear();
      for (let i = 0; i < nodes.length; i++) nodes[i].heats = [];
      const now = performance.now();
      const juiceFrom = Math.max(0, list.length - 8);
      for (let i = 0; i < list.length; i++) {
        applyEvent(list[i], now, i >= juiceFrom);
      }
      scheduleDraw();
    },
    appendActivity(event) {
      applyEvent(event, performance.now(), true);
    },
    resize,
    destroy() {
      destroyed = true;
      sim.stop();
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      d3.select(canvas).on(".zoom", null);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      canvas.removeEventListener("dblclick", onDblClick);
      window.removeEventListener("keydown", onKey);
      if (mq.removeEventListener) mq.removeEventListener("change", onMq);
      else if (mq.removeListener) mq.removeListener(onMq);
      if (ro) ro.disconnect();
      if (tip.parentNode) tip.parentNode.removeChild(tip);
      canvas.classList.remove("graph-canvas", "is-panning", "is-dragging", "is-over-node");
    },
    fit,
    setPaused,
    highlightSession(sid) {
      focusSid = sid || null;
      scheduleDraw();
    },
  };
}
