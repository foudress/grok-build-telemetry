/**
 * Feature gates — keep in sync with token_telemetry/features.py.
 * False = hidden from UI (code stays in tree; polish/debug for V1.0.0).
 */
export const FEATURES = Object.freeze({
  /** /history — chat_history mutation watch */
  mutationHistory: false,
  /** Period ⌛ Gantt timeline */
  gantt: false,
  /** Session + period tok/s charts */
  toksPerSec: false,
  /** /graph + Context panel Graph mode (agent animation) */
  agentAnimationGraph: false,
  /** Period D/W/M step chart — estimated In/Cached/Out $ per session */
  periodIoPriceStep: false,
});

export const WIP_NOTE =
  "Still in polish/debug for UI integration. Code remains in the tree; flip flags in features.js / token_telemetry.features.";

/** Sync gated chrome with FEATURES (idempotent; also unhides when flags flip on). */
export function applyFeatureGates() {
  const setHidden = (el, off) => {
    if (!el) return;
    el.hidden = !!off;
    if (off) el.setAttribute("aria-hidden", "true");
    else el.removeAttribute("aria-hidden");
  };

  document.querySelectorAll('a[href="/history"], a[href="/history.html"]').forEach((a) => {
    setHidden(a, !FEATURES.mutationHistory);
  });
  document.querySelectorAll('a[href="/graph"], a[href="/graph.html"]').forEach((a) => {
    setHidden(a, !FEATURES.agentAnimationGraph);
  });

  setHidden(document.getElementById("ctxModeRate"), !FEATURES.toksPerSec);
  setHidden(document.getElementById("ctxRateGrain"), !FEATURES.toksPerSec);
  setHidden(document.getElementById("aggUnitRate"), !FEATURES.toksPerSec);
  setHidden(document.getElementById("periodRateGrain"), !FEATURES.toksPerSec);
  setHidden(document.getElementById("aggUnitTime"), !FEATURES.gantt);
  setHidden(document.getElementById("ganttReset"), !FEATURES.gantt);
  setHidden(document.getElementById("aggUnitIoStep"), !FEATURES.periodIoPriceStep);
  setHidden(document.getElementById("ctxModeGraph"), !FEATURES.agentAnimationGraph);
  if (!FEATURES.agentAnimationGraph) {
    setHidden(document.getElementById("ctxGraphWrap"), true);
  }

  // Remove leftover footnote if an older build injected one.
  document.getElementById("wipFeaturesNote")?.remove();
}

export function featureEnabled(name) {
  return !!FEATURES[name];
}
