"""Self-tests for arch_overseer.py (the watcher over every Claude Code agent on this machine).
Fixtures for each source (agent log, Claude Code transcript + subagent, hub run + meta, fleet, ledger) in a throwaway data folder,
with a fixed clock: incremental scan, edit and conflict detection, stalls, refusals, summary and lessons shape.
    python arch_selftest_overseer.py
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

NOW = 1_800_000_000.0
_results = []


def check(name, cond, detail=""):
    _results.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"   -> {str(detail)[:400]}"))


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _jl(path, rows, tail=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write(tail)


def _write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def _append(path, text, mtime=None):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def _timeline(data):
    p = os.path.join(data, "learning", "overseer", "timeline.jsonl")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _by(s, name):
    return next((a for a in s["agents"] if a["name"] == name), None)


def fixtures(base):
    data, temp, P = os.path.join(base, "data"), os.path.join(base, "temp"), os.path.join(base, "proj")
    os.makedirs(temp, exist_ok=True)
    J = lambda *p: os.path.join(P, *p)

    # 1. an agent log as launch.py writes it (a copy also sits in learning/agent_logs: same session, folded into one agent)
    lines = ["[system.init] session S1 · model claude-opus-5-5 · 26 tools · mode acceptEdits"]
    for i in range(8):
        lines += ["[tool] " + json.dumps({"file_path": J("hub_ui.html"), "offset": i * 100}), "[tool_result] 1\t<html>",
                  "2\t<body> a tool_result runs on over several lines"]
    lines += ["[tool] " + json.dumps({"file_path": J("shared.py"), "old_string": "a", "new_string": "b"}),
              "[tool_result] The file has been updated successfully.",
              "[tool] " + json.dumps({"command": "python arch_selftest_overseer.py", "description": "run the tests"}),
              "[tool_result] Error: This command requires approval",
              '[tool] {"file_path": "' + J("big.py").replace("\\", "\\\\") + '", "old_string": "a very long old string that was cut',
              "[tool_result] The file has been updated successfully.",
              "[assistant] working on it", "[thinking] hmm"]
    text = "\n".join(lines) + "\n"
    _write(os.path.join(temp, "ag_alpha.log"), text, NOW - 10)
    _write(os.path.join(data, "learning", "agent_logs", "ag_alpha.log"), text, NOW - 10)

    # 2. a hub run (arch_claudecode.record_event + write_meta), with a 300 s gap and a torn last line
    t0 = NOW - 400
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}
    _jl(os.path.join(data, "cc_runs", "ccB.jsonl"), [
        {"t": t0, "type": "system.init", "text": "session S2 · model claude-sonnet-5-5 · 26 tools · mode acceptEdits"},
        {"t": t0 + 1, "type": "tool", "name": "Edit", "id": "tb1", "arg": J("shared.py"), "parent": None,
         "text": json.dumps({"file_path": J("shared.py"), "old_string": "x", "new_string": "y"})},
        {"t": t0 + 2, "type": "tool_result", "id": "tb1", "error": False, "text": "updated"},
        {"t": t0 + 3, "type": "assistant", "text": "part one", "model": "claude-sonnet-5-5", "usage": usage, "parent": None},
        {"t": t0 + 3, "type": "assistant", "text": "part two", "model": "claude-sonnet-5-5", "usage": usage, "parent": None},
        {"t": t0 + 4, "type": "thinking", "text": "plan", "parent": None},
        {"t": t0 + 304, "type": "tool", "name": "Bash", "id": "tb2", "arg": "pytest -q", "text": json.dumps({"command": "pytest -q"})},
        {"t": t0 + 305, "type": "tool_result", "id": "tb2", "error": True,
         "text": "Claude requested permissions to use Bash, but you haven't granted it yet."},
        {"t": NOW - 20, "type": "tool", "name": "Read", "id": "tb3", "arg": J("owned.py"),
         "text": json.dumps({"file_path": J("owned.py")})},
    ], tail='{"t": 1, "type": "tool", "name": "Gr')
    _write(os.path.join(data, "cc_runs", "ccB.meta.json"), json.dumps({
        "prompt": "You are the beta agent for Architect.", "mode": "acceptEdits", "root": P, "started": t0, "ended": None,
        "status": "running", "cost": 0.12, "turns": None, "session": "S2", "id": "ccB"}))

    # 3. a Claude Code transcript (one block per assistant line, usage repeated per message id) and its subagent
    proj = os.path.join(data, "claude_code", "projects", "C--proj")
    common = {"sessionId": "S3", "cwd": P, "permissionMode": "acceptEdits"}
    u1 = {"input_tokens": 3, "output_tokens": 7, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}
    _jl(os.path.join(proj, "S3.jsonl"), [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": _iso(NOW - 200), "sessionId": "S3",
         "content": "You are the gamma agent for Architect."},
        {"type": "user", "message": {"role": "user", "content": "You are the gamma agent for Architect."},
         "timestamp": _iso(NOW - 200), **common},
        {"type": "attachment", "attachment": {"type": "environment"}, "timestamp": _iso(NOW - 200), **common},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u1,
                                          "content": [{"type": "thinking", "thinking": "let me look"}]},
         "timestamp": _iso(NOW - 190), **common},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u1,
                                          "content": [{"type": "tool_use", "id": "tc1", "name": "Read",
                                                       "input": {"file_path": J("notes.md")}}]},
         "timestamp": _iso(NOW - 189), **common},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc1", "is_error": True,
                                                                 "content": "Error: Command contains script block"}]},
         "toolDenialKind": "user-rejected", "timestamp": _iso(NOW - 180), **common},
        {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 2, "output_tokens": 4,
                                                                "cache_read_input_tokens": 1200},
                                          "content": [{"type": "tool_use", "id": "tc2", "name": "Write",
                                                       "input": {"file_path": J("other.py"), "content": "x = 1\n"}}]},
         "timestamp": _iso(NOW - 170), **common},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc2", "is_error": True,
                                                                 "content": [{"type": "text", "text":
                                                                              "<tool_use_error>File has not been read yet"
                                                                              "</tool_use_error>"}]}]},
         "timestamp": _iso(NOW - 160), **common},
        {"type": "assistant", "message": {"id": "m3", "usage": {"input_tokens": 1, "output_tokens": 1},
                                          "content": [{"type": "text", "text": "done"}]},
         "timestamp": _iso(NOW - 60), **common},
    ])
    _jl(os.path.join(proj, "S3", "subagents", "agent-a1.jsonl"), [
        {"type": "assistant", "isSidechain": True, "message": {"id": "s1", "content": [
            {"type": "tool_use", "id": "ts1", "name": "Edit", "input": {"file_path": J("other.py"), "old_string": "1",
                                                                         "new_string": "2"}}]},
         "timestamp": _iso(NOW - 50), **common},
    ])

    # 4. a fleet that owns the hub run, and 5. the ledger with an agent whose log is gone
    _write(os.path.join(data, "fleet", "fl1234567890.json"), json.dumps({
        "id": "fl1234567890", "root": P, "phase": "running", "verdict": None,
        "agents": [{"name": "beta-fleet", "run": "ccB", "kind": "claude", "files": ["owned.py"], "status": "running"}]}))
    _jl(os.path.join(data, "learning", "ledger.jsonl"),
        [{"agent": "ag_old", "status": "done", "cost_usd": 0.5, "turns": "7", "logged": "2026-11-04 19:44"}])
    return data, temp, P


def main():
    base = tempfile.mkdtemp(prefix="arch_overseer_test_")
    old_env = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(base, "data")
    try:
        import arch_overseer as ov
        print("arch_overseer self-test")
        data, temp, P = fixtures(base)
        clock = [NOW]
        o = ov.Overseer(data, temp_dir=temp, project=P, clock=lambda: clock[0])

        # helpers
        check("a tool is named from its input keys (the logs keep no tool name)",
              ov._guess_tool({"file_path": "a"}) == "Read" and ov._guess_tool({"file_path": "a", "old_string": "x"}) == "Edit"
              and ov._guess_tool({"file_path": "a", "content": "x"}) == "Write" and ov._guess_tool({"command": "ls"}) == "Shell"
              and ov._guess_tool({"pattern": "**/*.py"}) == "Glob" and ov._guess_tool({"pattern": "def x|y"}) == "Grep"
              and ov._guess_tool({"pattern": "x", "output_mode": "content"}) == "Grep")
        lo = ov._loose('{"file_path": "C:\\\\a\\\\b.py", "old_string": "cut off her')
        check("tool input JSON cut at 600 characters still gives the path", lo.get("file_path") == "C:\\a\\b.py" and "old_string" in lo, lo)

        # first scan
        s = o.scan()
        names = sorted(a["name"] for a in s["agents"])
        check("every source becomes an agent; the two copies of one log fold into one",
              names == ["ag_alpha", "ag_old", "beta-fleet", "gamma agent", "gamma agent (subagent)"], names)
        al, be, ga = _by(s, "ag_alpha"), _by(s, "beta-fleet"), _by(s, "gamma agent")
        sub, old = _by(s, "gamma agent (subagent)"), _by(s, "ag_old")
        check("log: tool calls by name, inferred", al and al["tool_calls"] == {"Read": 8, "Edit": 2, "Shell": 1}, al and al["tool_calls"])
        check("log: files read vs edited, project-relative", al and al["files_read"] == {"hub_ui.html": 8}
              and al["files_edited"] == {"shared.py": 1, "big.py": 1} and al["edits"] == 2, al and (al["files_read"], al["files_edited"]))
        check("log: a refused test command is counted as a refusal and a refused test",
              al and al["refusals"] == 1 and al["tests_refused"] == 1 and al["errors"] == 0 and al["refused_by_tool"] == {"Shell": 1}, al)
        check("log: session, mode, thinking, live", al and al["session"] == "S1" and al["mode"] == "acceptEdits" and al["thinking"] == 1
              and al["live"] and al["status"] == "running", al)
        check("run: named from its fleet, mode and session from the meta", be and be["session"] == "S2" and be["mode"] == "acceptEdits"
              and be["fleet"] == "fl1234567890" and be["cost"] == 0.12, be)
        check("run: usage repeated on two text blocks of one message counted once",
              be and be["tokens"] == {"in": 10, "out": 5, "cached": 100, "cache_write": 50}, be and be["tokens"])
        check("run: a 300 s gap is a stall, with the tool that was running before it",
              be and len(be["stalls"]) == 1 and be["stalls"][0]["gap_s"] == 300 and be["stalls"][0]["after"] == "Edit", be and be["stalls"])
        check("run: permission refusal on pytest counted; the torn last line is not read",
              be and be["refusals"] == 1 and be["tests_refused"] == 1 and be["tool_calls"] == {"Edit": 1, "Bash": 1, "Read": 1},
              be and (be["refusals"], be["tool_calls"]))
        check("run: an edit outside the fleet brief's files is a stray", be and be["strays"] == ["shared.py"], be and be["strays"])
        check("transcript: role from the prompt, turns per message id, tokens once per message",
              ga and ga["turns"] == 3 and ga["tokens"] == {"in": 6, "out": 12, "cached": 2200, "cache_write": 200}, ga)
        check("transcript: toolDenialKind is a refusal, a plain is_error is an error",
              ga and ga["refusals"] == 1 and ga["errors"] == 1 and ga["refused_by_tool"] == {"Read": 1}
              and ga["error_samples"][0]["tool"] == "Write", ga and (ga["refusals"], ga["errors"], ga["error_samples"]))
        check("transcript: thinking blocks counted, no stall under 120 s", ga and ga["thinking"] == 1 and ga["stalls"] == [], ga)
        check("subagent: named after its parent and its edit kept", sub and sub["files_edited"] == {"other.py": 1}, sub)
        check("ledger: an agent whose log is gone still counts its cost", old and old["cost"] == 0.5 and old["status"] == "done", old)
        check("conflict: two live agents editing the same file (an agent and its own subagent are not one)",
              s["conflicts"] == [{"file": "shared.py", "agents": ["ag_alpha", "beta-fleet"]}], s["conflicts"])
        check("summary shape", set(s) >= {"agents", "totals", "conflicts", "stalls", "top_tools", "busiest_files"}
              and set(s["totals"]) >= {"cost", "tokens", "tools", "edits", "errors", "refusals"}
              and s["top_tools"][0] == ("Read", 10) and s["busiest_files"][0]["file"] == "hub_ui.html", (s["totals"], s["top_tools"]))
        check("totals add up without double counting", s["totals"]["tools"] == 11 + 3 + 2 + 1 and s["totals"]["refusals"] == 3
              and abs(s["totals"]["cost"] - 0.62) < 1e-9 and s["totals"]["live"] == 4, s["totals"])
        check("every agent is flagged as Claude's work, for analysis only",
              all(a["by_claude"] == 1 and a["use"] == "analysis" for a in s["agents"]))
        st_path = os.path.join(data, "learning", "overseer", "state.json")
        with open(st_path, encoding="utf-8") as f:
            st = json.load(f)
        check("state.json holds the current picture, flagged", st["by_claude"] == 1 and st["use"] == "analysis"
              and len(st["agents"]) == 5, list(st)[:8])
        tl = _timeline(data)
        kinds = {x["kind"] for x in tl}
        check("timeline has one line per observation", kinds >= {"seen", "tool", "edit", "refusal", "error", "stall", "conflict"}, kinds)
        check("timeline lines carry t, agent, kind, detail and the never-train flag",
              all({"t", "agent", "kind", "detail"} <= set(x) and x["by_claude"] == 1 and x["use"] == "analysis" for x in tl))

        # incremental
        n1 = len(tl)
        s2 = o.scan()
        check("a scan with nothing new adds nothing and counts nothing twice",
              len(_timeline(data)) == n1 and s2["totals"]["tools"] == s["totals"]["tools"], (len(_timeline(data)), n1))
        log = os.path.join(temp, "ag_alpha.log")
        _append(log, "[tool] " + json.dumps({"file_path": os.path.join(P, "hub_ui.html")}) + "\n[tool_result] ok\n\n"
                "=== STATUS done | cost $0.75 | turns 12 | error None\n[tool] {\"file_path\": \"half", NOW - 5)
        s3 = o.scan()
        al3 = _by(s3, "ag_alpha")
        check("appended lines are read; a half-written line waits", al3["tools_total"] == 12 and al3["files_read"]["hub_ui.html"] == 9,
              al3["tool_calls"])
        check("the STATUS line ends the agent with its cost and turns", al3["status"] == "done" and al3["cost"] == 0.75
              and al3["turns"] == 12 and not al3["live"], al3)
        check("a finished agent's shared file moves from conflicts to overlaps",
              s3["conflicts"] == [] and {"file": "shared.py", "agents": ["ag_alpha", "beta-fleet"]} in s3["overlaps"], s3["overlaps"])
        check("the status change is on the timeline",
              any(x["kind"] == "status" and x["agent"] == "ag_alpha" and "done" in x["detail"] for x in _timeline(data)))
        _append(log, '.py"}\n', NOW - 5)
        s4 = o.scan()
        check("the half line is read once it is finished", _by(s4, "ag_alpha")["tools_total"] == 13)
        copy = os.path.join(data, "learning", "agent_logs", "ag_alpha.log")
        _write(copy, "[system.init] session S1 · model m · 1 tools · mode acceptEdits\n[tool] "
               + json.dumps({"file_path": os.path.join(P, "a.py")}) + "\n", NOW - 5)
        o.scan()
        rec = o.cur["records"]["log:" + os.path.abspath(copy)]
        check("a file that got shorter is read again from the start", rec["tool_calls"] == {"Read": 1} and rec["events"] == 2, rec)

        # live stall
        clock[0] = NOW + 600
        s5 = o.scan()
        live_stalls = [x for x in s5["stalls"] if x["now"]]
        check("a live agent silent for more than 120 s is flagged now", any(x["agent"] == "beta-fleet" for x in live_stalls), live_stalls)
        n5 = len(_timeline(data))
        o.scan()
        check("a live silence is put on the timeline once", len(_timeline(data)) == n5
              and any(x["kind"] == "stall" and x.get("live") for x in _timeline(data)))
        clock[0] = NOW + 5000
        s6 = o.scan()
        check("a 'running' agent silent past LIVE_S is no longer live", not _by(s6, "beta-fleet")["live"] and s6["totals"]["live"] == 0,
              s6["totals"])

        # persistence across instances
        n6 = len(_timeline(data))
        o2 = ov.Overseer(data, temp_dir=temp, project=P, clock=lambda: clock[0])
        s7 = o2.scan()
        check("a new overseer resumes from the saved offsets", len(_timeline(data)) == n6 and s7["totals"]["tools"] == s6["totals"]["tools"],
              (len(_timeline(data)), n6))

        # lessons
        les = o2.lessons()
        joined = "\n".join(les)
        check("lesson: refused test runs -> the orchestrator should run tests",
              "tried to run tests 2 times and were refused" in joined and "orchestrator should run the tests" in joined, les)
        check("lesson: an agent that spent most of its tool calls reading one file",
              any(x.startswith("ag_alpha spent") and "hub_ui.html" in x for x in les), les)
        check("lesson: stalls, strays and shared files", "beta-fleet went quiet" in joined
              and "beta-fleet edited shared.py outside" in joined and "shared.py was edited by ag_alpha and beta-fleet" in joined, les)
        lp = os.path.join(data, "learning", "LESSONS.md")
        o2.lessons()
        with open(lp, encoding="utf-8") as f:
            md = f.read()
        check("LESSONS.md is appended once for the same findings", md.count("\n## ") == 1 and "tried to run tests" in md
              and "never for weight training" in md, md[:300])
        check("module-level summary() uses the shared data folder", isinstance(ov.summary(data)["agents"], list))
        check("the CLI refuses an unknown command", ov.main(["nope"]) == 2)
    finally:
        if old_env is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_env
        shutil.rmtree(base, ignore_errors=True)
    n, ok = len(_results), sum(_results)
    print(f"\n  {'ALL ' + str(n) + ' OVERSEER CHECKS PASSED' if ok == n else str(n - ok) + ' of ' + str(n) + ' OVERSEER CHECKS FAILED'}\n")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
