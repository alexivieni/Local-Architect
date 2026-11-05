"""Self-test for arch_orchestration: fixture transcripts and a fixture data dir in a temp folder (never the real home)."""
import json
import os
import shutil
import tempfile

import arch_orchestration as orch

N = [0, 0]


def ok(cond, what):
    N[0 if cond else 1] += 1
    print(("[ok] " if cond else "[x] ") + what)


def _w(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def _asst(mid, usage=None, tools=(), text=None, ts="2026-11-05T10:00:00Z"):
    content = [{"type": "tool_use", "id": tid, "name": n, "input": inp} for tid, n, inp in tools]
    if text:
        content.append({"type": "text", "text": text})
    m = {"model": "claude-x", "id": mid, "role": "assistant", "content": content}
    if usage is not None:
        m["usage"] = usage
    return {"type": "assistant", "timestamp": ts, "message": m}


def _fixture(base):
    proj = os.path.join(base, "projects", "C--proj")
    sid = "sess1"
    _w(os.path.join(proj, sid + ".jsonl"), [
        {"type": "user", "timestamp": "2026-11-05T09:59:00Z", "sessionId": sid, "message": {"role": "user", "content": "build it"}},
        _asst("m1", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10},
              tools=[("tu_A", "Agent", {"description": "child A"}), ("tu_r", "Read", {})]),
        _asst("m1", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}),
        "not json {",
        _asst("m2", None, text="done"),            # missing usage
    ])
    sub = os.path.join(proj, sid, "subagents")
    _w(os.path.join(sub, "agent-aaa.jsonl"), [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "X" * 900}]}},
        _asst("s1", {"input_tokens": 100, "output_tokens": 20}, tools=[("tu_B", "Agent", {"description": "grandchild"})]),
        _asst("s2", {"input_tokens": 50, "cache_read_input_tokens": 300, "output_tokens": 7}, text="A result"),
    ])
    with open(os.path.join(sub, "agent-aaa.meta.json"), "w") as f:
        json.dump({"description": "child A", "toolUseId": "tu_A", "agentType": "general-purpose"}, f)
    _w(os.path.join(sub, "agent-bbb.jsonl"), [_asst("g1", {"output_tokens": 3})])
    with open(os.path.join(sub, "agent-bbb.meta.json"), "w") as f:
        json.dump({"description": "grandchild", "toolUseId": "tu_B"}, f)
    wf = os.path.join(sub, "workflows", "wf_abc-123")
    _w(os.path.join(wf, "journal.jsonl"), [{"type": "launched"},
        {"type": "started", "agentId": "ccc", "label": "research:x", "phase": "Research"},
        {"type": "result", "agentId": "ccc", "result": {"findings": [1]}}])
    _w(os.path.join(wf, "agent-ccc.jsonl"), [_asst("w1", {"input_tokens": 1, "cache_creation_input_tokens": 99, "output_tokens": 2})])
    scripts = os.path.join(proj, sid, "workflows", "scripts")
    os.makedirs(scripts)
    with open(os.path.join(scripts, "my-flow-wf_abc-123.js"), "w") as f:
        f.write("export const meta = {\n  name: 'my-flow',\n  description: 'does things',\n  phases: [\n"
                "    { title: 'Research', detail: 'r' },\n    { title: 'Verify', detail: 'v' },\n  ],\n}\n")
    return os.path.join(base, "projects"), proj


