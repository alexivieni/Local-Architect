"""The workload broker: one job in, one place to run it out, with the reason in plain words.

    route(job) -> {"target", "why", "eta_s", "cost_usd", "evidence", "kind", "considered"}

A job is {"kind", "size_tokens", "deadline_s", "max_cost_usd", "needs_quality"} (optionally "prompt_tokens"):
  kind           generate | plan | judge | summarise   (model work)   test | index | ingest | export   (CPU work, never a model)
  size_tokens    tokens the reply is expected to write (CPU work: ignored)
  deadline_s     the answer is wanted within this many seconds; 0/None = no deadline
  max_cost_usd   the most this one job may cost in money; 0/None = it must stay free (local)
  needs_quality  low | normal | high (True = high, False = normal)

Targets: gpu_worker (the worker tier on the card), heavy (the larger tier), cpu_router (the router tier on the CPU; for CPU work on a
machine without efficiency cores, the CPU lane), ecores (the efficiency cores: tests, scans, indexing), claude (the optional cloud
tier), wait (nothing suitable is free right now; eta_s is a guess at how long until it is).

Policy: local first. Claude is considered only when the local tiers cannot meet the job's quality or its deadline, and only when
arch_cloud.allowed() says yes AND the estimated cost fits the job's own max_cost_usd, what is left of the per-run cap and what is left
of today's cap. Speeds come from what was measured on this machine (the passports' gen_tok_s); without one, from the measured memory
bandwidth (hub_data/bandwidth.json) and the model's size -- labelled "estimate" -- and only then a fixed fallback. The broker never
starts or stops a server, never sends a request: it only decides, and every decision is appended to hub_data/broker.jsonl so the
choices become data that can be checked against what really happened.

`route(job, state=...)` takes the machine's state as a plain dict (tests pass fakes); `gather(cfg)` reads it from the live machine."""
import json
import os
import time

import arch_core as core

KINDS_MODEL = ("generate", "plan", "judge", "summarise")
KINDS_CPU = ("test", "index", "ingest", "export")
KINDS = KINDS_MODEL + KINDS_CPU
TARGETS = ("gpu_worker", "heavy", "cpu_router", "ecores", "claude", "wait")

QUALITY = {"low": 1, "normal": 2, "high": 3}
TIER_QUALITY = {"router": 1, "worker": 2, "heavy": 3}       # what each tier can be trusted with
TIER_TARGET = {"router": "cpu_router", "worker": "gpu_worker", "heavy": "heavy"}
KIND_FLOOR = {"generate": 2, "plan": 2, "judge": 1, "summarise": 1}   # the router does not write code or plans, whatever was asked

START_S = 8.0               # bringing a stopped tier up: measured 5-8 s restart here (CLAUDE.md, Make room)
BUSY_WAIT_S = 30.0          # a slot busy with another answer: a guess at the wait (estimate)
GPU_HOT_PCT = 85            # the card this busy with work that is not ours slows every tier on it ...
GPU_HOT_FACTOR = 2.0        # ... by about this much (estimate)
BENCH_GPU_PCT = 90          # a heavy job (eval/tune/livetest) holding the card this hard is measuring: model work waits
RAM_LOW_GB = 1.5            # less free RAM than this: nothing new is started (a stopped tier, a test run)
FALLBACK_TOK_S = {"router": 20.0, "worker": 15.0, "heavy": 3.0}     # last resort when nothing was measured (estimate)
FALLBACK_PREFILL = 400.0    # prompt tokens read per second when nothing was measured (estimate)
CLOUD_TOK_S = 50.0          # Claude's writing speed (estimate; not measured here)
CLOUD_FIRST_S = 3.0         # Claude's time to first token (estimate)
CPU_JOB_S = {"test": 20.0, "index": 30.0, "ingest": 15.0, "export": 10.0}   # typical CPU job length (estimate)
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000

WORDS = {"gpu_worker": "the worker model on the graphics card", "heavy": "the larger (heavy) model",
         "cpu_router": "the small router model on the processor", "ecores": "the processor's efficiency cores",
         "claude": "Claude (the online tier, paid)", "wait": "nowhere yet: it waits"}


