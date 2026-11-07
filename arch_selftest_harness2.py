"""Self-test for the second harness set: error-driven repair (arch_repair), diff records in the trace (core.diff_record), git checkpoints
(arch_checkpoint) and the ARCHITECT.md project memory (arch_memory). No model, no network; git is used only in throwaway folders.
Run: python arch_selftest_harness2.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TB = '''============ FAILURES ============
Traceback (most recent call last):
  File "{root}/tests/test_calc.py", line 5, in test_half
    assert half(3) == 1.5
  File "{root}/calc.py", line 9, in half
    return x // 2
AssertionError: assert 1 == 1.5
'''


def run():
    results = []

    def check(name, cond, detail=None):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f"  -> {str(detail)[:500]}"))

    tmp = tempfile.mkdtemp(prefix="arch_h2_")
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    try:
        import arch_checkpoint as ck
        import arch_core as core
        import arch_memory as mem
        import arch_repair as rp

        def mk(name, files):
            root = os.path.join(tmp, name)
            for rel, text in files.items():
                p = os.path.join(root, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
            return root

        calc = "import math\n\n\ndef add(a, b):\n    return a + b\n\n\ndef half(x):\n    return x // 2\n\n\ndef other():\n    return 'unrelated'\n"
        proj = mk("proj", {"calc.py": calc, "tests/test_calc.py": "from calc import half\n\n\ndef test_half():\n    assert half(3) == 1.5\n"})

        # ---- 1. error-driven repair
        fb = "FEEDBACK: after your last change this still fails:\n" + "noise line\n" * 300 + TB.format(root=proj.replace("\\", "/"))
        tail = rp.failure_tail(fb)
        check("failure tail starts at the traceback and keeps the assertion", tail.startswith("Traceback") and "AssertionError" in tail and "noise" not in tail, tail[:120])
        check("no test failure in the text -> no tail", rp.failure_tail("none of your changes could be applied") == "")
        funcs = rp.functions_under_test(proj, fb)
        check("function under test is found from the traceback, tests excluded",
              [f[0] for f in funcs] == ["calc.py"] and "def half" in funcs[0][2] and "unrelated" not in funcs[0][2], funcs)

        class Stub:
            def __init__(self, on):
                self.cfg = {"behavior": {"repair_focus": "on" if on else "off"}}
                self.root = proj
                self.traced = []

            def _trace(self, r):
                self.traced.append(r)

        goal = "half(x) must return a float: half(3) == 1.5"
        out = rp.focused(Stub(True), goal, fb, ["calc.py"])
        check("focused retry: contract first, function, failure tail; far smaller than the feedback",
              out and out[1].startswith("=== the request's contract") and "def half" in out[1] and "AssertionError" in out[0]
              and len(out[0]) + len(out[1]) < len(fb) // 2 and "import math" not in out[1], out and (len(out[0]), len(out[1]), len(fb)))
        check("setting off -> nothing changes", rp.focused(Stub(False), goal, fb, []) is None)
        check("apply-error feedback is left alone", rp.focused(Stub(True), goal, "FEEDBACK: nothing could be applied:\n- a.py: bad", []) is None)
        a = rp.focused(Stub(True), goal, fb, [])[1]
        b = rp.focused(Stub(True), goal, fb.replace("1 == 1.5", "2 == 1.5"), [])[1]
        check("prefix-stable: the context is identical while the code is, only the failure tail differs", a == b)

        # ---- 1b. test or code? (the run's own wrong test)
        wproj = mk("wc", {"wc.py": "def word_count(t):\n    return len(t.split())\n",
                          "tests/test_wc.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
                                              "from wc import word_count\n\n\ndef test_wc():\n    assert word_count('hello world  test') == 4\n"})
        pr = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "tests", "-p", "no:cacheprovider"], cwd=wproj, capture_output=True, text=True)
        wfb = "FEEDBACK: your last change failed these checks:\n" + pr.stdout

        class TStub(Stub):
            def __init__(self, on, created=()):
                super().__init__(on)
                self.root = wproj
                self.created, self.changed_files = list(created), []

        st = TStub(True, ["tests/test_wc.py"])
        out = rp.focused(st, "word_count counts words", wfb, ["tests/test_wc.py"])
        check("test_or_code: the only failure is in a test this run created -> the prompt names the actual value and asks test-or-code",
              out and "TEST OR CODE" in out[0] and "assert 3 == 4" in out[0] and "ONLY that side" in out[0], out and out[0][-700:])
        with open(os.path.join(wproj, "tests", "test_wc.py"), "w", encoding="utf-8", newline="\n") as f:      # the fake decides: the TEST was wrong
            f.write("import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
                    "from wc import word_count\n\n\ndef test_wc():\n    assert word_count('hello world  test') == 3\n")
        check("...the decision is recorded in the trace as repair/test_or_code/test",
              rp.record_decision(st, ["tests/test_wc.py"]) == "test" and any(r.get("mode") == "test_or_code" and r.get("decision") == "test" for r in st.traced)
              and any(r.get("mode") == "test_or_code" and "tests" in r for r in st.traced), st.traced)
        pr2 = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"], cwd=wproj, capture_output=True, text=True)
        check("...and fixing the test side makes the run pass", pr2.returncode == 0, pr2.stdout[-300:])
        check("a failing test the run did NOT write keeps the normal repair prompt",
              "TEST OR CODE" not in (rp.focused(TStub(True, []), "g", wfb, [])[0]))
        check("a failure in project code (not only tests) is not test_or_code", rp.test_or_code(st, fb, ["calc.py"]) is None)
        st2 = TStub(True, ["tests/test_wc.py"])
        rp.focused(st2, "g", wfb, ["tests/test_wc.py"])
        check("a code-side change is recorded as decision=code", rp.record_decision(st2, ["wc.py"]) == "code" and st2.traced[-1]["decision"] == "code")
        check("with behavior.repair_focus off there is no test_or_code step", rp.focused(TStub(False, ["tests/test_wc.py"]), "g", wfb, []) is None)

        # ---- 2. diff records
        d = core.unified("a\nb\nc\n", "a\nB\nc\nd\n", "x.txt")
        rec = core.diff_record("x.txt", d, True)
        check("diff record: type, file, hunks and +/- counts", rec["type"] == "diff" and rec["file"] == "x.txt" and rec["n_hunks"] == 1
              and rec["added"] == 2 and rec["removed"] == 1 and "+B" in rec["diff"], rec)
        cfg = core.deep_merge(core.load_config(proj), {"roles": {}, "behavior": {"battery_saver": "off"}})
        e = core.Engine(proj, "bugfix", "fix half", cfg, review="auto", say=lambda *_a, **_k: None)
        e.apply("FILE: calc.py\n```python\n" + calc.replace("x // 2", "x / 2") + "```\n", "t")
        tr = [json.loads(l) for l in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8") if l.strip()]
        dr = [r for r in tr if r.get("type") == "diff"]
        check("apply writes one diff record to the trace", len(dr) == 1 and dr[0]["file"] == "calc.py" and dr[0]["added"] == 1 and dr[0]["removed"] == 1, dr)
        with open(os.path.join(proj, "calc.py"), encoding="utf-8") as f:
            check("apply itself is unchanged (file written)", "x / 2" in f.read())

        # ---- 3. checkpoints: git repo and shadow repo
        if shutil.which("git"):
            def git(root, *a):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), cwd=root, capture_output=True, text=True)

            gp = mk("gitproj", {"a.py": "v0\n", "keep.txt": "mine\n"})
            git(gp, "init", "-q")
            git(gp, "add", "-A")
            git(gp, "commit", "-qm", "init")
            head0 = git(gp, "rev-parse", "HEAD").stdout.strip()
            branch0 = git(gp, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            with open(os.path.join(gp, "a.py"), "w", newline="\n") as f:
                f.write("v1\n")
            with open(os.path.join(gp, "keep.txt"), "a", newline="\n") as f:      # an unrelated edit of the person's, not staged
                f.write("dirty\n")
            git(gp, "add", "keep.txt")                                            # ... and staged: the index must stay as is
            idx0 = git(gp, "diff", "--cached", "--name-only").stdout
            r1 = ck.snapshot(gp, "run1", 1, ["a.py"])
            with open(os.path.join(gp, "a.py"), "w", newline="\n") as f:
                f.write("v2\n")
            r2 = ck.snapshot(gp, "run1", 2, ["a.py", "new.py"])
            check("checkpoints are made", r1.get("ok") and r2.get("ok") and r1["ref"] == "refs/architect/run1/1", (r1, r2))
            check("person's branch, HEAD and index are untouched",
                  git(gp, "rev-parse", "HEAD").stdout.strip() == head0 and git(gp, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch0
                  and git(gp, "diff", "--cached", "--name-only").stdout == idx0)
            check("refs live under refs/architect/", "refs/architect/run1/2" in git(gp, "for-each-ref", "--format=%(refname)").stdout)
            check("list_checkpoints", [c["try"] for c in ck.list_checkpoints(gp, "run1")] == [1, 2])
            cmp_ = ck.compare(gp, "run1", 1, 2)
            check("compare shows the attempt-to-attempt diff", cmp_.get("ok") and "-v1" in cmp_["diff"] and "+v2" in cmp_["diff"], cmp_)
            rs = ck.restore(gp, "run1", 1)
            with open(os.path.join(gp, "a.py"), encoding="utf-8") as f:
                a_now = f.read()
            with open(os.path.join(gp, "keep.txt"), encoding="utf-8") as f:
                k_now = f.read()
            check("undo restores that attempt's files only", rs.get("ok") and a_now == "v1\n" and k_now == "mine\ndirty\n" and "a.py" in rs["restored"], (rs, a_now, k_now))
            check("no stray index file left behind", not os.path.exists(os.path.join(gp, ".arch", "ckpt", "index")))

            sp = mk("shadowproj", {"s.py": "s1\n"})
            s1 = ck.snapshot(sp, "r", 1, ["s.py"])
            with open(os.path.join(sp, "s.py"), "w", newline="\n") as f:
                f.write("s2\n")
            s2 = ck.snapshot(sp, "r", 2, ["s.py"])
            check("non-git project: hidden shadow repo under .arch/ckpt, no .git in the project",
                  s1.get("ok") and s2.get("ok") and s1["shadow"] and os.path.isdir(os.path.join(sp, ".arch", "ckpt", "git")) and not os.path.exists(os.path.join(sp, ".git")), (s1, s2))
            ck.restore(sp, "r", 1)
            with open(os.path.join(sp, "s.py"), encoding="utf-8") as f:
                check("shadow undo restores the file", f.read() == "s1\n")
            check("a bad file name cannot leave the project", ck.snapshot(sp, "r", 3, ["../evil.txt"]).get("files") == [])
        else:
            print("  [SKIP] git is not installed: checkpoint checks skipped")

        class E2:
            cfg = {"behavior": {"checkpoints": "off"}}
            root = proj
            run_id = "x"
            changed_files = []

            def _trace(self, r):
                return None
        check("checkpoints off -> the hook does nothing", ck.attempt(E2(), 1, ["calc.py"]) is None)

        # ---- 4. ARCHITECT.md
        pm = mk("memproj", {"x.py": "pass\n"})
        check("no ARCHITECT.md -> prompt memory has no notes block", "ARCHITECT.md" not in mem.for_prompt(pm)[0])
        with open(os.path.join(pm, "ARCHITECT.md"), "w", encoding="utf-8") as f:
            f.write("# Notes\nUse tabs. " + "x" * 3000 + "\nTAILMARK")
        text = mem.for_prompt(pm)[0]
        check("ARCHITECT.md is read into the stable prefix, top 1500 chars only", text.startswith("Project notes (ARCHITECT.md):\n# Notes") and "TAILMARK" not in text and len(text) < 1700, len(text))
        check("setting off drops it", "ARCHITECT.md" not in mem.for_prompt(pm, architect_md=False)[0])
        before = open(os.path.join(pm, "ARCHITECT.md"), encoding="utf-8").read()
        pr = mem.propose(pm, "Run the tests with python -m pytest -q", "run9")
        check("a learned line is only PROPOSED", pr.get("ok") and mem.proposed(pm) == ["Run the tests with python -m pytest -q"]
              and open(os.path.join(pm, "ARCHITECT.md"), encoding="utf-8").read() == before)
        check("proposals refuse secrets and dedupe", not mem.propose(pm, "password = hunter2hunter2")["ok"] and mem.propose(pm, "Run the tests with python -m pytest -q").get("duplicate"))
        ac = mem.accept(pm, 1)
        body = open(os.path.join(pm, "ARCHITECT.md"), encoding="utf-8").read()
        check("accept moves the line into ARCHITECT.md and clears it from proposed", ac.get("ok") and "## Learned" in body and "- Run the tests with python -m pytest -q" in body and mem.proposed(pm) == [])
        check("accept of an unknown line fails", not mem.accept(pm, 5)["ok"])
    except Exception as ex:
        import traceback
        traceback.print_exc()
        check(f"no crash ({type(ex).__name__}: {ex})", False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n, ok = len(results), sum(results)
    print(f"\n  {'ALL ' + str(n) + ' HARNESS2 CHECKS PASSED' if ok == n else str(n - ok) + ' of ' + str(n) + ' HARNESS2 CHECKS FAILED'}\n")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(run())
