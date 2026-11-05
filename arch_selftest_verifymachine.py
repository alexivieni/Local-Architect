"""Self-test for tools/verify_machine.py: the matrix logic (three outcomes, timing, crash -> FAIL, Skip/timeout -> NOT_SURE,
verdict, report files) and the selftest-output parser with its known-flake rule. Fakes only: no model, no server, no subprocess."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
sys.path.insert(0, HERE)


def run():
    import verify_machine as vm
    results = []

    def ok(name, cond, extra=""):
        results.append(bool(cond))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   ({extra})" if extra and not cond else ""))

    def mk(name, fn):
        return {"name": name, "layer": "cpu", "fn": fn, "timeout": 1}

    def boom(c):
        raise RuntimeError("bang")

    def skip(c):
        raise vm.Skip("no model")

    def tout(c):
        raise subprocess.TimeoutExpired("x", 5)

    cs = [mk("p", lambda c: (vm.PASS, "fine")), mk("f", lambda c: (vm.FAIL, "bad")), mk("b", lambda c: True),
          mk("crash", boom), mk("skip", skip), mk("tout", tout), mk("weird", lambda c: ("MAYBE", ""))]
    r = {x["name"]: x for x in vm.run_matrix(cs, {}, say=lambda m: None)}
    ok("tuple PASS kept", r["p"]["status"] == vm.PASS and r["p"]["evidence"] == "fine")
    ok("tuple FAIL kept", r["f"]["status"] == vm.FAIL)
    ok("bool True -> PASS", r["b"]["status"] == vm.PASS)
    ok("exception -> FAIL with reason", r["crash"]["status"] == vm.FAIL and "bang" in r["crash"]["evidence"])
    ok("Skip -> NOT_SURE", r["skip"]["status"] == vm.NOT_SURE and "no model" in r["skip"]["evidence"])
    ok("timeout -> NOT_SURE", r["tout"]["status"] == vm.NOT_SURE and "5" in r["tout"]["evidence"])
    ok("unknown status -> FAIL", r["weird"]["status"] == vm.FAIL)
    ok("every result timed", all(isinstance(x["seconds"], float) for x in r.values()))
    ok("verdict FAIL beats NOT_SURE", vm.summarize(list(r.values()))["verdict"] == vm.FAIL)
    ok("verdict NOT_SURE never PASS", vm.summarize([r["p"], r["skip"]])["verdict"] == vm.NOT_SURE)
    ok("verdict PASS", vm.summarize([r["p"], r["b"]])["verdict"] == vm.PASS)

    tmp = tempfile.mkdtemp(prefix="vm_st_")
    try:
        rep, base = vm.write_report(list(r.values()), "cpu", tmp)
        ok("json report written", json.load(open(base + ".json"))["summary"]["FAIL"] == 3)
        md = open(base + ".md", encoding="utf-8").read()
        ok("md report has a row per check", md.count("\n| ") >= len(r) + 1 and "NOT_SURE" in md)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    good = "  PASS  a\n  PASS  b\n\n  ALL 2 ARCHITECT CHECKS PASSED\nALL 5 CREW CHECKS PASSED\n"
    p = vm.parse_selftest(good)
    ok("parser counts suites", len(p["suites"]) == 2 and p["pass_lines"] == 2, p)
    ok("clean output -> PASS", vm.judge_selftest(0, p)[0] == vm.PASS)
    flake = good + "  FAIL  port 8000 runner   (busy)\n  4 of 40 SECURITY CHECKS FAILED\n"
    ok("only known flake -> NOT_SURE", vm.judge_selftest(1, vm.parse_selftest(flake))[0] == vm.NOT_SURE)
    real = good + "  FAIL  router picks bugfix\n  1 of 9 ROUTING CHECKS FAILED\n"
    st, ev = vm.judge_selftest(1, vm.parse_selftest(real))
    ok("unknown suite failure -> FAIL, named", st == vm.FAIL and "router picks bugfix" in ev, ev)
    ok("stray FAIL line without summary -> FAIL", vm.judge_selftest(0, vm.parse_selftest(good + "  FAIL  x\n"))[0] == vm.FAIL)
    ok("no summary at all -> NOT_SURE", vm.judge_selftest(1, vm.parse_selftest("Traceback ..."))[0] == vm.NOT_SURE)

    ok("cpu mode selects no gpu check", all(c["layer"] == "cpu" for c in vm.select_checks("cpu")))
    gpu = vm.select_checks("gpu")
    ok("gpu mode has the live checks", len(gpu) >= 10 and all(c["layer"] == "gpu" for c in gpu))
    ok("--only filters", [c["name"] for c in vm.select_checks("cpu", only=["disk"])] == ["disk space"])
    ok("--skip filters", "disk space" not in [c["name"] for c in vm.select_checks("cpu", skip=["disk"])])
    ok("cleanup is the last gpu check", gpu[-1]["name"].startswith("cleanup"))

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' VERIFY-MACHINE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' VERIFY-MACHINE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
