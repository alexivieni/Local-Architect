"""Self-test for the supervisor: the queue the machine works through on its own.

What is being held to account:
  * a task that fails is tried again with a DIFFERENT flow, never the same one twice, and is given up on honestly when the
    alternatives run out;
  * "not sure" is treated as "not done", not as a pass;
  * it cannot run away: the kill switch and the daily limits are checked before every task;
  * a crash costs one task, not the queue;
  * two supervisors can never take the same task;
  * the verdict recorded is the one the run actually wrote, read from its own trace.

No model, no GPU, no network: the run itself is replaced by a scripted stand-in, because what is under test is the supervisor.
"""
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

    print("\n== the supervisor: a queue it works through on its own ==")
    import arch_core as core
    import arch_supervisor as sv

    home = tempfile.mkdtemp(prefix="archsv_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    proj = os.path.join(home, "proj")
    os.makedirs(proj)
    cfg = core.deep_merge(core.load_config(proj), {"supervisor": {"daily_runs": 0, "daily_minutes": 0}})
    quiet = lambda *_a, **_k: None
    try:
        # ---- the queue itself
        a = sv.add("fix the failing login test", proj, "bugfix")
        b = sv.add("add a health endpoint", proj)
        check("tasks go on the queue in order", [r["id"] for r in sv.listing()] == [b, a])
        check("an empty task is refused with a reason", _raises(lambda: sv.add("   ", proj)))
        j1, j2 = sv.claim(), sv.claim()
        check("two claims never take the same task", j1 and j2 and j1["id"] != j2["id"], (j1, j2))
        check("a claimed task is marked running and its attempt is counted", sv.get(j1["id"])["state"] == "running" and j1["attempts"] == 1)
        check("with nothing pending, a claim returns nothing", sv.claim() is None)
        check("a task whose supervisor is alive is never taken back, even with an old heartbeat", sv.recover(stale_after=0) == 0, sv.counts())
        import subprocess as _sp, sqlite3 as _sq
        dead = _sp.Popen([sys.executable, "-c", "pass"]); dead.wait()          # a real pid that has ended: the supervisor that was killed
        _c = _sq.connect(sv.db_path()); _c.execute("update jobs set owner_pid=?, owner_start=null where state='running'", (dead.pid,)); _c.commit(); _c.close()
        n = sv.recover()
        check("a task left running by a supervisor that was killed goes back on the queue", n == 2 and sv.counts()["pending"] == 2, sv.counts())
        sv.clear(("pending",))

        hi = sv.add("urgent", proj, priority=5)
        lo = sv.add("whenever", proj)
        check("a higher priority goes first", sv.claim()["id"] == hi)
        check("and cancelling only works on a task that has not started",
              sv.cancel(lo) is True and sv.cancel(hi) is False and sv.get(lo)["state"] == "cancelled")
        sv.clear(("pending", "running", "cancelled"))

        # ---- choosing what to try next
        check("after a bug fix fails, the next approach is a bug hunt", sv.next_flow("bugfix", ["bugfix"])[0] == "hunt")
        check("and after that, a build", sv.next_flow("bugfix", ["bugfix", "hunt"])[0] == "build")
        check("an approach already tried is never tried again", sv.next_flow("bugfix", ["bugfix", "hunt", "build"]) == (None, ""))
        check("every alternative says why it is worth trying", all(why for alts in sv.ALTERNATES.values() for _, why in alts))
        check("a flow with no alternatives listed still gets one fallback",
              sv.next_flow("ml", ["ml"])[0] == "bugfix" and sv.next_flow("ml", ["ml", "bugfix"])[0] is None)
        check("retries stay anchored to what was asked: a failed bug fix never drifts into a report-only review",
              sv.next_flow("bugfix", ["bugfix", "hunt"], current="hunt")[0] == "build")

        # ---- running tasks with a scripted stand-in for the real run
        script = {"bugfix": "fail", "hunt": "not_sure", "build": "pass"}
        seen = []

        def runner(job, say):
            flow = job.get("flow") or "bugfix"
            seen.append(flow)
            return {"verdict": script.get(flow, "fail"), "why": f"{flow} said so", "run_id": f"run-{flow}", "flow": flow}

        t = sv.add("the login page crashes on an empty password", proj, "bugfix", max_attempts=3)
        r1 = sv.run_one(say=quiet, runner=runner, cfg=cfg)
        check("a failed task is recorded as failed, with the run's own reason", sv.get(t)["state"] == "failed" and sv.get(t)["why"] == "bugfix said so")
        check("and is put back on the queue with a different flow", r1["retry"] and r1["retry"]["flow"] == "hunt", r1)
        retry = sv.get(r1["retry"]["id"])
        check("the retry remembers what has been tried and where it came from",
              retry["tried"] == ["bugfix"] and retry["parent"] == t and retry["note"].startswith("after bugfix ended 'fail'"), retry)
        r2 = sv.run_one(say=quiet, runner=runner, cfg=cfg)
        check("'not sure' is not a pass: it is tried again too", r2["verdict"] == "not_sure" and r2["retry"] and r2["retry"]["flow"] == "build", r2)
        r3 = sv.run_one(say=quiet, runner=runner, cfg=cfg)
        check("the third approach passes and the task is done", r3["verdict"] == "pass" and sv.get(r3["id"])["state"] == "done" and not r3["retry"], r3)
        check("each approach was tried exactly once, in order", seen == ["bugfix", "hunt", "build"], seen)

        always = sv.add("an impossible task", proj, "bugfix", max_attempts=3)
        out = sv.supervise(hours=1, max_tasks=10, say=quiet, runner=lambda job, say: {"verdict": "fail", "why": "no", "flow": job["flow"] or "bugfix"}, cfg=cfg)
        last = sv.listing()[0]
        check("when every approach has failed, the task is left for a person instead of looping",
              last["state"] == "dead" and "no other approach" in last["why"], last)
        check("and the supervisor stops because the queue is empty, not because it gave up early", out["why"] == "the queue is empty", out["why"])
        check("it never tried more approaches than allowed", len([r for r in sv.listing() if r["goal"] == "an impossible task"]) == 3)

        # ---- it cannot run away
        sv.add("a task", proj)
        sv.stop(True)
        check("with the kill switch on, nothing new starts", sv.run_one(say=quiet, runner=runner, cfg=cfg) is None and sv.counts()["pending"] == 1)
        sv.stop(False)
        capped = core.deep_merge(cfg, {"supervisor": {"daily_runs": 1}})
        check("the daily run limit is honoured, and says why", "today's limit" in sv.budget_left(capped), sv.budget_left(capped))
        check("and stops the next task from starting", sv.run_one(say=quiet, runner=runner, cfg=capped) is None)
        check("today's spend is counted from real runs", sv.spent_today()["runs"] >= 4, sv.spent_today())

        def crash(job, say):
            raise RuntimeError("the run process died")
        c1 = sv.add("this one crashes", proj, "review", max_attempts=1)
        c2 = sv.add("this one is fine", proj, "build")
        sv.run_one(say=quiet, runner=crash, cfg=cfg)
        sv.run_one(say=quiet, runner=crash, cfg=cfg)
        r = sv.run_one(say=quiet, runner=runner, cfg=cfg)
        check("a crashing run costs one task, not the queue", r and r["verdict"] == "pass" and sv.get(c2)["state"] == "done",
              (sv.get(c1)["state"], sv.get(c2)["state"]))
        check("and the crash is recorded as the reason", "died" in (sv.get(c1)["why"] or ""), sv.get(c1)["why"])

        # ---- a machine problem is not a failed approach
        sv.clear(("pending", "failed", "dead", "done", "not_sure", "cancelled"))
        down = sv.add("explain the router", proj, "explore", max_attempts=3)
        sv.add("a second task behind it", proj, "build")
        out = sv.supervise(hours=1, max_tasks=10, say=quiet, cfg=cfg,
                           runner=lambda job, say: {"verdict": "fail", "flow": job["flow"],
                                                    "why": "Ollama is not reachable at http://127.0.0.1:9 (connection refused)"})
        dj = sv.get(down)
        check("when the model server is down, the task keeps its place instead of burning another approach",
              dj["state"] == "pending" and dj["attempts"] == 0 and dj["tried"] == [] and "waiting for the machine" in dj["note"], dj)
        check("and the supervisor stops at once rather than failing every task behind it the same way",
              len(out["ran"]) == 1 and sv.counts()["pending"] == 2 and "not ready" in out["why"], (out["why"], sv.counts()))
        # the same "machine problem" again and again is not taken on faith forever: it must not block the queue for good
        sayings = lambda job, say: {"verdict": "fail", "flow": job["flow"], "why": "Ollama is not reachable (connection refused)"}
        for _ in range(3):
            sv.run_one(say=quiet, runner=sayings, cfg=cfg)
        dj3 = sv.get(down)
        check("after three identical machine-problem pauses the task is treated as a real failure, so it stops blocking the queue",
              dj3["state"] != "pending" or dj3["attempts"] >= 1, dj3)
        check("a real failure of the approach is still treated as one", sv.environmental("assertion failed: expected 2, got 3") is False
              and sv.environmental("model 'qwen3:4b' not found, try pulling it first") is True)
        sv.clear(("pending",))

        # ---- reading the verdict a run really wrote
        rd = os.path.join(proj, ".arch", "runs", "20261103_010101_bugfix")
        os.makedirs(rd)
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "call", "id": 1}) + "\n")
            f.write(json.dumps({"type": "verdict", "verdict": "not_sure", "why": "no test command", "flow": "bugfix"}) + "\n")
        check("the verdict is read from the run's own trace, including 'not sure'",
              sv.verdict_of(rd) == ("not_sure", "no test command", "bugfix"), sv.verdict_of(rd))
        check("a run that left no record says so instead of guessing", sv.verdict_of(None)[0] == "")
        check("the newest run since a task started is the one that is read", sv._newest_run(proj, 0) == rd)

        # ---- a CPU-only task runs alongside a GPU task; never two GPU tasks
        import threading
        import time as _t
        sv.clear(("done", "cancelled", "dead"))
        for s_ in ("pending", "running", "failed", "not_sure"):
            for r_ in sv.listing([s_]):
                sv._finish(r_["id"], "done")
        g1, g2 = sv.add("gpu one", proj, "build", max_attempts=1), sv.add("gpu two", proj, "build", max_attempts=1)
        c1, c2 = sv.add("index it", proj, "index"), sv.add("scan it", proj, "scan")
        check("a task's resource comes from its flow", sv.get(g1)["needs"] == "gpu" and sv.get(c1)["needs"] == "cpu")
        check("claiming by resource never hands out the other kind", sv.claim("cpu")["id"] == c1 and sv.claim("gpu")["id"] == g1)
        sv._finish(c1, "done")
        sv._finish(g1, "done")
        live, peak, kinds_at_once, mu = {"gpu": 0, "cpu": 0}, {"gpu": 0}, [0], threading.Lock()

        def slow(job, say):
            k = job["needs"]
            with mu:
                live[k] += 1
                peak["gpu"] = max(peak["gpu"], live["gpu"])
                if live["gpu"] and live["cpu"]:
                    kinds_at_once[0] += 1
            _t.sleep(0.4)
            with mu:
                live[k] -= 1
            return {"verdict": "pass", "why": "", "run_id": "", "flow": job["flow"], "code": 0}
        t0 = _t.time()
        out = sv.supervise(hours=1, max_tasks=10, say=quiet, runner=slow, cfg=cfg)
        wall = _t.time() - t0
        ran = sorted(d["id"] for d in out["ran"])
        check("both lanes ran every waiting task", ran == sorted([g2, c2]), ran)
        check("the CPU-only task ran while the GPU task did", kinds_at_once[0] >= 1 and wall < 0.75, (kinds_at_once, wall))
        check("never more than one GPU task at a time", peak["gpu"] <= 1, peak)
        check("with only one kind waiting it is the plain sequential queue", not sv._both_kinds_waiting())
    finally:
        try:
            sv.stop(False)
        except OSError:
            pass
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SUPERVISOR CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SUPERVISOR CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(run())
