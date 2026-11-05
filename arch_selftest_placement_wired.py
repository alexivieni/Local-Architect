"""Self-test: the background chores really go through arch_placement.run_low (the efficiency cores, below-normal priority).

For each wired chore -- the run archive's write-behind copy (arch_archive.archive_run), the overseer's `watch` scans
(arch_overseer.main), the Logbook's command-line ingest and playbooks (arch_learn.main) and the learning sync (tools/sync_learning.py)
-- three runs over the same throwaway data: on a CPU with efficiency cores 8-11, on one without any, and with
behavior.chores_on_ecores = off. run_low and the OS calls are recorded (a stand-in OS; nothing is really pinned), and the three runs
must leave exactly the same result behind. No model, no GPU, no network; ARCHITECT_DATA_DIR and the settings home are temporary.

    py -3.12 arch_selftest_placement_wired.py
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ECORES = 0xF00                       # logical processors 8-11, as on the i5-13420H
NOW = 1_800_000_000.0
MODES = ("ecores", "none", "off")    # efficiency cores present / a CPU without them / the opt-out setting


def _write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding=None if "b" in mode else "utf-8") as f:
        f.write(text)


def _jl(path, rows):
    _write(path, "".join(json.dumps(r) + "\n" for r in rows))


def _tree(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for fn in fs:
            p = os.path.join(dp, fn)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = fh.read()
    return out


class FakeOS:
    """Stands in for SetThreadAffinityMask / SetThreadPriority, so the checks see what would have been pinned."""

    def __init__(self):
        self.masks, self.lowered = [], 0

    def affinity(self, m):
        self.masks.append(m)
        return True

    def lower(self):
        self.lowered += 1
        return True


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_hw
    import arch_placement as P

    base = tempfile.mkdtemp(prefix="arch_wired_")
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_HOME")}
    old_home, old_cwd = core.HOME, os.getcwd()
    real = {"run_low": P.run_low, "pin": P._pin_this_thread, "mask": arch_hw.ecore_mask}
    rec = {"calls": [], "pins": [], "os": FakeOS(), "mask": ECORES}

    def rec_run_low(fn, *a, **kw):
        rec["calls"].append({"fn": getattr(fn, "__name__", repr(fn)), "wait": kw.get("wait", True), "daemon": kw.get("daemon", True)})
        return real["run_low"](fn, *a, **kw)

    def rec_pin(mask, api=None):
        rec["pins"].append((mask, threading.current_thread().name))
        return real["pin"](mask, rec["os"])                  # the stand-in OS: nothing on this machine is really pinned

    def settings(mode, extra=None):
        """Write the (temporary) settings file for one mode and point ecore_mask at the matching CPU."""
        cfg = dict(extra or {})
        if mode == "off":
            cfg["behavior"] = {"chores_on_ecores": "off"}
        with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        rec.update(calls=[], pins=[], os=FakeOS(), mask=0 if mode == "none" else ECORES)
        return cfg

    def data(name):
        d = os.path.join(base, name, "data")
        os.makedirs(d, exist_ok=True)
        os.environ["ARCHITECT_DATA_DIR"] = d
        return d

    def judge(chore, sigs, logs, want_fn=None):
        """The same three checks for every chore, from what each mode recorded."""
        e, n, o = logs["ecores"], logs["none"], logs["off"]
        ok_fn = (lambda c: True) if want_fn is None else (lambda c: c["fn"] == want_fn)
        check(f"{chore}: goes through run_low, pinned to efficiency cores 8-11 at below-normal priority",
              e["calls"] and all(ok_fn(c) for c in e["calls"]) and e["pins"] and all(m == ECORES and t == "architect-low" for m, t in e["pins"])
              and e["os"].masks == [ECORES] * len(e["pins"]) and e["os"].lowered == len(e["pins"]), (e["calls"], e["pins"], e["os"].masks))
        check(f"{chore}: a CPU without efficiency cores: still run_low, no pinning at all, priority lowered, same result",
              n["calls"] and n["os"].masks == [] and n["os"].lowered == len(n["pins"]) >= 1 and all(m == 0 for m, _ in n["pins"])
              and sigs["none"] == sigs["off"], (n["calls"], n["pins"], n["os"].masks))
        check(f"{chore}: behavior.chores_on_ecores = off: run_low is not used and nothing is lowered (as before)",
              o["calls"] == [] and o["pins"] == [], (o["calls"], o["pins"]))
        check(f"{chore}: the result is the same on the efficiency cores as without them and as before", sigs["ecores"] == sigs["off"]
              and sigs["off"] is not None, {m: (str(s)[:120] if s is not None else None) for m, s in sigs.items()})

    def snap():
        return {"calls": list(rec["calls"]), "pins": list(rec["pins"]), "os": rec["os"]}

    try:
        core.set_home(os.path.join(base, "home"))
        P.run_low, P._pin_this_thread = rec_run_low, rec_pin
        arch_hw.ecore_mask = lambda: rec["mask"]

        # ---- the switch itself
        check("chores_low: missing setting = auto = on", P.chores_low({}) is True and P.chores_low({"behavior": {}}) is True)
        check("chores_low: auto on, off / false off", P.chores_low({"behavior": {"chores_on_ecores": "auto"}}) is True
              and P.chores_low({"behavior": {"chores_on_ecores": "off"}}) is False and P.chores_low({"behavior": {"chores_on_ecores": False}}) is False)
        settings("off")
        check("chores_low() reads the settings file (off)", P.chores_low() is False)
        settings("ecores")
        check("chores_low() reads the settings file (no setting: on)", P.chores_low() is True)
        with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        check("an unreadable settings file counts as auto (on)", P.chores_low() is True)
        rec.update(calls=[], pins=[])
        th = P.run_chore(lambda: 41 + 1, cfg={"behavior": {"chores_on_ecores": "off"}}, wait=False, daemon=False)
        th.join(5)
        check("run_chore off, wait=False: a plain normal-priority thread with the result, run_low untouched",
              th.result == 42 and th.error is None and th.daemon is False and th.name == "architect-chore" and rec["calls"] == [] and rec["pins"] == [],
              (th.result, th.name, rec["calls"]))
        check("run_chore off, waited for: called right here", P.run_chore(threading.current_thread, cfg={"behavior": {"chores_on_ecores": "off"}})
              is threading.current_thread())
        th = real["run_low"](lambda: 7, wait=False, mask=0, api=FakeOS(), daemon=False)
        th.join(5)
        check("run_low(daemon=False): the process waits for it (a write-behind copy is not cut off at exit)", th.daemon is False and th.result == 7)
        check("run_low on the real OS with no efficiency cores: no pinning, works", real["run_low"](lambda: "ok", mask=0) == "ok")

        # ---- the placement plan says what really happens
        lap = dict(hw={"gpu": "RTX 4050", "vendor": "nvidia", "vram_gb": 6.0, "ram_gb": 31.6, "ram_free_gb": 20, "cpu_cores": 12, "cpu_perf_cores": 4},
                   drs=[{"mount": "C:\\", "fs": "NTFS", "total_gb": 999, "free_gb": 200, "bus": "nvme", "removable": False, "suspect": ""}],
                   speeds={}, bw={"gpu_bw": 158.0, "ram_bw": 19.5}, ecores=ECORES, models="C:\\m", data_dir=base, app_dir=base)
        on = {r["work"]: r for r in P.compute(P.facts(chores=True, **lap))}
        off = {r["work"]: r for r in P.compute(P.facts(chores=False, **lap))}
        check("plan: every wired chore on the efficiency cores", len(P.BACKGROUND) == 4 and all(
              (on.get(j) or {}).get("place") == "cpu-e" and "run_low" in (on.get(j) or {}).get("why", "") for j in P.BACKGROUND),
              [on.get(j) for j in P.BACKGROUND])
        check("plan with the setting off: the chores are said to run at normal priority on any core", all(
              (off.get(j) or {}).get("place") == "cpu" and "chores_on_ecores = off" in (off.get(j) or {}).get("why", "") for j in P.BACKGROUND),
              [off.get(j) for j in P.BACKGROUND])
        check("plan: the index refresh is said to stay with the run (the prompt waits for it)", on.get("index refresh", {}).get("place") == "cpu"
              and "waits" in on["index refresh"]["why"], on.get("index refresh"))

        # ---- 1. the run archive's write-behind copy
        import arch_archive
        d = data("archive")
        rdir = os.path.join(base, "archive", "run")
        for fn in ("run.json", "trace.jsonl", "report.md"):
            _write(os.path.join(rdir, fn), fn * 3)
        arc, gone = os.path.join(base, "archive", "card"), os.path.join(base, "archive", "unplugged")
        sigs, logs, daemons = {}, {}, {}
        for mode in MODES:
            shutil.rmtree(arc, ignore_errors=True)
            os.makedirs(arc)
            cfg = settings(mode)
            arch_archive._save_pending([])
            t1 = arch_archive.archive_run(dict(cfg, storage={"archive": gone}), rdir, "proj:1", "r1", wait=True)
            pend = [p.get("run_id") for p in arch_archive._load_pending()]
            t2 = arch_archive.archive_run(dict(cfg, storage={"archive": arc}), rdir, "proj:1", "r2", wait=True)
            daemons[mode] = (t1.daemon, t2.daemon, t1.is_alive(), t2.is_alive())
            sigs[mode] = (pend, _tree(arc), [p.get("run_id") for p in arch_archive._load_pending()])
            logs[mode] = snap()
        check("archive: unplugged drive remembered, the next run copies both (as before)", sigs["off"][0] == ["r1"]
              and sorted({k.split("/")[2] for k in sigs["off"][1]}) == ["r1", "r2"] and sigs["off"][2] == [], sigs["off"][:1])
        check("archive: run_low is asked for a thread the process waits for (wait=False, daemon=False)",
              logs["ecores"]["calls"] and all(c["wait"] is False and c["daemon"] is False for c in logs["ecores"]["calls"]), logs["ecores"]["calls"])
        check("archive: the copy thread is never a daemon, in every mode", all(v[:2] == (False, False) and not any(v[2:]) for v in daemons.values()), daemons)
        judge("archive (arch_archive.archive_run)", sigs, logs, want_fn="work")

        # ---- 2. the overseer's watch scans
        import arch_overseer as ov
        d = data("overseer")
        proj = os.path.join(base, "overseer", "proj")
        empty_temp = os.path.join(base, "overseer", "temp")
        os.makedirs(empty_temp, exist_ok=True)
        _jl(os.path.join(d, "learning", "ledger.jsonl"), [{"agent": "ag_old", "status": "done", "cost_usd": 0.5, "turns": "7", "logged": "2026-11-04 19:44"}])
        _jl(os.path.join(d, "cc_runs", "ccA.jsonl"), [
            {"t": NOW - 100, "type": "system.init", "text": "session S9 · model claude-opus-5-5 · 26 tools · mode acceptEdits"},
            {"t": NOW - 99, "type": "tool", "name": "Edit", "id": "t1", "arg": os.path.join(proj, "a.py"),
             "text": json.dumps({"file_path": os.path.join(proj, "a.py"), "old_string": "x", "new_string": "y"})},
            {"t": NOW - 98, "type": "tool_result", "id": "t1", "error": False, "text": "updated"}])
        _write(os.path.join(d, "cc_runs", "ccA.meta.json"), json.dumps({
            "prompt": "You are the delta agent for Architect.", "mode": "acceptEdits", "root": proj, "started": NOW - 100, "ended": NOW - 50,
            "status": "done", "cost": 0.2, "turns": 3, "session": "S9", "id": "ccA"}))

        class _Clock:                                        # the watch loop's time module: one scan, then Ctrl+C at the first sleep
            def __getattr__(self, n):
                import time as _t
                return getattr(_t, n)

            @staticmethod
            def sleep(_s):
                raise KeyboardInterrupt
        real_time = ov.time
        key = os.path.abspath(d)
        sigs, logs, rcs = {}, {}, {}
        try:
            ov.time = _Clock()
            for mode in MODES:
                shutil.rmtree(os.path.join(d, "learning", "overseer"), ignore_errors=True)
                ov._SHARED[key] = ov.Overseer(d, temp_dir=empty_temp, project=proj, clock=lambda: NOW)
                settings(mode)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rcs[mode] = ov.main(["watch", "--every", "2"])
                s = ov._SHARED[key].summary()
                sigs[mode] = (json.dumps(s, sort_keys=True, default=str), out.getvalue().split("\n", 2)[-1])
                logs[mode] = snap()
        finally:
            ov.time = real_time
            ov._SHARED.pop(key, None)
        check("overseer watch: one scan per period, Ctrl+C ends it with 0, the agents are seen", rcs == {m: 0 for m in MODES}
              and "delta agent" in sigs["off"][0] and "ag_old" in sigs["off"][0], (rcs, sigs["off"][0][:200]))
        judge("overseer watch scans (arch_overseer.main watch)", sigs, logs, want_fn="scan")

        # ---- 3. the Logbook's command-line ingest and playbooks
        import arch_learn as L
        d = data("logbook")
        proj = os.path.join(base, "logbook", "proj")
        rd = os.path.join(proj, ".arch", "runs", "r1")
        _jl(os.path.join(rd, "trace.jsonl"), [
            {"type": "call", "t": NOW, "model": "fake:7b", "role": "coder", "label": "sprint 1", "goal": "add a csv export"},
            {"type": "mark", "t": NOW + 1, "ok": True, "label": "tests"},
            {"type": "verdict", "t": NOW + 2, "verdict": "pass"}])
        _write(os.path.join(rd, "run.json"), json.dumps({"goal": "add a csv export", "flow": "build"}))
        _jl(os.path.join(d, "cc_runs", "x1.jsonl"), [{"t": NOW, "type": "assistant", "text": "fixed it", "model": "claude-opus-5-5"}])
        _write(os.path.join(d, "cc_runs", "x1.meta.json"), json.dumps({"prompt": "fix the parser", "status": "done", "cost": 0.1}))

        def table(sql):
            con = sqlite3.connect(L.db_path())
            try:
                return [tuple(r) for r in con.execute(sql)]
            finally:
                con.close()
        sigs, logs = {}, {}
        try:
            os.chdir(proj)                                   # ingest reads the current folder's runs
            for mode in MODES:
                for p in (L.db_path(), L.playbooks_path()):
                    if os.path.exists(p):
                        os.remove(p)
                settings(mode)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc1 = L.main(["ingest"])
                    rc2 = L.main(["playbooks"])
                with open(L.playbooks_path(), encoding="utf-8") as f:
                    pb = f.read()
                sigs[mode] = (rc1, rc2, out.getvalue(), table("SELECT * FROM tasks ORDER BY id"), table("SELECT * FROM steps ORDER BY task_id, n"), pb)
                logs[mode] = snap()
        finally:
            os.chdir(old_cwd)
        first = json.loads(sigs["off"][2].splitlines()[0])
        check("logbook CLI: ingest finds the run and the Claude run, playbooks are written", sigs["off"][:2] == (0, 0)
              and first.get("added") == 2 and len(sigs["off"][3]) == 2 and "[build]" in sigs["off"][5], (first, sigs["off"][5][-200:]))
        check("logbook CLI: both commands went through run_low", [c["fn"] for c in logs["ecores"]["calls"]] == ["ingest", "playbooks"],
              logs["ecores"]["calls"])
        judge("logbook ingest + playbooks (arch_learn.main)", sigs, logs)

        # ---- 4. the learning sync
        d = data("sync")
        drive = os.path.join(base, "sync", "card")
        lead = os.path.join(base, "sync", "lead")
        app = os.path.join(base, "sync", "app")
        _write(os.path.join(d, "learning", "LESSONS.md"), "# Lessons\n- read less\n")
        _write(os.path.join(d, "memory", "notes.md"), "a note\n")
        _write(os.path.join(d, "logbook.db"), b"SQLite format 3\x00 not really", "wb")
        _jl(os.path.join(d, "claude_code", "projects", "p", "s1.jsonl"), [{"type": "user", "text": "token sk-ant-api03-" + "A" * 40}])
        _jl(os.path.join(lead, "lead.jsonl"), [{"type": "user", "text": "hello"}])
        _write(os.path.join(app, "skills", "tests.md"), "# a skill\n")
        _write(os.path.join(app, "docs", "plan.md"), "# a plan\n")
        spec = importlib.util.spec_from_file_location("sync_learning_wired", os.path.join(HERE, "tools", "sync_learning.py"))
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        sync.LEAD, sync.APP, sync.DRY = lead, app, False      # never the real transcripts or the real repo
        sigs, logs = {}, {}
        for mode in MODES:
            shutil.rmtree(drive, ignore_errors=True)
            os.makedirs(drive)
            settings(mode, {"storage": {"archive": drive}})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = sync.cli()
            sigs[mode] = (rc, _tree(drive))
            logs[mode] = snap()
        files = sigs["off"][1]
        check("sync: everything is copied, the secret is scrubbed on the way", sigs["off"][0] == 0
              and "Architect/learning/knowledge/logbook.db" in files and "Architect/learning/conversations/lead/lead.jsonl" in files
              and "Architect/learning/knowledge/skills/tests.md" in files
              and b"A" * 40 not in files.get("Architect/learning/conversations/agents/s1.jsonl", b"A" * 40), sorted(files))
        judge("learning sync (tools/sync_learning.py cli)", sigs, logs, want_fn="main")
    finally:
        P.run_low, P._pin_this_thread = real["run_low"], real["pin"]
        arch_hw.ecore_mask = real["mask"]
        os.chdir(old_cwd)
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(base, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' WIRED-PLACEMENT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' WIRED-PLACEMENT CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
