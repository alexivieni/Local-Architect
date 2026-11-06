"""Self-test for pipelining checks beside the model (arch_overlap) and CPU work sent to another machine (arch_hosts.run_cpu).
Fake slow checks and a fake host: no model, no GPU, no network."""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== overlap: checks of attempt N beside the model writing attempt N+1 ==")
    import arch_overlap as ov
    import arch_hosts as H
    import arch_core as core

    # ---- the fix loop shape: 3 attempts, "model" 0.3 s each, "checks" 0.3 s each
    def checks(n):
        time.sleep(0.3)
        return {"attempt": n, "ok": n == 3}

    def loop(o):
        toks = []
        for n in (1, 2, 3):
            time.sleep(0.3)                                    # the model writing attempt n
            toks.append(o.submit_checks(checks, n))            # attempt n's checks start; the model moves on
        return o.join_all(toks)
    o = ov.Overlap(workers=1, max_pending=4, low_priority=False)
    t0 = time.time()
    got = loop(o)
    wall = time.time() - t0
    o.close()
    s = ov.Overlap(workers=0)
    t0 = time.time()
    seq = loop(s)
    wall_seq = time.time() - t0
    check("overlapped wall time is less than model plus checks added up", wall < 1.45 and wall < wall_seq - 0.4, (wall, wall_seq))
    check("verdicts come back in attempt order and are identical to the inline run", got == seq and [g["attempt"] for g in got] == [1, 2, 3], got)
    check("only the last attempt passes, as when run one after the other", [g["ok"] for g in got] == [False, False, True])

    # ---- errors surface at result(), in order; the worker survives
    o = ov.Overlap(workers=1, low_priority=False)

    def boom():
        raise RuntimeError("lint exploded")
    t1, t2 = o.submit_checks(boom), o.submit_checks(lambda: 7)
    try:
        o.result(t1)
        raised = False
    except RuntimeError as e:
        raised = "lint exploded" in str(e)
    check("a check that raises is re-raised to the caller at result()", raised)
    check("the worker keeps going after a failed check", o.result(t2) == 7)
    check("a result can be read again", o.result(t2) == 7)
    try:
        o.result(o.submit_checks(time.sleep, 2), timeout=0.05)
        late = False
    except TimeoutError:
        late = True
    check("result() with a timeout gives up instead of hanging", late)
    o.close(wait=False)

    # ---- bounded queue: submit waits instead of piling up
    o = ov.Overlap(workers=1, max_pending=2, low_priority=False)
    gate, started = threading.Event(), []

    def hold(i):
        started.append(i)
        gate.wait(5)
        return i
    a, b = o.submit_checks(hold, 1), o.submit_checks(hold, 2)
    blocked = threading.Event()
    third = []

    def late_submit():
        blocked.set()
        third.append(o.submit_checks(hold, 3))
    th = threading.Thread(target=late_submit, daemon=True)
    th.start()
    blocked.wait(1)
    time.sleep(0.2)
    check("a third submit waits while two are pending (bounded queue)", not third and o.stats["waited_slot"] >= 1, third)
    gate.set()
    th.join(3)
    check("it goes through when a slot frees, and nothing is lost", o.join_all([a, b] + third) == [1, 2, 3])
    o.close()

    # ---- below-normal priority / efficiency cores are asked for, never fatal
    d = ov._lower_this_thread(mask=0)
    check("priority and affinity are best effort and never raise", isinstance(d, dict) and set(d) == {"priority", "ecores"})

    # ---- CPU work on another machine
    print("\n== hosts: CPU work on a machine you added, local when it is not there ==")
    home = tempfile.mkdtemp(prefix="archov_")
    old = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    try:
        H.forget()
        calls = {"local": 0, "post": 0}

        def local(p):
            calls["local"] += 1
            return {"ok": True, "code": 0, "out": "ran here: " + " ".join(p["argv"])}

        def up(url, timeout=0):
            return {"models": [{"name": "m:7b"}]}

        def down(url, timeout=0):
            raise OSError("connection refused")

        def good(url, body, timeout):
            calls["post"] += 1
            calls["last"] = (url, body)
            return {"ok": True, "code": 0, "out": "ran there"}
        cfg = {"hosts": {"desk": "http://desk:11434"}, "role_hosts": {"cpu": "desk"}}
        pl = {"argv": ["pytest", "-q"], "files": {"a.py": "x=1"}}
        r = H.run_cpu("tests", pl, local, cfg, post=good, get=up)
        check("work goes to the host routed for cpu", r["where"] == "desk" and r["out"] == "ran there" and calls["local"] == 0, r)
        check("what is sent is the kind, the command and the files, to the host's own address",
              calls["last"][0] == "http://desk:11434/arch/cpu" and calls["last"][1]["kind"] == "tests" and calls["last"][1]["files"] == pl["files"])
        H.forget()
        r = H.run_cpu("tests", pl, local, cfg, post=good, get=down)
        check("a host that is down means the work runs here, with the reason", r["where"] == "local" and "down" in r["fallback"] and calls["local"] == 1, r)
        H.forget()

        def refuse(url, body, timeout):
            raise OSError("reset")
        r = H.run_cpu("tests", pl, local, cfg, post=refuse, get=up)
        check("a host that fails mid-work falls back to local", r["where"] == "local" and r["ok"] and calls["local"] == 2, r)
        n_post = calls["post"]
        r = H.run_cpu("tests", pl, local, cfg, post=good, get=up)
        check("a host that just failed is not asked again straight away", r["where"] == "local" and calls["post"] == n_post, r)
        H.forget()
        r = H.run_cpu("index", pl, local, cfg, post=lambda *a: {"nonsense": 1}, get=up)
        check("an answer that is not an Architect worker's falls back to local", r["where"] == "local")
        r = H.run_cpu("tests", pl, local, {"hosts": {}, "role_hosts": {}}, post=good, get=up)
        check("with no cpu host everything runs here and says nothing is wrong", r["where"] == "local" and r["fallback"] is None)
        r = H.run_cpu("tests", {"argv": ["x"], "files": {"big": "z" * 2_100_000}}, local, cfg, post=good, get=up)
        check("something too big to ship runs here", r["where"] == "local" and "too much" in r["fallback"])
        check("an unknown kind of work is refused", _raises(lambda: H.run_cpu("shell", pl, local, cfg, post=good, get=up)))
        H.forget()
        r = H.run_cpu("tests", pl, local, {"hosts": {"desk": "http://desk:11434"}, "role_hosts": {"cpu": "ghost"}}, post=good, get=up)
        check("a role routed to a machine that was never added is local", r["where"] == "local")
    finally:
        if old is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old
        H.forget()
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' OVERLAP CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' OVERLAP CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(run())
