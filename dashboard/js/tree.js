/** Round hierarchy tree rendering */
import {
  $,
  fmtTokens,
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
  shortPath,
  compressCwdText,
  shellInteresting,
  pickTokenizerTokens,
  eolTokenizerMeta,
} from './fmt.js';

function eventMs(e) {
  const n = Number(e && e.agent_ms);
  return Number.isFinite(n) ? n : 0;
}

/** User-owned Compact Out mass on a step (between-rounds — shown under User[N]). */
function userCompactPeel(step) {
  let t = 0;
  let u = 0;
  if (step && step.user_compact_out_tokens != null) {
    t = Math.round(Number(step.user_compact_out_tokens) || 0);
    u = Number(step.user_compact_out_usd) || 0;
    if (t > 0 || u > 0) return { t, u };
  }
  const est = step && step.estimate;
  if (est && est.user_compact_out_tokens != null) {
    t = Math.round(Number(est.user_compact_out_tokens) || 0);
    u = Number(est.user_compact_out_usd) || 0;
    if (t > 0 || u > 0) return { t, u };
  }
  for (const ch of (step && step.children) || []) {
    if (!ch || ch.kind !== "phase_harness") continue;
    for (const sub of ch.children || []) {
      if (sub && sub.kind === "compact_out_in" && String(sub.attribution || "") === "user") {
        t += Math.round(Number(sub.tokens_in || sub.context_delta) || 0);
        u += Number(sub.cost_in_usd) || 0;
      }
    }
  }
  return { t, u };
}

/**
 * Compact Out to subtract from Call/Harness display totals.
 * Reconstruct already excludes User-owned Compact Out from tokens_in /
 * harness_in_* and stamps user_compact_out_* — peeling again under-counts
 * (e.g. tools 9.7k shown under a 3.4k Harness header).
 * Only peel when parent totals still include that mass (legacy payloads).
 */
function userCompactPeelFromTotals(step) {
  const peel = userCompactPeel(step);
  if (!(peel.t > 0 || peel.u > 0)) return { t: 0, u: 0 };
  // Server stamp ⇒ Call/Harness In already exclude Compact Out.
  if (step && (step.user_compact_out_tokens != null
    || (step.estimate && step.estimate.user_compact_out_tokens != null))) {
    return { t: 0, u: 0 };
  }
  let dispT = 0;
  for (const ch of (step && step.children) || []) {
    if (!ch || ch.kind !== "phase_harness") continue;
    for (const sub of ch.children || []) {
      if (!sub || sub.to_user) continue;
      if (sub.kind === "compact_out_in" && String(sub.attribution || "") === "user") continue;
      if (["tool", "late_context", "hook", "llm_to_in", "compact_out_in"].includes(sub.kind)
        || sub.name) {
        dispT += Math.round(Number(sub.tokens_in || sub.context_delta) || 0);
      }
    }
  }
  const raw = Math.round(Number(step && (step.tokens_in ?? step.harness_in_tokens)) || 0);
  const full = dispT + peel.t;
  // Peel only when parent looks like it still includes Compact Out.
  if (Math.abs(raw - full) <= Math.abs(raw - dispT)) return peel;
  return { t: 0, u: 0 };
}

/** Peel user Compact Out from a phase_harness node only if its tokens_in still include it. */
function harnessPhaseDisplayIn(phase, money) {
  let peelT = 0;
  let peelU = 0;
  let dispT = 0;
  for (const sub of (phase && phase.children) || []) {
    if (!sub || sub.to_user) continue;
    const isUserCo = sub.kind === "compact_out_in" && String(sub.attribution || "") === "user";
    const tin = Math.round(Number(sub.tokens_in || sub.context_delta) || 0);
    const uin = Number(sub.cost_in_usd) || 0;
    if (isUserCo) {
      peelT += tin;
      peelU += uin;
      continue;
    }
    if (["tool", "late_context", "hook", "llm_to_in", "compact_out_in"].includes(sub.kind)
      || sub.name) {
      dispT += tin;
    }
  }
  const rawT = Math.round(Number(money && money.ti) || 0);
  const rawU = Number(money && money.ci) || 0;
  const full = dispT + peelT;
  const includes = peelT > 0 && Math.abs(rawT - full) <= Math.abs(rawT - dispT);
  return {
    inTok: Math.max(0, includes ? rawT - peelT : rawT),
    inUsd: Math.max(0, includes ? rawU - peelU : rawU),
  };
}

/** Always-visible hook row (display-only — not billed model In). */
function renderHookNode(h) {
  if (!h) return "";
  const names = (h.run_names || []).slice(0, 2).join(", ");
  const ev = String(h.event_name || "hook");
  const slot = String(h.slot || "").toLowerCase();
  const toUser = !!(h.to_user || h.display_only
    || slot === "user" || slot === "to_user"
    || ["stop", "session_stop", "agent_stop", "session_end", "user_prompt_submit"]
      .includes(ev.toLowerCase())
    || ev.toLowerCase().startsWith("user_prompt"));
  const msBit = h.elapsed_ms != null
    ? `<span class="muted">${h.elapsed_ms}ms</span>`
    : "";
  const gray = `${ev}${names ? " · " + names : ""}`;
  // Hooks never contribute to cost UI; keep a trailing muted cue only.
  const trail = toUser && slot !== "user" && !ev.toLowerCase().startsWith("user_prompt")
    ? `<span class="muted">→ user</span>`
    : `<span class="muted">· display</span>`;
  return `<div class="node hook-node" title="${esc(h.estimate_note || "hook_execution (not billed)")}">
    <span class="tag hook">hook</span>
    <span class="sum-gray" title="${esc(gray)}">${esc(gray)}</span>
    ${trail}
    ${msBit}
  </div>`;
}

/** Same tot as the System card header In (do not re-derive). */
function systemBoxPartsAndTotal(sp) {
  const rawParts = (sp && sp.parts || []).filter((p) => p && p.kind !== "hooks");
  const residualTok = Math.round(Number(sp && sp.message_residual_tokens) || 0);
  if (
    residualTok > 0
    && !rawParts.some((p) => p.kind === "tool_definitions" || p.kind === "tool_defs_message")
  ) {
    rawParts.push({
      kind: "tool_defs_message",
      label: "Tool definitions + Message",
      tokens: residualTok,
      tokenizer_tokens: residualTok,
    });
  }
  const partsTok = rawParts.reduce((s, p) => s + (Number(p.tokens ?? p.tokens_in) || 0), 0);
  const tot = (sp && (sp.message_residual_tokens != null || rawParts.length))
    ? partsTok
    : (sp && (sp.tokens_in ?? sp.logical_tokens ?? sp.uncached_est));
  return { rawParts, tot };
}

function findSubSession(subs, sa) {
  const list = subs || [];
  const sid = String((sa && sa.session_id) || "").toLowerCase();
  const root = String((sa && (sa.root_session_id || sa.session_id)) || "").toLowerCase();
  const n = sa && sa.n;
  return list.find((s) => String(s.session_id || "").toLowerCase() === sid)
    || list.find((s) => {
      const rs = String(s.root_session_id || s.session_id || "").toLowerCase();
      return rs === root || String(s.session_id || "").toLowerCase() === root;
    })
    || (n != null ? list.find((s) => Number(s.n) === Number(n)) : null)
    || null;
}

