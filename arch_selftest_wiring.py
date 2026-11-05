"""Self-test for arch_flows.wire_contract: the IDE-style quick fixes that make the interface a request states reachable, built from the
failure modes of 30 live whole-project builds (2026-11-04), and the rule that a try passed with this help never becomes training data.

    python arch_selftest_wiring.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPORT_GOAL = ("Build a Python package `salesreport` (folder salesreport/ with __init__.py and __main__.py). Public interface, exactly: "
               "`from salesreport import build_report`; `build_report(csv_text)` returns a dict. `python -m salesreport IN.csv OUT.md` "
               "writes the markdown and exits 0.")
INV_GOAL = ("Build a command-line inventory tool as a Python package `inventory` (folder inventory/ with __init__.py and __main__.py). "
            "It is run as `python -m inventory --db PATH <command>`.")


def _write(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if not ok and extra else ""))

    home = tempfile.mkdtemp(prefix="wiring_")
    old = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    try:
        import arch_core as core
        import arch_flows as F

        def engine(name, files, review="auto"):
            root = os.path.join(home, name)
            os.makedirs(root)
            _write(root, files)
            return core.Engine(root, "build", "g", review=review, say=lambda *a, **k: None, persist=False)

        def runs(root, *args):
            p = subprocess.run([sys.executable, "-B", *args], cwd=root, capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1"))
            return p.returncode, p.stdout.strip()

        # 1. seen live: build_report written in salesreport/report.py, never exported; the CLI in cli.py and no __main__.py
        e = engine("sales", {"salesreport/__init__.py": '"""Sales reports."""\n',
                             "salesreport/report.py": "def build_report(csv_text):\n    return {'totals': {}, 'markdown': ''}\n",
                             "salesreport/cli.py": "import sys\n\ndef main(argv=None):\n    print('ran')\n    return 0\n"})
        ok0, _ = F.contract_check(e, REPORT_GOAL)
        fixes = F.wire_contract(e, REPORT_GOAL)
        ok1, text1 = F.contract_check(e, REPORT_GOAL)
        init = open(os.path.join(e.root, "salesreport", "__init__.py"), encoding="utf-8").read()
        check("a name defined in one submodule and never exported is re-exported from __init__.py (the docstring is kept)",
              not ok0 and "from .report import build_report" in init and init.startswith('"""Sales reports."""'), init)
        check("`python -m pkg` with main() in exactly one module gets a __main__.py that runs it, and the contract now passes",
              ok1 and runs(e.root, "-m", "salesreport") == (0, "ran") and {f for f, _ in fixes} == {"salesreport/__init__.py", "salesreport/__main__.py"},
              (ok1, text1, fixes))
        check("every fix says why, in words", all(why and ("export" in why or "python -m" in why) for _, why in fixes), fixes)
        check("running it again changes nothing (already wired)", F.wire_contract(e, REPORT_GOAL) == [])

        # 2. seen live: modules in inventory/ but no __init__.py at all
        e = engine("inv", {"inventory/store.py": "def load(p):\n    return {}\n",
                           "inventory/cli.py": "def main():\n    return 0\n"})
        fixes = F.wire_contract(e, INV_GOAL)
        check("a package folder with modules but no __init__.py gets one, then its __main__.py",
              os.path.isfile(os.path.join(e.root, "inventory", "__init__.py")) and runs(e.root, "-m", "inventory")[0] == 0
              and [f for f, _ in fixes] == ["inventory/__init__.py", "inventory/__main__.py"], fixes)

        # 3. ambiguity and limits: the model decides
        e = engine("amb", {"salesreport/__init__.py": "",
                           "salesreport/a.py": "def build_report(t):\n    return {}\n",
                           "salesreport/b.py": "def build_report(t):\n    return {}\n",
                           "salesreport/cli.py": "def main(argv):\n    return 0\n"})
        check("a name defined in two modules is left alone, and a main() that needs an argument gets no __main__.py",
              F.wire_contract(e, REPORT_GOAL) == [] and not os.path.isfile(os.path.join(e.root, "salesreport", "__main__.py"))
              and open(os.path.join(e.root, "salesreport", "__init__.py"), encoding="utf-8").read() == "")
        e = engine("broken", {"salesreport/__init__.py": "",
                              "salesreport/report.py": "import not_a_real_module_xyz\n\ndef build_report(t):\n    return {}\n"})
        check("a fix that still does not import is undone (the file is as it was), and nothing is reported",
              F.wire_contract(e, REPORT_GOAL) == [] and open(os.path.join(e.root, "salesreport", "__init__.py"), encoding="utf-8").read() == "")
        e = engine("dry", {"salesreport/report.py": "def build_report(t):\n    return {}\n"}, review="dry")
        check("a dry run writes nothing", F.wire_contract(e, REPORT_GOAL) == [] and not os.path.isfile(os.path.join(e.root, "salesreport", "__init__.py")))
        e = engine("none", {"app.py": "x = 1\n"})
        check("a request that names no interface, or a package not built yet, is left alone",
              F.wire_contract(e, "make it faster") == [] and F.wire_contract(e, REPORT_GOAL) == [])

        # 4. the build flow uses it, and a try it helped is traced as `assist`
        src = open(F.__file__, encoding="utf-8").read()
        check("the build sprints run the wiring before the checks and trace an assisted try",
              src.index("wired = wire_contract(e, goal)") < src.index("v = v0 = verify(e, changed)") and '"type": "assist"' in src)

        # 5. training data: an accepted try that needed the help is not a sample
        import arch_dataset as DS
        rd = os.path.join(home, "proj", ".arch", "runs", "r1")
        os.makedirs(rd)
        recs = [{"type": "call", "id": 1, "role": "generator", "model": "w", "label": "sprint1-try1",
                 "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "build the sales report package"}], "reply": "FILE: x"},
                {"type": "mark", "id": 1, "accepted": True},
                {"type": "assist", "id": 1, "fixes": [{"file": "salesreport/__init__.py", "why": "export"}]},
                {"type": "call", "id": 2, "role": "generator", "model": "w", "label": "sprint2-try1",
                 "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "write the tests for the package"}], "reply": "FILE: y"},
                {"type": "mark", "id": 2, "accepted": True}]
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs))
        data = DS.build([os.path.join(home, "proj")], dedupe=False)
        n_sft = sum(len(v) for v in data["sft"].values())
        check("an accepted try that passed only with the wiring is not a training sample; an unaided one still is",
              data["stats"]["assisted"] == 1 and n_sft == 1, (data["stats"], n_sft))
    finally:
        if old is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' WIRING CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' WIRING CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
