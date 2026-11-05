"""Swapping models per TIER (router/worker/heavy/draft) or per JOB (arch_jobs.JOBS), driven by our own math, what was measured here,
and local-AI best practice (docs/research/local_ai_playbook.md).

    plan(cfg, target)      ranked options for one tier or job: placement math, expected decode tok/s, measured C / pass rate, family
                           prior, speculation, recommended settings, and a plain-words reason. Reads only.
    lint(cfg)              best-practice findings on the CURRENT settings, each with a reason and an evidence tier. Reads only.
    trial(target, model)   a small pre-swap test: edit-format probe + 2-3 tasks of that job + decode speed. Real runs take the heavy lock.
    apply(target, model)   writes tiers/roles with an undo record; refuses without yes=True, and without a passed trial unless force.
    undo()                 puts back what the last apply() changed.

Every number is labelled: [M] measured on this machine, [E] estimate from the formulas here. Score = measured C (seconds per passed task)
when n >= MEASURED_N decided tasks exist for that (job, model); otherwise a prior-adjusted estimate, and the option says which.

    python arch_swap.py --lint
    python arch_swap.py --plan --job code        (or --tier worker)
"""
import json
import math
import os
import re
import time

import arch_core as core
import arch_jobs

GB = 1024 ** 3
TIERS = ("router", "worker", "heavy", "draft")
TIER_JOB = {"router": "route", "worker": "code", "heavy": "plan", "draft": "draft"}
MEASURED_N = 8              # decided tasks before measured C replaces the estimate
PRIOR_WEIGHT = 4            # pseudo-tasks the family prior is worth when blending with a few measured tasks
VRAM_HEADROOM_GB = 300 / 1024
RAM_HEADROOM_GB = 4.0
RAM_RESERVE_GB = 6.0        # Windows + desktop + apps (same figure as arch_server.RAM_RESERVE_GB)
SWAP_COST_S = (5, 15)       # one model load on this class of machine [B]
TASK_TOKENS = 900           # [E] output tokens for one eval task, for the estimate of seconds per task
TASK_OVERHEAD_S = 20.0      # [E] prompt reading, checks, tests per task
TRIAL_TASKS = 3
TRIAL_MAX_AGE_S = 7 * 86400
MIN_CODE_B = 4.0           # [C] smallest effective size worth trying as builder
MIN_TOK_S = {"router": 8.0, "worker": 10.0, "heavy": 3.0, "draft": 20.0}
# context window per job (best practice: router small, planner moderate, builder full)
JOB_CTX = {"route": 4096, "plan": 8192, "review": 8192, "docs": 8192, "security": 8192, "draft": 4096,
           "code": None, "fix": None, "test": None, "infra": None}       # None = the full window (limits.num_ctx)
LOW_QUANT = re.compile(r"^(IQ1|IQ2|Q2)", re.I)
MID_QUANT = re.compile(r"^(IQ3|Q3)", re.I)
CODE_JOBS = {"code", "fix", "test", "infra"}
THINK_JOBS = {"plan", "review"}


def _swap_dir():
    return os.path.join(core.data_dir(), "swap")


def _trials_path():
    return os.path.join(_swap_dir(), "trials.jsonl")


def _undo_path():
    return os.path.join(_swap_dir(), "undo.json")


# ------------------------------------------------------------------ environment (all injectable for tests)
def _target(target):
    """'worker' / 'tier:worker' / {'tier': 'worker'} / 'job:code' / 'code' -> ('tier'|'job', name)."""
    if isinstance(target, dict):
        if target.get("tier"):
            return "tier", target["tier"]
        if target.get("job"):
            return "job", target["job"]
    t = str(target or "")
    if ":" in t:
        k, v = t.split(":", 1)
        if k in ("tier", "job"):
            return k, v
    if t in TIERS:
        return "tier", t
    if t in arch_jobs.JOBS:
        return "job", t
    raise ValueError(f"unknown target {target!r}: a tier ({', '.join(TIERS)}) or a job ({', '.join(arch_jobs.JOBS)})")


def _job_of(kind, name):
    return TIER_JOB[name] if kind == "tier" else name


