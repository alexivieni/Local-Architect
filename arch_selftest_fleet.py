"""Self-test for arch_fleet (the parallel-agent dispatcher) and the Claude Code run log on disk (arch_claudecode).
No SDK, no network, no subprocess: the starter and the command runner are scripted stand-ins.
Run: python arch_selftest_fleet.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, cond, detail=None):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f"  -> {str(detail)[:400]}"))

    tmp = tempfile.mkdtemp(prefix="arch_fleet_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    root = os.path.join(tmp, "proj")
    os.makedirs(root)
    try:
        import arch_claudecode as cc
        import arch_fleet as fl

        # ---- the run log on disk
        rid = "cc1000000000001"
        cc.write_meta(rid, prompt="add a test", mode="plan", model=None, budget=1.0, root=root, started=100.0, status="running")
        for i in range(3):
            cc.record_event(rid, {"t": i, "type": "assistant", "text": f"line {i}"})
        cc.write_meta(rid, status="done", cost=0.25, turns=4, session="s-1", ended=101.0)
        with open(os.path.join(cc.runs_dir(), rid + ".jsonl"), "a", encoding="utf-8") as f:
            f.write('{"t": 9, "type": "assis')                      # a crash mid-write
        d = cc.load_run(rid)
        check("load_run reads back every event in order and the merged meta; a torn last line is skipped",
              [e["text"] for e in d.get("events", [])] == ["line 0", "line 1", "line 2"] and d["status"] == "done"
              and d["prompt"] == "add a test" and d["cost"] == 0.25 and d["turns"] == 4 and d["session"] == "s-1"
              and d["started"] == 100.0, d)
        cc.write_meta("cc1000000000002", prompt="older", started=50.0, status="done")
        lr = cc.list_runs()
        check("list_runs gives the newest first, meta only", [r["id"] for r in lr[:2]] == [rid, "cc1000000000002"]
              and "events" not in lr[0], lr)
        check("list_runs honours the limit", len(cc.list_runs(limit=1)) == 1)
        check("load_run refuses a path-like id and reports an unknown run",
              "error" in cc.load_run("../evil") and cc.load_run("cc404").get("error") == "no such run")
        check("record_event refuses a path-like id without raising", cc.record_event("a/../b", {"text": "x"}) is False
              and not os.path.exists(os.path.join(tmp, "data", "b.jsonl")))
        with cc._LOCK:
            a1 = cc._new_run_id()
            cc._RUNS[a1] = {"events": []}
            a2 = cc._new_run_id()
            cc._RUNS.pop(a1)
        check("two runs started in the same millisecond get different ids", a1 != a2, (a1, a2))

        # ---- plan_check
        good = [{"name": "api", "brief": "add pagination", "files": ["api.py", "tests/test_api.py"], "budget_usd": 3},
                {"name": "docs", "brief": "document it", "files": ["docs/"], "budget_usd": 2, "mode": "plan"}]
        check("a sound plan has no problems", fl.plan_check(good) == [], fl.plan_check(good))
        ov = [dict(good[0]), dict(good[1], files=[".\\API.py" if os.name == "nt" else "./api.py"])]
        p = fl.plan_check(ov)
        check("two agents owning the same file are refused (slashes, ./ and case on Windows do not hide it)",
              any("both own" in x for x in p), p)
        p = fl.plan_check([dict(good[0]), dict(good[1], files=["tests"])])
        check("a folder overlapping another agent's file is refused", any("both own" in x for x in p), p)
        p = fl.plan_check([dict(good[0], brief="   "), good[1]])
        check("an empty brief is refused", any("brief is empty" in x for x in p), p)
        p = fl.plan_check([dict(good[0], budget_usd=15), dict(good[1], budget_usd=15)], total_cap_usd=20)
        check("budgets above the cap are refused", any("above the cap" in x for x in p), p)
        check("budgets exactly at the cap pass", fl.plan_check([dict(good[0], budget_usd=10), dict(good[1], budget_usd=10)], 20) == [])
        p = fl.plan_check([dict(good[0], budget_usd=0), dict(good[1], budget_usd="lots")])
        check("a zero or non-numeric budget is refused", sum("budget_usd" in x for x in p) == 2, p)
        p = fl.plan_check([dict(good[0], files=["../outside.py"]), dict(good[1], files=["C:/x.py"])])
        check("a path outside the project is refused", sum("not a path inside" in x for x in p) == 2, p)
        p = fl.plan_check([dict(good[0], mode="bypassPermissions"), dict(good[1], name="api", files=["b.py"])])
        check("an unknown mode and a repeated name are refused", any("mode must" in x for x in p) and any("share this name" in x for x in p), p)
        check("an empty plan is refused", fl.plan_check([]) and fl.plan_check(None))

        # ---- the test-command allow-list
        okc = ["python arch_hub_check.py", "py -3.12 arch_selftest_fleet.py", "node --check hub.js", "python arch_selftest.py --suite full"]
        check("allowed test commands pass", all(fl.command_problem(c) is None for c in okc), [fl.command_problem(c) for c in okc])
        bad = ["rm -rf .", "python evil.py", "python arch_x.py & del *", "python arch_x.py | more", "python arch_x.py > out",
               "python arch_../../x.py", "node --checkout", "", None, "git push"]
        check("anything else is refused", all(fl.command_problem(c) for c in bad), [(c, fl.command_problem(c)) for c in bad])

        # ---- launch with scripted stand-ins
        calls, ran = [], []

        def starter(**kw):
            calls.append(kw)
            if "explode" in kw["prompt"]:
                raise RuntimeError("no key")
            r = cc._new_run_id()
            cc.write_meta(r, prompt=kw["prompt"], mode=kw["mode"], status="running", started=time.time(), tag=kw["tag"], pid=os.getpid())
            cc.record_event(r, {"t": time.time(), "type": "assistant", "text": "working on " + kw["prompt"].split()[0]})
            return r

        outputs = {"git status --porcelain": [{"rc": 0, "output": " M old_dirty.py\n"},
                                              {"rc": 0, "output": " M old_dirty.py\n M api.py\n?? docs/new.md\n?? stray.txt\n"}],
                   "git diff --stat": [{"rc": 0, "output": " api.py | 2 +-\n 1 file changed\n"}],
                   "python arch_hub_check.py": [{"rc": 0, "output": "ALL OK"}],
                   "py -3.12 arch_selftest_fleet.py": [{"rc": 1, "output": "1 FAILED"}]}

        def runner(cmd, cwd, timeout):
            ran.append((cmd, cwd))
            return outputs[cmd].pop(0)

        try:
            fl.launch(good, root, tests=["python arch_hub_check.py", "rm -rf ."], starter=starter, runner=runner)
            refused = False
        except ValueError as e:
            refused = "not an allowed test command" in str(e)
        check("launch refuses a command outside the allow-list and starts nothing", refused and not calls and not ran)
        try:
            fl.launch(ov, root, starter=starter, runner=runner)
            refused = False
        except ValueError as e:
            refused = "both own" in str(e)
        check("launch refuses an overlapping plan before any agent starts", refused and not calls)

        tests = ["python arch_hub_check.py", "py -3.12 arch_selftest_fleet.py"]
        fid = fl.launch(good, root, total_cap_usd=10, tests=tests, starter=starter, runner=runner, poll=0.05)
        st_path = os.path.join(tmp, "data", "fleet", fid + ".json")
        check("launch starts every agent at once and writes the fleet state", len(calls) == 2 and os.path.isfile(st_path), calls)
        p0, p1 = calls[0]["prompt"], calls[1]["prompt"]
        check("each brief carries a footer naming its own files and forbidding others",
              "edit ONLY" in p0 and "api.py" in p0 and "tests/test_api.py" in p0 and "docs" not in p0.split("---")[-1]
              and "read-only task" in p1 and "api.py" not in p1.split("---")[-1] and "python arch_hub_check.py" in p0, p0)
        check("the starter gets mode, budget and the fleet id as tag",
              calls[0]["mode"] == "acceptEdits" and calls[1]["mode"] == "plan" and calls[0]["budget"] == 3.0 and calls[0]["tag"] == fid)
        time.sleep(0.3)
        s = fl.status(fid)
        check("while the agents work, status shows them running with their last event and no tests run yet",
              s["phase"] == "running" and [a["status"] for a in s["agents"]] == ["running", "running"]
              and s["agents"][0]["last_event"]["text"] == "working on add" and not s["tests"]
              and [c for c, _ in ran] == ["git status --porcelain"], s)
        cc.write_meta(s["agents"][0]["run"], status="done", cost=0.75, turns=5)
        time.sleep(0.3)
        check("the fleet waits for every agent before testing", fl.status(fid)["phase"] == "running" and len(ran) == 1)
        cc.write_meta(s["agents"][1]["run"], status="error", cost=0.25, turns=2, error="budget reached")
        deadline = time.time() + 10
        while time.time() < deadline and fl.status(fid)["phase"] != "done":
            time.sleep(0.05)
        s = fl.status(fid)
        check("when all are done the fleet itself runs each test in the project folder, then git diff --stat",
              s["phase"] == "done" and [c for c, _ in ran] == ["git status --porcelain"] + tests + ["git diff --stat", "git status --porcelain"]
              and all(cwd == os.path.abspath(root) for _, cwd in ran), ran)
        check("pass and fail are recorded per command, and one failure makes the verdict fail",
              [(t["cmd"], t["ok"], t["rc"]) for t in s["tests"]] == [(tests[0], True, 0), (tests[1], False, 1)]
              and s["tests"][1]["output"] == "1 FAILED" and s["verdict"] == "fail", s["tests"])
        check("diffstat, total cost and final agent states are kept",
              "api.py | 2" in (s["diffstat"] or "") and s["total_cost"] == 1.0
              and [a["status"] for a in s["agents"]] == ["done", "error"] and s["agents"][0]["turns"] == 5, s)
        check("a changed file no agent owned is reported (one dirty before launch is not)", s["strays"] == ["stray.txt"], s["strays"])
        with open(st_path, encoding="utf-8") as f:
            disk = json.load(f)
        check("the state on disk holds the same result", disk["verdict"] == "fail" and len(disk["tests"]) == 2 and disk["phase"] == "done")

        # ---- could not run -> not_sure, never a pass; a starter that fails does not stop the others
        calls.clear()
        ran.clear()

        def quick(**kw):
            r = starter(**kw)
            cc.write_meta(r, status="done", cost=0.5, turns=1)
            return r
        outputs.update({"git status --porcelain": [{"rc": None, "output": "no git"}, {"rc": None, "output": "no git"}],
                        "git diff --stat": [{"rc": None, "output": "no git"}],
                        "python arch_hub_check.py": [{"rc": None, "output": "(stopped after 1800 s)"}]})
        plan2 = [dict(good[0]), dict(good[1], brief="explode please")]
        fid2 = fl.launch(plan2, root, tests=["python arch_hub_check.py"], starter=quick, runner=runner, poll=0.05)
        deadline = time.time() + 10
        while time.time() < deadline and fl.status(fid2)["phase"] != "done":
            time.sleep(0.05)
        s2 = fl.status(fid2)
        check("a starter that raises marks only that agent as an error; the rest still run",
              [a["status"] for a in s2["agents"]] == ["done", "error"] and "could not start" in (s2["agents"][1]["error"] or ""), s2["agents"])
        check("a test that could not finish is ok=None and the verdict is not_sure, never pass",
              s2["tests"][0]["ok"] is None and s2["verdict"] == "not_sure" and s2["diffstat"] is None, s2)

        # ---- an agent whose process is gone is not waited on forever
        gone = cc._new_run_id()
        cc.write_meta(gone, status="running", pid=-1, started=time.time())
        check("a run left 'running' by another (dead) process counts as lost", fl._view(gone)["status"] == "lost")

        lf = fl.list_fleets()
        check("list_fleets lists both, newest first", [f["id"] for f in lf[:2]] == [fid2, fid] and lf[1]["total_cost"] == 1.0, lf)
        check("status refuses a path-like fleet id and reports an unknown one",
              "error" in fl.status("../x") and fl.status("fl0000000000000").get("error") == "no such fleet")
    finally:
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(tmp, ignore_errors=True)
    n, ok = len(results), sum(results)
    print(f"\n  {'ALL ' + str(n) + ' FLEET CHECKS PASSED' if ok == n else str(n - ok) + ' of ' + str(n) + ' FLEET CHECKS FAILED'}\n")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(run())
