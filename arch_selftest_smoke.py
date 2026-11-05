"""Self-test for arch_smoke.smoke_contract: the request's own first usages, actually run, at the end of a build.

For each whole-project (e2e) task: the reference solution passes the smoke, and what the deliberately wrong solution does is reported
(its mistakes sit beyond the first call, so the smoke is expected to pass it; the hidden judges catch those). Mistakes AT the first
call -- the inventory `--db` declared on the subparsers, a store that refuses a missing file, a GET that crashes, a server that cannot be
shut down, a CLI that crashes on its arguments -- fail with a clear message. Ambiguous requests are skipped (None), placeholders never
leave a temp folder, and the oracle build flow still passes every e2e/build/feature task end to end with the hook in place.

    py -3.12 arch_selftest_smoke.py
"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "evals", "families"))


def _write(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


def _tree(root):
    out = set()
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != "__pycache__"]
        out |= {os.path.relpath(os.path.join(dp, f), root) for f in fn}
    return out


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if not ok and extra else ""))

    home = tempfile.mkdtemp(prefix="smoke_")
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    try:
        import arch_core as core
        import arch_smoke as S
        import e2e
        old_home = core.HOME
        core.set_home(home)

        def project(name, files):
            root = os.path.join(home, name)
            os.makedirs(root)
            _write(root, files)
            return types.SimpleNamespace(root=root)

        tmp_root = os.path.normcase(os.path.realpath(tempfile.gettempdir()))

        # ---- 1. the four e2e tasks: reference passes; the wrong one is reported
        report = {}
        for name, goal, _hidden, sol, wrong, _must in e2e.PROJECTS:
            e = project(name + "-ref", sol)
            before = _tree(e.root)
            d = []
            ok, text = S.smoke_contract(e, goal, d)
            check(f"{name}: the reference solution passes the smoke", ok is True, text)
            check(f"{name}: running it left the project folder as it was (placeholders live in a temp folder)", _tree(e.root) == before,
                  sorted(_tree(e.root) - before))
            paths = [p for r in d for p in r.get("paths") or []]
            check(f"{name}: every placeholder file was inside the temp folder, and that folder is gone afterwards",
                  all(os.path.normcase(os.path.realpath(p)).startswith(tmp_root) and not os.path.exists(os.path.dirname(p)) for p in paths), paths)
            w = project(name + "-wrong", wrong)
            wok, wtext = S.smoke_contract(w, goal)
            report[name] = "fails" if wok is False else "skipped" if wok is None else "passes (its mistake is beyond the first call)"
            check(f"{name}: the wrong solution gets a definite outcome ({report[name]})", wok in (True, False, None), wtext)
        print("    wrong solutions: " + "; ".join(f"{k} {v}" for k, v in report.items()))

        # ---- 2. mistakes at the first call fail, with words that say what to fix
        inv = dict(e2e.INV_SOLUTION)
        inv["inventory/__main__.py"] = inv["inventory/__main__.py"].replace(
            '    ap.add_argument("--db", required=True)\n', "").replace(
            '        p.add_argument("name")\n', '        p.add_argument("--db", required=True)\n        p.add_argument("name")\n').replace(
            '    ls.add_argument("--json", action="store_true")\n', '    ls.add_argument("--json", action="store_true")\n    ls.add_argument("--db", required=True)\n')
        ok, text = S.smoke_contract(project("inv-subdb", inv), e2e.INV_GOAL)
        check("inventory with --db on the subparsers fails the smoke", ok is False, text)
        check("... and the message names the command, the exit status and where --db belongs",
              text.startswith(S.LABEL) and "python -m inventory --db PATH add item 1" in text and "exited 2" in text
              and "`--db` BEFORE the command" in text and "main parser" in text, text)

        sh = dict(e2e.SHORT_SOLUTION)
        sh["shortener/store.py"] = sh["shortener/store.py"].replace("        if os.path.isfile(path):\n    ", "        if True:\n    ")
        ok, text = S.smoke_contract(project("short-nofile", sh), e2e.SHORT_GOAL)
        check("a Shortener(path) that refuses a missing file (the request says it is created) fails, naming the call",
              ok is False and "Shortener(path)" in text and "FileNotFoundError" in text, text)
        sh2 = dict(e2e.SHORT_SOLUTION)
        sh2["shortener/codes.py"] = sh2["shortener/codes.py"].replace('f"{salt}:{url}"', 'f"{salt}:{url}:{__import__(\'random\').random()}"')
        sh2["shortener/core.py"] = sh2["shortener/core.py"].replace("        if known:\n            return known\n", "")
        ok, text = S.smoke_contract(project("short-random", sh2), e2e.SHORT_GOAL)
        check("s.shorten(url) giving a new code each time fails (the request says the same url always gets the same code)",
              ok is False and "s.shorten(url)" in text and "then" in text, text)

        td = dict(e2e.TODO_SOLUTION)
        td["todo_api/store.py"] = td["todo_api/store.py"].replace("            return [dict(v) for _, v in sorted(self.items.items())]",
                                                                  "            return [dict(v) for _, v in sorted(self.itemz.items())]")
        ok, text = S.smoke_contract(project("todo-500", td), e2e.TODO_GOAL)
        check("a GET /todos that crashes the handler fails the smoke (the request says 200)", ok is False and "GET /todos" in text and "no HTTP answer" in text and "AttributeError" in text, text)
        td2 = dict(e2e.TODO_SOLUTION)
        td2["todo_api/server.py"] = td2["todo_api/server.py"].replace(
            '    return ThreadingHTTPServer(("127.0.0.1", port), Handler)',
            '    class Srv(ThreadingHTTPServer):\n        def serve_forever(self, poll_interval=0.5):\n            self.handle_request()\n\n'
            '    return Srv(("127.0.0.1", port), Handler)')
        import time
        t0 = time.time()
        ok, text = S.smoke_contract(project("todo-noshut", td2), e2e.TODO_GOAL)
        check("a server whose shutdown would hang is reported within seconds, not left running", ok is False and "shutdown" in text
              and time.time() - t0 < 40, (round(time.time() - t0, 1), text))
        d = []
        t0 = time.time()
        ok, _ = S.smoke_contract(project("todo-clean", e2e.TODO_SOLUTION), e2e.TODO_GOAL, d)
        check("the reference server exercise starts, answers one GET and shuts down cleanly (quickly, exit 0)",
              ok is True and d and d[0]["rc"] == 0 and time.time() - t0 < 30, (d, round(time.time() - t0, 1)))

        sr = dict(e2e.REPORT_SOLUTION)
        sr["salesreport/__main__.py"] = sr["salesreport/__main__.py"].replace("sys.argv[1], sys.argv[2]", "sys.argv[2], sys.argv[3]")
        ok, text = S.smoke_contract(project("sales-argv", sr), e2e.REPORT_GOAL)
        check("python -m salesreport IN.csv OUT.md that crashes on its arguments fails, saying what IN.csv was",
              ok is False and "IndexError" in text and "header row" in text, text)

        # ---- 3. ambiguous requests are skipped, never guessed
        e = project("amb", {"tool/__init__.py": "", "tool/__main__.py": "print('x')\n", "x.py": "def f(a, limit=3):\n    return a\n"})
        amb = [
            ("a command placeholder with no listed commands", "Build `python -m tool <command>`. Successful commands exit 0."),
            ("a data file that may not exist", "Build `python -m tool --db PATH add NAME`. Commands: `add NAME`. Exit 0 on success."),
            ("an input whose content the request does not give", "Build `python -m tool IN.txt OUT.md`, exits 0."),
            ("keyword arguments", "Public interface: `from x import f`; `f(1, limit=3)`."),
            ("an argument with no known placeholder", "Public interface: `from x import f`; `f(data)`."),
            ("a port without a loopback http.server", "Public interface: `from x import f`; `f(port)`."),
            ("a url the code may fetch", "Fetch pages. Public interface: `from x import f`; `f(url)`."),
            ("a long-running serve command", "Run `python -m tool serve`."),
            ("no usage at all", "Make the code faster."),
        ]
        for why, goal in amb:
            ok, text = S.smoke_contract(e, goal)
            check(f"skipped (None): {why}", ok is None and text, (ok, text))
        exs, skipped = S.plan("Build `python -m tool --db PATH <command>` (PATH created if missing). Commands: `list`, `remove NAME QTY`, `add NAME QTY`.")
        check("listed commands run creating ones first, removing ones last, in the request's order otherwise",
              [x["display"].split()[5] for x in exs] == ["add", "list", "remove"], [x["display"] for x in exs])

        # ---- 4. the oracle build flow, with the hook in place, still passes every e2e / build / feature task end to end
        import arch_eval as E
        import arch_flows as F
        seen = []
        real = S.smoke_contract

        def spy(e_, goal_, *a, **k):
            r = real(e_, goal_, *a, **k)
            seen.append((goal_[:40], r[0]))
            return r
        S.smoke_contract = spy
        try:
            tasks = E.load_tasks(flows=["build", "feature"], quick=True)
            ids = {t["id"] for t in tasks}
            tasks += [t for t in E.load_tasks(flows=["build"]) if "e2e" in t["tags"] and t["id"] not in ids]
            rep = E.run(fake=True, tasks=tasks, label="smoke", say=lambda *a, **k: None)
            bad = [(r["id"], r["verdict"], r.get("error"), [j["reason"] for j in r["judges"] if not j["ok"]]) for r in rep["results"] if r["verdict"] != "pass"]
            n_e2e = sum(1 for t in tasks if "e2e" in t["tags"])
            check(f"oracle: all {rep['n']} build/feature tasks ({n_e2e} e2e) still pass end to end with the smoke in the build flow",
                  rep["n"] == len(tasks) and n_e2e >= 4 and not bad, bad)
            check("the hook really ran the smoke in those builds, and it passed every e2e reference (never failed a correct build)",
                  sum(1 for _, r in seen if r is True) >= 4 and not any(r is False for _, r in seen), seen)

            # the hook fails the try: an oracle whose 'solution' is the --db-on-subparser CLI never passes its build
            bad_task = dict([t for t in tasks if t["id"].startswith("e2e-inventory")][0])
            bad_task["solution"] = {"files": inv}
            bad_task["id"] = "e2e-inventory-subdb"
            seen.clear()
            rep = E.run(fake=True, tasks=[bad_task], label="smoke-bad", say=lambda *a, **k: None)
            check("with the --db-on-subparser CLI the smoke fails inside the build, so the build does not pass",
                  any(r is False for _, r in seen) and rep["results"][0]["verdict"] != "pass", (seen, rep["results"][0]["verdict"]))
        finally:
            S.smoke_contract = real
        check("arch_flows imports with the hook", hasattr(F, "BuildFlow"))
        core.set_home(old_home)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