def env(cfg=None, **over):
    """Everything the math reads, gathered once. Keys: cfg, hw, desk_gb, desk_measured, bw, vm, facts, results, server_plan, tokenizer."""
    e = dict(over)
    e["cfg"] = cfg if cfg is not None else (over.get("cfg") or core.load_config())
    if "server_plan" not in e:
        try:
            import arch_server
            e["server_plan"] = arch_server.plan(e["cfg"])
        except Exception as ex:
            e["server_plan"] = {"error": str(ex)}
    sp = e.get("server_plan") or {}
    if "hw" not in e:
        prof = core.load_json(os.path.join(core.data_dir(), "profile.json")) or {}
        e["hw"] = prof.get("hw") or {}
        if not e["hw"].get("vram_gb"):
            try:
                import arch_hw
                e["hw"] = arch_hw.detect()
            except Exception:
                e["hw"] = {}
    e.setdefault("desk_gb", float(sp.get("desk_gb") if sp.get("desk_gb") is not None else 0.7))
    e.setdefault("desk_measured", bool(sp.get("desk_measured")))
    if "bw" not in e:
        try:
            import arch_hw
            e["bw"] = arch_hw.load_bandwidth() or {}
        except Exception:
            e["bw"] = {}
    if "vm" not in e:
        try:
            import arch_hw
            e["vm"] = arch_hw.vm_memory()
        except Exception:
            e["vm"] = {}
    if "facts" not in e:
        import arch_select
        e["facts"] = arch_select._facts()
    if "results" not in e:
        import arch_select
        e["results"] = arch_select.load_results()
    e.setdefault("tokenizer", _live_tokenizer(e["cfg"]))
    return e


def _live_tokenizer(cfg):
    memo = {}

    def tok(name):
        if name not in memo:
            try:
                import arch_server
                rm = arch_server.resolve_model(name, cfg)
                memo[name] = arch_server._tokenizer(rm["gguf"]) if rm else (None, None, None)
            except Exception:
                memo[name] = (None, None, None)
        return memo[name]
    return tok


def _passport(fact):
    try:
        import arch_passport
        p = arch_passport.load(fact.get("name"))
        if p:
            return p
    except Exception:
        pass
    return dict(fact, capabilities=fact.get("caps") or fact.get("capabilities") or [])


def _arch(f):
    a = dict(f.get("arch") or {})
    for k in ("layers", "kv_heads", "head_dim", "params_b", "experts", "experts_used"):
        if a.get(k) is None and f.get(k) is not None:
            a[k] = f[k]
    return a


def _measured_tok_s(f):
    m = f.get("measured") or {}
    return float(m["gen_tok_s"]) if m.get("gen_tok_s") else None


def _ctx_for(job, cfg, server_plan=None):
    full = int(((cfg.get("limits") or {}).get("num_ctx")) or 8192)
    w = next((p for p in (server_plan or {}).get("processes") or [] if p.get("tier") == "worker"), None)
    if w and w.get("ctx"):
        full = min(full, int(w["ctx"]))          # the builder's window is what the worker server really holds
    c = JOB_CTX.get(job)
    return full if c is None else min(c, full)


# ------------------------------------------------------------------ placement math
def placement(fact, hw, ctx, kv="q8_0", desk_gb=0.7, bw=None, vm=None, on_cpu=False, slots=1):
    """Where this model lands and what it costs. -> dict with layers on card, KV bytes, MoE --n-cpu-moe, GPU/RAM GB, headrooms,
    expected decode tok/s (bandwidth model) and plain words. Pure arithmetic over arch_server's own fit functions."""
    import arch_server
    import model_hub as mh
    a = _arch(fact)
    size = float(fact.get("size_gb") or 0)
    layers = int(a.get("layers") or 0)
    vram = float(hw.get("vram_gb") or 0)
    ram = float(hw.get("ram_gb") or 0)
    bw = bw or {}
    bg, br = float(bw.get("gpu_bw") or 158.0), float(bw.get("ram_bw") or 19.5)
    bw_label = "[M]" if bw.get("source") else "[E]"
    kvt = mh.kv_bytes_per_token(a, kv) or 0
    kv_gb = kvt * ctx * max(1, slots) / GB
    moe = bool(a.get("experts") and a.get("experts_used") and int(a["experts"]) > 1)
    frac = (float(a["experts_used"]) / float(a["experts"])) if moe else 1.0
    out = {"ctx": ctx, "kv": kv, "kv_gb": round(kv_gb, 3), "kv_bytes_per_token": round(kvt), "layers": layers, "moe": moe,
           "n_cpu_moe": 0, "ngl": 0, "full": False, "gpu_gb": 0.0, "ram_gb": 0.0, "where": ""}
    if on_cpu or vram <= 0 or not layers:
        out.update(where="cpu", ram_gb=round(size + kv_gb, 2))
        t = size * frac / br
    else:
        ngl, est, full = arch_server._ngl(size * GB, a, hw, ctx, kv, slots=slots, desktop_gb=desk_gb, headroom_gb=VRAM_HEADROOM_GB)
        if full:
            out.update(where="card", ngl=layers, full=True, gpu_gb=est)
            t = size * frac / bg
        elif moe:
            sp = arch_server._moe_split(size * GB, a, hw, ctx, kv, slots=slots, desktop_gb=desk_gb, headroom_gb=VRAM_HEADROOM_GB)
            if sp is None:
                out.update(where="cpu", ram_gb=round(size + kv_gb, 2))
                t = size * frac / br
            else:
                n, gpu_gb, ram_gb = sp
                shared = size * arch_server.MOE_SHARED
                out.update(where="moe_offload", ngl=layers, n_cpu_moe=n, gpu_gb=gpu_gb, ram_gb=ram_gb)
                t = (shared + (gpu_gb - shared - kv_gb) * frac) / bg + ram_gb * frac / br
        else:
            g = ngl / layers
            out.update(where="split" if ngl else "cpu", ngl=ngl, gpu_gb=est, ram_gb=round(size * (1 - g), 2))
            t = size * g / bg + size * (1 - g) / br
    vm_gb = float((vm or {}).get("vm_gb") or 0)
    out["vram_headroom_gb"] = round(vram - desk_gb - out["gpu_gb"], 2) if vram else None
    out["ram_headroom_gb"] = round(ram - RAM_RESERVE_GB - vm_gb - out["ram_gb"], 2) if ram else None
    out["vm_gb"] = vm_gb
    out["est_tok_s"] = round(1 / t, 1) if t > 0 else None
    out["formula"] = (f"tok/s ~ 1 / (GPU GB read per token / {bg:.0f} GB/s + RAM GB read per token / {br:.1f} GB/s) {bw_label}"
                      + (f"; MoE reads {frac:.3f} of its experts per token" if moe else "")
                      + f"; KV = {out['kv_bytes_per_token']} B/token x {ctx} at {kv} = {out['kv_gb']} GB")
    words = {"card": f"all {layers} layers on the card ({out['gpu_gb']} GB incl. a {ctx}-token memory)",
             "moe_offload": f"every layer on the card except the experts of {out['n_cpu_moe']} of {layers} layers (--n-cpu-moe {out['n_cpu_moe']}): "
                            f"{out['gpu_gb']} GB on the card, {out['ram_gb']} GB of experts in RAM",
             "split": f"{out['ngl']} of {layers} layers on the card, the rest ({out['ram_gb']} GB) on the processor",
             "cpu": f"entirely on the processor ({out['ram_gb']} GB of RAM)"}
    out["plain"] = words[out["where"]]
    return out


