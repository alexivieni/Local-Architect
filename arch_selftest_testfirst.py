"""Self-test for behavior.test_first: the spec extractor (arch_spec.spec) and the acceptance-test step of the build flow.

  * spec() on the four e2e goals finds the interface, the usages and the checkable statements;
  * off (the default) changes nothing: the same prompts and labels as a build with no such setting at all;
  * on: the tester writes tests/test_acceptance.py from the request, it is checked in code, locked for the builder and joins the checks;
  * a wrong acceptance test is dropped ONLY when it is the last failure and the interface check and contract smoke pass.
Scripted model, isolated data folder, no cache. Run: py -3.12 arch_selftest_testfirst.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HEAD = "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nimport pytest\nfrom shortener import Shortener\n\n\n"
GOOD_ACC = HEAD + ("def test_same_url_same_code(tmp_path):\n    s = Shortener(str(tmp_path / 'd.json'))\n    first = s.shorten('https://a.example')\n    assert len(first) == 6 and s.shorten('https://a.example') == first\n\n\n"
                   "def test_bad_url(tmp_path):\n    with pytest.raises(ValueError):\n        Shortener(str(tmp_path / 'd.json')).shorten('ftp://x')\n")
WRONG_ACC = GOOD_ACC + "\n\ndef test_code_is_seven_long(tmp_path):\n    assert len(Shortener(str(tmp_path / 'd.json')).shorten('https://b.example')) == 7\n"
BAD_IMPORT = GOOD_ACC.replace("from shortener import Shortener", "from shortener import Shortener\nfrom shortener.codes import secret_helper")
OWN_FAIL = "def test_own_wrong():\n    assert 1 + 1 == 3\n"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== test first: spec extractor + acceptance tests ==")
    import arch_core as core
    import arch_flows as fl
    import arch_roles
    import arch_selftest
    import arch_spec
    from evals.families import e2e

    # ---- spec() on the four e2e goals
    want = {"shortener": (e2e.SHORT_GOAL, ("shortener", "Shortener"), "s.shorten(url)", "the same url always gets the same code"),
            "todo": (e2e.TODO_GOAL, ("todo_api", "make_server"), "srv = make_server(port)", "Ids start at 1 and increase"),
            "inventory": (e2e.INV_GOAL, None, "python -m inventory --db PATH <command>", "Successful commands exit 0"),
            "report": (e2e.REPORT_GOAL, ("salesreport", "build_report"), "build_report(csv_text)", "Totals are rounded to 2 decimals")}
    for k, (g, imp, use, inv) in want.items():
        sp = arch_spec.spec(g)
        check(f"spec {k}: interface found", arch_spec.has_interface(sp) and (imp is None or imp in sp["interface"]["imports"])
              and (imp is not None or "inventory" in sp["interface"]["mains"]), sp["interface"])
        check(f"spec {k}: usage `{use}` found", use in sp["usages"], sp["usages"])
        check(f"spec {k}: statement '{inv}' found", any(inv in s for s in sp["invariants"]), sp["invariants"])
        check(f"spec {k}: deterministic and JSON-able", json.dumps(arch_spec.spec(g)) == json.dumps(sp))
    sp_short = arch_spec.spec(e2e.SHORT_GOAL)
    check("spec: raises ValueError unless ... is a statement", any("raises ValueError unless" in s for s in sp_short["invariants"]))
    check("spec: inventory exits with status 2 is a statement", any("status 2" in s for s in arch_spec.spec(e2e.INV_GOAL)["invariants"]))
    check("spec: a request with no stated interface states none", not arch_spec.has_interface(arch_spec.spec("make the app faster")))
    check("check_text: a good file passes", arch_spec.check_text(GOOD_ACC, sp_short) == [], arch_spec.check_text(GOOD_ACC, sp_short))
    check("check_text: a name the spec does not state is refused", any("secret_helper" in p for p in arch_spec.check_text(BAD_IMPORT, sp_short)))
    check("check_text: a test that asserts nothing is refused", arch_spec.check_text(HEAD + "def test_x():\n    Shortener('a')\n", sp_short) != [])
    check("check_text: a file that does not parse is refused", arch_spec.check_text("def test(:\n", sp_short) != [])

    # ---- the build flow, scripted
    home = tempfile.mkdtemp(prefix="archtf_")
    core.set_home(home)
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    S = {"acc": GOOD_ACC, "extra": "", "log": []}
    sol = dict(e2e.SHORT_SOLUTION)
    plan = {"sprints": [{"name": "all", "kind": "backend", "goal": "the shortener", "files": sorted(sol),
                         "acceptance": [{"id": "A1", "text": "round trip works"}, {"id": "A2", "text": "bad urls are refused"}]}], "interface": []}

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            S["log"].append((role, user))
            if role == "planner":
                return json.dumps(plan)
            if role == "tester":
                return f"FILE: tests/test_acceptance.py\n```python\n{S['acc']}```\n"
            if role in ("evaluator", "router"):
                return arch_selftest.Fake.script(self, role, user, full, st)
            files = dict(sol)
            files["tests/test_acceptance.py"] = "def test_x():\n    assert True\n"     # the builder tries to rewrite the locked file
            if S["extra"]:
                files["tests/test_own.py"] = S["extra"]
            return "".join(f"FILE: {k}\n```python\n{v}```\n" for k, v in files.items())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"

    def build(tag, setting, acc=GOOD_ACC, extra=""):
        S.update(acc=acc, extra=extra, log=[])
        proj = os.path.join(home, tag)
        os.makedirs(proj)
        open(os.path.join(proj, "conftest.py"), "w").close()
        beh = {"battery_saver": "off", "recheck_before_retry": False, "evaluator": "off", "merge_small_plans": False}
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "behavior": beh,
                                                       "limits": {"max_revisions": 1}})
        if setting is None:
            cfg["behavior"].pop("test_first", None)
        else:
            cfg["behavior"]["test_first"] = setting
        e = core.Engine(proj, "build", e2e.SHORT_GOAL, cfg, review="auto", say=lambda *_a, **_k: None)
        out = fl.BuildFlow().run(e, {"domains": ["api"]})
        tr = [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8")]
        return e, out, proj, tr, list(S["log"])

    def acc_text(proj):
        p = os.path.join(proj, "tests", "test_acceptance.py")
        return open(p, encoding="utf-8").read() if os.path.isfile(p) else None

    try:
        check("the setting defaults to off", core.DEFAULTS["behavior"].get("test_first") == "off")
        _e, out_n, p_n, tr_n, log_n = build("none", None)
        e_o, out_o, p_o, tr_o, log_o = build("off", "off")
        labels = lambda tr: [r.get("label") for r in tr if r.get("type") == "call"]
        check("off: the same prompts as a build without the setting", log_n == log_o and len(log_o) >= 2, (len(log_n), len(log_o)))
        check("off: the same call labels", labels(tr_n) == labels(tr_o), (labels(tr_n), labels(tr_o)))
        check("off: no tester call and no acceptance step", not any(r == "tester" for r, _ in log_o)
              and not any(r.get("gate", "").startswith("acceptance") for r in tr_o if r.get("type") == "check"))
        check("off: the builder's attempt to write test_acceptance.py is NOT blocked", acc_text(p_o) is not None and "assert True" in acc_text(p_o))

        e1, out1, p1, tr1, log1 = build("on-good", "on")
        tester = [u for r, u in log1 if r == "tester"]
        check("on: the tester is asked once, from the request and the spec (no code yet)",
              len(tester) == 1 and "SPEC:" in tester[0] and "from shortener import Shortener" in tester[0] and "PROJECT CONTEXT" not in tester[0])
        check("on: the tester comes before the first sprint", [r for r, _ in log1].index("tester") < [r for r, _ in log1].index("generator"),
              [r for r, _ in log1])
        check("on: the acceptance file is written as the tester sent it", acc_text(p1) == GOOD_ACC, acc_text(p1))
        check("on: it is locked (the builder's rewrite was refused)", "tests/test_acceptance.py" in e1.deny_prefixes and "assert True" not in (acc_text(p1) or ""))
        check("on: the sprint prompt says the file is locked", any("is locked" in u for r, u in log1 if r == "generator"))
        check("on: the step is traced as a check record", any(r.get("type") == "check" and r.get("gate") == "acceptance_tests" and r.get("ok") for r in tr1))
        check("on: a correct build passes with the acceptance tests in its checks", out1["ok"], out1)
        check("on: the acceptance tests ran in the sprint checks", arch_spec.failing_tests(e1) == set())

        e2, out2, p2, tr2, _l2 = build("on-wrong", "on", acc=WRONG_ACC)
        t2 = acc_text(p2) or ""
        check("drop: a wrong acceptance test is dropped when it is the only failure and the contract passes",
              out2["ok"] and "test_code_is_seven_long" not in t2 and "test_same_url_same_code" in t2, (out2, t2[-200:]))
        check("drop: it is recorded", any(r.get("gate") == "acceptance_drop" and r.get("ok") for r in tr2 if r.get("type") == "check")
              and "dropped 1 acceptance test" in open(os.path.join(e2.run_dir, "report.md"), encoding="utf-8").read()
              if os.path.isfile(os.path.join(e2.run_dir, "report.md")) else any(r.get("gate") == "acceptance_drop" for r in tr2))
        check("drop: the builder's own files are untouched", open(os.path.join(p2, "shortener", "core.py"), encoding="utf-8").read() == sol["shortener/core.py"])

        e3, out3, p3, tr3, _l3 = build("on-wrong-own-fail", "on", acc=WRONG_ACC, extra=OWN_FAIL)
        check("no drop: when another test fails too, the acceptance test stays and the build fails",
              not out3["ok"] and "test_code_is_seven_long" in (acc_text(p3) or ""), out3)
        check("no drop: the refusal is traced", any(r.get("gate") == "acceptance_drop" and r.get("ok") is False for r in tr3 if r.get("type") == "check"))

        e4, out4, p4, tr4, l4 = build("on-badimport", "on", acc=BAD_IMPORT)
        check("on: a file importing an unstated name is refused (twice) and the build runs without it",
              acc_text(p4) is not None and "assert True" in acc_text(p4) and sum(1 for r, _ in l4 if r == "tester") == 2
              and "tests/test_acceptance.py" not in e4.deny_prefixes, acc_text(p4))
        # direct check of the drop rule's contract condition: an interface check that fails keeps the test
        core.write_text(os.path.join(p2, "tests", "test_acceptance.py"), WRONG_ACC)
        ok5, why5 = arch_spec.drop_rule(e2, e2e.SHORT_GOAL, fl.verify, lambda e, g: (False, "no"), [], arch_spec.ACCEPTANCE)
        check("no drop: when the interface check fails, nothing is dropped", not ok5 and "interface" in why5
              and "test_code_is_seven_long" in (acc_text(p2) or ""), why5)
    finally:
        srv.shutdown()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    n_ok = sum(results)
    print(f"\n{'ALL ' + str(n_ok) + ' TEST-FIRST CHECKS PASSED' if all(results) else f'{len(results) - n_ok} of {len(results)} TEST-FIRST CHECKS FAILED'}")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
