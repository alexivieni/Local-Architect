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
    mh.CONFIG_FILE = core.CONFIG_FILE
    mh.write_config({"eval": {"pass_rate_drop": 2.0}})
    check("setting the allowance to 2 points makes the same drop a regression", arch_eval.compare(base, cand)["ok"] is False)
    mh.write_config({})

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
