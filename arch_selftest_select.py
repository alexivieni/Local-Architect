"""Self-test for arch_select + arch_jobs: per-job model choice by C = t / p, with no GPU and an isolated home.

    python arch_selftest_select.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    home = tempfile.mkdtemp(prefix="selftest_select_")
    prev = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST")}
    os.environ.update(ARCHITECT_DATA_DIR=os.path.join(home, "data"), ARCHITECT_NO_CACHE="1", ARCHITECT_SELFTEST="1")
    import arch_core as core
    old_home = core.HOME
    core.set_home(home)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    try:
        import arch_jobs
        import arch_select as sel
        import model_hub as mh
        facts = [
            {"name": "big:14b", "size_gb": 9.0, "eff_b": 14, "quant": "Q4_K_M", "caps": ["completion"]},
            {"name": "nomic-embed-text", "size_gb": 0.3, "eff_b": 0.1, "embed": True},
            {"name": "small:3b", "size_gb": 2.0, "eff_b": 3, "quant": "Q4_K_M", "caps": ["completion"]},
            {"name": "qwen2.5vl:7b", "size_gb": 5.0, "eff_b": 7, "caps": ["completion", "vision"]},
            {"name": "mid-noedit:7b", "size_gb": 4.5, "eff_b": 7, "quant": "Q4_K_M"},
        ]
        c = [m["name"] for m in sel.candidates({}, facts)]
        check("candidates: embedding and vision models are left out, the rest small to large", c == ["small:3b", "mid-noedit:7b", "big:14b"], c)
        check("candidates: an allowed list narrows", [m["name"] for m in sel.candidates({}, facts, allowed=["big:14b"])] == ["big:14b"])

        # every role is in exactly one job
        import arch_roles
        roles = [r for d in arch_jobs.JOBS.values() for r in d["roles"]]
        check("jobs cover every role exactly once", sorted(roles) == sorted(arch_roles.ROLES), set(arch_roles.ROLES) ^ set(roles))

        tasks = [{"id": f"{fam}-{i}", "family": fam} for fam in ("bugfix", "build", "review") for i in range(5)]
        rows = sel.screen(["small:3b", "mid-noedit:7b", "big:14b"], ["fix", "code"], fake=True, facts=facts, tasks=tasks, say=lambda m: None)
        probes = {r["model"]: r["verdict"] for r in rows if r["kind"] == "probe"}
        task_models = {r["model"] for r in rows if r["kind"] == "task"}
        check("screen: a model that fails the edit format is dropped before any task", probes.get("mid-noedit:7b") == "fail"
              and "mid-noedit:7b" not in task_models and task_models == {"small:3b", "big:14b"}, (probes, task_models))
        check("screen: 3 tasks per job per model, written to results.jsonl",
              sum(1 for r in rows if r["kind"] == "task") == 2 * 2 * 3 and len(sel.load_results(include_fake=True)) == len(rows))
        check("screen: fake rows are not read by default", sel.load_results() == [])

        # why(): math
        lo, hi = sel.wilson(8, 10)
        check("Wilson 8/10 = 0.49-0.94", abs(lo - 0.490) < 0.005 and abs(hi - 0.943) < 0.005, (lo, hi))
        check("Wilson n=0 is the whole range", sel.wilson(0, 0) == (0.0, 1.0))

        def mk(model, job, n, passed, secs):
            return [{"kind": "task", "job": job, "model": model, "verdict": "pass" if i < passed else "fail", "secs": secs, "tok_s": 10.0}
                    for i in range(n)]
        cfg = {"roles": {"bug_hunter": "a", "optimizer": "a", "reliability_engineer": "a", "type_engineer": "a"}, "tiers": {"worker": "a"}}
        vol = {j: {"calls": 4, "source": "prior"} for j in arch_jobs.JOBS}
        # overlap: 3 tasks each, b slightly better
        w = sel.why(cfg, rows=mk("a", "fix", 3, 2, 30) + mk("b", "fix", 3, 3, 30), vol=vol)
        f = w["fix"]
        check("why: C = total seconds / passed tasks", f["best"] == "b" and f["C"] == 30.0, f)
        check("why: no suggestion when the ranges overlap", f["suggestion"] is None and "overlap" in f["reason"], f["reason"])
        # clear win: b 40/40 at 20 s vs a 8/40 at 60 s
        w = sel.why(cfg, rows=mk("a", "fix", 40, 8, 60) + mk("b", "fix", 40, 40, 20), vol=vol)
        check("why: a clear win (non-overlapping ranges, pass >= floor) is suggested", w["fix"]["suggestion"] == "b" and w["fix"]["C"] == 20.0, w["fix"])
        # below the floor never suggested
        w = sel.why(cfg, rows=mk("a", "fix", 40, 2, 600) + mk("c", "fix", 40, 16, 1), vol=vol)
        check("why: a model below the pass floor is never suggested", w["fix"]["suggestion"] is None and w["fix"]["best"] is None, w["fix"])
        # an allowed filter: b wins but is not allowed
        w = sel.why(cfg, rows=mk("a", "fix", 40, 8, 60) + mk("b", "fix", 40, 40, 20), vol=vol, allowed=["z"])
        check("why: a model outside the allowed list is not suggested (the current one is still shown)",
              w["fix"]["suggestion"] is None and [m["model"] for m in w["fix"]["models"]] == ["a"], w["fix"])
        check("why: docs has no tasks and says so", w["docs"]["suggestion"] is None and "no eval tasks" in w["docs"]["reason"])

        # apply / undo / no apply without a call
        core._atomic_write(core.CONFIG_FILE, json.dumps({"roles": {"bug_hunter": "a"}, "keep": 1}))
        before = open(core.CONFIG_FILE, encoding="utf-8").read()
        sel.why(core.load_config(), rows=mk("a", "fix", 40, 8, 60) + mk("b", "fix", 40, 40, 20), vol=vol)
        check("no setting changes without an apply call", open(core.CONFIG_FILE, encoding="utf-8").read() == before)
        arch_jobs.assign({}, "fix", "b")
        mh.CONFIG_FILE = core.CONFIG_FILE
        r = mh.read_config()
        check("apply writes every role of the job and keeps the rest", all(r["roles"][x] == "b" for x in arch_jobs.JOBS["fix"]["roles"]) and r.get("keep") == 1, r)
        arch_jobs.undo()
        r = mh.read_config()
        check("undo restores exactly what was there", r.get("roles") == {"bug_hunter": "a"} and r.get("keep") == 1, r)
        check("undo with nothing left says so", arch_jobs.undo() is None)

        # fake end to end: screen -> measure -> why -> apply the suggestion -> undo
        open(sel.results_path(), "w").close()
        cfg2 = {"roles": {r: "small:3b" for r in arch_jobs.JOBS["fix"]["roles"]}}
        sel.screen(["small:3b", "big:14b"], ["fix"], fake=True, facts=facts, tasks=tasks, say=lambda m: None)
        sel.measure(2, 5, [1, 2, 3], cfg=cfg2, jobs=["fix"], fake=True, facts=facts, tasks=tasks, say=lambda m: None)
        w = sel.why(cfg2, include_fake=True, facts=facts, vol=vol)
        check("fake end to end: why() reports measured points for the job", w["fix"]["n"] > 10 and len(w["fix"]["models"]) == 2, w["fix"])
        p = sel.plan({}, None, ["fix", "docs"], facts=facts, tasks=tasks, rows=[])
        check("plan: counts tasks and models, runs nothing", p["runs"] == 3 * 3 and p["jobs"]["docs"]["tasks"] == 0 and "GPU" in p["text"], p)
    finally:
        core.set_home(old_home)
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    ok = all(results)
    print(f"{'ALL' if ok else 'NOT ALL'} {len(results)} SELECT CHECKS {'PASSED' if ok else 'PASSED: ' + str(results.count(False)) + ' failed'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
