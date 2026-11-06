"""A curriculum from the worker's own failures: which kinds of task to generate more of, and the tasks themselves.

Reads the saved LIVE eval reports (no model, no network), classifies each failed task with arch_triage, and turns the counts into
weights over SHAPES (package wiring, contract-first functions, edge-case behaviour, tests present). `evals/families/curriculum.py`
holds the code-generated templates for each shape (every task carries a reference solution proven against its hidden judge); this
module chooses how many of each, with a fresh seed, and proves the guarantees that keep the benchmark honest:

  * every task is tagged "training", with its lesson (`lesson:<id>`, one of LESSONS), and only non-holdout tasks are returned;
  * a held-out template is held out WHOLE (all its variants) and never trained on;
  * no curriculum task copies a bench task: no shared id, goal, template key, or public identifier named in the goal;
  * no two tasks share a goal text (dedupe), within a night or across the seeds a data folder has already used.

Nothing here trains, downloads, or calls Claude.
"""
import glob
import hashlib
import importlib.util
import json
import math
import os
import re

import arch_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
FAMILY_FILE = os.path.join(HERE, "evals", "families", "curriculum.py")
# What the 153-task live bench said when this was written (arch_triage families); used when no saved report is readable.
DEFAULT_PROFILE = {"wiring": 26, "behaviour": 43, "tests": 8, "environment": 7, "timeout": 0, "refusal": 0, "total": 153}


