"""First visit: the guided path in the hub (hub_ui.html) and its two endpoints (architect_hub.py).

What is proven here, without a model, a GPU or the network:
  - the practice project is written ONLY under Architect's data folder, always from the same bug, and its tests are sound:
    the shipped code fails exactly the one test the request names, and a correct fix passes them all;
  - /api/arch/practice starts the normal run (review auto, the bug-fix flow) in that folder, and refuses while a job runs;
  - first_visit_info reports the machine, Get ready and the team in plain words, and one failing part never blanks the others;
  - the page wires it up: the endpoints it calls exist, every page note names a real page, the path has three stops (ux_audit) and is
    remembered, and the narrow-screen rules are there.

Run: python arch_selftest_firstvisit.py   ->   ALL N FIRST-VISIT CHECKS PASSED
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def run():
    tmp = tempfile.mkdtemp(prefix="arch_fv_")
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.makedirs(os.environ["ARCHITECT_DATA_DIR"], exist_ok=True)
    sys.path.insert(0, HERE)
    passed, failed = [], []

    def check(name, ok, extra=""):
        (passed if ok else failed).append(name)
        print(("  ok    " if ok else "  FAIL  ") + name + (f"  ({extra})" if extra and not ok else ""))

    try:
        import arch_core as core
        import architect_hub as hub
        import model_hub as mh

        # ---- the practice project ----
        root = hub.practice_root()
        data = os.path.abspath(core.data_dir())
        check("the practice folder is inside Architect's own data folder", os.path.abspath(root).startswith(data + os.sep), root)
        got = hub.practice_prepare()
        check("practice_prepare returns the practice folder", os.path.abspath(got) == os.path.abspath(root))
        check("it writes exactly the two practice files", sorted(os.listdir(root)) == sorted(hub.PRACTICE_FILES), str(os.listdir(root)))
        with open(os.path.join(root, "textkit.py"), "a", encoding="utf-8") as f:
            f.write("\n# an earlier practice run changed this\n")
        hub.practice_prepare()
        with open(os.path.join(root, "textkit.py"), encoding="utf-8") as f:
            check("every practice run starts from the same bug (files rewritten)", f.read() == hub.PRACTICE_FILES["textkit.py"])

        def load(path, name):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        def outcomes(module_src):
            d = tempfile.mkdtemp(dir=tmp)
            with open(os.path.join(d, "textkit.py"), "w", encoding="utf-8") as f:
                f.write(module_src)
            with open(os.path.join(d, "test_textkit.py"), "w", encoding="utf-8") as f:
                f.write(hub.PRACTICE_FILES["test_textkit.py"])
            sys.modules.pop("textkit", None)
            sys.path.insert(0, d)
            try:
                t = load(os.path.join(d, "test_textkit.py"), "test_textkit_" + os.path.basename(d))
                res = {}
                for n in sorted(x for x in dir(t) if x.startswith("test_")):
                    try:
                        getattr(t, n)()
                        res[n] = True
                    except AssertionError:
                        res[n] = False
                return res
            finally:
                sys.path.remove(d)
                sys.modules.pop("textkit", None)

        shipped = outcomes(hub.PRACTICE_FILES["textkit.py"])
        check("the shipped code fails exactly the repeated-spaces test", shipped == {"test_punctuation": True, "test_repeated_spaces": False, "test_simple": True}, str(shipped))
        fixed_src = hub.PRACTICE_FILES["textkit.py"].replace('return text.replace(" ", "-")', 'return re.sub(r"\\s+", "-", text)')
        check("the reference fix is a real change", fixed_src != hub.PRACTICE_FILES["textkit.py"])
        fixed = outcomes(fixed_src)
        check("a correct fix passes every practice test", fixed and all(fixed.values()), str(fixed))
        check("the request names the file, the function and the test file",
              all(w in hub.PRACTICE_GOAL for w in ("textkit.py", "slugify", "test_textkit.py")))

        # ---- /api/arch/practice ----
        class FakeJob:
            def __init__(self, busy):
                self.busy, self.started, self.epoch = busy, None, 6

            def running(self):
                return self.busy

            def start(self, title, argv):
                self.started, self.epoch = (title, argv), self.epoch + 1

        real_job = mh.JOB
        try:
            mh.JOB = FakeJob(busy=True)
            try:
                hub.AHandler.route(None, "/api/arch/practice", {})
                check("the practice task is refused while another job runs", False, "no error")
            except RuntimeError as e:
                check("the practice task is refused while another job runs", "already running" in str(e), str(e))
            check("a refused practice task starts nothing", mh.JOB.started is None)
            mh.JOB = FakeJob(busy=False)
            r = hub.AHandler.route(None, "/api/arch/practice", {})
            argv = mh.JOB.started[1] if mh.JOB.started else []
            check("the practice task starts a normal run", argv[:2] == ["architect.py", "run"], str(argv))
            check("it runs in the practice folder only", "--root" in argv and os.path.abspath(argv[argv.index("--root") + 1]) == os.path.abspath(root), str(argv))
            check("changes in the scratch folder are applied without asking (review auto)", "--review" in argv and argv[argv.index("--review") + 1] == "auto")
            import arch_flows as fl
            if "bugfix" in fl.FLOWS:
                check("it uses the bug-fix workflow", "--flow" in argv and argv[argv.index("--flow") + 1] == "bugfix", str(argv))
            check("the reply carries the run's epoch, so the page can tell it from the next job", r.get("ok") and r.get("epoch") == 7, str(r))
            check("the reply names the practice folder", os.path.abspath(r.get("root") or "") == os.path.abspath(root))
        finally:
            mh.JOB = real_job

        # ---- first_visit_info ----
        def doctor_ok(root_, port):
            return {"ok": False, "checks": [{"name": "python", "ok": True, "detail": "3.12"},
                                            {"name": "pytest", "ok": False, "detail": "not installed", "advisory": False},
                                            {"name": "git", "ok": False, "detail": "not found", "advisory": True}]}
        plan = {"tiers": {"worker": {"model": "qwen-coder:7b", "why": "fits the card", "full_gpu": True},
                          "router": {"model": "qwen:0.6b", "cpu": True}, "heavy": None, "draft": {}},
                "notes": ["a note"]}
        info = hub.first_visit_info(tmp, 8766, doctor_run=doctor_ok, team_plan=plan)
        m = info["machine"]
        check("machine: passed and total are counted", m.get("passed") == 1 and m.get("total") == 3, str(m))
        check("machine: only a non-advisory failure blocks", m.get("blocking") == ["pytest"], str(m.get("blocking")))
        t = info["team"]
        check("team: all four jobs are described in plain words", [x["tier"] for x in t["tiers"]] == ["worker", "router", "heavy", "draft"] and all(x["what"] for x in t["tiers"]))
        w = next(x for x in t["tiers"] if x["tier"] == "worker")
        check("team: the worker's recommendation and where it runs", w["recommended"] == "qwen-coder:7b" and w["on_card"] and not w["cpu"], str(w))
        check("team: a missing tier in the plan (None) does not break it", next(x for x in t["tiers"] if x["tier"] == "heavy")["recommended"] == "")
        check("team: has_worker when a worker is recommended", t["has_worker"] is True)
        check("team: notes are passed on", t["notes"] == ["a note"])
        check("practice: the folder and the request are in the reply", info["practice"]["root"] == root and info["practice"]["goal"] == hub.PRACTICE_GOAL)
        check("ready: a fresh data folder says Get ready has not run", info["ready"] and info["ready"]["ran"] is False, str(info["ready"]))

        def doctor_boom(root_, port):
            raise OSError("disk unplugged")
        _real_load = hub.core.load_config                     # this check is about a machine with no worker: do not read YOUR settings
        hub.core.load_config = lambda *a, **k: dict(_real_load(*a, **k), tiers={})
        try:
            info2 = hub.first_visit_info(tmp, 8766, doctor_run=doctor_boom, team_plan={"tiers": {"worker": None}})
        finally:
            hub.core.load_config = _real_load
        check("a checkup that fails is reported with its reason", "disk unplugged" in (info2["machine"] or {}).get("error", ""), str(info2["machine"]))
        check("... and the team part still answers", isinstance(info2["team"].get("tiers"), list) and info2["team"]["has_worker"] is False, str(info2["team"]))

        class BadPlan(dict):                       # a tier picker whose answer cannot be read
            def get(self, *a, **k):
                raise ValueError("picker failed")
        info3 = hub.first_visit_info(tmp, 8766, doctor_run=doctor_ok, team_plan=BadPlan())
        check("a team picker that fails is reported, and has_worker is False", "picker failed" in info3["team"].get("error", "") and info3["team"]["has_worker"] is False, str(info3["team"]))
        check("... and the machine part still answers", info3["machine"].get("total") == 3)

        # ---- the page and the server agree ----
        with open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8") as f:
            ui = f.read()
        with open(os.path.join(HERE, "architect_hub.py"), encoding="utf-8") as f:
            src = f.read()
        check("the server answers /api/arch/first_visit", 'u.path == "/api/arch/first_visit"' in src)
        check("the server answers /api/arch/practice", 'path == "/api/arch/practice"' in src)
        check("the page asks /api/arch/first_visit and /api/arch/practice", "/api/arch/first_visit" in ui and '"/api/arch/practice"' in ui)
        steps = re.search(r"FV_STEPS=\[([^\]]*)\]", ui)
        check("the guided path has three stops: This computer, Practice task, Result", steps and re.findall(r'"([^"]+)"', steps.group(1)) == ["This computer", "Practice task", "Result"])
        check("Skip sticks: no async tick re-opens a skipped tour", "FV.closed=true;fvSave({status:\"skipped\"})" in ui and "if(FV.closed||s2.status!==\"open\"" in ui)
        check("where you were is remembered (firstVisit in this browser)", 'FV_KEY="firstVisit"' in ui and "store.set(FV_KEY" in ui)
        for st in ("done", "skipped", "later"):
            check(f"a '{st}' tour does not open by itself again", f's.status==="{st}"' in ui)
        check("the tour can be skipped and resumed from Home", "function fvSkip" in ui and "function fvEntry" in ui and "fvEntry()" in ui)
        check("it opens by itself only after the first state has loaded", "refresh(true).then(()=>setTimeout(fvMaybeOpen" in ui)
        check("Esc closes the tour for now without stopping a run (handled on the dialog)", 'e.key==="Escape"){e.preventDefault();e.stopPropagation();fvLater();}' in ui)
        check("the dialog is announced as a modal dialog", 'class:"fv",role:"dialog","aria-modal":"true"' in ui)
        check("the result is read back from the run's own events (right after a reload too)", '/api/arch/events?since=0' in ui and "fvLoadResult" in ui)
        check("not sure is its own outcome on the result, never a pass or a fail", "not_sure:[\"unsure\"" in ui)
        import arch_uireg
        pages = set(arch_uireg.load(os.path.join(HERE, "hub_ui.html"))["page_ids"])
        hints = re.findall(r"^  ([a-z]+):\[\"", ui.split("const HINTS={", 1)[1].split("};", 1)[0], re.M)
        check("there is a page note for every major page", {"overview", "console", "tiers", "agents", "fleet", "monitor", "map", "queue", "settings", "doctor"} <= set(hints), str(hints))
        bad = [x for x in hints if x not in pages]
        check("every page note names a real page", not bad, str(bad))
        check("page notes are shown from go() and dismissed for good", "pageHint(id);" in ui and 'store.set("hint."+id,"1")' in ui and 'id="pageHint"' in ui)
        css = ui.split("/* ===== First visit", 1)[1].split("</style>", 1)[0] if "/* ===== First visit" in ui else ""
        check("the narrow-screen rules exist (375 px)", "@media (max-width:600px)" in css and ".fv-next{grid-template-columns:minmax(0,1fr)}" in css)
        check("colours come from the theme tokens (dark and light)", "var(--panel)" in css and "var(--ink)" in css and "#fff" not in css)
        check("the page reaches nothing off this machine", not re.search(r"(?:fetch|api|XMLHttpRequest|src\s*[:=])\(?[^\n]{0,40}https?://(?!127\.0\.0\.1|localhost)[a-z]", ui.split("const FV_KEY", 1)[1].split("go(\"console\");refresh(true)", 1)[0]))
    finally:
        sys.path.remove(HERE) if HERE in sys.path else None
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print(f"{len(failed)} of {len(passed) + len(failed)} FIRST-VISIT CHECKS FAILED: " + "; ".join(failed))
        return 1
    print(f"ALL {len(passed)} FIRST-VISIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
