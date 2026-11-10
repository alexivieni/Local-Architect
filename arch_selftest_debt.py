"""Self-test: tech-debt Phase 1 (2026-11-09) stays fixed.

  - Stop asks a run to end before its next model call (the project's .arch/STOP), ends the process only after a grace period, and
    removes the stop file it wrote (it used to kill the process mid-step, and a STOP file left behind ends every later run at once)
  - a job that is not a run (tune, ready, selftest) still ends at once
  - the tool cache sees an edit made outside the tools: the tree fingerprint was kept until Architect wrote a file, so ls/grep/search
    answered from the old tree after a hook, a test run or an edit in the person's editor

The FinOps price list and the escalation host are checked in arch_selftest_finops / arch_selftest_win, the button crawl in
arch_selftest_crawl. Everything runs in a throwaway home and data folder; no model, no network.

    python arch_selftest_debt.py
"""
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# a stand-in run: it stops by itself when the project's .arch/STOP appears (as the engine does before each model call)
POLITE = ("import os,sys,time\nroot=sys.argv[sys.argv.index('--root')+1]\nt=time.time()\n"
          "while time.time()-t<30:\n    if os.path.exists(os.path.join(root,'.arch','STOP')):\n        sys.exit(0)\n    time.sleep(0.05)\n")
STUBBORN = "import time\nt=time.time()\nwhile time.time()-t<30:\n    time.sleep(0.05)\n"     # ignores the stop file


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    tmp = tempfile.mkdtemp(prefix="arch_debt_")
    old = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST")}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.makedirs(os.environ["ARCHITECT_HOME"], exist_ok=True)
    try:
        import arch_core as core
        core.set_home(os.environ["ARCHITECT_HOME"])
        import model_hub as mh
        import arch_tools

        # ---- 1. Stop: polite run ends on the stop file, the file is removed afterwards
        root = os.path.join(tmp, "proj")
        os.makedirs(root)
        stop_file = os.path.join(root, ".arch", "STOP")
        job = mh.Job()
        job.start("a run", ["-c", POLITE, "--root", root])
        time.sleep(0.5)
        r = job.stop()
        t0 = time.time()
        while job.running() and time.time() - t0 < 10:
            time.sleep(0.05)
        took = time.time() - t0
        time.sleep(0.3)                                  # the helper thread removes the file once the process is gone
        check("Stop asks a run to end before its next model call (the project's .arch/STOP) instead of killing it",
              r.get("graceful") is True and not job.running() and took < 5 and job.proc.returncode == 0, (r, round(took, 2), job.proc.returncode))
        check("the stop file Stop wrote is removed once the run has ended (a leftover one ends every later run at once)",
              not os.path.exists(stop_file))

        # ---- 2. a run that ignores the stop file is ended after the grace period
        job2 = mh.Job()
        job2.STOP_GRACE_S = 1.0
        job2.start("a stubborn run", ["-c", STUBBORN, "--root", root])
        time.sleep(0.3)
        r2 = job2.stop()
        t0 = time.time()
        while job2.running() and time.time() - t0 < 15:
            time.sleep(0.05)
        took2 = time.time() - t0
        time.sleep(0.3)
        check("a run that does not stop by itself is ended after the grace period, and its stop file still goes",
              r2.get("graceful") is True and not job2.running() and 0.8 <= took2 < 8 and not os.path.exists(stop_file), (r2, round(took2, 2)))

        # ---- 3. a job that is not a run ends at once; a stop file the person put there is never taken over or removed
        job3 = mh.Job()
        job3.start("tune", ["-c", STUBBORN])
        time.sleep(0.3)
        r3 = job3.stop()
        check("a job that is not a run (no project folder) still ends at once", r3.get("graceful") is False and not job3.running(), r3)
        os.makedirs(os.path.dirname(stop_file), exist_ok=True)
        with open(stop_file, "w", encoding="utf-8") as f:
            f.write("the person's own kill switch\n")
        job4 = mh.Job()
        job4.start("a run", ["-c", POLITE, "--root", root])
        time.sleep(0.5)
        job4.stop()
        t0 = time.time()
        while job4.running() and time.time() - t0 < 10:
            time.sleep(0.05)
        time.sleep(0.3)
        check("a stop file the person put there is left alone (neither taken over nor removed)",
              os.path.exists(stop_file) and "own kill switch" in open(stop_file, encoding="utf-8").read())
        os.remove(stop_file)

        # ---- 4. the tool cache sees an edit made outside the tools
        cfg = core.deep_merge(core.DEFAULTS, {})
        e = core.Engine(root, "explore", "g", cfg, persist=False, say=lambda m: None)
        with open(os.path.join(root, "a.py"), "w", encoding="utf-8") as f:
            f.write("def first():\n    return 1\n")
        out1, _ = arch_tools.call(e, "grep", {"pattern": "needle_xyz"})
        with open(os.path.join(root, "b.py"), "w", encoding="utf-8") as f:   # written by "someone else": a hook, a test, an editor
            f.write("needle_xyz = 1\n")
        time.sleep(arch_tools.TREE_FP_TTL + 0.1)
        out2, _ = arch_tools.call(e, "grep", {"pattern": "needle_xyz"})
        check("grep sees a file written outside the tools (it answered from the old tree until Architect wrote a file)",
              "b.py" not in out1 and "b.py" in out2, (out1[:120], out2[:120]))
        arch_tools._tc(e)["tree_fp"], arch_tools._tc(e)["tree_fp_t"] = "stale", time.monotonic()
        with open(os.path.join(root, "c.py"), "w", encoding="utf-8") as f:
            f.write("needle_xyz = 2\n")
        arch_tools.call(e, "run_tests", {})
        out3, _ = arch_tools.call(e, "grep", {"pattern": "needle_xyz"})
        check("after run_tests the tree is read again at once (tests may write files)", "c.py" in out3, out3[:160])

        # ---- 5. a refused POST is answered with its reason, never a reset connection (seen in CI: WinError 10053, intermittent)
        import json
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mh.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        body = json.dumps({"pad": "x" * 20000}).encode()
        got = []
        try:
            for _ in range(30):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/stop", data=body,
                                             headers={"Content-Type": "application/json"})     # no X-Hub-Token
                try:
                    urllib.request.urlopen(req, timeout=10).read()
                    got.append("200?")
                except urllib.error.HTTPError as ex:
                    got.append(f"{ex.code}:{json.loads(ex.read().decode() or '{}').get('error', '')[:9]}")
                except OSError as ex:
                    got.append(type(ex).__name__)
        finally:
            srv.shutdown()
        check("a POST without the token is refused with its reason every time (the body was left unread, so Windows reset it)",
              got == ["403:bad token"] * 30, sorted(set(got)))

        # ---- 6. no stray control character in the source (an escaped "\b" that became a real backspace made three regexes match
        #         nothing: the design suite's alias check passed whatever the page held, and ci_parallel never named a crash's error)
        stray = []
        for dp, dns, fns in os.walk(HERE):
            dns[:] = [d for d in dns if d not in (".git", "hub_data", ".arch", "__pycache__", "bin", "node_modules", "static")]
            for fn in fns:
                if fn.endswith((".py", ".html", ".md", ".yaml", ".json")):
                    try:
                        t = open(os.path.join(dp, fn), encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    if re.search(r"[\x00-\x08\x0b\x0e-\x1f]", t):
                        stray.append(os.path.relpath(os.path.join(dp, fn), HERE))
        check("no source file holds a stray control character (a backspace in a regex silently matches nothing)", not stray, stray[:5])
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' TECH-DEBT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' TECH-DEBT CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
