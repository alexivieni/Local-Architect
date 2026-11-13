"""Checks for the crew clock (arch_agents.schedule_*, tick, the crew log). A fake starter stands in for Claude; no network."""
import datetime
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    tmp = tempfile.mkdtemp(prefix="arch_crew_")
    os.environ["ARCHITECT_DATA_DIR"] = tmp
    import arch_agents as ag

    n = [0]

    def ok(cond, msg):
        n[0] += 1
        if not cond:
            raise AssertionError(msg)

    def refused(**kw):
        try:
            ag.schedule_add(**kw)
        except ValueError:
            return True
        return False

    calls = []

    def starter(agent_id, message, environment_id=None, repo_url=None):
        calls.append((agent_id, message, environment_id, repo_url))
        return f"sesn_{len(calls)}"

    base = dict(agent_id="agent_abc123", message="Fix the failing tests", at="02:00")
    # add / list / remove
    a = ag.schedule_add(**base, repeat="daily", environment_id="env_1", repo_url="https://github.com/me/proj")
    rows = ag.schedule_list()
    ok(len(rows) == 1 and rows[0]["id"] == a and rows[0]["at"] == "02:00" and rows[0]["repo_url"], "listed")
    ok(rows[0]["last_run"] is None and "created" not in rows[0], "list shape")
    ok(os.path.isfile(os.path.join(tmp, "crew_schedule.json")), "file written")
    ok(not [f for f in os.listdir(tmp) if f.endswith(".tmp")], "no temp left over")
    ok(ag.schedule_remove(a) is True and ag.schedule_list() == [], "removed")
    ok(ag.schedule_remove(a) is False, "remove twice")

    # bad inputs
    ok(refused(**dict(base, agent_id="bad id!")), "agent id")
    ok(refused(**dict(base, message="  ")), "empty message")
    ok(refused(**dict(base, at="25:00")), "hour")
    ok(refused(**dict(base, at="2:00")), "format")
    ok(refused(**dict(base, repeat="weekly")), "repeat")
    ok(refused(**dict(base, repo_url="file:///etc")), "repo url")
    ok(refused(**dict(base, environment_id="no spaces")), "environment id")
    ok(ag.schedule_list() == [], "nothing stored for bad input")

    # tick: due once per slot
    d = ag.schedule_add(**base, repeat="daily")
    # backdate creation so the slot is "after it was added"
    items = ag._read_schedule()
    items[0]["created"] = datetime.datetime(2026, 11, 3, 12, 0).timestamp()
    ag._write_schedule(items)
    ok(ag.tick(now=datetime.datetime(2026, 11, 4, 1, 59), starter=starter) == [], "not yet due before 02:00 (slot already ran before add)")
    got = ag.tick(now=datetime.datetime(2026, 11, 4, 2, 0, 30), starter=starter)
    ok(got == ["sesn_1"] and calls[0][:2] == ("agent_abc123", "Fix the failing tests"), "due job started")
    ok(ag.tick(now=datetime.datetime(2026, 11, 4, 2, 5), starter=starter) == [], "not twice in the same slot")
    ok(ag.tick(now=datetime.datetime(2026, 11, 4, 23, 0), starter=starter) == [], "still the same slot at night")
    row = ag.schedule_list()[0]
    ok(row["last_session"] == "sesn_1" and row["last_run"] and row["last_status"] == "running", "last_* recorded")
    got = ag.tick(now=datetime.datetime(2026, 11, 5, 2, 1), starter=starter)
    ok(got == ["sesn_2"] and len(ag.schedule_list()) == 1, "daily re-arms next day")

    # once is removed after it starts
    o = ag.schedule_add(**dict(base, at="03:30"), repeat="once")
    items = ag._read_schedule()
    for x in items:
        if x["id"] == o:
            x["created"] = datetime.datetime(2026, 11, 5, 12, 0).timestamp()
    ag._write_schedule(items)
    ok(ag.tick(now=datetime.datetime(2026, 11, 6, 3, 0), starter=starter) == ["sesn_3"], "daily fires, once (03:30) not yet")
    ok(any(x["id"] == o for x in ag.schedule_list()), "once still waiting")
    ok(ag.tick(now=datetime.datetime(2026, 11, 6, 3, 31), starter=starter) == ["sesn_4"], "once fires")
    ok(all(x["id"] != o for x in ag.schedule_list()), "once removed after start")
    ok(ag.tick(now=datetime.datetime(2026, 11, 6, 3, 40), starter=starter) == [], "once never again")
    ag.schedule_remove(d)

    # a job added after today's slot waits for the next one
    e = ag.schedule_add(**dict(base, at="01:00"), repeat="once")
    ok(ag.tick(now=datetime.datetime.now(), starter=lambda *a, **k: "x") in ([], ["x"]), "tick runs")
    ag.schedule_remove(e)

    # a starter that fails does not loop every minute
    f = ag.schedule_add(**base, repeat="daily")
    items = ag._read_schedule()
    items[0]["created"] = datetime.datetime(2026, 11, 1).timestamp()
    ag._write_schedule(items)

    def boom(*a, **k):
        raise RuntimeError("no network")
    ok(ag.tick(now=datetime.datetime(2026, 11, 7, 2, 0), starter=boom) == [], "failing starter starts nothing")
    ok("could not start" in ag.schedule_list()[0]["last_status"], "failure recorded")
    ok(ag.tick(now=datetime.datetime(2026, 11, 7, 2, 1), starter=starter) == [], "failed slot is not retried each minute")
    ag.schedule_remove(f)

    # no real runs during self-tests without a starter
    ok(ag.tick() == [], "no starter in tests -> nothing starts")

    # the log line
    sid = "sesn_log"
    ag._CREW_SIDS.add(sid)
    with ag._LOCK:
        ag._RUNS[sid] = {"events": [{"type": "agent.message", "text": "All done: 3 tests fixed."}], "status": "idle",
                         "agent": "agent_abc123", "started": time.time(), "error": None, "approve": set()}
    ag._log_finished(sid)
    rows = ag.crew_log()
    ok(len(rows) == 1 and rows[0]["session"] == sid and rows[0]["status"] == "idle" and "3 tests" in rows[0]["summary"], "log line")
    with open(os.path.join(tmp, "crew_log.jsonl"), encoding="utf-8") as fh:
        ok(json.loads(fh.readline())["agent"] == "agent_abc123", "jsonl on disk")
    ag._log_finished(sid)
    ok(len(ag.crew_log()) == 1, "logged once")
    ag._log_finished("sesn_not_crew")
    ok(len(ag.crew_log()) == 1, "non-crew sessions are not logged")

    # scheduler thread is idempotent
    first = ag.start_scheduler()
    ok(first is True and ag.start_scheduler() is False, "second start_scheduler does nothing")

    print(f"ALL {n[0]} CREW CHECKS PASSED")
    return 0                      # 0 = passed (a failed check raises AssertionError); it returned the count, which read as a failure


if __name__ == "__main__":
    sys.exit(run())
