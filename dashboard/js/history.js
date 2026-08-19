(() => {
  const $ = (id) => document.getElementById(id);
  const live = $("liveBadge");
  const meta = $("meta");
  const note = $("note");
  const sessEl = $("sessions");
  const evEl = $("events");
  const mutOnly = $("mutOnly");

  let last = null;
  // Persist expand/collapse across polls (innerHTML would reopen details).
  const openById = new Map();

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, (Date.now() / 1000) - ts);
    if (s < 60) return `${Math.floor(s)}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    return `${Math.floor(s / 3600)}h`;
  }

  function kindBadge(k) {
    return `<span class="kind ${esc(k)}">${esc(k)}</span>`;
  }

  function compactBadge(e) {
    if (!e.from_compact) return "";
    const tip = e.compact_why || "harness compact";
    return `<span class="kind compact" title="${esc(tip)}">compact</span>`;
  }

  function renderSessions(sessions) {
    $("sessCount").textContent = sessions.length ? `(${sessions.length})` : "";
    if (!sessions.length) {
      sessEl.innerHTML = `<p class="empty">No chat_history seen yet. New or updating sessions appear here.</p>`;
      return;
    }
    sessEl.innerHTML = sessions
      .map((s) => {
        const mut = s.mutations || 0;
        return `<article class="sess">
          <div class="row">
            <strong>${esc(s.label)}</strong>
            ${kindBadge(s.last_kind || "baseline")}
          </div>
          <div class="id">${esc(s.session_id)}</div>
          <div class="row muted">
            <span>${s.records} rec · ${ago(s.mtime)} ago</span>
            <span>mut ${mut} · app ${s.appends || 0} · tail ${s.tails || 0}</span>
          </div>
        </article>`;
      })
      .join("");
  }

  function changeBlock(c) {
    const idx = c.index;
    const bits = [`<span class="op">${esc(c.op)}</span> [${idx}] ${esc(c.type || "")}`];
    if (c.key) bits.push(`<span class="muted">${esc(c.key)}</span>`);
    if (c.compact) bits.push(`<span class="kind compact">compact</span>`);
    const ch = [];
    if (c.old_chars != null || c.new_chars != null) {
      ch.push(`${c.old_chars ?? "—"} → ${c.new_chars ?? "—"} ch`);
    }
    let html = `<div class="chg"><div>${bits.join(" ")} ${ch.length ? "· " + esc(ch.join(" ")) : ""}</div>`;
    if (c.old_preview) html += `<p class="was">− ${esc(c.old_preview)}</p>`;
    if (c.new_preview) html += `<p class="now">+ ${esc(c.new_preview)}</p>`;
    html += `</div>`;
    return html;
  }

  function renderEvents(events) {
    const only = mutOnly.checked;
    const shown = events.filter((e) =>
      only ? e.kind === "mutate" || e.kind === "truncate" : e.kind !== "baseline"
    );
    $("evCount").textContent = shown.length ? `(${shown.length})` : "";
    if (!shown.length) {
      evEl.innerHTML = `<p class="empty">${
        only
          ? "No prefix mutations yet. Append-only updates are hidden."
          : "No updates since start."
      }</p>`;
      return;
    }
    evEl.innerHTML = shown
      .map((e) => {
        const br =
          e.cache_break_index == null
            ? `prefix ${e.prefix}/${e.old_n}`
            : `break @ ${e.cache_break_index} (was ${e.old_n} → ${e.new_n})`;
        const chg = (e.changes || []).filter((c) =>
          only ? c.op !== "insert" || e.kind !== "append" : true
        );
        const body = chg.length
          ? chg.map(changeBlock).join("")
          : `<p class="muted">no record-level edits</p>`;
        const id = e.id;
        const opened = openById.has(id) ? openById.get(id) : false;
        const why = e.from_compact && e.compact_why
          ? `<div class="compact-why">${esc(e.compact_why)}</div>`
          : "";
        return `<details class="ev" data-id="${esc(id)}" ${opened ? "open" : ""}>
          <summary>
            <div class="row">
              <span>${kindBadge(e.kind)} ${compactBadge(e)} <strong>${esc(e.label || e.session_id)}</strong></span>
              <span class="muted">${ago(e.ts)}</span>
            </div>
            <div class="muted">${esc(br)} · edit ${e.changed || 0} · del ${e.removed || 0} · +${e.added || 0}</div>
            ${why}
          </summary>
          ${body}
        </details>`;
      })
      .join("");
    evEl.querySelectorAll("details.ev").forEach((el) => {
      el.addEventListener("toggle", () => {
        const id = Number(el.dataset.id);
        if (!Number.isNaN(id)) openById.set(id, el.open);
      });
    });
  }

  function paint(data) {
    last = data;
    live.textContent = "live";
    live.className = "badge ok";
    note.textContent = data.note || "";
    const up = data.uptime_s != null ? `${Math.floor(data.uptime_s)}s up` : "";
    meta.textContent = `${data.sessions?.length || 0} watched · ${up}`;
    renderSessions(data.sessions || []);
    renderEvents(data.events || []);
  }

  async function poll() {
    try {
      const r = await fetch("/api/history", { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      paint(await r.json());
    } catch (err) {
      live.textContent = "offline";
      live.className = "badge err";
      meta.textContent = String(err.message || err);
    }
  }

  mutOnly.addEventListener("change", () => {
    if (last) renderEvents(last.events || []);
  });

  poll();
  setInterval(poll, 800);
})();
