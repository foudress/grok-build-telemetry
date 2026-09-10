/** Session picker + switch POST */
import { $ } from './fmt.js';

let _sessionSwitching = false;
let _lastSessionOptsKey = "";
let _fullPickerLoaded = false;
/** @type {null | (() => Promise<void>)} */
let _pollRef = null;
/** @type {null | Record<string, any>} */
let _lastStateRef = null;

function beginViewLoad() {
  document.body.classList.add("is-loading");
  const el = document.getElementById("viewLoader");
  if (el) el.hidden = false;
}

function endViewLoad() {
  document.body.classList.remove("is-loading");
  const el = document.getElementById("viewLoader");
  if (el) el.hidden = true;
}

export function bindPoll(fn) {
  _pollRef = fn;
}

function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function clipTitle(s, n) {
  const t = String(s || "").trim();
  if (t.length <= n) return t;
  return t.slice(0, n - 1) + "…";
}

function ensureSessionDd(sel) {
  let wrap = $("sessionDd");
  if (wrap) return wrap;
  wrap = document.createElement("div");
  wrap.id = "sessionDd";
  wrap.className = "session-dd";
  wrap.innerHTML = `<button type="button" id="sessionDdBtn" class="session-dd-btn session-picker-face"></button>
    <div id="sessionDdMenu" class="session-dd-menu" hidden></div>`;
  sel.classList.add("session-select-native");
  sel.insertAdjacentElement("afterend", wrap);
  const btn = $("sessionDdBtn");
  const menu = $("sessionDdMenu");
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    menu.hidden = !menu.hidden;
    if (!menu.hidden) {
      const br = wrap.getBoundingClientRect();
      menu.style.left = "0";
      menu.style.right = "auto";
      const mw = Math.min(420, window.innerWidth - 16);
      if (br.left + mw > window.innerWidth - 8) {
        menu.style.left = "auto";
        menu.style.right = "0";
      }
      // Pinned /telemetry startup keeps /api/state slim; load the full
      // recent list only when the user opens the picker.
      if (!_fullPickerLoaded && _lastStateRef && _lastStateRef.pinned_session_id) {
        _fullPickerLoaded = true;
        fetch("/api/sessions?_=" + Date.now())
          .then((r) => (r.ok ? r.json() : null))
          .then((data) => {
            if (!data || !Array.isArray(data.sessions) || !_lastStateRef) return;
            _lastSessionOptsKey = "";
            fillSessionSelect({ ..._lastStateRef, sessions: data.sessions });
            const m = $("sessionDdMenu");
            if (m) m.hidden = false;
          })
          .catch(() => { _fullPickerLoaded = false; });
      }
    }
  });
  document.addEventListener("click", (ev) => {
    if (!wrap.contains(ev.target)) menu.hidden = true;
  });
  return wrap;
}