# ------------------------------------------------------------------ measured / prior
def _measured(results, job, model):
    import arch_select
    return arch_select._stats([r for r in results if r.get("job") == job and r.get("model") == model]).get((job, model))


def _prior(fact, job):
    try:
        import arch_modelknow
        return arch_modelknow.job_prior(_passport(fact), job)
    except Exception as ex:
        return None, [f"no family prior ({ex})"]


def _settings(fact, job):
    try:
        import arch_modelknow
        return arch_modelknow.settings_for(_passport(fact), job)
    except Exception as ex:
        return {"note": f"no family settings ({ex})"}


def speculation(fact, facts, tokenizer):
    """Which speculation applies to this model as the main model: drafts with an IDENTICAL tokenizer at <= 1/3 the size (dense),
    MTP when the model ships its own heads, n-gram always (llama-server; no extra memory)."""
    a = _arch(fact)
    pb = float(a.get("params_b") or fact.get("params_b") or 0)
    if a.get("experts") and a.get("experts_used") and pb:   # a MoE reads only its active experts per token: the draft must be small
        pb = pb * float(a["experts_used"]) / float(a["experts"]) + pb * 0.12   # against THAT (+ shared part, est.), not the total
    want = tokenizer(fact["name"])
    drafts = []
    if pb and want and want[0] and want[2]:
        for d in facts:
            da = _arch(d)
            dp = float(da.get("params_b") or d.get("params_b") or 0)
            if d["name"] == fact["name"] or not dp or dp > pb / 3 or da.get("experts") or d.get("embed"):
                continue
            if tokenizer(d["name"]) == want:
                drafts.append(d["name"])
    text = (fact.get("name", "") + " " + json.dumps(fact.get("lineage") or {})).lower()
    mtp = "mtp" in text or bool(a.get("nextn_layers"))
    return {"tokenizer": list(want) if want and want[0] else None, "drafts": drafts, "mtp": mtp, "ngram": True,
            "use": "draft " + drafts[0] if drafts else ("MTP" if mtp else "ngram"),
            "why": (f"{len(drafts)} installed draft(s) share the exact tokenizer and are <= 1/3 the size" if drafts else
                    ("the file carries its own multi-token-prediction heads" if mtp else
                     "no installed draft shares its tokenizer at <= 1/3 the size, so n-gram lookup (free, no memory)"))}


# ------------------------------------------------------------------ plan
def _quant(f):
    return str(f.get("quant") or "").upper()


