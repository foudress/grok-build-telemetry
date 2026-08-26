import { createGraph } from "./graph-engine.js";

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function folderLabel(root) {
  const t = String(root || "").replace(/[\\/]+$/, "");
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  return (i >= 0 ? t.slice(i + 1) : t) || root || "project";
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

function baseName(p) {
  const s = String(p || "").replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

/** $ only if discover already put a number on the row — never fetch hierarchy. */
function costLabel(row) {
  const v = row.cost ?? row.usd ?? row.cost_usd ?? row.official_cost;
  if (v == null || v === "") return "";
  if (typeof v === "number" && Number.isFinite(v)) {
    if (v === 0) return "$0";
    if (Math.abs(v) < 0.01) return "$" + v.toFixed(4);
    return "$" + v.toFixed(2);
  }
  const t = String(v).trim();
  if (!t) return "";
  return t.startsWith("$") ? t : "$" + t;
}

function setBadge(el, kind, text) {
  if (!el) return;
  el.textContent = text;
  el.className = "badge" + (kind ? " " + kind : "");
}

async function getJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  let data = {};
  try {
    data = await r.json();
  } catch {
    data = {};
  }
  return { ok: r.ok, status: r.status, data };
}

function rootFromUrl() {
  return new URLSearchParams(location.search).get("root") || "";
}

function nodeMatches(n, needle) {
  const path = String(n.path || "").toLowerCase();
  const id = String(n.id || "").toLowerCase();
  return path.includes(needle) || id.includes(needle) || baseName(path).includes(needle);
}

function filterGraph(graph, q) {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const needle = String(q || "").trim().toLowerCase();
  if (!needle) return { nodes, edges, hits: nodes.length, filtered: false };
  const hit = nodes.filter((n) => nodeMatches(n, needle));
  if (!hit.length) return { nodes, edges, hits: 0, filtered: false };
  const keep = new Set(hit.map((n) => n.id));
  const byPath = new Map();
  for (const n of nodes) {
    if (n.kind === "cluster" && n.path) byPath.set(n.path, n);
  }
  for (const n of hit) {
    let d = n.dir || "";
    while (d) {
      const cluster = byPath.get(d);
      if (cluster) keep.add(cluster.id);
      const i = Math.max(d.lastIndexOf("/"), d.lastIndexOf("\\"));
      d = i >= 0 ? d.slice(0, i) : "";
    }
  }
  return {
    nodes: nodes.filter((n) => keep.has(n.id)),
    edges: edges.filter((e) => keep.has(e.src) && keep.has(e.dst)),
    hits: hit.length,
    filtered: true,
  };
}

function emptyReason(status, data) {
  if (status === 403) return "cwd not in sessions";
  if (status === 400) return "cwd not in sessions";
  const err = data && data.error;
  if (err === "root not allowlisted" || err === "missing root") return "cwd not in sessions";
  if (status && status >= 400) return "cwd not in sessions";
  return "no files";
}

/* ── Listing ─────────────────────────────────────────────────── */

async function showListing() {
  document.body.classList.add("view-list");
  document.body.classList.remove("view-project");
  $("listing").hidden = false;
  $("project").hidden = true;
  const box = $("projects");
  try {
    const { ok, data } = await getJson("/api/graph/projects");
    if (!ok) throw new Error("projects");
    const projects = data.projects || [];
    $("projCount").textContent = projects.length ? `(${projects.length})` : "";
    setBadge($("listBadge"), "ok", "live");
    if (!projects.length) {
      box.innerHTML = `<p class="empty">No session workspaces yet. Open a Grok chat whose cwd is a repo.</p>`;
      return;
    }
    box.innerHTML = projects
      .map((p) => {
        const href = "/graph?root=" + encodeURIComponent(p.root);
        const n = p.session_count || 0;
        const sess = n === 1 ? "1 session" : `${n} sessions`;
        return `<a class="proj" href="${esc(href)}">
          <div class="name">${esc(p.label || folderLabel(p.root))}</div>
          <div class="path">${esc(p.root)}</div>
          <div class="row"><span>${esc(sess)}</span><span>${esc(p.age_label || "—")}</span></div>
        </a>`;
      })
      .join("");
  } catch {
    setBadge($("listBadge"), "err", "offline");
    box.innerHTML = `<p class="empty">Could not load projects.</p>`;
  }
}

/* ── Project view ────────────────────────────────────────────── */

let api = null;
let root = "";
let fullGraph = { nodes: [], edges: [] };
let sessions = [];
let allEvents = [];
let selectedSid = null;
let paused = false;
let seenKeys = new Set();
let pollTimer = null;
let pollBusy = false;
let searchTimer = 0;

const REPLAY_SPEEDS = [1, 4, 16];
const REPLAY_INDEX_DT = 0.4;
const REPLAY_MAX_GAP = 1.25; // 1× idle cap — long sessions stay watchable
let replayEvents = [];
let replayUseIndex = true;
let replayT0 = 0;
let replayT1 = 0;
let replayCursor = 0;
let replayNext = 0;
let replayPlaying = false;
let replayMode = false;
let replaySpeed = 1;
let replayRaf = 0;
let replayLastMs = 0;
let replayAcc = 0;
let replayBooting = false;

function visibleEvents() {
  if (!selectedSid) return allEvents;
  return allEvents.filter((e) => String(e.sid || "") === selectedSid);
}

function ingestNew() {
  if (!api) return;
  const vis = new Set(visibleEvents().map(eventKey));
  for (const ev of allEvents) {
    const k = eventKey(ev);
    const fresh = !seenKeys.has(k);
    seenKeys.add(k);
    if (fresh && vis.has(k)) api.appendActivity(ev);
  }
}

function paintMeta() {
  const n = sessions.length;
  const sess = n === 1 ? "1 sess" : `${n} sess`;
  $("projMeta").innerHTML = `<b>${esc(folderLabel(root))}</b> · ${esc(sess)}`;
  $("sessCount").textContent = n ? `(${n})` : "";
}

function paintSessions() {
  const box = $("sessions");
  if (!sessions.length) {
    box.innerHTML = `<p class="empty">No sessions for this cwd.</p>`;
    return;
  }
  box.innerHTML = sessions
    .map((s) => {
      const title = s.title || s.label || s.session_id || "session";
      const sub = String(s.session_kind || "").toLowerCase() === "subagent";
      const kind = sub
        ? `<span class="kind sub">↳ sub</span>`
        : s.session_kind
          ? `<span class="kind">${esc(s.session_kind)}</span>`
          : "";
      const money = costLabel(s);
      const cost = money ? `<span class="cost">${esc(money)}</span>` : "";
      const on = selectedSid && s.session_id === selectedSid ? " is-on" : "";
      return `<article class="sess${on}" role="button" tabindex="0" data-sid="${esc(s.session_id)}">
        <div class="row">
          <span class="title">${esc(title)}</span>
          <span class="age">${esc(s.age_label || "—")}</span>
        </div>
        <div class="sub">
          <span>${kind} ${cost}</span>
          <button type="button" class="open-tel" data-open="${esc(s.session_id)}">open in telemetry</button>
        </div>
      </article>`;
    })
    .join("");
}

function selectSession(sid) {
  selectedSid = selectedSid === sid ? null : sid;
  if (api) {
    api.highlightSession(selectedSid);
    if (replayMode) {
      setupReplayRange();
      if (!replayPool().length) stopReplay();
      else applyReplayPrefix(replayCursor);
    } else {
      api.setActivity(visibleEvents());
    }
  }
  paintSessions();
}

async function openTelemetry(sid) {
  try {
    await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid }),
    });
  } catch {
    /* still navigate — picker can recover */
  }
  location.href = "/";
}

