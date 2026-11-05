"""Self-test for the workload broker (arch_broker.route): fake machine states (tiers, load, speeds, money caps) drive every branch --
local first, quality floors, deadlines, Claude only inside every cap, CPU work to the efficiency cores, waits for low RAM and for a
benchmark holding the card -- plus explain() in plain words, the decision log, and gather() over stand-in readers.
No GPU, no server, no network: Claude is never called (arch_cloud.cost is arithmetic only)."""
import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE = {
    "tiers": {
        "router": {"up": True, "busy": False, "on_cpu": True, "tok_s": 60.0, "size_gb": 1.1},
        "worker": {"up": True, "busy": False, "on_cpu": False, "tok_s": 40.0, "prefill_tok_s": 1000.0, "size_gb": 2.5},
        "heavy": {"up": True, "busy": False, "on_cpu": False, "tok_s": 5.0, "size_gb": 9.0},
    },
    "gpu_pct": 20, "ram_free_gb": 12.0, "ecores": 4, "heavy_job": None, "ours_busy": False,
    "bandwidth": {"gpu_bw": 158.2, "ram_bw": 19.5},
    "cloud": {"ok": True, "why": "", "model": "claude:claude-haiku-5-5", "run_cap": 0.5, "spent_run": 0.0, "day_cap": 5.0, "spent_today": 0.0},
}


def S(**over):
    s = copy.deepcopy(BASE)
    for k, v in over.items():
        if k in ("router", "worker", "heavy"):
            if v is None:
                s["tiers"].pop(k, None)
            else:
                s["tiers"][k].update(v)
        elif k == "cloud":
            s["cloud"].update(v)
        else:
            s[k] = v
    return s