def _option(f, kind, name, job, e):
    cfg, hw = e["cfg"], e["hw"]
    srv = cfg.get("server") or {}
    kv = srv.get("kv") or "q8_0"
    ctx = _ctx_for(job, cfg, e.get("server_plan"))
    on_cpu = kind == "tier" and name == "router"
    pl = placement(f, hw, ctx, kv=kv, desk_gb=e["desk_gb"], bw=e["bw"], vm=e["vm"], on_cpu=on_cpu)
    mt = _measured_tok_s(_passport(f)) if f.get("measured") is None else _measured_tok_s(f)
    est = pl["est_tok_s"]
    if mt and est:
        tok_s = min(mt, est)
        tok_src = f"[M] passport {mt} (placement it was measured at unknown) vs [E] bandwidth model {est}; the lower is used"
    elif mt:
        tok_s, tok_src = mt, "[M] passport"
    else:
        tok_s, tok_src = est, "[E] bandwidth model"
    meas = _measured(e["results"], job, f["name"])
    prior, prior_why = _prior(f, job)
    checks = []
    q = _quant(f)
    if job in CODE_JOBS and LOW_QUANT.match(q):
        checks.append(("error", f"{q} is a 2-bit-class quant: too lossy for code [B]"))
    elif job in CODE_JOBS and MID_QUANT.match(q):
        checks.append(("warn", f"{q} is below Q4_K_M; code edits suffer first [B]"))
    eff = float(f.get("eff_b") or 0)
    if (kind == "tier" and name == "worker" or job in CODE_JOBS) and eff and eff < MIN_CODE_B:
        checks.append(("error", f"about {eff}B: under {MIN_CODE_B:g}B is too small to be the builder (edits fail more than speed saves) [C]"))
    if (kind == "tier" and name == "worker" or job in CODE_JOBS) and not pl["full"]:
        checks.append(("error", "the worker must be 100% on the card; this one is not, so it is not offered as worker [B]"))
    if pl["vram_headroom_gb"] is not None and pl["where"] != "cpu" and pl["vram_headroom_gb"] < VRAM_HEADROOM_GB:
        checks.append(("warn", f"leaves {pl['vram_headroom_gb']} GB of graphics memory, under the {round(VRAM_HEADROOM_GB * 1024)} MiB headroom [B]"))
    if pl["ram_headroom_gb"] is not None and pl["ram_headroom_gb"] < RAM_HEADROOM_GB:
        checks.append(("error" if pl["ram_headroom_gb"] < 0 else "warn",
                       f"RAM left after this, Windows ({RAM_RESERVE_GB:.0f} GB) and Docker/WSL ({pl['vm_gb']} GB): {pl['ram_headroom_gb']} GB, under {RAM_HEADROOM_GB:.0f} GB [B]"))
    if kind == "tier" and name == "draft":
        w = next((x for x in e["facts"] if x["name"] == (cfg.get("tiers") or {}).get("worker")), None)
        if w:
            sp_w = speculation(w, e["facts"], e["tokenizer"])
            if f["name"] not in sp_w["drafts"]:
                checks.append(("error", "not a valid draft for the current worker: tokenizer differs or it is over 1/3 the size [B]"))
    eligible = not any(s == "error" for s, _ in checks)
    if meas and meas["n"] >= MEASURED_N and meas["C"] is not None:
        score, src = float(meas["C"]), "measured"
        p_used = meas["p"]
    else:
        p0 = prior if prior is not None else 0.4
        if meas and meas["n"]:
            p_used = (meas["passed"] + p0 * PRIOR_WEIGHT) / (meas["n"] + PRIOR_WEIGHT)
            src = f"prior-adjusted estimate ({meas['n']} measured task(s) blended with the family prior)"
        else:
            p_used, src = p0, "estimate (family prior; nothing measured for this job)"
        t = (TASK_TOKENS / tok_s if tok_s else 600.0) + TASK_OVERHEAD_S
        score = round(t / max(p_used, 0.05), 1)
    st = _settings(f, job)
    if job in CODE_JOBS:
        st = dict(st, thinking=False, thinking_rule="off for code edits [B]")
    elif job in THINK_JOBS and st.get("thinking_mode") not in (None, "none"):
        st = dict(st, thinking=True, thinking_rule="on for plan/review [B]")
    spec = speculation(f, e["facts"], e["tokenizer"])
    rec = {"ctx": ctx, "kv": kv if pl["where"] != "cpu" else kv, "thinking": st.get("thinking"), "sampling": st.get("sampling"),
           "speculation": spec["use"], "n_cpu_moe": pl["n_cpu_moe"] or None}
    reason = (f"{f['name']}: {pl['plain']}; about {tok_s} tok/s {tok_src}; "
              + (f"measured {meas['passed']}/{meas['n']} passed (95% {meas['ci'][0]:.2f}-{meas['ci'][1]:.2f}), C = {meas['C']} s per passed task [M]; "
                 if meas else "not measured on this job yet; ")
              + f"family prior {prior} for {job}; score {score} s per passed task ({src})"
              + ("; " + "; ".join(m for _, m in checks) if checks else ""))
    return {"model": f["name"], "eligible": eligible, "score": score, "score_source": src, "placement": pl, "tok_s": tok_s,
            "tok_s_source": tok_src, "tok_s_measured": mt, "tok_s_estimate": est, "measured": meas, "prior": prior, "prior_reasons": prior_why, "p_used": round(p_used, 3),
            "speculation": spec, "settings": rec, "checks": [{"level": s, "msg": m} for s, m in checks], "reason": reason,
            "quant": q, "size_gb": f.get("size_gb")}


