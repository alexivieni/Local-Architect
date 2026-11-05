"""Self-test for local agents in the fleet (arch_fleet): the graphics card takes one writer at a time, planning-only and Claude agents
run beside it, files stay owned, the hybrid plan has its shape, and everything is persisted.
No subprocess, no GPU, no SDK: the Claude starter, the local runner and the command runner are scripted stand-ins.
Run: python arch_selftest_localfleet.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, cond, detail=None):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f"  -> {str(detail)[:500]}"))

    tmp = tempfile.mkdtemp(prefix="arch_localfleet_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    root = os.path.join(tmp, "proj")
    os.makedirs(root)
    try:
        import arch_claudecode as cc
        import arch_fleet as fl

        lock = threading.Lock()
        spans = {}                                   # agent -> [start, end] of its local run
        live = {"now": 0, "max_gpu": 0}
        goals = {}
        order = []                                   # every start, Claude or local, in time order
        counter = [0]
        script = {"rc": {}}

        def local_runner(argv, cwd, timeout, env):
            name = env["ARCHITECT_FLEET_AGENT"].split(":", 1)[1]
            cpu = "--flow" in argv and argv[argv.index("--flow") + 1] in fl.CPU_FLOWS
            with lock:
                spans[name] = [time.time(), None]
                goals[name] = argv[3]
                order.append(name)
                live["now"] += 0 if cpu else 1
                live["max_gpu"] = max(live["max_gpu"], live["now"])
            time.sleep(0.25)
            with lock:
                spans[name][1] = time.time()
                live["now"] -= 0 if cpu else 1
            rc = script["rc"].get(name, 0)
            return {"rc": rc, "output": f"[done]\nreport: {os.path.join(cwd, '.arch', 'runs', name, 'report.md')}\n"}

        def starter(root, prompt, mode, model, budget, max_turns, tag):
            with lock:
                counter[0] += 1
                rid = f"cc{2000000000000 + counter[0]}"
                order.append("claude:" + prompt.split()[0])
            cc.write_meta(rid, prompt=prompt, mode=mode, model=model, budget=budget, root=root, started=time.time(), status="done",
                          cost=0.5, turns=2)
            cc.record_event(rid, {"t": 1, "type": "assistant", "text": "BRIEF-FOR-" + prompt.split()[0]})
            return rid

        def runner(cmd, cwd, timeout):
            return {"rc": 0, "output": ""}

        def wait_done(fid, secs=15):
            end = time.time() + secs
            while time.time() < end and fl.status(fid)["phase"] != "done":
                time.sleep(0.05)
            return fl.status(fid)

        def L(name, files, flow=None, **kw):
            return dict({"name": name, "kind": "local", "goal": "GOAL:" + name + " do it", "flow": flow, "files": files}, **kw)

        # ---- plan_check for local agents
        ok = [L("a", ["a.py"]), L("b", ["b.py"]), {"name": "think", "brief": "plan", "files": ["docs/"], "budget_usd": 1, "mode": "plan"}]
        check("a plan with local and Claude agents on their own files passes; a local agent needs no budget", fl.plan_check(ok) == [],
              fl.plan_check(ok))
        p = fl.plan_check([L("a", ["src/"]), dict(ok[2], name="c", files=["src/x.py"], mode="acceptEdits")])
        check("a local agent and a Claude agent sharing files are refused", any("both own" in x for x in p), p)
        p = fl.plan_check([L("a", ["x.py"]), L("b", ["x.py"])])
        check("two local agents sharing a file are refused", any("both own" in x for x in p), p)
        check("a local agent without a goal is refused", any("needs a goal" in x for x in fl.plan_check([dict(L("a", ["a.py"]), goal="")])))
        check("a local agent in plan mode is refused", any("no plan mode" in x for x in fl.plan_check([dict(L("a", ["a.py"]), mode="plan")])))
        check("a bad flow name and a bad kind are refused",
              any("not a flow name" in x for x in fl.plan_check([L("a", ["a.py"], flow="x; rm")]))
              and any("kind must be" in x for x in fl.plan_check([dict(L("a", ["a.py"]), kind="cloud")])))
        check("'after' must name an earlier agent", any("'after'" in x for x in fl.plan_check([L("a", ["a.py"], after=["zzz"])]))
              and any("'after'" in x for x in fl.plan_check([L("a", ["a.py"], after=["a"])]))
              and any("'after'" in x for x in fl.plan_check([L("a", ["a.py"], after=["b"]), L("b", ["b.py"])])))
        try:
            fl.launch([L("a", ["x.py"]), L("b", ["x.py"])], root, starter=starter, runner=runner, local_runner=local_runner)
            refused = False
        except ValueError as e:
            refused = "both own" in str(e)
        check("launch refuses overlapping local files before anything runs", refused and not order)

        # ---- GPU serialisation, with a CPU step and a Claude agent beside it
        plan = [L("w1", ["w1.py"]), L("w2", ["w2.py"], flow="build"), L("w3", ["w3.py"]),
                L("scout", ["notes/scout.md"], flow="route"),
                {"name": "thinker", "brief": "think about it", "files": ["docs/"], "budget_usd": 1, "mode": "plan"}]
        fid = fl.launch(plan, root, tests=["python arch_hub_check.py"], starter=starter, runner=runner, local_runner=local_runner,
                        poll=0.05)
        s = wait_done(fid)
        check("every agent finished and the fleet passed", s["phase"] == "done" and s["verdict"] == "pass"
              and all(a["status"] == "done" for a in s["agents"]), s)
        check("three local writers never overlapped on the card", live["max_gpu"] == 1, live)
        check("the writers ran in plan order", [n for n in order if n in ("w1", "w2", "w3")] == ["w1", "w2", "w3"], order)
        gpu_end = max(spans[n][1] for n in ("w1", "w2", "w3"))
        gpu_start = min(spans[n][0] for n in ("w1", "w2", "w3"))
        check("the CPU planning step ran beside a writer, not after the queue",
              spans["scout"][0] < spans["w1"][1] and spans["scout"][1] <= gpu_end and spans["scout"][0] >= gpu_start - 0.5, spans)
        check("the Claude agent started at once, while the first writer ran", order.index("claude:think") < order.index("w3"), order)
        by = {a["name"]: a for a in s["agents"]}
        check("state says who waited and why", by["w2"]["waiting_for"] == "w1" and by["w2"]["waited_s"] > 0.1
              and "graphics card" in by["w2"]["why"] and by["w3"]["waiting_for"] in ("w1", "w2")
              and not by["w1"]["waiting_for"] and not by["scout"]["waiting_for"] and "CPU" in by["scout"]["why"],
              {k: (v["waiting_for"], v["waited_s"], v["why"]) for k, v in by.items()})
        check("the waits list names the agents that waited", {w["agent"] for w in s["waits"]} >= {"w2", "w3"}, s["waits"])
        check("lanes are recorded", by["w1"]["lane"] == "gpu" and by["scout"]["lane"] == "cpu" and by["thinker"]["kind"] == "claude")
        check("a local agent costs nothing; the total is the Claude agent's", by["w1"]["cost"] == 0.0 and s["total_cost"] == 0.5, s["total_cost"])

        # ---- the command a local agent runs, and what is persisted
        argv_goal = goals["w2"]
        check("a local agent's goal carries the footer with its own files only",
              "change ONLY" in argv_goal and "w2.py" in argv_goal and "w1.py" not in argv_goal)
        with open(os.path.join(tmp, "data", "fleet", fid + ".json"), encoding="utf-8") as f:
            disk = json.load(f)
        da = {a["name"]: a for a in disk["agents"]}
        check("the state on disk keeps the local runs: kind, exit code, output tail and the run's trace folder",
              da["w1"]["kind"] == "local" and da["w1"]["rc"] == 0 and "report:" in da["w1"]["output"]
              and da["w1"]["trace"] == os.path.join(os.path.abspath(root), ".arch", "runs", "w1"), da["w1"])
        check("the listing counts local agents too", fl.list_fleets()[0]["agents"] == 5)

        # ---- a failed local run and one that could not run
        script["rc"] = {"f1": 1, "f2": None}
        fid2 = fl.launch([L("f1", ["f1.py"]), L("f2", ["f2.py"]), L("ok", ["ok.py"])], root, tests=["python arch_hub_check.py"],
                         starter=starter, runner=runner, local_runner=local_runner, poll=0.05)
        s2 = wait_done(fid2)
        b2 = {a["name"]: a for a in s2["agents"]}
        check("a failed local run is an error with its exit code; one that could not run says so; the queue goes on",
              b2["f1"]["status"] == "error" and b2["f1"]["rc"] == 1 and "exit code 1" in b2["f1"]["error"]
              and b2["f2"]["status"] == "error" and "could not run" in b2["f2"]["error"] and b2["ok"]["status"] == "done", b2)
        check("the card is free again afterwards", fl._GPU_HOLDER[0] is None and not fl._GPU_QUEUE)

        # ---- hybrid_plan
        parts = [{"name": "api", "files": ["api.py"], "task": "add paging"}, {"name": "cli", "files": ["cli.py"], "flow": "feature"}]
        hp = fl.hybrid_plan("add paging everywhere", parts)
        names = [a["name"] for a in hp]
        check("hybrid_plan: a planner and a writer per part, then one reviewer",
              names == ["api-plan", "cli-plan", "api", "cli", "review"], names)
        kinds = {a["name"]: (a["kind"], a.get("mode")) for a in hp}
        check("planners and the reviewer are read-only Claude agents; writers are local",
              kinds["api-plan"] == ("claude", "plan") and kinds["review"] == ("claude", "plan") and kinds["api"][0] == "local"
              and kinds["cli"][0] == "local", kinds)
        check("each writer comes after its planner and owns the part's files; the reviewer comes after every writer",
              hp[2]["after"] == ["api-plan"] and hp[2]["files"] == ["api.py"] and hp[3]["flow"] == "feature"
              and hp[4]["after"] == ["api", "cli"])
        check("the hybrid plan passes plan_check", fl.plan_check(hp) == [], fl.plan_check(hp))
        check("hybrid_plan without the reviewer, and bad parts refused",
              [a["name"] for a in fl.hybrid_plan("g", parts, review=False)] == ["api-plan", "cli-plan", "api", "cli"]
              and all(_raises(lambda b=b: fl.hybrid_plan("g", b)) for b in ([], [{"name": "x y", "files": ["a"]}],
                                                                                [{"name": "ok", "files": []}])))

        order.clear()
        script["rc"] = {}
        fid3 = fl.launch(hp, root, total_cap_usd=5, tests=["python arch_hub_check.py"], starter=starter, runner=runner,
                         local_runner=local_runner, poll=0.05)
        s3 = wait_done(fid3)
        check("the hybrid fleet ran to the end and passed", s3["phase"] == "done" and s3["verdict"] == "pass"
              and all(a["status"] == "done" for a in s3["agents"]), [(a["name"], a["status"], a["error"]) for a in s3["agents"]])
        first_local = min(order.index("api"), order.index("cli"))
        check("both planners started before any writer, and the reviewer after both writers",
              order.index("claude:Overall") < first_local and order.count("claude:Overall") == 3
              and max(i for i, n in enumerate(order) if n == "claude:Overall") > max(order.index("api"), order.index("cli")), order)
        check("a writer is handed its planner's brief", "BRIEF-FOR-Overall" in goals["api"] and "Brief from api-plan" in goals["api"],
              goals["api"][:300])
        check("the writers did not overlap on the card", live["max_gpu"] == 1)

        # ---- a dependency that failed: the dependent is not started
        script["rc"] = {"bad": 1}
        order.clear()
        fid4 = fl.launch([L("bad", ["bad.py"]), L("next", ["next.py"], after=["bad"])], root, tests=["python arch_hub_check.py"],
                         starter=starter, runner=runner, local_runner=local_runner, poll=0.05)
        s4 = wait_done(fid4)
        b4 = {a["name"]: a for a in s4["agents"]}
        check("an agent whose predecessor failed is not started and says why",
              b4["next"]["status"] == "error" and "not started" in b4["next"]["error"] and "next" not in order, b4["next"])

        # ---- stop while queued
        script["rc"] = {}
        slow = threading.Event()

        def slow_runner(argv, cwd, timeout, env):
            slow.wait(5)
            return {"rc": 0, "output": ""}
        fid5 = fl.launch([L("s1", ["s1.py"]), L("s2", ["s2.py"])], root, tests=["python arch_hub_check.py"], starter=starter,
                         runner=runner, local_runner=slow_runner, poll=0.05)
        time.sleep(0.4)
        fl.stop(fid5)
        time.sleep(0.4)
        mid = {a["name"]: a["status"] for a in fl.status(fid5)["agents"]}
        check("stop ends a queued local agent before it ever starts", mid["s2"] == "stopped", mid)
        slow.set()
        s5 = wait_done(fid5)
        check("the stopped fleet still finishes and releases the card", s5["phase"] == "done" and fl._GPU_HOLDER[0] is None, s5["phase"])
    finally:
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(tmp, ignore_errors=True)

    n = len(results)
    if all(results):
        print(f"\nALL {n} LOCALFLEET CHECKS PASSED")
        return 0
    print(f"\n{results.count(False)} of {n} LOCALFLEET CHECKS FAILED")
    return 1


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    sys.exit(run())