def _data_fixture(base):
    dd = os.path.join(base, "data")
    rd = os.path.join(dd, "evals", "runs", "train", ".arch", "runs")
    call = {"type": "call", "id": 1, "t": 1.0, "flow": "build", "role": "coder", "tier": "worker", "model": "m:7b",
            "prompt_tokens": 1000, "gen_tokens": 100, "prefix_reused_est": 400, "ctx_pct": 30, "transport": "ollama",
            "cached_exact": False, "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "write add()"}],
            "reply": "FILE: a.py\n```\ndef add(a, b):\n    return a + b\n```"}
    good = os.path.join(rd, "r1")
    _w(os.path.join(good, "trace.jsonl"), [call, dict(call, id=2, cached_exact=True, prompt_tokens=0, gen_tokens=0, prefix_reused_est=0),
        {"type": "mark", "id": 1, "accepted": True}, {"type": "verdict", "verdict": "pass", "flow": "build"}])
    bad = os.path.join(rd, "r2")
    _w(os.path.join(bad, "trace.jsonl"), [call, {"type": "mark", "id": 1, "accepted": True},
                                          {"type": "verdict", "verdict": "pass"}])
    with open(os.path.join(bad, "eval_task.json"), "w") as f:
        json.dump({"verdict": "fail"}, f)
    cl = os.path.join(rd, "r3")
    _w(os.path.join(cl, "trace.jsonl"), [dict(call, transport="claude"), {"type": "verdict", "verdict": "pass"}])
    return dd


