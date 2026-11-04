"""Live test: does the harness actually WORK with the models you have? Real tasks, real model calls, judged by code.

Each scenario builds a small fixture project, runs a real flow with your configured team, then checks the result with checks the model never saw
(a hidden behaviour check, the unchanged tests, the old functions still being there). Nothing here is scripted; with a real Ollama the model does the work.
The result is saved to hub_data/livetest.json and shown in the hub. Your own projects are never touched: fixtures live in a throwaway folder.

    python architect.py livetest            all scenarios (a few minutes with a small model, longer with a big one)
    python architect.py livetest --quick    routing, structured output and one build
"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import arch_core as core

HERE = os.path.dirname(os.path.abspath(__file__))

ROUTE_CASES = [("the login page crashes with a traceback when the password is empty", "bugfix"),
               ("make the report generation a lot faster", "optimize"),
               ("write unit tests for the parser module", "test"),
               ("set up GitHub Actions so our tests run on every push", "cicd"),
               ("restructure the order module without changing what it does", "refactor"),
               ("do a code review of the payments module", "review")]

STATS = "def average(xs):\n    \"\"\"Mean of a list of numbers.\"\"\"\n    return sum(xs) / (len(xs) - 1)\n\n\ndef total(xs):\n    return sum(xs)\n"
STATS_TEST = ("import unittest\nfrom stats import average, total\n\n\nclass T(unittest.TestCase):\n    def test_average(self):\n        self.assertEqual(average([2, 4, 6]), 4)\n\n"
              "    def test_total(self):\n        self.assertEqual(total([1, 2]), 3)\n\n\nif __name__ == '__main__':\n    unittest.main()\n")
INV = ('"""Tiny inventory helpers."""\n\n\ndef add_item(items, name, qty):\n    items[name] = items.get(name, 0) + qty\n    return items\n\n\n'
       'def remove_item(items, name, qty):\n    if items.get(name, 0) < qty:\n        raise ValueError("not enough stock")\n    items[name] -= qty\n    return items\n\n\n'
       'def count(items):\n    return sum(items.values())\n\n\ndef names(items):\n    return sorted(items)\n\n\ndef has(items, name):\n    return items.get(name, 0) > 0\n')
INV_TEST = ("import unittest\nfrom inventory import add_item, remove_item, count, names, has\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add_item({}, 'a', 2), {'a': 2})\n\n"
            "    def test_remove(self):\n        with self.assertRaises(ValueError):\n            remove_item({'a': 1}, 'a', 5)\n\n    def test_count_names_has(self):\n"
            "        d = {'a': 1, 'b': 0}\n        self.assertEqual(count(d), 1)\n        self.assertEqual(names(d), ['a', 'b'])\n        self.assertTrue(has(d, 'a'))\n        self.assertFalse(has(d, 'b'))\n\n\n"
            "if __name__ == '__main__':\n    unittest.main()\n")
BAD_CI = "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@main\n      - run: python -m unittest discover -s tests -q || true\n"
CI_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"


def _write(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


def _read(root, rel):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _py(root, code, timeout=60):
    """Run a snippet or command in the fixture folder, offline, and return (ok, output)."""
    try:
        p = subprocess.run([sys.executable, "-B", *code] if isinstance(code, list) else [sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1"))
        return p.returncode == 0, (p.stdout + p.stderr)[-400:]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def _tests_pass(root):
    ok, out = _py(root, ["-m", "unittest", "discover", "-s", "tests", "-q"] if os.path.isdir(os.path.join(root, "tests")) else ["-m", "unittest", "discover", "-q"])
    return ok and "NO TESTS" not in out.upper() and "Ran 0 tests" not in out, out


def _engine(proj, flow, goal, calls=40):
    cfg = core.load_config(proj)
    cfg["limits"]["max_calls"] = calls
    cfg["limits"]["max_revisions"] = 3
    cfg["behavior"]["postmortem"] = "off"                 # a measurement: no extra explaining call on a failed scenario
    e = core.Engine(proj, flow, goal, cfg, review="auto", say=lambda m: None, persist=True)
    _OPEN.append(e)
    return e


_OPEN = []      # engines of the scenario running now: whatever they moved off the graphics card comes back before the next one


# ------------------------------------------------------------------------------------------------------------ scenarios
def s_build(proj):
    goal = "Create calc.py with functions add, subtract, multiply and divide (divide by zero must raise ZeroDivisionError), and unit tests for them in tests/test_calc.py."
    e = _engine(proj, "build", goal)
    import arch_flows
    arch_flows.run_flow(e, "build", {"domains": []})
    ok, reasons = judge_build(proj)
    return ok, reasons, e


def judge_build(proj):
    ok, why = _py(proj, "import calc\nassert calc.add(2,3)==5 and calc.subtract(5,2)==3 and calc.multiply(3,4)==12 and calc.divide(8,2)==4\n"
                        "try:\n    calc.divide(1,0)\n    raise SystemExit('divide by zero did not raise')\nexcept ZeroDivisionError:\n    pass")
    t_ok, t_out = _tests_pass(proj)
    tests_exist = any(f.startswith("test") for f in os.listdir(os.path.join(proj, "tests"))) if os.path.isdir(os.path.join(proj, "tests")) else False
    reasons = []
    if not ok:
        reasons.append("hidden behaviour check failed: " + why.strip().splitlines()[-1][:120] if why.strip() else "calc.py missing")
    if not (t_ok and tests_exist):
        reasons.append("the project's own tests do not pass (or were not written)")
    return (ok and t_ok and tests_exist), reasons


def s_bugfix(proj):
    _write(proj, {"stats.py": STATS, "tests/test_stats.py": STATS_TEST})
    before = hashlib.sha1((_read(proj, "tests/test_stats.py") or "").encode()).hexdigest()
    e = _engine(proj, "bugfix", "The test suite in tests/ fails. Find the cause in the code and fix it.")
    import arch_flows
    arch_flows.run_flow(e, "bugfix", {})
    ok, reasons = judge_bugfix(proj, before)
    return ok, reasons, e


def judge_bugfix(proj, before):
    reasons = []
    if hashlib.sha1((_read(proj, "tests/test_stats.py") or "").encode()).hexdigest() != before:
        reasons.append("the model edited the tests instead of fixing the code")
    ok, why = _py(proj, "from stats import average, total\nassert average([2,4,6])==4 and average([10])==10 and total([1,2])==3")
    if not ok:
        reasons.append("the bug is still there: " + (why.strip().splitlines()[-1][:100] if why.strip() else "average() fails"))
    return not reasons, reasons


def s_feature(proj):
    _write(proj, {"inventory.py": INV, "tests/test_inventory.py": INV_TEST})
    e = _engine(proj, "feature", "Add a function low_stock(items, threshold) to inventory.py that returns the sorted names whose quantity is below threshold, with a test. Keep every existing function.")
    import arch_flows
    arch_flows.run_flow(e, "feature", {"domains": []})
    ok, reasons = judge_feature(proj)
    return ok, reasons, e


def judge_feature(proj):
    n_before = len(INV.splitlines())
    src = _read(proj, "inventory.py") or ""
    reasons = []
    try:
        have = {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    except SyntaxError:
        have = set()
        reasons.append("inventory.py no longer parses")
    lost = {"add_item", "remove_item", "count", "names", "has"} - have
    if lost:
        reasons.append("existing functions were lost: " + ", ".join(sorted(lost)))
    if src and len(src.splitlines()) < n_before * 0.7:
        reasons.append("the file was cut down instead of edited")
    ok, why = _py(proj, "from inventory import low_stock\nassert low_stock({'a':1,'b':5,'c':0}, 2)==['a','c'] and low_stock({}, 3)==[]")
    if not ok:
        reasons.append("low_stock does not behave correctly")
    t_ok, _ = _tests_pass(proj)
    if not t_ok:
        reasons.append("the project's tests no longer pass")
    return not reasons, reasons


def s_cicd(proj):
    _write(proj, {".github/workflows/ci.yml": BAD_CI, "tests/test_a.py": CI_TEST})
    e = _engine(proj, "cicd", "Fix the CI pipeline: it has security and reliability problems.", calls=30)
    import arch_flows
    arch_flows.run_flow(e, "cicd", {})
    ok, reasons = judge_cicd(proj)
    return ok, reasons, e


def judge_cicd(proj):
    import arch_ci
    blocking = [f for f in arch_ci.check_all(proj)["findings"] if f["gate"]]
    wf = _read(proj, ".github/workflows/ci.yml") or ""
    reasons = []
    if blocking:
        reasons.append("blocking findings remain: " + "; ".join(f"{f['rule']}" for f in blocking[:3]))
    if "unittest" not in wf and "pytest" not in wf:
        reasons.append("the test step was removed from the pipeline")
    if "|| true" in wf:
        reasons.append("failures are still swallowed with '|| true'")
    return not reasons, reasons


def s_route(proj):
    _write(proj, {"app.py": "x = 1\n", "tests/test_app.py": CI_TEST})
    import arch_flows
    e = _engine(proj, "route", "x")
    e.persist_runs = False
    wrong, hit = [], 0
    for req, want in ROUTE_CASES:
        got, _, why = arch_flows.route(e, req)
        if "router model" not in why:                       # the keyword fallback answered: that says nothing about the model
            wrong.append(f"'{req[:40]}…' -> the router model gave no usable answer")
        elif got == want:
            hit += 1
        else:
            wrong.append(f"'{req[:40]}…' -> {got} (wanted {want})")
    return hit >= 4, [f"{hit} of {len(ROUTE_CASES)} requests went to the right workflow" + ("; " + "; ".join(wrong[:2]) if wrong else "")], e


def s_json(proj):
    import arch_schemas
    e = _engine(proj, "route", "x")
    good = 0
    for goal in ("a todo list command line app", "a function that parses dates", "a small REST endpoint for notes"):
        _, j = e.ask_json("planner", "Plan the work as small SPRINTS now. " + goal, schema=arch_schemas.PLAN)
        good += 1 if isinstance(j, dict) and j.get("sprints") else 0
    return good == 3, [f"{good} of 3 plans came back in the required structure"], e


SCENARIOS = [("route", "Choose the right workflow for plain requests", s_route),
             ("json", "Answer in a required structure (plans)", s_json),
             ("build", "Build a small module with tests from nothing", s_build),
             ("bugfix", "Fix a bug without touching the tests", s_bugfix),
             ("feature", "Add a function without losing existing code", s_feature),
             ("cicd", "Repair a weak CI pipeline", s_cicd)]
QUICK = ("route", "json", "build")


def path():
    return os.path.join(core.data_dir(), "livetest.json")


def load():
    return core.load_json(path())


def run(only=None, quick=False, say=print, scenarios=None):
    """-> {"ts","roles","results":[{id,name,ok,secs,calls,models,detail}],"passed","total"}. Never raises for a model failure."""
    pick = set(only or (QUICK if quick else [s[0] for s in (scenarios or SCENARIOS)]))
    results = []
    cfg = core.load_config(tempfile.gettempdir())
    say(f"Live test with your team (builder: {(cfg.get('roles') or {}).get('generator') or cfg.get('default_model') or 'first installed model'})")
    for sid, title, fn in (scenarios or SCENARIOS):
        if sid not in pick:
            continue
        proj = tempfile.mkdtemp(prefix=f"live_{sid}_", dir=core.data_dir() if os.path.isdir(core.data_dir()) else None)
        t0 = time.time()
        say(f"  [{sid}] {title} ...")
        e = None
        try:
            ok, reasons, e = fn(proj)
            detail = "; ".join(reasons) if reasons else "passed"
        except (core.LLMError, core.BudgetExceeded, core.LoopDetected) as ex:
            ok, detail = False, f"stopped: {str(ex)[:140]}"
        except Exception as ex:                                               # a scenario bug must not hide the other results
            ok, detail = False, f"the test itself failed: {type(ex).__name__}: {str(ex)[:120]}"
        for x in _OPEN:                                   # a scenario that never finished a run (json, route) still puts the servers back
            try:
                x._bring_back()
            except Exception:
                pass
        _OPEN.clear()
        r = {"id": sid, "name": title, "ok": bool(ok), "secs": round(time.time() - t0), "calls": getattr(e, "calls", None), "models": sorted(getattr(e, "models_used", {}) or []), "detail": detail}
        results.append(r)
        say(f"      {'PASS' if ok else 'FAIL'}  {r['secs']}s, {r['calls']} model calls" + ("" if ok else f"  - {detail}"))
        shutil.rmtree(proj, ignore_errors=True)
    out = {"ts": time.time(), "roles": {r: m for r, m in (cfg.get("roles") or {}).items() if m}, "default_model": cfg.get("default_model"),
           "results": results, "passed": sum(1 for r in results if r["ok"]), "total": len(results)}
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        with open(path(), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    except OSError:
        pass
    say(f"Live test: {out['passed']} of {out['total']} passed." + ("" if out["passed"] == out["total"] else " The lines above say what failed and why; a failure is information about the model/team, not a crash."))
    return out
