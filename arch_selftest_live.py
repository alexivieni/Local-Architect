"""Self-test for the live-test module itself: its judges must pass a correct result and fail each kind of wrong one, and the runner must report
failures instead of crashing. (Whether YOUR models pass is what `python architect.py livetest` finds out; no model is involved here.)"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_livetest as lt
    home = tempfile.mkdtemp(prefix="archlive_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    core.set_home(home)
    os.environ.pop("ARCHITECT_DATA_DIR", None)

    def proj():
        return tempfile.mkdtemp(prefix="j_", dir=home)

    try:
        # ---- build judge
        good = {"calc.py": "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n\ndef multiply(a, b):\n    return a * b\n\ndef divide(a, b):\n    return a / b\n",
                "tests/test_calc.py": "import unittest\nimport calc\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n    def test_div(self):\n        with self.assertRaises(ZeroDivisionError):\n            calc.divide(1, 0)\n"}
        p = proj(); lt._write(p, good)
        ok, why = lt.judge_build(p)
        check("build judge: a correct module with passing tests passes", ok, why)
        p = proj(); lt._write(p, dict(good, **{"calc.py": good["calc.py"].replace("a + b", "a - b")}))
        ok, why = lt.judge_build(p)
        check("build judge: wrong behaviour is caught by the hidden check even though the model's own tests pass", not ok and any("hidden behaviour" in w for w in why), why)
        p = proj(); lt._write(p, {"calc.py": good["calc.py"]})
        ok, why = lt.judge_build(p)
        check("build judge: no tests written is a failure", not ok and any("tests" in w for w in why), why)
        p = proj()
        ok, why = lt.judge_build(p)
        check("build judge: nothing written is a failure, not a crash", not ok)

        # ---- bugfix judge
        before = hashlib.sha1(lt.STATS_TEST.encode()).hexdigest()
        p = proj(); lt._write(p, {"stats.py": lt.STATS.replace("(len(xs) - 1)", "len(xs)"), "tests/test_stats.py": lt.STATS_TEST})
        ok, why = lt.judge_bugfix(p, before)
        check("bugfix judge: fixing the code passes", ok, why)
        p = proj(); lt._write(p, {"stats.py": lt.STATS, "tests/test_stats.py": lt.STATS_TEST.replace("4)", "6)")})
        ok, why = lt.judge_bugfix(p, before)
        check("bugfix judge: editing the test to match the bug is caught", not ok and any("edited the tests" in w for w in why), why)
        p = proj(); lt._write(p, {"stats.py": lt.STATS, "tests/test_stats.py": lt.STATS_TEST})
        ok, why = lt.judge_bugfix(p, before)
        check("bugfix judge: an unchanged bug fails", not ok and any("still there" in w for w in why), why)

        # ---- feature judge
        feat = lt.INV + "\n\ndef low_stock(items, threshold):\n    return sorted(n for n, q in items.items() if q < threshold)\n"
        p = proj(); lt._write(p, {"inventory.py": feat, "tests/test_inventory.py": lt.INV_TEST})
        ok, why = lt.judge_feature(p)
        check("feature judge: the new function plus every old one passes", ok, why)
        p = proj(); lt._write(p, {"inventory.py": "def low_stock(items, threshold):\n    return sorted(n for n, q in items.items() if q < threshold)\n", "tests/test_inventory.py": lt.INV_TEST})
        ok, why = lt.judge_feature(p)
        check("feature judge: replacing the file with only the new function (the classic small-model failure) is caught", not ok and any("lost" in w for w in why) and any("cut down" in w for w in why), why)
        p = proj(); lt._write(p, {"inventory.py": lt.INV, "tests/test_inventory.py": lt.INV_TEST})
        ok, why = lt.judge_feature(p)
        check("feature judge: not adding the function fails", not ok and any("low_stock" in w for w in why), why)

        # ---- cicd judge
        fixed = "name: CI\non: [push]\npermissions:\n  contents: read\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: python -m unittest discover -s tests -q\n"
        p = proj(); lt._write(p, {".github/workflows/ci.yml": fixed, "tests/test_a.py": lt.CI_TEST})
        ok, why = lt.judge_cicd(p)
        check("cicd judge: a pipeline with permissions, a real test step and no swallowed failures passes", ok, why)
        p = proj(); lt._write(p, {".github/workflows/ci.yml": lt.BAD_CI, "tests/test_a.py": lt.CI_TEST})
        ok, why = lt.judge_cicd(p)
        check("cicd judge: the original weak pipeline fails, naming what is wrong", not ok and any("swallowed" in w for w in why), why)
        p = proj(); lt._write(p, {".github/workflows/ci.yml": fixed.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: echo hi\n"), "tests/test_a.py": lt.CI_TEST})
        ok, why = lt.judge_cicd(p)
        check("cicd judge: deleting the test step to make the checker happy is caught", not ok and any("test step was removed" in w for w in why), why)

        # ---- launching like an app
        import arch_launch as al
        page = "<html><head><title>Architect hub</title></head>"
        check("an Architect hub that is already open is found and reused", al.running_hub(8766, get=lambda u: page if u.endswith(":8768/") else (_ for _ in ()).throw(OSError())) == "http://127.0.0.1:8768/")
        check("some other web page on that port is not mistaken for the hub", al.running_hub(8766, get=lambda u: "<html><title>Jenkins</title>") is None)
        opened = []
        check("open_existing opens the page and says it did; with none running it does nothing", al.open_existing(8766, opener=opened.append, get=lambda u: page) is True and opened and
              al.open_existing(8766, opener=opened.append, get=lambda u: (_ for _ in ()).throw(OSError())) is False and len(opened) == 1)
        desk = tempfile.mkdtemp(prefix="desk_", dir=home)
        sc = al.make_shortcut(desk, platform="linux")
        check("a desktop launcher is written, executable, and points at this install", sc and os.access(sc, os.X_OK) and "architect_hub.py" in open(sc).read() and sc.endswith(".desktop"), sc)
        check("windows and mac launchers are the right kind of file", al.make_shortcut(desk, platform="win32").endswith(".bat") and al.make_shortcut(desk, platform="darwin").endswith(".command"))
        open(os.path.join(desk, "Architect.desktop"), "w").write("someone else's file")
        check("a different file with that name is never overwritten", al.make_shortcut(desk, platform="linux") is None and open(os.path.join(desk, "Architect.desktop")).read() == "someone else's file")
        check("no Desktop folder means no shortcut, not a crash", al.make_shortcut(os.path.join(home, "nope"), platform="linux") is None)

        # ---- the runner
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        lines = []
        out = lt.run(quick=True, say=lines.append)
        check("with no model answering, every scenario fails with the reason, nothing crashes, and the result is saved",
              out["total"] == 3 and out["passed"] == 0 and all("not reachable" in r["detail"] or "stopped" in r["detail"] or r["detail"] for r in out["results"])
              and json.load(open(lt.path()))["total"] == 3, out["results"])
        check("--only picks scenarios by id, in a stable order", [r["id"] for r in lt.run(only=["json", "route"], say=lambda m: None)["results"]] == ["route", "json"])
        check("every scenario has an id, a plain title and a function", all(i and t and callable(f) for i, t, f in lt.SCENARIOS) and len({i for i, _, _ in lt.SCENARIOS}) == len(lt.SCENARIOS))
        check("a scenario that raises its own bug is reported as the test failing, not as a model failure",
              "the test itself failed" in lt.run(only=["x"], say=lambda m: None, scenarios=[("x", "boom", lambda pr: 1 / 0)])["results"][0]["detail"])
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' LIVE-TEST CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' LIVE-TEST CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