def run():
    home = tempfile.mkdtemp(prefix="arch_broker_")
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "hub_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    results = []

    def check(name, ok):
        results.append(bool(ok))
        print(("  ok   " if ok else "  FAIL ") + name)

    import arch_broker as B
    R = lambda job, st: B.route(job, state=st, log=False)
    try:
        # ---- local first
        d = R({"kind": "generate", "size_tokens": 400}, S())
        check("generate, normal quality, idle worker -> gpu_worker, free, measured speed, eta = 400/40 = 10 s",
              d["target"] == "gpu_worker" and d["cost_usd"] == 0 and d["evidence"] == "measured" and d["eta_s"] == 10.0)
        d = R({"kind": "generate", "size_tokens": 400, "prompt_tokens": 2000}, S())
        check("prompt tokens add prefill time from the measured prefill speed (2000/1000 = 2 s)", d["eta_s"] == 12.0)
        d = R({"kind": "summarise", "size_tokens": 200, "needs_quality": "low"}, S())
        check("summarise, low quality -> cpu_router (the router is fastest and good enough)", d["target"] == "cpu_router")
        d = R({"kind": "generate", "size_tokens": 200, "needs_quality": "low"}, S())
        check("generate never goes to the router even when low quality is asked (kind floor)", d["target"] == "gpu_worker")
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high"}, S())
        check("plan, high quality, heavy up -> heavy (only the heavy tier is trusted with it)", d["target"] == "heavy" and d["eta_s"] == 60.0)
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": True}, S())
        check("needs_quality True means high", d["target"] == "heavy")
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high"}, S(heavy={"up": False}))
        check("heavy stopped: it still wins, with the start time added (60 + 8 s)", d["target"] == "heavy" and d["eta_s"] == 68.0
              and "start" in " ".join(c["note"] for c in d["considered"]))
        d = R({"kind": "judge", "size_tokens": 100}, S(worker={"busy": True}, ours_busy=True, heavy=None))
        check("worker busy -> its wait is counted; judge at normal quality still on the worker but later (2.5 + 30 s)",
              d["target"] == "gpu_worker" and d["eta_s"] == 32.5)
        d = R({"kind": "judge", "size_tokens": 100}, S(worker={"busy": True}, ours_busy=True))
        check("worker busy and an idle heavy that answers sooner (100/5 = 20 s) -> heavy", d["target"] == "heavy" and d["eta_s"] == 20.0)
        d = R({"kind": "generate", "size_tokens": 400}, S(gpu_pct=95))
        check("graphics card 95% busy with someone else's work -> eta doubled (20 s) and said so",
              d["eta_s"] == 20.0 and "other work" in d["why"])

        # ---- speeds when nothing was measured
        d = R({"kind": "generate", "size_tokens": 632}, S(worker={"tok_s": None}))
        check("no measured speed -> bandwidth / size (158.2 / 2.5 = 63.3 tok/s), labelled estimate",
              d["evidence"] == "estimate" and abs(d["eta_s"] - 632 / 63.3) < 0.15)
        check("tok_s_for falls back to the fixed table with no bandwidth either",
              B.tok_s_for("worker", {"size_gb": 2.5}, {}) == (B.FALLBACK_TOK_S["worker"], "estimate"))
        check("a model on the CPU is estimated from RAM bandwidth", B.tok_s_for("router", {"on_cpu": True, "size_gb": 1.0}, BASE["bandwidth"])[0] == 19.5)

        # ---- deadlines and Claude
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 0.05}, S())
        check("heavy too slow for a 20 s deadline, Claude allowed and inside every cap -> claude, with a cost",
              d["target"] == "claude" and 0 < d["cost_usd"] <= 0.05 and d["eta_s"] <= 20)
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20}, S())
        check("same job with max_cost_usd 0 -> stays local, runs late, says Claude was not an option",
              d["target"] == "heavy" and d["cost_usd"] == 0 and "no spending" in d["why"])
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 0.05},
              S(cloud={"model": "claude:claude-opus-5-5"}, heavy={"tok_s": 5.0}))
        check("an Opus reply of 300 tokens fits $0.05? cost computed by arch_cloud.cost (300*20/1e6 = $0.006)", d["cost_usd"] == 0.006)
        d = R({"kind": "plan", "size_tokens": 300000, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 0.5},
              S(cloud={"model": "claude:claude-opus-5-5"}))
        check("over the job's own limit -> local, reason names the job's limit", d["target"] == "heavy" and "this job's limit" in d["why"])
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 1.0},
              S(cloud={"spent_run": 0.5}))
        check("per-run cap used up -> local, reason names the run's cap", d["target"] == "heavy" and "run's cap" in d["why"])
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 1.0},
              S(cloud={"spent_today": 5.0}))
        check("daily cap used up -> local, reason names today's cap", d["target"] == "heavy" and "today's cap" in d["why"])
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 1.0},
              S(cloud={"ok": False, "why": "Claude is off (Settings > Online > Use Claude)"}))
        check("Claude not allowed -> local, and the reason is arch_cloud's own words", d["target"] == "heavy" and "Use Claude" in d["why"])
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "max_cost_usd": 0.05}, S(heavy=None))
        check("high quality but no heavy tier at all, Claude inside caps -> claude", d["target"] == "claude")
        d = R({"kind": "plan", "size_tokens": 300, "needs_quality": "high"}, S(heavy=None))
        check("no heavy tier, Claude not affordable -> the worker, said to be below the quality asked",
              d["target"] == "gpu_worker" and "below the quality" in d["why"])
        d = R({"kind": "generate", "size_tokens": 400, "deadline_s": 60, "max_cost_usd": 1.0}, S())
        check("local meets the deadline -> Claude is not even considered (local first)",
              d["target"] == "gpu_worker" and not any(c["target"] == "claude" for c in d["considered"]))
        d = R({"kind": "generate", "size_tokens": 400, "max_cost_usd": 1.0},
              S(router={"up": False}, worker={"up": False}, heavy={"up": False}, ram_free_gb=1.0, cloud={"ok": False, "why": "off"}))
        check("nothing running, too little RAM to start anything, no Claude -> wait", d["target"] == "wait" and d["eta_s"] > 0)
        d = R({"kind": "generate", "size_tokens": 400, "max_cost_usd": 1.0},
              S(router={"up": False}, worker={"up": False}, heavy={"up": False}, ram_free_gb=1.0))
        check("nothing local can start but Claude is allowed and affordable -> claude", d["target"] == "claude")

        # ---- a benchmark holding the card
        d = R({"kind": "generate", "size_tokens": 400}, S(heavy_job="eval", gpu_pct=98))
        check("eval holds the heavy lock and the card is 98% busy, no deadline -> wait (do not skew the measurement)",
              d["target"] == "wait" and "eval" in d["why"])
        d = R({"kind": "generate", "size_tokens": 400, "deadline_s": 600}, S(heavy_job="eval", gpu_pct=98))
        check("same, but the job has a deadline -> it runs (slower) instead of waiting", d["target"] == "gpu_worker")

        # ---- CPU work
        d = R({"kind": "test"}, S())
        check("test -> ecores, free, never a model", d["target"] == "ecores" and d["cost_usd"] == 0)
        d = R({"kind": "index", "max_cost_usd": 5, "needs_quality": "high", "deadline_s": 1}, S())
        check("index never goes to Claude whatever the money/deadline say", d["target"] == "ecores")
        d = R({"kind": "export"}, S(ecores=0))
        check("no efficiency cores -> the CPU lane (cpu_router)", d["target"] == "cpu_router")
        d = R({"kind": "ingest"}, S(ecores=0, heavy_job="selftest"))
        check("no efficiency cores and a heavy job holds the processor -> wait", d["target"] == "wait" and "selftest" in d["why"])
        d = R({"kind": "test"}, S(ram_free_gb=0.8))
        check("test with 0.8 GB RAM free -> wait", d["target"] == "wait" and "RAM" in d["why"])

        # ---- input handling
        try:
            R({"kind": "dance"}, S())
            check("an unknown kind is refused (ValueError)", False)
        except ValueError:
            check("an unknown kind is refused (ValueError)", True)
        check("'summarize' is accepted as summarise; junk quality becomes normal; bad sizes become defaults",
              B.normalise({"kind": "summarize", "needs_quality": "ultra", "size_tokens": "x"}) ==
              {"kind": "summarise", "size_tokens": 500.0, "prompt_tokens": 0.0, "deadline_s": 0.0, "max_cost_usd": 0.0, "needs_quality": "normal"})
        d = R({"kind": "generate"}, {})
        check("an empty state (nothing readable) never raises: no model to use and no Claude -> wait", d["target"] == "wait")
        check("every target the broker gave is one of TARGETS", all(t in B.TARGETS for t in ("gpu_worker", "heavy", "cpu_router", "ecores", "claude", "wait")))

        # ---- explain
        e = B.explain(R({"kind": "generate", "size_tokens": 400}, S()))
        check("explain: names the place, the reason, the time, 'measured', and that it costs nothing",
              "worker model on the graphics card" in e and "Why:" in e and "10 s" in e and "measured" in e and "nothing" in e)
        e = B.explain(R({"kind": "plan", "size_tokens": 300, "needs_quality": "high", "deadline_s": 20, "max_cost_usd": 0.05}, S()))
        check("explain for Claude shows the dollar estimate", "Claude" in e and "$" in e and "estimate" in e)
        e = B.explain(R({"kind": "test"}, S(ram_free_gb=0.5)))
        check("explain for a wait says when to look again", "waits" in e and "Look again" in e)

        # ---- the decision log
        B.route({"kind": "generate", "size_tokens": 400}, state=S())
        B.route({"kind": "test"}, state=S())
        rows = B.recent(10)
        check("route() appends to <data>/broker.jsonl: job, target, eta, cost, evidence, why",
              os.path.isfile(os.path.join(home, "hub_data", "broker.jsonl")) and len(rows) == 2
              and rows[0]["target"] == "gpu_worker" and rows[1]["target"] == "ecores" and rows[0]["job"]["kind"] == "generate"
              and all(k in rows[0] for k in ("eta_s", "cost_usd", "evidence", "why", "ts")))
        check("log=False writes nothing", (R({"kind": "test"}, S()) and len(B.recent(10)) == 2))
        check("tally counts decisions per target", B.tally() == {"gpu_worker": 1, "ecores": 1})
        with open(B.log_path(), "a", encoding="utf-8") as f:
            f.write("not json\n")
        check("a broken line in the log is skipped, not fatal", len(B.recent(10)) == 2)
        mb, keep = B.LOG_MAX_BYTES, B.LOG_KEEP_LINES
        B.LOG_MAX_BYTES, B.LOG_KEEP_LINES = 400, 3
        try:
            for _ in range(6):
                B.route({"kind": "export"}, state=S())
        finally:
            B.LOG_MAX_BYTES, B.LOG_KEEP_LINES = mb, keep
        with open(B.log_path(), encoding="utf-8") as f:
            n_lines = len(f.readlines())
        check("the log rolls to the last lines once it passes its size", n_lines <= 4)

        # ---- gather over stand-in readers
        import arch_cloud
        import arch_heavy
        import arch_hw
        import arch_passport
        import arch_server
        saved = (arch_hw.live, arch_hw.ecore_mask, arch_heavy.holder, arch_server.status, arch_server.busy, arch_server._alive, arch_passport.load)
        os.makedirs(os.path.join(home, "hub_data"), exist_ok=True)
        with open(os.path.join(home, "hub_data", "bandwidth.json"), "w", encoding="utf-8") as f:
            json.dump({"gpu_bw": 150.0, "ram_bw": 20.0}, f)
        try:
            arch_hw.live = lambda *a, **k: {"gpu_pct": 33, "ram_free_gb": 7.5}
            arch_hw.ecore_mask = lambda: 0xF00
            arch_heavy.holder = lambda: {"job": "eval", "pid": 4242}
            arch_server._alive = lambda pid: True
            arch_server.status = lambda cfg=None: {"processes": [
                {"tier": "worker", "name": "fw:4b", "up": True, "url": "http://127.0.0.1:1", "ngl": 99},
                {"tier": "router", "name": "fr:1b", "up": True, "url": "http://127.0.0.1:2", "ngl": 0},
                {"tier": "heavy", "name": "fbig:14b", "up": False, "url": None, "ngl": 20}]}
            arch_server.busy = lambda url, timeout=2.0: {"http://127.0.0.1:1": True, "http://127.0.0.1:2": None}.get(url)
            arch_passport.load = lambda name: {"fw:4b": {"size_gb": 2.5, "measured": {"gen_tok_s": 41.0}}}.get(name)
            g = B.gather({"online": {"enabled": False}})
        finally:
            (arch_hw.live, arch_hw.ecore_mask, arch_heavy.holder, arch_server.status, arch_server.busy, arch_server._alive, arch_passport.load) = saved
        check("gather: GPU %, free RAM, 4 efficiency cores, the heavy job, bandwidth.json",
              g["gpu_pct"] == 33 and g["ram_free_gb"] == 7.5 and g["ecores"] == 4 and g["heavy_job"] == "eval" and g["bandwidth"]["gpu_bw"] == 150.0)
        check("gather: worker up+busy with its passport speed; router on the CPU, unreadable /slots = not busy; heavy down",
              g["tiers"]["worker"] == dict(g["tiers"]["worker"], up=True, busy=True, tok_s=41.0, size_gb=2.5, on_cpu=False)
              and g["tiers"]["router"]["on_cpu"] and g["tiers"]["router"]["busy"] is False and g["tiers"]["router"]["tok_s"] is None
              and g["tiers"]["heavy"]["up"] is False and g["ours_busy"] is True)
        check("gather: Claude is not allowed inside the self-tests, whatever the settings say (arch_cloud.allowed)",
              g["cloud"]["ok"] is False and g["cloud"]["why"] and arch_cloud._testing())
        d = B.route({"kind": "generate", "size_tokens": 410, "max_cost_usd": 1.0}, state=g, log=False)
        check("a gathered state routes: the busy worker still wins over a stopped 14B, eta = 410/41 + 30 s", d["target"] == "gpu_worker" and d["eta_s"] == 40.0)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' BROKER CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' BROKER CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
