"""The supervisor: a queue of work the machine gets through on its own, and what it does when something does not work.

You add tasks; it runs them one at a time, each in its own process, each inside a hard budget, and records the verdict it actually
reached. Three things make it more than a to-do list:

  * **A failure is not the end of a task.** When a run fails or ends "not sure", the task goes back on the queue with a DIFFERENT flow
    from the alternates table -- a bug the bugfix flow could not reproduce gets a hunt instead, a build that keeps failing gets
    re-scoped as a feature. Each task remembers what has been tried, so it never tries the same thing twice, and it gives up honestly
    when the alternatives run out.
  * **It cannot run away.** A daily ceiling on runs and on minutes, a per-task timeout, and a kill switch (a `STOP` file in the data
    folder, or `supervise --stop`) that is checked before every task. A run is stopped between steps, never mid-edit.
  * **A crash costs one task, not the queue.** Every task runs as a separate process. If it dies, the job is marked failed with the
    reason and the supervisor carries on with the next one.

Storage is `<data_dir>/jobs.db`. Every operation opens and closes its own connection: a long-lived handle on Windows locks the file.

    python architect.py queue add "fix the failing login test" --root C:/work/shop
    python architect.py queue
    python architect.py supervise --hours 2
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import arch_core as core

HERE = os.path.dirname(os.path.abspath(__file__))

# When a flow fails or cannot tell, which flow to try next. Each says why, so the queue can explain its own decisions.
ALTERNATES = {
    "bugfix":   [("hunt", "the bug could not be pinned down directly, so read the code adversarially and turn each suspicion into a test"),
                 ("build", "treat it as missing behaviour and build it with acceptance checks")],
    "build":    [("feature", "re-scope it as one feature across data, API and UI, contract first"),
                 ("design", "step back and design it before building")],
    "feature":  [("build", "build it in smaller sprints")],
    "refactor": [("test", "pin the behaviour with more tests first, then refactor safely")],
    "optimize": [("refactor", "make the code simpler first; speed often follows")],
    "test":     [("build", "the code under test may be missing; build it")],
    "hunt":     [("review", "fall back to a review that reports without changing anything")],
    "secure":   [("review", "report the risks for a person to decide on")],
    "config":   [("review", "report the configuration risks instead of changing them")],
    "cicd":     [("review", "report what the pipeline is missing")],
    "explore":  [("review", "read the code broadly instead of answering one question")],
    "design":   [("explore", "learn the existing code before designing on top of it")],
}
DEFAULT_FALLBACK = [("bugfix", "try it as a bug: reproduce, then fix")]

# A failure that says nothing about the APPROACH, only about the machine. Trying a different flow cannot help -- every flow needs the
# model server -- so the task keeps its place and the supervisor stops and says what to fix, instead of burning every alternative on it.
import re as _re
ENVIRONMENTAL = _re.compile(r"not reachable|connection refused|no connection could be made|actively refused|"
                            r"model .{0,60}not found|pull the model|out of memory|cuda error|cudaMalloc|"
                            r"no space left|disk is full|permission denied: .{0,80}\.arch", _re.I)


def environmental(why):
    """True when a failure is about the machine (server down, model missing, out of memory), not about how the task was tackled."""
    return bool(ENVIRONMENTAL.search(why or ""))

# What a task keeps busy. Every model flow needs the GPU; a few jobs are CPU only (indexing, scanning, running a project's tests) and can
# run beside a GPU task. Still one GPU task at a time (the heavy lock).
CPU_FLOWS = ("index", "scan", "lint", "tests-only")


def infer_needs(flow):
    return "cpu" if (flow or "").strip().lower() in CPU_FLOWS else "gpu"


STATES = ("pending", "running", "done", "failed", "not_sure", "dead", "cancelled")

SCHEMA = """
create table if not exists jobs (
  id          integer primary key autoincrement,
  goal        text not null,
  root        text not null,
  flow        text default '',
  state       text default 'pending',
  attempts    integer default 0,
  max_attempts integer default 3,
  tried       text default '[]',
  verdict     text default '',
  why         text default '',
  run_id      text default '',
  minutes     real default 10,
  priority    integer default 0,
  parent      integer,
  note        text default '',
  created     real,
  started     real,
  heartbeat   real,
  finished    real,
  secs        real default 0
);
create index if not exists jobs_state on jobs(state, priority, id);
create table if not exists spend (day text primary key, runs integer default 0, secs real default 0);
"""


def db_path():
    return os.path.join(core.data_dir(), "jobs.db")


def _open():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=5.0)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("pragma table_info(jobs)")}
    if "needs" not in cols:                                                            # a queue made before resources existed
        try:
            c.execute("alter table jobs add column needs text default 'gpu'")
            c.commit()
        except sqlite3.OperationalError:
            pass                                                                       # another process added it first
    for col, typ in (("heartbeat", "real"), ("owner_pid", "integer"), ("owner_start", "real")):
        if col not in cols:
            try:
                c.execute(f"alter table jobs add column {col} {typ}")
                c.commit()
            except sqlite3.OperationalError:
                pass
    return c


def _row(r):
    if r is None:
        return None
    d = dict(r)
    try:
        d["tried"] = json.loads(d.get("tried") or "[]")
    except ValueError:
        d["tried"] = []
    return d


def stop_path():
    return os.path.join(core.data_dir(), "STOP")


def stopped():
    return os.path.isfile(stop_path())


# ---------------------------------------------------------------- the queue

def add(goal, root, flow="", minutes=10, max_attempts=3, priority=0, parent=None, tried=None, note="", needs=""):
    """Put a task on the queue. -> its id. needs = 'gpu' or 'cpu' (default: from the flow)."""
    needs = needs if needs in ("gpu", "cpu") else infer_needs(flow)
    goal = " ".join(str(goal or "").split())
    if not goal:
        raise ValueError("say what the task is")
    root = os.path.abspath(root or os.getcwd())
    c = _open()
    try:
        cur = c.execute("insert into jobs(goal, root, flow, minutes, max_attempts, priority, parent, tried, note, created, needs) "
                        "values(?,?,?,?,?,?,?,?,?,?,?)",
                        (goal[:2000], root, (flow or "").strip().lower(), float(minutes or 10), int(max_attempts or 3),
                         int(priority or 0), parent, json.dumps(list(tried or [])), note[:1600], time.time(), needs))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def get(job_id):
    c = _open()
    try:
        return _row(c.execute("select * from jobs where id = ?", (int(job_id),)).fetchone())
    finally:
        c.close()


def listing(states=None, limit=200):
    c = _open()
    try:
        if states:
            q = "select * from jobs where state in (%s) order by id desc limit ?" % ",".join("?" * len(states))
            rows = c.execute(q, (*states, int(limit))).fetchall()
        else:
            rows = c.execute("select * from jobs order by id desc limit ?", (int(limit),)).fetchall()
        return [_row(r) for r in rows]
    finally:
        c.close()


def counts():
    c = _open()
    try:
        out = {s: 0 for s in STATES}
        for r in c.execute("select state, count(*) n from jobs group by state"):
            out[r["state"]] = r["n"]
        return out
    finally:
        c.close()


def cancel(job_id):
    c = _open()
    try:
        n = c.execute("update jobs set state='cancelled', finished=? where id=? and state='pending'", (time.time(), int(job_id))).rowcount
        c.commit()
        return n > 0
    finally:
        c.close()


def clear(states=("done", "cancelled", "dead")):
    c = _open()
    try:
        n = c.execute("delete from jobs where state in (%s)" % ",".join("?" * len(states)), tuple(states)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def claim(needs=None):
    """Take the next pending task (highest priority, then oldest), optionally only one that needs 'gpu' or 'cpu'. Marked running in the
    same transaction, so two supervisors (or two lanes) can never take the same task."""
    c = _open()
    try:
        c.execute("begin immediate")
        r = c.execute("select * from jobs where state='pending'" + (" and coalesce(needs,'gpu')=?" if needs else "")
                      + " order by priority desc, id asc limit 1", (needs,) if needs else ()).fetchone()
        if not r:
            c.execute("commit")
            return None
        import arch_proc
        me = arch_proc.identity(os.getpid())
        c.execute("update jobs set state='running', attempts=attempts+1, started=?, heartbeat=?, owner_pid=?, owner_start=? where id=?",
                  (time.time(), time.time(), me["pid"], me["start"], r["id"]))
        c.execute("commit")
        d = _row(r)
        d["attempts"] += 1
        d["state"] = "running"
        return d
    finally:
        c.close()


def beat(job_id):
    """Record a liveness heartbeat on a running task so another supervisor's recover() leaves it alone. Never raises: a locked queue
    (another supervisor claiming, the hub reading) skips one beat instead of ending the heartbeat thread."""
    try:
        c = _open()
    except sqlite3.Error:
        return False
    try:
        c.execute("update jobs set heartbeat=? where id=? and state='running'", (time.time(), int(job_id)))
        c.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        c.close()


def recover(stale_after=15.0):
    """Tasks left 'running' by a supervisor that was killed (no heartbeat within `stale_after` seconds) go back to pending. -> how many."""
    c = _open()
    try:
        import arch_proc
        cutoff = time.time() - float(stale_after or 0)
        rows = c.execute("select id, owner_pid, owner_start, heartbeat from jobs where state='running'").fetchall()
        n = 0
        for r in rows:
            if r["owner_pid"]:
                if arch_proc.same(r["owner_pid"], r["owner_start"]) is not False:
                    continue                 # its supervisor is alive (only slow, or its beat was locked out): never take a live task
            elif r["heartbeat"] is not None and r["heartbeat"] >= cutoff:
                continue                     # an older row with no owner recorded: the heartbeat rule as before
            n += c.execute("update jobs set state='pending', heartbeat=null, owner_pid=null, owner_start=null, "
                           "note='recovered after the supervisor stopped mid-task' || "
                           "case when instr(note, 'POSTMORTEM: ') > 0 then char(10) || substr(note, instr(note, 'POSTMORTEM: ')) else '' end "
                           "where id=? and state='running'", (r["id"],)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def _finish(job_id, state, verdict="", why="", run_id="", secs=0.0):
    c = _open()
    try:
        c.execute("update jobs set state=?, heartbeat=null, verdict=?, why=?, run_id=?, finished=?, secs=? where id=?",
                  (state, verdict, (why or "")[:1000], run_id or "", time.time(), float(secs or 0), int(job_id)))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- daily budget

def _today():
    return time.strftime("%Y-%m-%d")


def spent_today():
    c = _open()
    try:
        r = c.execute("select runs, secs from spend where day=?", (_today(),)).fetchone()
        return {"runs": int(r["runs"]) if r else 0, "minutes": round((r["secs"] if r else 0) / 60.0, 1)}
    finally:
        c.close()


def _spend(secs):
    c = _open()
    try:
        c.execute("insert into spend(day, runs, secs) values(?, 1, ?) on conflict(day) do update set runs=runs+1, secs=secs+?",
                  (_today(), float(secs), float(secs)))
        c.commit()
    finally:
        c.close()


def budget_left(cfg=None):
    """-> '' when there is room for another task today, else the reason there is not."""
    sv = ((cfg or core.load_config()).get("supervisor") or {})
    s = spent_today()
    if sv.get("daily_runs") and s["runs"] >= int(sv["daily_runs"]):
        return f"today's limit of {sv['daily_runs']} runs is used up (Settings > Supervisor)"
    if sv.get("daily_minutes") and s["minutes"] >= float(sv["daily_minutes"]):
        return f"today's limit of {sv['daily_minutes']} minutes is used up (Settings > Supervisor)"
    return ""


# ---------------------------------------------------------------- running one task

def next_flow(origin, tried, current=None):
    """The next flow to try, skipping anything already tried. -> (flow, why) or (None, '').

    Anchored to the task's ORIGINAL flow: a bug fix that failed tries a hunt, then a build -- the alternatives for fixing a bug. It does
    not chain through each alternative's own alternatives, because that drifts away from what was asked: bugfix -> hunt -> review ends
    at a flow that only writes a report, and a person who asked for a fix gets a document instead.
    """
    order = list(ALTERNATES.get(origin or "", []))       # the origin's list only: `current` no longer adds its own alternatives
    if not order:
        order = list(DEFAULT_FALLBACK)
    for alt, why in order:
        if alt not in tried:
            return alt, why
    return None, ""


def _newest_run(root, since):
    d = os.path.join(root, ".arch", "runs")
    try:
        runs = [r for r in os.listdir(d) if os.path.isdir(os.path.join(d, r))]
    except OSError:
        return None
    runs = [r for r in runs if os.path.getmtime(os.path.join(d, r)) >= since - 2]
    return os.path.join(d, sorted(runs)[-1]) if runs else None


def verdict_of(run_dir):
    """The verdict a run actually recorded, read from its own trace. -> (verdict, why, flow)."""
    if not run_dir:
        return ("", "the run left no record", "")
    v, why, flow = "", "", ""
    try:
        with open(os.path.join(run_dir, "trace.jsonl"), encoding="utf-8") as f:
            for line in f:
                if '"type": "verdict"' in line:
                    try:
                        j = json.loads(line)
                        v, why, flow = j.get("verdict") or "", j.get("why") or "", j.get("flow") or flow
                    except ValueError:
                        pass
    except OSError:
        pass
    if not flow:
        flow = (core.load_json(os.path.join(run_dir, "run.json")) or {}).get("flow", "")
    return (v, why, flow)


def _subprocess_runner(job, say):
    """Run one task as its own process, so a crash costs one task, not the queue. -> {"verdict", "why", "run_id", "flow", "code"}."""
    argv = [sys.executable, "-u", os.path.join(HERE, "architect.py"), "run", job["goal"], "--root", job["root"], "--review", "auto"]
    if job.get("flow"):
        argv += ["--flow", job["flow"]]
    t0 = time.time()
    limit = max(60, int(float(job.get("minutes") or 10) * 60))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    hint = (job.get("note") or "").partition("POSTMORTEM: ")[2].strip()
    if hint:                                   # a retry starts from what the failed attempt learned, not from nothing (Engine.context shows it)
        env["ARCHITECT_RETRY_HINT"] = hint[:900]
    else:
        env.pop("ARCHITECT_RETRY_HINT", None)
    try:
        p = subprocess.run(argv, cwd=job["root"] if os.path.isdir(job["root"]) else HERE, capture_output=True, text=True,
                           timeout=limit, env=env, encoding="utf-8", errors="replace")
        code, tail = p.returncode, (p.stdout + p.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        code, tail = -9, f"stopped: it took longer than its {limit // 60}-minute limit"
    except OSError as ex:
        return {"verdict": "fail", "why": f"could not start: {ex}", "run_id": "", "flow": job.get("flow") or "", "code": -1}
    run_dir = _newest_run(job["root"], t0)
    v, why, flow = verdict_of(run_dir)
    if not v:
        v = "pass" if code == 0 else "fail"
        why = why or (tail.strip().splitlines()[-1][:300] if tail.strip() else f"exit code {code}")
    return {"verdict": v, "why": why, "run_id": os.path.basename(run_dir) if run_dir else "", "flow": flow or job.get("flow") or "",
            "code": code}


def run_one(say=print, runner=None, cfg=None, needs=None, lane=False):
    """Take one task off the queue and see it through. -> a summary dict, or None when there is nothing to do or no room to do it.
    needs: only a 'gpu' or 'cpu' task. lane=True (parallel mode): a GPU task holds the heavy lock while it runs, so only one runs at a time
    even across supervisors; a CPU task does not wait for it."""
    cfg = cfg or core.load_config()
    if stopped():
        say("[supervisor] the STOP file is present, so nothing new is started. Remove it (or run `supervise --resume`) to continue.")
        return None
    room = budget_left(cfg)
    if room:
        say(f"[supervisor] not starting anything: {room}.")
        return None
    job = claim(needs)
    if not job:
        return None
    say(f"\n[supervisor] task {job['id']}: {job['goal'][:90]}" + (f"  (flow {job['flow']})" if job["flow"] else "  (the router picks the flow)"))
    t0 = time.time()
    beat(job["id"])
    hb_stop = threading.Event()

    def _hb_loop():
        while not hb_stop.wait(2.0):
            try:
                beat(job["id"])
            except Exception:
                continue                     # a missed beat is fine; a dead heartbeat thread lets recover() steal a live task

    hb_th = threading.Thread(target=_hb_loop, name=f"sv-hb-{job['id']}", daemon=True)
    hb_th.start()
    try:
        if lane and (job.get("needs") or "gpu") == "gpu":
            import arch_heavy
            with arch_heavy.one_at_a_time("supervisor gpu task %s" % job["id"], cfg, say=say):
                res = (runner or _subprocess_runner)(job, say)
        else:
            res = (runner or _subprocess_runner)(job, say)
    except (Exception, SystemExit) as ex:                     # one broken task must never stop the queue (SystemExit: the heavy-job lock gave up waiting)
        res = {"verdict": "fail", "why": f"{type(ex).__name__}: {ex}"[:300], "run_id": "", "flow": job.get("flow") or "", "code": -1}
    finally:
        hb_stop.set()
    secs = round(time.time() - t0, 1)
    v = res.get("verdict") or "fail"
    _em = _re.match(r"\[env x(\d+)\] ", job.get("note") or "")
    _env_n = int(_em.group(1)) if _em else 0
    if not (v != "pass" and environmental(res.get("why", "")) and _env_n < 3):
        _spend(secs)                     # a task that never really started (the machine was not ready) costs nothing of the daily budget
    # The same "machine problem" three times in a row is no longer taken on faith: the words may come from the project being worked
    # on (a test that prints "connection refused"), and an endless pause at the head of the queue blocks every other task.
    if v != "pass" and environmental(res.get("why", "")) and _env_n < 3:
        # The machine is not ready; the approach was never really tried. Keep the task exactly as it was and stop.
        c = _open()
        try:
            old = job.get("note") or ""
            keep = old[old.find("POSTMORTEM: "):] if "POSTMORTEM: " in old else ""      # an earlier attempt's explanation stays with the task
            c.execute("update jobs set state='pending', attempts=max(0, attempts-1), note=? where id=?",
                      ((f"[env x{_env_n + 1}] waiting for the machine: " + (res.get("why") or ""))[:400] + ("\n" + keep if keep else ""), job["id"]))
            c.commit()
        finally:
            c.close()
        say(f"[supervisor] task {job['id']} could not really start: {res.get('why', '')[:160]}")
        say("[supervisor] that is a problem with this machine, not with the task, so trying another flow would fail the same way."
            " The task keeps its place. Fix that, then run `supervise` again.")
        return {"id": job["id"], "verdict": "waiting", "why": res.get("why", ""), "flow": res.get("flow") or job.get("flow") or "",
                "secs": secs, "retry": None, "environmental": True}
    flow_used = res.get("flow") or job.get("flow") or ""
    tried = list(dict.fromkeys((job.get("tried") or []) + ([flow_used] if flow_used else [])))
    state = {"pass": "done", "fail": "failed", "not_sure": "not_sure"}.get(v, "failed")
    _finish(job["id"], state, v, res.get("why", ""), res.get("run_id", ""), secs)
    out = {"id": job["id"], "verdict": v, "why": res.get("why", ""), "flow": flow_used, "secs": secs, "retry": None}
    say(f"[supervisor] task {job['id']}: {v}" + (f" -- {res.get('why')}" if res.get("why") else "") + f"  ({secs}s)")
    if v != "pass":
        # A different approach, not the same one again: that is what the alternates table is for.
        alt, why = next_flow(tried[0] if tried else flow_used, tried, current=flow_used)
        if alt and len(tried) < int(job.get("max_attempts") or 3):
            pm = ""
            if res.get("run_id"):
                import arch_postmortem
                pm = arch_postmortem.read(os.path.join(job["root"], ".arch", "runs", res["run_id"]))
            if pm:
                out["postmortem"] = pm
            nid = add(job["goal"], job["root"], alt, job.get("minutes"), job.get("max_attempts"), job.get("priority"),
                      parent=job["id"], tried=tried, note=f"after {flow_used or 'the first try'} ended '{v}': {why}"[:600]
                      + (f"\nPOSTMORTEM: {pm}" if pm else ""))
            out["retry"] = {"id": nid, "flow": alt, "why": why}
            say(f"[supervisor] trying again as task {nid} with the {alt} flow: {why}")
        else:
            _finish(job["id"], "dead" if v == "fail" else "not_sure", v,
                    (res.get("why", "") + " | no other approach left to try: " + ", ".join(tried))[:1000], res.get("run_id", ""), secs)
            say(f"[supervisor] task {job['id']}: every approach has been tried ({', '.join(tried) or 'none'}); leaving it for you")
    return out


def _both_kinds_waiting():
    c = _open()
    try:
        kinds = {r[0] or "gpu" for r in c.execute("select distinct needs from jobs where state='pending'")}
        return {"gpu", "cpu"} <= kinds
    finally:
        c.close()


def supervise(hours=1.0, max_tasks=20, say=print, runner=None, cfg=None):
    """Work through the queue until it is empty, the time is up, the daily budget runs out, or someone presses stop."""
    cfg = cfg or core.load_config()
    back = recover()
    if back:
        say(f"[supervisor] {back} task(s) were left half-done by an earlier session; they are back on the queue")
    t_end = time.time() + float(hours) * 3600
    done = []
    say(f"[supervisor] {counts()['pending']} task(s) waiting; working for up to {hours} h or {max_tasks} task(s)")
    sv_cfg = cfg.get("supervisor") or {}
    parallel = bool(sv_cfg.get("parallel_cpu", True)) and _both_kinds_waiting()
    lock = threading.Lock()
    halt = threading.Event()

    def lane_loop(needs, lane):
        while not halt.is_set() and time.time() < t_end:
            with lock:
                if len(done) + inflight[0] >= max_tasks:
                    return
                inflight[0] += 1
            try:
                r = run_one(say=say, runner=runner, cfg=cfg, needs=needs, lane=lane)
            finally:
                with lock:
                    inflight[0] -= 1
            if r is None:
                return
            with lock:
                done.append(r)
            if r.get("environmental"):
                halt.set()
                return
            e = eta()
            if e.get("left"):
                say(f"[supervisor] queue: {e['line']}  ({e['source']})")

    inflight = [0]
    if parallel:
        say("[supervisor] GPU and CPU-only tasks are both waiting: the CPU-only ones run alongside (one GPU task at a time)")
        cpu = threading.Thread(target=lane_loop, args=("cpu", True), name="supervisor-cpu", daemon=True)
        cpu.start()
        lane_loop("gpu", True)
        cpu.join()
    else:
        lane_loop(None, False)
    c = counts()
    env_stop = next((d for d in done if d.get("environmental")), None)
    why = ("stopped by the STOP file" if stopped() else
           ("paused: the machine is not ready (" + env_stop["why"][:120] + ")") if env_stop else budget_left(cfg) or
           ("time is up" if time.time() >= t_end else "task limit reached" if len(done) >= max_tasks else "the queue is empty"))
    say(f"[supervisor] finished: {sum(1 for d in done if d['verdict'] == 'pass')} passed, "
        f"{sum(1 for d in done if d['verdict'] != 'pass')} did not, {c['pending']} still waiting -- {why}")
    if done:
        try:
            import arch_notify
            arch_notify.notify("Architect: queue finished", f"{sum(1 for d in done if d['verdict'] == 'pass')} of {len(done)} passed; "
                               f"{c['pending']} still waiting ({why})", cfg)
        except Exception:
            pass
    return {"ran": done, "counts": c, "why": why, "spent_today": spent_today()}


def eta(jobs=None):
    """How long the queue has left, from measured seconds per task (arch_eta). -> {line, done, total, left, secs_lo/mid/hi, ends_hm, source}."""
    try:
        import arch_eta
        return arch_eta.queue_eta(jobs)
    except Exception as ex:
        return {"line": "", "left": 0, "source": f"no estimate: {type(ex).__name__}"}


def stop(on=True):
    """The kill switch: present = nothing new starts and any running flow stops between steps."""
    if on:
        os.makedirs(os.path.dirname(stop_path()), exist_ok=True)
        with open(stop_path(), "w", encoding="utf-8") as f:
            f.write("stopped by the supervisor kill switch\n")
    elif os.path.isfile(stop_path()):
        os.remove(stop_path())
    return stopped()
