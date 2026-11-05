"""Self-test for arch_triage: real failure texts from this machine's eval reports land in the right family.

    python arch_selftest_triage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    import arch_triage as T
    J = lambda t, ok, reason="", could=True: {"type": t, "ok": ok, "reason": reason, "could_run": could}
    cases = [
        ("a pass has no family", {"verdict": "pass"}, None),
        ("cannot import name -> wiring", {"verdict": "fail", "judges": [J("python_assert", False, "ImportError: cannot import name 'build_report' from 'salesreport'")]}, "wiring"),
        ("missing __main__.py -> wiring", {"verdict": "fail", "judges": [J("file_exists", False, "missing: salesreport/__main__.py")]}, "wiring"),
        ("constructor arity -> wiring", {"verdict": "fail", "judges": [J("python_assert", False, "TypeError: Shortener.__init__() takes 1 positional argument but 2 were given")]}, "wiring"),
        ("context overflow -> environment", {"verdict": "fail", "error": "Context size has been exceeded.", "judges": []}, "environment"),
        ("server loading -> environment", {"verdict": "fail", "error": "llama-server said: Loading model", "judges": [J("python_assert", False, "AssertionError")]}, "environment"),
        ("hidden passed, own tests failed -> tests", {"verdict": "fail", "judges": [J("python_assert", True), J("tests_pass", False, "1 failed, 4 passed")]}, "tests"),
        ("a refusal -> refusal", {"verdict": "fail", "judges": [J("no_refusal", False, "declined: I can't help with that")]}, "refusal"),
        ("an assertion -> behaviour", {"verdict": "fail", "judges": [J("python_assert", False, "AssertionError: same url must give the same code")]}, "behaviour"),
        ("a timeout -> timeout", {"verdict": "fail", "judges": [J("tests_pass", False, "Command timed out")]}, "timeout"),
    ]
    results = []
    for name, r, want in cases:
        got = T.classify(r)
        results.append(got == want)
        print(f"  {'PASS' if got == want else 'FAIL'}  {name}" + ("" if got == want else f"   (got {got})"))
    ok = T.summarize([c[1] for c in cases]) == {"wiring": 3, "environment": 2, "tests": 1, "refusal": 1, "behaviour": 1, "timeout": 1}
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  summarize counts failures by family")
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' TRIAGE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' TRIAGE CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