function updatePauseBtn() {
  const btn = $("pause");
  btn.textContent = paused ? "Resume" : "Pause";
  btn.classList.toggle("on", paused);
  btn.setAttribute("aria-pressed", paused ? "true" : "false");
}

function applySearch() {
  const q = $("search").value;
  const chip = $("matchChip");
  if (!api) return;
  const next = filterGraph(fullGraph, q);
  api.setData(next);
  if (replayMode) applyReplayPrefix(replayCursor);
  else api.setActivity(visibleEvents());
  if (selectedSid) api.highlightSession(selectedSid);
  if (!String(q || "").trim()) {
    chip.hidden = true;
    chip.textContent = "";
  } else if (next.hits === 0) {
    chip.hidden = false;
    chip.textContent = "no matches";
  } else {
    chip.hidden = false;
    chip.textContent = next.hits === 1 ? "1 match" : `${next.hits} matches`;
  }
}

function setEmpty(msg) {
  const el = $("emptyOverlay");
  if (!msg) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = msg;
}

async function fetchActivity() {
  if (replayMode) return false;
  const url = "/api/graph/activity?root=" + encodeURIComponent(root);
  const { ok, status, data } = await getJson(url);
  if (!ok) {
    setBadge($("liveBadge"), "err", status === 403 ? "blocked" : "offline");
    return false;
  }
  setBadge($("liveBadge"), "ok", "live");
  allEvents = Array.isArray(data.events) ? data.events : [];
  ingestNew();
  return true;
}

