"""Self-test for route sense (arch_routelearn.py): fixture runs + fixture Logbook rows, no model. Run: python arch_selftest_routelearn.py"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="arch_routelearn_")
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["ARCHITECT_SELFTEST"] = "1"
os.environ["ARCHITECT_NO_CACHE"] = "1"
import arch_learn as L  # noqa: E402
import arch_routelearn as R  # noqa: E402

N = 0
FAILS = []


def check(name, ok, detail=""):
    global N
    N += 1
    if not ok:
        FAILS.append(name)
        print("FAIL:", name, detail)


PROJ = os.path.join(TMP, "proj")
T0 = 1790000000.0
_n = [0]


def run(flow, goal, verdict, secs, model="qwen3:8b", tier="worker", transport="llama", usd=None, roles=("coder",), status=None):
    """One fixture run folder: run.json + trace.jsonl. verdict=None writes no verdict record (status decides)."""
    _n[0] += 1
    d = os.path.join(PROJ, ".arch", "runs", "r%03d" % _n[0])
    os.makedirs(d, exist_ok=True)
    st = T0 + _n[0] * 1000
    with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"flow": flow, "goal": goal, "started": st, "updated": st + secs,
                   "status": status or ("done" if verdict == "pass" else "incomplete")}, f)
    with open(os.path.join(d, "trace.jsonl"), "w", encoding="utf-8") as f:
        for i, role in enumerate(roles):
            f.write(json.dumps({"type": "call", "id": i + 1, "t": st + 1 + i, "flow": flow, "role": role, "model": model,
                                "tier": tier, "transport": transport, "usd": usd, "gen_tokens": 100, "secs": 1.0}) + "\n")
        if verdict is not None:
            f.write(json.dumps({"type": "verdict", "t": st + secs, "flow": flow, "verdict": verdict}) + "\n")
    return d


def main():
    # bugfix on the local worker: 5 of 6 judged + 1 not sure
    qwen_dirs = []
    for i, (v, s) in enumerate([("pass", 100), ("pass", 120), ("fail", 140), ("pass", 160), ("pass", 180), ("pass", 200),
                                ("not_sure", 1000)]):
        qwen_dirs.append(run("bugfix", "fix the crash in module%d" % i, v, s))
    # bugfix with every call on Claude: 5 of 5
    for i in range(5):
        run("bugfix", "fix the crash when saving file%d" % i, "pass", 60, model="claude:claude-sonnet-5-5", tier="", transport="claude", usd=0.2)
    # a run with Claude only for planning, one escalated: modes inferred from the trace
    run("build", "add export", "pass", 50, roles=("planner", "coder"))
    # optimize: too few runs for an opinion
    run("optimize", "make report faster", "pass", 30)
    run("optimize", "make report faster again", "fail", 30)
    # refactor-ish requests with no keyword at all: only the "similar words" level can find them
    for i in range(5):
        run("refactor", "tidy the invoice module totals %d" % i, "pass", 40)
    # a run still in progress (no verdict, status running): ignored
    run("bugfix", "fix the crash live", None, 5, status="running")

    # the Logbook: a Claude Code run, a fleet, a copy of a trace run (must not be counted twice), an unrelated local task
    conn = L._connect()
    base = {"started": T0, "turns": 3, "steps": [], "origin": "x", "kind": ""}
    L._store(conn, dict(base, id="cc_run:a", source="cc_run", title="fix the crash in parser", cost=0.4, status="pass",
                        model="claude-sonnet", by_claude=1))
    L._store(conn, dict(base, id="fleet:b", source="fleet", title="refactor the storage layer", cost=1.1, status="fail",
                        model="claude-sonnet", by_claude=1))
    L._store(conn, dict(base, id="trace:" + os.path.abspath(qwen_dirs[0]), source="trace", title="fix the crash in module0",
                        kind="bugfix", cost=0.0, status="pass", model="qwen3:8b", by_claude=0))
    conn.commit()
    conn.close()

    # 1. samples: traces once, logbook copy dropped, running run ignored
    ss = R.samples(roots=[PROJ])
    traces = [s for s in ss if s["source"] == "trace"]
    check("trace samples", len(traces) == 20, len(traces))
    check("logbook samples", sum(1 for s in ss if s["source"].startswith("logbook:")) == 2,
          [s["id"] for s in ss if s["source"].startswith("logbook:")])
    check("no run counted twice", len({s["id"] for s in ss}) == len(ss))
    check("running run ignored", not any(s["goal"] == "fix the crash live" for s in ss))
    by_goal = {s["goal"]: s for s in ss}
    check("claude mode all", by_goal["fix the crash when saving file0"]["claude"] == "all")
    check("claude mode off", by_goal["add export"]["claude"] == "off")
    check("cc_run is code", by_goal["fix the crash in parser"]["claude"] == "code")
    check("fleet is fleet", by_goal["refactor the storage layer"]["claude"] == "fleet")
    check("logbook flow guessed", by_goal["fix the crash in parser"]["flow"] == "bugfix", by_goal["fix the crash in parser"]["flow"])
    check("not_sure kept as unknown", by_goal["fix the crash in module6"]["verdict"] == "unknown")
    check("secs from run.json", by_goal["fix the crash in module1"]["secs"] == 120)
    check("usd summed", abs(by_goal["fix the crash when saving file0"]["usd"] - 0.2) < 1e-9)

    # 1b. plan / escalation inference from per-call transport
    d1 = run("build", "x", "pass", 10, model="claude:claude-opus-5-5", transport="claude", roles=("planner",))
    with open(os.path.join(d1, "trace.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "call", "id": 9, "role": "coder", "model": "qwen3:8b", "transport": "llama", "gen_tokens": 900}) + "\n")
    check("claude mode plan", R.read_run(d1)["claude"] == "plan", R.read_run(d1))
    with open(os.path.join(d1, "trace.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "call", "id": 10, "role": "coder", "model": "claude:x", "transport": "claude"}) + "\n")
    check("claude mode escalation", R.read_run(d1)["claude"] == "escalation")
    check("main model by tokens", R.read_run(d1)["model"] == "qwen3:8b")
    import shutil
    shutil.rmtree(d1)

    # 2. learn: kinds, counts, rates, medians, confidence
    m = R.learn(roots=[PROJ])
    kinds = m["kinds"]
    check("kind bugfix", "bugfix" in kinds, sorted(kinds))
    check("kind bugfix:crash", "bugfix:crash" in kinds, sorted(kinds))
    opts = {(o["flow"], o["model"], o["claude"]): o for o in kinds["bugfix"]["options"]}
    q = opts.get(("bugfix", "qwen3:8b", "off"))
    check("local option counted", q and (q["passed"], q["failed"], q["unknown"]) == (5, 1, 1), q)
    check("unknown not in rate", q and q["rate"] == round(5 / 6, 4), q and q["rate"])
    check("median secs", q and q["median_secs"] == 160, q and q["median_secs"])
    check("local tier", q and q["tier"] == "worker")
    check("confidence ok", q and q["confidence"] == "ok")
    c = opts.get(("bugfix", "claude:claude-sonnet-5-5", "all"))
    check("claude option", c and c["passed"] == 5 and c["median_usd"] == 0.2, c)
    cc = opts.get(("bugfix", "claude-sonnet", "code"))
    check("one run = no opinion", cc and cc["confidence"] == "none", cc)
    check("no-opinion ranked last", kinds["bugfix"]["options"][-1]["confidence"] == "none")
    check("low confidence band", R.confidence(3) == "low" and R.confidence(2) == "none" and R.confidence(5) == "ok")
    check("wilson: 5/5 beats 1/1", R.wilson_low(5, 5) > R.wilson_low(1, 1))
    check("wilson: 5/5 beats 5/6", R.wilson_low(5, 5) > R.wilson_low(5, 6))

    # 3. advise: best option, numbers, why
    a = R.advise("fix the crash in paginate", roots=[PROJ])
    check("advise picks", a["pick"] is not None, a["why"])
    check("advise best is claude all (no settings given)", a["pick"] and a["pick"]["claude"] == "all", a["pick"])
    check("advise level similar", a["level"] == "similar", a["level"])
    check("advise why has numbers", "5 of 5" in a["why"] and "measured on this machine" in a["why"].lower(), a["why"])
    off = R.advise("fix the crash in paginate", roots=[PROJ], cfg={"online": {"enabled": False}})
    check("claude off -> local pick", off["pick"] and off["pick"]["claude"] == "off" and off["pick"]["model"] == "qwen3:8b", off["pick"])
    check("claude off -> says what was left out", "Claude is off" in off["why"], off["why"])
    check("local why has 5 of 6", "5 of 6" in off["why"], off["why"])
    nope = R.advise("make the export faster", roots=[PROJ])
    check("too few runs -> no opinion", nope["pick"] is None and "No opinion" in nope["why"], nope)
    check("guess optimize", nope["guess"][0] == "optimize", nope["guess"])
    sim = R.advise("tidy the invoice module report", roots=[PROJ])
    check("similar words find the flow", sim["pick"] and sim["pick"]["flow"] == "refactor" and sim["level"] == "similar", sim)
    check("guess bugfix", R.guess_flow("fix the crash in paginate")[0] == "bugfix")
    check("keyword normalised", R.keyword_of("bugfix", "it crashes on load") == "crash")

    # 4. the router hook
    R.forget()
    s = R.suggest_for("fix the crash in paginate", cfg={"online": {"enabled": False}}, roots=[PROJ])
    check("suggest_for", s and s["flow"] == "bugfix" and s["claude"] == "off", s)
    check("suggest_for hint", s and s["hint"].startswith("\n") and "measured on this machine" in s["hint"], s)
    check("suggest_for no opinion -> None", R.suggest_for("make the export faster", roots=[PROJ]) is None)
    check("suggest_for empty", R.suggest_for("", roots=[PROJ]) is None and R.suggest_for(None) is None)
    check("one-line integration never raises", isinstance((R.suggest_for(12345, roots=[PROJ]) or {}).get("hint", ""), str))
    check("cache held", R._CACHE["samples"] is not None and R._CACHE["key"] == (PROJ,))

    # 5. read-only: a missing Logbook is not created; nothing is written to the run folders
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(TMP, "empty")
    check("missing logbook -> no rows", R.logbook_samples() == [])
    check("missing logbook not created", not os.path.exists(os.path.join(TMP, "empty", "logbook.db")))
    files = sorted(os.listdir(os.path.join(PROJ, ".arch", "runs", "r001")))
    check("run folder untouched", files == ["run.json", "trace.jsonl"], files)

    if FAILS:
        print("%d of %d ROUTELEARN CHECKS FAILED: %s" % (len(FAILS), N, ", ".join(FAILS)))
        return 1
    print("ALL %d ROUTELEARN CHECKS PASSED" % N)
    return 0


if __name__ == "__main__":
    sys.exit(main())