# ------------------------------------------------------------------------------------------------------------ the job
def normalise(job):
    """-> a clean copy of the job; raises ValueError on an unknown kind (a typo must not silently become 'generate')."""
    j = dict(job or {})
    kind = str(j.get("kind") or "").strip().lower()
    if kind == "summarize":
        kind = "summarise"
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {j.get('kind')!r}; one of: {', '.join(KINDS)}")
    q = j.get("needs_quality")
    q = "high" if q is True else "normal" if q in (None, False, "") else str(q).strip().lower()
    if q not in QUALITY:
        q = "normal"

    def num(x, default=0.0):
        try:
            v = float(x)
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default
    return {"kind": kind, "size_tokens": num(j.get("size_tokens"), 500.0), "prompt_tokens": num(j.get("prompt_tokens")),
            "deadline_s": num(j.get("deadline_s")), "max_cost_usd": num(j.get("max_cost_usd")), "needs_quality": q}


# ------------------------------------------------------------------------------------------------------------ speeds
def tok_s_for(tier, rec, bandwidth):
    """-> (tokens per second, "measured" | "estimate") for one tier record {"tok_s", "size_gb", "on_cpu"}."""
    if rec.get("tok_s"):
        return float(rec["tok_s"]), "measured"
    bw = (bandwidth or {}).get("ram_bw" if rec.get("on_cpu") else "gpu_bw")
    if bw and rec.get("size_gb"):              # writing one token reads every weight once: tok/s ~ bandwidth / size
        return round(float(bw) / float(rec["size_gb"]), 1), "estimate"
    return FALLBACK_TOK_S.get(tier, 10.0), "estimate"


def _local_option(tier, rec, job, state):
    """-> one candidate {"target", "tier", "eta_s", "evidence", "ok", "note"} for a local tier, or None when it is not there at all."""
    if not rec or not rec.get("configured", True):
        return None
    tok_s, ev = tok_s_for(tier, rec, state.get("bandwidth"))
    notes = [f"{tok_s:g} tok/s ({ev})"]
    # the ETA is "measured" only while every part of it is: a fixed start time, busy wait, prefill speed or hot-card factor makes it
    # an estimate (the speed itself keeps its own label in the note)
    if job["prompt_tokens"] and not rec.get("prefill_tok_s"):
        ev = "estimate"
    eta = job["size_tokens"] / max(tok_s, 0.1) + job["prompt_tokens"] / float(rec.get("prefill_tok_s") or FALLBACK_PREFILL)
    ok = True
    if not rec.get("up"):
        ram = state.get("ram_free_gb")
        if ram is not None and ram < RAM_LOW_GB:
            return {"target": TIER_TARGET[tier], "tier": tier, "eta_s": None, "evidence": ev, "ok": False,
                    "note": f"stopped, and only {ram:g} GB of RAM is free to start it"}
        eta += float(rec.get("load_s") or START_S)
        ev = ev if rec.get("load_s") else "estimate"
        notes.append("not running: it has to start first")
    if rec.get("busy"):
        eta += BUSY_WAIT_S
        ev = "estimate"
        notes.append("busy with another answer")
    gpu = state.get("gpu_pct")
    if gpu is not None and gpu >= GPU_HOT_PCT and not rec.get("on_cpu") and not rec.get("busy") and not state.get("ours_busy"):
        eta *= GPU_HOT_FACTOR                  # the card is busy and none of our tiers is the reason: another program has it
        ev = "estimate"
        notes.append(f"graphics card {gpu:g}% busy with other work")
    return {"target": TIER_TARGET[tier], "tier": tier, "eta_s": round(eta, 1), "evidence": ev, "ok": ok, "note": "; ".join(notes)}


def _claude_option(job, cloud):
    """-> {"target": "claude", "eta_s", "cost_usd", "ok", "note"}; ok False with the plain reason when it may not be used."""
    cloud = cloud or {}
    eta = round(CLOUD_FIRST_S + job["size_tokens"] / CLOUD_TOK_S, 1)
    if not cloud.get("ok"):
        return {"target": "claude", "eta_s": eta, "cost_usd": 0.0, "ok": False, "evidence": "estimate",
                "note": cloud.get("why") or "Claude is off"}
    import arch_cloud
    model = cloud.get("model") or arch_cloud.PREFIX + arch_cloud.DEFAULT_MODEL
    usd = round(arch_cloud.cost(model, job["prompt_tokens"], job["size_tokens"]), 4)
    run_left = max(0.0, float(cloud.get("run_cap", 0.5)) - float(cloud.get("spent_run", 0.0)))
    day_left = max(0.0, float(cloud.get("day_cap", 5.0)) - float(cloud.get("spent_today", 0.0)))
    why = ("this job allows no spending (max_cost_usd 0)" if not job["max_cost_usd"] else
           f"about ${usd:.4f} is more than this job's limit of ${job['max_cost_usd']:.4f}" if usd > job["max_cost_usd"] else
           f"about ${usd:.4f} is more than the ${run_left:.4f} left of this run's cap" if usd > run_left else
           f"about ${usd:.4f} is more than the ${day_left:.4f} left of today's cap" if usd > day_left else "")
    return {"target": "claude", "eta_s": eta, "cost_usd": usd, "ok": not why, "evidence": "estimate",
            "note": why or f"about ${usd:.4f} with {arch_cloud.api_model(model)}"}