function startPoll() {
  stopPoll();
  if (replayMode) return;
  pollTimer = setInterval(async () => {
    if (pollBusy || document.hidden || replayMode) return;
    pollBusy = true;
    try {
      await fetchActivity();
    } finally {
      pollBusy = false;
    }
  }, 1000);
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function replayPool() {
  if (!selectedSid) return replayEvents;
  return replayEvents.filter((e) => String(e.sid || "") === selectedSid);
}

function setupReplayRange() {
  const pool = replayPool();
  const el = $("scrub");
  const play = $("play");
  const speed = $("speed");
  if (!pool.length) {
    replayUseIndex = true;
    replayT0 = 0;
    replayT1 = 0;
    replayCursor = 0;
    replayNext = 0;
    if (el) {
      el.min = "0";
      el.max = "0";
      el.value = "0";
      el.step = "1";
      el.disabled = true;
    }
    if (play) {
      play.disabled = true;
      play.classList.remove("on");
      play.textContent = "Play";
      play.removeAttribute("aria-pressed");
      play.title = "no replay";
    }
    if (speed) speed.disabled = true;
    return;
  }
  const t0 = +pool[0].t || 0;
  const t1 = +pool[pool.length - 1].t || 0;
  replayUseIndex = !(t1 > t0);
  replayT0 = replayUseIndex ? 0 : t0;
  replayT1 = replayUseIndex ? pool.length : t1;
  if (replayCursor < replayT0 || replayCursor > replayT1) {
    replayCursor = replayT0;
    replayNext = 0;
  }
  if (el) {
    el.disabled = false;
    el.min = String(replayT0);
    el.max = String(replayT1);
    el.step = replayUseIndex ? "1" : "any";
    el.value = String(replayCursor);
  }
  if (play) play.disabled = false;
  if (speed) speed.disabled = false;
  updateReplayBtns();
}

function updateReplayBtns() {
  const play = $("play");
  const speed = $("speed");
  if (play && !play.disabled) {
    play.textContent = replayPlaying ? "Playing" : "Replay";
    play.classList.toggle("on", replayPlaying);
    play.setAttribute("aria-pressed", replayPlaying ? "true" : "false");
    play.title = replayPlaying ? "Pause replay" : replayMode ? "Resume replay" : "Replay";
  }
  if (speed) {
    speed.textContent = replaySpeed + "×";
    speed.title = "Replay speed " + replaySpeed + "×";
  }
}

function syncScrubEl() {
  const el = $("scrub");
  if (el && !el.disabled) el.value = String(replayCursor);
}

function paintReplayBadge() {
  const el = $("liveBadge");
  setBadge(el, "replay", "replay");
  if (el) el.title = "Return to live";
}

function applyReplayPrefix(cursor) {
  const pool = replayPool();
  let prefix;
  if (replayUseIndex) {
    const n = Math.max(0, Math.min(pool.length, Math.round(cursor)));
    prefix = pool.slice(0, n);
  } else {
    prefix = [];
    for (let i = 0; i < pool.length; i++) {
      if ((+pool[i].t || 0) <= cursor + 1e-9) prefix.push(pool[i]);
      else break;
    }
  }
  replayNext = prefix.length;
  if (!api) return;
  api.setActivity(prefix);
}

function enterReplayMode() {
  if (replayMode) return;
  replayMode = true;
  stopPoll();
  paintReplayBadge();
}

function pauseReplay() {
  replayPlaying = false;
  if (replayRaf) {
    cancelAnimationFrame(replayRaf);
    replayRaf = 0;
  }
  updateReplayBtns();
}

function stopReplay() {
  replayPlaying = false;
  replayMode = false;
  if (replayRaf) {
    cancelAnimationFrame(replayRaf);
    replayRaf = 0;
  }
  const badge = $("liveBadge");
  if (badge) badge.title = "";
  updateReplayBtns();
  if (api) api.setActivity(visibleEvents());
  setBadge($("liveBadge"), "ok", "live");
  startPoll();
  fetchActivity();
}

function replayTick(now) {
  if (!replayPlaying) return;
  const pool = replayPool();
  const dt = Math.min(0.25, Math.max(0, (now - replayLastMs) / 1000));
  replayLastMs = now;
  if (replayUseIndex) {
    replayAcc += dt * replaySpeed;
    while (replayAcc >= REPLAY_INDEX_DT && replayNext < pool.length) {
      replayAcc -= REPLAY_INDEX_DT;
      if (api) api.appendActivity(pool[replayNext]);
      replayNext += 1;
    }
    replayCursor = replayNext;
  } else {
    let remain = dt * replaySpeed;
    while (remain > 0 && replayNext < pool.length) {
      const nextT = +pool[replayNext].t || 0;
      const gap = Math.max(0, nextT - replayCursor);
      const wait = Math.min(gap, REPLAY_MAX_GAP);
      if (remain < wait) {
        replayCursor += wait ? (remain / wait) * gap : 0;
        remain = 0;
        break;
      }
      remain -= wait;
      replayCursor = nextT;
      if (api) api.appendActivity(pool[replayNext]);
      replayNext += 1;
    }
    if (replayNext >= pool.length) replayCursor = replayT1;
  }
  syncScrubEl();
  if (replayNext >= pool.length) {
    stopReplay();
    return;
  }
  replayRaf = requestAnimationFrame(replayTick);
}

async function loadReplay() {
  const { ok, data } = await getJson("/api/graph/replay?root=" + encodeURIComponent(root));
  replayEvents = ok && Array.isArray(data.events) ? data.events.slice() : [];
  replayEvents.sort((a, b) => (+a.t || 0) - (+b.t || 0));
  replayCursor = 0;
  replayNext = 0;
  setupReplayRange();
}

async function beginReplay() {
  if (replayPlaying || replayBooting) return;
  replayBooting = true;
  try {
    if (!replayMode) await loadReplay();
    const pool = replayPool();
    if (!pool.length) {
      setupReplayRange();
      return;
    }
    if (replayNext >= pool.length) {
      replayCursor = replayT0;
      replayNext = 0;
      replayAcc = 0;
      if (api) api.setActivity([]);
    }
    enterReplayMode();
    replayPlaying = true;
    replayLastMs = performance.now();
    replayAcc = 0;
    updateReplayBtns();
    replayRaf = requestAnimationFrame(replayTick);
  } finally {
    replayBooting = false;
  }
}

async function rescan() {
  const btn = $("rescan");
  btn.disabled = true;
  try {
    const r = await fetch("/api/graph/rescan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      setEmpty(emptyReason(r.status, data));
      return;
    }
    fullGraph = { nodes: data.nodes || [], edges: data.edges || [] };
    if (!fullGraph.nodes.length) setEmpty("no files");
    else setEmpty("");
    if (api) applySearch();
    else mountEngine(fullGraph);
  } catch {
    setBadge($("liveBadge"), "err", "offline");
  } finally {
    btn.disabled = false;
  }
}

function mountEngine(graph) {
  if (api) {
    api.destroy();
    api = null;
  }
  const canvas = $("g");
  if (!globalThis.d3) {
    setEmpty("missing d3");
    return;
  }
  api = createGraph(canvas, { nodes: graph.nodes || [], edges: graph.edges || [] });
  if (replayMode) applyReplayPrefix(replayCursor);
  else api.setActivity(visibleEvents());
  if (selectedSid) api.highlightSession(selectedSid);
  updatePauseBtn();
}

function bindProject() {
  $("fit").addEventListener("click", () => api && api.fit());
  $("pause").addEventListener("click", () => {
    if (!api) return;
    paused = !paused;
    api.setPaused(paused);
    updatePauseBtn();
  });
  $("rescan").addEventListener("click", () => rescan());
  $("play").addEventListener("click", () => {
    if (replayPlaying) pauseReplay();
    else beginReplay();
  });
  $("speed").addEventListener("click", () => {
    const i = REPLAY_SPEEDS.indexOf(replaySpeed);
    replaySpeed = REPLAY_SPEEDS[(i + 1) % REPLAY_SPEEDS.length];
    updateReplayBtns();
  });
  $("scrub").addEventListener("pointerdown", () => {
    if (replayPlaying) pauseReplay();
  });
  $("scrub").addEventListener("input", () => {
    replayCursor = +$("scrub").value;
    enterReplayMode();
    applyReplayPrefix(replayCursor);
    updateReplayBtns();
  });
  $("liveBadge").addEventListener("click", () => {
    if (replayMode) stopReplay();
  });
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applySearch, 160);
  });
  $("sessions").addEventListener("click", (ev) => {
    const open = ev.target.closest("[data-open]");
    if (open) {
      ev.preventDefault();
      ev.stopPropagation();
      openTelemetry(open.getAttribute("data-open"));
      return;
    }
    const card = ev.target.closest("[data-sid]");
    if (card) selectSession(card.getAttribute("data-sid"));
  });
  $("sessions").addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const card = ev.target.closest("[data-sid]");
    if (!card || ev.target.closest("[data-open]")) return;
    ev.preventDefault();
    selectSession(card.getAttribute("data-sid"));
  });
  window.addEventListener("keydown", (ev) => {
    const tag = (ev.target && ev.target.tagName) || "";
    if (ev.key === "Escape") {
      const search = $("search");
      const hadSearch = search && search.value;
      const hadFocus = selectedSid;
      if (hadSearch) {
        search.value = "";
        applySearch();
      }
      if (hadFocus) {
        selectedSid = null;
        if (api) {
          api.highlightSession(null);
          if (replayMode) {
            setupReplayRange();
            applyReplayPrefix(replayCursor);
          } else {
            api.setActivity(allEvents);
          }
        }
        paintSessions();
      }
      if (hadSearch && tag === "INPUT") ev.preventDefault();
      return;
    }
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") return;
    // Engine already toggles pause on space — keep the label in sync, do not double-set.
    if (ev.key === " " && api) {
      paused = !paused;
      updatePauseBtn();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || replayMode) return;
    fetchActivity();
  });
}

