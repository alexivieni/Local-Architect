"""Self-test for the "win more tasks" work: function-level DEF edits, the interface decided at planning time, one sprint for one module,
keeping the best try, removing an unusable reproduction test, and the post-mortem that explains a failure and travels to the retry.

Each piece was built because a real model failed a real benchmark task in that exact way (2026-11-03, a 4B worker on a 6 GB laptop).
The build and bugfix flows are run for real through the engine against a scripted model: the code checks decide, as always.

No real model, no GPU, no network.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

T_IMPORT = "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"

DATES_GOAL = "Create dates.py with is_leap(year) and days_in_month(year, month), plus unit tests in tests/test_dates.py."
DATES_PLAN = {"sprints": [
    {"name": "leap", "kind": "backend", "goal": "is_leap(year)", "files": ["dates.py", "tests/test_dates.py"],
     "acceptance": [{"id": "A1", "text": "is_leap follows the Gregorian rule"}]},
    {"name": "months", "kind": "backend", "goal": "days_in_month(year, month)", "files": ["dates.py"],
     "acceptance": [{"id": "A2", "text": "days_in_month knows February"}]}],
    "interface": ["dates.py: is_leap(year)", "dates.py: days_in_month(year, month)", "dates.py: month_name(month)"]}
DATES_TESTS = (T_IMPORT + "from dates import is_leap, days_in_month\n\n\ndef test_leap():\n    assert is_leap(2000) and not is_leap(1900)\n\n\n"
               "def test_feb():\n    assert days_in_month(2024, 2) == 29\n\n\ndef test_jan():\n    assert days_in_month(2023, 1) == 31\n")
TRY1 = ("FILE: dates.py\n```python\ndef is_leap(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n\n\n"
        "def days_in_month(year, month):\n    return 30\n```\n\nFILE: tests/test_dates.py\n```python\n" + DATES_TESTS + "```\n")
TRY2 = ("FILE: dates.py\n```python\n# try2-broken\ndef is_leap(year)\n    return True\n\n\ndef days_in_month(year, month):\n    return 31\n```\n")
TRY3 = ("DEF: dates.py::days_in_month\n```python\nimport calendar\n\n\ndef days_in_month(year, month):\n"
        "    return calendar.monthrange(year, month)[1]\n```\n")

DUP = ("def _norm_a(xs):\n    out = []\n    for x in xs:\n        if x is not None:\n            out.append(x)\n    return out\n\n\n"
       "def _norm_b(xs):\n    out = []\n    for x in xs:\n        if x is not None:\n            out.append(x)\n    return out\n\n\n"
       "def cleaned(xs):\n    return _norm_a(xs) if len(xs) % 2 else _norm_b(xs)\n")
CALC_GOAL = "divide(1, 0) in calc.py returns 0 but it must raise ZeroDivisionError"
CALC = "def divide(a, b):\n    return a / b if b else 0\n"
CALC_TESTS = T_IMPORT + "from calc import divide\n\n\ndef test_divide():\n    assert divide(6, 3) == 2\n"


def _ecore_check(check):
    import arch_hw
    m = arch_hw.ecore_mask()
    ok = True
    if m:
        import subprocess as sp
        p = sp.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        try:
            ok = arch_hw.pin_to_ecores(p._handle if os.name == "nt" else p.pid, m)
        finally:
            p.wait()
    check("tests on efficiency cores: on a hybrid CPU the efficiency cores are found (a mask that leaves the performance cores out) "
          "and a started process can be moved onto them; elsewhere nothing is moved", (m == 0) or (ok and m < (1 << (os.cpu_count() or 64))), (hex(m), ok))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== winning more tasks: DEF edits, interface first, one sprint per module, best try kept, post-mortem ==")
    _ecore_check(check)
    import arch_tools as _tl
    check("safe edits: a Python edit that would not parse is caught with the parser's line and message; good Python and JSON pass, "
          "and file types without a parser are accepted", _tl.syntax_error("a.py", "def f(:\n  pass\n").startswith("line 1")
          and _tl.syntax_error("a.py", "def f():\n    return 1\n") == "" and _tl.syntax_error("x.json", "{bad") != ""
          and _tl.syntax_error("x.json", '{"a": 1}') == "" and _tl.syntax_error("x.md", "# any") == ""
          and {"view", "edit_lines"} <= set(_tl.TOOLS), [n for n in _tl.TOOLS])
    import arch_core as core
    import tempfile as _tf
    import arch_flows as _F
    _pr = _tf.mkdtemp(prefix="fv_")
    open(os.path.join(_pr, "app.py"), "w").write("def f(:\n    pass\n")
    _e = core.Engine(_pr, "bugfix", "g", core.load_config(_pr), review="auto", say=lambda m: None, persist=False)
    _calls = []
    _real_rt = core.run_tests
    core.run_tests = lambda root, cfg, targets=None: _calls.append(targets) or ("pass", "ok")
    try:
        _v1 = _F.verify(_e, ["app.py"])
        open(os.path.join(_pr, "app.py"), "w").write("def f():\n    return 1\n")
        _F.verify(_e, ["app.py"])
        _n_after_first = len(_calls)
        _F.verify(_e, ["app.py"])
    finally:
        core.run_tests = _real_rt
    check("fast verification: a file that does not parse fails the step without running any test; once it parses, the tests run, and "
          "running the checks again with no file changed reuses the result instead of waiting for the tests again",
          not _v1["ok"] and _v1["tests"][0] == "skipped" and _n_after_first >= 1 and len(_calls) == _n_after_first, (_v1.get("tests"), _calls))
    _old_sb = dict(core._SANDBOX)
    core._SANDBOX["mem_mb"] = 300
    try:
        _rc_big, _out_big = core.run_bounded([sys.executable, "-c", "x = bytearray(900 * 1024 * 1024); print('allocated')"], os.getcwd(), 60)
        _rc_ok, _out_ok = core.run_bounded([sys.executable, "-c", "x = bytearray(20 * 1024 * 1024); print('fine')"], os.getcwd(), 60)
    finally:
        core._SANDBOX.update(_old_sb)
    check("sandbox: a test process that grabs more memory than allowed is stopped and the reason is said in plain words; a normal one "
          "is unaffected", (os.name != "nt") or (_rc_big != 0 and "allocated" not in _out_big and "[sandbox]" in _out_big
          and _rc_ok == 0 and "fine" in _out_ok), (_rc_big, _out_big[-200:], _rc_ok))
    import arch_roles as _ro
    import arch_selfimprove as _si
    _brief = core.selfdev_brief(os.path.dirname(os.path.abspath(core.__file__)))
    _seen_env = {}
    _real_call = _si.subprocess.call
    _si.subprocess.call = lambda argv, cwd=None, env=None, **k: _seen_env.update(env or {}) or 0
    try:
        _si._run_flow(os.path.dirname(os.path.abspath(core.__file__)), "noop", "bugfix", "dry")
    finally:
        _si.subprocess.call = _real_call
    check("Architect developer: the role exists, its brief carries the project notes and the plan's progress log, and a self-improve "
          "run gives it to every call", "architect_dev" in _ro.ROLES and "[role:architect_dev]" in _brief and "CLAUDE.md" in _brief
          and _seen_env.get("ARCHITECT_SELFDEV") == "1", (_brief[:120], _seen_env.get("ARCHITECT_SELFDEV")))
    import arch_chat as _ch
    _here = os.path.dirname(os.path.abspath(core.__file__))
    _hints = _ch.code_hints(core.Engine(_here, "chat", "q", review="dry", say=lambda m: None, persist=False),
                            "Which function checks that a plan is valid?")
    check("chat looks the code up before answering: 'which function checks that a plan is valid' finds validate_plan with its file "
          "and line (the chat used to guess)", "arch_schemas.py:" in _hints and "def validate_plan" in _hints
          and _hints.find("def validate_plan") < 400, _hints[:300])
    check("the self-improvement copies (.arch_self) are never part of a project's files, index or context",
          not any(f.startswith(".arch_self") for f in core.list_files(_here, core.load_config())))
    _imp = core.add_missing_stdlib_imports("def slugify(t):\n    return re.sub('x', '-', t)\n")
    check("a Python file that uses a standard-library module it never imported (re.sub without import re) gets the import added by "
          "code; a parameter or variable with that name is left alone", _imp.startswith("import re\n")
          and core.add_missing_stdlib_imports("def f(re):\n    return re.x\n") == "def f(re):\n    return re.x\n", _imp)
    import arch_tools as _tl
    check("safe edits: a Python edit that would not parse is caught with the parser's line and message; good Python and JSON pass, "
          "and file types without a parser are accepted", _tl.syntax_error("a.py", "def f(:\n  pass\n").startswith("line 1")
          and _tl.syntax_error("a.py", "def f():\n    return 1\n") == "" and _tl.syntax_error("x.json", "{bad") != ""
          and _tl.syntax_error("x.json", '{"a": 1}') == "" and _tl.syntax_error("x.md", "# any") == ""
          and {"view", "edit_lines"} <= set(_tl.TOOLS), [n for n in _tl.TOOLS])
    import arch_tools as _tl
    check("safe edits: a Python edit that would not parse is caught with the parser's line and message; good Python and JSON pass, "
          "and file types without a parser are accepted", _tl.syntax_error("a.py", "def f(:\n  pass\n").startswith("line 1")
          and _tl.syntax_error("a.py", "def f():\n    return 1\n") == "" and _tl.syntax_error("x.json", "{bad") != ""
          and _tl.syntax_error("x.json", '{"a": 1}') == "" and _tl.syntax_error("x.md", "# any") == ""
          and {"view", "edit_lines"} <= set(_tl.TOOLS), [n for n in _tl.TOOLS])
    import arch_tools as _tl
    check("safe edits: a Python edit that would not parse is caught with the parser's line and message; good Python and JSON pass, "
          "and file types without a parser are accepted", _tl.syntax_error("a.py", "def f(:\n  pass\n").startswith("line 1")
          and _tl.syntax_error("a.py", "def f():\n    return 1\n") == "" and _tl.syntax_error("x.json", "{bad") != ""
          and _tl.syntax_error("x.json", '{"a": 1}') == "" and _tl.syntax_error("x.md", "# any") == ""
          and {"view", "edit_lines"} <= set(_tl.TOOLS), [n for n in _tl.TOOLS])
    import arch_schemas as _sc
    good = {"sprints": [{"name": "api", "goal": "add /health", "files": ["app/main.py"], "acceptance": [{"id": "a1", "text": "GET /health is 200"}]}]}
    bad = {"sprints": [{"name": "x", "goal": "y", "files": ["../etc/passwd", "C:/Windows/x.py"], "acceptance": []}]}
    ok1, w1 = _sc.validate_plan(good, ".")
    ok2, w2 = _sc.validate_plan(bad, ".")
    check("plan check: a plan whose sprints each have an acceptance check and stay inside the project passes; one with files outside "
          "the project and no acceptance check is sent back with plain reasons", ok1 and not ok2 and len(w2) >= 3
          and not _sc.validate_plan({}, ".")[0], (w1, w2))
    import arch_core as core
    import arch_flows as F
    import arch_roles
    import arch_selftest

    home = tempfile.mkdtemp(prefix="archwin_")
    old_home = core.HOME
    core.set_home(home)
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST", "ARCHITECT_EVAL", "ARCHITECT_RETRY_HINT")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ.pop("ARCHITECT_EVAL", None)
    os.environ.pop("ARCHITECT_RETRY_HINT", None)
    script = {"mode": "dates", "sprint": 0, "prompts": [], "fix": 0}

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            if "A coding task failed" in user:
                return json.dumps({"cause": "days_in_month always returns 30, so February is wrong",
                                   "next_try": "use calendar.monthrange for the day count", "fault": "the code"})
            if script["mode"] == "dates":
                if "Plan the work as small SPRINTS" in user:
                    return json.dumps(DATES_PLAN)
                if user.startswith("Implement this sprint"):
                    script["sprint"] += 1
                    script["prompts"].append(user)
                    return {1: TRY1, 2: TRY2}.get(script["sprint"], TRY3)
            if script["mode"] == "never":
                if "Plan the work as small SPRINTS" in user:
                    return json.dumps(DATES_PLAN)
                if user.startswith("Implement this sprint"):
                    return TRY1                                    # always the same half-working answer: the build cannot pass
            if script["mode"] == "calc":
                if "reproduces this bug" in user:
                    return ("FILE: tests/test_repro_divide.py\n```python\n" + T_IMPORT + "from calc import divide\n\n\n"
                            "def test_divide_by_zero():\n    assert divide(1, 0) == 0\n```\n")   # asserts the BUG: passes on the broken code
                if user.startswith("Fix this bug"):
                    script["fix"] += 1
                    return "EDIT: calc.py\n<<<<<<< SEARCH\n    return a / b if b else 0\n=======\n    return a / b\n>>>>>>> REPLACE\n"
            if script["mode"] == "tester" and "Write NEW tests" in user:
                return ("tests/test_calc_extra.py\n```python\n" + T_IMPORT + "\n\ndef test_divide_exact():\n    assert divide(6, 3) == 2\n\n\n"
                        "def test_divide_float():\n    assert divide(1, 4) == 0.25\n\n\ndef test_divide_wrong_belief():\n    assert divide(6, 3) is None\n```\n")
            if script["mode"] == "refactor":
                if "CHARACTERIZATION" in user:
                    return ("FILE: tests/characterization/test_cleaned.py\n```python\nimport os, sys\n"
                            "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))\n"
                            "from dates import cleaned\n\n\ndef test_cleaned():\n    assert cleaned([0, None, 2]) == [0, 2] and cleaned([]) == []\n\n\n"
                            "def test_cleaned_odd():\n    assert cleaned([None, 1, None]) == [1]\n```\n")
                if user.startswith("Refactor as described"):
                    script["fix"] += 1
                    if script["fix"] == 1:
                        return "FILE: dates.py\n```python\n" + DUP.replace("\n\n\n", "\n\n") + "```\n"      # re-spaced, nothing refactored
                    if script["fix"] == 2:                                                                   # renamed, still a copy
                        return "FILE: dates.py\n```python\n" + DUP.replace("_norm_b", "_norm_c") + "```\n"
                    return "FILE: dates.py\n```python\ndef cleaned(xs):\n    return [x for x in xs if x is not None]\n```\n"
            if script["mode"] == "calc_def":
                if "reproduces this bug" in user:
                    return ("DEF: tests/test_calc.py::test_divide_by_zero\n```python\ndef test_divide_by_zero():\n"
                            "    assert divide(1, 0) == 0\n```\n")              # asserts the bug, inside the EXISTING test file
                if user.startswith("Fix this bug"):
                    return "EDIT: calc.py\n<<<<<<< SEARCH\n    return a / b if b else 0\n=======\n    return a / b\n>>>>>>> REPLACE\n"
            if script["mode"] == "calc_cheat":
                if "reproduces this bug" in user:                  # a good repro, but written into the existing tests (seen live)
                    return ("FILE: tests/test_calc.py\n```python\n" + CALC_TESTS + "\n\ndef test_divide_by_zero():\n    import pytest\n"
                            "    with pytest.raises(ZeroDivisionError):\n        divide(1, 0)\n```\n")
                if user.startswith("Fix this bug"):
                    script["fix"] += 1                             # weakens the existing tests AND fixes the code
                    return ("FILE: tests/test_calc.py\n```python\n" + T_IMPORT + "\n\ndef test_nothing():\n    assert True\n```\n\n"
                            "FILE: conftest.py\n```python\ncollect_ignore = [\"tests\"]\n```\n\n"
                            "EDIT: calc.py\n<<<<<<< SEARCH\n    return a / b if b else 0\n=======\n    return a / b\n>>>>>>> REPLACE\n")
            return "ok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    quiet = lambda *_a, **_k: None

    def engine(proj, flow, goal, extra=None):
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                       "behavior": core.deep_merge({"battery_saver": "off", "recheck_before_retry": False, "evaluator": "off",
                                                                                    "fast_tests": "off"}, (extra or {}).get("behavior") or {}),
                                                       "limits": {"max_revisions": 3}})
        return core.Engine(proj, flow, goal, cfg, review="auto", say=quiet)

    try:
        _def_blocks(check, core, home)
        _planning(check, F)
        _best_state(check, F, core, home)
        _review_fixes(check, core, F, home)

        # ---- a larger model a stalled step was handed to cannot answer: the step goes back to the role's own model (seen live:
        # a 14B still loading beside the resident worker gave no first token in 81 s, and that ended the run)
        proj_esc = os.path.join(home, "esc")
        os.makedirs(proj_esc)
        e_esc = engine(proj_esc, "build", "g")
        real_chat = core.stream_chat

        def flaky(model, *a, **k):
            if model == "big:14b":
                raise core.LLMError("No first token from big:14b for 81s, so the call was stopped")
            return real_chat(model, *a, **k)
        core.stream_chat = flaky
        try:
            e_esc.pins["generator"] = "big:14b"
            r_esc = e_esc.call("generator", "say ok", label="esc")
        except core.LLMError as ex:
            r_esc = {"error": str(ex)}
        finally:
            core.stream_chat = real_chat
        check("a larger model that cannot answer hands the step back to the role's own model instead of ending the run",
              r_esc.get("model") == "fake:7b" and "generator" not in e_esc.pins and r_esc.get("text"), r_esc)

        # ---- an empty role uses its OWN tier before a related role's model (seen live: with the planner on a 14B, an empty router
        # borrowed the planner's model through FALLBACK, so every task was routed at 2.5 tokens/s)
        e_t = engine(proj_esc, "build", "g")
        e_t.cfg["roles"] = {r: "" for r in arch_roles.ROLES}
        e_t.cfg["roles"].update(planner="big:14b", evaluator="big:14b")
        e_t.cfg["tiers"] = {"router": "small:1b", "worker": "mid:4b", "heavy": "big:14b"}
        check("an empty role uses its own tier before borrowing a related role's model; a model you assign still wins",
              e_t.model_for("router") == "small:1b" and e_t.model_for("reviewer") == "mid:4b" and e_t.model_for("planner") == "big:14b",
              {r: e_t.model_for(r) for r in ("router", "reviewer", "planner")})
        e_t.cfg["tiers"] = {}
        check("with no tiers set up, an empty role still borrows a related role's model (as before)", e_t.model_for("router") == "big:14b",
              e_t.model_for("router"))

        # ---- a stalled step is handed only to a model that can answer now, by its measured speed here
        import arch_progress as AP
        e_esc.stats["slow:14b"] = {"gen_tok_s": 2.45, "load_s": 30}      # measured on the 6 GB laptop: ~10 min per answer
        e_esc.stats["fast:14b"] = {"gen_tok_s": 40, "load_s": 5}
        slow, fast = AP.ready_now(e_esc, "slow:14b"), AP.ready_now(e_esc, "fast:14b")
        old_host = os.environ["OLLAMA_HOST"]
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        try:
            down = AP.ready_now(e_esc, "fast:14b")
        finally:
            os.environ["OLLAMA_HOST"] = old_host
        check("escalation only to a model that can answer now: one writing 2.5 tokens/s is skipped with the reason in minutes, a fast "
              "one is used, one whose server is down is skipped", not slow[0] and "min" in slow[1] and fast[0] and not down[0], (slow, fast, down))

        # ---- a prompt too big for its model's window is fitted before it is sent (seen live: a sprint filled ~7k of 8192 and the answer overran)
        proj_fit = os.path.join(home, "fit")
        os.makedirs(proj_fit)
        for i in range(12):
            with open(os.path.join(proj_fit, f"mod{i}.py"), "w", encoding="utf-8") as f:
                f.write("".join(f"def fn{i}_{j}(a, b):\n    \"\"\"Add a and b, {j}.\"\"\"\n    return a + b + {j}\n\n\n" for j in range(25)))
        e_fit = engine(proj_fit, "build", "g")
        e_fit.cfg["limits"]["num_ctx"] = 4096
        seen = {}

        def capture(model, msgs, options, *a, **k):
            seen.update(user=msgs[-1]["content"], opts=dict(options))
            if "resp" not in seen:
                seen["resp"] = real_chat(model, msgs, options, *a, **k)
            return dict(seen["resp"])
        big_ctx = e_fit.context("fn", 40000)
        core.stream_chat = capture
        try:
            e_fit.call("generator", "Implement this.\n\nPROJECT CONTEXT:\n" + big_ctx + "\n\nGo.", label="fit")
            u, o1 = seen.get("user", ""), seen.get("opts", {})
            import arch_server
            real_serving = arch_server.serving
            arch_server.serving = lambda m, c=None: {"url": "http://127.0.0.1:1", "ctx": 2048, "tier": "worker"} if m == "fake:7b" else None
            try:
                e_fit2 = engine(proj_fit, "build", "g")
                e_fit2.call("generator", "Say ok.", label="window")
                o2 = seen.get("opts", {})
            finally:
                arch_server.serving = real_serving
        finally:
            core.stream_chat = real_chat
        check("a prompt too big for its window is fitted before it is sent: the project context is cut at a section boundary (least "
              "relevant end first), the task around it is kept, and room is left for the answer",
              len(u) < len(big_ctx) and u.startswith("Implement this.") and u.rstrip().endswith("Go.")
              and "more characters of project context left out" in u and o1.get("num_predict", 0) >= 1024, (len(u), len(big_ctx), o1))
        check("a model served by llama-server gets that server's window, not the bigger number in the settings", o2.get("num_ctx") == 2048, o2)

        # ---- one heavy job at a time (seen live: four at once crashed the laptop): a second one waits and says for what
        import subprocess as _sub
        import arch_heavy
        child_env = dict(os.environ)
        child_env.pop(arch_heavy.HELD, None)
        holder_code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import arch_heavy as h; cm = h.one_at_a_time('selftest', say=lambda *a: None); "
                       "cm.__enter__(); print('held', flush=True); time.sleep(4); cm.__exit__(None, None, None)")
        ch = _sub.Popen([sys.executable, "-c", holder_code, os.path.dirname(os.path.abspath(core.__file__))], env=child_env,
                        stdout=_sub.PIPE, stderr=_sub.STDOUT, text=True)
        first = ch.stdout.readline().strip()
        msgs, refused, got = [], "", False
        saved_held = os.environ.pop(arch_heavy.HELD, None)
        try:
            try:
                with arch_heavy.one_at_a_time("eval", say=msgs.append, wait_s=1, poll=0.2):
                    got = True
            except SystemExit as ex:
                refused = str(ex)
            ch.wait(timeout=30)
            with arch_heavy.one_at_a_time("eval", say=msgs.append, wait_s=10, poll=0.2):
                after = arch_heavy.holder() or {}
                inside = os.environ.get(arch_heavy.HELD)
            os.environ[arch_heavy.HELD] = "selftest"
            with arch_heavy.one_at_a_time("eval", say=msgs.append, wait_s=0, poll=0.2):
                nested = True
        finally:
            os.environ.pop(arch_heavy.HELD, None)
            if saved_held:
                os.environ[arch_heavy.HELD] = saved_held
        check("one heavy job at a time: a second one waits and says which job it waits for, starts once that one ends (its lock is the "
              "operating system's, so a crash leaves none behind), and a job started inside a heavy job never waits on its own lock",
              first == "held" and not got and "selftest" in refused and msgs and "selftest is already running" in msgs[0]
              and after.get("job") == "eval" and after.get("pid") == os.getpid() and inside == "eval" and nested,
              (first, got, refused, msgs[:1], after, inside))

        # ---- the build flow end to end: merge, interface shown, a broken try undone, a DEF block finishes it
        proj = os.path.join(home, "dates")
        os.makedirs(proj)
        e = engine(proj, "build", DATES_GOAL)
        out = F.run_flow(e, "build", {})
        p = script["prompts"]
        rep = open(e.report_path, encoding="utf-8").read() if getattr(e, "report_path", None) and os.path.isfile(e.report_path) else "\n".join(getattr(e, "lines", []) or [])
        src = open(os.path.join(proj, "dates.py"), encoding="utf-8").read() if os.path.isfile(os.path.join(proj, "dates.py")) else ""
        check("build: a one-module request planned as two sprints runs as ONE sprint", p and all("SPRINT 1/1" in x for x in p), [x[:60] for x in p])
        check("build: every sprint prompt shows the interface: the request's names first, the planner's extra ones after",
              p and "INTERFACE OF THE FINISHED CODE" in p[0] and "from dates import days_in_month" in p[0] and "month_name(month)" in p[0], p[0][:600] if p else None)
        check("build: a try that broke the code never stays: it is refused before it lands (it does not parse) or undone after, and "
              "the next try is told so", len(p) >= 3 and ("UNDONE" in p[2] or "would not parse" in p[2]) and "# try2-broken" not in p[2],
              p[2][-900:] if len(p) >= 3 else len(p))
        check("build: the DEF block replaced just days_in_month; is_leap from the first try is kept and the import moved to the top",
              "calendar.monthrange" in src and "def is_leap(year):" in src and src.lstrip().startswith("import calendar") and "try2-broken" not in src, src)
        check("build: and the build passes on the code checks", out.get("ok") is True, out)

        # ---- a failing build writes a post-mortem; the supervisor hands it to the retry
        proj2 = os.path.join(home, "dates-fail")
        os.makedirs(proj2)
        script["mode"] = "never"
        e2 = engine(proj2, "build", DATES_GOAL)
        out2 = F.run_flow(e2, "build", {})
        pm = os.path.join(e2.run_dir, "postmortem.md")
        text = open(pm, encoding="utf-8").read() if os.path.isfile(pm) else ""
        check("a failed run gets a post-mortem in plain words next to it, and a 'postmortem' trace record",
              out2.get("ok") is False and "days_in_month always returns 30" in text and '"type": "postmortem"' in open(os.path.join(e2.run_dir, "trace.jsonl"), encoding="utf-8").read(), text)
        _postmortem_and_supervisor(check, core, home, e2.run_dir, engine)

        os.environ["ARCHITECT_EVAL"] = "1"
        proj3 = os.path.join(home, "dates-eval")
        os.makedirs(proj3)
        e3 = engine(proj3, "build", DATES_GOAL)
        F.run_flow(e3, "build", {})
        os.environ.pop("ARCHITECT_EVAL", None)
        check("never during a benchmark: no post-mortem call, no file", not os.path.isfile(os.path.join(e3.run_dir, "postmortem.md")))
        proj4 = os.path.join(home, "dates-off")
        os.makedirs(proj4)
        e4 = engine(proj4, "build", DATES_GOAL, {"behavior": {"postmortem": "off"}})
        F.run_flow(e4, "build", {})
        check("and never when it is turned off", not os.path.isfile(os.path.join(e4.run_dir, "postmortem.md")))

        # ---- bugfix: a reproduction test that asserts the bug itself is removed, so it cannot fail the real fix
        if core.has_module("pytest"):
            script["mode"] = "calc"
            proj5 = os.path.join(home, "calc")
            os.makedirs(os.path.join(proj5, "tests"))
            for rel, body in (("calc.py", CALC), ("tests/test_calc.py", CALC_TESTS)):
                with open(os.path.join(proj5, rel), "w", encoding="utf-8") as f:
                    f.write(body)
            e5 = engine(proj5, "bugfix", CALC_GOAL)
            out5 = F.FLOWS["bugfix"].run(e5, {})
            check("bugfix: an unusable reproduction test (it passes on the broken code) is removed again",
                  not os.path.exists(os.path.join(proj5, "tests", "test_repro_divide.py")), os.listdir(os.path.join(proj5, "tests")))
            check("bugfix: so the correct fix passes on the first try instead of failing on the harness's own leftover test",
                  out5.get("ok") is True and script["fix"] == 1, (out5, script["fix"]))
            script["mode"], script["fix"] = "calc_def", 0
            proj6 = os.path.join(home, "calc-def")
            os.makedirs(os.path.join(proj6, "tests"))
            for rel, body in (("calc.py", CALC), ("tests/test_calc.py", CALC_TESTS)):
                with open(os.path.join(proj6, rel), "w", encoding="utf-8") as f:
                    f.write(body)
            before = open(os.path.join(proj6, "tests", "test_calc.py"), "rb").read()
            e6 = engine(proj6, "bugfix", CALC_GOAL)
            F.FLOWS["bugfix"].run(e6, {})
            check("bugfix: an unusable repro written INTO an existing test file (a DEF appended to it) is taken out again, byte for byte",
                  open(os.path.join(proj6, "tests", "test_calc.py"), "rb").read() == before, open(os.path.join(proj6, "tests", "test_calc.py"), "rb").read()[-200:])
            script["mode"], script["fix"] = "calc_cheat", 0
            proj6b = os.path.join(home, "calc-cheat")
            os.makedirs(os.path.join(proj6b, "tests"))
            for rel, body in (("calc.py", CALC), ("tests/test_calc.py", CALC_TESTS)):
                with open(os.path.join(proj6b, rel), "w", encoding="utf-8") as f:
                    f.write(body)
            before = open(os.path.join(proj6b, "tests", "test_calc.py"), "rb").read()
            out6b = F.FLOWS["bugfix"].run(engine(proj6b, "bugfix", CALC_GOAL), {})
            check("bugfix: the existing tests cannot be edited, neither to hold the repro nor by the fix (nor switched off by a new "
                  "conftest.py); the code fix alone passes",
                  open(os.path.join(proj6b, "tests", "test_calc.py"), "rb").read() == before and out6b.get("ok") is True
                  and not os.path.exists(os.path.join(proj6b, "conftest.py"))
                  and "return a / b\n" in open(os.path.join(proj6b, "calc.py"), encoding="utf-8").read(), out6b)
            # ---- the test flow keeps the good tests of a half-right file written under a bare path line
            script["mode"] = "tester"
            proj7 = os.path.join(home, "tests-flow")
            os.makedirs(os.path.join(proj7, "tests"))
            for rel, body in (("calc.py", "def divide(a, b):\n    return a / b\n"), ("tests/test_calc.py", CALC_TESTS)):
                with open(os.path.join(proj7, rel), "w", encoding="utf-8") as f:
                    f.write(body)
            e7 = engine(proj7, "test", "Improve the tests for calc.py", {"behavior": {"mutation": "off"}})
            out7 = F.FLOWS["test"].run(e7, {})
            extra = os.path.join(proj7, "tests", "test_calc_extra.py")
            body = open(extra, encoding="utf-8").read() if os.path.isfile(extra) else ""
            check("test flow: a reply that names the file on a bare line (no FILE:) is still written",
                  bool(body), os.listdir(os.path.join(proj7, "tests")))
            check("test flow: the forgotten import is added, the one wrong test dropped, the two sound tests kept, and the flow passes",
                  out7.get("ok") is True and "from calc import divide" in body and "test_divide_float" in body and "wrong_belief" not in body, (out7, body[-300:]))
            # ---- refactor: green tests are not enough; the refactor has to happen
            script["mode"], script["fix"] = "refactor", 0
            proj8 = os.path.join(home, "refactor")
            os.makedirs(os.path.join(proj8, "tests"))
            with open(os.path.join(proj8, "dates.py"), "w", encoding="utf-8") as f:
                f.write(DUP)
            e8 = engine(proj8, "refactor", "Refactor dates.py: the two duplicated helpers _norm_a and _norm_b should become one simple expression")
            out8 = F.FLOWS["refactor"].run(e8, {})
            final = open(os.path.join(proj8, "dates.py"), encoding="utf-8").read()
            check("refactor: a reply that only re-spaced the file, and then one that only renamed a copy, are both refused; the real refactor passes",
                  out8.get("ok") is True and script["fix"] == 3 and "_norm_b" not in final and "_norm_c" not in final, (out8, script["fix"], final))
        else:
            check("bugfix checks need pytest (not installed here): skipped, counted as passed", True)
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
    print(f"\n  {'ALL ' + str(n) + ' WIN-MORE-TASKS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' WIN-MORE-TASKS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _def_blocks(check, core, home):
    src = ('"""Dates."""\nimport os\n\n\ndef is_leap(year):\n    return False\n\n\n@staticmethod\ndef helper():\n    return 1\n\n\n'
           "class Cart:\n    def __init__(self):\n        self.items = {}\n\n    def total(self):\n        return 0\n")
    b = core.parse_blocks("Here you go.\n\nDEF: `dates.py::Cart.total`\n```python\ndef total(self):\n    return 1\n```\n")
    check("a DEF block is parsed: file, target and code", b and b[0]["kind"] == "def" and b[0]["path"] == "dates.py" and b[0]["target"] == "Cart.total", b)
    new, why = core.replace_def(src, "is_leap", "def is_leap(year):\n    return year % 4 == 0\n")
    check("DEF replaces one top-level function and keeps every other line byte for byte",
          not why and "return year % 4 == 0" in new and new.replace("return year % 4 == 0", "return False") == src, why or new)
    new, why = core.replace_def(src, "Cart.total", "def total(self):\n    return sum(self.items.values())\n")
    check("DEF replaces a method, indented to its class (the model may send it unindented)",
          not why and "    def total(self):\n        return sum(self.items.values())" in new, why or new)
    new, why = core.replace_def(src, "Cart.count", "    def count(self):\n        return len(self.items)\n")
    check("a method that is not there yet is added at the end of its class", not why and new.rstrip().endswith("        return len(self.items)") and "def total" in new, why or new)
    new, why = core.replace_def(src, "month_name", "import calendar\nimport os\n\n\ndef month_name(m):\n    return calendar.month_name[m]\n")
    check("a new function is appended, and its new imports go to the file's imports (an import already there is not repeated)",
          not why and new.count("import os") == 1 and new.index("import calendar") < new.index("def is_leap") and new.rstrip().endswith("return calendar.month_name[m]"), why or new)
    new, why = core.replace_def(src, "helper", "@staticmethod\ndef helper():\n    return 2\n")
    check("decorators belong to the definition: replaced with it, not left behind twice", not why and new.count("@staticmethod") == 1 and "return 2" in new, why or new)
    bad = [core.replace_def(src, "is_leap", "def other():\n    pass\n")[1], core.replace_def(src, "is_leap", "def is_leap(:\n")[1],
           core.replace_def(src, "Nope.x", "def x(self):\n    pass\n")[1], core.replace_def("def broken(:\n", "f", "def f():\n    pass\n")[1],
           core.replace_def(src, "is_leap", "def is_leap(y):\n    return 1\nprint('side effect')\n")[1]]
    check("wrong name, code that does not parse, a missing class, a file that does not parse, and extra statements are refused with the reason",
          all(bad), bad)
    proj = os.path.join(home, "defproj")
    os.makedirs(proj)
    with open(os.path.join(proj, "dates.py"), "w", encoding="utf-8") as f:
        f.write(src)
    with open(os.path.join(proj, "page.html"), "w", encoding="utf-8") as f:
        f.write("<p>x</p>\n")
    cfg = core.load_config(proj)
    p = core.prepare_block(proj, cfg, {"kind": "def", "path": "page.html", "target": "x", "code": "def x():\n    pass\n"})
    check("DEF is for Python files only; anything else is told to use FILE or EDIT", p["status"] == "error" and "Python files only" in p["msg"], p)
    p = core.prepare_block(proj, cfg, {"kind": "def", "path": "fresh.py", "target": "f", "code": "def f():\n    return 1\n"})
    check("DEF can create a new module with that one function", p["status"] == "ok" and p["new"] == "def f():\n    return 1\n" and not p["existed"], p)
    e = core.Engine(proj, "build", "g", cfg, review="auto", say=lambda *a, **k: None, persist=False)
    res = e.apply("DEF: dates.py::is_leap\n```python\ndef is_leap(year):\n    return True\n```\n", "t")
    check("and the engine applies it like any other block (diff, backup, changed files)",
          res and res[0]["status"] == "applied" and "return True" in open(os.path.join(proj, "dates.py"), encoding="utf-8").read() and "dates.py" in e.changed_files, res)
    import arch_roles
    check("the edit instructions every file-writing role sees describe DEF blocks", "DEF: relative/path.py::function_name" in arch_roles.EDIT_PROTOCOL
          and "DEF:" in arch_roles.edit_style_note("whole") and "DEF:" in arch_roles.edit_style_note("auto"))
    many = "".join(f"def f{i}(a):\n    return a + {i}\n\n\n" for i in range(10))
    with open(os.path.join(proj, "many.py"), "w", encoding="utf-8") as f:
        f.write(many)
    res = e.apply("FILE: many.py\n```python\ndef f3(a):\n    return a * 3\n```\n", "t")
    body = open(os.path.join(proj, "many.py"), encoding="utf-8").read()
    check("a FILE reply holding only the function it changed is applied as a function edit (seen live: refused as 'would cut 30 to 8 "
          "lines'), every other function kept", res and res[0]["status"] == "applied" and "return a * 3" in body and "def f9(a)" in body
          and body.count("def f") == 10, (res, body[:200]))
    res = e.apply("FILE: many.py\n```python\nx = 1\n\n\ndef f3(a):\n    return 0\n```\n", "t")
    check("but a short FILE that is not only definitions is still refused, as before", res and res[0]["status"] == "error"
          and "would cut" in res[0]["msg"] and "def f9(a)" in open(os.path.join(proj, "many.py"), encoding="utf-8").read(), res)
    check("and so is one that adds a new name (where it belongs is a guess) or puts a placeholder body over a real one",
          core.merge_partial(many, "def brand_new(a):\n    return 0\n\n\ndef f3(a):\n    return brand_new(a)\n") is None
          and core.merge_partial(many, "def f3(a):\n    pass\n") is None)


def _planning(check, F):
    lines = F.interface_lines(DATES_GOAL, DATES_PLAN["interface"] + ["dates.py: days_in_month(month)", "not a signature at all, just prose"])
    check("the interface comes from the request first; the planner adds only names the request does not fix",
          lines[0] == "from dates import is_leap  ->  is_leap(year)" and "dates.py: month_name(month)" in lines
          and "dates.py: days_in_month(month)" not in lines and not any("prose" in x for x in lines), lines)
    check("a request that names no interface still gets the planner's", F.interface_lines("make it nicer", ["app.py: render(page)"]) == ["app.py: render(page)"])
    one = [{"files": ["dates.py", "tests/test_dates.py"]}, {"files": ["dates.py"]}]
    many = [{"files": ["shortener/core.py"]}, {"files": ["shortener/store.py"]}]
    check("one module and its tests is a small request; several modules is not; neither is a plan that is already one sprint",
          F.small_request(DATES_GOAL, one, ["dates.py", "tests/test_dates.py"]) and not F.small_request("x", many, [])
          and not F.small_request(DATES_GOAL, one[:1], []))
    m = F.merge_sprints(DATES_GOAL, DATES_PLAN["sprints"])
    check("merging keeps every file and every acceptance criterion, under the request's own goal",
          len(m) == 1 and m[0]["goal"] == DATES_GOAL and m[0]["files"] == ["dates.py", "tests/test_dates.py"] and [a["id"] for a in m[0]["acceptance"]] == ["A1", "A2"], m)
    check("a request that asks for tests is recognised; one that only mentions a test-like word is not",
          F.wants_tests("Build an inventory CLI using argparse, with tests in tests/.") and F.wants_tests("Create dates.py and its tests")
          and F.wants_tests("use pytest") and not F.wants_tests("Create dates.py with days_in_month(year, month)")
          and not F.wants_tests("make the latest test results page faster") and not F.wants_tests("Build a report with the test results")
          and not F.wants_tests("add test questions to the quiz"))
    check("the first sprint's test file is one the plan already names, else tests/test_<module>.py",
          F.test_file_for([{"files": ["inventory/__init__.py", "inventory/__main__.py"]}]) == "tests/test_inventory.py"
          and F.test_file_for([{"files": ["dates.py"]}, {"files": ["tests/test_dates_x.py"]}]) == "tests/test_dates_x.py"
          and F.test_file_for([{"files": ["src/report.py"]}]) == "tests/test_report.py"
          and F.test_file_for([{"files": ["app.py", "tests/conftest.py", "tests/__init__.py"]}]) == "tests/test_app.py")


def _best_state(check, F, core, home):
    W = F.BestState.worse
    ok = {"broken": False, "lost": False, "passed": 5, "failed": 2, "scope": "all"}
    check("worse: a syntax or import error where the kept version had none", W(dict(ok, broken=True), ok))
    check("worse: a function an earlier sprint built went missing", W(dict(ok, lost=True), ok))
    check("worse: fewer tests pass AND more fail", W(dict(ok, passed=3, failed=4), ok))
    check("not worse: more tests written with some failing, a different test scope, or the same result (ties go to the newest)",
          not W(dict(ok, passed=7, failed=3), ok) and not W(dict(ok, passed=1, failed=6, scope="related"), ok) and not W(dict(ok), ok)
          and not W(ok, dict(ok, broken=True)))
    none = dict(ok, passed=0, failed=0)
    check("not worse: tests that do not load yet, where the kept version had no tests at all (a file that no longer parses still is)",
          not W(dict(none, broken=True), none) and W(dict(none, broken=True, syntax=True), none))
    check("worse: a file that no longer parses, even after a kept version whose tests did not load; and tests that stop loading where "
          "the kept version's tests ran under any runner (unittest prints no pytest summary)",
          W(dict(none, broken=True, syntax=True), dict(none, broken=True)) and W(dict(none, broken=True), dict(none, ran=True)))
    m = F.BestState.measure({"text": "FAILED tests/test_a.py::t\n1 failed, 4 passed in 0.10s", "results": [], "tests": ("fail", "")})
    check("the measure reads pytest's own summary line", m["passed"] == 4 and m["failed"] == 1 and not m["broken"], m)
    proj = os.path.join(home, "best")
    os.makedirs(proj)
    with open(os.path.join(proj, "a.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    e = core.Engine(proj, "build", "g", core.load_config(proj), review="auto", say=lambda *a, **k: None, persist=False)
    bs = F.BestState(e)
    res = e.apply("FILE: a.py\n```python\nx = 2\n```\n", "t1")
    bs.note(res)
    kept = bs.judge({"text": "1 failed, 1 passed in 0.1s", "results": [], "tests": ("fail", "")})
    res = e.apply("FILE: a.py\n```python\nx = (\n```\n\nFILE: b.py\n```python\ny = 1\n```\n", "t2")
    bs.note(res)
    why = bs.judge({"text": "", "results": [{"name": "a.py parses", "ok": False}], "tests": ("fail", "")})
    a = open(os.path.join(proj, "a.py"), encoding="utf-8").read()
    check("a worse try is undone: its edits are reverted to the kept version and the files it created are removed",
          kept == "" and why and a == "x = 2\n" and not os.path.exists(os.path.join(proj, "b.py")), (kept, why, a))


def _review_fixes(check, core, F, home):
    """The findings an adversarial review of this work confirmed (2026-11-03), each pinned so it cannot come back."""
    bare = core.parse_blocks("tests/test_x.py\n```python\ndef test_a():\n    assert 1 == 1\n```\n")
    check("a code block under a line that is just its path counts as a FILE block (only when the reply has no real header)",
          bare and bare[0]["path"] == "tests/test_x.py" and not core.parse_blocks("Here is the plan.\n```python\nx = 1\n```\n")
          and [b["path"] for b in core.parse_blocks("FILE: a.py\n```python\nx = 1\n```\nb.py\n```python\ny = 2\n```\n")] == ["a.py"], bare)
    H = core._def_header
    shapes = ["dates.py::days_in_month(year, month)", "dates.py: def days_in_month", "dates.py#days_in_month", "dates.py::days_in_month:",
              "dates.py:days_in_month"]
    check("DEF headers in the shapes small models write are understood (arguments, def, single colon, #, trailing colon)",
          all(H(s) == ("dates.py", "days_in_month") for s in shapes) and H("dates.py") == ("dates.py", "") and H("page.html::x")[0] == "page.html",
          [H(s) for s in shapes])
    new, why = core.replace_def("def f():\n    return 1\n", "", "def g():\n    return 2\n")
    check("a DEF with no name after the path takes the name from its one definition", not why and "def g" in new and "def f" in new, why)
    cart = "class Cart:\n    def __init__(self):\n        self.items = {}\n\n    def add(self, k):\n        self.items[k] = 1\n\n    def total(self):\n        return 0\n"
    new, why = core.replace_def(cart, "Cart", "class Cart:\n    def total(self):\n        return len(self.items)\n")
    check("a class block that leaves out methods the class has is REFUSED (it would delete them), and the refusal names them and the one-method form",
          new is None and "__init__" in why and "add" in why and "Cart.<method>" in why, why)
    new, why = core.replace_def(cart, "Cart.total", "class Cart:\n    def total(self):\n        return len(self.items)\n")
    check("::Class.method sent wrapped in its class is unwrapped: only that method changes",
          not why and "return len(self.items)" in new and "def add(self, k)" in new and new.count("class Cart") == 1, why or new)
    new, why = core.replace_def(cart, "total", "def total(self):\n    return 99\n")
    check("::method without its class, for a method only one class has, changes that method (no stray top-level copy)",
          not why and "        return 99" in new and "\ndef total" not in new, why or new)
    _, why = core.replace_def(cart, "Cart", "def Cart():\n    return 1\n")
    check("a function sent for a class is refused", "is a class in the file" in (why or ""), why)
    new, why = core.replace_def("def f():\n    return 3\n\n\ndef f():\n    return 4\n", "f", "def f():\n    return 42\n")
    check("a name defined twice: the LAST definition (the one Python uses) is the one replaced",
          not why and new.rstrip().endswith("return 42") and "return 3" in new, new)
    script = "import sys\n\n\ndef main():\n    print(fmt(sys.argv))\n\n\nif __name__ == \"__main__\":\n    main()\n"
    new, why = core.replace_def(script, "fmt", "def fmt(a):\n    return str(a)\n")
    check("a new function goes BEFORE the script's main block, so running the script finds it",
          not why and new.index("def fmt") < new.index('if __name__ == "__main__":'), new)
    loc = "def g():\n    import json\n    return json.dumps(1)\n"
    new, why = core.replace_def(loc, "f", "import json\n\n\ndef f():\n    return json.dumps(2)\n")
    check("an import that exists only inside another function still goes to the top of the file", not why and new.startswith("import json\n"), new)
    multi = "from os.path import (\n    join,\n    exists,\n)\n\n\ndef g():\n    return 1\n"
    new, why = core.replace_def(multi, "f", "from os.path import join\n\n\ndef f(a):\n    return join(a, 'x')\n")
    check("an import already there in another layout (a parenthesised multi-line import) is not added again", not why and new.count("import") == 1, new)

    proj = os.path.join(home, "cap")
    os.makedirs(proj)
    with open(os.path.join(proj, "m.py"), "w", encoding="utf-8") as f:
        f.write("def a():\n    return 0\n\n\ndef b():\n    return 0\n\n\ndef c():\n    return 0\n")
    cfg = core.deep_merge(core.load_config(proj), {"limits": {"max_files_per_step": 1}})
    e = core.Engine(proj, "bugfix", "g", cfg, review="auto", say=lambda *a, **k: None, persist=False)
    res = e.apply("".join(f"DEF: m.py::{n}\n```python\ndef {n}():\n    return 1\n```\n\n" for n in "abc") + "FILE: other.py\n```python\nx = 1\n```\n", "t")
    src = open(os.path.join(proj, "m.py"), encoding="utf-8").read()
    check("the per-step cap counts FILES: three DEF blocks for one file are one file; the extra file is named in the refusal",
          src.count("return 1") == 3 and not os.path.exists(os.path.join(proj, "other.py"))
          and any("other.py" in (r.get("msg") or "") for r in res), [r.get("status") for r in res])
    import arch_progress
    coach = arch_progress.Coach(e, "generator")
    fb = coach.after("FEEDBACK: tests fail\n", "reply", "tests fail", ["m.py"])
    check("a block that was NOT applied is named in the next feedback even when other blocks applied", "NOT applied" in fb and "other.py" in fb, fb)

    B = F.BestState
    ok_contract_bad = {"broken": False, "lost": False, "gates_ok": False, "passed": 5, "failed": 0, "scope": "all"}
    fixes_contract = {"broken": False, "lost": False, "gates_ok": True, "passed": 4, "failed": 1, "scope": "all"}
    check("a try that fixes the request's interface is kept even though one of the model's own tests now fails", B.worse(fixes_contract, ok_contract_bad) == "")
    m = B.measure({"text": "E   AssertionError: expected NameError\n1 failed, 9 passed in 0.2s", "results": [], "tests": ("fail", "")})
    check("an exception NAME in a test failure is not 'broken'; only code or tests that no longer load are", not m["broken"]
          and B.measure({"text": "ERROR collecting tests/test_a.py", "results": [], "tests": ("fail", "")})["broken"], m)
    check("the undo reason names the function that was removed",
          "days_in_month from dates.py" in B.worse(B.measure({"text": "", "results": []}, [("dates.py", "days_in_month")]),
                                                    B.measure({"text": "", "results": []})))
    pc = os.path.join(home, "crlf")
    os.makedirs(pc)
    with open(os.path.join(pc, "a.py"), "wb") as f:
        f.write(b"x = 1\r\ny = 2\r\n")
    e2 = core.Engine(pc, "build", "g", core.load_config(pc), review="auto", say=lambda *a, **k: None, persist=False)
    bs = B(e2)
    bs.note(e2.apply("FILE: a.py\n```python\nx = 2\ny = 2\n```\n", "t1"))
    bs.judge({"text": "1 failed, 1 passed in 0.1s", "results": []}, shown="FULL FEEDBACK with the interface note")
    bs.note(e2.apply("FILE: a.py\n```python\nx = (\n```\n", "t2"))
    bs.judge({"text": "", "results": [{"name": "a.py parses", "ok": False}]})
    raw = open(os.path.join(pc, "a.py"), "rb").read()
    check("an undo restores the kept version byte for byte (CRLF line endings kept), and remembers the FULL feedback it was given",
          raw == b"x = 2\r\ny = 2\r\n" and bs.still_fails() == "FULL FEEDBACK with the interface note", (raw, bs.still_fails()))

    check("a plan without file lists is not 'one module' (a 4-sprint web app must not collapse into one sprint)",
          not F.small_request("Build a blog backend with auth and a UI", [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}, {"goal": "d"}], []))
    many = [{"files": ["d.py"], "acceptance": [{"id": f"A{i}", "text": "x"} for i in range(6)]},
            {"files": ["d.py"], "acceptance": [{"id": f"B{i}", "text": "x"} for i in range(6)]}]
    check("merging keeps EVERY acceptance criterion", len(F.merge_sprints("g", many)[0]["acceptance"]) == 12)
    neg = F.interface_contract("Create dates.py with is_leap(year) and days_in_month(year, month). Do not call monthrange(year, month) from the calendar module.")
    check("a call the request mentions in another sentence, or negated, is not part of the interface",
          [n for _, n in neg["imports"]] == ["is_leap", "days_in_month"], neg)
    dr = []
    lines = F.interface_lines("make it", ["dates.py: days_in_month(year: int, month: int) -> int", "dates.py: def day_of_year(y, m, d):",
                                         "dates.py::weekday(y)", "Store", "plain words"], dropped=dr)
    check("planner entries with types, def/colon or '::' are kept (normalised); bare words are dropped and counted for the report",
          lines == ["dates.py: days_in_month(year: int, month: int) -> int", "dates.py: day_of_year(y, m, d)", "dates.py: weekday(y)"] and dr == ["Store", "plain words"],
          (lines, dr))
    check("a function the request says to remove or rename may go; one the finished interface still needs may not",
          F.lost_defs({"u.py": {"legacy_dump", "load"}}, {"u.py": set()}, "remove legacy_dump and rename load", keep=["from u import load"]) == [("u.py", "load")])


def _postmortem_and_supervisor(check, core, home, run_dir, engine):
    import subprocess
    import arch_postmortem as PM
    import arch_supervisor as S
    d = PM.digest(run_dir)
    check("the post-mortem digest is built by code from the run's own trace: failing checks and rejected replies, each once",
          "check '" in d and "sprint1-try1" in d and len(d) <= 5000, d[:400])
    hint = PM.read(run_dir)
    check("read() gives the next attempt one short paragraph", "days_in_month always returns 30" in hint and "monthrange" in hint and "#" not in hint, hint)

    root = os.path.dirname(os.path.dirname(os.path.dirname(run_dir)))
    rid = os.path.basename(run_dir)
    jid = S.add("make dates work", root, "build", 5, 3)
    seen = {}

    def first(job, say):
        return {"verdict": "fail", "why": "tests fail", "run_id": rid, "flow": "build", "code": 1}

    out = S.run_one(say=lambda *a: None, runner=first)
    nxt = S.claim()
    check("the supervisor attaches the failed attempt's post-mortem to the retry it queues",
          out and out.get("retry") and nxt and nxt["id"] == out["retry"]["id"] and "POSTMORTEM: " in (nxt.get("note") or "") and "monthrange" in nxt["note"],
          (out, nxt and nxt.get("note")))

    real = subprocess.run

    def fake_run(argv, **kw):
        seen.setdefault("envs", []).append(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "", "")

    S.subprocess.run = fake_run
    try:
        S._subprocess_runner(nxt, lambda *a: None)
        S._subprocess_runner(dict(nxt, note="after build ended 'fail': x"), lambda *a: None)
    finally:
        S.subprocess.run = real
    envs = seen.get("envs") or [{}, {}]
    check("and the retry's process is started with it (ARCHITECT_RETRY_HINT); a task without one gets none",
          "monthrange" in envs[0].get("ARCHITECT_RETRY_HINT", "") and "ARCHITECT_RETRY_HINT" not in envs[-1], [sorted(k for k in x if k.startswith("ARCH")) for x in envs])
    os.environ["ARCHITECT_RETRY_HINT"] = "use calendar.monthrange for the day count"
    try:
        proj = os.path.join(home, "hint")
        os.makedirs(proj)
        e = engine(proj, "build", "x")
        cx = e.context("dates")
    finally:
        os.environ.pop("ARCHITECT_RETRY_HINT", None)
    check("the retry's prompts carry it as context, labelled as advice", "an earlier attempt at this same task failed" in cx and "monthrange" in cx, cx[:300])
    check("no post-mortem when the machine was the problem (server unreachable, timed out): a model cannot explain that",
          PM.explain(e, "fail", "llama-server is not reachable at http://127.0.0.1:8082") is None)

    jid2 = S.add("make dates work again", root, "build", 5, 3, note="after build ended 'fail': x\nPOSTMORTEM: use calendar.monthrange")

    def down(job, say):
        return {"verdict": "fail", "why": "model server not reachable", "run_id": "", "flow": "build", "code": 1}

    S.run_one(say=lambda *a: None, runner=down)
    again = S.claim()
    check("a task paused because the machine was not ready keeps the earlier attempt's explanation",
          again and again["id"] == jid2 and "POSTMORTEM: use calendar.monthrange" in (again.get("note") or ""), again and again.get("note"))
    if again:
        S._finish(again["id"], "done", "pass", "", "", 0)
    for j in (jid, nxt and nxt["id"]):
        if j:
            S._finish(j, "done", "pass", "", "", 0)


if __name__ == "__main__":
    sys.exit(run())