function renderSubagentCard(sa, subs) {
  if (!sa) return "";
  const n = sa.n != null ? sa.n : "?";
  const rk = Number(sa.resume_index) || 0;
  const multi = Number(sa.resume_max) > 1 || rk > 1;
  const tag = sa.label || (multi && rk >= 1 ? `Sub Agent ${n} R${rk}` : `Sub Agent ${n}`);
  const title = sa.is_sys ? "" : (sa.title || sa.label || tag);
  const u = sa.usage || {};
  const sub = findSubSession(subs, sa);
  const sid = (sub && sub.session_id) || sa.session_id || "";
  let inTok = sa.tokens_in != null
    ? sa.tokens_in
    : Math.max(0, Number(u.inputTokens || 0) - Number(u.cachedReadTokens || 0));
  let cacheTok = sa.tokens_cached != null ? sa.tokens_cached : (u.cachedReadTokens || 0);
  let outTok = sa.tokens_out != null ? sa.tokens_out : (u.outputTokens || 0);
  let cin = sa.cost_in_usd;
  const ccache = sa.cost_cached_usd;
  const cout = sa.cost_out_usd;
  let usd = sa.estimate_usd != null
    ? sa.estimate_usd
    : (Number(cin || 0) + Number(ccache || 0) + Number(cout || 0));
  if (sa.is_sys && sub) {
    const rounds = sub.rounds || [];
    const r1 = rounds.find((r) => Number(r.index) === 1)
      || rounds.find((r) => r && r.system_prompt)
      || rounds[0];
    const sp = r1 && r1.system_prompt;
    if (sp) {
      const { tot } = systemBoxPartsAndTotal(sp);
      inTok = tot;
      cin = sp.cost_in_usd;
      usd = sp.estimate_usd != null ? sp.estimate_usd : cin;
      cacheTok = 0;
      outTok = 0;
    }
  }
  const goRound = sa.is_sys ? 1 : (rk >= 1 ? rk : 1);
  const line = joinParts([
    partIn(inTok, cin),
    partCached(cacheTok, ccache),
    partOut(outTok, cout),
  ]);
  const typeTip = sa.agent_name
    ? `type ${sa.agent_name} (spawn role) · ${sid}`
    : sid;
  return `<div class="subagent-card" data-sub-tab="${esc(sid)}" data-sub-round="${esc(String(goRound))}" title="${esc(typeTip)}">
    <span class="tag subagent">${esc(tag)}</span>
    <span class="sum-gray">${esc(title)}</span>
    ${line}
    ${usd != null && usd !== "" ? totalPrice(usd) : ""}
  </div>`;
}

function compBar(comp, se, step) {
  if (!comp && !se) return "";
  // Thought/Message/ToolReq = exact TokZ; Enc = residual of full off_out
  const th = Math.max(0, Number(
    se?.output_thought_tokens ?? comp?.thought_summary_out ?? comp?.thought_out
  ) || 0);
  const re = Math.max(0, Number(
    se?.output_reasoning_tokens ?? comp?.reasoning_encrypted_out
  ) || 0);
  const em = Math.max(0, Number(
    se?.output_emit_tokens ?? comp?.model_emit
  ) || 0);
  const msg = Math.max(0, Number(
    se?.output_message_tokens ?? comp?.message_out
  ) || 0);
  const outTot = Math.max(0, Number(
    se?.output_tokens ?? comp?.output_total ?? (th + re + em + msg)
  ) || 0);
  // Harness bar = Call In (User-owned Compact Out already excluded when stamped).
  let h = Math.max(0, Number(
    se?.uncached_input_tokens
    ?? se?.harness_in_tokens
    ?? comp?.harness_results
    ?? comp?.harness_in_total
  ) || 0);
  const peel = userCompactPeelFromTotals(step || null);
  h = Math.max(0, h - (peel.t || 0));
  // late residual redistributed into tools — never shown
  if (!th && !re && !em && !msg && !h) return "";
  const total = th + re + em + msg + h || 1;
  const pct = (n) => (n / total) * 100;
  return `<div class="comp-legend">
      <span class="lbl-thought">thought ${fmtTokens(th)}</span>
      · <span class="lbl-reasoning">enc ${fmtTokens(re)}</span>
      · <span class="lbl-toolreq">toolreq ${fmtTokens(em)}</span>
      · <span class="lbl-message">msg ${fmtTokens(msg)}</span>
      <span class="muted">(out ${fmtTokens(outTot)})</span>
      ${h ? ` · <span class="tok-in">harness ${fmtTokens(h)}</span>` : ""}
    </div>
    <div class="comp-bar" title="thought · enc · toolreq · message · Call In">
      <span class="cth" style="width:${pct(th)}%"></span>
      <span class="cre" style="width:${pct(re)}%"></span>
      <span class="ce" style="width:${pct(em)}%"></span>
      <span class="cm" style="width:${pct(msg)}%"></span>
      <span class="ch" style="width:${pct(h)}%"></span>
    </div>`;
}

