"""Run every self-test suite in parallel, each in its own throwaway data folder, and print one table.

    py -3.12 tools/ci_parallel.py [--jobs 6] [--only a,b] [--timeout 900] [--no-rerun]

Each suite gets a fresh ARCHITECT_DATA_DIR and ARCHITECT_HOME (so no suite can touch the person's real settings or data),
ARCHITECT_NO_CACHE=1 and below-normal priority. A suite passes when it exits 0. The summary line of each suite is shown.
Exit code 1 when any suite fails or times out. Writes hub_data/ci/<stamp>.json.

A suite that fails or times out in the parallel run is run once more ON ITS OWN. If it passes alone it is marked LOAD (it fails only
when the machine is busy): listed apart, not counted as a failure, so red always means a real failure. A suite that fails alone too
stays FAIL. NOT_SURE suites (checks that could not run here, e.g. a missing optional package) are listed apart with their reason.
"""
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Environment-dependent checks no longer hide here: each suite reports them NOT_SURE itself ("0 of N FAILED, k COULD NOT RUN"). Keep this for a suite that is flaky as a whole.
KNOWN_FLAKY = {}
SLOW = {"arch_selftest_self": 2400}


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def suites():
    only = [s.strip() for s in arg("--only", "").split(",") if s.strip()]
    names = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(APP, "arch_selftest*.py")))
    names.append("arch_hub_check")
    return [n for n in names if not only or any(n == o or n == "arch_selftest_" + o or (o not in ("arch_selftest", "arch") and o in n) for o in only)]


