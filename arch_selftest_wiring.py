"""Self-test for arch_flows.wire_contract: the IDE-style quick fixes that make the interface a request states reachable, built from the
failure modes of 30 live whole-project builds (2026-11-04), and the rules that keep them honest: an undone try takes its wiring with
it, a person reviewing edits is never bypassed, nothing is done to an interface that already works, and a try passed with this help
is neither training data nor a few-shot example. Reviewed adversarially on 2026-11-05 (findings F1-F11 each have a check here).

    python arch_selftest_wiring.py
"""
import glob
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
NS_GOAL = "Public interface, exactly: `from tools import helper`."


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
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    try:
        import arch_core as core
        import arch_flows as F

        def engine(name, files, review="auto"):
            root = os.path.join(home, name)
            os.makedirs(root)
            _write(root, files)
            return core.Engine(root, "build", "g", review=review, say=lambda *a, **k: None, persist=False)

        def runs(root, *args):
            p = subprocess.run([sys.executable, "-B", *args], cwd=root, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1"))
            return p.returncode, p.stdout.strip()

        def read(e, rel):
            p = os.path.join(e.root, rel)
            return open(p, encoding="utf-8").read() if os.path.isfile(p) else None

        # 1. seen live: build_report written in salesreport/report.py, never exported; the CLI in cli.py and no __main__.py
        e = engine("sales", {"salesreport/__init__.py": '"""Sales reports."""\n',
                             "salesreport/report.py": "def build_report(csv_text):\n    return {'totals': {}, 'markdown': ''}\n",
                             "salesreport/cli.py": "import sys\n\ndef main(argv=None):\n    print('ran')\n    return 0\n"})
        ok0, _ = F.contract_check(e, REPORT_GOAL)
        fixes = F.wire_contract(e, REPORT_GOAL)
        ok1, text1 = F.contract_check(e, REPORT_GOAL)
        init = read(e, "salesreport/__init__.py")
        check("a name defined in one submodule and never exported is re-exported from __init__.py (the docstring is kept)",
              not ok0 and "from .report import build_report" in init and init.startswith('"""Sales reports."""'), init)
        check("`python -m pkg` with one argument-free main() gets a __main__.py that runs it, and the contract now passes",
              ok1 and runs(e.root, "-m", "salesreport") == (0, "ran") and {f for f, _ in fixes} == {"salesreport/__init__.py", "salesreport/__main__.py"},
              (ok1, text1, fixes))
        check("every fix says why, in words", all(why and ("export" in why or "python -m" in why) for _, why in fixes), fixes)
        check("running it again changes nothing (already wired)", F.wire_contract(e, REPORT_GOAL) == [])

        # 2. seen live: modules in inventory/ but no __init__.py, which the request names
        e = engine("inv", {"inventory/store.py": "def load(p):\n    return {}\n", "inventory/cli.py": "def main():\n    return 0\n"})
        fixes = F.wire_contract(e, INV_GOAL)
        check("an __init__.py the request names is created when the folder has modules, then the __main__.py",
              read(e, "inventory/__init__.py") is not None and runs(e.root, "-m", "inventory")[0] == 0
              and [f for f, _ in fixes] == ["inventory/__init__.py", "inventory/__main__.py"], fixes)

        # 3. F7: a namespace package whose stated import already works is left alone (no 'repair', no assist)
        e = engine("ns", {"tools/helper.py": "x = 1\n"})
        check("F7: an interface that already works (a namespace package) gets no wiring at all",
              F.wire_contract(e, NS_GOAL) == [] and read(e, "tools/__init__.py") is None)

        # 4. ambiguity and limits: the model decides (F2: every main() counts, not only argument-free ones)
        e = engine("amb", {"salesreport/__init__.py": "",
                           "salesreport/a.py": "def build_report(t):\n    return {}\n",
                           "salesreport/b.py": "def build_report(t):\n    return {}\n",
                           "salesreport/cli.py": "def main(argv):\n    return 0\n"})
        check("a name defined in two modules is left alone, and a main() that needs an argument gets no __main__.py",
              F.wire_contract(e, REPORT_GOAL) == [] and read(e, "salesreport/__main__.py") is None and read(e, "salesreport/__init__.py") == "")
        e = engine("demo", {"salesreport/__init__.py": "from .report import build_report\n",
                            "salesreport/report.py": "def build_report(t):\n    return {}\n\ndef main():\n    print('DEMO')\n",
                            "salesreport/cli.py": "def main(argv=None):\n    print('real')\n"})
        check("F2: two modules with a main() (a demo one and the real CLI) is ambiguous: no __main__.py, the check keeps failing",
              F.wire_contract(e, REPORT_GOAL) == [] and read(e, "salesreport/__main__.py") is None and not F.contract_check(e, REPORT_GOAL)[0])
        e = engine("moved", {"salesreport/__init__.py": "from .report import build_report\n",
                             "salesreport/report.py": "def build_report(t):\n    return {}\n\ndef main():\n    print('early')\n"})
        F.wire_contract(e, REPORT_GOAL)
        first = read(e, "salesreport/__main__.py")
        _write(e.root, {"salesreport/cli.py": "def main():\n    print('later')\n"})
        gone = F.wire_contract(e, REPORT_GOAL)
        check("F2: a __main__.py the wiring wrote is checked again; when a second main() appears it is removed, and the check says so",
              first and F.WIRED_MAIN in first and read(e, "salesreport/__main__.py") is None and gone and "removed" in gone[0][1]
              and not F.contract_check(e, REPORT_GOAL)[0], (first, gone))
        e = engine("own", {"salesreport/__init__.py": "from .report import build_report\n",
                           "salesreport/report.py": "def build_report(t):\n    return {}\n",
                           "salesreport/__main__.py": "print('mine')\n", "salesreport/cli.py": "def main():\n    pass\n"})
        check("a __main__.py the model wrote is never touched", F.wire_contract(e, REPORT_GOAL) == [] and read(e, "salesreport/__main__.py") == "print('mine')\n")

        # 5. F3: what the generated __main__.py does with main()'s result
        e = engine("async", {"salesreport/__init__.py": "from .report import build_report\n",
                             "salesreport/report.py": "def build_report(t):\n    return {}\n",
                             "salesreport/cli.py": "async def main():\n    return 0\n"})
        check("F3: an async main() is not wired (calling it would never run it)", F.wire_contract(e, REPORT_GOAL) == [])
        e = engine("truthy", {"salesreport/__init__.py": "from .report import build_report\n",
                              "salesreport/report.py": "def build_report(t):\n    return {}\n",
                              "salesreport/cli.py": "def main():\n    print('# report')\n    return '# report'\n"})
        F.wire_contract(e, REPORT_GOAL)
        rc_text = runs(e.root, "-m", "salesreport")
        e = engine("guard", {"salesreport/__init__.py": "from .report import build_report\n",
                             "salesreport/report.py": "def build_report(t):\n    return {}\n",
                             "salesreport/cli.py": "import sys\n\ndef main():\n    print('ok')\n    return 3\n\nif __name__ == '__main__':\n    main()\n"})
        F.wire_contract(e, REPORT_GOAL)
        check("F3: main() returning text exits 0; a module with its own main guard is run as __main__ (it ignored the 3, so exit 0)",
              rc_text == (0, "# report") and runs(e.root, "-m", "salesreport") == (0, "ok") and "runpy" in (read(e, "salesreport/__main__.py") or ""),
              (rc_text, read(e, "salesreport/__main__.py")))

        # 6. F5/F6: never bypass a person reviewing edits; dry runs write nothing; an undo puts back the exact bytes
        for mode in ("ask", "dry"):
            e = engine("mode_" + mode, {"salesreport/report.py": "def build_report(t):\n    return {}\n"}, review=mode)
            check(f"F5/F6: review '{mode}' gets no wiring (a person decides; the contract check's words reach the model instead)",
                  F.wire_contract(e, REPORT_GOAL) == [] and read(e, "salesreport/__init__.py") is None)
        e = engine("broken", {"salesreport/__init__.py": "# keep me\r\n",
                              "salesreport/report.py": "import not_a_real_module_xyz\n\ndef build_report(t):\n    return {}\n"})
        before = open(os.path.join(e.root, "salesreport", "__init__.py"), "rb").read()
        check("F6: a re-export that still does not import is undone byte for byte (CRLF kept), and nothing is reported",
              F.wire_contract(e, REPORT_GOAL) == [] and open(os.path.join(e.root, "salesreport", "__init__.py"), "rb").read() == before)
        e = engine("none", {"app.py": "x = 1\n"})
        check("a request that names no interface, or a package not built yet, is left alone",
              F.wire_contract(e, "make it faster") == [] and F.wire_contract(e, REPORT_GOAL) == [])

        # 7. F1: wiring is registered with the best-try undo, so an undone try takes its wiring with it
        e = engine("undo", {"salesreport/__init__.py": '"""S."""\n', "salesreport/report.py": "def build_report(t):\n    return {}\n"})
        best = F.BestState(e)
        best.judge({"text": "", "results": [], "tests": ("", "1 passed in 0.1s")})          # the kept version
        r = e.put_file("salesreport/report.py", "def other():\n    return 1\n", label="try", quiet=True)
        best.note([r])
        r = e.put_file("salesreport/core.py", "def build_report(t):\n    return {}\n", label="try", quiet=True)
        best.note([r])
        wired = F.wire_contract(e, REPORT_GOAL, note=best.note)
        why = best.judge({"text": "", "results": [{"ok": False, "name": "salesreport/report.py parses"}], "tests": ("", "")})
        check("F1: a try that is undone takes its wiring with it: __init__.py is back as it was and the package imports",
              wired and why and read(e, "salesreport/__init__.py") == '"""S."""\n' and read(e, "salesreport/core.py") is None
              and runs(e.root, "-c", "import salesreport")[0] == 0, (wired, why, read(e, "salesreport/__init__.py")))

        # 8. F4/F8: the import check keeps the network guard first on the path and runs bounded with no console input
        src = open(F.__file__, encoding="utf-8").read()
        env = F._sealed_env(e)
        g = core.guard_env(True).get("PYTHONPATH", "")
        check("F4/F8: the project is added AFTER the network guard on PYTHONPATH, and the import check uses run_bounded",
              env["PYTHONPATH"].startswith(g.split(os.pathsep)[0]) and env["PYTHONPATH"].rstrip(os.pathsep).endswith(e.root)
              and "core.run_bounded([sys.executable, \"-B\", \"-c\", stmt]" in src)

        # 9a. found by the F11 replay: the request's example command is not a file to create
        check("a backticked command example (`python -m salesreport IN.csv OUT.md`) names no file, even after other code spans",
              F.named_files("Build `salesreport` with a `date,region,amount` header and a `| Region | Total |` table. "
                            "`python -m salesreport IN.csv OUT.md` writes it. Tests in tests/test_report.py.") == ["tests/test_report.py"],
              F.named_files("Build `salesreport` with a `date,region,amount` header and a `| Region | Total |` table. "
                            "`python -m salesreport IN.csv OUT.md` writes it. Tests in tests/test_report.py."))

        # 9. F11: through the real build flow with a scripted model whose reply is right but miswired
        import arch_eval as E
        import arch_roles
        task = [t for t in E.load_tasks(["build"]) if t["id"].startswith("e2e-sales-report")][0]
        sol = dict(task["solution"]["files"])
        sol["salesreport/__init__.py"] = '"""Sales CSV to a Markdown report."""\n'
        sol.pop("salesreport/__main__.py")
        sol["salesreport/cli.py"] = ('import sys\n\nfrom .report import build_report\n\n\ndef main(argv=None):\n'
                                     '    argv = sys.argv[1:] if argv is None else argv\n    with open(argv[0], encoding="utf-8") as f:\n'
                                     '        r = build_report(f.read())\n    with open(argv[1], "w", encoding="utf-8") as f:\n'
                                     '        f.write(r["markdown"])\n    return 0\n')
        task = dict(task, solution={"files": sol})
        fake = {"roles": {r_: "fake:7b" for r_ in arch_roles.ROLES}, "default_model": "fake:7b", "behavior": {"learn_from_runs": "off"}}
        srv, prev = E.start_oracle([task])
        try:
            res = E.run_task(task, fake, keep=True)
        finally:
            E.stop_oracle(srv, prev)
        recs = []
        for t in glob.glob(os.path.join(res.get("proj", ""), ".arch", "runs", "*", "trace.jsonl")):
            recs += [json.loads(x) for x in open(t, encoding="utf-8") if x.strip()]
        assists = {r_["id"] for r_ in recs if r_.get("type") == "assist"}
        accepted = {r_["id"] for r_ in recs if r_.get("type") == "mark" and r_.get("accepted")}
        check("F11: through the real build flow, a right-but-miswired reply passes the hidden check, and that accepted try is traced `assist`",
              res["verdict"] == "pass" and assists and assists & accepted, (res["verdict"], res.get("detail"), assists, accepted))
        shutil.rmtree(res.get("proj", ""), ignore_errors=True)

        # 10. training data and the few-shot ledger: an accepted try that needed the help is neither
        import arch_dataset as DS
        import arch_ledger as LG
        rd = os.path.join(home, "proj", ".arch", "runs", "r1")
        os.makedirs(rd)
        trace = [{"type": "call", "id": 1, "role": "generator", "model": "w", "label": "sprint1-try1",
                  "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "build the sales report package"}], "reply": "FILE: x"},
                 {"type": "mark", "id": 1, "accepted": True},
                 {"type": "assist", "id": 1, "fixes": [{"file": "salesreport/__init__.py", "why": "export"}]},
                 {"type": "call", "id": 2, "role": "generator", "model": "w", "label": "sprint2-try1",
                  "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "write the tests for the package"}], "reply": "FILE: y"},
                 {"type": "mark", "id": 2, "accepted": True}, {"type": "verdict", "verdict": "pass"}]
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r_) for r_ in trace))
        data = DS.build([os.path.join(home, "proj")], dedupe=False)
        n_sft = sum(len(v) for v in data["sft"].values())
        check("an accepted try that passed only with the wiring is not a training sample; an unaided one still is",
              data["stats"]["assisted"] == 1 and n_sft == 1, (data["stats"], n_sft))
        old_eval = os.environ.pop("ARCHITECT_EVAL", None)
        try:
            LG.record_run(rd, "r1-wiring-test")
        finally:
            if old_eval is not None:
                os.environ["ARCHITECT_EVAL"] = old_eval
        ex = [x["req"] for x in (LG.load().get("_examples") or [])]
        check("F10: nor is it kept as a few-shot 'past reply of yours that passed'; the unaided one is",
              not any("sales report" in x for x in ex) and any("write the tests" in x for x in ex), ex)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' WIRING CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' WIRING CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
