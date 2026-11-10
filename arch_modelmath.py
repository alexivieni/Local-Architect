"""Model math, explained: for THIS machine and a given kind of work, what each option costs and buys, side by side, with the formula under
every number. Dense vs mixture-of-experts, quant levels, context length vs speed, draft speculation, thinking models, partial offload vs a
smaller model, CPU-only. Nothing here is a benchmark claim: every row says whether its numbers were measured here, come from the model's
own card, or are an estimate from the arithmetic below.

The plain-words rule: every row leads with one sentence anyone understands ("faster, but it forgets more of the file"), then the numbers,
then the formula. Acronyms are explained the first time they appear on a screen.

    advise(hw, candidates, workload) -> {"machine": {...}, "workload", "rows": [...], "glossary": {...}}
      candidate: {"name", "size_gb", "arch": {layers, kv_heads, head_dim, experts, experts_used}, "params_b", "active_b", "quant", "think", "measured": {gen_tok_s, prefill_tok_s, gpu_pct}}
      workload:  "edits" (short prompts, short replies) | "long_doc" (big prompts) | "reason" (plans, judging) | "batch" (best-of-N) | "tools" (many small calls)
    row: {"option", "plain", "benefit", "weakness", "numbers": {...}, "formula": "...", "evidence": "measured" | "vendor" | "estimate", "fit": "full" | "partial" | "cpu" | "no"}
"""

GB = 1024 ** 3
KV_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}
# bits per weight by quant name (estimates from llama.cpp's own tables); used only when no file size is known
BPW = {"F16": 16.0, "BF16": 16.0, "Q8_0": 8.5, "Q6_K": 6.6, "Q5_K_M": 5.7, "Q5_K_S": 5.5, "Q4_K_M": 4.85, "Q4_K_S": 4.6, "IQ4_XS": 4.3, "Q4_0": 4.55, "Q3_K_M": 3.9, "IQ3_M": 3.7, "Q3_K_S": 3.5, "Q2_K": 2.9, "MXFP4": 4.25}
# expected relative pass-rate loss by quant, versus Q8 (community measurements on code tasks; labelled estimate; the eval set replaces this with YOUR number)
QUALITY_LOSS = {"F16": 0.0, "BF16": 0.0, "Q8_0": 0.0, "Q6_K": 0.5, "Q5_K_M": 1.0, "Q5_K_S": 1.5, "Q4_K_M": 2.5, "Q4_K_S": 3.5, "IQ4_XS": 4.0, "Q4_0": 4.5, "Q3_K_M": 8.0, "IQ3_M": 9.0, "Q3_K_S": 12.0, "Q2_K": 20.0, "MXFP4": 2.0}
GLOSSARY = {
    "VRAM": "the memory on the graphics card; a model that fits in it runs fast",
    "KV cache": "the model's working memory for the conversation; grows with how much text it holds in mind (the context)",
    "quant": "how compactly the model's numbers are stored; smaller files are faster and a little less accurate",
    "MoE": "mixture of experts: a model that only runs part of itself for each word, so it is quick for its size but needs room for all of it",
    "draft model": "a tiny helper that guesses the next few words so the big model only has to check them",
    "offload": "keeping some layers of the model in ordinary RAM when the graphics card is full; those layers run much slower",
    "tokens": "pieces of words; about three quarters of a word each in English, less in code",
}


def kv_per_token(arch, kv="q8_0"):
    a = arch or {}
    if not (a.get("layers") and a.get("kv_heads") and a.get("head_dim")):
        return None
    return 2 * a["layers"] * a["kv_heads"] * a["head_dim"] * KV_BYTES.get(kv, 2.0)


def weights_gb(c):
    if c.get("size_gb"):
        return float(c["size_gb"])
    p = c.get("params_b")
    if p and c.get("quant"):
        return round(p * 1e9 * BPW.get(str(c["quant"]).upper(), 4.85) / 8 / GB, 2)
    return None


def _plain_size(gb):
    return f"{gb:.1f} GB" if gb is not None else "unknown size"