def _current(cfg, kind, name):
    if kind == "tier":
        return (cfg.get("tiers") or {}).get(name) or ""
    return arch_jobs.current(cfg)[name]["model"]


def plan(cfg=None, target="worker", candidates=None, e=None):
    """-> {"target", "job", "current", "options": [ranked, eligible first, lowest score first], "text"}. Reads only."""
    import arch_select
    e = e or env(cfg)
    cfg = e["cfg"]
    kind, name = _target(target)
    job = _job_of(kind, name)
    facts = arch_select.candidates(cfg, e["facts"], allowed=candidates)
    opts = [_option(f, kind, name, job, e) for f in facts]
    opts.sort(key=lambda o: (not o["eligible"], o["score"]))
    cur = _current(cfg, kind, name)
    lines = [f"Swap plan for {kind} {name} (job {job}); current: {cur or '(none)'}"]
    for i, o in enumerate(opts, 1):
        tag = "" if o["eligible"] else "  [not offered]"
        lines.append(f" {i:>2}. {o['model']}{'  (current)' if o['model'] == cur else ''}{tag}")
        lines.append(f"     score {o['score']} s/passed task -- {o['score_source']}")
        lines.append(f"     {o['reason']}")
        s = o["settings"]
        lines.append(f"     settings: ctx {s['ctx']}, KV {s['kv']}, thinking {'on' if s['thinking'] else 'off'}, speculation {s['speculation']}"
                     + (f", --n-cpu-moe {s['n_cpu_moe']}" if s.get("n_cpu_moe") else ""))
    lines.append(f"Score = measured C (seconds per passed task) when >= {MEASURED_N} tasks were decided, else estimate. "
                 f"Nothing changes until `swap --trial` passes and `swap --apply ... --yes`.")
    return {"target": {"kind": kind, "name": name}, "job": job, "current": cur, "options": opts, "text": "\n".join(lines)}


# ------------------------------------------------------------------ lint (best practice on the current settings)
RULES = {
    "worker_on_card": "The worker is called most; any layer off the card costs 3-10x per token.",
    "code_quant": "2-bit quants break code edits first; Q4_K_M or better for code.",
    "kv_q8": "q8_0 KV halves cache memory at near-zero quality cost; f16 only when there is room.",
    "ctx_per_role": "Router needs ~4k, planner ~8k, builder the full window; a bigger window costs KV memory and prompt time.",
    "moe_cpu_ollama": "A MoE heavy model belongs beside the worker with only its experts in RAM (--n-cpu-moe); CPU-only via Ollama is several times slower.",
    "thinking": "Thinking off for code edits (time, format drift), on for plan/review.",
    "draft": "A draft must have an identical tokenizer and be <= 1/3 the worker's size; otherwise use n-gram or MTP.",
    "swap_churn": "One model swap costs ~5-15 s; roles naming models outside the tiers swap per call.",
    "vram_headroom": "Leave >= 300 MiB of graphics memory free, or the driver pages and decode collapses.",
    "ram_headroom": "Leave >= 4 GB of RAM after models, Windows and Docker/WSL, or Windows pages to disk.",
}


