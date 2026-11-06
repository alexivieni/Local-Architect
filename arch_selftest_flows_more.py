"""Self-test for the newer flows -- harden, types, a11y, document, migrate, deps -- and the scanners behind them.

Each flow is run for real through the interpreter and the engine, against a small project with a known defect, twice: once with a
scripted model that fixes it (the flow must pass) and once with one that hands back the code unchanged (the flow must NOT pass). That is
the property that matters: the code check, not the model's say-so, decides.

No real model, no GPU, no network.
"""
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

T_IMPORT = "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"

CASES = {
    "harden": {
        "goal": "improve error handling in app.py, there is a bare except",
        "files": {"app.py": "def load(path):\n    try:\n        with open(path, encoding='utf-8') as f:\n            return f.read()\n    except:\n        pass\n",
                  "tests/test_app.py": T_IMPORT + "from app import load\n\n\ndef test_missing_file_gives_none(tmp_path):\n    assert load(str(tmp_path / 'nope.txt')) is None\n\n\n"
                                                  "def test_reads(tmp_path):\n    p = tmp_path / 'a.txt'\n    p.write_text('hi')\n    assert load(str(p)) == 'hi'\n"},
        "fix": "FILE: app.py\n```python\ndef load(path):\n    try:\n        with open(path, encoding='utf-8') as f:\n            return f.read()\n    except FileNotFoundError:\n        return None\n```\n",
        "role": "reliability_engineer",
    },
    "types": {
        "goal": "add type hints to calc.py",
        "files": {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef half(x):\n    return x / 2\n",
                  "tests/test_calc.py": T_IMPORT + "from calc import add, half\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_half():\n    assert half(3) == 1.5\n"},
        "fix": "FILE: calc.py\n```python\ndef add(a: float, b: float) -> float:\n    return a + b\n\n\ndef half(x: float) -> float:\n    return x / 2\n```\n",
        "role": "type_engineer",
    },
    "a11y": {
        "goal": "make index.html accessible to screen reader users",
        "files": {"index.html": "<!doctype html>\n<html>\n<head><meta charset=\"utf-8\"><title>Sign up</title></head>\n<body>\n<h1>Sign up</h1>\n"
                                "<img src=\"logo.png\">\n<form><input id=\"email\" type=\"email\"><button type=\"submit\">Join</button></form>\n</body>\n</html>\n"},
        "fix": "FILE: index.html\n```html\n<!doctype html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\"><title>Sign up</title></head>\n<body>\n<h1>Sign up</h1>\n"
               "<img src=\"logo.png\" alt=\"Company logo\">\n<form><label for=\"email\">Email</label><input id=\"email\" type=\"email\"><button type=\"submit\">Join</button></form>\n</body>\n</html>\n```\n",
        "role": "accessibility_engineer",
    },
    "document": {
        "goal": "write docstrings for every function in shapes.py",
        "files": {"shapes.py": "def area(w, h):\n    return w * h\n\n\nclass Box:\n    def __init__(self, w):\n        self.w = w\n",
                  "tests/test_shapes.py": T_IMPORT + "from shapes import Box, area\n\n\ndef test_area():\n    assert area(2, 3) == 6\n\n\ndef test_box():\n    assert Box(4).w == 4\n"},
        "fix": "FILE: shapes.py\n```python\n\"\"\"Simple shape helpers.\"\"\"\n\n\ndef area(w, h):\n    \"\"\"Return the area of a w-by-h rectangle.\"\"\"\n    return w * h\n\n\n"
               "class Box:\n    \"\"\"A box with a width.\"\"\"\n\n    def __init__(self, w):\n        self.w = w\n```\n",
        "role": "docs_writer",
    },
    "migrate": {
        "goal": "replace `os.system` with subprocess.run in tools.py",
        "files": {"tools.py": "import os\n\n\ndef greet(name):\n    return 'hello ' + name\n\n\ndef ping():\n    return os.system('echo ping')\n",
                  "tests/test_tools.py": T_IMPORT + "from tools import greet\n\n\ndef test_greet():\n    assert greet('ada') == 'hello ada'\n"},
        "fix": "FILE: tools.py\n```python\nimport subprocess\n\n\ndef greet(name):\n    return 'hello ' + name\n\n\ndef ping():\n    return subprocess.run(['echo', 'ping'], check=False).returncode\n```\n",
        "role": "migration_engineer",
    },
}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== the newer flows: harden, types, a11y, document, migrate, deps ==")
    import arch_core as core
    import arch_dataflow as df
    import arch_gates as G
    import arch_gates_more as GM
    import arch_roles
    import arch_selftest

    home = tempfile.mkdtemp(prefix="archfm_")
    old_home = core.HOME
    core.set_home(home)                     # this suite must never read the install's real settings (its tiers point at a live server)
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    mode = {"fix": True, "current": ""}

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            for fid, c in CASES.items():
                if role == c["role"]:
                    if mode["fix"]:
                        return c["fix"]
                    name, text = next((k, v) for k, v in c["files"].items() if not k.startswith("tests/"))
                    return f"FILE: {name}\n```\n{text}```\n"            # the same code back: nothing actually fixed
            if role == "dependency_engineer":
                return "## Missing\nNothing missing.\n\n## Unused\nNone declared.\n"
            return "ok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    flows = df.load_all(say=lambda _m: None)
    try:
        # ---- the scanners, on their own
        _scanner_checks(check, GM, home)
        _settings_are_real(check, home)
        _interface_contract(check, home)
        _live_failure_fixes(check, home)
        _leftovers(check, home)

        # ---- every new flow exists, validates and has a gate pair registered
        for fid in ("harden", "types", "a11y", "document", "migrate", "deps"):
            check(f"the {fid} flow loads from its YAML file", fid in flows, sorted(flows))
        for k in ("harden", "types", "a11y", "docs", "migrate"):
            check(f"the {k} check has a before and an after gate", f"{k}_scan" in G.GATES and f"{k}_fewer" in G.GATES)

        # ---- each flow end to end, fixed and not fixed
        for fid, case in CASES.items():
            for fixed in (True, False):
                mode["fix"] = fixed
                proj = os.path.join(home, f"{fid}-{'fixed' if fixed else 'unfixed'}")
                for rel, text in case["files"].items():
                    p = os.path.join(proj, rel.replace("/", os.sep))
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(text)
                with open(os.path.join(proj, "conftest.py"), "w", encoding="utf-8") as f:
                    f.write("")
                cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                               "behavior": {"battery_saver": "off", "recheck_before_retry": False},
                                                               "limits": {"max_revisions": 1}})
                e = core.Engine(proj, fid, case["goal"], cfg, review="auto", say=lambda *_a, **_k: None)
                out = flows[fid].run(e, {})
                if fixed:
                    check(f"{fid}: when the model really fixes it, the flow passes on the code check", out["verdict"] == "pass", (out["verdict"], out.get("why")))
                    doc = os.path.join(proj, "docs")
                    check(f"{fid}: and it writes its report", os.path.isdir(doc) and any(n.endswith(".md") for n in os.listdir(doc)),
                          os.listdir(doc) if os.path.isdir(doc) else None)
                else:
                    check(f"{fid}: when the model hands back the same code, the flow does NOT pass", out["verdict"] != "pass", out["verdict"])

        # ---- deps: a report flow that changes nothing
        proj = os.path.join(home, "deps")
        os.makedirs(proj)
        with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as f:
            f.write("import json\n\n\ndef dump(x):\n    return json.dumps(x)\n")
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "behavior": {"battery_saver": "off"}})
        e = core.Engine(proj, "deps", "audit the dependencies", cfg, review="auto", say=lambda *_a, **_k: None)
        out = flows["deps"].run(e, {})
        rep = os.path.join(proj, "docs", "dependencies.md")
        check("deps: every import being installed passes, and the review is written down",
              out["verdict"] == "pass" and os.path.isfile(rep) and "Missing" in open(rep, encoding="utf-8").read(), (out["verdict"], out.get("why")))
        check("deps: it changes no source file", open(os.path.join(proj, "app.py"), encoding="utf-8").read().startswith("import json"))
        with open(os.path.join(proj, "app.py"), "a", encoding="utf-8") as f:
            f.write("import definitely_not_installed_pkg_xyz\n")
        e2 = core.Engine(proj, "deps", "audit the dependencies", cfg, review="auto", say=lambda *_a, **_k: None)
        out2 = flows["deps"].run(e2, {})
        check("deps: an import that is not installed is caught by code, and the flow does not pass", out2["verdict"] != "pass", out2["verdict"])
    finally:
        srv.shutdown()
        core.set_home(old_home)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' NEWER-FLOW CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' NEWER-FLOW CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _leftovers(check, home):
    """plan_override, call sections, recipe id, overlapped checks, template contract merge."""
    import json
    import arch_core as core
    import arch_flows as F
    proj = tempfile.mkdtemp(prefix="arch_lo_")
    try:
        e = core.Engine(proj, "build", "g", review="auto", say=lambda *a, **k: None)
        e.todo_set([{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
        check("no plan_override.json: nothing skipped, nothing added", e.plan_override() == {"skipped": set(), "added": []})
        with open(os.path.join(e.run_dir, "plan_override.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "a", "status": "pending"}, {"id": "b", "status": "skipped"}, {"id": "c", "title": "New", "status": "pending"}]}, f)
        o = e.plan_override()
        st = {it["id"]: it["status"] for it in e.state["todo"]}
        check("plan_override skips the skipped item and appends the added one", o["skipped"] == {"b"} and o["added"] == ["c"] and st == {"a": "pending", "b": "skipped", "c": "pending"}, (o, st))
        check("plan_override is idempotent (nothing added twice)", e.plan_override()["added"] == [] and len(e.state["todo"]) == 3)
        with open(os.path.join(e.run_dir, "plan_override.json"), "w") as f:
            f.write("{bad")
        check("a broken plan_override.json is ignored", e.plan_override()["skipped"] == set())

        sec = e._ctx_sections([{"role": "system", "content": "x" * 400}, {"role": "user", "content": "=== app.py ===\n" + "y" * 800 + "\n## Memory\n" + "m" * 400 + "\nfix it"}], 4.0)
        check("call sections: system/project_files/memory/conversation in tokens, labelled est",
              sec and sec["est"] and 95 <= sec["system_prompt"] <= 110 and sec["project_files"] >= 200 and sec["memory"] >= 100 and set(sec) >= {"skills", "tools", "conversation"}, sec)

        e.state["recipe"] = None
        F.core = core
        # recipe id reaches run.json and the verdict record
        class Fl:
            id, steps = "x", None
            def run(self, e_, p):
                return {"ok": True}
        F.FLOWS["_lo"] = Fl()
        try:
            F.run_flow(e, "_lo", {"recipe": "fix-bug"})
        finally:
            F.FLOWS.pop("_lo", None)
        tr = [json.loads(l) for l in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8") if l.strip()]
        vd = [r for r in tr if r.get("type") == "verdict"]
        import arch_progress as _ap

        class Fl2:
            id, steps = "x2", None

            def run(self, e_, p):
                co = _ap.Coach(e_, "generator")
                for i in range(2):
                    co.observe("r%d" % i, "FAILED tests/test_wc.py::test_wc - assert 3 == 4\nE   assert 3 == 4")
                F.same_failure_stop(e_, co)
                return {"ok": False, "error": "still failing"}
        F.FLOWS["_sf"] = Fl2()
        try:
            F.run_flow(e, "_sf", {})
        finally:
            F.FLOWS.pop("_sf", None)
        tr = [json.loads(l) for l in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8") if l.strip()]
        vd2 = [r for r in tr if r.get("type") == "verdict" and r.get("flow") == "_sf"]
        check("same_failure_stop writes its reason and the repeated failure line to the verdict record (and the report)",
              vd2 and (vd2[-1].get("same_failure") or {}).get("line", "").startswith("FAILED tests/test_wc.py") and "identical failure" in vd2[-1]["same_failure"]["reason"]
              and any("repeated failure: FAILED tests/test_wc.py" in str(x) for x in e.report_lines), (vd2, e.report_lines[-3:], getattr(e, "same_failure", None)))
        check("a recipe id is written to run.json and the verdict record", e.state.get("recipe") == "fix-bug" and vd and vd[-1].get("recipe") == "fix-bug", (e.state.get("recipe"), vd))

        # overlapped checks: identical verdict and results
        with open(os.path.join(proj, "m.py"), "w", encoding="utf-8") as f:
            f.write("import nosuchmodule_xyz\n\n\ndef f():\n    pass\n")
        e.changed_files = ["m.py"]
        outs = []
        for mode in ("on", "off"):
            e.cfg["behavior"]["overlap_checks"] = mode
            e._test_cache = {}
            r = F.verify(e, ["m.py"], run_tests=True)
            outs.append((r["ok"], r["text"], [(x["name"], x["ok"]) for x in r["results"]]))
        check("verify gives identical verdicts with overlap_checks on and off", outs[0] == outs[1], outs)

        # template contract merged into the interface contract
        with open(os.path.join(proj, "CONTRACT.json"), "w", encoding="utf-8") as f:
            json.dump({"modules": [{"module": "pkgx", "public": [{"name": "run_it", "kind": "function", "signature": "(a, b) -> int"}]}]}, f)
        c = F.interface_contract("make it faster", proj)
        c2 = F.interface_contract("Use `from pkgx import run_it`", proj)
        check("CONTRACT.json joins the interface contract (request entries not repeated)",
              ("pkgx", "run_it") in c["imports"] and c2["imports"].count(("pkgx", "run_it")) == 1 and F.interface_contract("make it faster")["imports"] == [], (c, c2))
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    _small_requests(check)
    _baseline_tests(check)
    import arch_plugins
    import arch_commands
    d = tempfile.mkdtemp(prefix="arch_pl_")
    try:
        os.makedirs(os.path.join(d, "agents"))
        with open(os.path.join(d, "agents", "scout.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: scout\ndescription: looks around\ntools: Read, Grep\n---\nYou read code and report.\n")
        c = arch_plugins.contents(d)
        check("a plugin's agents/*.md agent is validated and listed", "scout" in c["agents"] and not c["problems"], c)
        r = arch_commands.parse("---\ndescription: d\nargs: [what to cover]\n---\nDo standup for {args}\n", "x/standup.md", "shipped")
        r2 = arch_commands.parse("---\ndescription: d\nargs: a file\n---\nFix {args}\n", "x/fix.md", "shipped")
        check("an args hint in [brackets] is optional; a plain one still needs args", r and not r["needs_args"] and r2 and r2["needs_args"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _baseline_tests(check):
    """A project with one test that already fails: judged against the baseline, and a new-symbol request skips the planner."""
    import arch_core as core
    import arch_flows as F
    proj = tempfile.mkdtemp(prefix="arch_bl_")
    try:
        os.makedirs(os.path.join(proj, "tests"))
        with open(os.path.join(proj, "kit.py"), "w", encoding="utf-8") as f:
            f.write("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        with open(os.path.join(proj, "tests", "test_old.py"), "w", encoding="utf-8") as f:
            f.write("from kit import one, two\n\n\ndef test_one():\n    assert one() == 1\n\n\ndef test_bad():\n    assert two() == 3\n")
        e = core.Engine(proj, "build", "g", review="auto", say=lambda *a, **k: None, persist=False)
        e.cfg["behavior"]["fast_tests"] = "off"
        e.cfg["tests"]["command"] = ""
        base = core.test_baseline(proj, e.cfg)
        check("baseline records the one failing test", base == {"tests/test_old.py::test_bad"}, base)
        e._test_baseline = base

        def ver():
            e._test_cache = {}
            return F.verify(e, ["kit.py"], run_tests=True)

        def tests_ok(v):
            return all(r["ok"] for r in v["results"] if r["name"] == "project tests pass") and any(r["name"] == "project tests pass" for r in v["results"])
        e.changed_files = ["kit.py"]
        with open(os.path.join(proj, "kit.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef three():\n    return 3\n")
        check("an unrelated build passes beside the old failure", tests_ok(ver()))
        with open(os.path.join(proj, "tests", "test_new.py"), "w", encoding="utf-8") as f:
            f.write("from kit import three\n\n\ndef test_three():\n    assert three() == 3\n")
        e.created = ["tests/test_new.py"]
        check("...and a new passing test too", tests_ok(ver()))
        with open(os.path.join(proj, "tests", "test_new.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef test_three_bad():\n    assert three() == 4\n")
        check("a new failing test fails", not tests_ok(ver()))
        with open(os.path.join(proj, "tests", "test_new.py"), "w", encoding="utf-8") as f:
            f.write("from kit import three\n\n\ndef test_three():\n    assert three() == 3\n")
        with open(os.path.join(proj, "kit.py"), "w", encoding="utf-8") as f:
            f.write("def one():\n    return 0\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n")
        check("breaking a test that passed before fails", not tests_ok(ver()))
        with open(os.path.join(proj, "kit.py"), "w", encoding="utf-8") as f:
            f.write("def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n")
        check("restored: passes again", tests_ok(ver()))
        e.changed_files = ["kit.py", "tests/test_old.py"]
        check("editing the file with the old failure makes it the run's own", not tests_ok(ver()))
        e.changed_files = ["kit.py"]
        e._test_baseline = None
        check("no baseline: today's behaviour (the old failure fails the run)", not tests_ok(ver()))
        e.cfg["tests"]["command"] = "py -c pass"
        check("a custom test command: no baseline taken", core.test_baseline(proj, e.cfg) is None)

        e.cfg["behavior"]["skip_planner_small"] = "on"
        ok = lambda g: F.single_file_change(e, g)
        check("one new function in a one-module project skips the planner",
              ok("add `word_count(text)` to kit") and ok("add word_count with tests") and ok("implement a function slug(text) returning a str")
              and ok("add a class Counter"))
        check("...two symbols, a new project, or no symbol keep the planner",
              not ok("add `a(x)` and `b(x)`") and not ok("build a new app with `a(x)`") and not ok("add tests") and not ok("add logging"))
        with open(os.path.join(proj, "other.py"), "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        check("...a project with two source modules keeps the planner", not ok("add `word_count(text)`"))
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def _small_requests(check):
    """skip_planner_small, related-tests-first-then-full, same_failure_stop."""
    import arch_core as core
    import arch_flows as F
    proj = tempfile.mkdtemp(prefix="arch_sm_")
    try:
        with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        e = core.Engine(proj, "build", "g", review="auto", say=lambda *a, **k: None, persist=False)
        check("one existing file + a change: planner skipped", F.single_file_change(e, "fix the off by one in app.py") is True)
        check("...a file that does not exist, two files, or no change asked: planner kept",
              not F.single_file_change(e, "fix the bug in missing.py") and not F.single_file_change(e, "fix app.py and util.py")
              and not F.single_file_change(e, "what is app.py") and not F.single_file_change(e, "build a new app, add app.py"))
        e.cfg["behavior"]["skip_planner_small"] = "off"
        check("skip_planner_small = off keeps the planner", not F.single_file_change(e, "fix the off by one in app.py"))

        import arch_progress

        class C:
            fails = []
        c = C()
        e.cfg["behavior"]["same_failure_stop"] = 2
        c.fails = [arch_progress.fp("E assert 1 == 2"), arch_progress.fp("E assert 1 == 2")]
        check("same_failure_stop: the identical failure twice stops the loop", F.same_failure_stop(e, c) is True)
        c.fails = [arch_progress.fp("E assert 1 == 2"), arch_progress.fp("NameError: x")]
        check("...a different failure does not", F.same_failure_stop(e, c) is False)
        e.cfg["behavior"]["same_failure_stop"] = 0
        c.fails = [arch_progress.fp("a")] * 3
        check("...and 0 turns it off", F.same_failure_stop(e, c) is False)

        # a copy of an already-failing test is excused; a changed copy and a missing file are not
        _cd = tempfile.mkdtemp()
        os.makedirs(os.path.join(_cd, "tests"))
        _src = "def test_a():\n    assert f(1) == 2\n"
        open(os.path.join(_cd, "test_k.py"), "w").write(_src)
        open(os.path.join(_cd, "tests", "test_k_new.py"), "w").write(_src)
        open(os.path.join(_cd, "tests", "test_k_b.py"), "w").write(_src.replace("2", "3"))
        _base = {"test_k.py::test_a"}
        check("an exact copy of a test that already failed is not the run's breakage",
              F._copied_failure(_cd, "tests/test_k_new.py::test_a", _base) is True)
        check("a copy with a changed body, or a missing file, still counts against the run",
              F._copied_failure(_cd, "tests/test_k_b.py::test_a", _base) is False and F._copied_failure(_cd, "tests/gone.py::test_a", _base) is False)
        shutil.rmtree(_cd, ignore_errors=True)

        # test-file name clash: a file the run CREATED is renamed (and the run's lists follow); a pre-existing file it only edited is never moved
        class _CE:
            def __init__(s, root, created, changed):
                s.root, s.cfg, s.created, s.changed_files = root, dict(core.DEFAULTS), list(created), list(changed)
            def put_file(s, rel, text, **k):
                open(os.path.join(s.root, rel), "w").write(text)
                return {"status": "applied"}
            def report(s, x): pass
            def say(s, x): pass
        def _clash(created, changed):
            d = tempfile.mkdtemp()
            os.makedirs(os.path.join(d, "tests"))
            open(os.path.join(d, "test_k.py"), "w").write("def test_a(): pass\n")
            open(os.path.join(d, "tests", "test_k.py"), "w").write("def test_b(): pass\n")
            ce = _CE(d, created, changed)
            out = F.fix_test_name_collisions(ce)
            left = sorted(os.listdir(os.path.join(d, "tests")))
            shutil.rmtree(d, ignore_errors=True)
            return out, left, ce.created
        _o1, _l1, _c1 = _clash(["tests/test_k.py"], [])
        check("a clashing test file the run created is renamed to _new and the run's file list follows",
              _o1 == [("tests/test_k.py", "tests/test_k_new.py")] and _l1 == ["test_k_new.py"] and _c1 == ["tests/test_k_new.py"], (_o1, _l1, _c1))
        _o2, _l2, _ = _clash([], ["tests/test_k.py"])
        check("a pre-existing test file the run only edited is never renamed or deleted", _o2 == [] and _l2 == ["test_k.py"], (_o2, _l2))

        # related tests first, then the whole suite once before the verdict
        os.makedirs(os.path.join(proj, "tests"), exist_ok=True)
        with open(os.path.join(proj, "tests", "test_app.py"), "w", encoding="utf-8") as f:
            f.write("from app import f\n\n\ndef test_f():\n    assert f() == 1\n")
        e.cfg["behavior"]["fast_tests"] = "on"
        e.changed_files = ["app.py"]
        calls = []
        real = core.run_tests
        core.run_tests = lambda root, cfg, targets=None: (calls.append(targets), ("pass", ""))[1]
        try:
            e._test_cache = {}
            F.verify(e, ["app.py"], run_tests=True)
        finally:
            core.run_tests = real
        check("verify runs the related tests first, then the whole suite once", len(calls) == 2 and calls[0] and calls[1] is None, calls)
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def _interface_contract(check, home):
    """The two shapes of failure the live benchmark actually produced: a constructor missing an argument the request named, and a
    function the request imports from a package that never exported it."""
    import arch_flows as F

    class Eng:
        pass

    goal = ("Public interface, exactly: `from shortener import Shortener`; `s = Shortener(path)` stores data; "
            "`from shortener import resolve_all`; run it as `python -m shortener --db X list`.")
    c = F.interface_contract(goal)
    check("the interface is read from what the request puts in backticks",
          ("shortener", "Shortener") in c["imports"] and ("Shortener", 1, [], "s = Shortener(path)") in c["calls"] and c["mains"] == ["shortener"], c)
    check("a request that names no interface asks for no check", F.interface_contract("make it faster") == {"imports": [], "calls": [], "mains": [], "methods": []})
    d = os.path.join(home, "contract")
    os.makedirs(os.path.join(d, "shortener"))
    with open(os.path.join(d, "shortener", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("class Shortener:\n    def __init__(self):\n        pass\n")
    e = Eng()
    e.root = d
    ok, text = F.contract_check(e, goal)
    check("a constructor that does not take the argument the request named is caught, with the reason",
          not ok and "Shortener(path)" in text and "resolve_all" in text and "python -m shortener" in text, text)
    with open(os.path.join(d, "shortener", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("class Shortener:\n    def __init__(self, path):\n        self.path = path\n\n\ndef resolve_all():\n    return []\n")
    with open(os.path.join(d, "shortener", "__main__.py"), "w", encoding="utf-8") as f:
        f.write("print('ok')\n")
    check("and code that matches the interface passes", F.contract_check(e, goal) == (True, ""), F.contract_check(e, goal))
    check("the check never runs the code it inspects, only imports and binds it",
          "bind(" in open(F.__file__, encoding="utf-8").read())


def _settings_are_real(check, home):
    """Every option added for depth must change behaviour. An option that does nothing is worse than no option."""
    import arch_core as core
    import arch_dataflow as df
    import arch_eval
    import arch_memory
    import arch_skills
    import model_hub as mh

    b = {"calls": 10, "tokens": 1000, "seconds": 60, "heavy_calls": 1}
    s2 = df._scaled(b, {"flows": {"budget_scale": 2}})
    check("budget scale 2 doubles a flow's calls, tokens and seconds", s2["calls"] == 20 and s2["tokens"] == 2000 and s2["seconds"] == 120, s2)
    check("but never the number of heavy-model calls, which is a separate, deliberate limit", s2["heavy_calls"] == 1)
    check("budget scale 1 leaves the budget exactly as written", df._scaled(b, {}) == b)

    base = {"summary": {"flows": {"build": {"pass_rate": 80.0, "tokens_p50": 1000, "secs_p95": 60}}}}
    cand = {"summary": {"flows": {"build": {"pass_rate": 77.0, "tokens_p50": 1000, "secs_p95": 60}}}}
    check("by default a 3-point drop is within the regression allowance", arch_eval.compare(base, cand)["ok"] is True)
    _real_home = core.HOME
    core.set_home(os.path.join(home, "cfg_home"))          # NEVER the install's real settings: the earlier set_home was undone in the finally above
    try:
        mh.CONFIG_FILE = core.CONFIG_FILE
        mh.write_config({"eval": {"pass_rate_drop": 2.0}})
        check("setting the allowance to 2 points makes the same drop a regression", arch_eval.compare(base, cand)["ok"] is False)
        mh.write_config({})
    finally:
        core.set_home(_real_home)

    root = os.path.join(home, "memproj")
    os.makedirs(root)
    for i in range(30):
        arch_memory.append(root, "lessons.md", f"lesson number {i} about this project", max_lines=12)
    check("the memory file is cut to the configured length", len(arch_memory.lines(root, "lessons.md")) <= 12, len(arch_memory.lines(root, "lessons.md")))
    text, used = arch_memory.for_prompt(root, 5000, top=3)
    check("and only the configured number of lessons rides along in a prompt", len(used) == 3, len(used))

    goal = "fix the crash"
    loose = [x["name"] for x in arch_skills.relevant(goal, min_score=0.5)]
    strict = [x["name"] for x in arch_skills.relevant(goal, min_score=9)]
    check("lowering the skill relevance bar loads skills for a vague request; raising it loads none", loose and strict == [], (loose, strict))


def _scanner_checks(check, GM, home):
    class E:
        def __init__(self, root):
            self.root, self.cfg = root, {"files": {"extensions": [".py", ".html"], "ignored_dirs": [], "protected_dirs": []}}

    root = os.path.join(home, "scan")
    os.makedirs(root)
    files = {
        "bad.py": "import subprocess, urllib.request\n\n\ndef a():\n    try:\n        x = 1\n    except:\n        pass\n\n\n"
                  "def b(u):\n    return urllib.request.urlopen(u)\n\n\ndef c(cmd):\n    subprocess.run(cmd, shell=True)\n\n\n"
                  "def d(v):\n    assert v > 0\n    f = open('x')\n    return f\n",
        "good.py": "def a(path: str) -> str:\n    \"\"\"Read a file.\"\"\"\n    with open(path, encoding='utf-8') as f:\n        return f.read()\n",
        "page.html": "<html><body><h1>T</h1><h3>skip</h3><img src=a.png><input type=text><div onclick=\"go()\">x</div><button></button>"
                     "<a href=\"/\"></a><span tabindex=\"3\">y</span></body></html>",
        "ok.html": "<!doctype html><html lang=\"en\"><head><title>Fine</title></head><body><h1>A</h1><h2>B</h2>"
                   "<img src=a.png alt=\"\"><label for=n>Name</label><input id=n><button>Go</button></body></html>",
    }
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    e = E(root)
    rules = sorted({x["rule"] for x in GM.scan_robustness(e, ["bad.py"])})
    check("the robustness scan finds a bare except, a swallowed error, a missing timeout, shell=True, assert-as-validation and an unclosed file",
          rules == sorted(["bare-except", "swallowed-error", "no-timeout", "shell-true", "assert-for-validation", "open-without-with"]), rules)
    check("and finds nothing in careful code", GM.scan_robustness(e, ["good.py"]) == [])
    check("the typing scan names every public function with a missing hint, and passes a fully hinted one",
          len(GM.scan_typing(e, ["bad.py"])) == 4 and GM.scan_typing(e, ["good.py"]) == [])
    a = sorted({x["rule"] for x in GM.scan_a11y(e, ["page.html"])})
    check("the accessibility scan finds missing alt, unlabelled field, clickable div, empty controls, no language, no title, a skipped heading and a positive tabindex",
          a == sorted(["img-no-alt", "unlabelled-input", "click-on-div", "empty-control", "no-lang", "no-title", "heading-skip", "positive-tabindex"]), a)
    check("and passes a page that does all of it right", GM.scan_a11y(e, ["ok.html"]) == [], GM.scan_a11y(e, ["ok.html"]))
    check("the docs scan finds the module and the functions with no docstring", len(GM.scan_docs(e, ["bad.py"])) == 5 and GM.scan_docs(e, ["good.py"])[0]["rule"] == "no-module-doc")
    check("the migration search reads the old name from the request", GM.old_pattern({"goal": "please replace `os.system` with subprocess.run"}) == "os.system"
          and GM.old_pattern({"goal": "x", "params": {"old": "foo("}}) == "foo(" and GM.old_pattern({"goal": "tidy up"}) == "")


def _live_failure_fixes(check, home):
    """Each of these was a real live-benchmark failure on a 4B worker (2026-11-03); the check proves the harness no longer causes it."""
    import time
    import arch_core as core
    import arch_flows as F
    c = F.interface_contract("Create dates.py with is_leap(year), days_in_month(year, month) and day_of_year(year, month, day), plus unit tests in tests/test_dates.py.")
    check("an interface written as plain words is read: module functions with their argument counts",
          c["imports"] == [("dates", "is_leap"), ("dates", "days_in_month"), ("dates", "day_of_year")]
          and [(n, k) for n, k, _, _ in c["calls"]] == [("is_leap", 1), ("days_in_month", 2), ("day_of_year", 3)], c)
    c = F.interface_contract("Create cart.py with a Cart class: add(sku, price, qty=1), remove(sku), total() rounded to 2 decimals.")
    check("with one named class, the plain-words calls are its methods (keywords kept)",
          c["imports"] == [("cart", "Cart")] and c["methods"][0][:4] == ("Cart", "add", 2, ["qty"]) and len(c["methods"]) == 3, c)
    vague = [F.interface_contract(g) for g in ("Make the report faster; it calls len(rows) too often.",
                                               "Create a.py and b.py with helpers f(x) and g(y).",
                                               "Create stats.py where print(x) shows the value and total(xs + 1) sums.")]
    check("anything less certain names nothing: no module to create, two modules, builtins, or arguments that are not names",
          all(not (v["imports"] or v["calls"] or v["methods"]) for v in vague), vague)
    check("a request that already uses backticks is read exactly as before",
          F.interface_contract("Build it. `from shortener import Shortener`; `s = Shortener(path)`")["imports"] == [("shortener", "Shortener")])

    before = {"dates.py": {"is_leap", "days_in_month"}}
    check("a rewrite that drops a function an earlier sprint built is caught",
          F.lost_defs(before, {"dates.py": {"is_leap", "day_of_year"}}) == [("dates.py", "days_in_month")])
    check("but not when the function moved to another file, or when this sprint's goal names it (a rename)",
          F.lost_defs(before, {"dates.py": {"is_leap"}, "cal.py": {"days_in_month"}}) == []
          and F.lost_defs(before, {"dates.py": {"is_leap"}}, "rename days_in_month to month_days") == [])
    proj = os.path.join(home, "defs")
    os.makedirs(os.path.join(proj, "tests"), exist_ok=True)
    with open(os.path.join(proj, "m.py"), "w") as f:
        f.write("def a():\n    return 1\n\n\ndef _private():\n    return 2\n\n\nclass B:\n    pass\n")
    with open(os.path.join(proj, "tests", "test_m.py"), "w") as f:
        f.write("def test_x():\n    assert True\n")
    check("public_defs reads top-level public names from non-test files only", F.public_defs(proj) == {"m.py": {"a", "B"}}, F.public_defs(proj))

    with open(os.path.join(proj, "t.py"), "w") as f:
        f.write("class T:\n    def test_div(self):\n        self.assertEqual(divide(1, 0), 0)\n")
    p = core.prepare_block(proj, core.load_config(proj), {"kind": "edit", "path": "t.py", "pairs": [
        ("    self.assertEqual(divide(1, 0), 0)", "    with self.assertRaises(ZeroDivisionError):\n        divide(1, 0)")]})
    ok = p["status"] == "ok"
    try:
        compile(p.get("new") or "", "t.py", "exec")
    except SyntaxError:
        ok = False
    check("an EDIT whose SEARCH matched part-way into a line keeps the block's own indentation (no IndentationError)",
          ok and "        with self.assertRaises(ZeroDivisionError):\n            divide(1, 0)" in (p.get("new") or ""), p.get("new"))

    t0 = time.time()
    code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('started', flush=True); time.sleep(60)")
    rc, out = core.run_cmd([sys.executable, "-c", code], proj, timeout=3)
    check("a test run that leaves a server running is stopped at its time limit, whole process tree included (it used to wait forever on Windows)",
          rc == 124 and "started" in out and time.time() - t0 < 30, (rc, out[:200], round(time.time() - t0, 1)))

    e = core.Engine(proj, "build", "g", review="auto", say=lambda *a, **k: None, persist=False)
    e._local_restarts = 2
    check("a crashed local server is restarted at most twice per run", e._restart_local(RuntimeError("x")) is False)


if __name__ == "__main__":
    sys.exit(run())