def fit(c, hw, ctx, kv="q8_0", overhead_gb=0.7, headroom_gb=0.35):
    """Where the model lands: -> {"fit", "ngl", "layers", "gpu_gb", "cpu_gb", "kv_gb", "total_gb", "est_tok_s", "formula"}."""
    w = weights_gb(c) or 0.0
    a = c.get("arch") or {}
    layers = int(a.get("layers") or 0)
    kvt = kv_per_token(a, kv) or 0
    kv_gb = kvt * ctx / GB
    total = w + kv_gb
    vram = max(float(hw.get("vram_gb") or 0) - overhead_gb - headroom_gb, 0.0)
    ram = max(float(hw.get("ram_gb") or 0) - 4.0, 0.0)
    import arch_hw
    saved = {}
    if not (hw.get("gpu_bw") and hw.get("ram_bw")):
        try:
            saved = arch_hw.load_bandwidth()
        except Exception:
            saved = {}
    # each figure on its own: measured (given, or saved here) or the labelled estimate. A saved GPU figure with no RAM figure used to
    # label BOTH "measured here".
    src = hw.get("bw_source")                # a figure handed in counts as measured only when it says where it was measured
    B = arch_hw.bandwidth({"gpu_bw": (hw.get("gpu_bw") if src else None) or saved.get("gpu_bw"),
                           "ram_bw": (hw.get("ram_bw") if src else None) or saved.get("ram_bw")})
    gpu_bw = float(hw.get("gpu_bw") or B["gpu_bw"])
    ram_bw = float(hw.get("ram_bw") or B["ram_bw"])
    bw_label = B["label"]
    moe = bool(a.get("experts") and a.get("experts_used"))
    read_frac = (0.15 + 0.85 * a["experts_used"] / a["experts"]) if moe else 1.0
    if vram <= 0:
        ngl, gpu_gb, cpu_gb, kind = 0, 0.0, total, "cpu"
    elif total <= vram:
        ngl, gpu_gb, cpu_gb, kind = layers, total, 0.0, "full"
    elif layers:
        per = total / layers
        ngl = int(vram / per)
        gpu_gb, cpu_gb = ngl * per, total - ngl * per
        kind = "partial" if ngl > 0 else "cpu"
    else:
        ngl, gpu_gb, cpu_gb, kind = 0, 0.0, total, "cpu"
    if cpu_gb > ram:
        kind = "no"
    t = (gpu_gb * read_frac) / gpu_bw + (cpu_gb * read_frac) / ram_bw
    est = round(1 / t, 1) if t > 0 else None
    return {"fit": kind, "ngl": ngl, "layers": layers, "gpu_gb": round(gpu_gb, 2), "cpu_gb": round(cpu_gb, 2), "kv_gb": round(kv_gb, 2), "total_gb": round(total, 2), "est_tok_s": est, "moe": moe,
            "formula": f"total = weights {w:.2f} GB + KV {kv_gb:.2f} GB (2 x {layers} layers x {a.get('kv_heads')} heads x {a.get('head_dim')} x {KV_BYTES.get(kv, 2.0)} bytes x {ctx} tokens); "
                       f"usable VRAM = {float(hw.get('vram_gb') or 0):.1f} - {overhead_gb} (driver) - {headroom_gb} (headroom); tok/s ~ 1 / (GPU bytes / {gpu_bw:.0f} GB/s + RAM bytes / {ram_bw:.0f} GB/s) (bandwidth: {bw_label})"
                       + (f"; MoE reads {read_frac:.2f} of its weights per token" if moe else "")}


def _speed(c, f):
    m = c.get("measured") or {}
    if m.get("gen_tok_s"):
        return float(m["gen_tok_s"]), "measured"
    return f["est_tok_s"], "estimate"


def _quality_hint(c):
    q = str(c.get("quant") or "").upper()
    loss = QUALITY_LOSS.get(q)
    ev = c.get("eval_pass_rate")
    if ev is not None:
        return f"passed {ev}% of the fixed tasks here", "measured"
    if loss is None:
        return "accuracy not known for this storage format", "estimate"
    return f"about {loss:.0f} points below the same model stored at 8-bit on code tasks", "estimate"


