"""Self-test for local agents (arch_localagent.py) and the fleet's kind "local_agent" (arch_fleet.py).

A scripted local model (the fake Ollama server from arch_selftest, answering with tool calls) proves: writes outside the agent's own
files are refused (before the tool runs, and again at the engine's commit), the agent runs the tests itself and keeps the three
outcomes, the step budget, the seconds budget, a stall and the Stop button each end the run, every step is in the cc_runs format with
by_claude false, the fleet runs local agents one at a time on the graphics card, and every template header parses (with every brief
of the day covered by a template). No GPU, no network, no Claude.
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP = "def add(a, b):\n    return a - b\n"
TEST = ("import unittest\nfrom app import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n")
OTHER = "VALUE = 1\n"
NEW_TEMPLATES = ("code_audit", "report_apply", "suite_wiring", "docs_update", "secret_scrub", "decision_module", "event_explainer",
                 "flow_improver", "harness_pack", "flow_pack", "eval_family", "project_starters", "host_mesh", "night_runner",
                 "suite_fixer", "design_pass", "onboarding", "agent_runtime")
LOCAL_YES = ("suite_wiring", "docs_update", "project_starters", "suite_fixer")


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_agentlib as lib
    import arch_claudecode as cc
    import arch_core as core
    import arch_fleet as fl
    import arch_localagent as la
    import arch_roles
    import arch_selftest

    script = []                                                   # the scripted model's next replies, in order

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            if "You are a local agent" not in user:
                return "{}"
            return script.pop(0) if script else json.dumps({"final": "nothing more to do"})

    def tool(name, **args):
        return json.dumps({"tool": name, "args": args})

    tmp = tempfile.mkdtemp(prefix="archlocalagent_")
    keys = ("OLLAMA_HOST", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in keys}
    core.set_home(os.path.join(tmp, "home"))
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"                        # a scripted model answers the same prompt differently; no reply cache
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    quiet = lambda m: None
    try:
        proj = os.path.join(tmp, "p")
        os.makedirs(proj)

        def reset():
            for rel, text in (("app.py", APP), ("test_app.py", TEST), ("other.py", OTHER)):
                with open(os.path.join(proj, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            del script[:]

        def read(rel):
            with open(os.path.join(proj, rel), encoding="utf-8") as f:
                return f.read()

        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                        "behavior": {"playwright": "off"}})

        def go(brief="fix add() in app.py so the tests pass", files=("app.py",), **kw):
            kw.setdefault("cfg", core.deep_merge(cfg, {}))
            return la.run(brief, proj, files=list(files), say=quiet, **kw)

        # ---- own files: the path rule itself
        check("owns(): a listed file, a file inside a listed folder; not a neighbour, not .., not an absolute path",
              la.owns(["app.py", "docs"], "app.py") and la.owns(["app.py", "docs"], "./docs/x.md") and la.owns(["docs/"], "docs\\a\\b.md")
              and not la.owns(["app.py"], "other.py") and not la.owns(["docs"], "docs2/x.md") and not la.owns(["app.py"], "../app.py")
              and not la.owns(["app.py"], "C:/p/app.py") and not la.owns(["app.py"], "/app.py"))
        check("write_targets(): apply names every block's file; edit_lines its path; reading tools write nothing",
              la.write_targets("apply", {"blocks": "FILE: a.py\n```python\nx = 1\n```\nFILE: b/c.py\n```python\ny = 2\n```\n"}) == ["a.py", "b/c.py"]
              and la.write_targets("edit_lines", {"path": "z.py"}) == ["z.py"] and la.write_targets("read", {"path": "z.py"}) == [],
              la.write_targets("apply", {"blocks": "FILE: a.py\n```python\nx = 1\n```\n"}))

        # ---- a whole run: look, try to write elsewhere (refused twice), fix its own file, run the tests, report
        reset()
        script.extend([
            tool("ls"),
            tool("read", path="app.py"),
            tool("edit_lines", path="other.py", start=1, end=1, text="VALUE = 2"),
            tool("apply", blocks="FILE: other.py\n```python\nVALUE = 3\n```\n"),
            tool("edit_lines", path="app.py", start=2, end=2, text="    return a + b"),
            tool("run_tests"),
            json.dumps({"final": "app.py: add() now adds; the tests pass"}),
        ])
        r = go()
        check("a scripted run ends with the model's own report", r["status"] == "done" and "add() now adds" in (r["answer"] or ""), r)
        check("a write to a file the agent does not own is refused, and the file is untouched", read("other.py") == OTHER
              and "other.py" in r["refused"], (read("other.py"), r["refused"]))
        check("a write to its own file goes through", "return a + b" in read("app.py") and r["changed"] == ["app.py"], r["changed"])
        check("it ran the tests itself and kept the outcome (True: ran after its last change and passed)",
              r["checks"]["ok"] is True and r["tools"].count("run_tests") == 1, r["checks"])
        check("the result says by_claude False and which tier and model did the work",
              r["by_claude"] is False and r["tier"] == "worker" and r["model"] == "fake:7b", r)

        # ---- the run log: arch_claudecode's cc_runs format
        d = cc.load_run(r["id"])
        ev = d.get("events") or []
        types = [x.get("type") for x in ev]
        check("the run is in cc_runs (<id>.jsonl + <id>.meta.json) and load_run/list_runs see it",
              os.path.isfile(os.path.join(cc.runs_dir(), r["id"] + ".jsonl")) and os.path.isfile(os.path.join(cc.runs_dir(), r["id"] + ".meta.json"))
              and any(m.get("id") == r["id"] for m in cc.list_runs()), d.get("error"))
        check("the meta has the Claude run's fields plus by_claude false, kind local_agent, cost 0",
              all(k in d for k in ("prompt", "mode", "model", "budget", "max_turns", "root", "started", "ended", "status", "cost", "turns", "error", "pid"))
              and d["by_claude"] is False and d["kind"] == "local_agent" and d["cost"] == 0.0 and d["status"] == "done" and d["pid"] == os.getpid()
              and d["n_events"] == len(ev), {k: d.get(k) for k in ("by_claude", "kind", "cost", "status", "n_events")})
        check("events: system.init first, result last, a tool and a tool_result per step with the same id, each with t and text",
              types[0] == "system.init" and types[-1] == "result" and types.count("tool") == 6 and types.count("tool_result") == 6
              and all(isinstance(x.get("t"), (int, float)) and isinstance(x.get("text"), str) and x["text"] for x in ev)
              and [x["id"] for x in ev if x["type"] == "tool"] == [x["id"] for x in ev if x["type"] == "tool_result"], types)
        check("a refused write shows in the log as an error tool_result, with the tool's main argument like Claude Code shows it",
              any(x["type"] == "tool_result" and x.get("error") and "not your file" in x["text"] for x in ev)
              and any(x["type"] == "tool" and x.get("name") == "edit_lines" and x.get("arg") == "other.py" for x in ev))
        recs = [json.loads(x) for x in open(os.path.join(glob.glob(os.path.join(proj, ".arch", "runs", "*_local_agent"))[-1], "trace.jsonl"), encoding="utf-8")]
        check("every tool call is also in the engine's trace (the tool trajectory the dataset exports)",
              [t["tool"] for t in recs if t.get("type") == "tool"] == ["ls", "read", "edit_lines", "apply", "edit_lines", "run_tests"])

        # ---- the second wall: the engine's commit refuses a write outside the files, whatever path reached it
        e = core.Engine(proj, "local_agent", "g", core.deep_merge(cfg, {}), review="auto", say=quiet, persist=False)
        refused = []
        la._guard(e, ["app.py"], refused)
        p = e.put_file("other.py", "VALUE = 9\n")
        e.apply("FILE: other.py\n```python\nVALUE = 8\n```\n")
        check("the commit guard refuses other files even when a tool's own check was skipped", read("other.py") == OTHER
              and p["status"] == "error" and refused.count("other.py") == 2, (p.get("status"), refused))

        # ---- stops: a stall, the step budget, the seconds budget, the Stop button, unusable replies
        reset()
        script.extend([tool("read", path="app.py")] * 6)
        r = go(budget={"calls": 10})
        check("the same call again and again is a stall: the run stops by itself", r["status"] == "stopped" and "stalled" in r["why"]
              and r["steps"] <= la.STALL_AFTER + 1, r)
        reset()
        script.extend([tool("ls"), tool("read", path="app.py"), tool("outline", path="app.py"), tool("grep", pattern="add")])
        r = go(budget={"calls": 2})
        check("the step budget ends the run after that many model calls", r["status"] == "stopped" and r["steps"] == 2
              and ("2 model calls" in r["why"]), r)
        check("a stopped run is written as stopped, with the reason, in the run log",
              cc.load_run(r["id"]).get("status") == "stopped" and "2 model calls" in (cc.load_run(r["id"]).get("error") or ""))
        reset()
        script.append(tool("ls"))
        r = go(budget={"calls": 5, "seconds": 0.0001})
        check("the seconds budget is enforced by the engine before the request goes out", r["status"] == "stopped" and "seconds" in r["why"]
              and r["steps"] == 0, r)
        reset()
        r = go(should_stop=lambda: True)
        check("the Stop button (should_stop) ends the run before the next step", r["status"] == "stopped" and r["why"] == "stopped from the page", r)
        reset()
        script.extend(["this is not json", "still not json", "{\"nope\": 1}", tool("ls")])
        r = go()
        check("unusable replies cost a step each and three in a row stop the run", r["status"] == "stopped" and "stalled" in r["why"], r)
        reset()
        script.extend([tool("read", path="app.py"), tool("edit_lines", path="app.py", start=2, end=2, text="    return a + b"),
                       json.dumps({"final": "done, did not run the tests"})])
        r = go()
        check("a change not followed by run_tests leaves the agent's own check as None (not run), never a pass",
              r["status"] == "done" and r["checks"]["ok"] is None, r["checks"])
        reset()
        script.append(tool("edit_lines", path="app.py", start=1, end=1, text="x = 1"))
        r = go(files=())
        check("an agent that owns no files is read-only: the write tools are not offered", read("app.py") == APP
              and r["changed"] == [] and cc.load_run(r["id"]).get("mode") == "plan"
              and "edit_lines" not in json.dumps(la.preamble("b", [], [t for t in la.TOOLS if t not in la.WRITE_TOOLS], False)), r)

        # ---- a template as the job
        reset()
        script.append(json.dumps({"final": "the suite list already has them all"}))
        r = la.run("suite_wiring", proj, values=lib.load("suite_wiring")["examples"], cfg=core.deep_merge(cfg, {}), say=quiet)
        check("a template name is filled from the library and its own files become the agent's files",
              r["status"] == "done" and r["files"] == ["architect.py"] and r["template"] == "suite_wiring"
              and "test-wiring agent" in cc.load_run(r["id"]).get("prompt", ""), r)
        try:
            la.run("x", proj, files=["../outside.py"], cfg=cfg, say=quiet)
            check("a file outside the project is refused before anything runs", False)
        except ValueError:
            check("a file outside the project is refused before anything runs", True)

        # ---- template headers: local_ok + tier on every brief-derived template, every brief covered
        names = [t["name"] for t in lib.list_templates()]
        bad = []
        for n in names:
            try:
                la.header(n)
            except ValueError as ex:
                bad.append(f"{n}: {ex}")
        check("every template header parses (the library's loader and the local_ok/tier lines)", not bad and len(names) >= 39, bad)
        heads = {n: la.header(n) for n in NEW_TEMPLATES if n in names}
        check("every new template is there, says local_ok yes/no and suggests a tier",
              len(heads) == len(NEW_TEMPLATES) and all(h["local_ok"] in (True, False) and h["tier"] in la.TIERS for h in heads.values()),
              [n for n in NEW_TEMPLATES if n not in heads or heads[n]["local_ok"] is None])
        check("the jobs a 4B worker with tools can do say yes (worker); heavy-judgment jobs say no",
              all(heads[n]["local_ok"] and heads[n]["tier"] == "worker" for n in LOCAL_YES)
              and all(not h["local_ok"] for n, h in heads.items() if n not in LOCAL_YES))
        check("who_runs() sends local_ok work to a local agent and the rest to Claude or the heavy tier, in plain words",
              la.who_runs("suite_fixer")["runner"] == "local_agent" and la.who_runs("code_audit")["runner"] == "claude"
              and la.who_runs("code_audit")["tier"] == "heavy" and la.who_runs("builder")["runner"] == "claude"
              and "does not say" in la.who_runs("builder")["why"])
        sources = " ".join(lib.load(n)["source"] for n in names)
        briefs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "hub_data", "learning", "briefs", "brief_*.md")))
        check("every brief given to an agent today is the source of some template", all(b in sources for b in briefs),
              [b for b in briefs if b not in sources])
        own = os.path.join(tmp, "tpl")
        os.makedirs(own)
        with open(os.path.join(own, "odd.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: odd\ntitle: t\npurpose: p\nwhy: w\n# local_ok: maybe\nbudget_usd: 1\nfiles: a.py\n---\nbody\n")
        try:
            la.header("odd", own)
            check("a local_ok that is not yes or no is refused in plain words", False)
        except ValueError as ex:
            check("a local_ok that is not yes or no is refused in plain words", "yes or no" in str(ex), ex)

        # ---- the fleet: kind local_agent
        check("plan_check accepts a local_agent with no dollar budget (it spends none)",
              fl.plan_check([{"name": "a", "kind": "local_agent", "goal": "g", "files": ["app.py"], "budget": {"calls": 5, "seconds": 60}}]) == [])
        probs = fl.plan_check([{"name": "a", "kind": "local_agent", "goal": "", "files": ["app.py"]},
                               {"name": "b", "kind": "local_agent", "goal": "g", "files": ["b.py"], "tier": "huge"},
                               {"name": "c", "kind": "local_agent", "goal": "g", "files": ["c.py"], "budget": {"calls": -1}},
                               {"name": "d", "kind": "local_agent", "goal": "g", "files": ["d.py"], "mode": "plan"},
                               {"name": "e", "kind": "local_agent", "goal": "g", "files": ["app.py"]}])
        check("plan_check refuses a local_agent with no goal, a bad tier, a bad budget, plan mode, or a shared file",
              len(probs) == 5 and any("both own" in p for p in probs), probs)
        check("a local_agent on the router tier takes the CPU lane; others queue for the graphics card",
              fl.lane_of({"kind": "local_agent", "tier": "router"}) == "cpu" and fl.lane_of({"kind": "local_agent", "tier": "worker"}) == "gpu")

        def wait_done(fid, limit=60):
            t0 = time.time()
            while time.time() - t0 < limit:
                s = fl.status(fid)
                if s.get("phase") == "done":
                    return s
                time.sleep(0.05)
            return fl.status(fid)

        ok_runner = lambda cmd, cwd, timeout: {"rc": 0, "output": ""}
        live, peak, calls, lock = [0], [0], [], threading.Lock()

        def fake_agent(brief, root, files=None, budget=None, tier="worker", tag=None, should_stop=None, on_start=None, **_):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                calls.append({"files": files, "budget": budget, "tier": tier, "tag": tag, "brief": brief})
            time.sleep(0.15)
            with lock:
                live[0] -= 1
            return {"id": f"la{len(calls)}", "status": "done", "answer": "ok", "checks": {"ok": True, "status": "pass"}, "refused": []}
        fid = fl.launch([{"name": "one", "kind": "local_agent", "goal": "fix a", "files": ["a.py"], "budget": {"calls": 7}},
                         {"name": "two", "kind": "local_agent", "goal": "fix b", "files": ["b.py"], "tier": "heavy"}],
                        proj, tests=["python arch_probe_check.py"], runner=ok_runner, poll=0.05, agent_runner=fake_agent)
        s = wait_done(fid)
        check("two local agents in one fleet take the graphics card one at a time", peak[0] == 1 and len(calls) == 2, peak[0])
        check("each gets its own files, budget, tier and the fleet id as its tag",
              sorted((tuple(c["files"]), c["tier"]) for c in calls) == [(("a.py",), "worker"), (("b.py",), "heavy")]
              and any(c["budget"] == {"calls": 7} for c in calls) and all(c["tag"] == fid for c in calls)
              and all("Your files" not in c["brief"] for c in calls), calls)
        check("the fleet finishes, runs its own checks afterwards and reports done agents with their run and own-check result",
              s.get("phase") == "done" and s.get("verdict") == "pass" and all(a["status"] == "done" and a["kind"] == "local_agent"
                                                                          and a["run"] and a["checks"]["ok"] is True for a in s["agents"]), s)

        reset()
        script.extend([tool("read", path="app.py"), tool("edit_lines", path="app.py", start=2, end=2, text="    return a + b"), tool("run_tests"),
                       json.dumps({"final": "fixed add()"})])
        real = lambda *a, **k: la.run(*a, cfg=core.deep_merge(cfg, {}), say=quiet, **k)
        fid2 = fl.launch([{"name": "fixer", "kind": "local_agent", "goal": "fix add() in app.py", "files": ["app.py"]}],
                         proj, tests=["python arch_probe_check.py"], runner=ok_runner, poll=0.05, agent_runner=real)
        s2 = wait_done(fid2)
        ag = (s2.get("agents") or [{}])[0]
        meta = cc.load_run(ag.get("run")) if ag.get("run") else {}
        check("a real local agent runs inside the fleet: done, its run log tagged with the fleet id, the fix on disk",
              ag.get("status") == "done" and meta.get("tag") == fid2 and meta.get("by_claude") is False
              and "return a + b" in read("app.py") and (ag.get("checks") or {}).get("ok") is True, (ag, meta.get("tag")))
    finally:
        srv.shutdown()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n, ok = len(results), sum(results)
    if ok == n:
        print(f"ALL {n} LOCALAGENT CHECKS PASSED")
    else:
        print(f"{n - ok} OF {n} LOCALAGENT CHECKS FAILED")
    return ok == n


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
