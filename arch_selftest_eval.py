"""Self-test for the eval harness: tasks load and validate, every judge passes a right answer and fails the task's own wrong answer,
the oracle fake drives real flows end to end, results are saved, and `compare` flags a regression. No model, no GPU."""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_eval as E
    import arch_roles
    home = tempfile.mkdtemp(prefix="archeval_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    fake_roles = {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "default_model": "fake:7b"}
    try:
        tasks = E.load_tasks()
        flows = {t["flow"] for t in tasks}
        check("the task set loads: 100+ tasks across 12+ flows, every one valid", len(tasks) >= 100 and len(flows) >= 12 and not [p for t in tasks for p in E.validate(t)],
              (len(tasks), sorted(flows), [p for t in tasks for p in E.validate(t)][:3]))
        quick = E.load_tasks(quick=True)
        check("--quick keeps at most 10 per flow, in a stable order", all(sum(1 for t in quick if t["flow"] == f) <= E.QUICK_N for f in flows) and [t["id"] for t in quick] == [t["id"] for t in E.load_tasks(quick=True)])
        check("a task without a judge or with an unknown flow is refused, not silently counted",
              E.validate({"id": "x", "flow": "nope", "goal": "g", "judge": []}) and E.validate({"id": "y", "flow": "build", "goal": "g", "judge": [{"type": "no_such_judge"}]}))
        check("every family seeds deterministically (same seed, same ids; another seed, a different order)",
              [t["id"] for t in E.load_tasks(seed=1)] == [t["id"] for t in E.load_tasks(seed=1)])

        # ---- judges: the reference solution passes, the wrong answer fails -- for one task of every family that has both
        seen, wrong_ok, right_bad = set(), [], []
        for t in tasks:
            if t["family"] in seen or not t.get("solution") or t.get("wrong") is None or t["kind"] != "flow":
                continue
            seen.add(t["family"])
            for which, files in (("solution", t["solution"].get("files")), ("wrong", (t["wrong"] or {}).get("files"))):
                proj = tempfile.mkdtemp(prefix="j_", dir=home)
                E._write(proj, t["fixture"].get("files"))
                ctx = {"before": {}, "lines_before": {}, "route": None, "json": None}
                for j in t["judge"]:
                    for rel in (j.get("paths") or []) if j.get("type") == "files_unchanged" else []:
                        ctx["before"][rel] = E._sha(E._read(proj, rel))
                    if j.get("type") == "no_shrink":
                        ctx["lines_before"][j["path"]] = len((E._read(proj, j["path"]) or "").splitlines())
                E._write(proj, files)
                verdicts = [E.JUDGES[j["type"]](proj, j, t, ctx) for j in t["judge"] if (j["type"] not in ("file_exists", "docs_structure") or which == "solution")
                            and j["type"] not in getattr(E, "TRACE_JUDGES", ())]   # a trace judge needs a run; arch_selftest_refusal proves it
                ok = all(v[0] for v in verdicts)
                if which == "solution" and not ok and t["family"] not in ("review", "design", "network", "hunt", "test"):     # those need the flow's own output files
                    right_bad.append((t["id"], [v for v in verdicts if not v[0]]))
                if which == "wrong" and ok and files:
                    wrong_ok.append(t["id"])
        check("every family's reference solution satisfies its judges", not right_bad, right_bad[:3])
        check("every family's plausible wrong answer is caught by at least one judge", not wrong_ok, wrong_ok)

        # ---- the oracle fake drives the real flows
        lines = []
        rep = E.run(flows=["bugfix", "build", "feature", "route", "planjson", "cicd"], fake=True, n=1, label="t", say=lines.append)
        bad = [(r["id"], r["verdict"], r["error"], [j["reason"] for j in r["judges"] if not j["ok"]]) for r in rep["results"] if r["verdict"] != "pass"]
        check("oracle run: bugfix, build (incl. a whole-project e2e build), feature, route, planjson and cicd all pass end to end through the real flows", rep["n"] == len(rep["results"]) >= 7 and not bad, bad)
        check("the report counts tokens and calls from the trace and saves itself", all(r["calls"] >= 1 and r["tokens_in"] + r["tokens_out"] > 0 for r in rep["results"] if r["kind"] == "flow")
              and os.path.isfile(rep.get("path", "")) and json.load(open(rep["path"]))["n"] == rep["n"], rep.get("path"))
        check("the summary line lands in evals.jsonl and latest() finds the run by label", (E.latest(label="t") or {}).get("n") == rep["n"])

        # ---- live runs keep each task's trace (its folder is thrown away); the oracle keeps none; training data only from the train bucket
        check("an oracle (fake) run keeps no task traces", not any(r.get("kept_run") for r in rep["results"])
              and not os.path.isdir(E.kept_runs_dir("train")) and not os.path.isdir(E.kept_runs_dir("bench")))
        bf = [t for t in tasks if t["flow"] == "bugfix"][0]
        check("buckets: a training task -> train; a held-out training task and a benchmark task -> bench",
              E.bucket_for({"tags": ["distributed", "training"]}) == "train" and E.bucket_for({"tags": ["training", "holdout"]}) == "bench"
              and E.bucket_for(bf) == "bench")
        srv, prev = E.start_oracle(tasks)
        try:
            rb = E.run_task(bf, fake_roles, keep_run=True, label="k")
            rt = E.run_task(dict(bf, id=bf["id"] + "-train", tags=list(bf.get("tags") or []) + ["training"]), fake_roles, keep_run=True, label="k")
        finally:
            E.stop_oracle(srv, prev)
        kb, kt = rb.get("kept_run") or "", rt.get("kept_run") or ""
        meta = json.load(open(os.path.join(kt, "eval_task.json"), encoding="utf-8")) if os.path.isfile(os.path.join(kt, "eval_task.json")) else {}
        check("a kept run has its trace and a note of the task, verdict and bucket, under the right bucket",
              os.path.isfile(os.path.join(kb, "trace.jsonl")) and kb.startswith(E.kept_runs_dir("bench"))
              and os.path.isfile(os.path.join(kt, "trace.jsonl")) and kt.startswith(E.kept_runs_dir("train"))
              and meta.get("bucket") == "train" and meta.get("verdict") == rt["verdict"] and meta.get("label") == "k", (kb, kt, meta))
        import arch_dataset as DS
        rs = DS.roots(home)
        check("dataset reads the train bucket and never the bench bucket (the benchmark cannot learn its own answers)",
              os.path.abspath(E.kept_runs_dir("train")) in rs and os.path.abspath(E.kept_runs_dir("bench")) not in rs
              and len(DS.runs_of(E.kept_runs_dir("train"))) == 1, rs)
        import arch_costmodel as CM
        rows = CM.measure([E.kept_runs_dir("bench"), E.kept_runs_dir("train")], min_calls=1)
        check("the cost model reads kept runs: per role and model, C = t / p-bar from the checks' marks",
              any(r["calls"] >= 1 and r["p_bar"] is not None for r in rows) and set(CM.default_roots()) >= {E.kept_runs_dir("train"), E.kept_runs_dir("bench")}, rows)
        old_keep = E.KEEP_RUNS
        try:
            E.KEEP_RUNS = 1
            for i in range(2):
                E.keep_trace(os.path.dirname(os.path.join(kb, "trace.jsonl")), dict(bf, id=f"{bf['id']}-p{i}"), {"verdict": "pass"})
        finally:
            E.KEEP_RUNS = old_keep
        check("old kept runs are pruned (a nightly run cannot fill the disk)", len(DS.runs_of(E.kept_runs_dir("bench"))) == 1,
              DS.runs_of(E.kept_runs_dir("bench")))
        wrong_srv, prev = E.start_oracle(tasks, wrong=True)
        try:
            one = [t for t in tasks if t["flow"] == "bugfix"][:1]
            r = E.run_task(one[0], fake_roles)
        finally:
            E.stop_oracle(wrong_srv, prev)
        check("a model that edits the tests instead of fixing the code is caught by the judges", r["verdict"] in ("fail", "not_sure") and r["verdict"] != "pass", r)
        r = E.run_task({"id": "nojudge", "flow": "review", "goal": "review app.py", "fixture": {"files": {"app.py": "x = 1\n"}},
                        "judge": [{"type": "scanner_zero", "kind": "nope"}]}, fake_roles)
        check("a judge that cannot run makes the verdict 'not sure', never a pass", r["verdict"] == "not_sure", r["verdict"])
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        r = E.run_task([t for t in tasks if t["flow"] == "build"][0], fake_roles)
        check("with no model answering the task fails with the reason, nothing crashes", r["verdict"] != "pass" and "not reachable" in (r["error"] or ""), r["error"])

        # ---- compare
        base = {"summary": {"flows": {"bugfix": {"pass_rate": 90.0, "tokens_p50": 1000, "secs_p95": 10.0}, "build": {"pass_rate": 80.0, "tokens_p50": 1000, "secs_p95": 10.0}}}}
        cand = {"summary": {"flows": {"bugfix": {"pass_rate": 80.0, "tokens_p50": 1000, "secs_p95": 10.0}, "build": {"pass_rate": 82.0, "tokens_p50": 1400, "secs_p95": 30.0}}}}
        c = E.compare(base, cand)
        check("compare flags a 10-point pass-rate drop, a 40% token rise and a 3x latency rise, and names the flow", not c["ok"] and len(c["regressions"]) == 3 and all("bugfix" in c["regressions"][0] for _ in [0]), c)
        check("compare passes small changes and ignores flows only one side measured",
              E.compare(base, {"summary": {"flows": {"bugfix": {"pass_rate": 88.0, "tokens_p50": 1100, "secs_p95": 11.0}, "new": {"pass_rate": 0, "tokens_p50": 1, "secs_p95": 1}}}})["ok"])
        nl = E.compare(base, cand, latency=False)
        check("with latency off (the oracle gate: its seconds time the machine, not the change) pass rate and tokens still gate, seconds do not",
              len(nl["regressions"]) == 2 and not any("seconds" in x for x in nl["regressions"]), nl["regressions"])
        check("compare_table renders without a model or a file", "REGRESSION" in E.compare_table(c) and "No regression" in E.compare_table(E.compare(base, base)))
        rset = E.representative()
        kinds = {}
        for t in rset:
            g = "e2e" if "e2e" in (t.get("tags") or []) else t["flow"]
            kinds[g] = kinds.get(g, 0) + 1
        check("the representative live set is the same 17 tasks every time: 4 whole projects, 3 builds, 3 fixes, 3 features, 2 test and 2 refactor tasks",
              len(rset) == 17 and kinds == E.REPRESENTATIVE and [t["id"] for t in rset] == [t["id"] for t in E.representative()], kinds)
        ch = E.task_changes({"results": [{"id": "a", "verdict": "fail"}, {"id": "b", "verdict": "pass"}, {"id": "c", "verdict": "pass"}]},
                            {"results": [{"id": "a", "verdict": "pass"}, {"id": "b", "verdict": "not_sure"}, {"id": "c", "verdict": "pass"}, {"id": "d", "verdict": "pass"}]})
        check("two runs compare task by task: what passes now, what no longer passes ('not sure' is not a pass), the rest unchanged",
              ch == {"won": ["a"], "lost": ["b"], "same": 1}, ch)
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' EVAL CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' EVAL CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