def row_for(c, hw, workload, ctx, kv="q8_0"):
    f = fit(c, hw, ctx, kv)
    tok, ev_speed = _speed(c, f)
    qual, ev_q = _quality_hint(c)
    w = weights_gb(c)
    eff = c.get("eff_b") or c.get("params_b")
    name = c.get("name", "?")
    numbers = {"weights_gb": w, "kv_gb_at_ctx": f["kv_gb"], "total_gb": f["total_gb"], "layers_on_gpu": f"{f['ngl']}/{f['layers']}" if f["layers"] else "?", "tok_s": tok, "ctx": ctx, "quant": c.get("quant"), "eff_b": eff}
    # the plain sentence, by fit
    if f["fit"] == "full":
        plain = f"Fits on the graphics card with room for a {ctx}-token memory: fast."
    elif f["fit"] == "partial":
        plain = f"Too big for the card alone: {f['layers'] - f['ngl']} of {f['layers']} layers run from ordinary RAM, so replies come slowly."
    elif f["fit"] == "cpu":
        plain = "Runs on the processor only: several seconds per sentence, but it works."
    else:
        plain = "Does not fit this machine at all (more than RAM and the card together)."
    benefit, weakness = [], []
    if f["moe"]:
        benefit.append("quick for its size: only part of the model runs for each word")
        weakness.append(f"needs room for all of it ({_plain_size(w)}) even though only part is used")
    if eff:
        benefit.append(f"about a {eff}B-class model: {'strong at reasoning and larger edits' if eff >= 12 else 'solid at everyday edits' if eff >= 6 else 'fast, best for small, well-defined steps'}")
    if c.get("think"):
        benefit.append("thinks before answering: better plans and judgements")
        weakness.append("spends tokens thinking on simple edits (slower)")
    qn = str(c.get("quant") or "").upper()
    if qn in ("Q2_K", "Q3_K_S", "Q3_K_M", "IQ3_M"):
        weakness.append("stored very compactly: noticeably less accurate on code")
    elif qn in ("Q8_0", "Q6_K", "F16", "BF16"):
        weakness.append("stored at high precision: larger and slower than a 4-bit copy of the same model")
    if f["fit"] == "partial":
        weakness.append("every reply waits on RAM, not the card")
    if workload == "long_doc" and ctx < 16384:
        weakness.append("a long document needs a longer memory than this setting")
    if workload == "batch" and f["fit"] != "full":
        weakness.append("producing several answers at once needs the whole model on the card")
    if workload == "reason" and (eff or 0) < 6:
        weakness.append("small models plan poorly: use for edits, not for judging")
    return {"option": name, "plain": plain, "benefit": "; ".join(benefit) or "simple and small", "weakness": "; ".join(weakness) or "none worth noting at this size",
            "numbers": numbers, "speed_evidence": ev_speed, "quality": qual, "quality_evidence": ev_q, "formula": f["formula"], "fit": f["fit"]}


def compare_quants(c, hw, ctx, quants=("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_K_S", "IQ4_XS", "Q3_K_M")):
    """The same model at different storage formats: size, fit and the expected accuracy loss, with the plain line first."""
    p = c.get("params_b") or c.get("eff_b")
    out = []
    for q in quants:
        cc = dict(c, quant=q, size_gb=None, params_b=p, name=f"{c.get('name', 'model')} @ {q}")
        r = row_for(cc, hw, "edits", ctx)
        r["plain"] = {"full": "fits on the card", "partial": "spills into RAM", "cpu": "processor only", "no": "does not fit"}[r["fit"]] + f" at {q}: " + {
            "Q8_0": "closest to the original, biggest file", "Q6_K": "almost no loss, still large", "Q5_K_M": "a good balance of size and accuracy", "Q4_K_M": "the usual choice on a small card",
            "Q4_K_S": "a bit smaller, a bit less accurate", "IQ4_XS": "compact; a small accuracy cost", "Q3_K_M": "small enough for tight cards; visibly less accurate on code"}[q]
        out.append(r)
    return out


