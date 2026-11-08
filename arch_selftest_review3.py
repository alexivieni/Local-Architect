"""Self-test for the 2026-11-07 architecture review, wave 3 (docs/ADR-001): three outcomes end to end and the YAML flows matching
their Python twins. A rollback never deletes a file it did not see (it used to delete everything past the first 400 files), "no test
ran" is never a pass for a bug fix, gates that write tests keep a file the runner did not collect, a refactor without a safety net is
not_sure, a check that could not run rolls its step back, not_sure travels up through sub-flows, a reproduction may only add files
under tests/, and the bug fix may not touch the tests that judge it. No model, no GPU, no network: replies are scripted."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archrev3_")
_KEYS = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST")
_OLD = {k: os.environ.get(k) for k in _KEYS}


def run():
    os.environ["ARCHITECT_HOME"] = os.path.join(_TMP, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "data")
    os.environ["ARCHITECT_NO_CACHE"] = os.environ["ARCHITECT_SELFTEST"] = "1"
    for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR"):
        os.makedirs(os.environ[k], exist_ok=True)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_roles
    old_home = core.HOME
    core.set_home(os.environ["ARCHITECT_HOME"])
    had = core.has_module
    try:
        def project(name, files):
            p = os.path.join(_TMP, name)
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(p, rel)) or p, exist_ok=True)
                with open(os.path.join(p, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            return p

        def engine(root, flow="bugfix"):
            cfg = core.deep_merge(core.load_config(root), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}})
            return core.Engine(root, flow, "check", cfg, review="auto", say=lambda *a: None)

        # ---- the rollback snapshot
        import arch_flows  # noqa: F401  (first: arch_flows_pro imports it back)
        import arch_flows_pro as pro
        big = project("big", {f"m{i:02d}.py": f"X = {i}\n" for i in range(12)})
        e = engine(big)
        said = []
        e.say = said.append
        cap = pro.capture(e, limit=3)                        # bytes for 3 files only, as if the project were larger than the memory budget
        with open(os.path.join(big, "new_file.py"), "w") as f:
            f.write("Y = 1\n")
        pro.restore(e, cap)
        left = sorted(x for x in os.listdir(big) if x.endswith(".py"))
        check("restore() deletes only files that did not exist at capture time, never the ones past the snapshot's limit",
              left == [f"m{i:02d}.py" for i in range(12)], left)
        cap = pro.capture(e, limit=3)
        with open(os.path.join(big, "m10.py"), "w") as f:
            f.write("X = 'changed'\n")
        said.clear()
        pro.restore(e, cap)
        check("a changed file whose bytes were not kept is reported, never guessed at", any("could not roll back" in s and "m10.py" in s for s in said), said)
        cap = pro.capture(e)
        with open(os.path.join(big, "m10.py"), "w") as f:
            f.write("X = 'again'\n")
        pro.restore(e, cap)
        check("with the default budget every file's bytes are kept and restored", open(os.path.join(big, "m10.py")).read() == "X = 'changed'\n")

        # ---- verify(): did a test run?
        import arch_flows
        import arch_gates
        bare = project("bare", {"app.py": "def add(a, b):\n    return a + b\n"})
        eb = engine(bare)
        core.has_module = lambda m: False if m == "pytest" else had(m)
        try:
            v = arch_flows.verify(eb, ["app.py"], run_tests=True)
            check("verify(): with no test runner the other checks can pass, and it says no test ran", v["ok"] and v["tests_ran"] is False, v)
            g = arch_gates.GATES["verify_files"](eb, {}, {"require_tests": True}, ["app.py"])
            check("verify_files with require_tests: no test ran -> could not run (None), never a pass", g["ok"] is None, g)
            g2 = arch_gates.GATES["verify_files"](eb, {}, {}, ["app.py"])
            check("verify_files without require_tests keeps the old meaning (a build with no tests yet)", g2["ok"] is True, g2)
            gp = arch_gates.GATES["characterization_pinned"](eb, {}, {}, [])
            check("a refactor with no test runner has no safety net: could not run", gp["ok"] is None, gp)
        finally:
            core.has_module = had
        notests = project("notests", {"app.py": "X = 1\n", "tests/helper.py": "# not a test module\n"})
        en = engine(notests, "test")
        if __import__("arch_flows_pro").can_test(en):
            gn = arch_gates.GATES["new_tests_pass"](en, {}, {}, ["tests/helper.py"])
            check("new_tests_pass: a file the runner did not collect is 'could not run' and is KEPT (it used to be deleted as failing)",
                  gn["ok"] is None and os.path.isfile(os.path.join(notests, "tests", "helper.py")), gn)
            gpn = arch_gates.GATES["characterization_pinned"](en, {}, {}, [])
            check("characterization_pinned: tests that ran nothing pin nothing (could not run, not pinned)", gpn["ok"] is None, gpn)
        else:
            check("new_tests_pass / characterization_pinned 'none' cases (needs a test runner here)", None is None)
        src = open(os.path.join(HERE, "arch_flows.py"), encoding="utf-8").read()
        check("the Python bug fix returns not_sure when its fix passed with no test run", 'return {"ok": False, "unsure": True, "why": "no test ran"' in src)
        check("the Python design flow fails on open issues and is not_sure on an unusable judge",
              'return {"ok": design_ok, "unsure": design_unsure' in src)

        # ---- the bug fix's protected files
        guarded = project("guarded", {"app.py": "X = 1\n", "tests/test_app.py": "def test_x():\n    assert True\n", "pytest.ini": "[pytest]\n"})
        eg = engine(guarded)
        ctx = {"goal": "g", "params": {}, "steps": {}}
        gpt = arch_gates.GATES["protect_tests"](eg, ctx, {}, [])
        check("protect_tests records every existing file and the files a fix may not touch",
              gpt["ok"] is True and "app.py" in ctx["existing_files"] and {"tests/test_app.py", "pytest.ini", "conftest.py"} <= set(ctx["kept_tests"]), ctx.get("kept_tests"))
        check("the Python and YAML bug fixes protect the same files (one definition)", "kept_tests = protected_test_files(files)" in src)
        import arch_dataflow as df
        doc = df.load_yaml_file(os.path.join(df.FLOWS_DIR, "bugfix.yaml"))
        steps = {s["id"]: s for s in doc["steps"]}
        check("bugfix.yaml: the reproduction may only ADD files, under tests/, and is rolled back when unusable",
              steps["repro"]["apply"].get("deny_ctx") == "existing_files" and steps["repro"].get("rollback_on_fail") and steps["guard"]["gate"] == "protect_tests")
        check("bugfix.yaml: the fix may not edit the tests or the runner settings, and needs a test to have run",
              steps["fix"]["apply"].get("deny_ctx") == "kept_tests" and steps["fix"]["verify"].get("require_tests"))

        # ---- the interpreter: expect_new_under, rollback on None, unsure through sub-flows
        flow = df.DataFlow({"id": "r3a", "title": "t", "steps": [
            {"id": "w", "kind": "generate", "role": "generator", "prompt_text": "x", "apply": {"expect_new_under": "tests/"}}],
            "verdict": {"pass_if": "w.ok"}})
        ew = engine(project("under", {"app.py": "X = 1\n"}))
        ew.call = lambda role, user, **k: {"text": "FILE: stray.py\n```python\nZ = 1\n```\n", "id": 0}
        ew.mark = lambda *a, **k: None
        out = flow._run_generate(ew, {"goal": "g", "params": {}, "steps": {}, "cfg": ew.cfg}, flow.doc["steps"][0], "r3a", prompt_text="write a test")
        check("a step that may write only under tests/ leaves no new file elsewhere (it used to stay on disk)",
              not os.path.exists(os.path.join(ew.root, "stray.py")) and not out.get("ok"), out)
        arch_gates.GATES["_r3_none"] = lambda e, ctx, step, changed: arch_gates.R(None, "could not run")
        try:
            er = engine(project("rollback", {"app.py": "X = 1\n"}))
            er.call = lambda role, user, **k: {"text": "FILE: app.py\n```python\nX = 2\n```\n", "id": 0}
            er.mark = lambda *a, **k: None
            st = {"id": "c", "kind": "generate", "role": "generator", "rollback_on_fail": True, "apply": {}, "verify": {"gate": "_r3_none"}}
            o2 = df.DataFlow({"id": "r3b", "title": "t", "steps": [st]})._run_generate(er, {"goal": "g", "params": {}, "steps": {}, "cfg": er.cfg}, st, "r3b", prompt_text="change it")
            check("a rollback step whose check could not run is rolled back (not_sure, and the change does not stay)",
                  o2.get("unsure") and open(os.path.join(er.root, "app.py")).read() == "X = 1\n", (o2, open(os.path.join(er.root, "app.py")).read()))
        finally:
            arch_gates.GATES.pop("_r3_none", None)

        class _Unsure:
            id = "r3_unsure"

            def run(self, e, params):
                return {"ok": False, "unsure": True}
        arch_flows.FLOWS["r3_unsure"] = _Unsure()
        try:
            fs = df.DataFlow({"id": "r3c", "title": "t", "steps": [{"id": "inner", "kind": "sub_flow", "flow": "r3_unsure", "on_fail": "continue"}],
                              "verdict": {"pass_if": "inner.ok"}})
            check("a sub-flow that ended not_sure makes the outer run not_sure (it used to become a plain fail)",
                  fs.run(engine(project("sub", {"app.py": "X = 1\n"})), {})["verdict"] == "not_sure")
        finally:
            arch_flows.FLOWS.pop("r3_unsure", None)
        dsrc = open(os.path.join(HERE, "arch_dataflow.py"), encoding="utf-8").read()
        check("foreach carries not_sure up from any item", '"unsure": any(d.get("unsure") for d in done)' in dsrc)
        check("a doc step whose check could not run is not_sure, never marked failed", 'e.mark(r["id"], None, g.get("text") or "")' in dsrc)
        ship = df.load_yaml_file(os.path.join(df.FLOWS_DIR, "ship.yaml"))
        check("ship.yaml passes only when build, tests, security and review pass (like the Python ShipFlow)",
              all(x in ship["verdict"]["pass_if"] for x in ("build.ok", "test.ok", "secure.ok", "review.ok")))
        ref = {s["id"]: s for s in df.load_yaml_file(os.path.join(df.FLOWS_DIR, "refactor.yaml"))["steps"]}
        check("refactor.yaml writes characterization tests where the change step may not edit them",
              ref["pin"]["apply"]["expect_new_under"] == "tests/characterization/" and ref["change"]["apply"]["deny"] == ["tests/characterization/"])
        bad = [d for d in (df.load_yaml_file(os.path.join(df.FLOWS_DIR, n)) for n in os.listdir(df.FLOWS_DIR) if n.endswith(".yaml")) if df.validate(d)]
        check("every shipped flow still validates", not bad, [d.get("id") for d in bad])
        import re as _re
        check("routing: 'find hidden bugs ... before we ship' is a bug hunt (hunt outranks ship; only 'find bugs' used to count)",
              _re.search(arch_flows.KEYWORDS["hunt"], "find hidden bugs in the scheduler before we ship") is not None
              and arch_flows.PRIORITY.index("hunt") < arch_flows.PRIORITY.index("ship"))
        check("routing: an ordinary bug report is still a bug fix, not a hunt", not _re.search(arch_flows.KEYWORDS["hunt"], "the login form crashes on submit"))

        # ---- a check that could not run (the optional browser check without playwright) is not a pass (2026-11-08)
        import json
        import arch_checks as ck
        s = ck.summarize([ck.R("a.py parses", True), ck.cannot_run("browser smoke test", "playwright is not installed")])
        check("summarize: a check that could not run is neither passed nor failed, and is listed as unsure",
              s["ok"] is True and s["passed"] == 1 and s["total"] == 2 and s["failures"] == "" and s["unsure"] and "browser smoke test" in s["unsure"][0], s)
        check("R() still makes a missing value a FAILURE (many checks pass None for 'not there'); only cannot_run() means could not run",
              ck.R("x", None)["ok"] is False and ck.cannot_run("x", "why")["ok"] is None)
        core.has_module = lambda m: False if m == "playwright" else had(m)
        pw = ck.playwright_smoke(project("pw", {"index.html": "<!doctype html><title>t</title>"}), "index.html")
        check("the browser check without playwright reports COULD NOT RUN, not passed", len(pw) == 1 and pw[0]["ok"] is None and "could not run" in pw[0]["detail"], pw)
        web = project("web", {"index.html": "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                                            "<title>t</title><style>:focus-visible{outline:2px solid #000}main{display:flex}"
                                            "@media (max-width:600px){main{padding:0}}</style></head><body><main><h1>t</h1></main></body></html>\n"})
        ew = engine(web, "build")
        ew.cfg["behavior"]["playwright"] = "always"
        v = arch_flows.verify(ew, ["index.html"], run_tests=False)
        check("verify: nothing failed but the browser check could not run -> ok, with the check named under unsure",
              v["ok"] is True and any("browser smoke test" in u for u in v.get("unsure") or []) and ew.unsure_checks, (v.get("ok"), v.get("unsure"), v["text"][:200]))
        import arch_gates
        g = arch_gates.GATES["verify_files"](ew, {}, {"run_tests": False}, ["index.html"])
        check("the data engine's verify_files gate answers None (could not run) in the same case, so the YAML run ends not_sure too",
              g["ok"] is None and "browser smoke test" in g["text"], g)
        bdir = os.path.join(ew.run_dir, "backup")
        os.makedirs(bdir, exist_ok=True)
        with open(os.path.join(bdir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<p>old</p>\n")
        kept = arch_flows._not_counting_old(ew, [ck.cannot_run("index.html: opens in a browser", "no browser")])
        check("an old-findings filter never turns 'could not run' into a pass", kept[0]["ok"] is None, kept)

        class _WebBuild:
            id = "r3_web"
            steps = []

            def run(self, e, params):
                return {"ok": arch_flows.verify(e, ["index.html"], run_tests=False)["ok"]}
        arch_flows.FLOWS["r3_web"] = _WebBuild()
        try:
            ew2 = engine(web, "build")
            ew2.cfg["behavior"]["playwright"] = "always"
            arch_flows.run_flow(ew2, "r3_web", {})
            recs = [json.loads(ln) for ln in open(os.path.join(ew2.run_dir, "trace.jsonl"), encoding="utf-8") if '"verdict"' in ln]
            verdict = [r for r in recs if r.get("type") == "verdict"][-1]
            check("a run that passed every check that RAN, with one that could not run, ends not_sure (and says which)",
                  verdict["verdict"] == "not_sure" and "browser smoke test" in verdict.get("why", ""), verdict)
            ew3 = engine(web, "build")
            ew3.cfg["behavior"]["playwright"] = "off"
            arch_flows.run_flow(ew3, "r3_web", {})
            recs3 = [json.loads(ln) for ln in open(os.path.join(ew3.run_dir, "trace.jsonl"), encoding="utf-8") if '"verdict"' in ln]
            check("with the browser check switched off (behavior.playwright = off) the same run passes",
                  [r for r in recs3 if r.get("type") == "verdict"][-1]["verdict"] == "pass")
        finally:
            arch_flows.FLOWS.pop("r3_web", None)
            core.has_module = had
    finally:
        core.has_module = had
        core.set_home(old_home)
        for k, v in _OLD.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' REVIEW3 CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' REVIEW3 CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
