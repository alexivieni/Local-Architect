"""What actually happened with each of your models, from your own runs.

Every model call in a run is later marked accepted or rejected by the code checks (tests, syntax, fake-code gate, verifier). That is better evidence
than a model card, so the outcome is kept per model and role in `ledger.json` (next to the other measured data), and shown beside each model.
It informs; it does not silently reassign anything. Each run is counted once. Numbers are attempts, not quality scores: a model given the hard
sprints will look worse than one given easy ones, so read them as "how often did its answer pass the checks here".
"""
import json
import os
import re

import arch_core as core

KEEP_RUNS = 500
KEEP_EXAMPLES = 400          # accepted (request, reply) pairs kept per install, newest first
EXAMPLE_CHARS = 1400         # how much of a past reply is shown as a format reference


def path():
    return os.path.join(core.data_dir(), "ledger.json")


def load():
    return core.load_json(path()) or {}


def _save(d):
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path())


def read_trace(run_dir):
    calls, marks = {}, {}
    try:
        with open(os.path.join(run_dir, "trace.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "call":
                    calls[r["id"]] = r
                elif r.get("type") == "mark":
                    marks[r["id"]] = None if r.get("accepted") is None else bool(r.get("accepted"))   # None = the checks could not run
    except OSError:
        return {}, {}
    return calls, marks


def record_run(run_dir, run_id):
    """Add this run's marked calls to the ledger, once. -> number of calls counted.

    A reply remembered here is later shown to a real model as "a past reply of yours that passed the checks", so what goes in matters.
    The guarantee is enforced where it belongs -- the self-tests each run against their own throwaway data folder, so a fixture written to
    make a test pass never lands in the install's history. Before that was fixed, 73 of the 73 stored examples on this machine had been
    written by the tests' scripted stand-in model, and were being fed to real models as examples of good work.
    """
    if os.environ.get("ARCHITECT_EVAL") == "1":
        return 0                      # a benchmark run is a measurement, not experience: nothing it does is learned
    calls, marks = read_trace(run_dir)
    led = load()
    seen = led.setdefault("_runs", [])
    if run_id in seen:
        return 0
    n = 0
    for cid, ok in marks.items():
        c = calls.get(cid)
        if not c or not c.get("model") or ok is None:
            continue                       # the checks could not run: this call says nothing about how well the model did

        slot = led.setdefault(c["model"], {}).setdefault(c.get("role") or "?", {"n": 0, "ok": 0, "secs": 0.0, "tokens": 0})
        slot["n"] += 1
        slot["ok"] += 1 if ok else 0
        slot["secs"] = round(slot["secs"] + float(c.get("secs") or 0), 1)
        slot["tokens"] += int(c.get("gen_tokens") or 0)
        slot.setdefault("lat", []).append(round(float(c.get("secs") or 0), 1))
        slot["lat"] = slot["lat"][-200:]
        n += 1
        if ok and c.get("reply") and c.get("messages"):
            _remember(led, c)
    seen.append(run_id)
    led["_runs"] = seen[-KEEP_RUNS:]
    if n:
        _save(led)
    return n


def _remember(led, c):
    """Keep an accepted reply as a future example for the same role (your own successes are the cheapest few-shot data there is)."""
    user = next((m.get("content") for m in reversed(c["messages"]) if m.get("role") == "user"), "") or ""
    ex = led.setdefault("_examples", [])
    ex.insert(0, {"role": c.get("role"), "model": c.get("model"), "flow": c.get("flow"), "req": user[:600], "reply": c["reply"][:EXAMPLE_CHARS], "t": c.get("t")})
    del ex[KEEP_EXAMPLES:]


_BLOCK = re.compile(r"^\s*(FILE|EDIT|DEF):\s*([^\s:]+)", re.M)


def _safe_example(e, root, named):
    """Would this past reply teach the right SHAPE here without handing over the wrong CONTENT?

    Two ways it goes wrong on a small model, both seen in the live benchmark. An example made of EDIT: blocks, attached to a project that
    does not have those files yet, teaches edits to files that do not exist -- the try is rejected outright. And an example that writes a
    file of the same name as one here (inventory/, tests/test_cart.py) is copied as content: the model imports names from another project.
    """
    if root is None:
        return True
    for kind, path in _BLOCK.findall(e.get("reply") or ""):
        path = path.strip("`'\"").replace("\\", "/")
        if path in named or os.path.exists(os.path.join(root, path)):
            return False
        if kind in ("EDIT", "DEF"):                 # both change files that exist in THAT project
            return False
    return True


def example_for(role, user, led=None, root=None, named=()):
    """The most similar accepted past reply for this role, or None. Plain term overlap; no model call.
    With `root`, an example is skipped when it would mislead rather than help (see _safe_example)."""
    led = led if led is not None else load()
    named = set(named or ())
    pool = [e for e in led.get("_examples") or [] if e.get("role") == role and e.get("reply") and _safe_example(e, root, named)]
    if not pool:
        return None
    q = set(core._terms(user[:2000]))
    best, score = None, 0.0
    for e in pool:
        t = set(core._terms(e["req"]))
        if not t:
            continue
        s = len(q & t) / (len(q | t) or 1)
        if s > score:
            best, score = e, s
    return best if score >= 0.12 else None


def percentiles(vals):
    if not vals:
        return None, None
    v = sorted(vals)
    return v[len(v) // 2], v[min(len(v) - 1, int(len(v) * 0.95))]


def roles_overview(led=None):
    """-> [{role, model, n, ok, rate, p50, p95}] across every model, for the Health page."""
    led = led if led is not None else load()
    out = []
    for model, roles in led.items():
        if model.startswith("_") or not isinstance(roles, dict):
            continue
        for r, v in roles.items():
            if not v.get("n"):
                continue
            p50, p95 = percentiles(v.get("lat") or [])
            out.append({"role": r, "model": model, "n": v["n"], "ok": v["ok"], "rate": round(100 * v["ok"] / v["n"]), "p50": p50, "p95": p95,
                        "cost_per_success": (round(v["secs"] / v["ok"], 1) if v["ok"] else None)})
    return sorted(out, key=lambda x: (-x["n"], x["role"]))


def summary(model, led=None):
    """-> {"n", "ok", "rate", "by_role": {role: {"n","ok","rate"}}} or None when this model has no marked calls yet."""
    led = led if led is not None else load()
    roles = led.get(model)
    if not isinstance(roles, dict) or model.startswith("_"):
        return None
    by = {r: {"n": v["n"], "ok": v["ok"], "rate": round(100 * v["ok"] / v["n"]) if v["n"] else None} for r, v in roles.items() if v.get("n")}
    n, ok = sum(v["n"] for v in by.values()), sum(v["ok"] for v in by.values())
    return {"n": n, "ok": ok, "rate": round(100 * ok / n) if n else None, "by_role": by} if n else None
