"""Choose a model PER JOB by measurement: C = t / p (seconds spent per task that PASSED its hidden checks).

    candidates(cfg)        installed chat models (no embedding / vision models), small to large; `allowed` narrows, never hides
    plan(...)              what a screen would run and roughly how many GPU minutes it costs (nothing runs)
    screen(models, jobs)   edit-format check (arch_probe.modes) + 3 quick eval tasks per job per model (fixed budget)
    measure(top_k, ...)    more tasks for the best few per job (and the current one), over several seeds
    why(cfg)               per job: current, best, C, pass rate, Wilson CI, tok/s, fits, n, plain-words reason, suggestion or None

Results are appended to <data>/select/results.jsonl. Nothing here changes a setting: only `arch_jobs.assign` (CLI `select --apply`)
does, with an undo record. Real runs take the heavy-job lock (arch_heavy.one_at_a_time) so they queue behind a running benchmark.
"""
import hashlib
import json
import math
import os
import re
import time

import arch_core as core
import arch_jobs

SCREEN_TASKS = 3            # quick tasks per job per model in a screen (the fixed budget)
PASS_FLOOR = 0.5            # a suggested model must pass at least half its decided tasks: a fast model that mostly fails is not "cheap".
                            # A constant, not a setting: below a coin flip, C is dominated by noise and retries, whatever t says.
Z = 1.96                    # 95% Wilson interval
PRIOR_TASK_S = 120.0        # estimate of seconds per eval task when nothing was measured yet (labelled estimate in plan())
PROBE_S = 60.0              # estimate of seconds for the edit-format check of one model
_NOT_CHAT = re.compile(r"embed|bge|gte-|e5-|minilm|rerank|vision|llava|-vl\b|vl:|moondream|clip", re.I)


def results_path():
    return os.path.join(core.data_dir(), "select", "results.jsonl")