def lint(cfg=None, e=None):
    """-> [{"rule", "level": error|warn|info, "msg", "why", "fix"}] for the current settings. Reads only."""
    e = e or env(cfg)
    cfg = e["cfg"]
    hw = e["hw"]
    tiers = cfg.get("tiers") or {}
    roles = cfg.get("roles") or {}
    srv = cfg.get("server") or {}
    lim = cfg.get("limits") or {}
    mo = cfg.get("model_overrides") or {}
    fb = {f["name"]: f for f in e["facts"]}
    sp = e.get("server_plan") or {}
    procs = {p.get("tier"): p for p in sp.get("processes") or []}
    out = []

    def add(rule, level, msg, fix):
        out.append({"rule": rule, "level": level, "msg": msg, "why": RULES[rule], "fix": fix})

    w = fb.get(tiers.get("worker") or "")
    full_ctx = int(lim.get("num_ctx") or 8192)
    if w:
        pl = placement(w, hw, min(full_ctx, int((procs.get("worker") or {}).get("ctx") or full_ctx)), kv=srv.get("kv") or "q8_0",
                       desk_gb=e["desk_gb"], bw=e["bw"], vm=e["vm"])
        if not pl["full"]:
            add("worker_on_card", "error", f"worker {w['name']}: {pl['plain']}", "pick a worker that fits whole (swap --plan --tier worker)")
        if pl["vram_headroom_gb"] is not None and pl["full"] and pl["vram_headroom_gb"] < VRAM_HEADROOM_GB:
            add("vram_headroom", "warn", f"worker leaves {pl['vram_headroom_gb']} GB free on the card", "a smaller window or quant")
    code_models = {tiers.get("worker")} | {roles.get(r) for r in sum((arch_jobs.JOBS[j]["roles"] for j in CODE_JOBS), []) if roles.get(r)}
    for m in sorted(x for x in code_models if x):
        q = _quant(fb.get(m) or {})
        if LOW_QUANT.match(q):
            add("code_quant", "error", f"{m} does code at {q}", "a Q4_K_M or better build of a model that fits")
        elif MID_QUANT.match(q):
            add("code_quant", "warn", f"{m} does code at {q}", "Q4_K_M or better when one fits")
    if str(srv.get("kv") or "q8_0") not in ("q8_0",):
        add("kv_q8", "info" if srv.get("kv") == "f16" else "warn", f"server.kv = {srv.get('kv')}", "server.kv = q8_0")
    # ctx per role
    r_ctx = int((procs.get("router") or {}).get("ctx") or (mo.get(tiers.get("router") or "") or {}).get("num_ctx") or full_ctx)
    if r_ctx > 4096:
        add("ctx_per_role", "warn", f"router window {r_ctx} tokens", "4096 is enough to pick a flow")
    for r in arch_jobs.JOBS["plan"]["roles"]:
        m = roles.get(r) or tiers.get("worker")
        c = int((mo.get(m) or {}).get("num_ctx") or full_ctx)
        if c > JOB_CTX["plan"]:
            add("ctx_per_role", "warn", f"{r} ({m}) runs with a {c}-token window (limits.num_ctx, no per-model override)",
                f"model_overrides[{m!r}].num_ctx = {JOB_CTX['plan']} (planner moderate)")
            break
    if full_ctx < 8192:
        add("ctx_per_role", "warn", f"builder window only {full_ctx} tokens", "limits.num_ctx >= 8192 for code")
    # MoE heavy on Ollama CPU
    for m in sorted({v for v in roles.values() if v} | ({tiers.get("heavy")} - {None, ""})):
        f = fb.get(m)
        if not f or not (f.get("moe") or _arch(f).get("experts")):
            continue
        hp = procs.get("heavy") or {}
        served = hp.get("name") == m and hp.get("resident")
        users = sorted(r for r, v in roles.items() if v == m)
        if not served and str(srv.get("ollama_gpu", "auto")) == "auto" and users:
            mt = _measured_tok_s(f)
            add("moe_cpu_ollama", "error",
                f"{m} (MoE) is named for {', '.join(users)}, but its llama-server is not resident (fast lane: starts only on escalation), so those "
                f"calls go to Ollama, which keeps it entirely in RAM while the worker holds the card (server.ollama_gpu=auto): CPU-only"
                + (f", measured {mt} tok/s [M]" if mt else ""),
                "serve it as the heavy tier beside the worker with experts in RAM (server.heavy_beside, --n-cpu-moe), or name the heavy "
                "TIER for these roles so they use its server; otherwise give them a dense model that fits")
    # thinking
    for m in sorted(x for x in code_models if x):
        if (mo.get(m) or {}).get("think") is True:
            add("thinking", "warn", f"{m} thinks on code edits (model_overrides.think = true)", "think off for code; on for plan/review")
    if str((cfg.get("behavior") or {}).get("thinking", "roles")) == "on":
        add("thinking", "warn", "behavior.thinking = on (every role thinks, code included)", "behavior.thinking = roles")
    # draft
    d = tiers.get("draft")
    if d and w and fb.get(d):
        spw = speculation(w, e["facts"], e["tokenizer"])
        if d not in spw["drafts"]:
            add("draft", "error", f"draft {d} does not share the worker's tokenizer at <= 1/3 its size", f"tiers.draft = '' (use {spw['use']})")
    # swap churn
    tier_models = {v for v in tiers.values() if v}
    extra = sorted({v for v in roles.values() if v and v not in tier_models})
    if extra:
        add("swap_churn", "warn", f"roles name {len(extra)} model(s) outside the tiers ({', '.join(extra)}): each call there swaps "
            f"(~{SWAP_COST_S[0]}-{SWAP_COST_S[1]} s)", "assign roles to tier models, or make the model a tier")
    # RAM
    h = procs.get("heavy") or {}
    ram = float(hw.get("ram_gb") or 0)
    if ram:
        used = sum(float(p.get("ram_gb") or 0) for p in procs.values())
        left = round(ram - RAM_RESERVE_GB - float((e["vm"] or {}).get("vm_gb") or 0) - used, 2)
        if left < RAM_HEADROOM_GB:
            add("ram_headroom", "error" if left < 0 else "warn",
                f"RAM left with every planned model loaded, Windows and Docker/WSL ({(e['vm'] or {}).get('vm_gb') or 0} GB): {left} GB",
                "a smaller heavy model, fewer resident tiers, or a lower WSL memory limit (your choice)")
    return out


