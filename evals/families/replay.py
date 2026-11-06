"""replay: training tasks made from this project's own Claude orchestration (arch_replay.py).

An agent that edited repo files and ran a selftest suite becomes a local task: the fixture is the files at the commit before the
work, the goal is written by code (template + file names + the check + what the check prints + the definitions that differ), and
the judge is that suite run against the model's files in a throwaway copy of the project. Only tasks whose judge runs offline in
under 60 s and whose fixture is at most 6 files / 1500 lines are kept; each carries the finished files as its reference solution
and the unchanged fixture as the wrong answer, both proven against the real judge when `arch_replay.py --build` made them.
Whole task KINDS (module families) are held out (tag `holdout`, ~15%).

The tasks live in <data>/replay/tasks.json (built from the transcripts and git, minutes of suite runs), so loading this family is
instant; with no build yet it serves nothing. DEFAULT_N is 0: a normal `eval` carries none (`eval --n 40 --tags replay` runs them).

    py -3.12 evals/families/replay.py --list
    py -3.12 evals/families/replay.py --check [--quick]   # validate every task; prove solution passes and wrong fails the real judge
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

FLOW = "feature"
DEFAULT_N = 0
PREFIX = "train-replay"
TAG = "replay"


def all_tasks(seed=0, data_dir=None):
    import arch_replay
    return arch_replay.load_tasks(data_dir)


def generate(n, seed=0):
    """Up to n saved tasks, trainable ones first, round-robin over kinds so a short run touches every module family."""
    ts = all_tasks(seed)
    by = {}
    for t in ts:
        by.setdefault((("holdout" in t["tags"]), t.get("kind")), []).append(t)
    out = []
    for hold in (False, True):
        keys = sorted(k for k in by if k[0] == hold)
        i = 0
        while any(i < len(by[k]) for k in keys):
            out += [by[k][i] for k in keys if i < len(by[k])]
            i += 1
    return out[:max(0, n)]


def check(quick=False, seed=0):
    """-> exit code. Every task valid; the reference passes the real judge; the unchanged fixture fails it."""
    import tempfile
    import shutil
    import arch_eval as E
    import arch_replay as R
    tasks = [E.normalize(dict(t)) for t in all_tasks(seed)]
    if not tasks:
        print("no replay tasks yet: run  py -3.12 arch_replay.py --build")
        return 0
    if quick:
        tasks = tasks[:1]
    bad = 0
    for t in tasks:
        probs = E.validate(t)
        spec = t["replay"]
        res = {}
        for label, files in (("solution", t["solution"]["files"]), ("wrong", t["wrong"]["files"])):
            d = tempfile.mkdtemp(prefix="replaycheck_")
            try:
                for rel, text in files.items():
                    p = os.path.join(d, rel)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                res[label] = R.run_judge(d, spec, REPO)[0]
            finally:
                shutil.rmtree(d, ignore_errors=True)
        ok = not probs and res["solution"] is True and res["wrong"] is False
        bad += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {t['id']}" + ("" if ok else f"   {probs or res}"))
    print(f"\n{'ALL ' + str(len(tasks)) + ' REPLAY CHECKS PASSED' if not bad else str(bad) + ' of ' + str(len(tasks)) + ' REPLAY CHECKS FAILED'}")
    return 1 if bad else 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check(a.quick)
    for t in all_tasks():
        print(f"  {t['id']:40} {t['lesson']:15} {'HOLD ' if 'holdout' in t['tags'] else ''}{t['goal'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