def speculation(worker, draft, hw, ctx, accept=0.7):
    """What a draft model buys: speed-up ~ (1 + accepted drafts) / (1 + draft cost share). Plain line first; labelled estimate unless measured."""
    fw, fd = fit(worker, hw, ctx), fit(draft, hw, min(ctx, 4096))
    both = fw["total_gb"] + fd["total_gb"]
    vram = float(hw.get("vram_gb") or 0) - 0.7
    fits = both <= vram and fw["fit"] == "full"
    draft_n = 4
    cost = (weights_gb(draft) or 0.5) / max(weights_gb(worker) or 4.0, 0.1)
    speedup = (1 + accept * draft_n) / (1 + draft_n * cost)
    m = (worker.get("measured") or {}).get("draft_speedup")
    plain = (f"A tiny helper guesses the next words and the main model only checks them: about {m or round(speedup, 1)}x faster when its guesses are right"
             + (" (it fits beside the main model here)." if fits else ", but both together do not fit on the card here, so it would slow things down."))
    return {"option": f"{worker.get('name')} + draft {draft.get('name')}", "plain": plain, "benefit": "faster replies on code, where guesses are usually right", "weakness": "no gain on long prompts (reading, not writing, is the cost); needs card memory for both",
            "numbers": {"both_gb": round(both, 2), "fits": fits, "expected_speedup": m or round(speedup, 2), "acceptance_assumed": accept}, "speed_evidence": "measured" if m else "estimate",
            "formula": f"speed-up ~ (1 + {accept} x {draft_n} drafted tokens) / (1 + {draft_n} x {cost:.2f} draft/worker size ratio)", "fit": "full" if fits else "no"}


def advise(hw, candidates, workload="edits", ctx=8192, kv="q8_0"):
    rows = [row_for(c, hw, workload, ctx, kv) for c in candidates]
    order = {"full": 0, "partial": 1, "cpu": 2, "no": 3}
    rows.sort(key=lambda r: (order[r["fit"]], -(r["numbers"].get("eff_b") or 0)))
    return {"machine": {"vram_gb": hw.get("vram_gb"), "ram_gb": hw.get("ram_gb"), "gpu": hw.get("gpu"), "cpu_cores": hw.get("cpu_cores")},
            "workload": workload, "ctx": ctx, "kv": kv, "rows": rows, "glossary": GLOSSARY,
            "reading": "Rows are ordered by where the model lands (on the card first). 'measured' numbers come from this machine; 'estimate' from the formula shown; the fixed-task eval is the real quality number."}


def render(adv):
    lines = [f"Model options for {adv['workload']} on this machine ({adv['machine'].get('gpu') or 'no GPU'}, {adv['machine'].get('vram_gb') or 0:.1f} GB on the card, {adv['machine'].get('ram_gb') or 0:.0f} GB RAM), {adv['ctx']}-token memory, {adv['kv']} cache:"]
    for r in adv["rows"]:
        n = r["numbers"]
        lines.append(f"\n  {r['option']}")
        lines.append(f"    {r['plain']}")
        lines.append(f"    + {r['benefit']}")
        lines.append(f"    - {r['weakness']}")
        lines.append(f"    numbers: {_plain_size(n.get('weights_gb'))} weights + {n.get('kv_gb_at_ctx')} GB memory = {n.get('total_gb')} GB; {n.get('layers_on_gpu')} layers on the card; "
                     f"about {n.get('tok_s') or '?'} tokens/s ({r['speed_evidence']}); accuracy: {r['quality']} ({r['quality_evidence']})")
        lines.append(f"    formula: {r['formula']}")
    lines.append("\n  Words: " + "; ".join(f"{k} = {v}" for k, v in adv["glossary"].items()))
    return "\n".join(lines)


def candidates_from_passports(passports):
    """Passports (arch_passport.build) -> candidate dicts for advise()."""
    out = []
    for p in passports:
        a = p.get("arch") or {}
        out.append({"name": p["name"], "size_gb": p.get("size_gb"), "arch": a, "params_b": a.get("params_b") or p.get("eff_b"), "active_b": p.get("active_b"), "eff_b": p.get("eff_b"),
                    "quant": p.get("quant"), "think": bool(p.get("think_style")), "measured": p.get("measured") or {}})
    return out