function fillSessionSelect(state) {
  const sel = $("sessionSelect");
  if (!sel) return;
  _lastStateRef = state;
  if (!state.pinned_session_id) _fullPickerLoaded = false;
  const sessions = (state.sessions || []).slice();
  const current = state.session_id || "";
  const pinned = state.pinned_session_id || null;
  const follow = state.follow_active !== false && !pinned;
  const ageBucket = (s) => {
    const a = s.age_seconds != null ? Number(s.age_seconds) : 0;
    if (a < 60) return "s";
    if (a < 3600) return "m" + Math.floor(a / 60);
    if (a < 86400) return "h" + Math.floor(a / 3600);
    return "d" + Math.floor(a / 86400);
  };
  const key =
    sessions.map((s) => s.session_id + (s.active ? "A" : "") + ageBucket(s) + (s.label || "")).join("|") +
    ">" + current + ">" + (pinned || "") + ">" + (follow ? "F" : "P");
  ensureSessionDd(sel);
  const btn = $("sessionDdBtn");
  const menu = $("sessionDdMenu");
  const curRow = sessions.find((s) => s.session_id === current);
  const curTitle = (curRow && (curRow.title || curRow.label)) || current.slice(0, 8);
  if (btn) {
    btn.textContent = follow
      ? `● ${clipTitle(curTitle, 48)}`
      : clipTitle((curRow && (curRow.title || curRow.label)) || "Session", 52);
    btn.title = curTitle;
  }
  const optsHasCurrent = !current || [...sel.options].some((o) => o.value === current);
  if (key === _lastSessionOptsKey && sel.options.length > 1 && optsHasCurrent) {
    if (sel.value !== current) sel.value = current || "";
    return;
  }
  _lastSessionOptsKey = key;

  const frag = document.createDocumentFragment();
  const followOpt = document.createElement("option");
  followOpt.value = "";
  followOpt.textContent = follow ? "● Follow active (auto)" : "○ Follow active (auto)";
  frag.appendChild(followOpt);

  if (current && !sessions.some((s) => s.session_id === current)) {
    const curRow = {
      session_id: current,
      label: current,
      title: current,
      active: true,
      last_active_epoch: Date.now() / 1000,
    };
    sessions.unshift(curRow);
  }
  const byRecent = (a, b) =>
    (Number(b.last_active_epoch) || Number(b.mtime) || 0)
    - (Number(a.last_active_epoch) || Number(a.mtime) || 0);
  const active = sessions.filter((s) => s.active).sort(byRecent);
  const other = sessions.filter((s) => !s.active).sort(byRecent);
  function addGroup(label, list) {
    if (!list.length) return;
    const og = document.createElement("optgroup");
    og.label = label;
    for (const s of list) {
      const o = document.createElement("option");
      o.value = s.session_id;
      const title = (s.title || s.label || "").trim() || (s.session_id || "").slice(0, 8);
      const age = s.age_label ? ` · ${s.age_label}` : "";
      o.textContent = `${s.session_id === current ? "▸ " : ""}${title}${age}`;
      og.appendChild(o);
    }
    frag.appendChild(og);
  }
  addGroup("Active", active);
  addGroup("Recent", other);
  sel.innerHTML = "";
  sel.appendChild(frag);
  sel.value = current || "";

  if (menu) {
    const bits = [];
    bits.push(`<button type="button" class="session-dd-item${follow ? " is-on" : ""}" data-sid="">${follow ? "●" : "○"} Follow active (most recent)</button>`);
    const grp = (name, list) => {
      if (!list.length) return;
      bits.push(`<div class="session-dd-group">${name}</div>`);
      for (const s of list) {
        const title = (s.title || s.label || "").trim() || s.session_id.slice(0, 8);
        const age = s.age_label ? ` · ${s.age_label}` : "";
        const on = s.session_id === current ? " is-on" : "";
        bits.push(`<button type="button" class="session-dd-item${on}" data-sid="${escHtml(s.session_id)}" title="${escHtml(title)}">${escHtml(title)}${escHtml(age)}</button>`);
      }
    };
    grp("Active", active);
    grp("Recent", other);
    menu.innerHTML = bits.join("");
    menu.querySelectorAll("[data-sid]").forEach((el) => {
      el.addEventListener("click", () => {
        const sid = el.getAttribute("data-sid") || "";
        menu.hidden = true;
        sel.value = sid;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
  }
}

function _wantSid(sessionId) {
  return sessionId ? String(sessionId) : "";
}

function _optimisticPicker(sessionId) {
  const sid = _wantSid(sessionId);
  const sel = $("sessionSelect");
  if (sel) {
    // First /telemetry load used to leave the native <select> on "loading…"
    // until poll painted — pin POST + sid-mismatch retries never called
    // fillSessionSelect, so the header stayed stuck until a full reload.
    ensureSessionDd(sel);
    if (sid && ![...sel.options].some((o) => o.value === sid)) {
      const o = document.createElement("option");
      o.value = sid;
      o.textContent = sid.slice(0, 8);
      sel.appendChild(o);
    }
    sel.value = sid;
  }
  const btn = $("sessionDdBtn");
  if (!btn) return;
  const sessions = (_lastStateRef && _lastStateRef.sessions) || [];
  const row = sessions.find((s) => String(s.session_id) === sid);
  const title = (row && (row.title || row.label)) || (sid ? sid.slice(0, 8) : "Follow active");
  btn.textContent = sid ? clipTitle(title, 52) : `● ${clipTitle(title, 48)}`;
  btn.title = title;
}

async function switchSession(sessionId) {
  const want = _wantSid(sessionId);
  _sessionSwitching = true;
  window.__sessionSwitching = true;
  window.__pendingSid = want || null;
  _optimisticPicker(sessionId);
  // Force a fresh /api/state body after pin/follow change.
  try { window.__clearStateEtag && window.__clearStateEtag(); } catch { /* ignore */ }
  beginViewLoad();
  try {
    const r = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId || null }),
    });
    const res = await r.json();
    if (!res.ok) {
      console.warn("session switch failed", res);
      $("liveBadge").textContent = "switch err";
      $("liveBadge").className = "badge warn";
      window.__pendingSid = null;
      endViewLoad();
      return;
    }
    // Prefer server-resolved id so pending checks match /api/state.
    if (res.session_id) window.__pendingSid = String(res.session_id);
    else if (!want) window.__pendingSid = null;
    _lastSessionOptsKey = "";
    if (_pollRef) {
      await _pollRef();
      // Parallel / busy poll used to return without paint; keep retrying
      // until /api/state session_id matches (poll clears __pendingSid).
      for (let i = 0; i < 40 && window.__pendingSid; i++) {
        await new Promise((resolve) => setTimeout(resolve, 50));
        try { window.__clearStateEtag && window.__clearStateEtag(); } catch { /* ignore */ }
        await _pollRef();
      }
    }
  } catch (e) {
    console.warn(e);
    window.__pendingSid = null;
  } finally {
    _sessionSwitching = false;
    window.__sessionSwitching = false;
    // Keep __pendingSid until poll() paints matching session_id so the
    // header picker does not stay on the previous row after D/W/M open.
    endViewLoad();
  }
}


export { fillSessionSelect, switchSession };