def _append(rows):
    os.makedirs(os.path.dirname(results_path()), exist_ok=True)
    with open(results_path(), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def load_results(include_fake=False):
    out = []
    try:
        with open(results_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("fake") and not include_fake:
                    continue
                out.append(r)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ candidates
def _facts(refresh=False):
    """arch_port.facts() from the saved passports. refresh=False (the default here) reads them as they are, writing nothing."""
    import arch_passport
    import arch_port
    if refresh:
        return arch_port.facts()
    keep = arch_passport.refresh
    arch_passport.refresh = lambda *a, **k: None
    try:
        return arch_port.facts()
    finally:
        arch_passport.refresh = keep


def candidates(cfg=None, facts=None, allowed=None):
    """-> [fact dict] of installed chat models, smallest first. `allowed` (list of names) narrows the list; models left out are
    reported by the caller, never removed from the machine or the settings."""
    out = []
    for m in (facts if facts is not None else _facts()):
        name = m.get("name") or ""
        caps = [str(c) for c in (m.get("caps") or [])]
        if m.get("embed") or "embedding" in caps or _NOT_CHAT.search(name) or (caps and "completion" not in caps):
            continue
        if allowed and name not in allowed:
            continue
        out.append(m)
    return sorted(out, key=lambda m: (float(m.get("size_gb") or 0), float(m.get("eff_b") or 0), m.get("name")))


# ------------------------------------------------------------------ the scripted fake (no GPU)
def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def fake_ask(model):
    """An edit-format `ask` that answers correctly unless the model's name says it cannot ('noedit')."""
    import arch_probe

    def ask(system, user, fmt):
        if fmt is not None:
            return json.dumps({"flow": "bugfix", "why": "crash"})
        if "noedit" in model:
            return "I would change the greeting words."
        fixed = arch_probe.FIXTURE.replace('"hello', '"hi').replace("'hello", "'hi")
        return "FILE: greeter.py\n```python\n" + fixed + "\n```\n"
    return ask


def fake_runner(facts_by_name):
    """A deterministic scripted runner: pass chance grows with the model's estimated quality, seconds with its size."""
    import arch_port

    def run(model, job, task, seed):
        m = facts_by_name.get(model) or {}
        q = arch_port.quality(m) if m else 20.0
        p = max(0.05, min(0.95, q / 40.0))
        secs = round(5.0 + 10.0 * float(m.get("size_gb") or 2.0), 1)
        return {"verdict": "pass" if _h(model, task.get("id"), seed) < p else "fail", "secs": secs,
                "tok_s": round(200.0 / (1.0 + float(m.get("size_gb") or 2.0)), 1)}
    return run


def live_runner(cfg=None):
    import arch_eval

    def run(model, job, task, seed):
        over = {"roles": {r: model for r in arch_jobs.JOBS[job]["roles"]}, "behavior": {"learn_from_runs": "off"}}
        prev = os.environ.get("ARCHITECT_EVAL")
        os.environ["ARCHITECT_EVAL"] = "1"
        try:
            r = arch_eval.run_task(task, over, keep_run=True, label="select")
        finally:
            if prev is None:
                os.environ.pop("ARCHITECT_EVAL", None)
            else:
                os.environ["ARCHITECT_EVAL"] = prev
        secs = float(r.get("secs") or 0)
        return {"verdict": r["verdict"], "secs": secs, "tok_s": round(r.get("tokens_out", 0) / secs, 1) if secs else None}
    return run


# ------------------------------------------------------------------ tasks
_TASK_MEMO = {}

def job_tasks(job, n, seed=0, tasks=None):
    import arch_eval
    if tasks is None:
        key = (max(n, 2), seed)
        if key not in _TASK_MEMO:
            _TASK_MEMO[key] = [t for t in arch_eval.load_tasks(n=key[0], seed=seed) if not arch_eval.validate(t)]
        tasks = _TASK_MEMO[key]
    pool = arch_jobs.tasks_for(job, tasks)
    pool.sort(key=lambda t: _h(t.get("id"), seed))            # mixed across the job's families, same order for the same seed
    return pool[:n]


def _locked(fake, cfg, say):
    import contextlib
    if fake:
        return contextlib.nullcontext()
    import arch_heavy
    return arch_heavy.one_at_a_time("select", cfg, say=say)


# ------------------------------------------------------------------ screen / measure
def screen(models, jobs=None, fake=False, cfg=None, n=SCREEN_TASKS, seed=0, facts=None, runner=None, ask_for=None, tasks=None, say=print):
    """Edit-format check, then `n` tasks per job per model that passed it. -> rows appended to results.jsonl.
    A model whose edits apply in neither style is dropped (its probe row says why) and costs no task time."""
    import arch_probe
    jobs = [j for j in (jobs or list(arch_jobs.JOBS)) if j in arch_jobs.JOBS]
    facts = facts if facts is not None else (_facts() if not fake or runner is None else [])
    fb = {m["name"]: m for m in facts}
    runner = runner or (fake_runner(fb) if fake else live_runner(cfg))
    ask_for = ask_for or (fake_ask if fake else (lambda m: arch_probe.live_ask(m)))
    models = screen_order(models, jobs, fb)
    rows = []
    with _locked(fake, cfg, say):
        for model in models:
            t0 = time.time()
            md = arch_probe.modes(ask_for(model), say=lambda _m: None)
            ok = md.get("recommend_edit_style") is not None
            pr = {"ts": time.time(), "phase": "screen", "kind": "probe", "model": model, "edit_style": md.get("recommend_edit_style"),
                  "json": md.get("json"), "verdict": "pass" if ok else "fail", "secs": round(time.time() - t0, 1), "fake": bool(fake),
                  "notes": md.get("notes")}
            rows.append(pr)
            say(f"  {model}: edit format {'ok (' + str(md.get('recommend_edit_style')) + ')' if ok else 'FAILED -- dropped'}")
            if not ok:
                continue
            for job in jobs:
                for t in job_tasks(job, n, seed, tasks):
                    r = runner(model, job, t, seed)
                    row = {"ts": time.time(), "phase": "screen", "kind": "task", "model": model, "job": job, "task": t.get("id"),
                           "family": t.get("family"), "seed": seed, "verdict": r["verdict"], "secs": r["secs"], "tok_s": r.get("tok_s"), "fake": bool(fake)}
                    rows.append(row)
                    say(f"    [{job}] {t.get('id')}: {r['verdict']} in {r['secs']}s")
    _append(rows)
    return rows


def measure(top_k=2, n_per_job=5, seeds=(1, 2), cfg=None, jobs=None, fake=False, facts=None, runner=None, tasks=None, say=print):
    """More tasks for the top_k models per job (by C from what is recorded) plus the job's current model."""
    cfg = cfg or core.load_config()
    cur = arch_jobs.current(cfg)
    stats = _stats(load_results(include_fake=fake))
    facts = facts if facts is not None else (_facts() if runner is None or not fake else [])
    runner = runner or (fake_runner({m["name"]: m for m in facts}) if fake else live_runner(cfg))
    rows = []
    with _locked(fake, cfg, say):
        for job in [j for j in (jobs or list(arch_jobs.JOBS)) if j in arch_jobs.JOBS]:
            ranked = sorted((s for (j, _m), s in stats.items() if j == job), key=lambda s: s["C"] if s["C"] is not None else 1e18)
            pick = [s["model"] for s in ranked[:top_k]]
            if cur[job]["model"] and cur[job]["model"] not in pick:
                pick.append(cur[job]["model"])
            for model in pick:
                for seed in seeds:
                    for t in job_tasks(job, n_per_job, seed, tasks):
                        r = runner(model, job, t, seed)
                        rows.append({"ts": time.time(), "phase": "measure", "kind": "task", "model": model, "job": job, "task": t.get("id"),
                                     "family": t.get("family"), "seed": seed, "verdict": r["verdict"], "secs": r["secs"], "tok_s": r.get("tok_s"),
                                     "fake": bool(fake)})
                        say(f"    [{job}] {model} {t.get('id')} seed {seed}: {r['verdict']} in {r['secs']}s")
    _append(rows)
    return rows


# ------------------------------------------------------------------ the arithmetic
def wilson(passed, n, z=Z):
    """95% Wilson score interval for a pass rate -> (lo, hi). n = 0 -> (0, 1)."""
    if n <= 0:
        return (0.0, 1.0)
    p = passed / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def _stats(rows):
    """{(job, model): {model, n, passed, not_sure, t, p, C, ci, C_ci, tok_s}} over task rows. Not-sure tasks are left out of p, their
    seconds still count (as in arch_costmodel.from_evals)."""
    agg = {}
    for r in rows:
        if r.get("kind") != "task" or not r.get("job"):
            continue
        a = agg.setdefault((r["job"], r["model"]), {"model": r["model"], "n": 0, "passed": 0, "fail": 0, "secs": 0.0, "tok": []})
        a["n"] += 1
        a["secs"] += float(r.get("secs") or 0)
        a["passed"] += r.get("verdict") == "pass"
        a["fail"] += r.get("verdict") == "fail"
        if r.get("tok_s"):
            a["tok"].append(float(r["tok_s"]))
    out = {}
    for k, a in agg.items():
        dec = a["passed"] + a["fail"]
        t = a["secs"] / a["n"] if a["n"] else 0.0
        lo, hi = wilson(a["passed"], dec)
        out[k] = {"model": a["model"], "n": dec, "tasks": a["n"], "passed": a["passed"], "not_sure": a["n"] - dec, "t": round(t, 1),
                  "p": round(a["passed"] / dec, 3) if dec else 0.0, "C": round(a["secs"] / a["passed"], 1) if a["passed"] else None,
                  "ci": [round(lo, 3), round(hi, 3)],
                  "C_ci": [round(t / hi, 1) if hi > 0 else None, round(t / lo, 1) if lo > 0 else None],      # None on the right = unbounded
                  "tok_s": round(sum(a["tok"]) / len(a["tok"]), 1) if a["tok"] else None}
    return out


def _prior(model, job, fb=None):
    """-> (score 0-1, reasons) from arch_modelknow (family knowledge); (None, [why]) when it cannot be read. Orders and breaks ties only."""
    try:
        import arch_modelknow
        return arch_modelknow.job_prior((fb or {}).get(model) or model, job)
    except Exception as ex:
        return None, [f"no family prior ({ex})"]


def screen_order(models, jobs=None, fb=None):
    """Models most likely to do well on these jobs (family prior) are screened first; the measurement still decides."""
    jobs = jobs or list(arch_jobs.JOBS)

    def key(m):
        v = [_prior(m, j, fb)[0] for j in jobs]
        v = [x for x in v if x is not None]
        return -(sum(v) / len(v)) if v else 0.0
    return sorted(models, key=key)


def _fits(model, fb, hw):
    m = fb.get(model)
    if not m or not hw:
        return None
    try:
        import arch_port
        return bool(arch_port._fit(m, hw, 8192)[2])
    except Exception:
        return None


def why(cfg=None, rows=None, allowed=None, facts=None, hw=None, vol=None, include_fake=False):
    """-> {job: {current, best, C, pass_rate, ci, tok_s, fits, n, reason, suggestion, p_cheapest, models}}. Read-only.
    A switch is suggested only when the best model's C interval lies entirely below the current one's and its pass rate >= PASS_FLOOR."""
    import arch_costmodel as cm
    cfg = cfg if cfg is not None else core.load_config()
    st = _stats(rows if rows is not None else load_results(include_fake=include_fake))
    cur = arch_jobs.current(cfg)
    vol = vol if vol is not None else arch_jobs.volume()
    fb = {m["name"]: m for m in (facts or [])}
    out = {}
    for job in arch_jobs.JOBS:
        cm_ = cur[job]["model"]
        pts = {m: s for (j, m), s in st.items() if j == job and (not allowed or m in allowed or m == cm_)}
        c = pts.get(cm_)
        eligible = [s for m, s in pts.items() if s["C"] is not None and s["p"] >= PASS_FLOOR and (not allowed or m in allowed)]
        best = min(eligible, key=lambda s: (s["C"], -(_prior(s["model"], job, fb)[0] or 0))) if eligible else None   # ties: family prior
        v = (vol.get(job) or {}).get("calls")
        try:
            pc = cm.p_cheapest([dict(s, kind=job) for s in pts.values()], draws=500).get(job, {}) if pts else {}
        except Exception:
            pc = {}
        sug, reason = None, ""
        if not arch_jobs.JOBS[job]["families"]:
            reason = "no eval tasks measure this job yet, so nothing is suggested"
        elif not pts:
            reason = "not measured yet (select --screen)"
        elif best is None:
            reason = f"no measured model passes at least {int(PASS_FLOOR * 100)}% of its tasks here"
        elif best["model"] == cm_:
            reason = f"the current model is the cheapest measured: {best['C']} s per passed task"
        elif c is None:
            reason = f"{best['model']} looks best ({best['C']} s per passed task) but the current model was not measured on this job, so there is nothing to compare"
        else:
            b_hi = best["C_ci"][1]
            c_lo = c["C_ci"][0]
            if b_hi is not None and c_lo is not None and b_hi < c_lo:
                sug = best["model"]
                reason = (f"{best['model']} costs {best['C']} s per passed task against {c['C'] if c['C'] is not None else 'no pass'} for the current one, "
                          "and the ranges do not overlap")
            else:
                reason = (f"{best['model']} measured {best['C']} s per passed task vs the current {c['C']}, but the ranges overlap "
                          f"({best['n']} and {c['n']} tasks): measure more before switching")
        if best and c and c.get("tok_s") and best.get("tok_s") and best["tok_s"] < c["tok_s"] and v is not None and v <= arch_jobs.LOW_VOLUME:
            reason += f"; it is slower per token, but this job makes only ~{v} call(s) a run, so a slow, smarter model costs little here"
        show = best or c
        out[job] = {"current": cm_, "best": best["model"] if best else None, "C": show["C"] if show else None,
                    "pass_rate": show["p"] if show else None, "ci": show["ci"] if show else None, "C_ci": show["C_ci"] if show else None,
                    "tok_s": show["tok_s"] if show else None, "fits": _fits(show["model"], fb, hw) if show else None,
                    "n": show["n"] if show else 0, "volume": v, "volume_source": (vol.get(job) or {}).get("source"),
                    "reason": reason, "suggestion": sug, "p_cheapest": pc,
                    "prior": _prior(show["model"] if show else cm_, job, fb)[0], "prior_reasons": _prior(show["model"] if show else cm_, job, fb)[1],
                    "models": sorted(pts.values(), key=lambda s: s["C"] if s["C"] is not None else 1e18)}
    return out


def why_table(w):
    lines = [f"{'job':<9} {'current':<28} {'best':<28} {'C (s)':>7} {'pass':>5} {'95% CI':>13} {'n':>3}  suggestion"]
    for job, d in w.items():
        ci = f"{d['ci'][0]:.2f}-{d['ci'][1]:.2f}" if d.get("ci") else "-"
        lines.append(f"{job:<9} {str(d['current'])[:28]:<28} {str(d['best'] or '-')[:28]:<28} {str(d['C'] if d['C'] is not None else '-'):>7} "
                     f"{str(d['pass_rate'] if d['pass_rate'] is not None else '-'):>5} {ci:>13} {d['n']:>3}  {d['suggestion'] or '-'}")
        lines.append(f"          {d['reason']}")
    lines.append(f"C = seconds per task that PASSED (t / p). A switch is suggested only on a clear win: non-overlapping 95% ranges and >= "
                 f"{int(PASS_FLOOR * 100)}% passed. Nothing changes until `select --apply JOB MODEL`.")
    return "\n".join(lines)


# ------------------------------------------------------------------ plan (nothing runs)
def plan(cfg=None, models=None, jobs=None, n=SCREEN_TASKS, facts=None, rows=None, tasks=None):
    """-> {"models", "jobs": {job: tasks}, "runs", "est_minutes", "basis", "text"}. Reads only."""
    jobs = [j for j in (jobs or list(arch_jobs.JOBS)) if j in arch_jobs.JOBS]
    facts = facts if facts is not None else _facts()
    cands = candidates(cfg, facts)
    names = models or [m["name"] for m in cands]
    st = rows if rows is not None else load_results()
    per_job = {}
    for j in jobs:
        secs = sorted(float(r["secs"]) for r in st if r.get("job") == j and r.get("kind") == "task" and r.get("secs"))
        per_job[j] = {"tasks": len(job_tasks(j, n, 0, tasks)), "task_s": secs[len(secs) // 2] if secs else PRIOR_TASK_S,
                      "basis": "measured median" if secs else "estimate"}
    total = sum(PROBE_S + sum(d["tasks"] * d["task_s"] for d in per_job.values()) for _ in names)
    runs = len(names) * sum(d["tasks"] for d in per_job.values())
    lines = [f"select --screen would check {len(names)} model(s) (edit format first; a model that fails it is dropped) and run {runs} task(s):"]
    for m in names:
        f = next((x for x in facts if x["name"] == m), {})
        lines.append(f"  {m}  ({f.get('size_gb', '?')} GB, {f.get('quant') or '?'}{', MoE' if f.get('moe') else ''})")
    for j, d in per_job.items():
        lines.append(f"  job {j:<9} {d['tasks']} task(s) x ~{int(d['task_s'])} s ({d['basis']})" + ("  -- no eval tasks, skipped" if not d["tasks"] else ""))
    lines.append(f"Estimated GPU time: ~{round(total / 60)} min (estimate; models that fail the edit check cost ~{int(PROBE_S)} s only). "
                 "Real runs wait for the heavy-job lock, so they queue behind any benchmark already running. Nothing is applied.")
    return {"models": names, "jobs": per_job, "runs": runs, "est_minutes": round(total / 60, 1), "text": "\n".join(lines)}
