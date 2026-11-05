"""Self-test for the Logbook (arch_learn.py). Run: python arch_selftest_learn.py"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="arch_learn_")
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(TMP, "data")
import arch_learn as L  # noqa: E402

N = 0
FAILS = []


def check(name, ok, detail=""):
    global N
    N += 1
    if not ok:
        FAILS.append(name)
        print("FAIL:", name, detail)


def jl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def js(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def trace(path, goal, verdict, tools, cost=0.0):
    rows = [{"type": "call", "id": 1, "model": "qwen3:8b", "goal": goal, "label": "write", "cost": cost}]
    rows += [{"type": "tool", "tool": t, "args": {"x": 1}} for t in tools]
    rows += [{"type": "mark", "id": 1, "ok": verdict == "pass", "label": "tests"},
             {"type": "verdict", "verdict": verdict}]
    jl(path, rows)


def main():
    dd = L.data_dir()
    proj = os.path.join(TMP, "proj")
    extra = os.path.join(TMP, "extra")
    # 1. hub Claude Code runs
    cc = os.path.join(dd, "cc_runs", "run1")
    jl(cc + ".jsonl", [
        {"t": 1700000000, "type": "text", "text": "starting", "model": "claude-sonnet", "usage": {"output_tokens": 5}},
        {"t": 1700000001, "type": "tool_use", "name": "Grep", "input": {"pattern": "page"}},
        {"t": 1700000002, "type": "tool_use", "name": "Read", "input": {"file": "api.py"}},
        {"t": 1700000003, "type": "tool_use", "name": "Edit", "input": {"file": "api.py"}},
        {"t": 1700000004, "type": "result", "text": "all done"}])
    js(cc + ".meta.json", {"prompt": "add pagination to the list api", "status": "done", "cost": 0.12, "turns": 3})
    # 2. fleet
    js(os.path.join(dd, "fleet", "job1.json"), {
        "prompt": "refactor the billing module", "status": "failed", "cost": 0.5,
        "agents": [{"model": "qwen3:14b", "text": "tried it"}, {"model": "qwen3:14b", "tool": "grep", "args": "x"}]})
    # 3. Claude Code's own transcript
    jl(os.path.join(dd, "claude_code", "projects", "p1", "s.jsonl"), [
        {"type": "user", "message": {"role": "user", "content": "fix the login redirect bug"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus", "content": [
            {"type": "text", "text": "looking"}, {"type": "tool_use", "name": "Read", "input": {"f": "login.py"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}])
    # 4. project traces
    trace(os.path.join(proj, ".arch", "runs", "r1", "trace.jsonl"), "add pagination to the users endpoint", "pass",
          ["grep", "read", "apply"], cost=0.02)
    trace(os.path.join(proj, ".arch", "runs", "r2", "trace.jsonl"), "fix the login redirect loop", "fail",
          ["read", "apply"], cost=0.01)
    # 5. extra folder
    jl(os.path.join(extra, "mine", "t.jsonl"), [
        {"type": "user", "message": {"role": "user", "content": "write a csv exporter"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "sure"}]}}])

    r1 = L.ingest(extra_dirs=[extra], projects=[proj], portfolio=False, include_cwd=False)
    check("first ingest adds all six tasks", r1["added"] == 6 and r1["updated"] == 0, str(r1))
    r2 = L.ingest(extra_dirs=[extra], projects=[proj], portfolio=False, include_cwd=False)
    check("second ingest adds nothing", r2["added"] == 0 and r2["updated"] == 0 and r2["skipped"] == 6, str(r2))

    s = L.stats()
    check("stats: six tasks", s["tasks"] == 6, str(s))
    check("stats: by source", s["by_source"] == {"cc_run": 1, "fleet": 1, "claude_code": 1, "trace": 2, "extra": 1},
          str(s["by_source"]))
    check("stats: total cost", abs(s["total_cost"] - 0.65) < 1e-6, str(s["total_cost"]))
    check("stats: cost by source", abs(s["cost_by_source"]["fleet"] - 0.5) < 1e-6)
    check("stats: success rate over known outcomes only", s["passed"] == 2 and s["failed"] == 2 and
          s["success_rate"] == 0.5, str(s))
    tools = dict(s["top_tools"])
    check("stats: top tools", tools.get("Read") == 2 and tools.get("read") == 2 and tools.get("Grep") == 1, str(tools))
    check("stats: steps stored", s["steps"] > 15, str(s["steps"]))

    # by_claude flags and the training exclusion
    ex = L.exclude_from_training()
    check("Claude-made tasks are excluded", "cc_run:run1" in ex and any(i.startswith("claude_code:") for i in ex), str(ex))
    check("local-model tasks are not excluded", not any(i.startswith("trace:") or i == "fleet:job1.json" for i in ex), str(ex))
    check("extra transcript without a Claude model is not flagged as Claude unless it is a Claude transcript",
          len(ex) == 3, str(ex))  # cc_run, claude_code, extra Claude-style transcript
    check("stats: by_claude count", s["by_claude"] == 3, str(s["by_claude"]))

    # similar()
    sim = L.similar("add pagination to the orders list", k=3)
    check("similar: finds pagination tasks first", len(sim) >= 2 and "pagination" in sim[0]["title"] and
          "pagination" in sim[1]["title"], str(sim))
    check("similar: carries outcome, cost and tools", sim[0]["status"] in ("pass", "unknown") and
          isinstance(sim[0]["cost"], float) and isinstance(sim[0]["tools"], list), str(sim[0]))
    top_trace = [x for x in sim if x["source"] == "trace"]
    check("similar: trace tool sequence kept in order", top_trace and top_trace[0]["tools"] == ["grep", "read", "apply"],
          str(top_trace))
    login = L.similar("login redirect problem", k=5)
    check("similar: different request ranks a different task first", login and "login" in login[0]["title"], str(login))
    check("similar: passed work outranks failed on equal footing",
          L.similar("login redirect", k=5, include_claude=False)[0]["source"] == "trace")
    check("similar: no Claude when asked", all(not x["by_claude"] for x in L.similar("login redirect", include_claude=False)))
    check("similar: empty text gives nothing", L.similar("   ") == [])
    check("similar: no hit gives nothing", L.similar("zzzqqq xylophone") == [])

    # playbooks
    p = L.playbooks()
    check("playbooks file written in memory/", p == os.path.join(dd, "memory", "playbooks.md") and os.path.isfile(p), p)
    with open(p, encoding="utf-8") as f:
        text = f.read()
    check("playbooks: dated lines with success rate", "- 20" in text and "worked" in text, text)
    check("playbooks: names the usual tool sequence", "grep > read > apply" in text, text)
    check("playbooks: failure line mentions a common failure", "common failure" in text, text)
    p2 = L.playbooks(include_claude=False)
    with open(p2, encoding="utf-8") as f:
        t2 = f.read()
    check("playbooks without Claude leaves Claude-only kinds out", "csv" not in t2 and "billing" in t2, t2)

    # growth: a run that got longer is re-read, nothing else
    with open(cc + ".jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": 1700000009, "type": "tool_use", "name": "Bash", "input": {"cmd": "pytest"}}) + "\n")
    r3 = L.ingest(extra_dirs=[extra], projects=[proj], portfolio=False, include_cwd=False)
    check("a grown file is updated, the rest skipped", r3["updated"] == 1 and r3["added"] == 0 and r3["skipped"] == 5, str(r3))
    check("grown task has the new tool step", dict(L.stats()["top_tools"]).get("Bash") == 1)
    check("task count unchanged after re-read", L.stats()["tasks"] == 6)

    # meta wins for the hub run
    sim = L.similar("pagination list api", k=5)
    hub = [x for x in sim if x["id"] == "cc_run:run1"]
    check("cc_run meta: status, cost, turns", hub and hub[0]["status"] == "pass" and abs(hub[0]["cost"] - 0.12) < 1e-9 and
          hub[0]["turns"] == 3 and hub[0]["by_claude"] is True, str(hub))

    # a broken file does not stop ingest
    with open(os.path.join(dd, "cc_runs", "bad.jsonl"), "w", encoding="utf-8") as f:
        f.write("not json\n{\n")
    r4 = L.ingest(extra_dirs=[extra], projects=[proj], portfolio=False, include_cwd=False)
    check("a broken file is tolerated", r4["added"] == 1 and L.stats()["tasks"] == 7, str(r4))

    # CLI
    check("CLI stats returns 0", L.main(["stats"]) == 0)
    check("CLI unknown command returns 2", L.main(["nope"]) == 2)

    if FAILS:
        print("%d of %d LOGBOOK CHECKS FAILED: %s" % (len(FAILS), N, "; ".join(FAILS)))
        return 1
    print("ALL %d LOGBOOK CHECKS PASSED" % N)
    return 0


if __name__ == "__main__":
    sys.exit(main())
