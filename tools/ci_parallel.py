"""Run every self-test suite in parallel, each in its own throwaway data folder, and print one table.

    py -3.12 tools/ci_parallel.py [--jobs 6] [--only a,b] [--timeout 900]

Each suite gets a fresh ARCHITECT_DATA_DIR and ARCHITECT_HOME (so no suite can touch the person's real settings or data),
ARCHITECT_NO_CACHE=1 and below-normal priority. A suite passes when it exits 0. The summary line of each suite is shown.
Exit code 1 when any suite fails or times out. Writes hub_data/ci/<stamp>.json.
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
    flags = 0x00004000 if os.name == "nt" else 0          # BELOW_NORMAL_PRIORITY_CLASS
    t = time.time()
    try:
        p = subprocess.run([sys.executable, os.path.join(APP, name + ".py")], cwd=APP, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
        out, rc = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired:
        out, rc = "timed out", None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    summ = next((l for l in reversed(lines) if re.search(r"PASSED|FAILED|passed|failed|CHECKS|OK:", l)), lines[-1] if lines else "")
    fails = [l for l in lines if l.startswith("FAIL")][:5]
    return {"suite": name, "rc": rc, "secs": round(time.time() - t, 1), "summary": summ[:140], "fails": fails}


def main():
    jobs, timeout = int(arg("--jobs", max(2, (os.cpu_count() or 4) // 2))), int(arg("--timeout", 900))
    names = suites()
    print(f"running {len(names)} suites, {jobs} at a time")
    res = []
    with cf.ThreadPoolExecutor(jobs) as ex:
        for r in ex.map(lambda n: run(n, SLOW.get(n, timeout)), names):
            mark = "PASS" if r["rc"] == 0 else ("NOT_SURE" if "COULD NOT RUN" in r["summary"] and re.search(r"(^|\s)0 of ", r["summary"], re.I) else None) or ("FLAKY" if r["suite"] in KNOWN_FLAKY else "FAIL" if r["rc"] is not None else "TIMEOUT")
            r["status"] = mark
            print(f"  {mark:7} {r['suite']:38} {r['secs']:6.1f}s  {r['summary']}", flush=True)
            res.append(r)
    bad = [r for r in res if r["status"] in ("FAIL", "TIMEOUT")]
    os.makedirs(os.path.join(APP, "hub_data", "ci"), exist_ok=True)
    out = os.path.join(APP, "hub_data", "ci", time.strftime("%Y%m%d_%H%M%S") + ".json")
    json.dump(res, open(out, "w", encoding="utf-8"), indent=1)
    print(f"\n{len(res) - len(bad)}/{len(res)} suites green" + (f"; failing: {', '.join(r['suite'] for r in bad)}" if bad else "") + f"  ({out})")
    for r in bad:
        print(f"\n--- {r['suite']}: {r['summary']}\n" + "\n".join("    " + f for f in r["fails"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