def run(name, timeout):
    home = tempfile.mkdtemp(prefix="ci_" + name[:20] + "_")
    env = dict(os.environ, ARCHITECT_DATA_DIR=os.path.join(home, "data"), ARCHITECT_HOME=home, ARCHITECT_NO_CACHE="1",
               PYTHONDONTWRITEBYTECODE="1", ARCHITECT_SELFTEST="1")
    # BELOW_NORMAL_PRIORITY_CLASS, and on Windows a console of its own (CREATE_NO_WINDOW): a Ctrl+C a suite raises on its console then
    # reaches that suite only. Sharing this console, one such event ended the whole run on GitHub's runner (2026-11-13; on the
    # laptop every process inherits "ignore Ctrl+C" from the shell, so it went unseen). Output still comes back through the pipe.
    flags = (0x00004000 | 0x08000000) if os.name == "nt" else 0
    t = time.time()
    sys.path.insert(0, APP)
    import arch_sandbox
    job = None
    try:
        # Every suite runs inside a Windows job that kills ALL its descendants when it ends (finished, timed out or crashed):
        # a suite that never reached its own stop() used to leave `python -m app.server` children holding ports for hours.
        p = subprocess.Popen([sys.executable, os.path.join(APP, name + ".py")], cwd=APP, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=flags)
        job = arch_sandbox.attach(p, 16384)
        try:
            out, _ = p.communicate(timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            arch_sandbox.close(job)                     # kill the grandchildren first: they hold the pipe open and communicate() would wait for them
            job = None
            out, rc = (p.communicate()[0] or "") + " timed out", None
        out = out or ""
    finally:
        arch_sandbox.close(job)                     # stops anything the suite left running
        shutil.rmtree(home, ignore_errors=True)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    summ = next((l for l in reversed(lines) if re.search(r"PASSED|FAILED|passed|failed|CHECKS|OK:", l)), lines[-1] if lines else "")
    fails = [l for l in lines if l.startswith("FAIL")][:5]
    if rc not in (0, None) and any(l.startswith("Traceback") for l in lines):
        # the suite crashed: its last check line is not its summary, and the traceback is the one thing worth reading
        summ = next((l for l in reversed(lines) if re.match(r"\w+(Error|Exception|Exit)\b", l)), "crashed: " + (lines[-1] if lines else ""))
        fails = fails + ["    " + l for l in lines[-6:]]
    return {"suite": name, "rc": rc, "secs": round(time.time() - t, 1), "summary": summ[:140], "fails": fails}


def main():
    jobs, timeout = int(arg("--jobs", max(2, (os.cpu_count() or 4) // 2))), int(arg("--timeout", 900))
    names = suites()
    # The person's own settings and data must survive a test run untouched. A suite (or a tool "cleaning up") that removes them is a
    # bug worth a red line, not something to notice days later (2026-11-07: hub_data/ and architect_config.json were deleted by a
    # testing tool and nobody saw it until the hub came up empty).
    sys.path.insert(0, APP)
    import arch_core as _core
    _mine = {"settings file": _core.CONFIG_FILE, "data folder": _core.data_dir()}
    _had = {k: os.path.exists(p) for k, p in _mine.items()}
    print(f"running {len(names)} suites, {jobs} at a time")
    res = []
    with cf.ThreadPoolExecutor(jobs) as ex:
        for r in ex.map(lambda n: run(n, SLOW.get(n, timeout)), names):
            mark = "PASS" if r["rc"] == 0 else ("NOT_SURE" if "COULD NOT RUN" in r["summary"] and re.search(r"(^|\s)0 of ", r["summary"], re.I) else None) or ("FLAKY" if r["suite"] in KNOWN_FLAKY else "FAIL" if r["rc"] is not None else "TIMEOUT")
            r["status"] = mark
            print(f"  {mark:7} {r['suite']:38} {r['secs']:6.1f}s  {r['summary']}", flush=True)
            res.append(r)
            gone = [k for k, p_ in _mine.items() if _had[k] and not os.path.exists(p_)]
            if gone:                                     # stop at once, not at the end: whatever is running must not go on "cleaning"
                print(f"\nSTOP: your {' and '.join(gone)} disappeared while {r['suite']} ran ({', '.join(_mine[k] for k in gone)}). "
                      "No suite may touch them. Ending the run here.", flush=True)
                ex.shutdown(wait=False, cancel_futures=True)
                break
    if "--no-rerun" not in sys.argv:
        for r in [r for r in res if r["status"] in ("FAIL", "TIMEOUT")]:
            print(f"  rerun   {r['suite']} on its own ...", flush=True)
            r2 = run(r["suite"], SLOW.get(r["suite"], timeout))
            r["alone"] = {k: r2[k] for k in ("rc", "secs", "summary", "fails")}
            if r2["rc"] == 0:
                r["status"] = "LOAD"
            print(f"  {r['status']:7} {r['suite']:38} {r2['secs']:6.1f}s  alone: {r2['summary']}", flush=True)
    bad = [r for r in res if r["status"] in ("FAIL", "TIMEOUT")]
    load = [r for r in res if r["status"] == "LOAD"]
    unsure = [r for r in res if r["status"] == "NOT_SURE"]
    # The report goes where this run's data lives: under ARCHITECT_DATA_DIR when the caller set one (an isolated test home), else
    # the real data folder. Writing into <repo>/hub_data regardless is what made a testing tool "clean up" the person's data (2026-11-07).
    ci_dir = os.path.join(os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(APP, "hub_data"), "ci")
    os.makedirs(ci_dir, exist_ok=True)
    out = os.path.join(ci_dir, time.strftime("%Y%m%d_%H%M%S") + ".json")
    json.dump(res, open(out, "w", encoding="utf-8"), indent=1)
    print(f"\n{len(res) - len(bad)}/{len(res)} suites green" + (f"; failing: {', '.join(r['suite'] for r in bad)}" if bad else "") + f"  ({out})")
    for r in bad:
        print(f"\n--- {r['suite']}: {r['summary']}\n" + "\n".join("    " + f for f in r["fails"]))
    if load:
        print("failed only under load (passed when run alone): " + ", ".join(r["suite"] for r in load))
    if unsure:
        print("not sure (some checks could not run here): " + "; ".join(f"{r['suite']} ({r['summary']})" for r in unsure))
    lost = [k for k, p in _mine.items() if _had[k] and not os.path.exists(p)]
    if lost:
        print(f"\nWARNING: your {' and '.join(lost)} disappeared during this run ({', '.join(_mine[k] for k in lost)}). "
              "A suite must never touch them: treat this as a failure and look at what ran.")
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