def lint_text(findings):
    if not findings:
        return "swap --lint: nothing to flag; the current settings follow the playbook."
    lines = [f"swap --lint: {len(findings)} finding(s) (docs/research/local_ai_playbook.md)"]
    for x in findings:
        lines += [f"  [{x['level'].upper()}] {x['rule']}: {x['msg']}", f"      why: {x['why']}", f"      fix: {x['fix']}"]
    return "\n".join(lines)


# ------------------------------------------------------------------ trial
def _load_trials():
    out = []
    try:
        with open(_trials_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def trial(target, model, fake=False, cfg=None, n=TRIAL_TASKS, runner=None, ask=None, tasks=None, facts=None, say=print):
    """Edit-format probe + `n` tasks of the target's job + decode speed. -> {"verdict": pass|fail|not_sure, ...}, appended to
    <data>/swap/trials.jsonl. Real runs take the heavy-job lock; fake=True uses arch_select's scripted fakes (no GPU)."""
    import arch_probe
    import arch_select
    kind, name = _target(target)
    job = _job_of(kind, name)
    cfg = cfg or core.load_config()
    if facts is None:
        facts = arch_select._facts() if not fake else []
    fb = {m["name"]: m for m in facts}
    run_task = runner or (arch_select.fake_runner(fb) if fake else arch_select.live_runner(cfg))
    ask = ask or (arch_select.fake_ask(model) if fake else arch_probe.live_ask(model))
    eval_job = job if job in arch_jobs.JOBS else "code"
    t0 = time.time()
    with arch_select._locked(fake, cfg, say):
        md = arch_probe.modes(ask, say=lambda _m: None)
        probe_ok = md.get("recommend_edit_style") is not None
        say(f"  {model}: edit format {'ok (' + str(md.get('recommend_edit_style')) + ')' if probe_ok else 'FAILED'}")
        rows = []
        if probe_ok and eval_job != "draft":
            for t in arch_select.job_tasks(eval_job, n, 0, tasks):
                r = run_task(model, eval_job, t, 0)
                rows.append({"task": t.get("id"), "verdict": r["verdict"], "secs": r["secs"], "tok_s": r.get("tok_s")})
                say(f"    [{eval_job}] {t.get('id')}: {r['verdict']} in {r['secs']}s")
    passed = sum(r["verdict"] == "pass" for r in rows)
    dec = sum(r["verdict"] in ("pass", "fail") for r in rows)
    toks = [float(r["tok_s"]) for r in rows if r.get("tok_s")]
    tok_s = round(sorted(toks)[len(toks) // 2], 1) if toks else _measured_tok_s(fb.get(model) or {})
    floor = MIN_TOK_S.get(name if kind == "tier" else "worker", 5.0)
    why = []
    if not probe_ok:
        verdict = "fail"
        why.append("its edits applied in neither style")
    elif eval_job == "draft":
        verdict = "pass"
    elif not rows or dec == 0:
        verdict = "not_sure"
        why.append("no task could be decided")
    elif passed * 3 >= dec * 2:
        verdict = "pass"
    elif passed == 0:
        verdict = "fail"
        why.append("passed none of its tasks")
    else:
        verdict = "not_sure"
        why.append(f"passed {passed}/{dec}: below 2/3, measure more (select --measure)")
    if verdict == "pass" and tok_s is not None and tok_s < floor:
        verdict = "not_sure"
        why.append(f"decode {tok_s} tok/s is under the {floor} tok/s floor for this target")
    rec = {"ts": time.time(), "target": {"kind": kind, "name": name}, "job": eval_job, "model": model, "fake": bool(fake),
           "probe": {"ok": probe_ok, "edit_style": md.get("recommend_edit_style"), "json": md.get("json")},
           "tasks": rows, "passed": passed, "decided": dec, "tok_s": tok_s, "tok_s_floor": floor, "verdict": verdict,
           "secs": round(time.time() - t0, 1),
           "why": "; ".join(why) or f"edit format ok, passed {passed}/{dec}, {tok_s} tok/s"}
    os.makedirs(_swap_dir(), exist_ok=True)
    with open(_trials_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def last_trial(target, model, include_fake=True):
    kind, name = _target(target)
    rs = [r for r in _load_trials() if r.get("model") == model and r.get("target") == {"kind": kind, "name": name}
          and (include_fake or not r.get("fake"))]
    return rs[-1] if rs else None


# ------------------------------------------------------------------ apply / undo
def _mh():
    import model_hub as mh
    mh.CONFIG_FILE = core.CONFIG_FILE
    return mh


def _load_undo():
    try:
        with open(_undo_path(), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, list) else []
    except (OSError, ValueError):
        return []


def apply(target, model, yes=False, force=False, now=None):
    """Write the swap. -> {"ok", "msg", "record"?}. Refuses without yes=True (says what it would write), and without a passed trial
    for (target, model) in the last 7 days unless force=True. A fake trial counts only under ARCHITECT_SELFTEST."""
    kind, name = _target(target)
    if not model:
        return {"ok": False, "msg": "no model given"}
    tr = last_trial(target, model, include_fake=bool(os.environ.get("ARCHITECT_SELFTEST")))
    now = now or time.time()
    fresh = tr and tr.get("verdict") == "pass" and now - float(tr.get("ts") or 0) <= TRIAL_MAX_AGE_S
    what = (f"tiers.{name} = {model}" if kind == "tier" else f"roles {', '.join(arch_jobs.JOBS[name]['roles'])} = {model}")
    if not fresh and not force:
        return {"ok": False, "msg": f"refused: no passed trial for {kind} {name} -> {model} in the last 7 days "
                                    f"({'last verdict: ' + tr['verdict'] if tr else 'never tried'}). Run `swap --trial {name} {model}` first, or --force."}
    if not yes:
        return {"ok": False, "msg": f"would write {what}; nothing changed (add --yes)"}
    if kind == "tier":
        mh = _mh()
        raw = mh.read_config()
        t = raw.setdefault("tiers", {})
        rec = {"kind": "tier", "name": name, "model": model, "before": {name: t.get(name)}, "at": now}
        t[name] = model
        mh.write_config(raw)
    else:
        a = arch_jobs.assign(None, name, model)
        rec = {"kind": "job", "name": name, "model": model, "before": a["before"], "at": now, "jobs_at": a["at"]}
    rec["trial"] = {"verdict": tr.get("verdict"), "ts": tr.get("ts")} if tr else None
    rec["forced"] = bool(force and not fresh)
    stack = _load_undo()
    stack.append(rec)
    os.makedirs(_swap_dir(), exist_ok=True)
    core._atomic_write(_undo_path(), json.dumps(stack[-50:], indent=1))
    return {"ok": True, "msg": f"wrote {what} (undo: swap --undo)", "record": rec}


def undo():
    """Put back what the last apply() changed. -> the record undone, or None."""
    stack = _load_undo()
    if not stack:
        return None
    rec = stack.pop()
    mh = _mh()
    raw = mh.read_config()
    if rec["kind"] == "tier":
        t = raw.setdefault("tiers", {})
        old = rec["before"].get(rec["name"])
        t[rec["name"]] = old if old is not None else ""
        mh.write_config(raw)
    else:
        js = arch_jobs._load_undo()                   # drop the matching arch_jobs record too, so select --undo does not replay it
        if js and js[-1].get("at") == rec.get("jobs_at") and js[-1].get("job") == rec["name"]:
            arch_jobs.undo()
        else:
            roles = raw.setdefault("roles", {})
            for r, old in rec["before"].items():
                if old:
                    roles[r] = old
                else:
                    roles.pop(r, None)
            mh.write_config(raw)
    core._atomic_write(_undo_path(), json.dumps(stack, indent=1))
    return rec


# ------------------------------------------------------------------ CLI
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="arch_swap", description="model swaps per tier or job, by our math + measurements + best practice")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--tier")
    ap.add_argument("--job")
    ap.add_argument("--lint", action="store_true")
    ap.add_argument("--trial", nargs=2, metavar=("TARGET", "MODEL"))
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--apply", nargs=2, metavar=("TARGET", "MODEL"))
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.lint:
        f = lint()
        print(json.dumps(f, indent=1) if a.json else lint_text(f))
        return 0
    if a.plan:
        p = plan(target={"job": a.job} if a.job else {"tier": a.tier or "worker"})
        print(json.dumps(p, indent=1, default=str) if a.json else p["text"])
        return 0
    if a.trial:
        r = trial(a.trial[0], a.trial[1], fake=a.fake)
        print(json.dumps(r, indent=1) if a.json else f"trial {a.trial[0]} {a.trial[1]}: {r['verdict'].upper()} -- {r['why']}")
        return 0 if r["verdict"] == "pass" else 1
    if a.apply:
        r = apply(a.apply[0], a.apply[1], yes=a.yes, force=a.force)
        print(r["msg"])
        return 0 if r["ok"] else 1
    if a.undo:
        r = undo()
        print(f"undone: {r['kind']} {r['name']} back to {r['before']}" if r else "nothing to undo")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