# ------------------------------------------------------------------------------------------------------------ the decision
def _decide(job, state):
    kind = job["kind"]
    if kind in KINDS_CPU:
        ram = state.get("ram_free_gb")
        base = CPU_JOB_S[kind]
        if ram is not None and ram < RAM_LOW_GB:
            return _d("wait", f"only {ram:g} GB of RAM is free; starting a {kind} job now could push the model servers out of memory",
                      30.0, "estimate", [])           # the RAM figure is measured; the 30 s wait is a fixed guess
        held = state.get("heavy_job")
        if state.get("ecores"):
            return _d("ecores", f"{kind} is processor work, not model work; the {state['ecores']} efficiency cores run it so the "
                      "performance cores stay free for the model servers", base, "estimate", [])
        if held:
            return _d("wait", f"no efficiency cores here and a heavy job ({held}) already holds the processor", 60.0, "estimate", [])
        return _d("cpu_router", f"{kind} is processor work, not model work; this processor has no efficiency cores, so it shares "
                  "the processor with the router", base, "estimate", [])

    tiers = state.get("tiers") or {}
    need = max(QUALITY[job["needs_quality"]], KIND_FLOOR[kind])
    held, gpu = state.get("heavy_job"), state.get("gpu_pct")
    if held and gpu is not None and gpu >= BENCH_GPU_PCT and not job["deadline_s"]:
        return _d("wait", f"a heavy job ({held}) is measuring the graphics card ({gpu:g}% busy); work started now would skew its "
                  "numbers, and this job has no deadline", 120.0, "estimate", [])     # the 120 s wait is a fixed guess
    opts = [o for o in (_local_option(t, tiers.get(t), job, state) for t in ("router", "worker", "heavy")) if o]
    usable = [o for o in opts if o["ok"]]
    good = [o for o in usable if TIER_QUALITY[o["tier"]] >= need]
    best = min(good, key=lambda o: o["eta_s"]) if good else None
    considered = [{k: o[k] for k in ("target", "eta_s", "ok", "note")} for o in opts]
    dl = job["deadline_s"]
    if best and (not dl or best["eta_s"] <= dl):
        why = f"local first: {WORDS[best['target']]} is good enough for {job['needs_quality']}-quality {kind} work ({best['note']})"
        return _d(best["target"], why, best["eta_s"], best["evidence"], considered)

    cl = _claude_option(job, state.get("cloud"))
    considered.append({k: cl[k] for k in ("target", "eta_s", "ok", "note")})
    gap = (f"no local model meets the deadline of {dl:g} s (best: {best['eta_s']:g} s)" if best else
           f"no local model that is running or can start is trusted with {job['needs_quality']}-quality {kind} work")
    if cl["ok"] and (not dl or cl["eta_s"] <= dl or not best):
        return _d("claude", f"{gap}; Claude fits every money cap ({cl['note']})", cl["eta_s"], "estimate", considered, cl["cost_usd"])
    if best:
        return _d(best["target"], f"{gap}, and Claude is not an option ({cl['note']}); {WORDS[best['target']]} is the fastest local "
                  "choice, so it runs late rather than not at all", best["eta_s"], best["evidence"], considered)
    if usable:                                 # something local is up but below the quality asked: better than nothing
        lower = min(usable, key=lambda o: (-TIER_QUALITY[o["tier"]], o["eta_s"]))
        return _d(lower["target"], f"{gap}, and Claude is not an option ({cl['note']}); {WORDS[lower['target']]} is the best "
                  "available here, below the quality asked -- the checks will judge its work as always", lower["eta_s"],
                  lower["evidence"], considered)
    return _d("wait", f"no local model is running or can start, and Claude is not an option ({cl['note']})",
              BUSY_WAIT_S, "estimate", considered)


