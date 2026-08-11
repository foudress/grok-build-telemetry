"""Patch pricing.py calibration to chain In/Cached model."""
from pathlib import Path

p = Path(__file__).with_name("pricing.py")
text = p.read_text(encoding="utf-8")
start = text.index("    # --- Prefix model + official calibration ---")
end = text.index("    if off_out is not None:\n        outputs = _scale_ints(raw_outputs, off_out)")

new = r'''    # --- Chain model: In = new uncached; Cached grows by previous In ---
    #
    # Rules (match cost graphs + updates.jsonl):
    #   - Round starts with current context as Cached (warm: prior end)
    #   - Cached[n] = Cached[n-1] + Uncached[n-1]   (Uncached = new In)
    #   - Last call of a multi-call round: Cached = 0
    #   - Penultimate Cached ≈ live context (prior + Σ earlier In)
    #   - Σ In (uncached) == InputTokens - CachedReadTokens
    #   - Σ display-cache need NOT equal official (last is 0)
    phys = [max(0, int(round(x))) for x in raw_inputs]
    phys_raw = list(phys)
    bootstrap_residual_tokens = 0
    prior_i = (
        max(0, int(prior_context_tokens))
        if isinstance(prior_context_tokens, int) and prior_context_tokens > 0
        else 0
    )

    if n and any(p > 0 for p in phys):
        # Stream floors: warm prior + non-decreasing context cursor
        if not cold and prior_i > 0:
            phys[0] = max(phys[0], prior_i)
        for i, step in enumerate(steps):
            ce = step.get("context_end")
            if isinstance(ce, int) and ce > phys[i]:
                phys[i] = ce
        for i in range(1, n):
            if phys[i] < phys[i - 1]:
                phys[i] = phys[i - 1]
        if not cold and prior_i > 0:
            phys[0] = max(phys[0], prior_i)

        # --- New uncached In weights (growth only) ---
        growth_w: list[float] = []
        for i in range(n):
            if i == 0:
                if cold:
                    # Cold: first prompt is almost all uncached at API
                    w = float(max(phys[0], 1))
                else:
                    w = float(max(0, phys[0] - prior_i))
                    if w <= 0:
                        w = 1.0
            else:
                delta_s = float(max(0, phys[i] - phys[i - 1]))
                ce = steps[i].get("context_end")
                cs = steps[i].get("context_start")
                delta_e = 0.0
                if isinstance(ce, int) and isinstance(cs, int) and ce >= cs:
                    delta_e = float(ce - cs)
                h = float(call_growth[i - 1] if i - 1 < len(call_growth) else 0)
                w = max(delta_s, delta_e, h, 1.0)
            growth_w.append(w)

        if off_unc is not None and sum(growth_w) > 0:
            uncached = _scale_ints(growth_w, int(off_unc))
        elif off_unc is not None:
            uncached = [0] * n
            uncached[-1] = int(off_unc)
        else:
            uncached = [max(0, int(round(w))) for w in growth_w]

        # --- Cache chain ---
        # C0 = prior (warm) or 0 (cold — first prompt is pure In)
        # Ci = C{i-1} + U{i-1}
        # Last call (n>=2): C = 0 (does not rewrite earlier chain links)
        caches = [0] * n
        if not cold and prior_i > 0:
            caches[0] = prior_i
        else:
            caches[0] = 0
        for i in range(1, n):
            caches[i] = int(caches[i - 1] + uncached[i - 1])
        if n >= 2:
            caches[n - 1] = 0

        # Identity: prompt size = cached + new uncached In
        inputs = [int(caches[i] + uncached[i]) for i in range(n)]

        # Cold Message residual for System card (packaging beyond bare stream)
        if cold and n and raw_inputs:
            stream0 = max(0, int(round(raw_inputs[0])))
            if uncached[0] > stream0 > 0:
                bootstrap_residual_tokens = uncached[0] - stream0

        logical_inputs = list(inputs)
        logical_uncached = list(uncached)
        logical_caches = list(caches)
        phys = list(inputs)
    else:
        uncached = list(logical_uncached)
        caches = list(logical_caches)
        inputs = [int(uncached[i] + caches[i]) for i in range(n)]
        phys = list(inputs)
        phys_raw = list(inputs)

'''

p.write_text(text[:start] + new + text[end:], encoding="utf-8")
print("OK patched", start, "->", end)
