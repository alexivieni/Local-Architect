"""Self-test: the test flow's output keeps its own name. A model that writes passing tests under another file name gets them moved to
tests/test_<name>_extra.py (seen live: every failed `test` task ended with passing tests and no such file).

    python arch_selftest_testname.py
"""
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    home = tempfile.mkdtemp(prefix="testname_")
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    try:
        import arch_core as core
        import arch_flows  # noqa: F401  (the app loads it first; arch_flows_pro imports from it)
        import arch_flows_pro as P
        root = os.path.join(home, "proj")
        os.makedirs(os.path.join(root, "tests"))
        open(os.path.join(root, "cart.py"), "w", encoding="utf-8").write("def total(xs):\n    return sum(xs)\n")
        open(os.path.join(root, "tests", "test_cart.py"), "w", encoding="utf-8").write("from cart import total\n\ndef test_one():\n    assert total([1]) == 1\n")
        e = core.Engine(root, "test", "improve the tests for cart.py", review="auto", say=lambda *a, **k: None, persist=True)
        reply = ("FILE: tests/test_cart_new.py\n```python\nfrom cart import total\n\n\ndef test_empty():\n    assert total([]) == 0\n\n\n"
                 "def test_many():\n    assert total([1, 2, 3]) == 6\n```\n")
        e.call = lambda *a, **k: {"id": 1, "text": reply}
        f = P.TestFlow()
        out = f._write(e, ["cart.py"], "improve the tests for cart.py", "tests/test_cart_extra.py", [], "write")
        check("passing tests written under another name end up as the flow's own output file",
              out == ["tests/test_cart_extra.py"] and os.path.isfile(os.path.join(root, "tests", "test_cart_extra.py"))
              and not os.path.exists(os.path.join(root, "tests", "test_cart_new.py")), out)
        st, _ = core.run_tests(root, e.cfg)
        check("and the suite still passes after the move", st == "pass", st)
        recs = []
        for t in glob.glob(os.path.join(root, ".arch", "runs", "*", "trace.jsonl")):
            recs += [json.loads(x) for x in open(t, encoding="utf-8") if x.strip()]
        check("the move is traced as an assist (kept out of training data)", any(r.get("type") == "assist" for r in recs))
        os.remove(os.path.join(root, "tests", "test_cart_extra.py"))
        reply2 = reply.replace("tests/test_cart_new.py", "tests/test_cart_extra.py")
        e.call = lambda *a, **k: {"id": 2, "text": reply2}
        out2 = f._write(e, ["cart.py"], "improve the tests for cart.py", "tests/test_cart_extra.py", [], "write")
        check("a reply that already uses the right name is left as it is", out2 == ["tests/test_cart_extra.py"], out2)

        # the refactor flow's named merge (live: models kept both _norm_a and _norm_b, only changing a body)
        import arch_flows as F
        check("the refactor request's own words name the pair to merge",
              F.merge_names("the two duplicated helpers _norm_a and _norm_b should become one simple expression") == [("_norm_a", "_norm_b")]
              and F.merge_names("merge `load` and `read` into one") == [("load", "read")] and F.merge_names("make it faster and cleaner") == [])
        open(os.path.join(root, "dates.py"), "w", encoding="utf-8").write("def _norm_a(x):\n    return x\n\n\ndef _norm_b(x):\n    return x + 0\n")
        both = F.still_both(root, ["dates.py"], [("_norm_a", "_norm_b")])
        open(os.path.join(root, "dates.py"), "w", encoding="utf-8").write("def _norm_a(x):\n    return x\n")
        check("a try that keeps both names is not done (even with different bodies); one name left is",
              both == [("_norm_a", "_norm_b", "dates.py")] and F.still_both(root, ["dates.py"], [("_norm_a", "_norm_b")]) == [], both)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' TEST-NAME CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' TEST-NAME CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
