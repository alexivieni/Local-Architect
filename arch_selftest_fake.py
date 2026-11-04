"""Self-test for the fake-code detector, its gate in the flows, and the purge flow, using a scripted fake model."""
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

REAL_TEST = "import pay\n\n\ndef test_fee_is_five_percent():\n    assert pay.fee(200) == 10\n"
EMPTY_TEST = "import pay\n\n\ndef test_charge_works():\n    pay.charge(5)\n"
STUB_SRC = "def fee(amount):\n    pass\n\n\ndef charge(amount):\n    print('charging', amount)\n    return True\n"
GOOD_SRC = ("def fee(amount):\n    return round(amount * 0.05, 2)\n\n\ndef charge(amount):\n    if amount <= 0:\n        raise ValueError('amount must be positive')\n"
            "    return {'charged': amount, 'fee': fee(amount)}\n")
GOOD_TESTS = ("import pay\nimport pytest\n\n\ndef test_charge_works():\n    assert pay.charge(5) == {'charged': 5, 'fee': 0.25}\n\n\n"
              "def test_charge_rejects_zero():\n    with pytest.raises(ValueError):\n        pay.charge(0)\n")


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_fakecode as fc
    import arch_flows as fl
    import arch_roles as roles
    import arch_selftest as base

    class PurgeFake(base.Fake):
        attempts = 0

        def script(self, role, user, full, st):
            if "FAKE CODE" in user and "FINDINGS:" in user:
                PurgeFake.attempts += 1
                if PurgeFake.attempts == 1:        # a bad "fix": delete the real test and leave the stubs
                    return "FILE: tests/test_pay.py\n```python\nimport pay\n\n\ndef test_charge_works():\n    pay.charge(5)\n```\n"
                return (f"FILE: pay.py\n```python\n{GOOD_SRC}```\n\nFILE: tests/test_pay.py\n```python\n{GOOD_TESTS}```\n")
            return super().script(role, user, full, st)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), PurgeFake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    tmp = tempfile.mkdtemp(prefix="archfake_")
    quiet = lambda m: None
    try:
        def rules(rel, src):
            return {x["rule"] for x in fc.scan_text(rel, src)}
        check("an empty function body is a stub", "stub_body" in rules("a.py", "def f(x):\n    pass\n"))
        check("a docstring-only function is a stub", "stub_body" in rules("a.py", 'def f(x):\n    """Does a thing."""\n'))
        check("a function that only logs and returns True is flagged as pretending to work", "pretends_to_work" in rules("a.py", "def send_email(to):\n    print('sent')\n    return True\n"))
        check("real code is not flagged", not {"stub_body", "pretends_to_work", "constant_stub"} & rules("a.py", "def add(a, b):\n    return a + b\n\ndef send_email(to):\n    smtp.send(to)\n    return True\n"))
        check("a silenced log_message override is exempt, but a module-level function of that name and other empty methods are not",
              not rules("a.py", "class H:\n    def log_message(self, *a):\n        pass\n")
              and "stub_body" in rules("a.py", "def log_message(x):\n    pass\n")
              and "stub_body" in rules("a.py", "class H:\n    def charge(self, *a):\n        pass\n"))
        check("abstract methods, Protocols, overloads, dunder hooks and base-class templates are exempt", not rules("a.py",
              "from abc import ABC, abstractmethod\nfrom typing import Protocol\nclass A(ABC):\n    @abstractmethod\n    def run(self): pass\nclass P(Protocol):\n    def go(self) -> int: ...\n"
              "class Thing:\n    def __enter__(self):\n        pass\nclass PluginBase:\n    def hook(self):\n        pass\nclass MyError(Exception):\n    pass\n"))
        check("the allow pragma silences an advisory placeholder marker but never a stub or an empty test",
              not rules("a.py", "x = 'lorem ipsum'  # fakecode: allow (documentation example)\n") and "placeholder_text" in rules("a.py", "x = 'lorem ipsum'\n")
              and "stub_body" in rules("a.py", "def f():  # fakecode: allow\n    pass\n")
              and "test_no_assertion" in rules("tests/test_a.py", "def test_x():  # fakecode: allow\n    f(1)\n"))
        check("a test that asserts nothing is flagged", "test_no_assertion" in rules("tests/test_a.py", "def test_x():\n    f(1)\n"))
        check("a test with assert, pytest.raises, unittest asserts, or a check_ helper is accepted", not rules("tests/test_a.py",
              "import pytest\ndef test_a():\n    assert f(1) == 2\ndef test_b():\n    with pytest.raises(ValueError):\n        f(0)\ndef test_c(self):\n    self.assertEqual(f(1), 2)\ndef test_d():\n    check_result(f(1))\n"))
        check("assert True and x == x are flagged as always true", "trivial_assertion" in rules("tests/test_a.py", "def test_a():\n    assert True\n") and "trivial_assertion" in rules("tests/test_a.py", "def test_a():\n    x = f()\n    assert x == x\n"))
        check("a function that ignores its parameters and returns a constant is flagged (advisory)", [x for x in fc.scan_text("a.py", "def fee(amount):\n    return 5\n") if x["rule"] == "constant_stub" and not x["gate"]])
        check("a broad except that swallows every error is flagged (advisory)", [x for x in fc.scan_text("a.py", "def f():\n    try:\n        g()\n    except Exception:\n        pass\n") if x["rule"] == "swallowed_error" and not x["gate"]])
        check("mock libraries outside tests are flagged, inside tests are not", "mock_in_production" in rules("a.py", "from unittest.mock import Mock\n") and "mock_in_production" not in rules("tests/test_a.py", "from unittest.mock import Mock\n"))
        check("placeholder markers are found in comments and strings, not in identifiers", "placeholder_text" in rules("a.py", "x = 1  # TODO wire this up\n") and "placeholder_text" not in rules("a.py", "todo_list = []\n"))
        check("a file that does not parse is skipped, not crashed on", fc.scan_text("a.py", "def (:\n") == [])

        # ---- gate semantics
        proj = os.path.join(tmp, "proj")
        os.makedirs(os.path.join(proj, "tests"))
        open(os.path.join(proj, "pay.py"), "w").write(STUB_SRC)
        open(os.path.join(proj, "tests", "test_pay.py"), "w").write(REAL_TEST + "\n\n" + EMPTY_TEST.split("\n\n\n", 1)[1])
        r_fail = fc.check(proj, ["pay.py"], "fail")
        check("fail mode: gate-level findings fail the check and say how to fix them", r_fail and not r_fail[0]["ok"] and "Implement it for real" in r_fail[0]["detail"], r_fail)
        check("warn mode: same findings, never fails", fc.check(proj, ["pay.py"], "warn")[0]["ok"] is True and fc.check(proj, ["pay.py"], "off") == [])
        e = core.Engine(proj, "build", "x", core.load_config(proj), review="auto", say=quiet, persist=False)
        e.cfg["roles"] = {r: "fake:7b" for r in roles.ROLES}
        v = fl.verify(e, ["pay.py"], run_tests=False)
        check("legacy stubs in a file this run did not touch do not fail a step", v["ok"], v["text"])
        e.changed_files.append("pay.py")
        v = fl.verify(e, ["pay.py"], run_tests=False)
        check("the same stubs in a file the run changed DO fail the step, naming the finding", not v["ok"] and "stub_body" in v["text"], v["text"])
        e.cfg["behavior"]["fake_code"] = "off"
        check("behavior.fake_code=off disables the gate", fl.verify(e, ["pay.py"], run_tests=False)["ok"])

        # ---- purge flow end to end
        check("purge is a registered flow and the router picks it for fake-code requests",
              "purge" in fl.FLOWS and fl.route(e, "remove the fake code and placeholder stubs in pay.py")[0] == "purge")
        import arch_flows_fake as ff
        before_real = ff.real_tests(proj, ["tests/test_pay.py"])
        e2 = core.Engine(proj, "purge", "Remove fake code in pay.py", core.load_config(proj), review="auto", say=quiet)
        e2.cfg["roles"] = {r: "fake:7b" for r in roles.ROLES}
        e2.cfg["limits"]["max_revisions"] = 3
        out = fl.run_flow(e2, "purge", {})
        calls = PurgeFake.attempts
        after_src = open(os.path.join(proj, "pay.py")).read()
        check("purge: a 'fix' that deletes a real test is rejected, the retry is accepted", calls == 2 and out["ok"], (calls, out))
        check("purge: the stubs are really implemented and the scanner agrees", "round(amount * 0.05, 2)" in after_src and not fc.scan(proj, ["pay.py", "tests/test_pay.py"]), fc.scan(proj, ["pay.py", "tests/test_pay.py"]))
        check("purge: no real test was lost (and one was added)", ff.real_tests(proj, ["tests/test_pay.py"]) >= before_real, (before_real, ff.real_tests(proj, ["tests/test_pay.py"])))
        check("purge writes docs/fakecode.md with before and after", os.path.isfile(os.path.join(proj, "docs", "fakecode.md")) and "## After: 0" in open(os.path.join(proj, "docs", "fakecode.md")).read())
        # the CLI
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fc.main(["--root", proj])
        check("`fakecode` exits 0 on a clean project", rc == 0 and "0 gate-level" in buf.getvalue(), buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fc.main(["--root", os.path.join(tmp, "dirty")]) if os.path.isdir(os.path.join(tmp, "dirty")) else 0
        os.makedirs(os.path.join(tmp, "dirty"))
        open(os.path.join(tmp, "dirty", "x.py"), "w").write("def f():\n    pass\n")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = fc.main(["--root", os.path.join(tmp, "dirty")])
        check("`fakecode` exits 1 when there are gate-level findings", rc == 1)
    finally:
        if old is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = old
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' FAKE-CODE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' FAKE-CODE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