def _d(target, why, eta, evidence, considered, cost=0.0):
    return {"target": target, "why": why, "eta_s": None if eta is None else round(float(eta), 1), "cost_usd": round(float(cost), 4),
            "evidence": evidence, "considered": considered}


def route(job, state=None, cfg=None, log=True):
    """Decide where one job runs. `state` is the machine's state (gather() when omitted). Appends the decision to broker.jsonl
    unless log=False. Never raises for a missing reading: a value that cannot be read is simply not used."""
    j = normalise(job)
    if state is None:
        state = gather(cfg)
    d = _decide(j, state)
    d.update(kind=j["kind"], ts=round(time.time(), 3))
    if log:
        _log(j, d)
    return d


def explain(decision):
    """One decision in plain words, for the console or a receipt."""
    d = decision or {}
    t = d.get("target", "wait")
    eta = d.get("eta_s")
    when = "" if eta is None else (f" Expected to take about {eta:g} s" if t != "wait" else f" Look again in about {eta:g} s")
    when += f" ({d.get('evidence', 'estimate')})." if when else ""
    money = "" if t == "wait" else (f" Cost: about ${d['cost_usd']:.4f}." if d.get("cost_usd") else " Cost: nothing (it runs on this machine).")
    head = f"{(d.get('kind') or 'This job').capitalize()} -> {WORDS.get(t, t)}."
    return f"{head} Why: {d.get('why', '')}.{when}{money}".replace("..", ".")


# ------------------------------------------------------------------------------------------------------------ advice at a run's start
def eval_paths():
    import glob
    return sorted(glob.glob(os.path.join(core.data_dir(), "evals", "*.json")))


def family_rows(rows, flow):
    """The Cost map's rule: points whose family IS the flow id first; else the points measured through that flow."""
    same = [r for r in rows if (r.get("kind") or "") == flow]
    return same or [r for r in rows if (r.get("flow") or "") == flow]


def advise(cfg, flow, goal="", kind="generate", current=None, rows=None, state=None, log=True):
    """Suggestion only: which model the saved benchmarks measured as cheapest (C = seconds per passed task) for this flow's family,
    how sure (Thompson draws), next to the model that will actually run. Never changes any assignment.
    -> {"suggest": model | None, "why", "current", "family", "sure", "C", "target", "line"} or None when behavior.broker = off."""
    if str(((cfg or {}).get("behavior") or {}).get("broker", "suggest")) == "off":
        return None
    import arch_costmodel
    if rows is None:
        try:
            rows = arch_costmodel.from_evals(eval_paths())
        except Exception:
            rows = []
    fam = family_rows(rows or [], flow)
    family = (fam[0].get("kind") or flow) if fam else flow
    target = None
    if state is None and os.environ.get("ARCHITECT_SELFTEST") == "1":
        state = {}                                 # self-tests never read the live machine for advice
    try:
        target = route({"kind": kind if kind in KINDS else "generate"}, state=state if state is not None else gather(cfg), cfg=cfg, log=False).get("target")
    except Exception:
        pass
    priced = [r for r in fam if r.get("C")]
    out = {"suggest": None, "current": current, "family": family, "sure": None, "C": None, "target": target}
    if not priced:
        out["why"] = f"no measured benchmark for {family} tasks yet, so nothing to suggest"
        out["line"] = f"Measured: nothing yet for {family} (no saved benchmark); " + (f"{current} runs it." if current else "the assigned model runs it.")
    else:
        sure = (arch_costmodel.p_cheapest(fam).get(family) or {})
        best = min(priced, key=lambda r: r["C"])
        p = float(sure.get(best["model"], 0.0))
        out.update(suggest=best["model"], sure=round(p, 3), C=best["C"])
        out["why"] = (f"{best['model']} is cheapest for {family}: {best['C']:g} s per passed task "
                      f"({best['passed']} of {best['tasks']} passed), {round(p * 100)}% sure it is the cheapest")
        tail = ("it is also the model that runs it." if current == best["model"] else
                f"your worker {current} runs it." if current else "the assigned model runs it.")
        out["line"] = f"Measured: {best['model']} is cheapest for {family} ({best['C']:g} s per passed task, {round(p * 100)}% sure); {tail}"
    if target:
        out["line"] += f" Broker: {WORDS.get(target, target)}."
    if log:
        _log({"kind": "advice", "flow": flow, "family": family},
             {"ts": round(time.time(), 3), "target": "advice", "eta_s": None, "cost_usd": 0.0, "evidence": "measured" if out["suggest"] else "none",
              "why": out["why"] + f"; current {current}; suggest {out['suggest']}"})
    return out


