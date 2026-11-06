"""Self-test for the safe self-improvement pipeline. Everything runs on a throwaway COPY of the install, never the real one."""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

GOOD_NEW = "def double(x):\n    return x * 2\n"
GOOD_TEST_NOTE = "\n# note: added by the self-improve test\n"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_selfimprove as si

    tmp = tempfile.mkdtemp(prefix="arch_selfimp_")
    old_home = core.HOME
    quiet = lambda *a, **k: None
    try:
        core.set_home(os.path.join(tmp, "home"))
        base = os.path.join(tmp, "install")
        for rel in si.tracked_files(HERE):
            os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
            shutil.copy2(os.path.join(HERE, rel), os.path.join(base, rel))
        if os.path.exists(os.path.join(base, "MANIFEST.sha256")):          # the copy starts with no integrity record, whatever the source has
            os.remove(os.path.join(base, "MANIFEST.sha256"))
        # user data that must never travel into a candidate
        os.makedirs(os.path.join(base, "hub_data"), exist_ok=True)
        open(os.path.join(base, "hub_data", "results.json"), "w").write("{}")
        open(os.path.join(base, "architect_config.json"), "w").write('{"secret": 1}')

        check("tracked_files leaves out settings, results and caches",
              not any(r.startswith("hub_data") or r == "architect_config.json" for r in si.tracked_files(base)) and "architect.py" in si.tracked_files(base))
        v = si.verify(base)
        check("verify without a manifest says so", v["has_manifest"] is False and v["ok"] is False)
        si.write_manifest(base)
        v = si.verify(base)
        check("a freshly written manifest verifies", v["ok"] and not v["modified"] and not v["missing"], v)
        with open(os.path.join(base, "arch_tune.py"), "a") as f:
            f.write("\n# tampered\n")
        v = si.verify(base)
        check("a modified file is reported by name", (not v["ok"]) and v["modified"] == ["arch_tune.py"], v)
        shutil.copy2(os.path.join(HERE, "arch_tune.py"), os.path.join(base, "arch_tune.py"))
        os.remove(os.path.join(base, "arch_roles.py"))
        v = si.verify(base)
        check("a missing file is reported", "arch_roles.py" in v["missing"] and not v["ok"])
        shutil.copy2(os.path.join(HERE, "arch_roles.py"), os.path.join(base, "arch_roles.py"))
        check("restoring the files makes it verify again", si.verify(base)["ok"])

        cand = si.make_candidate(base)
        check("a candidate is a code-only copy (no settings, no results)",
              os.path.isfile(os.path.join(cand, "architect.py")) and not os.path.exists(os.path.join(cand, "hub_data"))
              and not os.path.exists(os.path.join(cand, "architect_config.json")))
        check("an untouched candidate has no changes", not any(si.changes(base, cand).values()))
        g = si.gate(base, cand, suite="mini", baseline=1, say=quiet)
        check("the gate refuses a candidate that changed nothing", not g["ok"] and g["gates"][0]["name"].startswith("the candidate differs"))

        # protected files: by default only the generated manifests; the guard/gate only when you ask for it
        with open(os.path.join(cand, "netguard.py"), "a") as f:
            f.write("\n# a change to the guard\n")
        g = si.gate(base, cand, suite="mini", baseline=1, say=quiet, timeout=5)
        check("by default the harness may edit its own network guard: the gate goes on to the real checks instead of refusing",
              g["gates"][0]["ok"] and g["gates"][0]["name"].startswith("protected files") and len(g["gates"]) > 1, g["gates"][:2])
        import arch_core as _c
        _cfg = _c.load_config(si.HERE)
        _cfg.setdefault("behavior", {})["self_protect"] = "guard"
        _orig = si.protected_now
        si.protected_now = lambda cfg=None: _orig(_cfg)
        try:
            g = si.gate(base, cand, suite="mini", baseline=1, say=quiet, timeout=5)
            check("with behavior.self_protect = guard, editing the network guard is refused before anything runs",
                  not g["ok"] and g["checks"] is None and "netguard.py" in g["gates"][-1]["detail"], g["gates"])
        finally:
            si.protected_now = _orig
        shutil.copy2(os.path.join(base, "netguard.py"), os.path.join(cand, "netguard.py"))
        check("the generated manifest is outside the comparison entirely (it is rewritten on install)", "MANIFEST.sha256" not in si.changes(base, cand)["modified"])

        # compile
        open(os.path.join(cand, "arch_broken.py"), "w").write("def x(:\n")
        g = si.gate(base, cand, suite="mini", baseline=1, say=quiet)
        check("a file that does not compile fails the compile gate and stops there",
              not g["ok"] and g["gates"][-1]["name"].startswith("every changed Python file") and len(g["gates"]) == 2, g["gates"])
        os.remove(os.path.join(cand, "arch_broken.py"))

        # fake code
        open(os.path.join(cand, "arch_stub.py"), "w").write("def feature():\n    pass\n")
        g = si.gate(base, cand, suite="mini", baseline=1, say=quiet)
        check("a new stub function fails the fake-code gate before the (slow) self-test runs",
              not g["ok"] and "fake code" in g["gates"][-1]["name"] and g["checks"] is None, g["gates"])
        os.remove(os.path.join(cand, "arch_stub.py"))

        # a benign addition passes; the check-count gate is real
        open(os.path.join(cand, "arch_extra.py"), "w").write(GOOD_NEW)
        t0 = time.time()
        n_base = si.baseline_checks(base, "mini", say=quiet)
        t1 = time.time()
        n_again = si.baseline_checks(base, "mini", say=quiet)
        check("the baseline counts the checks of the current install and is cached", n_base >= 100 and n_again == n_base and (time.time() - t1) < 1.0, (n_base, n_again, round(time.time() - t1, 2)))
        g = si.gate(base, cand, suite="mini", baseline=n_base + 1000, say=quiet)
        check("a candidate that runs fewer checks than the install is refused even though they all pass",
              not g["ok"] and g["checks"] == n_base and "at least as many checks" in g["gates"][-1]["name"], g["gates"])
        g = si.gate(base, cand, suite="mini", baseline=n_base, say=quiet)
        check("a benign, real addition passes every gate", g["ok"] and all(x["ok"] for x in g["gates"]) and len(g["gates"]) >= 6 and g["changed"]["added"] == ["arch_extra.py"], g["gates"])

        # promote needs a pass and a confirm
        try:
            si.promote(base, cand, {"ok": False, "changed": g["changed"]}, "x", confirm=True, say=quiet)
            refused_gate = False
        except PermissionError:
            refused_gate = True
        try:
            si.promote(base, cand, g, "x", confirm=False, say=quiet)
            refused_confirm = False
        except PermissionError:
            refused_confirm = True
        check("promotion is refused without a passing gate, and refused without a confirmation",
              refused_gate and refused_confirm and not os.path.exists(os.path.join(base, "arch_extra.py")))

        # a modification too (so rollback has something to restore)
        orig_tune = open(os.path.join(base, "arch_tune.py"), encoding="utf-8").read()
        with open(os.path.join(cand, "arch_tune.py"), "a", encoding="utf-8") as f:
            f.write(GOOD_TEST_NOTE)
        g2 = si.gate(base, cand, suite="mini", baseline=n_base, say=quiet)
        check("a modification plus an addition passes the gates", g2["ok"] and g2["changed"]["modified"] == ["arch_tune.py"], g2["gates"])
        out = si.promote(base, cand, g2, "test goal", confirm=True, say=quiet)
        check("promotion copies the files and the install still verifies against its new manifest",
              out["ok"] and os.path.isfile(os.path.join(base, "arch_extra.py")) and GOOD_TEST_NOTE in open(os.path.join(base, "arch_tune.py"), encoding="utf-8").read()
              and si.verify(base)["ok"], out)
        check("the promotion is in the history", [e["event"] for e in si.history()] == ["promote"] and si.history()[0]["goal"] == "test goal")

        r = si.rollback(base, say=quiet)
        check("rollback restores the modified file byte for byte and removes the added one",
              r["ok"] and open(os.path.join(base, "arch_tune.py"), encoding="utf-8").read() == orig_tune and not os.path.exists(os.path.join(base, "arch_extra.py")), r)
        check("after a rollback the install verifies and the rollback is recorded", si.verify(base)["ok"] and [e["event"] for e in si.history()] == ["promote", "rollback"])
        check("a second rollback has nothing left to undo", si.rollback(base, say=quiet)["ok"] is False)
        shutil.rmtree(os.path.dirname(cand), ignore_errors=True)

        # the whole path with a scripted worker
        def good_worker(c, goal, flow, review):
            open(os.path.join(c, "arch_extra2.py"), "w").write("def triple(x):\n    return x * 3\n")
            return 0

        def lazy_worker(c, goal, flow, review):
            open(os.path.join(c, "arch_lazy.py"), "w").write("def todo():\n    pass\n")
            return 0
        out = si.improve("add triple", base=base, suite="mini", yes=True, say=quiet, runner=good_worker, baseline=n_base)
        check("improve: a good worker's change is gated and promoted", out["ok"] and os.path.isfile(os.path.join(base, "arch_extra2.py")), out)
        out = si.improve("add a lazy stub", base=base, suite="mini", yes=True, say=quiet, runner=lazy_worker, baseline=n_base)
        check("improve: a lazy worker is rejected even with --yes, and the install is untouched",
              (not out["ok"]) and not os.path.exists(os.path.join(base, "arch_lazy.py")) and si.history()[-1]["event"] == "rejected")
        out = si.improve("no-op", base=base, suite="mini", yes=True, say=quiet, runner=lambda *a: 0, baseline=n_base)
        check("improve: a worker that changes nothing is not 'promoted'", not out["ok"])
        check("rollback after improve restores the previous state", si.rollback(base, say=quiet)["ok"] and not os.path.exists(os.path.join(base, "arch_extra2.py")) and si.verify(base)["ok"])
    finally:
        core.set_home(old_home)
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SELF-IMPROVE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SELF-IMPROVE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
