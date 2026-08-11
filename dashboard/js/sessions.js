/** Session picker + switch POST */
import { $ } from './fmt.js';

let _sessionSwitching = false;
let _lastSessionOptsKey = "";
/** @type {null | (() => Promise<void>)} */
let _pollRef = null;

export function bindPoll(fn) {
  _pollRef = fn;
}

function fillSessionSelect(state) {
  const sel = $("sessionSelect");
  const pinHint = $("sessionPinHint");
  if (!sel) return;
  const sessions = state.sessions || [];
  const current = state.session_id || "";
  const pinned = state.pinned_session_id || null;
  const follow = state.follow_active !== false && !pinned;
  if (pinHint) {
    pinHint.hidden = !pinned;
    pinHint.textContent = pinned ? "pinned" : "";
  }
  // Include age so labels refresh as time passes (bucketed to limit churn)
  const ageBucket = (s) => {
    const a = s.age_seconds != null ? Number(s.age_seconds) : 0;
    if (a < 60) return "s";
    if (a < 3600) return "m" + Math.floor(a / 60);
    if (a < 86400) return "h" + Math.floor(a / 3600);
    return "d" + Math.floor(a / 86400);
  };
  const key =
    sessions.map((s) => s.session_id + (s.active ? "A" : "") + ageBucket(s) + (s.label || "")).join("|") +
    ">" +
    current +
    ">" +
    (pinned || "") +
    ">" +
    (follow ? "F" : "P");
  if (key === _lastSessionOptsKey && sel.options.length > 1) {
    if (sel.value !== current && !_sessionSwitching && !follow) {
      for (const o of sel.options) {
        if (o.value === current) {
          sel.value = current;
          break;
        }
      }
    }
    return;
  }
  _lastSessionOptsKey = key;
  const prevFocus = document.activeElement === sel;
  const frag = document.createDocumentFragment();
  const followOpt = document.createElement("option");
  followOpt.value = "";
  followOpt.textContent = follow ? "● Follow active (auto)" : "○ Follow active (auto)";
  frag.appendChild(followOpt);

  const active = sessions.filter((s) => s.active);
  const other = sessions.filter((s) => !s.active);
  function addGroup(label, list) {
    if (!list.length) return;
    const og = document.createElement("optgroup");
    og.label = label;
    for (const s of list) {
      const o = document.createElement("option");
      o.value = s.session_id;
      const mark = s.session_id === current ? "▸ " : "";
      const title = (s.title || s.label || "").trim() || (s.session_id || "").slice(0, 8);
      const age = s.age_label ? ` · ${s.age_label}` : "";
      // Title + last-active age (no hash in the main line)
      o.textContent = `${mark}${title}${age}`;
      const tipParts = [
        s.session_id || "",
        s.cwd ? "cwd: " + s.cwd : "",
        s.last_active_at ? "last active: " + s.last_active_at : "",
        s.path || "",
      ].filter(Boolean);
      o.title = tipParts.join("\n");
      og.appendChild(o);
    }
    frag.appendChild(og);
  }
  addGroup("Active", active);
  addGroup("Recent", other);
  sel.innerHTML = "";
  sel.appendChild(frag);
  if (pinned || !follow) {
    sel.value = current;
    if (sel.value !== current && current) {
      const o = document.createElement("option");
      o.value = current;
      const match = sessions.find((s) => s.session_id === current);
      const t = (match && (match.title || match.label)) || (current || "").slice(0, 8);
      const age = match && match.age_label ? ` · ${match.age_label}` : "";
      o.textContent = `▸ ${t}${age} (current)`;
      sel.appendChild(o);
      sel.value = current;
    }
  } else {
    sel.value = "";
  }
  if (prevFocus) sel.focus();
}

async function switchSession(sessionId) {
  _sessionSwitching = true;
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
    }
    _lastSessionOptsKey = "";
    if (_pollRef) await _pollRef();
  } catch (e) {
    console.warn(e);
  } finally {
    _sessionSwitching = false;
  }
}


export { fillSessionSelect, switchSession };
