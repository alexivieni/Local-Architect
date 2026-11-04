"""Self-test for flows as data (arch_dataflow.py + arch_gates.py): every shipped flow validates, the expression evaluator is safe, templates
render, budgets and the kill switch stop a run, a check that cannot run ends in 'not sure' rather than a pass, foreach handles its fallback
and stops at a failing item, and a data flow produces the same outcome as the Python class it replaces. No model, no GPU: the fake server answers."""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MOD = "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    if b == 0:\n        raise ZeroDivisionError('divide by zero')\n    return a / b\n"
TEST = "import unittest\nfrom app import add, divide\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_dataflow as df
    import arch_gates
    import arch_roles
    import arch_selftest
    tmp = tempfile.mkdtemp(prefix="archflow_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    core.set_home(os.path.join(tmp, "home"))
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    fake = ThreadingHTTPServer(("127.0.0.1", 0), arch_selftest.Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{fake.server_address[1]}"
    quiet = lambda m: None
    try:
        # ---- every shipped flow is valid, and the ids the harness knows are covered
        docs = {}
        bad = {}
        for f in sorted(os.listdir(df.FLOWS_DIR)):
            if not f.endswith(".yaml"):
                continue
            d = df.load_yaml_file(os.path.join(df.FLOWS_DIR, f))
            docs[d.get("id", f)] = d
            probs = df.validate(d)
            if probs:
                bad[f] = probs
        check("every flows/*.yaml parses and validates (steps, kinds, gates, prompts, expressions)", len(docs) >= 18 and not bad, bad)
        check("the flow files cover the flows the harness ships", {"build", "bugfix", "design", "refactor", "test", "optimize", "hunt", "review", "secure", "config", "network", "ml", "pentest", "cicd", "purge", "new", "feature", "ship"} <= set(docs), sorted(docs))
        missing_prompt = [(fid, s["id"], s.get("prompt")) for fid, d in docs.items() for s in df.validate_steps(d) if s.get("prompt") and not os.path.isfile(df.prompt_path(fid, s["prompt"]))]
        check("every prompt a step names exists under prompts/", not missing_prompt, missing_prompt)
        unknown_gate = [(fid, s["id"], s.get("gate") or (s.get("verify") or {}).get("gate")) for fid, d in docs.items() for s in df.validate_steps(d)
                        if (s.get("gate") or (s.get("verify") or {}).get("gate")) and (s.get("gate") or s["verify"]["gate"]) not in arch_gates.GATES]
        check("every gate a step names exists in arch_gates", not unknown_gate, unknown_gate)
        check("a step id that would hide a context key is refused by the validator",
              any("hide the context key" in p for p in df.validate({"id": "x", "title": "x", "steps": [{"id": "scope", "kind": "code", "gate": "scope_files"}]})))
        check("an unknown gate, kind or on_fail is refused by the validator",
              len(df.validate({"id": "x", "title": "x", "steps": [{"id": "a", "kind": "nope"}, {"id": "b", "kind": "code", "gate": "nope"}, {"id": "c", "on_fail": "maybe"}]})) >= 3)

        # ---- the expression evaluator
        ctx = {"goal": "g", "params": {"repro": None}, "env": {"can_test": True}, "fix": {"ok": True, "tries": 2}, "cfg": {"behavior": {"evaluator": "final"}}}
        check("expressions read the context, including dotted names and missing ones",
              df.evaluate("fix.ok and env.can_test", ctx) is True and df.evaluate("params.repro is None", ctx) is True and df.evaluate("not missing.thing.deeper", ctx) is True
              and df.evaluate("fix.tries > 1 and cfg.behavior.evaluator != 'off'", ctx) is True and df.evaluate("missing == None", ctx) is True)
        refused = 0
        for bad_expr in ("__import__('os').system('x')", "open('x')", "fix._secret", "[].__class__", "exec('x')"):
            try:
                df.evaluate(bad_expr, ctx)
            except ValueError:
                refused += 1
        check("an expression that calls into Python, or reaches a private attribute, is refused (no eval of arbitrary code)", refused == 5, refused)

        # ---- templates
        r = df.render("a {goal} b {fix.ok} c {unknown} d {scope}", dict(ctx, scope=["x.py", "y.py"]))
        check("templates fill dotted context values, join a list of paths and leave an unknown placeholder alone", r == "a g b True c {unknown} d x.py, y.py", r)
        check("a number formatted as text does not lose the prompt", "0.5" in df.render("{baseline:.5f}", {"baseline": "0.5"}))

        # ---- a flow runs, with budgets, verdicts and the kill switch
        proj = os.path.join(tmp, "p")
        os.makedirs(proj)
        for rel, text in (("app.py", MOD), ("tests/test_app.py", TEST)):
            p = os.path.join(proj, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w", encoding="utf-8").write(text)

        def eng(flow="bugfix", goal="fix the bug", **over):
            c = core.load_config(proj)
            c["roles"] = {r_: "fake:7b" for r_ in arch_roles.ROLES}
            c["behavior"]["playwright"] = "off"
            c = core.deep_merge(c, over)
            return core.Engine(proj, flow, goal, c, review="auto", say=quiet)

        e = eng()
        e.push_budget("t", calls=1)
        e.call("generator", "hello")
        try:
            e.call("generator", "hello again")
            check("a call budget stops the next call", False)
        except core.BudgetExceeded as ex:
            check("a call budget stops the next call and names the level", "t" in str(ex) and "call" in str(ex), ex)
        e.pop_budget()
        e2 = eng()
        e2.push_budget("tokens", tokens=1)
        e2.meter["tokens_out"] = 100
        try:
            e2.call("generator", "x")
            check("a token budget stops the next call", False)
        except core.BudgetExceeded as ex:
            check("a token budget stops the next call", "tokens" in str(ex), ex)
        e3 = eng()
        os.makedirs(os.path.join(proj, ".arch"), exist_ok=True)
        open(os.path.join(proj, ".arch", "STOP"), "w").close()
        try:
            e3.call("generator", "x")
            check("the kill switch stops a run", False)
        except core.BudgetExceeded as ex:
            check("a STOP file stops the run at the next call, and says how to continue", "kill switch" in str(ex) and "remove it" in str(ex), ex)
        os.remove(os.path.join(proj, ".arch", "STOP"))
        check("the budget stack reports what is left at the tightest level", eng().budget_left() == {"tokens": None, "seconds": None, "calls": None})

        # ---- verdicts
        flow = df.DataFlow({"id": "t1", "title": "t", "steps": [{"id": "s", "kind": "code", "gate": "tests_pass"}], "verdict": {"pass_if": "s.ok"}})
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        e4 = core.Engine(empty, "t1", "g", core.deep_merge(core.load_config(empty), {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}}), review="auto", say=quiet)
        had = core.has_module
        core.has_module = lambda m: False if m == "pytest" else had(m)        # a machine with no test runner at all
        try:
            out = flow.run(e4, {})
        finally:
            core.has_module = had
        check("a check that cannot run makes the verdict 'not sure', never a pass", out["verdict"] == "not_sure" and not out["ok"], out)
        flow2 = df.DataFlow({"id": "t2", "title": "t", "steps": [{"id": "s", "kind": "code", "gate": "scope_files", "on_fail": "stop"}], "verdict": {"pass_if": "s.ok"}})
        out2 = flow2.run(core.Engine(empty, "t2", "g", core.deep_merge(core.load_config(empty), {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}}), review="auto", say=quiet), {})
        check("a step that fails with on_fail: stop ends the run as a failure, naming the step", out2["verdict"] == "fail" and "s" in (out2.get("why") or ""), out2)
        trace = []
        e5 = eng()
        e5._trace = lambda rec: trace.append(rec)
        df.DataFlow({"id": "t3", "title": "t", "steps": [{"id": "s", "kind": "code", "gate": "scope_files"}], "verdict": {"pass_if": "s.ok"}}).run(e5, {})
        kinds = {t["type"] for t in trace}
        check("the trace records a step, its check and one verdict for the run", {"step", "check", "verdict"} <= kinds, kinds)
        trace6 = []
        e6 = eng()
        e6._trace = lambda rec: trace6.append(rec)
        f6 = df.DataFlow({"id": "t6", "title": "t", "steps": [{"id": "a", "kind": "code", "gate": "scope_files", "title": "Step A"},
                                                             {"id": "b", "kind": "code", "gate": "scope_files", "title": "Step B"}],
                          "verdict": {"pass_if": "b.ok"}})
        f6.run(e6, {"resume": {"from": "old", "steps_done": ["a"], "step_outputs": {"a": {"ok": True, "marker": 7}}}})
        st6 = {r.get("step"): r for r in trace6 if r.get("type") == "step"}
        todo6 = {t["id"]: t["status"] for t in e6.state.get("todo") or []}
        check("resume: a step that passed before the interruption is not run again (its saved result is reused), the next step runs, "
              "and the live todo list shows both", st6.get("a", {}).get("status") == "resumed" and "kind" in st6.get("b", {})
              and todo6.get("a") == "done" and todo6.get("b") in ("done", "failed")
              and any(r.get("type") == "todo" for r in trace6), (st6, todo6))

        # ---- foreach
        seen = []
        fe = df.DataFlow({"id": "t4", "title": "t", "steps": [{"id": "loop", "kind": "foreach", "over": "plan_doc", "key": "sprints",
                                                               "fallback": [{"name": "only", "goal": "{goal}"}],
                                                               "steps": [{"id": "inner", "kind": "code", "gate": "scope_files"}]}], "verdict": {"pass_if": "loop.ok"}})
        e6 = eng()
        orig = arch_gates.GATES["scope_files"]
        arch_gates.GATES["scope_files"] = lambda e_, c_, s_, ch_: (seen.append(c_.get("item")), arch_gates.R(True))[1]
        step = fe.doc["steps"][0]
        ctx6 = {"goal": "g", "params": {}, "cfg": e6.cfg, "steps": {}}
        try:
            o = fe._run_foreach(e6, ctx6, step, "t4")
        finally:
            arch_gates.GATES["scope_files"] = orig
        check("foreach falls back to its own list when the planner gave nothing, and names each item", o["ok"] and o["n"] == 1 and (seen[0] or {}).get("name") == "only", (o, seen))
        seen2 = []
        calls = {"n": 0}

        def failing(e_, c_, s_, ch_):
            calls["n"] += 1
            seen2.append((c_.get("item") or {}).get("name"))
            return arch_gates.R(calls["n"] == 1)
        arch_gates.GATES["scope_files"] = failing
        f5 = df.DataFlow({"id": "t5", "title": "t", "steps": [{"id": "loop", "kind": "foreach", "over": "items", "stop_on_fail": True,
                                                               "steps": [{"id": "inner", "kind": "code", "gate": "scope_files"}]}], "verdict": {"pass_if": "loop.ok"}})
        e7 = eng()
        ctx7 = {"goal": "g", "params": {}, "cfg": e7.cfg, "steps": {}, "items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
        try:
            o2 = f5._run_foreach(e7, ctx7, f5.doc["steps"][0], "t5")
        finally:
            arch_gates.GATES["scope_files"] = orig
        check("foreach stops at the first item that fails its checks, instead of burning the budget on the rest", not o2["ok"] and seen2 == ["a", "b"] and o2["passed"] == 1, (o2, seen2))

        # ---- the same flow, both engines, same outcome
        import arch_flows
        legacy = {k: v for k, v in arch_flows.FLOWS.items()}
        data = df.load_all(say=quiet)
        same, diff = 0, []
        for fid in ("bugfix", "secure"):
            root_l, root_d = os.path.join(tmp, "l_" + fid), os.path.join(tmp, "d_" + fid)
            for root in (root_l, root_d):
                os.makedirs(os.path.join(root, "tests"), exist_ok=True)
                open(os.path.join(root, "app.py"), "w", encoding="utf-8").write(MOD)
                open(os.path.join(root, "tests", "test_app.py"), "w", encoding="utf-8").write(TEST)
            cfg_l = core.deep_merge(core.load_config(root_l), {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}, "behavior": {"playwright": "off"}})
            a = legacy[fid].run(core.Engine(root_l, fid, "fix the bug in app.py", cfg_l, review="auto", say=quiet), {})
            b = data[fid].run(core.Engine(root_d, fid, "fix the bug in app.py", core.deep_merge(cfg_l, {}), review="auto", say=quiet), {})
            if bool(a.get("ok")) == bool(b.get("ok")):
                same += 1
            else:
                diff.append((fid, a.get("ok"), b.get("ok")))
        check("a data flow reaches the same outcome as the Python class it replaces (bugfix, secure)", same == 2, diff)
        check("the data flow also reports a verdict the legacy class never had", "verdict" in b, b)

        # ---- tools and memory (phase 4)
        import arch_memory as mem
        import arch_tools as tools
        tp = os.path.join(tmp, "tp")
        os.makedirs(os.path.join(tp, "tests"), exist_ok=True)
        open(os.path.join(tp, "app.py"), "w", encoding="utf-8").write(MOD)
        open(os.path.join(tp, "tests", "test_app.py"), "w", encoding="utf-8").write(TEST)
        et = core.Engine(tp, "x", "g", core.deep_merge(core.load_config(tp), {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}}), review="auto", say=quiet)
        outs = {n_: tools.call(et, n_, a_)[0] for n_, a_ in (("ls", {}), ("grep", {"pattern": "def divide"}), ("read", {"path": "app.py", "start": 1, "end": 2}),
                                                             ("outline", {"path": "app.py"}), ("run_tests", {}))}
        check("the tools look around the project for real: ls lists it, grep finds a line with its number, read numbers the lines, outline names the functions, run_tests runs them",
              "app.py" in outs["ls"] and "app.py:5: def divide" in outs["grep"] and outs["read"].startswith("1: def add") and "divide" in outs["outline"] and "result: pass" in outs["run_tests"], outs)
        bad_tool, ok1 = tools.call(et, "nope", {})
        miss, ok2 = tools.call(et, "grep", {})
        esc, ok3 = tools.call(et, "read", {"path": "../../etc/passwd"})
        denied, ok4 = tools.call(et, "apply", {"blocks": "x"}, allowed=["ls"])
        check("an unknown tool, a missing argument, a path outside the project and a tool not allowed in this step are all refused with a reason, never a crash",
              not ok1 and not ok2 and "'..' is not allowed" in esc and not ok4 and "not available" in denied, (bad_tool, miss, esc, denied))
        check("ls understands the project root however it is written, and names what exists when a folder does not",
              tools.call(et, "ls", {"path": "."})[0] == tools.call(et, "ls", {})[0] and tools.call(et, "ls", {"path": "./"})[0] == tools.call(et, "ls", {})[0]
              and "tests/test_app.py" in tools.call(et, "ls", {"path": "tests"})[0] and "there is no 'nope' here" in tools.call(et, "ls", {"path": "nope"})[0]
              and "app.py" in tools.call(et, "ls", {"path": "nope"})[0], tools.call(et, "ls", {"path": "nope"})[0])
        check("a memory file may be named with or without .md, and nothing else is ever written",
              mem.path_for(tp, "gotchas") == mem.path_for(tp, "gotchas.md") == mem.path_for(tp, "Gotchas.md") and mem.path_for(tp, "nope") is None
              and mem.path_for(tp, "../../etc/passwd") is None and mem.path_for(tp, "machine/tuning") == mem.path_for(tp, "machine/tuning.md"))
        sch = tools.schema(["ls", "read"])
        check("the tool schema offers exactly the allowed tools plus a final answer", sch["properties"]["tool"]["enum"] == ["ls", "read"] and "final" in sch["properties"])
        check("a write tool is dropped when a step does not allow writes", "apply" in tools.TOOLS and tools.TOOLS["apply"]["writes"] and not tools.TOOLS["ls"]["writes"])

        r1 = mem.append(tp, "lessons.md", "tests import from the package root; add conftest.py first", run_id="r1")
        r2 = mem.append(tp, "lessons.md", "API_KEY = sk-live-0123456789abcdef")
        r3 = mem.append(tp, "../../etc/passwd", "anything")
        r4 = mem.append(tp, "lessons.md", "tests import from the package root; add conftest.py first")
        check("memory appends one dated line, refuses a credential, refuses a file that is not memory, and never stores the same line twice",
              r1["ok"] and not r2["ok"] and "credential" in r2["why"] and not r3["ok"] and r4.get("duplicate"), (r1, r2, r3, r4))
        text, used = mem.for_prompt(tp)
        check("what was learned goes into the prompt prefix and is reported back so the engine can confirm it", "conftest" in text and used == ["tests import from the package root; add conftest.py first"], (text[:80], used))
        mem.confirm(tp, ("lessons.md",), set(used))
        check("a lesson a passing run was shown earns a confirmation", mem.lines(tp, "lessons.md")[0]["confirmed"] == 1, mem.lines(tp, "lessons.md"))
        for _ in range(mem.PRUNE_AFTER):
            mem.mark_used(tp, ("gotchas.md",), {"never confirmed"})
        mem.append(tp, "gotchas.md", "a line that never helps anybody at all")
        rows = mem.lines(tp, "gotchas.md")
        for _ in range(mem.PRUNE_AFTER):
            mem.mark_used(tp, ("gotchas.md",), {rows[0]["text"]})
        mem.confirm(tp, ("gotchas.md",), set())
        check("a line that is shown again and again and never helps is dropped: memory shrinks when it is not earning its place", not mem.lines(tp, "gotchas.md"), mem.lines(tp, "gotchas.md"))
        ctxt = et.context("divide by zero", 3000)
        check("the engine puts that memory in front of the model", "what is known about this project" in ctxt and "conftest" in ctxt, ctxt[:200])

        # ---- the tool loop itself
        seq = {"n": 0}

        class Looper(arch_selftest.Fake):
            state = {"calls": [], "n": {}}

            def script(self, role, user, full, st):
                seq["n"] += 1
                if seq["n"] == 1:
                    return json.dumps({"tool": "ls", "args": {}})
                if seq["n"] == 2:
                    return "not json at all"
                if seq["n"] == 3:
                    return json.dumps({"tool": "read", "args": {"path": "app.py"}})
                if seq["n"] == 4:
                    return json.dumps({"tool": "read", "args": {"path": "app.py"}})      # the same call again
                return json.dumps({"final": "add() and divide() are in app.py; I could not tell who calls them."})
        loop_srv = ThreadingHTTPServer(("127.0.0.1", 0), Looper)
        threading.Thread(target=loop_srv.serve_forever, daemon=True).start()
        was = os.environ["OLLAMA_HOST"]
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{loop_srv.server_address[1]}"
        try:
            el = core.Engine(tp, "explore", "what does app.py do?", core.deep_merge(core.load_config(tp), {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}}), review="auto", say=quiet)
            tl = df.DataFlow({"id": "explore", "title": "t", "steps": [{"id": "look", "kind": "tool_loop", "role": "architect", "prompt": "explore",
                                                                        "tools": ["ls", "read"], "allow_writes": False, "max_steps": 6, "store_text": "answer"}],
                              "verdict": {"pass_if": "look.ok"}})
            ol = tl.run(el, {})
            recs = [json.loads(x) for x in open(os.path.join(el.run_dir, "trace.jsonl"), encoding="utf-8")]
        finally:
            loop_srv.shutdown()
            os.environ["OLLAMA_HOST"] = was
        check("the tool loop calls tools, survives an unusable reply, and ends with the model's own answer", ol["verdict"] == "pass" and ol["steps"]["look"]["ok"], ol)
        check("every tool call is traced with its arguments and result", [(t["tool"], t["ok"]) for t in recs if t["type"] == "tool"] == [("ls", True), ("read", True), ("read", True)], [t for t in recs if t["type"] == "tool"])
        check("the same call twice is answered with the same text plus a nudge to do something different, instead of looping",
              any("already made this exact call" in t["result"] for t in recs if t["type"] == "tool"), [t["result"][-60:] for t in recs if t["type"] == "tool"])

        # ---- registration honours the setting
        reg = {}
        check("flows.engine = off registers nothing; legacy fills ids with no class; data replaces them",
              df.register(dict(reg), "off") == [] and "bugfix" in df.register({}, "legacy") and "bugfix" in df.register({"bugfix": object()}, "data")
              and "bugfix" not in df.register({"bugfix": object()}, "legacy"))
    finally:
        fake.shutdown()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' FLOWS-AS-DATA CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' FLOWS-AS-DATA CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