function renderChildNode(c, phaseKey) {
  if (!c) return "";
  if (c.kind === "phase_llm") {
    const m = moneyFromNode(c);
    // Full detail in hierarchy — do not aggregate identical tools (graph only)
    const kids = (c.children || []).map(ch => renderChildNode(ch)).join("");
    const pid = phaseKey + "-llm";
    const isOpen = !window.__closedPhases?.has(pid);
    return `<details class="phase" data-pid="${esc(pid)}"${isOpen ? " open" : ""}>
      <summary>
        <span class="tag phase-llm">LLM</span>
        ${partOut(m.to, m.co)} ${totalPrice(m.tot)}
      </summary>
      ${kids}
    </details>`;
  }
  if (c.kind === "phase_harness") {
    const m = moneyFromNode(c);
    // User Compact Out lives under User[N]; peel only if phase totals still include it.
    const { inTok, inUsd } = harnessPhaseDisplayIn(c, m);
    // Full detail in hierarchy — do not aggregate identical tools (graph only)
    const kids = (c.children || []).map(ch => renderChildNode(ch)).join("");
    const pid = phaseKey + "-harness";
    const isOpen = !window.__closedPhases?.has(pid);
    // Tools present: In + Cached. Hook-only harness: no Cached (internal / → user).
    const hookOnly = !!c.hook_only || !!c.final_to_user || (!(inTok > 0) && !(c.tool_count > 0));
    const showCache = !hookOnly && (m.tc > 0 || m.cc > 0);
    const segs = showCache
      ? [partIn(inTok, inUsd), partCached(m.tc, m.cc)]
      : [partIn(inTok, inUsd)];
    return `<details class="phase" data-pid="${esc(pid)}"${isOpen ? " open" : ""}>
      <summary>
        <span class="tag phase-harness">Harness</span>
        ${joinParts(segs.filter(Boolean))}
        ${inTok ? " " + totalPrice(inUsd) : ""}
        ${c.tool_count ? `<span class="muted">· ${c.tool_count} tools</span>` : ""}
        <span class="muted" title="${hookOnly
          ? "Hook-only harness (→ user / internal). No Cached; not a next LLM prompt."
          : "In = LLM Out (re-enter) + tool results → next uncached. Cached = this call prompt prefix (display)."}">${hookOnly ? "· hook only" : "· out+tools + cache"}</span>
      </summary>
      ${kids}
    </details>`;
  }
  if (c.kind === "late_context") {
    // late residual removed — cost redistributed into tools in hierarchy
    return "";
  }
  if (c.kind === "hook") {
    return renderHookNode(c);
  }
  if (c.kind === "compact_out_in") {
    // Between-rounds: shown under User[N], not Call-1 harness.
    if (String(c.attribution || "") === "user") return "";
    const m = moneyFromNode(c);
    const inTok = Math.round(Number(m.ti || c.tokens_in || c.context_delta || 0));
    const z = Math.round(Number(c.tokenizer_tokens || inTok || 0));
    const cn = c.compact_index;
    const lab = (cn != null && Number(cn) > 0) ? `Compact ${Number(cn)} Out` : "Compact Out";
    return `<div class="node" title="${esc(c.estimate_note || "Compacted history re-enters next LLM prompt")}">
      <span class="tag llm-out-in">${esc(lab)}</span>
      ${partIn(inTok, m.ci)}
      ${eolTokenizerMeta({ ...c, tokenizer_tokens: z }, inTok > 0 ? inTok : null)}
    </div>`;
  }
  if (c.kind === "llm_to_in") {
    // First harness line: LLM Out [N] · tools… · In +tok
    const m = moneyFromNode(c);
    const n = c.call_index != null ? c.call_index : "?";
    const summary = (c.tool_summary || "").trim();
    // In = Thought + Reasoning + Message + ToolReq (all TokF)
    const inTok = Math.round(Number(m.ti || c.tokens_in || c.context_delta || 0));
    const outSrc = Math.round(
      Number(c.tokens_out_source || c.tokenizer_tokens || inTok || 0)
    );
    // Gray: thought · rez · msg · tools aggregated by name
    const grayBits = [];
    if (Number(c.reentry_thought_tokf) > 0) grayBits.push("thought");
    if (Number(c.reentry_reasoning_tokf) > 0) grayBits.push("rez");
    if (Number(c.reentry_message_tokf) > 0) grayBits.push("msg");
    // Prefer prebuilt tool_summary; else aggregate tool_names / tool_name_counts
    let toolAgg = (summary || "").trim();
    if (!toolAgg && c.tool_name_counts && typeof c.tool_name_counts === "object") {
      toolAgg = Object.entries(c.tool_name_counts)
        .map(([nm, cnt]) => (cnt > 1 ? `${nm} x${cnt}` : nm))
        .join(" · ");
    }
    if (!toolAgg && Array.isArray(c.tool_names) && c.tool_names.length) {
      const counts = new Map();
      c.tool_names.forEach(nm => {
        const k = String(nm || "tool");
        counts.set(k, (counts.get(k) || 0) + 1);
      });
      toolAgg = [...counts.entries()]
        .map(([nm, cnt]) => (cnt > 1 ? `${nm} x${cnt}` : nm))
        .join(" · ");
    }
    if (toolAgg) grayBits.push(toolAgg);
    // Fallback if no part stamps yet (still show tool summary)
    if (!grayBits.length && summary) grayBits.push(summary);
    const reHint = grayBits.join(" · ");
    return `<div class="node" title="${esc(c.estimate_note || "Thought + Reasoning + Message + ToolReq TokF → next In")}">
      <span class="tag llm-out-in">LLM Out [${esc(String(n))}]</span>
      ${reHint ? `<span class="sum-gray" title="${esc(reHint)}">${esc(reHint)}</span>` : ""}
      ${partIn(inTok, m.ci)}
      ${eolTokenizerMeta({ ...c, tokenizer_tokens: outSrc > 0 ? outSrc : null }, inTok > 0 ? inTok : null)}
    </div>`;
  }
  if (c.kind === "caused_in_residual") {
    // Only show if hierarchy still emits rare true residual (glue/lag)
    const m = moneyFromNode(c);
    if (!(m.ti || c.context_delta)) return "";
    return `<div class="node" title="${esc(c.estimate_note || "Unexplained growth — paid as next-call In")}">
      <span class="tag residual">other Δctx → In</span>
      <span class="muted">${esc(c.label || "reload/glue/lag")}</span>
      ${partIn(m.ti || c.context_delta, m.ci)}
    </div>`;
  }
  // LLM Out tool / plan request (one line per tool) — not harness tool
  if (c.kind === "tool_request" || c.kind === "tool_requests") {
    const m = moneyFromNode(c);
    let toolSeq = c.tool_seq;
    if (toolSeq == null && c.tool_call_id) {
      const tail = String(c.tool_call_id).split("-").pop();
      if (tail && /^\d+$/.test(tail)) toolSeq = tail;
    }
    const plan = c.plan || {};
    const isPlan = !!(c.is_plan || plan.is_plan || (c.name && String(c.name).toLowerCase().includes("todo")));
    const seqBit = toolSeq != null && toolSeq !== "" ? ` [${toolSeq}]` : "";
    const tag = isPlan ? `plan request${seqBit}` : `tool request${seqBit}`;
    const tagClass = isPlan ? "planreq" : "toolreq";
    // gray detail: create N steps / modify N steps (plan) or name·path (other)
    let gray = "";
    if (isPlan) {
      const n = plan.step_count || (plan.steps && plan.steps.length) || 0;
      const mode = plan.mode === "modify" ? "modify" : "create";
      gray = n > 0 ? `${mode} ${n} step${n === 1 ? "" : "s"}` : mode;
    } else {
      const nameBit = c.name ? c.name : "";
      const shellish = /run_terminal|get_command|subagent|bash|shell|powershell/i.test(nameBit);
      const pathBit = shellish
        ? shellInteresting(c.path || c.title || c.command || "")
        : (shortPath(c.path) || compressCwdText(c.title || ""));
      gray = [nameBit, pathBit].filter(Boolean).join(" · ");
    }
    const outTok = m.to || c.estimate_output_tokens || c.tokens_out || 0;
    const tipReq = c.path || c.title || c.command || c.estimate_note || "";
    return `<div class="node" title="${esc(tipReq || "Request RawInput — tokenizer definitive; in reasoningTokens")}">
      <span class="tag ${tagClass}">${esc(tag)}</span>
      ${gray ? `<span class="sum-gray" title="${esc(gray)}">${esc(gray)}</span>` : ""}
      ${partOut(outTok, m.co != null ? m.co : c.cost_out_usd)}
      ${eolTokenizerMeta(c, outTok)}
    </div>`;
  }
  if (c.kind === "tool" || (c.name && !["thought","reasoning","message","phase_llm","phase_harness","tool_request","tool_requests","llm_to_in","compact_out_in","hook","late_context"].includes(c.kind))) {
    const m = moneyFromNode(c);
    const pathBit = shortPath(c.path);
    const resume = c.title && c.title !== c.name ? String(c.title).slice(0, 36) : "";
    const plan = c.plan || {};
    const isPlan = !!(c.is_plan || plan.is_plan || (c.name && String(c.name).toLowerCase().includes("todo")));
    let toolSeq = c.tool_seq;
    if (toolSeq == null && c.tool_call_id) {
      const tail = String(c.tool_call_id).split("-").pop();
      if (tail && /^\d+$/.test(tail)) toolSeq = tail;
    }
    const seqBit = toolSeq != null && toolSeq !== "" ? ` [${toolSeq}]` : "";
    // plan [id] + colored step numbers; else tool [id] · name  (no chars / no "hist")
    let tag, tagClass, grayHtml, tipFull = "";
    if (isPlan) {
      // Harness: plan update [id] + colored step numbers (status)
      tag = `plan update${seqBit}`;
      tagClass = "plan";
      const steps = plan.steps || [];
      if (steps.length) {
        grayHtml = steps.map(s => {
          const st = String(s.status || "pending").toLowerCase().replace(/-/g, "_");
          const cls = st === "completed" ? "completed"
            : (st === "in_progress" ? "in_progress" : (st === "cancelled" ? "cancelled" : "pending"));
          const num = s.n != null ? s.n : s.id;
          return `<span class="plan-step ${cls}" title="${esc((s.content || st) + "")}">${esc(String(num))}</span>`;
        }).join("");
      } else {
        const n = plan.step_count || 0;
        grayHtml = n ? `${n} step${n === 1 ? "" : "s"}` : "";
      }
    } else {
      tag = toolSeq != null && toolSeq !== "" ? `tool${seqBit}` : "tool";
      tagClass = "tool";
      const nameBit = c.name || "tool";
      // Compress cwd for shell / subagent tools — interest only
      const shellish = /run_terminal|get_command|subagent|bash|shell|powershell/i.test(nameBit);
      const detailRaw = c.path || c.title || c.command || c.raw_input || "";
      tipFull = detailRaw || c.tool_call_id || "";
      const detail = shellish
        ? shellInteresting(detailRaw)
        : (pathBit || compressCwdText(resume || detailRaw));
      grayHtml = `${nameBit}${detail ? " · " + detail : ""}`;
    }
    // Prefer chat_history tokenizer stamps for harness result meta (exact ints)
    const tokZExact = Math.round(Number(
      c.ch_result_tokens ?? c.tokenizer_tokens ?? c.result_tokens_est ?? 0
    )) || null;
    const metaNode = {
      ...c,
      tokenizer_tokens: tokZExact,
      chars: c.ch_result_chars || c.result_chars || c.chars,
      ch_result_chars: c.ch_result_chars || c.result_chars,
      ch_result_tokens: tokZExact,
    };
    const inTok = Math.round(Number(
      m.ti
      || c.tokens_in
      || c.context_delta
      || c.ch_result_tokens
      || c.result_tokens_est
      || 0
    )) || 0;
    return `<div class="node" title="${esc(tipFull || c.tool_call_id || "")}">
      <span class="tag ${tagClass}">${esc(tag)}</span>
      ${grayHtml ? `<span class="sum-gray" title="${esc(String(tipFull || grayHtml))}">${isPlan ? grayHtml : esc(grayHtml)}</span>` : ""}
      ${partIn(inTok, m.ci)}
      ${eolTokenizerMeta(metaNode, inTok > 0 ? inTok : null)}
    </div>`;
  }
  if (c.kind === "reasoning") {
    const m = moneyFromNode(c);
    const outTok = Math.round(Number(
      c.tokens_out ?? c.estimate_output_tokens ?? m.to
    ) || 0);
    // Prefer stamped encrypted TokZ — never raw chars (chars//4 is last resort only)
    const metaNode = {
      ...c,
      tokenizer_tokens:
        c.tokenizer_tokens
        ?? c.encrypted_tokens
        ?? null,
    };
    return `<div class="node" title="${esc(c.estimate_note || "encrypted_content TokZ; residual Out bill separate")}">
      <span class="tag reasoning">reasoning</span>
      <span class="sum-gray">[encrypted]</span>
      ${partOut(outTok, m.co != null ? m.co : c.cost_out_usd)}
      ${eolTokenizerMeta(metaNode, outTok)}
    </div>`;
  }
  if (c.kind === "thought") {
    const m = moneyFromNode(c);
    const metaNode = {
      ...c,
      tokenizer_tokens: c.summary_tokens ?? c.tokenizer_tokens,
      summary_chars: c.summary_chars || c.chars,
    };
    const outTok = Math.round(Number(
      c.tokens_out ?? c.estimate_output_tokens ?? m.to
    ) || 0);
    const prev = c.preview ? String(c.preview) : "[summary]";
    return `<div class="node" title="${esc(c.estimate_note || "Thought summary — TokZ + share of official Out $")}">
      <span class="tag thought">thought</span>
      <span class="sum-gray" title="${esc(prev)}">${esc(prev)}</span>
      ${partOut(outTok, m.co != null ? m.co : c.cost_out_usd)}
      ${eolTokenizerMeta(metaNode, outTok)}
    </div>`;
  }
  if (c.kind === "message") {
    const m = moneyFromNode(c);
    const outTok = m.to || c.estimate_output_tokens || c.tokens_out || 0;
    const outUsd = m.co != null ? m.co : c.cost_out_usd;
    const metaNode = {
      ...c,
      tokenizer_tokens: c.message_tokens ?? c.tokenizer_tokens,
      message_chars: c.message_chars || c.chars,
    };
    const prev = c.preview ? String(c.preview) : "";
    return `<div class="node" title="${esc(c.estimate_note || "assistant.content — pure Out pro-rata")}">
      <span class="tag message">message</span>
      ${prev ? `<span class="sum-gray" title="${esc(prev)}">${esc(prev)}</span>` : ""}
      ${partOut(outTok, outUsd) || (pickTokenizerTokens(metaNode) != null
        ? `<span class="tok-out">Out +${fmtTokens(pickTokenizerTokens(metaNode))}</span>`
        : "")}
      ${eolTokenizerMeta(metaNode, outTok)}
    </div>`;
  }
  return "";
}