async function showProject(nextRoot) {
  root = nextRoot;
  document.body.classList.add("view-project");
  document.body.classList.remove("view-list");
  $("listing").hidden = true;
  $("project").hidden = false;
  bindProject();

  const q = "root=" + encodeURIComponent(root);
  const [gRes, sRes, aRes, rRes] = await Promise.all([
    getJson("/api/graph?" + q),
    getJson("/api/graph/sessions?" + q),
    getJson("/api/graph/activity?" + q),
    getJson("/api/graph/replay?" + q),
  ]);

  if (!gRes.ok) {
    setBadge($("liveBadge"), "err", gRes.status === 403 ? "blocked" : "offline");
    setEmpty(emptyReason(gRes.status, gRes.data));
    sessions = [];
    paintMeta();
    paintSessions();
    return;
  }

  fullGraph = { nodes: gRes.data.nodes || [], edges: gRes.data.edges || [] };
  sessions = Array.isArray(sRes.data.sessions) ? sRes.data.sessions.slice() : [];
  sessions.sort((a, b) => (b.last_active_epoch || 0) - (a.last_active_epoch || 0));
  allEvents = Array.isArray(aRes.data.events) ? aRes.data.events : [];
  for (const ev of allEvents) seenKeys.add(eventKey(ev));
  replayEvents = rRes.ok && Array.isArray(rRes.data.events) ? rRes.data.events.slice() : [];
  replayEvents.sort((a, b) => (+a.t || 0) - (+b.t || 0));
  replayCursor = 0;
  replayNext = 0;

  paintMeta();
  paintSessions();
  if (!fullGraph.nodes.length) setEmpty("no files");
  else setEmpty("");
  mountEngine(fullGraph);
  setBadge($("liveBadge"), aRes.ok ? "ok" : "err", aRes.ok ? "live" : "offline");
  setupReplayRange();
  startPoll();
}

if (rootFromUrl()) showProject(rootFromUrl());
else showListing();
