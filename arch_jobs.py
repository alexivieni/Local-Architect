"""Jobs: the roles grouped by the kind of work they do, so a model can be chosen PER JOB by measurement (arch_select).

A job names the roles it covers, the eval task families that measure it (evals/families/*.py, arch_eval.load_tasks) and how many
model calls it makes in a typical run (`volume`). Volume is what makes "slow but smart" sensible for some jobs: a planner called
twice a run can afford a big model, a generator called eight times cannot.

`assign(cfg, job, model)` writes cfg.roles for that job's roles through the settings writer and pushes an undo record
(<data>/select/undo.json, a stack, newest last); `undo()` restores the last one. Nothing here changes a setting on its own.
"""
import collections
import json
import os
import time

import arch_core as core

# job -> roles it covers, eval families that measure it (only families that exist in evals/families), prior calls per typical run.
# Priors (used only when no traces exist): read off the flows -- route is one call; a plan is planner + architect; a build sprint
# calls the generator ~8 times (sprints x revisions); tests ~3; a fix ~4 (hunt, patch, retry); review ~3 judges; the rest ~1-2.
JOBS = {
    "route":    {"roles": ["router"], "families": ["route"], "prior_calls": 1},
    "plan":     {"roles": ["planner", "architect"], "families": ["planjson", "design"], "prior_calls": 2},
    "code":     {"roles": ["generator", "generator_ui", "generator_sql", "fullstack", "accessibility_engineer", "migration_engineer"],
                 "families": ["build", "feature", "e2e"], "prior_calls": 8},
    "test":     {"roles": ["tester"], "families": ["test"], "prior_calls": 3},
    "fix":      {"roles": ["bug_hunter", "optimizer", "reliability_engineer", "type_engineer"], "families": ["bugfix", "hunt", "refactor"], "prior_calls": 4},
    "review":   {"roles": ["reviewer", "evaluator", "architect_dev"], "families": ["review"], "prior_calls": 3},
    "security": {"roles": ["security_engineer", "red_team", "blue_team", "pentester", "ioc_analyst"], "families": ["secure", "refusal"], "prior_calls": 2},
    "infra":    {"roles": ["network_engineer", "ml_engineer", "config_engineer", "ci_engineer", "protocol_engineer", "iot_dev", "dependency_engineer"],
                 "families": ["cicd", "config", "network", "distributed"], "prior_calls": 2},
    "docs":     {"roles": ["docs_writer"], "families": [], "prior_calls": 1},       # no docs eval family exists yet: never measured, never suggested
}
LOW_VOLUME = 2.5          # calls per run at or below which a slower, better model is usually worth it (said in why(), not enforced)


def job_of(role):
    for j, d in JOBS.items():
        if role in d["roles"]:
            return j
    return None


def tasks_for(job, tasks):
    fams = set(JOBS[job]["families"])
    return [t for t in tasks if t.get("family") in fams]


def _default_model(cfg):
    return (cfg.get("tiers") or {}).get("worker") or cfg.get("default_model") or ""


def current(cfg):
    """-> {job: {"model": most common model across the job's roles, "roles": {role: model}, "mixed": bool}}"""
    roles = cfg.get("roles") or {}
    out = {}
    for j, d in JOBS.items():
        per = {r: roles.get(r) or ((cfg.get("tiers") or {}).get("router") if r == "router" else None) or _default_model(cfg) for r in d["roles"]}
        cnt = collections.Counter(per.values())
        out[j] = {"model": cnt.most_common(1)[0][0] if cnt else "", "roles": per, "mixed": len(cnt) > 1}
    return out


def volume(roots=None):
    """-> {job: {"calls": calls per typical run, "source": "traces" | "prior", "runs": n}} from run traces when any exist."""
    per_job = collections.defaultdict(list)
    runs = 0
    try:
        import arch_costmodel as cm
        for _run, calls, _marks in cm._records(roots if roots is not None else cm.default_roots()):
            if not calls:
                continue
            runs += 1
            c = collections.Counter(job_of(x.get("role")) for x in calls.values())
            for j, n in c.items():
                if j:
                    per_job[j].append(n)
    except Exception:
        pass
    out = {}
    for j, d in JOBS.items():
        xs = per_job.get(j)
        if xs and len(xs) >= 3:
            out[j] = {"calls": round(sum(xs) / max(runs, 1), 1), "source": "traces", "runs": runs}
        else:
            out[j] = {"calls": d["prior_calls"], "source": "prior", "runs": runs}
    return out


def _undo_path():
    return os.path.join(core.data_dir(), "select", "undo.json")


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


def assign(cfg, job, model):
    """Write `model` into cfg.roles for every role of `job` (the settings file, via the hub's writer) and record what was there.
    `cfg` is accepted for symmetry and ignored for writing: the file on disk is read fresh so nothing else in it is lost.
    -> {"job", "model", "before": {role: old or None}}"""
    if job not in JOBS:
        raise ValueError(f"unknown job {job!r}; jobs: {', '.join(JOBS)}")
    if not model:
        raise ValueError("no model given")
    mh = _mh()
    raw = mh.read_config()
    roles = raw.setdefault("roles", {})
    before = {r: roles.get(r) for r in JOBS[job]["roles"]}
    for r in JOBS[job]["roles"]:
        roles[r] = model
    mh.write_config(raw)
    rec = {"job": job, "model": model, "before": before, "at": time.time()}
    stack = _load_undo()
    stack.append(rec)
    os.makedirs(os.path.dirname(_undo_path()), exist_ok=True)
    core._atomic_write(_undo_path(), json.dumps(stack[-50:], indent=1))
    return rec


def undo():
    """Put back the roles the last assign() changed. -> the record undone, or None when there is nothing to undo."""
    stack = _load_undo()
    if not stack:
        return None
    rec = stack.pop()
    mh = _mh()
    raw = mh.read_config()
    roles = raw.setdefault("roles", {})
    for r, old in rec["before"].items():
        if old:
            roles[r] = old
        else:
            roles.pop(r, None)
    mh.write_config(raw)
    core._atomic_write(_undo_path(), json.dumps(stack, indent=1))
    return rec