function renderRoundTree(rounds, opts) {
  const isSuper = !!(opts && opts.superAgent);
  const root = $("roundTree");
  if (!root) return;
  if (!rounds || !rounds.length) {
    root.innerHTML = `<div class="tree-empty">No rounds in this session yet. Follow an active session or pin one from the picker.</div>`;
    return;
  }
  const prevScroll = root.scrollTop;
  // Preserve open/closed across live re-renders
  const openIds = new Set(
    [...root.querySelectorAll("details[data-rid][open]")].map(d => d.getAttribute("data-rid"))
  );
  const openSteps = new Set(
    [...root.querySelectorAll("details[data-sid][open]")].map(d => d.getAttribute("data-sid"))
  );
  window.__closedPhases = new Set(
    [...root.querySelectorAll("details.phase[data-pid]")].filter(d => !d.open).map(d => d.getAttribute("data-pid"))
  );
  // Stagger enter only for rounds that appear after an existing tree (not first paint)
  const prevRids = new Set(
    [...root.querySelectorAll("details.round[data-rid]")].map(d => d.getAttribute("data-rid"))
  );
  const hadTree = prevRids.size > 0;
  let enterI = 0;

  function compactOnStep(c) {
    if (!c) return false;
    const ms = c.agent_ms;
    for (const rr of rounds || []) {
      for (const s of rr.model_steps || []) {
        for (const x of s.compacts_after || []) {
          if (x === c) return true;
          if (ms != null && x && x.agent_ms === ms) return true;
        }
      }
    }
    return false;
  }

  function renderCompactRow(c, where) {
    if (!c || c.kind !== "compaction") return "";
    const before = c.tokens_before;
    const after = c.tokens_after;
    const removed = c.tokens_removed != null
      ? c.tokens_removed
      : (typeof before === "number" && typeof after === "number" ? Math.max(0, before - after) : null);

    // In XOR Cached (miss vs hit) + Out (compressed history). Never both.
    const miss = !!c.pre_read_cache_miss;
    const preTok = Number(c.pre_read_tokens ?? before) || 0;
    const preCache = Number(c.pre_read_cached_tokens) || 0;
    const preUnc = Number(c.pre_read_uncached_tokens) || 0;
    const preCacheUsd = Number(c.pre_read_cached_usd) || 0;
    const preUncUsd = Number(c.pre_read_uncached_usd) || 0;
    const outTok = Number(c.out_tokens) || 0;
    const outUsd = Number(c.out_usd) || 0;
    let inTok = 0, inUsd = 0, cacheTok = 0, cacheUsd = 0;
    if (miss || (preUnc > 0 && !(preCache > 0))) {
      inTok = preUnc || preTok;
      inUsd = preUncUsd || Number(c.pre_read_usd) || 0;
    } else {
      cacheTok = preCache || preTok;
      cacheUsd = preCacheUsd || Number(c.pre_read_usd) || 0;
    }
    const totalUsd = c.cost_usd != null
      ? c.cost_usd
      : (Number(c.pre_read_usd) || 0) + outUsd;
    const headParts = joinParts([
      (inTok > 0 || inUsd > 0) ? partIn(inTok, inUsd) : null,
      (cacheTok > 0 || cacheUsd > 0) ? partCached(cacheTok, cacheUsd) : null,
      (outTok > 0 || outUsd > 0) ? partOut(outTok, outUsd) : null,
    ].filter(Boolean));
    // delta = removed; absolute = after (new context floor)
    const deltaBit = removed != null
      ? `<span class="tok-cached" title="context removed (no longer billed going forward)">${fmtDelta(-removed)}</span>`
      : "";
    const absBit = (before != null || after != null)
      ? `ctx ${fmtTokens(before)}${AR}${fmtTokens(after)}`
      : "";

    const autoTag = c.auto !== false;
    const whereBit = where || (c.placement === "mid_round" ? "mid-round" : "between rounds");
    return `<div class="compact-row" title="${esc(c.cost_note || whereBit)}">
      <span class="tag compact${autoTag ? " compact-auto" : ""}">Compact${autoTag ? " · auto" : ""}</span>
      <span class="compact-meta"></span>
      <span class="compact-ledger">
        ${headParts}
        ${totalUsd > 0 ? totalPrice(totalUsd) : ""}
      </span>
      <span class="compact-ctx muted">
        ${absBit}
        ${deltaBit}
      </span>
    </div>`;
  }

  /** Auto recap on harness fork — does not grow next-round context. */
  function renderRecapRow(c, where) {
    if (!c || c.kind !== "session_recap") return "";
    const ctxTok = c.context_tokens ?? c.context_cached_tokens;
    const promptTok = c.prompt_tokens;
    const outTok = c.out_tokens;
    // Same order as Round: In / Cached / Out / Price / delta / absolute
    const headParts = joinParts([
      (promptTok > 0 || c.prompt_in_usd > 0) ? partIn(promptTok, c.prompt_in_usd) : null,
      (ctxTok > 0 || c.pre_read_cached_usd > 0) ? partCached(ctxTok, c.pre_read_cached_usd) : null,
      (outTok > 0 || c.out_usd > 0) ? partOut(outTok, c.out_usd) : null,
    ].filter(Boolean));
    const totalUsd = c.cost_usd != null
      ? c.cost_usd
      : (Number(c.pre_read_cached_usd) || 0) + (Number(c.prompt_in_usd) || 0) + (Number(c.out_usd) || 0);
    // absolute = full fork context; delta N/A (fork does not grow session)
    const absBit = (ctxTok > 0)
      ? `ctx ${fmtTokens(ctxTok)}`
      : "";
    return `<div class="compact-row recap-row" title="${esc(c.cost_note || "Fork recap · " + (where || ""))}">
      <span class="tag recap">Recap${c.auto ? " · auto" : ""}</span>
      <span class="compact-meta"></span>
      <span class="compact-ledger">
        ${headParts}
        ${totalUsd > 0 ? totalPrice(totalUsd) : ""}
      </span>
      <span class="compact-ctx muted" title="Full session context re-read on the fork (cached)">${absBit}</span>
    </div>`;
  }

  /** Session bootstrap card (before Round 1) — same shell as Compact. */
  function renderSystemBootstrap(sp) {
    if (!sp || !(sp.tokens_in || sp.logical_tokens || (sp.parts && sp.parts.length)))
      return "";
    const { rawParts, tot } = systemBoxPartsAndTotal(sp);
    const parts = rawParts.map(p => {
      const tokF = Math.round(Number(p.tokens ?? p.tokens_in) || 0);
      const usd = p.cost_in_usd;
      const toolN = p.tool_count != null ? `${p.tool_count} tools` : "";
      const prev = String(p.preview || toolN || "").slice(0, 70);
      const tokZ = Math.round(Number(
        p.tokenizer_tokens || p.tok_w || tokF
      ) || 0) || null;
      // Same layout as tree .node: label · preview · In$ · tokZ (EOL)
      return `<div class="part-line node" title="${esc(p.note || prev || p.label || "")}">
        <span class="tag system">${esc(p.label || p.kind || "part")}</span>
        ${prev ? `<span class="sum-gray" title="${esc(prev)}">${esc(prev)}</span>` : ""}
        ${partIn(tokF, usd)}
        ${eolTokenizerMeta({ tokenizer_tokens: tokZ }, tokF > 0 ? tokF : null)}
      </div>`;
    }).join("");
    return `<div class="compact-row system-row" title="${esc(sp.note || "Session bootstrap (before Round 1)")}">
      <span class="tag system">System</span>
      <span class="compact-meta muted">${esc(sp.label || "System / tools / reminders / MCP / Message")}</span>
      <span class="compact-ledger">
        ${joinParts([partIn(tot, sp.cost_in_usd)])}
        ${sp.estimate_usd != null ? totalPrice(sp.estimate_usd) : ""}
      </span>
      <span class="compact-ctx"></span>
      ${parts ? `<div class="system-parts">${parts}</div>` : ""}
    </div>`;
  }

  let html = rounds.slice().reverse().map(r => {
    const rid = String(r.index);
    const open = openIds.has(rid) || (!r.completed && rounds[rounds.length - 1] === r) ? " open" : "";
    const livePill = r.completed ? "" : `<span class="pill live-round">live</span>`;
    const su = r.step_usage || {};
    const bd = r.breakdown || su.breakdown || {};
    const priorCache = su.prior_context_tokens ?? r.cache_baseline_at_start;
    const up = r.user_prompt || {};

    // Round In = user-prompt uncached In + sum(LLM call In / Harness).
    // System stays on its own card (R1). Not paid@start API uncached.
    const stepsForSum = r.model_steps || [];
    let sumCallIn = 0;
    let sumCallInUsd = 0;
    for (const s of stepsForSum) {
      const uc = userCompactPeelFromTotals(s);
      sumCallIn += Math.max(0, (Number(s.tokens_in) || 0) - uc.t);
      sumCallInUsd += Math.max(0, (Number(s.cost_in_usd) || 0) - uc.u);
    }
    const userInTok = Number(up.tokens_in ?? up.uncached_est ?? bd.user_in_tokens) || 0;
    const userInUsd = Number(up.cost_in_usd ?? bd.user_in_usd) || 0;
    const rIn = (bd.tree_in_tokens != null && bd.tree_in_tokens !== "")
      ? Number(bd.tree_in_tokens)
      : (userInTok + sumCallIn);
    const rInUsd = (bd.tree_in_usd != null && bd.tree_in_usd !== "")
      ? Number(bd.tree_in_usd)
      : (userInUsd + sumCallInUsd);
    const rCache = r.cached_read_tokens ?? bd.cached_tokens ?? su.totals?.cached_read;
    const rOut = r.output_tokens ?? bd.output_tokens ?? su.totals?.output;
    const rCacheUsd = r.cost_cached_usd ?? bd.cached_usd ?? su.totals?.cost_cached_usd;
    const rOutUsd = r.cost_out_usd ?? bd.output_usd ?? su.totals?.cost_out_usd;
    const rTotal = r.estimate_usd ?? bd.total_usd ?? su.totals?.cost_usd;

    const roundHeadParts = joinParts([
      partIn(rIn, rInUsd),
      partCached(rCache, rCacheUsd),
      partOut(rOut, rOutUsd),
    ]);

    // User [N]: LLM answer [N-1] (Thought TokZ + Reasoning TokF + Message TokZ)
    // + prompt residual — no double-count. Server peels when answer fits in pool.
    // On cache miss: prior re-read already includes answer N-1 — hide/skip it.
    // Miss lives on Attribution Reread→LLM + red box, never under User.
    const missTok = Number(
      bd.cache_miss_in_tokens
      ?? r.cache_miss_in_tokens
      ?? up.cache_miss_in_tokens
      ?? r.reread_in_tokens
      ?? up.reread_in_tokens
      ?? up.reread_tokens
      ?? bd.reread_in_tokens
      ?? bd.reread_tokens
    ) || 0;
    const missUsd = Number(
      bd.cache_miss_in_usd
      ?? r.cache_miss_in_usd
      ?? up.cache_miss_in_usd
      ?? r.reread_in_usd
      ?? up.reread_in_usd
      ?? bd.reread_in_usd
    ) || 0;
    const rereadHit = !!(missTok > 0 || missUsd > 0
      || up.session_restart || up.cache_miss || r.session_restart
      || up.context_reread || r.context_reread || r.cache_miss
      || (bd && bd.context_reread));
    const prevAns = up.prev_llm_answer || {};
    const prevAbsorbed = false;
    // TokZ meta for eol chip only — not billed In.
    const prevTokZ = Math.round(Number(
      prevAns.tokenizer_tokens || prevAns.tokens_in_full || prevAns.tokens_in || 0
    )) || 0;
    // Billed continuity In only (peel-capped on server). Do not fall back to
    // tokens_in_full / tokenizer — that re-invents warm cached answer as In.
    const prevInTokRaw = Math.round(Number(prevAns.tokens_in) || 0) || 0;
    const prevInTok = prevAbsorbed ? 0 : prevInTokRaw;
    const prevOutInUsd = prevAbsorbed ? 0 : (Number(prevAns.cost_in_usd) || 0);
    const prevFromPool = !prevAbsorbed && !!prevAns.from_user_pool;
    const prevRoundN = prevAns.round_index || Math.max(0, (r.index || 1) - 1);
    // Prompt residual (never includes answer mass)
    let promptIn = Number(
      up.prompt_tokens_in != null ? up.prompt_tokens_in : up.tokens_in ?? up.uncached_est
    ) || 0;
    let promptUsd = Number(
      up.prompt_cost_in_usd != null ? up.prompt_cost_in_usd : up.cost_in_usd
    ) || 0;
    // Fallback peel if server fields missing (use In mass, not pure TokZ)
    const rawUser = Number(up.uncached_est_raw ?? up.tokens_in ?? up.uncached_est) || 0;
    if (!prevAbsorbed && up.prompt_tokens_in == null && prevInTok > 0 && rawUser >= prevInTok) {
      promptIn = Math.max(0, rawUser - prevInTok);
      if (rawUser > 0) promptUsd = (Number(up.cost_in_usd) || 0) * (promptIn / rawUser);
    }
    // Prefer server tokens_in (prompt + billed peel only). Never invent answer In.
    const userTreeIn = Number(up.tokens_in ?? up.uncached_est) || promptIn || 0;
    const userTreeUsd = Number(up.cost_in_usd) || 0;
    // Keep User Cached even on round KV miss (unknown which prefix missed).
    // R1 User Cached is already 0 from server — do not invent it here.
    const isR1User = Number(r.index) === 1;
    let upCache = isR1User
      ? 0
      : (Number(up.tokens_cached ?? up.cached_est ?? priorCache) || 0);
    let upCacheUsd = isR1User ? 0 : (Number(up.cost_cached_usd) || 0);
    // Answer continuity: peel from Cached display when not taken from user pool
    if (prevInTok > 0 && upCache >= prevInTok && !prevFromPool) {
      const fullC = Number(up.tokens_cached ?? up.cached_est ?? priorCache) || 1;
      upCache = Math.max(0, upCache - prevInTok);
      if (upCacheUsd > 0 && fullC > 0)
        upCacheUsd = upCacheUsd * (upCache / fullC);
    }
    const ud = up.user_detail || {};
    // Full text only in title; short ellipsis in-row so hierarchy width stays stable
    const promptFull = String(up.preview || r.user_preview || "");
    const promptPreview = promptFull.length > 48
      ? promptFull.slice(0, 48) + "…"
      : promptFull;
    // Header In = prompt + billed LLM answer [N-1] + Compact Out (aligned with tree).
    const coTokHead = Math.round(Number((up.compact_out || {}).tokens_in) || 0);
    const coUsdHead = Number((up.compact_out || {}).cost_in_usd) || 0;
    const userHeadIn = Number(up.display_in_tokens)
      || (userTreeIn + coTokHead)
      || (promptIn + prevInTok + coTokHead);
    const userHeadInUsd = Number(up.display_in_usd)
      || (userTreeUsd + coUsdHead)
      || (promptUsd + prevOutInUsd + coUsdHead);
    const userHeadTot = userHeadInUsd + upCacheUsd;
    const uid = `${rid}-user`;
    const uOpen = openSteps.has(uid) ? " open" : "";
    const ansBits = [];
    if (prevAns.tokens_thought) ansBits.push("thought");
    if (prevAns.tokens_reasoning) ansBits.push("reasoning");
    if (prevAns.tokens_message || prevInTokRaw) ansBits.push("message");
    const ansLabel = ansBits.length ? ansBits.join(" · ") : "thought · reasoning · message";
    const prevAnsLine = (!prevAbsorbed && prevInTok > 0)
      ? `<div class="node" title="${esc(prevAns.note || "Thought TokZ + Reasoning TokF + Message TokZ (hooks excluded)")}">
          <span class="tag llm-out-in">LLM answer round [${esc(String(prevRoundN))}]</span>
          <span class="sum-gray" title="${esc(ansLabel)}">${esc(ansLabel)}</span>
          ${partIn(prevInTok, prevOutInUsd)}
          ${eolTokenizerMeta({ tokenizer_tokens: prevTokZ || prevInTok }, prevInTok)}
        </div>`
      : "";
    const co = up.compact_out || {};
    const coTok = Math.round(Number(co.tokens_in) || 0);
    const coUsd = Number(co.cost_in_usd) || 0;
    const coN = co.compact_index;
    const coLab = (coN != null && Number(coN) > 0) ? `Compact ${Number(coN)} Out` : "Compact Out";
    const compactOutLine = (coTok > 0 || coUsd > 0)
      ? `<div class="node" title="${esc(co.note || "Compacted history re-enters as User In (prior LLM answer already inside)")}">
          <span class="tag llm-out-in">${esc(coLab)}</span>
          ${partIn(coTok, coUsd)}
          ${eolTokenizerMeta({ tokenizer_tokens: coTok }, coTok > 0 ? coTok : null)}
        </div>`
      : "";
    const promptTokZ = Math.round(Number(
      up.tokenizer_tokens || up.prompt_tokenizer_tokens
      || ((ud.user_query_tokens || 0) + (ud.skill_information_tokens || 0))
      || 0
    )) || null;
    const promptTokF = Math.round(promptIn) || 0;
    const promptLine = `
      <div class="node prompt-node" title="${esc(promptFull)}">
        <span class="tag user">prompt</span>
        <span class="sum-gray" title="${esc(promptFull)}">${esc(promptPreview) || ""}</span>
        ${partIn(promptTokZ || promptTokF, promptUsd)}
        ${eolTokenizerMeta({ tokenizer_tokens: promptTokZ }, promptTokZ || promptTokF || null)}
      </div>`;
    // User-section hooks (user_prompt_submit + future user_prompt_*) after prompt
    const userHooks = (up.hooks || r.user_hooks || []).filter(Boolean);
    const userHookLines = userHooks.map(h => renderHookNode(h)).join("");
    const userBlock = `
      <details class="step user-prompt-step" data-sid="${uid}"${uOpen}>
        <summary>
          <span class="tag user">${isSuper ? "Super Agent" : "User"} [${esc(String(r.index))}]</span>
          ${joinParts([
            partIn(userHeadIn, userHeadInUsd),
            partCached(upCache, upCacheUsd),
          ].filter(Boolean))}
          ${userHeadTot > 0 ? " " + totalPrice(userHeadTot) : ""}
        </summary>
        ${prevAnsLine}
        ${compactOutLine}
        ${promptLine}
        ${userHookLines}
      </details>`;

    // Attribution above User.
    // Order: Reread→LLM / User→LLM / Harness→LLM / Reasoning / …
    // Capitals only on mid-level Attribution labels (User/Harness/Reasoning/…);
    // leaf rows under LLM / Harness stay lowercase.
    const attrParts = [];
    if (missTok || missUsd)
      attrParts.push(`Reread${AR}LLM ${partIn(missTok, missUsd)}`);
    if (bd.user_in_tokens || bd.user_in_usd)
      attrParts.push(`${isSuper ? "Super Agent" : "User"}${AR}LLM ${partIn(bd.user_in_tokens, bd.user_in_usd)}`);
    // Harness→LLM = tools/hooks only — subtract LLM→Harness In (Out re-entry)
    // so the same tokens are not attributed twice.
    {
      let hAllT = Number(bd.harness_in_tokens) || 0;
      let hAllU = Number(bd.harness_in_usd) || 0;
      // Peel between-rounds Compact Out only when call totals still include it.
      let peelCoT = 0;
      let peelCoU = 0;
      for (const s of r.model_steps || []) {
        const uc = userCompactPeelFromTotals(s);
        peelCoT += uc.t;
        peelCoU += uc.u;
      }
      hAllT = Math.max(0, hAllT - peelCoT);
      hAllU = Math.max(0, hAllU - peelCoU);
      const outInT = Number(bd.llm_out_to_harness_in_tokens) || 0;
      const outInU = Number(bd.llm_out_to_harness_in_usd) || 0;
      const hToolsT = Math.max(
        0,
        Number(bd.harness_tools_in_tokens != null
          ? bd.harness_tools_in_tokens
          : hAllT - outInT) || 0
      );
      const hToolsU = Math.max(
        0,
        Number(bd.harness_tools_in_usd != null
          ? bd.harness_tools_in_usd
          : hAllU - outInU) || 0
      );
      if (hToolsT || hToolsU)
        attrParts.push(`Harness${AR}LLM ${partIn(hToolsT, hToolsU)}`);
    }
    // Attribution Out split:
    //   Reasoning = Thought + Enc (merged)
    //   LLM→Harness = Σ Tool Request
    //   LLM→User = Message
    // So Reasoning + Harness + User = official Round Out.
    {
      const thT = Number(bd.llm_thought_summary_tokens) || 0;
      const thU = Number(bd.llm_thought_summary_usd) || 0;
      const encT = Number(
        bd.llm_reasoning_encrypted_tokens ?? bd.llm_reasoning_tokens
      ) || 0;
      const encU = Number(
        bd.llm_reasoning_encrypted_usd ?? bd.llm_reasoning_usd
      ) || 0;
      const reasonT = thT + encT;
      const reasonU = thU + encU;
      if (reasonT > 0 || reasonU > 0)
        attrParts.push(`Reasoning ${partOut(reasonT, reasonU)}`);
    }
    if (bd.llm_out_to_harness_tokens || bd.llm_out_to_harness_usd) {
      // Out only = tool requests (not full Out re-entry)
      const oT = Math.round(Number(bd.llm_out_to_harness_tokens) || 0);
      const oU = Number(bd.llm_out_to_harness_usd) || 0;
      if (oT || oU)
        attrParts.push(`LLM${AR}Harness ${partOut(oT, oU)}`);
    }
    if (bd.llm_out_to_user_tokens || bd.llm_out_to_user_usd)
      attrParts.push(`LLM${AR}User ${partOut(bd.llm_out_to_user_tokens, bd.llm_out_to_user_usd)}`);

    const kvMissBox = (missTok > 0 || missUsd > 0) ? `
      <div class="usage-line warn-restart" title="${esc(up.note || up.warning || "Prior context re-read as uncached Input (KV miss)")}">
        <span class="tag warn">Cache Miss context re-read (KV Miss)</span>
        ${partIn(missTok, missUsd)}
        ${missUsd > 0 ? totalPrice(missUsd) : ""}
      </div>` : "";
    const usageBlock = (attrParts.length || kvMissBox) ? `
      <div class="usage-line">
        <span class="tag">Attribution</span>
        ${attrParts.join(` <span class="sep">/</span> `) || "—"}
      </div>${kvMissBox}` : "";

    const steps = (r.model_steps || []).map(s => {
      const sid = `${rid}-${s.index}`;
      const sOpen = openSteps.has(sid) ? " open" : "";
      const se = s.estimate || {};
      const children = (s.children || []).map(ch => renderChildNode(ch, sid)).join("");
      // ONLY caused In (growth of this call → paid on next). NEVER paid@start.
      let growthIn = s.tokens_in != null ? s.tokens_in
        : (se.uncached_input_tokens != null ? se.uncached_input_tokens : se.logical_uncached_tokens);
      let growthInUsd = s.cost_in_usd != null ? s.cost_in_usd
        : (se.cost_in_usd != null ? se.cost_in_usd : se.cost_in_logical_usd);
      // Floor from harness if call In empty but tools have mass
      const harnessIn = (s.children || [])
        .filter(ch => ch && ch.kind === "phase_harness")
        .reduce((a, ch) => a + (Number(ch.tokens_in) || 0), 0);
      if ((!growthIn || growthIn <= 0) && harnessIn > 0) {
        growthIn = harnessIn;
        growthInUsd = (s.children || [])
          .filter(ch => ch && ch.kind === "phase_harness")
          .reduce((a, ch) => a + (Number(ch.cost_in_usd) || 0), 0);
      }
      // Between-rounds Compact Out under User — peel only if Call In still includes it.
      const uc = userCompactPeelFromTotals(s);
      if (uc.t > 0 || uc.u > 0) {
        growthIn = Math.max(0, (Number(growthIn) || 0) - uc.t);
        growthInUsd = Math.max(0, (Number(growthInUsd) || 0) - uc.u);
      }
      const cacheTok = s.tokens_cached
        ?? s.display_cached_tokens
        ?? se.display_cached_tokens
        ?? se.logical_cached_tokens
        ?? se.cached_read_tokens;
      const cacheUsd = s.cost_cached_usd
        ?? s.display_cached_usd
        ?? se.display_cached_usd
        ?? se.cost_cached_logical_usd
        ?? se.cost_cached_usd;
      const outTokLine = s.tokens_out ?? se.output_tokens;
      const outUsdLine = s.cost_out_usd ?? se.cost_out_usd;
      const callLine = joinParts([
        partIn(growthIn, growthInUsd),
        partCached(cacheTok, cacheUsd),
        partOut(outTokLine, outUsdLine),
      ]);
      // White total MUST equal In + Cached + Out shown on this line
      // (not api_call_usd / paid@start, which can differ from tree In).
      const lineSumUsd =
        (Number(growthInUsd) || 0) + (Number(cacheUsd) || 0) + (Number(outUsdLine) || 0);
      const apiCost = (lineSumUsd > 0 || growthIn || cacheTok || outTokLine)
        ? lineSumUsd
        : (s.cost_of_call_usd ?? s.estimate_usd ?? se.cost_of_call_usd ?? se.estimate_usd);
      // Δctx = same estimated Call In on this line (tree / Cached growth), not
      // API `_meta.totalTokens` window (ctxB−ctxA). Last call: skip_context.
      const skipCtx = !!s.skip_context;
      const ctxA = s.display_context_start ?? s.context_start;
      const ctxB = s.display_context_end ?? s.context_end;
      let ctxGrowth = null;
      if (!skipCtx) {
        const gin = Math.round(Number(growthIn) || 0);
        if (gin > 0) ctxGrowth = gin;
      }
      const subCards = (s.subagents_after || []).map((sa) =>
        renderSubagentCard(sa, opts && opts.subSessions)
      ).join("");
      const compactCards = (s.compacts_after || [])
        .map(c => renderCompactRow(c, `after LLM call [${s.index}]`))
        .join("");
      return `<details class="step" data-sid="${sid}"${sOpen}>
        <summary>
          <span class="tag">LLM call [${s.index}]</span>
          ${callLine || (skipCtx ? "" : `<span class="muted">ctx ${fmtTokens(ctxA)}${AR}${fmtTokens(ctxB)}</span>`)}
          ${apiCost != null ? " " + totalPrice(apiCost) : ""}
          ${ctxGrowth != null ? `<span class="muted" title="Δctx = this call's estimated In (same figure as In on this line). Not API totalTokens Δ."> · Δctx ${fmtDelta(ctxGrowth)}</span>` : ""}
        </summary>
        ${compBar(s.composition, se, s)}
        ${children || `<div class="node muted">—</div>`}
      </details>${subCards}${compactCards}`;
    }).join("");

    // Newest-first: between-round events sit *after* this card (toward older).
    // Later events first so a recap triggered before compact stays closer to
    // the previous round, not after the compact.
    const whereBetween = `between Round ${Math.max(0, (r.index || 1) - 1)} and Round ${r.index}`;
    const beforeItems = [];
    for (const rec of (r.recaps_before || []))
      beforeItems.push({ kind: "recap", ev: rec });
    if (r.compact_before && r.compact_before.kind === "compaction" && !compactOnStep(r.compact_before))
      beforeItems.push({ kind: "compact", ev: r.compact_before });
    beforeItems.sort((a, b) => eventMs(b.ev) - eventMs(a.ev));
    const betweenBefore = beforeItems.map((it) => (
      it.kind === "compact"
        ? renderCompactRow(it.ev, whereBetween)
        : renderRecapRow(it.ev, `before Round ${r.index} · after previous · fork`)
    )).join("");
    // Newest round: events after it still live on *_after — show *above* the card.
    // Use payload position, not index+1: after prune, reused indexes used to
    // make every card look like the newest (and hide Compact · auto).
    const rPos = (rounds || []).indexOf(r);
    const hasNewer = rPos >= 0 && rPos < (rounds || []).length - 1;
    let recapsAbove = "";
    if (!hasNewer) {
      const afterItems = [];
      for (const rec of (r.recaps_after || []))
        afterItems.push({ kind: "recap", ev: rec });
      if (r.compact_after && r.compact_after.kind === "compaction" && !compactOnStep(r.compact_after))
        afterItems.push({ kind: "compact", ev: r.compact_after });
      afterItems.sort((a, b) => eventMs(b.ev) - eventMs(a.ev));
      recapsAbove = afterItems.map((it) => (
        it.kind === "compact"
          ? renderCompactRow(it.ev, `after Round ${r.index}`)
          : renderRecapRow(it.ev, `after Round ${r.index} · fork (no session growth)`)
      )).join("");
    }

    const idleMs = r.idle_gap_ms;
    const idleLabel = fmtIdleGap(idleMs);
    // Always reserve a slot when gap known; show Δt even for short gaps
    const idleBit = (idleMs != null && idleMs >= 0 && idleLabel)
      ? `<span class="muted" title="Idle gap: previous round completed → this round started. Long idle often drops KV cache.">Δt ${idleLabel}</span>`
      : (r.index > 1
        ? `<span class="muted" title="Idle gap unavailable (missing timestamps)">Δt —</span>`
        : "");

    // New round cards: cascade fade/slide (CSS --enter-i); skip first paint
    const isNew = hadTree && !prevRids.has(rid);
    const enterCls = isNew ? " is-new" : "";
    const enterStyle = isNew ? ` style="--enter-i:${enterI++}"` : "";
    const focusCls = (window.__focusedRound != null && String(window.__focusedRound) === rid)
      ? " is-focus"
      : "";
    const cardHtml = `<details class="round${enterCls}${focusCls}" data-rid="${rid}"${open}${enterStyle}>
      <summary>
        <div class="round-head">
          <span class="title">Round ${r.index}</span>
          <span class="round-meta">
            ${livePill}
            ${idleBit}
          </span>
          <span class="round-ledger">
            ${roundHeadParts}
            ${rTotal != null ? totalPrice(rTotal) : ""}
          </span>
          <span class="round-ctx muted">ctx ${fmtTokens(r.context_start)}${AR}${fmtTokens(r.context_end)}
            <span class="tok-cached">${fmtDelta(r.context_delta)}</span></span>
        </div>
      </summary>
      ${usageBlock}
      ${userBlock}
      ${(() => {
        const hooks = r.hooks_before_llm || [];
        if (!hooks.length) return "";
        return hooks.map(h => renderHookNode(h)).join("");
      })()}
      ${steps || `<div class="muted" style="padding:0 10px 10px">no model steps yet</div>`}
      ${(() => {
        // Round-level hooks not already shown under User / before-LLM / a step
        const shown = new Set();
        for (const h of (up.hooks || r.user_hooks || [])) {
          if (h) shown.add(h);
        }
        for (const h of (r.hooks_before_llm || [])) {
          if (h) shown.add(h);
        }
        for (const s of (r.model_steps || [])) {
          for (const h of (s.hooks || [])) {
            if (h) shown.add(h);
          }
        }
        const leftover = (r.hooks || []).filter(h => {
          if (!h || shown.has(h)) return false;
          // Also match by event+prompt if object identity was lost in JSON
          const key = `${h.event_name}|${h.prompt_id || ""}`;
          for (const x of shown) {
            if (`${x.event_name}|${x.prompt_id || ""}` === key) return false;
          }
          return true;
        });
        const after = (r.hooks_after || []).filter(Boolean);
        const rest = leftover.concat(after.filter(h => !leftover.includes(h)));
        if (!rest.length) return "";
        return rest.map(h => renderHookNode(h)).join("");
      })()}
    </details>${betweenBefore}`;
    // recapsAbove: newest recap sits above the newest completed round (no R[n+1] yet)
    return recapsAbove + cardHtml;
  }).join("");

  // System bootstrap card sits above Round 1 (same shell as Compact).
  // Newest-first list: prepend after reverse so it appears just above R1 visually
  // when R1 is last in chronological order → first after reverse.
  let systemCard = "";
  const chrono = rounds || [];
  const firstRound = chrono.find(r => r && r.index === 1) || chrono[0];
  if (firstRound && firstRound.system_prompt) {
    systemCard = renderSystemBootstrap(firstRound.system_prompt);
  }
  // Newest-first: system should appear just above Round 1 card.
  // After reverse, R1 is last; inject system before the last details if R1 is last,
  // else search for Round 1 block — simpler: put system at end of HTML (bottom = oldest area)
  // so it sits above R1 when R1 is at the bottom of newest-first list.
  if (systemCard) {
    // Place System card *below* Round 1 (after its </details>), Compact-like.
    const marker = `>Round 1<`;
    const idx = html.indexOf(marker);
    if (idx >= 0) {
      const det = html.lastIndexOf("<details", idx);
      if (det >= 0) {
        // Walk nested <details> to find Round 1's closing tag
        let depth = 0, i = det, end = -1;
        while (i < html.length) {
          const openAt = html.indexOf("<details", i);
          const closeAt = html.indexOf("</details>", i);
          if (closeAt < 0) break;
          if (openAt >= 0 && openAt < closeAt) {
            depth++;
            i = openAt + 8;
          } else {
            depth--;
            i = closeAt + 10;
            if (depth === 0) { end = i; break; }
          }
        }
        if (end > 0) {
          html = html.slice(0, end) + systemCard + html.slice(end);
        } else {
          html = html + systemCard;
        }
      } else {
        html = html + systemCard;
      }
    } else {
      html = html + systemCard;
    }
  }

  root.innerHTML = html;
  root.scrollTop = prevScroll;
}