def family_module():
    spec = importlib.util.spec_from_file_location("curriculum_family", FAMILY_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reports(limit=6):
    d = os.path.join(core.data_dir(), "evals")
    paths = sorted(glob.glob(os.path.join(d, "*.json")), key=os.path.getmtime, reverse=True)
    out = []
    for p in paths:
        r = core.load_json(p)
        if isinstance(r, dict) and r.get("results") and not r.get("fake"):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def profile(reports=None):
    """-> {"wiring": n, "behaviour": n, ..., "total": n, "gates": {judge type: failed}, "source": "live"|"default"}.
    The newest live report that holds each task id counts once."""
    import arch_triage
    reports = _reports() if reports is None else reports
    seen, fam, gates, total = set(), {}, {}, 0
    for rep in reports:
        for res in rep.get("results") or []:
            tid = res.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            total += 1
            f = arch_triage.classify(res)
            if f:
                fam[f] = fam.get(f, 0) + 1
                for j in res.get("judges") or []:
                    if not j.get("ok"):
                        gates[j.get("type") or "?"] = gates.get(j.get("type") or "?", 0) + 1
    if not total or not fam:
        return dict(DEFAULT_PROFILE, gates={}, source="default")
    return dict({k: 0 for k in arch_triage.FAMILIES}, **fam, total=total, gates=gates, source="live")


def weights(prof=None):
    """-> {shape: weight} over the family module's SHAPES. wiring failures drill wiring; contract-gate failures are the most common
    way to fail, so contract-first gets the wiring count too; behaviour and tests failures drill their own shapes. environment,
    timeout and refusal failures are not the model's code, so they add nothing."""
    p = prof or DEFAULT_PROFILE
    w = {"wiring": float(p.get("wiring", 0)), "contract": float(p.get("wiring", 0)) * 0.6 + float(p.get("behaviour", 0)) * 0.3,
         "behaviour": float(p.get("behaviour", 0)), "tests": float(p.get("tests", 0)) * 2}     # tests: a small family, drilled twice as hard
    s = sum(w.values()) or 1.0
    return {k: round(v / s, 4) for k, v in w.items()}


def plan(n, seed, prof=None):
    """-> [(template key, count)] summing to n, trainable templates only, in proportion to the failure weights. A template that drills
    two shapes (cli_tally: contract and tests) takes a share of each."""
    fam = family_module()
    w = weights(prof)
    allowed = {k for k in fam.templates() if k not in fam.HOLDOUT_TEMPLATES}
    quota = {}
    for shape, bases in fam.SHAPES.items():
        ks = sorted(k for k in allowed if (k[:-4] if k.endswith("-fix") else k) in bases)
        for k in ks:
            quota[k] = quota.get(k, 0.0) + w.get(shape, 0) * n / max(1, len(ks))
    tot = sum(quota.values()) or 1.0
    quota = {k: v * n / tot for k, v in quota.items()}
    cnt = {k: int(math.floor(q)) for k, q in quota.items()}
    left = n - sum(cnt.values())
    for k in sorted(quota, key=lambda k: (quota[k] - cnt[k], k), reverse=True)[:max(0, left)]:
        cnt[k] += 1
    return [(k, c) for k, c in sorted(cnt.items()) if c]


def tasks(n=40, seed=0, prof=None, used_goals=None):
    """n NEW trainable tasks (flow build, normalised), none held out, deduped, shaped by the failure profile. -> list."""
    import arch_eval
    fam = family_module()
    want = dict(plan(n, seed, prof))
    seen = {hashlib.sha1(g.encode()).hexdigest() for g in (used_goals or ())}
    out = []
    for key, c in want.items():
        got, k = 0, 0
        while got < c and k < c * 6 + 12:
            built = fam._variant(key, k, seed)
            task = fam._task(key, k, seed, built, key in fam.HOLDOUT_TEMPLATES)
            k += 1
            h = hashlib.sha1(task["goal"].encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            out.append(arch_eval.normalize(dict(task, flow=fam.FLOW)))
            got += 1
    return [t for t in out if "holdout" not in t["tags"] and "training" in t["tags"]][:n]


def lesson_of(task_or_tags):
    """The lesson id of a task dict or a tag list ("lesson:<id>"), or None."""
    tags = task_or_tags.get("tags") if isinstance(task_or_tags, dict) else task_or_tags
    for t in tags or ():
        if str(t).startswith("lesson:"):
            return str(t)[7:]
    return task_or_tags.get("lesson") if isinstance(task_or_tags, dict) else None


# ------------------------------------------------------------------ guarantees
_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_./]*)`")


def bench_overlap(curriculum_tasks=None, bench_tasks=None):
    """-> list of problems (empty = clean): a curriculum task that shares an id, a goal, a template key, or a backticked public name
    with a bench (non-training, or held-out) task."""
    import arch_eval
    cur = curriculum_tasks if curriculum_tasks is not None else [arch_eval.normalize(dict(t, flow="build")) for t in family_module().all_tasks(0)]
    if bench_tasks is None:
        bench_tasks = [t for t in arch_eval.load_tasks() if arch_eval.bucket_for(t) == "bench" and t.get("family") != "curriculum"]
    ids = {t["id"] for t in bench_tasks}
    goals = {re.sub(r"\s+", " ", (t.get("goal") or "")).strip().lower() for t in bench_tasks}
    keys = {str(t.get("template")) for t in bench_tasks if t.get("template")}
    names = set()
    for t in bench_tasks:
        names |= {m.group(1) for m in _NAME.finditer(t.get("goal") or "")}
        for rel in ((t.get("solution") or {}).get("files") or {}):
            names.add(rel)
    probs = []
    for t in cur:
        if t["id"] in ids:
            probs.append(f"{t['id']}: id used by a bench task")
        if re.sub(r"\s+", " ", t["goal"]).strip().lower() in goals:
            probs.append(f"{t['id']}: goal copied from a bench task")
        if t.get("template") in keys:
            probs.append(f"{t['id']}: template key {t.get('template')} also in bench")
        shared = {m.group(1) for m in _NAME.finditer(t["goal"])} & names
        shared |= set((t.get("solution") or {}).get("files", {})) & names
        if shared:
            probs.append(f"{t['id']}: names also in a bench task: {sorted(shared)[:3]}")
    return probs


def held_out_whole(all_curriculum):
    """-> problems: a template with BOTH held-out and trainable variants (holdout must take the whole template)."""
    by = {}
    for t in all_curriculum:
        by.setdefault(t["template"], set()).add("holdout" in t["tags"])
    return [f"{k}: some variants held out, some trained on" for k, v in sorted(by.items()) if len(v) > 1]


def summary(n=40, seed=0, prof=None):
    """-> a plain dict for the CLI / selftest: profile, weights, plan, count."""
    prof = prof or profile()
    return {"profile": prof, "weights": weights(prof), "plan": plan(n, seed, prof), "n": n}
