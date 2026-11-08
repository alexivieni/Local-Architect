"""Self-test: the docs tell the truth about the code, and CI tells the truth about the suites.

- every count the docs state (roles, flows, suites) matches the code (tools/doc_counts.py)
- CLAUDE.md stays a short index: the never-break rules sit in the first 3000 characters (what Engine.context reads of a project's
  CLAUDE.md), every page it names exists, every page under docs/claude/ is named, and every "CLAUDE.md, <section>" a code comment cites
  is found in the index or one of its pages
- tools/ci_parallel reruns a failed suite on its own: passes alone -> LOAD (listed apart, not a failure), fails alone -> FAIL
- doctor says which Python runs the tests
No model, no network.
"""
import contextlib
import glob
import io
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))


def run():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   ({str(detail)[:400]})"))

    import arch_core  # noqa: F401  (a standalone run gets its throwaway home before anything else reads one)
    import doc_counts
    bad = doc_counts.check()
    check("every count the docs state matches the code (py -3.12 tools/doc_counts.py --write fixes them)", not bad, bad)
    c = doc_counts.counts()
    check("the counts are real numbers (roles, flows, flow files, suites)", c["roles"] > 20 and c["flows"] >= c["yaml"] > 10 and c["suites"] > 50, c)

    idx = open(os.path.join(HERE, "CLAUDE.md"), encoding="utf-8").read()
    check("CLAUDE.md is an index, not the manual (under 9000 characters)", len(idx) < 9000, len(idx))
    first = idx[:3000]
    check("the never-break rules are in the first 3000 characters (the part a run on this project reads)",
          "Never break these" in first and all(f"\n{n}. **" in first for n in range(1, 10)), [n for n in range(1, 10) if f"\n{n}. **" not in first])
    named = set(re.findall(r"docs/claude/([\w-]+\.md)", idx))
    pages = {os.path.basename(p) for p in glob.glob(os.path.join(HERE, "docs", "claude", "*.md"))}
    check("every page the index names exists", named and named <= pages, sorted(named - pages))
    check("every page under docs/claude/ is named in the index", pages <= named, sorted(pages - named))
    corpus = (idx + "\n" + "\n".join(open(os.path.join(HERE, "docs", "claude", p), encoding="utf-8").read() for p in pages)).lower()
    cites = set()
    for f in glob.glob(os.path.join(HERE, "*.py")) + glob.glob(os.path.join(HERE, "tools", "*.py")):
        for m in re.finditer(r"CLAUDE\.md, ([A-Za-z][\w ./-]{2,40}?)\)", open(f, encoding="utf-8", errors="replace").read()):
            cites.add(m.group(1).strip())
    lost = sorted(x for x in cites if x.lower() not in corpus)
    check(f"every 'CLAUDE.md, <section>' a code comment cites is found in the index or its pages ({len(cites)} cited)", not lost, lost)
    check("the open issue that is still real is listed (escalation fallback ignores role_hosts)", "## Open issues" in idx and "role_hosts" in idx)
    check("the stale claims are gone (bugfix guard mirrored; KV planned f16 without the flags)",
          "not yet mirrored in" not in corpus and "sizes the kv cache as quantised even when" not in corpus)

    # ---- CI: a failure is rerun alone; passing alone = LOAD, failing alone = FAIL
    import ci_parallel as ci
    tmp = tempfile.mkdtemp(prefix="docs_ci_")
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR",)}
    os.environ["ARCHITECT_DATA_DIR"] = tmp
    real_run, real_suites, real_argv = ci.run, ci.suites, sys.argv
    calls = []

    def fake_run(name, timeout):
        calls.append(name)
        n = calls.count(name)
        rc = 0 if name == "ok_suite" or (name == "load_suite" and n == 2) else 1
        return {"suite": name, "rc": rc, "secs": 0.1, "summary": "ALL 3 X CHECKS PASSED" if rc == 0 else "1 of 3 X CHECKS FAILED", "fails": []}

    try:
        ci.run, sys.argv = fake_run, ["ci_parallel.py", "--jobs", "2"]
        ci.suites = lambda: ["ok_suite", "load_suite"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ci.main()
        rep = json.load(open(sorted(glob.glob(os.path.join(tmp, "ci", "*.json")))[-1], encoding="utf-8"))
        st = {r["suite"]: r["status"] for r in rep}
        check("CI: a suite that fails in parallel but passes alone is LOAD, listed apart, and the run exits 0",
              rc == 0 and st == {"ok_suite": "PASS", "load_suite": "LOAD"} and "failed only under load" in buf.getvalue(), (rc, st))
        calls.clear()
        ci.suites = lambda: ["ok_suite", "real_fail"]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ci.main()
        rep = json.load(open(sorted(glob.glob(os.path.join(tmp, "ci", "*.json")))[-1], encoding="utf-8"))
        st = {r["suite"]: r["status"] for r in rep}
        check("CI: a suite that fails alone too stays FAIL and the run exits 1", rc == 1 and st.get("real_fail") == "FAIL" and calls.count("real_fail") == 2, (rc, st, calls))
        calls.clear()
        sys.argv = ["ci_parallel.py", "--no-rerun"]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ci.main()
        check("CI: --no-rerun runs each suite once", rc == 1 and calls.count("real_fail") == 1, calls)
    finally:
        ci.run, ci.suites, sys.argv = real_run, real_suites, real_argv
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    import arch_doctor
    d = arch_doctor.check_test_python()
    check("doctor says which Python runs the tests (and how to run a suite by hand when it is another one)",
          d["name"] == "Python for tests" and d["advisory"] and ("runs the tests" in d["detail"] or "tests run with" in d["detail"] or "no installed Python" in d["detail"]), d)
    check("doctor's run() includes that line", any(x["name"] == "Python for tests" for x in arch_doctor.run(tempfile.mkdtemp())["checks"]))

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' DOCS/CI CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' DOCS/CI CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