function _densityMode(mode) {
  return mode === "expert" ? "expert" : "standard";
}

function setTreeDensity(mode) {
  const m = _densityMode(mode);
  window.__treeDensity = m;
  try { localStorage.setItem("tt-tree-density", m); } catch { /* ignore */ }
  const root = $("roundTree");
  if (root) {
    root.classList.remove("density-compact", "density-standard", "density-expert");
    root.classList.add("density-" + m);
  }
  const map = { standard: "densStandard", expert: "densExpert" };
  Object.entries(map).forEach(([key, id]) => {
    const b = $(id);
    if (!b) return;
    const on = key === m;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function setRoundsOpen(open) {
  const root = $("roundTree");
  if (!root) return;
  root.querySelectorAll("details.round").forEach((d) => { d.open = !!open; });
}

function revealRound(rid) {
  if (rid == null || rid === "") return null;
  const root = $("roundTree");
  if (!root) return null;
  const el = root.querySelector(`details.round[data-rid="${CSS.escape(String(rid))}"]`);
  if (!el) return null;
  el.open = true;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  return el;
}

function focusRound(rid) {
  if (rid == null || rid === "") return;
  window.__focusedRound = String(rid);
  const root = $("roundTree");
  if (root) {
    root.querySelectorAll("details.round.is-focus").forEach((el) => el.classList.remove("is-focus"));
  }
  const el = revealRound(rid);
  if (el) el.classList.add("is-focus");
}

function clearRoundFocus() {
  window.__focusedRound = null;
  const root = $("roundTree");
  if (!root) return;
  root.querySelectorAll("details.round.is-focus").forEach((el) => el.classList.remove("is-focus"));
}

export { renderRoundTree, setTreeDensity, setRoundsOpen, focusRound, clearRoundFocus, revealRound };
