"""Self-test for the 2026-11-07 architecture review, wave 1 (docs/ADR-001_ARCHITECTURE_REVIEW_20261107.md). Each check proves one fix:
a liveness check never kills what it looks at; `server stop` never kills a process that only reuses a recorded pid; a check that could not
run makes a YAML run 'not sure' even when pass_if holds; a step named `scan` no longer hides the scanner record; a not_sure run records
not_sure (not fail) on its cached replies and leaves memory alone; engine writes refresh the tool loop's view of the tree; the HUD never
puts run text into innerHTML; the self-improve check counter reads every summary format. No model, no GPU, no network."""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archrev1_")
_OLD = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST")}


def _isolate():
    os.environ["ARCHITECT_HOME"] = os.path.join(_TMP, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.makedirs(os.environ["ARCHITECT_HOME"], exist_ok=True)
    os.makedirs(os.environ["ARCHITECT_DATA_DIR"], exist_ok=True)


def _sleeper():
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)


def run():
    _isolate()
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    old_home = core.HOME
    core.set_home(os.environ["ARCHITECT_HOME"])
    procs = []
    try:
        # ---- 1. arch_runner.is_running looks without touching
        import arch_runner
        p = _sleeper()
        procs.append(p)
        time.sleep(0.3)
        first = arch_runner.is_running(p.pid)
        time.sleep(0.3)
        check("is_running says a live process is running", first is True, first)
        check("is_running leaves that process running (on Windows os.kill(pid, 0) used to terminate it)", p.poll() is None, p.poll())
        p.kill(); p.wait()
        check("is_running says a finished process is not running", arch_runner.is_running(p.pid) is False)
        check("is_running of no pid is False", arch_runner.is_running(0) is False and arch_runner.is_running(None) is False)

        # ---- 2. arch_server.stop: a recycled pid is never ours
        import arch_server
        q = _sleeper()
        procs.append(q)
        time.sleep(0.3)
        st = arch_server.load_state()
        st.setdefault("processes", {})["worker"] = {"tier": "worker", "pid": q.pid, "port": 0, "image": "llama-server.exe", "name": "x"}
        arch_server.save_state(st)
        msgs = []
        arch_server.stop(["worker"], say=msgs.append, reason="selftest")
        time.sleep(0.5)
        if os.name == "nt":
            check("server stop leaves alone a live process that only holds a recorded pid (its image is not the one we launched)",
                  q.poll() is None, (q.poll(), msgs))
        else:
            check("server stop on POSIX still drops the record", "worker" not in (arch_server.load_state().get("processes") or {}))
        check("the stale record is gone from the state file after stop", "worker" not in (arch_server.load_state().get("processes") or {}))
        check("_alive with the launched image given: a python process is not a llama-server",
              (not arch_server._alive(q.pid, image="llama-server.exe")) if os.name == "nt" else True)
        check("_alive of a process that is what we launched is True",
              arch_server._alive(q.pid, image=os.path.basename(sys.executable)) is True if os.name == "nt" else True)
        q.kill(); q.wait()

        # ---- 3 + 4. the YAML interpreter: not_sure wins over pass_if; `scan` no longer hides the record
        import arch_dataflow as df
        import arch_gates
        import arch_roles
        proj = os.path.join(_TMP, "proj")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as f:
            f.write("def run(s):\n    return eval(s)\n")
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}})
        quiet = lambda m: None
        eng = lambda fid: core.Engine(proj, fid, "check", cfg, review="auto", say=quiet)
        arch_gates.GATES["_rev1_ok"] = lambda e, ctx, step, changed: arch_gates.R(True, "")
        arch_gates.GATES["_rev1_none"] = lambda e, ctx, step, changed: arch_gates.R(None, "the checker could not run here")
        arch_gates.GATES["_rev1_no"] = lambda e, ctx, step, changed: arch_gates.R(False, "it failed")
        try:
            f1 = df.DataFlow({"id": "rev1a", "title": "t", "steps": [{"id": "a", "kind": "code", "gate": "_rev1_ok"},
                                                                    {"id": "b", "kind": "code", "gate": "_rev1_none", "on_fail": "continue"}],
                              "verdict": {"pass_if": "a.ok"}})
            o1 = f1.run(eng("rev1a"), {})
            check("pass_if holds but a gate could not run: the verdict is not_sure (it used to be overridden to pass)", o1["verdict"] == "not_sure", o1)
            f2 = df.DataFlow({"id": "rev1b", "title": "t", "steps": [{"id": "a", "kind": "code", "gate": "_rev1_ok"}], "verdict": {"pass_if": "a.ok"}})
            check("every gate ran and passed: the verdict is pass", f2.run(eng("rev1b"), {})["verdict"] == "pass")
            f3 = df.DataFlow({"id": "rev1c", "title": "t", "steps": [{"id": "a", "kind": "code", "gate": "_rev1_no", "on_fail": "continue"}],
                              "verdict": {"pass_if": "a.ok"}})
            check("a gate ran and failed: the verdict is fail", f3.run(eng("rev1c"), {})["verdict"] == "fail")
            f4 = df.DataFlow({"id": "rev1d", "title": "t", "steps": [{"id": "a", "kind": "code", "gate": "_rev1_ok"}],
                              "verdict": {"pass_if": "a.ok", "not_sure_if": "True"}})
            check("a not_sure_if that holds makes the run not_sure even when pass_if holds", f4.run(eng("rev1d"), {})["verdict"] == "not_sure")
        finally:
            for k in ("_rev1_ok", "_rev1_none", "_rev1_no"):
                arch_gates.GATES.pop(k, None)
        trace = []
        e5 = eng("rev1e")
        e5._trace = lambda rec, _t=trace: _t.append(rec)
        f5 = df.DataFlow({"id": "rev1e", "title": "t", "steps": [{"id": "scan", "kind": "code", "gate": "sec_scan", "on_fail": "stop"},
                                                                {"id": "after", "kind": "code", "gate": "sec_fewer", "on_fail": "continue"}],
                          "verdict": {"pass_if": "after.ok"}})
        o5 = f5.run(e5, {})
        texts = " | ".join(str(r.get("text") or "") for r in trace if r.get("type") == "check")
        check("a step named `scan` no longer hides the scanner's record from the *_fewer gate", "was recorded" not in texts, texts)
        check("that flow ends fail (nothing was fixed), not not_sure", o5["verdict"] == "fail", (o5, texts))
        check("the validator refuses a step id a gate writes (scan_rec, static, pinned, ...)",
              all(df.validate({"id": "x", "title": "x", "steps": [{"id": k, "kind": "code", "gate": "scope_files"}]}) for k in ("scan_rec", "static", "pinned", "tests_before")))
        shipped = [df.load_yaml_file(os.path.join(df.FLOWS_DIR, n)) for n in sorted(os.listdir(df.FLOWS_DIR)) if n.endswith(".yaml")]
        check("every shipped flow still validates with the larger reserved set", all(not df.validate(d) for d in shipped),
              [d.get("id") for d in shipped if df.validate(d)])

        # ---- 5. finish(..., unsure=True): replies learn not_sure, memory learns nothing
        import arch_cache
        import arch_memory
        seen = {"verdict": [], "used": 0, "confirm": 0}
        old = (arch_cache.attach_verdict, arch_memory.mark_used, arch_memory.confirm)
        arch_cache.attach_verdict = lambda k, v: seen["verdict"].append(v) or True
        arch_memory.mark_used = lambda *a, **k: seen.__setitem__("used", seen["used"] + 1)
        arch_memory.confirm = lambda *a, **k: seen.__setitem__("confirm", seen["confirm"] + 1)
        try:
            e6 = eng("rev1f")
            e6.cache_keys, e6.memory_used = {"t1": "k1"}, ["a lesson"]
            e6.confirm_memory(None)
            check("a not_sure run tags its remembered replies not_sure (it used to say fail)", seen["verdict"] == ["not_sure"], seen)
            check("a not_sure run neither confirms nor strikes a lesson", seen["used"] == 0 and seen["confirm"] == 0, seen)
            seen["verdict"].clear()
            e6.confirm_memory(False)
            check("a failed run still tags fail and counts the lesson as used once", seen["verdict"] == ["fail"] and seen["used"] == 1, seen)
            seen["verdict"].clear()
            e6.confirm_memory(True)
            check("a passed run tags pass and confirms", seen["verdict"] == ["pass"] and seen["confirm"] == 1, seen)
        finally:
            arch_cache.attach_verdict, arch_memory.mark_used, arch_memory.confirm = old
        import inspect
        import arch_flows
        src = inspect.getsource(arch_flows.run_flow)
        check("run_flow hands the verdict's unsure to finish()", "unsure=bool(out.get(\"unsure\"))" in src)

        # ---- 8. an engine write refreshes the tool loop's cached tree
        import arch_tools
        e7 = eng("rev1g")
        before = arch_tools.call(e7, "ls", {"path": "."})
        e7.put_file("brand_new.py", "X = 1\n", quiet=True)
        after = arch_tools.call(e7, "ls", {"path": "."})
        check("ls after an engine write shows the new file (the cached tree used to hide it)",
              "brand_new.py" not in str(before) and "brand_new.py" in str(after), (str(before)[:200], str(after)[:200]))

        # ---- two runs started in the same second get two folders (they used to share one trace)
        ea, eb = eng("rev1h"), eng("rev1h")
        check("two engines started in the same second get their own run folders, in the usual id shape",
              ea.run_dir != eb.run_dir and all(__import__("re").fullmatch(r"\d{8}_\d{6}_rev1h", x.run_id) for x in (ea, eb)), (ea.run_id, eb.run_id))

        # ---- 7. the HUD treats run text as data
        hud = open(os.path.join(HERE, "static", "hud", "architect_harness_hud.html"), encoding="utf-8").read()
        feed = hud[hud.index("feedEvent: function"):][:1500]
        check("the HUD event log never builds a line with innerHTML", ".innerHTML" not in feed and "textContent = evt.msg" in feed, feed[:300])
        strip = hud[hud.index("async function pollLiveStrip"):][:1500]
        check("the HUD's live strip only puts numbers into its markup", "const num = v =>" in strip and "${g.vram_used_gb" not in strip)
        check("the HUD draws not_sure as unsure, not as a held gate", "'unsure')" in hud and "r.verdict === 'fail' || r.verdict === 'stopped'" in hud)

        # ---- 15/16. test-gate integrity
        llama = open(os.path.join(HERE, "arch_selftest_llama.py"), encoding="utf-8").read()
        check("the llama suite restores the Claude-gate markers it sets", '"ARCHITECT_CLOUD_TEST", "ARCHITECT_SELFTEST")}' in llama)
        import arch_selfimprove as si
        samples = {"ALL 61 ENGINE CHECKS PASSED": 61, "ALL 9 A11Y CHECKS PASSED": 9, "ALL 12 HARNESS2 CHECKS PASSED": 12,
                   "ALL 5 TEMPLATES_EXTRA CHECKS PASSED": 5, "  17/17 passed": 17, "61 passed, 0 failed": 61,
                   "contracts+recipes: 325 passed, 0 failed": 325, "OK: 44 checks": 44, "CPUWORKER: PASSED (12 checks)": 12}
        got = {k: si.count_passed(k) for k in samples}
        check("the self-improve counter reads every summary format the suites print", got == samples, {k: v for k, v in got.items() if v != samples[k]})
        check("a failing summary counts nothing", si.count_passed("3 of 9 FOO CHECKS FAILED") == 0)
        arch = open(os.path.join(HERE, "architect.py"), encoding="utf-8").read()
        check("the finops and review1 suites run in `selftest --suite full`", '"replay", "finops", "review1")' in arch)
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        core.set_home(old_home)
        for k, v in _OLD.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' REVIEW1 CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' REVIEW1 CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