# ------------------------------------------------------------------------------------------------------------ the decision log
def log_path():
    return os.path.join(core.data_dir(), "broker.jsonl")


def _log(job, d):
    """Append one line; roll to the last LOG_KEEP_LINES when the file passes LOG_MAX_BYTES. A log that cannot be written never
    stops the job."""
    p = log_path()
    rec = {"ts": d.get("ts"), "job": job, "target": d["target"], "eta_s": d["eta_s"], "cost_usd": d["cost_usd"],
           "evidence": d["evidence"], "why": d["why"]}
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        if os.path.getsize(p) > LOG_MAX_BYTES:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()[-LOG_KEEP_LINES:]
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, p)
    except OSError:
        pass


def recent(n=50):
    """-> the last n decisions (newest last); lines that do not parse are skipped."""
    try:
        with open(log_path(), encoding="utf-8") as f:
            lines = f.readlines()[-max(1, int(n)):]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def tally(n=500):
    """-> {target: count} over the last n decisions: where the work has been going."""
    out = {}
    for r in recent(n):
        out[r.get("target")] = out.get(r.get("target"), 0) + 1
    return out


# ------------------------------------------------------------------------------------------------------------ the live machine
def gather(cfg=None):
    """Read the machine's state for route(): tiers (up, busy, measured speed, size, on the CPU), GPU use, free RAM, efficiency cores,
    the heavy job holding the lock, measured bandwidth, and what Claude may cost. Every reading is best effort: one that fails is
    left out (None), never guessed."""
    if cfg is None:
        try:
            cfg = core.load_config()
        except Exception:
            cfg = {}
    st = {"tiers": {}, "gpu_pct": None, "ram_free_gb": None, "ecores": 0, "heavy_job": None, "bandwidth": {}, "cloud": {}}
    try:
        import arch_hw
        lv = arch_hw.live()
        st["gpu_pct"], st["ram_free_gb"] = lv.get("gpu_pct"), lv.get("ram_free_gb")
    except Exception:
        pass
    try:
        import arch_hw
        st["ecores"] = bin(int(arch_hw.ecore_mask() or 0)).count("1")
    except Exception:
        pass
    try:
        import arch_heavy
        h = arch_heavy.holder()
        st["heavy_job"] = (h or {}).get("job") if h and h.get("pid") and _pid_alive(h["pid"]) else None
    except Exception:
        pass
    st["bandwidth"] = core.load_json(os.path.join(core.data_dir(), "bandwidth.json")) or {}
    try:
        import arch_passport
        import arch_server
        for p in arch_server.status(cfg).get("processes") or []:
            t = p.get("tier")
            if t not in TIER_QUALITY:
                continue
            pp = (arch_passport.load(p.get("name") or "") or {}) if p.get("name") else {}
            m = pp.get("measured") or {}
            b = arch_server.busy(p["url"]) if p.get("up") and p.get("url") else None
            # unreadable /slots (None) counts as idle here: this only shapes an estimate, it never cuts off an answer
            st["tiers"][t] = {"up": bool(p.get("up")), "busy": b is True,
                              "on_cpu": bool(p.get("on_cpu")) or p.get("ngl") == 0, "model": p.get("name"),
                              "tok_s": m.get("gen_tok_s"), "prefill_tok_s": m.get("prefill_tok_s"), "load_s": m.get("load_s"),
                              "size_gb": pp.get("size_gb")}
        st["ours_busy"] = any(r["busy"] for r in st["tiers"].values())
    except Exception:
        pass
    try:
        import arch_cloud
        ok, why = arch_cloud.allowed(cfg)
        st["cloud"] = {"ok": bool(ok and arch_cloud.cloud_model(cfg)), "why": why or ("" if arch_cloud.cloud_model(cfg) else "no Claude model set"),
                       "model": arch_cloud.cloud_model(cfg), "run_cap": arch_cloud._cap(cfg), "spent_run": arch_cloud.spent()["usd"],
                       "day_cap": arch_cloud._day_cap(cfg), "spent_today": arch_cloud.spent_today()}
    except Exception:
        st["cloud"] = {"ok": False, "why": "the cloud tier could not be read"}
    return st


def _pid_alive(pid):
    try:
        import arch_server
        return bool(arch_server._alive(int(pid)))
    except Exception:
        return True                            # cannot tell: assume the lock holder is real (the cautious side)