def run():
    N[0] = N[1] = 0
    tmp = tempfile.mkdtemp(prefix="orch_st_")
    try:
        base, proj = _fixture(tmp)
        ok(orch.cache_share(80, 10, 10) == 0.8 and orch.cache_share(0, 0, 0) == 0.0, "cache hit share math")
        p = orch.parse_transcript(os.path.join(proj, "sess1.jsonl"))
        ok(p["turns"] == 1 and p["input_tokens"] == 10 and p["cache_read"] == 80, "streamed duplicate message counted once")
        ok(p["peak_context"] == 100, "peak context = input + cache read + cache write")
        ok(p["tools"] == {"Agent": 1, "Read": 1} and "tu_A" in p["spawns"], "tool calls by name and spawns")
        ok(p["prompt"] == "build it" and p["result"] == "done", "prompt and result; missing usage and a broken line tolerated")

        c = orch.claude_section(limit=5, base=base, near=os.path.join(tmp, "x"))
        ok(c["sessions_found"] == 1, "one session found")
        s = c["sessions"][0]
        kids = {n.get("label"): n for n in s["children"]}
        ok("child A" in kids and kids["child A"]["parent"] == "sess1", "subagent parent = the session's Agent call")
        a = kids["child A"]
        ok(len(a["children"]) == 1 and a["children"][0]["label"] == "grandchild" and a["children"][0]["parent"] == "aaa",
           "grandchild parent = the agent that spawned it")
        ok(len(a["prompt"]) < 700 and a["prompt"].endswith("..."), "prompt truncated to 600 chars")
        ok(a["peak_context"] == 350 and a["tokens"]["input"] == 150, "agent tokens and peak")
        wf = kids.get("my-flow")
        ok(wf and wf["kind"] == "workflow" and wf["description"] == "does things", "workflow named from its script meta")
        ph = wf["children"] if wf else []
        ok(len(ph) == 1 and ph[0]["label"] == "Research" and ph[0]["children"][0]["label"] == "research:x",
           "workflow -> phase -> agent (empty phases dropped)")
        ok("findings" in (ph[0]["children"][0]["result"] if ph else ""), "workflow agent result from the journal")
        tot = c["totals"]
        ok(tot["agents"] == 3 and tot["workflows"] == 1, "agents counted across the tree")
        ok(tot["input"] == 10 + 150 + 1 and tot["cache_write"] == 10 + 99, "token totals roll up")
        ok("_spawns" not in json.dumps(s), "internal keys stripped; JSON-able")
        ok(orch.claude_section(base=os.path.join(tmp, "none"))["sessions_found"] == 0, "missing projects folder is empty, not an error")

        dd = _data_fixture(tmp)
        loc = orch.local_section(limit=10, data_dir=dd, roots=[])
        ok(loc["totals"]["runs"] == 3 and loc["totals"]["cached_exact"] == 1, "local runs and exact cache hits")
        r1 = [r for r in loc["runs"] if r["run"] == "r1"][0]
        ok(r1["verdict"] == "pass" and r1["tokens_in"] == 1000 and r1["prefix_reuse_share"] == 0.4 and r1["peak_ctx_pct"] == 30,
           "per-run tokens, prefix reuse, ctx%")
        ok(loc["by_tier"]["worker"]["models"]["m:7b"] >= 2, "calls by tier/model")

        empty = os.path.join(tmp, "emptyproj")
        os.makedirs(empty)
        tr = orch.training_section(data_dir=dd, project_root=empty)
        ok("error" not in tr, "training counts computed: " + str(tr.get("error", "")))
        ok(tr.get("train_bucket_runs") == 3, "kept train-bucket runs counted")
        ex = tr.get("excluded") or {}
        ok(ex.get("not_passed") == 1 and ex.get("by_claude") == 1, "exclusions: not passed, by Claude")
        ok(tr.get("counts", {}).get("sft", 0) >= 1, "SFT sample from the passed run")
        ok(not os.path.exists(os.path.join(empty, ".arch")), "nothing written")

        snap = orch.snapshot(limit=2, claude_base=base, data_dir=dd, roots=[], project_root=empty)
        json.dumps(snap)
        ok(all(k in snap for k in ("claude", "local", "training", "glossary")) and "DPO pair" in snap["glossary"],
           "snapshot has all four sections and is JSON-able")
        ok(isinstance(orch.headline(snap), str), "headline text")

        # ---- categories
        C = orch.classify
        ok(C(edited=["C:/x/hub_ui.html"])[0] == "ui" and C(edited=["a/arch_selftest_x.py"])[0] == "harness"
           and C(edited=["a/b.py"]) == ("coding", "edited b.py"), "edited files decide first (ui, harness, coding)")
        ok(C("Review five server/hub changes")[0] in orch.CATEGORIES and C("Tune the GPU model server")[0] == "ai/models",
           "keywords in the label")
        ok(C("x", "", "", (), {}, None) == ("other", "no rule matched"), "nothing matched -> other, with the reason")
        ok(C("Plan the architecture") == C("Plan the architecture"), "deterministic")
        ok(C("", "", "", (), {"Agent": 2}) == ("agentic", "spawned agents"), "tools as a last resort")
        p2 = orch.parse_transcript(os.path.join(proj, "sess1.jsonl"))
        ok(p2.get("edited") == [], "no edits in the fixture session")
        s2 = orch.snapshot(limit=2, claude_base=base, data_dir=dd, roots=[], project_root=empty)
        sess = s2["claude"]["sessions"][0]
        ok(all("category" in n and "category_reason" in n and "collapsed_default" in n for n in [sess] + sess["children"]),
           "every node tagged: category, reason, collapsed_default")
        ok(sess["collapsed_default"] is False, "a session is never collapsed by default")
        cat = s2["categories"]
        ok("other" not in cat["shown"] and cat["folded_total"] == sum(cat["folded"].values()), "default hides 'other', folded into counts")
        allc = orch.snapshot(limit=2, claude_base=base, data_dir=dd, roots=[], project_root=empty, categories=list(orch.CATEGORIES))
        ok(allc["categories"]["folded_total"] == 0 and sum(allc["categories"]["counts"].values()) >= 3, "categories=all hides nothing")
        none = orch.snapshot(limit=2, claude_base=base, data_dir=dd, roots=[], project_root=empty, categories=["data"])
        ok(none["claude"]["totals"]["agents"] == 3 and none["categories"]["folded_total"] >= 1,
           "a filter hides agents but totals stay honest")
        ok(all(r.get("category") for r in loc["runs"]) or all("category" in r for r in allc["local"]["runs"]), "local runs carry a category")
        ok(set(orch.row(a)) == set(orch.COLUMNS) and a["source"] == "claude", "a Claude agent row has the shared columns")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{N[0]} passed, {N[1]} failed")
    return N[1] == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
