"""Self-test for the extra flows, templates and quality tools (new, feature, test, optimize, hunt, review).
A scripted fake Ollama plays every role; the verifiers (pytest, mutation, benchmark, query plans) are real.
Run:  python architect.py selftest      (this file runs after the core self-test)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import arch_selftest as base_test  # noqa: E402

CALC = '''def clamp(x, lo, hi):
    if lo > hi:
        raise ValueError("lo > hi")
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def mean(xs):
    if not xs:
        raise ValueError("empty")
    return sum(xs) / len(xs)
'''
CALC_WEAK = "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom calc import clamp\n\n\ndef test_inside():\n    assert clamp(5, 0, 10) == 5\n"
CALC_EXTRA_WRONG = "import sys, os\nimport pytest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom calc import clamp, mean\n\n\ndef test_wrong():\n    assert clamp(11, 0, 10) == 11\n"
CALC_EXTRA = ("import sys, os\nimport pytest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom calc import clamp, mean\n\n\n"
              "def test_below():\n    assert clamp(-1, 0, 10) == 0\n\n\ndef test_above():\n    assert clamp(11, 0, 10) == 10\n\n\n"
              "def test_edges():\n    assert clamp(0, 0, 10) == 0\n    assert clamp(10, 0, 10) == 10\n\n\n"
              "def test_bad_range():\n    with pytest.raises(ValueError):\n        clamp(1, 5, 2)\n\n\n"
              "def test_mean():\n    assert mean([2, 4]) == 3\n    assert mean([5]) == 5\n\n\n"
              "def test_mean_empty():\n    with pytest.raises(ValueError):\n        mean([])\n")
DEDUPE = "def dedupe(items):\n    out = []\n    for x in items:\n        if x not in out:\n            out.append(x)\n    return out\n"
DEDUPE_FAST = "def dedupe(items):\n    seen = set()\n    out = []\n    for x in items:\n        if x not in seen:\n            seen.add(x)\n            out.append(x)\n    return out\n"
DEDUPE_TEST = "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom dedupe import dedupe\n\n\ndef test_order_kept():\n    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]\n\n\ndef test_empty():\n    assert dedupe([]) == []\n"
BENCH = "import os, sys, time\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom dedupe import dedupe\n\ndata = list(range(2500)) * 2\nt = time.perf_counter()\nfor _ in range(2):\n    dedupe(data)\nprint('{\"seconds\": %f}' % ((time.perf_counter() - t) / 2))\n"
STATS = "def average(xs):\n    return sum(xs) / len(xs)\n\n\ndef top(xs, n):\n    return sorted(xs)[:n]\n"
STATS_TEST = "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom stats import average\n\n\ndef test_avg():\n    assert average([2, 4]) == 3\n"
HUNT_REPLY = ('FILE: tests/hunt/test_top_largest.py\n```python\n"""BUG: top() returns the smallest values, not the largest | stats.py:top | sorted() is ascending and never reversed"""\n'
              'import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))\nfrom stats import top\n\n\ndef test_top_returns_largest():\n    assert top([1, 5, 3], 2) == [5, 3]\n```\n'
              'FILE: tests/hunt/test_average_fine.py\n```python\n"""BUG: average is wrong | stats.py:average | suspicion only"""\nimport sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))\nfrom stats import average\n\n\ndef test_average():\n    assert average([2, 4]) == 3\n```\n'
              'FILE: tests/hunt/test_broken.py\n```python\n"""BUG: made-up | stats.py:nothing | invalid test"""\n\ndef test_x():\n    assert undefined_name == 1\n```\n')
GREET = "def greet(name):\n    if not name or not name.strip():\n        raise ValueError('name required')\n    return 'Hello, %s!' % name.strip()\n"
GREET_TEST = "import sys, os\nimport pytest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom greet import greet\n\n\ndef test_greet():\n    assert greet(' Ada ') == 'Hello, Ada!'\n\n\ndef test_blank():\n    with pytest.raises(ValueError):\n        greet(' ')\n"
E2E_TEST = "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom greet import greet\n\n\ndef test_e2e_greeting_flow():\n    assert greet('Grace') == 'Hello, Grace!'\n"


def fence(path, text, lang="python"):
    return f"FILE: {path}\n```{lang}\n{text}```\n"


class Pro(base_test.Fake):
    def script(self, role, user, full, st):
        if "Pick the best starting template" in user:
            return '{"template": "python_lib", "why": "a library"}'
        if "Write NEW tests for these files" in user:
            if "test_calc_extra.py" in user:
                return fence("tests/test_calc_extra.py", CALC_EXTRA if "FEEDBACK" in user else CALC_EXTRA_WRONG)
            return fence("tests/test_calc_gaps.py", "def test_placeholder_gap():\n    assert True\n")
        if "micro-benchmark" in user:
            return fence("bench/bench_dedupe.py", BENCH)
        if "Make this code faster" in user:
            if "FEEDBACK" in user:
                return fence("dedupe.py", DEDUPE_FAST)
            return "EDIT: dedupe.py\n<<<<<<< SEARCH\n        if x not in out:\n=======\n        if x not in list(out):\n>>>>>>> REPLACE\n"
        if "avoid full table scans" in user:
            return fence("db/migrations/003_total_index.sql", "CREATE INDEX idx_orders_total ON orders(total_cents);\n", "sql")
        if "Hunt for REAL bugs" in user:
            return HUNT_REPLY
        if "CURRENTLY FAILS WITH" in user and "test_top_largest.py" in user:
            return "EDIT: stats.py\n<<<<<<< SEARCH\n    return sorted(xs)[:n]\n=======\n    return sorted(xs, reverse=True)[:n]\n>>>>>>> REPLACE\n"
        if "Review this code as a senior engineer" in user:
            return "## Summary\nSmall module; top() looks wrong.\n## Risks\n| severity | where | problem | fix |\n|---|---|---|---|\n| high | stats.py:top | ascending sort | reverse |\n## Design\n- fine\n## Missing tests\n- top\n"
        if "Plan ONE feature across all layers" in user:
            return json.dumps({"sprints": [{"name": "greeting", "kind": "mixed", "goal": "greeting function", "files": ["greet.py", "tests/test_greet.py"],
                                            "acceptance": [{"id": "A1", "text": "greets by trimmed name"}, {"id": "A2", "text": "blank name raises ValueError"}]}]})
        if "Implement this sprint" in user and "greeting" in user:
            return fence("greet.py", GREET) + fence("tests/test_greet.py", GREET_TEST)
        if "END-TO-END test" in user:
            return fence("tests/test_e2e_greeting.py", E2E_TEST)
        return super().script(role, user, full, st)


def write(root, files):
    for p, t in files.items():
        f = os.path.join(root, p)
        os.makedirs(os.path.dirname(f), exist_ok=True)
        open(f, "w", encoding="utf-8").write(t)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    import arch_checks as ck
    import arch_core as core
    import arch_flows as fl
    import arch_quality as q
    import arch_roles as roles
    import arch_templates as T

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Pro)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{fake.server_address[1]}"
    os.environ["OLLAMA_HOST"] = host
    tmp = tempfile.mkdtemp(prefix="archpro_")
    # The suite must not read the install's own settings: once a person picks a team, those settings name real models on a real
    # server, and every engine here would reach for them instead of the scripted stand-in.
    _old_home = core.HOME
    core.set_home(os.path.join(tmp, "_home"))
    os.makedirs(core.HOME, exist_ok=True)
    quiet = lambda m: None
    py = sys.executable

    def eng(root, goal, flow, review="auto", **over):
        os.makedirs(root, exist_ok=True)
        c = core.load_config(root)
        c["roles"] = {r: "fake:7b" for r in roles.ROLES}
        c["behavior"]["playwright"] = "off"
        c = core.deep_merge(c, over)
        return core.Engine(root, flow, goal, c, review=review, say=quiet)

    def marks(e):
        out = {}
        for ln in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8"):
            j = json.loads(ln)
            if j.get("type") == "mark":
                out[j["id"]] = j
        return list(out.values())

    try:
        # ---- roles and routing
        check("new roles exist: tester, optimizer, bug hunter, full-stack", all(r in roles.ROLES for r in ("tester", "optimizer", "bug_hunter", "fullstack")))
        cats = {c for c, _ in roles.CATEGORIES}
        check("every role only needs real scoreboard categories", all(set(r["needs"]) <= cats for r in roles.ROLES.values()))
        check("every role has a fallback chain entry", all(r in roles.FALLBACK for r in roles.ROLES))
        models = [{"name": "m:7b", "params": "7B", "agg": {}, "rec": {"verdict": "fast"}, "know": {"kind": "coder"}}]
        ranked, plan = roles.recommend(models)
        check("recommendation covers all roles (as labelled guesses with no scores)", set(ranked) == set(roles.ROLES) and all(ranked[r][0]["basis"] == "guess" for r in roles.ROLES))

        proj = os.path.join(tmp, "route")
        write(proj, {"calc.py": CALC})
        e = eng(proj, "x", "route")
        cases = {"Add more tests for calc.py": "test", "Improve test coverage of the pricing module": "test", "Make the search function faster, it is too slow": "optimize",
                 "Find bugs in the cart module, look for edge cases": "hunt", "Code review of the payment code": "review", "Add an end-to-end feature: comments with API and UI": "feature",
                 "Fix: the total is wrong, Traceback ...": "bugfix", "Refactor the order handling": "refactor"}
        got = {g: fl.route(e, g)[0] for g in cases}
        check("router sends each kind of request to the right flow", got == cases, {g: (v, cases[g]) for g, v in got.items() if v != cases[g]})
        empty = os.path.join(tmp, "route_empty")
        os.makedirs(empty)
        e0 = eng(empty, "x", "route")
        check("in an empty folder a build request starts a new project", fl.route(e0, "Create a booking API with a web page")[0] == "new")

        # ---- templates
        check("every template renders; names and placeholders are filled", all("__NAME__" not in t and "__PKG__" not in t for tid in T.TEMPLATES for t in T.render(tid, "My App").values())
              and "my_app/core.py" in T.render("python_lib", "My App"))
        check("template suggestion follows the wording", T.suggest("a full-stack web app")[0] == "fullstack" and T.suggest("a SQL reporting database")[0] == "data_sql" and T.suggest("a REST API")[0] == "rest_api")
        tdir = os.path.join(tmp, "tpl")
        bad_tpl = []
        for tid in ("empty", "python_lib", "static_web", "data_sql", "rest_api", "fullstack"):
            d = os.path.join(tdir, tid)
            write(d, T.render(tid, "Demo", ["ci", "docker", "editorconfig"]))
            st, log = core.run_tests(d, core.load_config(d))
            res = ck.check_files(d, core.list_files(d, core.load_config(d)), dict(core.load_config(d), behavior=dict(core.load_config(d)["behavior"], playwright="off")), None)
            if st != "pass" or not ck.summarize(res)["ok"]:
                bad_tpl.append((tid, st, ck.summarize(res)["failures"][:200], log[-200:]))
        check("every template is green out of the box: its tests pass and its files pass our checks", not bad_tpl, bad_tpl)

        # ---- NEW from nothing, no model needed, via the real command line
        cli_root = os.path.join(tmp, "from_nothing", "my-lib")
        os.makedirs(os.path.dirname(cli_root))
        env = dict(os.environ, OLLAMA_HOST="127.0.0.1:9")                      # nothing listens here: there is NO model
        r = subprocess.run([py, os.path.join(HERE, "architect.py"), "run", "Slug tools", "--flow", "new", "--template", "python_lib", "--no-build", "--review", "auto", "--root", cli_root,
                            "--set", "behavior.git=commit"], capture_output=True, text=True, env=env, timeout=120)
        check("start from nothing: the folder is created, the template is written and its tests pass, with no model at all",
              r.returncode == 0 and os.path.isfile(os.path.join(cli_root, "my_lib", "core.py")) and "template checks" in r.stdout, r.stdout[-500:] + r.stderr[-300:])
        if shutil.which("git"):
            g = subprocess.run(["git", "log", "--oneline"], cwd=cli_root, capture_output=True, text=True)
            check("git checkpoint: --set behavior.git=commit creates the repository and commits", g.returncode == 0 and "architect new" in g.stdout, g.stdout + g.stderr)
        again = subprocess.run([py, os.path.join(HERE, "architect.py"), "run", "Slug tools", "--flow", "new", "--template", "python_lib", "--no-build", "--review", "auto", "--root", cli_root], capture_output=True, text=True, env=env, timeout=120)
        check("running the new flow again keeps existing files (nothing overwritten)", again.returncode == 0 and "0 file(s) to create" in again.stdout, again.stdout[-300:])
        r2 = subprocess.run([py, os.path.join(HERE, "architect.py"), "run", "A tiny library that can slugify titles and strip punctuation, with tests", "--flow", "new", "--review", "auto",
                             "--root", os.path.join(tmp, "from_nothing", "built"), "--template", "python_lib"], capture_output=True, text=True, env=dict(os.environ, OLLAMA_HOST=host), timeout=240)
        check("new flow builds the goal on top of the template (plan, code, failing check, fix, judge)",
              r2.returncode == 0 and os.path.isfile(os.path.join(tmp, "from_nothing", "built", "slugify.py")) and "[done] OK" in r2.stdout, r2.stdout[-600:] + r2.stderr[-300:])
        r3 = subprocess.run([py, os.path.join(HERE, "architect.py"), "run", "A tiny library that can slugify titles and strip punctuation, with tests", "--flow", "new", "--review", "auto",
                             "--root", os.path.join(tmp, "from_nothing", "nomodel"), "--template", "python_lib"], capture_output=True, text=True, env=env, timeout=120)
        check("with no model the template is still created and the build step explains what it needs",
              os.path.isfile(os.path.join(tmp, "from_nothing", "nomodel", "nomodel", "core.py")) and ("needs a model" in r3.stdout or "No model" in r3.stdout), r3.stdout[-400:])
        d = os.path.join(tmp, "dry")
        rd = subprocess.run([py, os.path.join(HERE, "architect.py"), "run", "x", "--flow", "new", "--template", "empty", "--review", "dry", "--root", d], capture_output=True, text=True, env=env, timeout=60)
        check("dry review shows the plan and writes nothing", rd.returncode == 0 and [x for x in os.listdir(d) if x != ".arch"] == [], rd.stdout[-200:])

        # ---- FEATURE
        fdir = os.path.join(tmp, "feature")
        write(fdir, {"README.md": "# x\n"})
        e = eng(fdir, "Add a greeting feature that greets people by name", "feature")
        out = fl.run_flow(e, "feature", {})
        used = {u: n for u, n in e.models_used.items()}
        roles_called = {json.loads(l).get("role") for l in open(os.path.join(e.run_dir, "trace.jsonl")) if '"type": "call"' in l}
        check("feature flow: layered plan, full-stack role writes it, tester adds an end-to-end test, all green",
              out["ok"] and out.get("e2e") and os.path.isfile(os.path.join(fdir, "tests", "test_e2e_greeting.py")) and {"fullstack", "tester"} <= roles_called, (out, roles_called, e.report_lines[-8:]))

        # ---- TEST (mutation-driven)
        tdir2 = os.path.join(tmp, "testflow")
        write(tdir2, {"calc.py": CALC, "tests/test_calc.py": CALC_WEAK})
        e = eng(tdir2, "Improve the tests for calc.py", "test")
        out = fl.run_flow(e, "test", {})
        wrong_rejected = any(not m["accepted"] for m in marks(e))
        check("test flow: weak suite measured, wrong first draft rejected by the real run, new tests pass, mutation score rises",
              out["ok"] and out["mutation_before"] is not None and out["mutation_after"] > out["mutation_before"] and wrong_rejected and os.path.isfile(os.path.join(tdir2, "tests", "test_calc_extra.py")),
              (out, e.report_lines[-12:]))
        check("test flow never edits existing tests", open(os.path.join(tdir2, "tests", "test_calc.py")).read() == CALC_WEAK)
        e = eng(tdir2, "Improve the tests for calc.py", "test", behavior={"mutation": "off"})
        write(tdir2, {"tests/test_zz_red.py": "def test_x():\n    assert False\n"})
        out = fl.run_flow(e, "test", {})
        check("test flow refuses to start when the existing tests already fail", not out["ok"])
        os.remove(os.path.join(tdir2, "tests", "test_zz_red.py"))
        # mutation tool directly
        m = q.mutation_check(tdir2, core.load_config(tdir2), ["calc.py"], 6)
        check("mutation check: full suite catches mutations, project untouched", m["ok"] and m["killed"] >= m["total"] - 1 and open(os.path.join(tdir2, "calc.py")).read() == CALC, m)

        # ---- OPTIMIZE (python benchmark gate)
        odir = os.path.join(tmp, "opt")
        write(odir, {"dedupe.py": DEDUPE, "tests/test_dedupe.py": DEDUPE_TEST})
        e = eng(odir, "Make dedupe in dedupe.py faster", "optimize", behavior={"bench_runs": 3, "min_gain_pct": 30})
        out = fl.run_flow(e, "optimize", {})
        ms = marks(e)
        check("optimize flow: writes a benchmark, rolls back a slower change, keeps the measured speed-up",
              out["ok"] and "seen" in open(os.path.join(odir, "dedupe.py")).read() and out["after"] < out["baseline"] * 0.7 and os.path.isfile(os.path.join(odir, "bench", "bench_dedupe.py")), (out, e.report_lines[-8:]))
        check("optimize flow: the rejected attempt is labelled as a negative for training", any((not m["accepted"]) and ("rolled back" in m["reason"] or "faster" in m["reason"]) for m in ms), [m["reason"] for m in ms])
        core.run_tests(odir, core.load_config(odir))

        # ---- OPTIMIZE (SQL query plans)
        sdir = os.path.join(tmp, "sqlopt")
        write(sdir, T.render("data_sql", "Shop"))
        open(os.path.join(sdir, "db", "queries.sql"), "a").write("\n-- name: big_orders\nSELECT * FROM orders WHERE total_cents > :min_total;\n")
        before = [p for p in q.sql_plans(sdir, core.list_files(sdir, core.load_config(sdir))) if p["scan"]]
        e = eng(sdir, "Optimize the slow queries in the database", "optimize")
        out = fl.run_flow(e, "optimize", {})
        after = [p for p in q.sql_plans(sdir, core.list_files(sdir, core.load_config(sdir))) if p["scan"]]
        check("optimize flow (SQL): finds the full scan, adds an index in a NEW migration, plans prove it",
              before and out["ok"] and not after and os.path.isfile(os.path.join(sdir, "db", "migrations", "003_total_index.sql")), (before, after, out))

        # ---- HUNT (proof or discard)
        hdir = os.path.join(tmp, "hunt")
        write(hdir, {"stats.py": STATS, "tests/test_stats.py": STATS_TEST})
        e = eng(hdir, "Hunt for bugs in stats.py", "hunt")
        out = fl.run_flow(e, "hunt", {})
        bugs = open(os.path.join(hdir, "docs", "bugs.md")).read() if os.path.isfile(os.path.join(hdir, "docs", "bugs.md")) else ""
        check("hunt flow: one bug proven by a failing test; the passing and the invalid suspicion are discarded",
              out["ok"] and out["confirmed"] == 1 and os.path.isfile(os.path.join(hdir, "tests", "hunt", "test_top_largest.py"))
              and not os.path.exists(os.path.join(hdir, "tests", "hunt", "test_average_fine.py")) and not os.path.exists(os.path.join(hdir, "tests", "hunt", "test_broken.py")), (out, e.report_lines[-6:]))
        check("hunt flow writes docs/bugs.md with the proof and static hints", "top() returns the smallest" in bugs and "Static findings" in bugs)
        hdir2 = os.path.join(tmp, "hunt2")
        write(hdir2, {"stats.py": STATS, "tests/test_stats.py": STATS_TEST})
        e = eng(hdir2, "Hunt for bugs in stats.py", "hunt")
        out = fl.run_flow(e, "hunt", {"autofix": True})
        st, _ = core.run_tests(hdir2, core.load_config(hdir2))
        check("hunt flow with autofix: the proven bug is fixed through the bugfix flow and the whole suite is green", out["fixed"] == 1 and st == "pass" and "reverse=True" in open(os.path.join(hdir2, "stats.py")).read(), (out, st))

        # ---- REVIEW
        e = eng(hdir, "Review stats.py", "review")
        out = fl.run_flow(e, "review", {})
        rv = open(os.path.join(hdir, "docs", "review.md")).read()
        check("review flow: facts from tools plus the reviewer's notes in docs/review.md", out["ok"] and "Facts from tools" in rv and "## Risks" in rv)

        # ---- quality tools
        bad = "import pickle\ndef f(a, b=[]):\n    try:\n        x = 1\n    except:\n        pass\n    if a is 'x':\n        return 1\n    db.execute('select * from t where id=%s' % a)\n    return 2\n    print('never')\n"
        write(os.path.join(tmp, "scan"), {"bad.py": bad})
        rules = {x["rule"] for x in q.static_scan(os.path.join(tmp, "scan"), ["bad.py"])}
        check("static scan finds mutable default, bare except, 'is' with a literal, SQL string building, unreachable code",
              {"mutable-default", "bare-except", "is-literal", "sql-injection", "unreachable"} <= rules, rules)
        t, det = q.run_bench(tmp, core.load_config(tmp), "scan/bad.py", 1)
        check("benchmark runner rejects a script that does not report seconds", t is None)
        sp = os.path.join(tdir, "data_sql")
        res = ck.check_files(sp, core.list_files(sp, core.load_config(sp)), core.load_config(sp), None)
        check("SQL is checked as a project: migrations in order, queries validated against the schema", ck.summarize(res)["ok"] and any("applies cleanly" in r["name"] for r in res) and any("query" in r["name"] for r in res), ck.summarize(res)["failures"])
        write(os.path.join(tmp, "badmig"), {"db/migrations/001_a.sql": "CREATE TABLE a (id INTEGER PRIMARY KEY);", "db/migrations/002_b.sql": "ALTER TABLE nope ADD COLUMN x INT;"})
        res = ck.check_files(os.path.join(tmp, "badmig"), ["db/migrations/001_a.sql", "db/migrations/002_b.sql"], core.load_config(tmp), None)
        check("a migration that cannot apply after the earlier ones is caught", not ck.summarize(res)["ok"] and any("002_b" in r["name"] and not r["ok"] for r in res))
        css_ok = ck.check_html('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>t</title></head><body><h1>x</h1></body></html>', "p", ":focus-visible{outline:1px solid} @media (max-width:600px){body{margin:0}}")
        check("linked stylesheet rules count for focus and responsive checks", all(r["ok"] for r in css_ok), [r for r in css_ok if not r["ok"]])

        # ---- scoreboard tasks for the quality roles (code-checked)
        import arch_tasks as at
        fenced = lambda c: "```python\n" + c + "```"
        tt = at.TASKS["testing"][0]["check"]
        good = "def test_below():\n    assert clamp(-5, 0, 10) == 0\n    assert clamp(-1, 0, 10) == 0\n\ndef test_above():\n    assert clamp(50, 0, 10) == 10\n\ndef test_in():\n    assert clamp(5, 0, 10) == 5\n\ndef test_err():\n    try:\n        clamp(1, 5, 2)\n    except ValueError:\n        return\n    assert False\n"
        check("scoreboard 'write tests': thorough tests score 1.0, a weak test scores less, a rewrite of the function scores 0, tests wrong on correct code score 0",
              tt(fenced(good))[0] == 1.0 and tt(fenced("def test_in():\n    assert clamp(5, 0, 10) == 5\n"))[0] < 0.5 and tt(fenced(at.CLAMP))[0] == 0.0 and tt(fenced("def test_x():\n    assert clamp(5, 0, 10) == 6\n"))[0] == 0.0,
              (tt(fenced(good)), tt(fenced("def test_in():\n    assert clamp(5, 0, 10) == 5\n"))))
        bt = at.TASKS["bughunt"][0]["check"]
        check("scoreboard 'expose a bug': a test that fails on the bug and passes on the fix scores 1.0; a test that cannot see the bug scores 0; a wrong expectation scores low",
              bt(fenced("def test_bug():\n    assert paginate([1,2,3,4], 1, 2) == [1, 2]\n"))[0] == 1.0 and bt(fenced("def test_bug():\n    assert True\n"))[0] == 0.0
              and bt(fenced("def test_bug():\n    assert paginate([1,2,3,4], 1, 2) == [9]\n"))[0] == 0.3)
        ot = at.TASKS["optimize"][0]["check"]
        check("scoreboard 'speed up': a correct much faster rewrite scores 1.0; the unchanged code scores low; a wrong rewrite scores 0",
              ot(fenced(at.DEDUPE_FAST if hasattr(at, "DEDUPE_FAST") else DEDUPE_FAST))[0] == 1.0 and ot(fenced(at.DEDUPE))[0] <= 0.3 and ot(fenced("def dedupe(items):\n    return sorted(set(items))\n"))[0] == 0.0)

        # ---- --set overrides and settings defaults
        c = core.load_config()
        check("new controls exist with safe defaults", c["behavior"]["git"] == "off" and c["behavior"]["mutation_max"] == 8 and c["behavior"]["min_gain_pct"] == 10 and c["limits"]["max_sprints"] == 5)

        # ---- the parameter schema matches arch_core.DEFAULTS exactly, and validates
        import arch_params as params

        def _flatten(d, pre=""):
            out = {}
            for k, v in d.items():
                if isinstance(v, dict) and k not in ("roles", "role_overrides", "model_overrides", "tiers"):
                    out.update(_flatten(v, pre + k + "."))
                else:
                    out[pre + k] = v
            return out
        mism = params.selftest_defaults_match(_flatten(core.DEFAULTS))
        check("the parameter schema matches arch_core.DEFAULTS exactly", not mism, mism)
        try:
            params.validate_settings_patch({"limits.max_revisions": "x", "behavior.network": "bogus"})
            bad_rejected = False
        except params.ValidationError as e:
            bad_rejected = "limits.max_revisions" in str(e) and "behavior.network" in str(e)
        check("invalid settings are rejected with a clear message naming every bad field", bad_rejected)
        check("a good settings patch is coerced and clamped", params.validate_settings_patch({"limits.max_revisions": "2", "limits.max_sprints": "999"}) == {"limits.max_revisions": 2, "limits.max_sprints": 20})
        ov = {}
        params.apply_override(ov, "sampling.temperature", "0.3")
        params.apply_override(ov, "sampling.top_p", "0.9")
        params.apply_override(ov, "sampling.temperature", "")
        params.apply_override(ov, "think", "on")
        check("role/model overrides merge per-field instead of replacing the whole sampling dict", ov == {"sampling": {"top_p": 0.9}, "think": True}, ov)

        # ---- config presets: real bundles, validated against the same schema as a manual settings save
        presets = params.list_presets()
        check("at least 3 presets are offered, each with a label and a human blurb", len(presets) >= 3 and all(p["label"] and p["blurb"] for p in presets), [p["id"] for p in presets])
        bad_preset_fields = []
        for p in presets:
            try:
                coerced = params.apply_preset(p["id"])
                if set(coerced) != set(p["settings"]):
                    bad_preset_fields.append(p["id"])
            except params.ValidationError as e:
                bad_preset_fields.append(f"{p['id']}: {e}")
        check("every preset's settings are real, valid schema paths that coerce cleanly", not bad_preset_fields, bad_preset_fields)
        try:
            params.apply_preset("not-a-real-preset")
            preset_rejected = False
        except KeyError as e:
            preset_rejected = "not-a-real-preset" in str(e)
        check("applying an unknown preset name fails clearly instead of silently doing nothing", preset_rejected)
        strict = params.apply_preset("strict-security")
        check("the strict-security preset actually locks the network down", strict.get("behavior.network") == "offline" and strict.get("behavior.net_block_tools") is True, strict)
    finally:
        fake.shutdown()
        core.set_home(_old_home)
        shutil.rmtree(tmp, ignore_errors=True)
        # the network guard lives under the data folder now and is rebuilt by every run: nothing to remove here
    print()
    if all(results):
        print(f"  ALL {len(results)} EXTENDED CHECKS PASSED")
        return 0
    print(f"  {results.count(False)} of {len(results)} EXTENDED CHECKS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(run())
