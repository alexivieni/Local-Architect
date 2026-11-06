"""Self-test for the ETA (arch_eta), the queue's estimate, and the data night (tools/train_traces.py) with a fake clock and fake tasks.

No model, no GPU, no real eval: the night's runner, export and clock are stand-ins. Runs in a throwaway data folder.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archeta_")
os.environ["ARCHITECT_DATA_DIR"] = _TMP
os.environ["ARCHITECT_HOME"] = _TMP                     # the settings file lives in HOME: never touch the real one
os.environ["ARCHITECT_NO_CACHE"] = "1"
os.environ["ARCHITECT_NO_NOTIFY"] = "1"


def _tt():
    spec = importlib.util.spec_from_file_location("train_traces", os.path.join(HERE, "tools", "train_traces.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== ETA ==")
    import arch_eta as E
    try:
        E._MEMO.update(t=0.0, rows=None)
        pt = E.per_task(rows=[], own=[])
        check("nothing measured -> a stated guess, said out loud", pt["n"] == 0 and "guess" in pt["source"] and pt["p50"] == E.PRIOR_S, pt)
        pt = E.per_task(rows=[], own=[100, 200, 300])
        check("a run's own pace beats history after 3 tasks", pt["p50"] == 200 and "own pace" in pt["source"], pt)
        rows = [{"kind": "distributed", "model": "m8", "runs": [{"secs": 60}, {"secs": 120}, {"secs": 180}]},
                {"kind": "other", "model": "x", "runs": [{"secs": 1000}]}]
        pt = E.per_task(family="distributed", model="m8", rows=rows)
        check("family+model match is used first", pt["p50"] == 120 and "distributed tasks on m8" in pt["source"], pt)
        pt = E.per_task(family="distributed", model="zzz", rows=rows)
        check("then the family alone", pt["p50"] == 120 and "any model" in pt["source"], pt)
        pt = E.per_task(model="x", rows=rows)
        check("then the model alone", pt["p50"] == 1000, pt)
        e = E.estimate(10, 40, own=[360, 360, 360], now=1_000_000.0, rows=[])
        check("30 tasks x 360 s = 3 h, the line says so",
              e["left"] == 30 and e["secs_mid"] == 10800 and "3.0 h left" in e["line"] and e["line"].startswith("10/40"), e["line"])
        check("ends = now + median", abs(e["ends"] - 1_010_800.0) < 1)
        check("a range is shown when p25-p75 is wide", "(" in E.estimate(0, 10, own=[100, 200, 900], now=0, rows=[])["line"])
        e = E.estimate(5, 5, own=[10, 10, 10], rows=[])
        check("nothing left -> finished", e["line"] == "5/5, finished" and e["left"] == 0, e)
        e = E.estimate(0, 20, own=[100, 100, 100], now=1000.0, until=1000.0 + 450, rows=[])
        check("an end time caps the work: 4 fit in 450 s", e["fits"] == 4 and "4 more fit" in e["line"] and e["ends"] <= 1450.0, e["line"])
        check("human() units", E.human(30) == "30 s" and E.human(600) == "10 min" and E.human(7200) == "2.0 h")
        check("eval_line is a bracketed suffix", E.eval_line(3, 10, [60, 60, 60]).startswith("  [3/10, ~7 min left"), E.eval_line(3, 10, [60, 60, 60]))
        jobs = [{"state": "done", "flow": "bugfix"}, {"state": "pending", "flow": "bugfix", "minutes": 10},
                {"state": "running", "flow": "bugfix", "minutes": 10, "started": time.time() - 100}]
        q = E.queue_eta(jobs, rows=[])
        check("queue: 1 of 3 done, 2 left, never beyond each task's own time limit",
              q["done"] == 1 and q["total"] == 3 and q["left"] == 2 and q["secs_hi"] <= 2 * 600, q)
        check("empty queue says nothing waiting", "nothing waiting" in E.queue_eta([], rows=[])["line"])
        import arch_supervisor as sv
        check("the supervisor exposes eta()", isinstance(sv.eta(jobs), dict) and "line" in sv.eta(jobs))
    except Exception as ex:
        check("ETA checks ran without an exception", False, repr(ex))

    print("\n== data night ==")
    tt = _tt()
    try:
        check("until_epoch: a past time means tomorrow", tt.until_epoch("00:00", now=time.time()) > time.time())
        try:
            tt.until_epoch("25:99")
            check("a bad end time is refused", False)
        except ValueError:
            check("a bad end time is refused", True)
        s1 = tt.fresh_seed()
        s2 = tt.fresh_seed()
        check("a fresh seed per night (never the same twice)", s1 != s2 and s2 > s1, (s1, s2))
        clock = [1_000_000.0]
        now = lambda: clock[0]                                                              # noqa: E731
        exported = []
        log = []

        def load(tag):
            return [{"id": f"{tag}{i}"} for i in range(5)]

        def runner(tasks, label, cfg_over, should_stop, say):
            got = []
            for t in tasks:
                if should_stop():
                    break
                clock[0] += 100
                say("  PASS " + t["id"])
                got.append({"verdict": "pass", "id": t["id"]})
            return got

        def export(say):
            exported.append(1)
            return 0
        r = tt.night(until=1_000_000.0 + 350, say=log.append, runner=runner, export=export, load=load, now=now, notify=False, seed=1)
        check("stops cleanly before the end time and still exports", r["ran"] == 3 and "end time" in r["stopped_by"] and exported == [1] and r["exported"], r)
        check("progress lines carry an ETA", any(l.strip().startswith("1/") or "left" in l or "fit" in l for l in log), log[-6:])
        clock[0] = 1_000_000.0
        r = tt.night(max_tasks=4, say=log.append, runner=runner, export=export, load=load, now=now, notify=False, seed=2)
        check("--max-tasks N stops after N", r["ran"] == 4 and "task limit" in r["stopped_by"], r)
        stop = os.path.join(_TMP, "STOP")
        open(stop, "w").close()
        r = tt.night(say=log.append, runner=runner, export=export, load=load, now=now, notify=False, seed=3)
        os.remove(stop)
        check("the STOP file ends the night before any task", r["ran"] == 0 and "STOP" in r["stopped_by"], r)
        r = tt.night(say=log.append, runner=runner, export=export, load=load, now=now, notify=False, seed=4)
        check("with no limit every task runs", r["ran"] == 10 and r["stopped_by"] == "every task ran", r)
        r = tt.night(say=log.append, runner=runner, export=export, load=load, now=now, notify=False)
        check("no seed given -> a fresh one", r["seed"] not in (1, 2, 3, 4) and r["seed"] > 0, r)
        sent = []
        import arch_notify
        real = arch_notify.notify
        arch_notify.notify = lambda t, x="", *a, **k: sent.append((t, x))
        try:
            tt.night(max_tasks=1, say=log.append, runner=runner, export=export, load=load, now=now, notify=True, seed=9)
        finally:
            arch_notify.notify = real
        check("the night notifies once at its end", len(sent) == 1 and "data night" in sent[0][0].lower(), sent)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        check("data night checks ran without an exception", False, repr(ex))
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(run())
