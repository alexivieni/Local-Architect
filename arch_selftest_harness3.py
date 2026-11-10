"""Self-test for the harness additions: auto-compaction (arch_compact), the delegate tool (arch_delegate + arch_tools) and permission
modes (arch_permissions). The scripted fake model only; no real model, no Claude. Isolated ARCHITECT_HOME / ARCHITECT_DATA_DIR."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
N = [0, 0]


def ok(cond, what, detail=None):
    N[0 if cond else 1] += 1
    print(("[ok] " if cond else "[x] ") + what + ("" if cond or detail is None else f"  -- {str(detail)[:300]}"))


def history():
    msgs = [{"role": "system", "content": "SYSTEM RULES " * 20}, {"role": "user", "content": "TASK: fix paginate() in pager.py " * 5}]
    for i in range(1, 15):
        msgs.append({"role": "assistant", "content": f"[step {i}] read({{\"path\": \"pager.py\"}})\nRESULT:\n" + "x = 1  # filler line\n" * 40})
    msgs.append({"role": "assistant", "content": "I decided to keep the page size at 10.\n[step 15] edit_lines({\"path\": \"pager.py\"})\nchanged pager.py: lines 3..4 replaced"})
    msgs.append({"role": "user", "content": "refused: after this edit utils/helpers.py would not parse (line 3: invalid syntax)\nTODO: add a test for page 0"})
    for i in range(16, 20):
        msgs.append({"role": "assistant", "content": f"[step {i}] ls()\nRESULT:\npager.py\ntests/test_pager.py"})
    return msgs


def compact_checks():
    import arch_compact as cp
    msgs = history()
    before = cp.messages_tokens(msgs)
    window = int(before * 1.5)
    same, info = cp.compact(msgs, window)
    ok(not info["compacted"] and same == msgs, "compact: under 80% of the window nothing changes", info)
    window = int(before / 0.9)                                      # the history is at 90% of the window
    ev = []
    new, info = cp.compact(msgs, window, keep_prefix=2, events=ev)
    ok(info["compacted"] and info["after_tokens"] < info["before_tokens"] and cp.messages_tokens(new) == info["after_tokens"],
       "compact: size drops and the event records before/after", info)
    ok(info["after_tokens"] <= int(window * 0.8), "compact: the result fits under the threshold", (info["after_tokens"], int(window * 0.8)))
    ok(ev == [info], "compact: the event is appended to the caller's list")
    ok(new[0] is msgs[0] and new[1] is msgs[1] and json.dumps(new[:2]) == json.dumps(msgs[:2]), "compact: the stable prefix is byte-identical (same objects)")
    ok(new[-info["kept_tail"]:] == msgs[-info["kept_tail"]:] and all(a is b for a, b in zip(new[-info["kept_tail"]:], msgs[-info["kept_tail"]:])),
       "compact: the newest turns are kept verbatim")
    dg = new[2]["content"]
    ok(dg.startswith(cp.MARK) and "pager.py" in dg, "digest: names the file touched", dg)
    ok("utils/helpers.py would not parse" in dg.replace("\n", " ") or "would not parse" in dg, "digest: keeps the failure", dg)
    ok("TODO: add a test for page 0" in dg, "digest: keeps the open todo", dg)
    ok("decided to keep the page size" in dg, "digest: keeps the decision", dg)
    new2, info2 = cp.compact(msgs, window, keep_prefix=2)
    ok(new2[2]["content"] == dg, "digest: the same turns give the same bytes (prefix cache stays valid)")
    # compacting again folds the earlier digest and loses nothing
    more = new + [{"role": "assistant", "content": "[step 20] apply(...)\nchanged: extra_file.py\nerror: tests failed"} for _ in range(6)]
    n3, i3 = cp.compact(more, cp.messages_tokens(more), pct=50, keep_tail=2, keep_prefix=2)
    d3 = n3[2]["content"] if i3["compacted"] else ""
    ok(i3["compacted"] and "pager.py" in d3 and "would not parse" in d3 and "extra_file.py" in d3 and d3.count(cp.MARK) == 1,
       "compact twice: one digest, earlier facts kept", d3)
    ok(n3[0] is msgs[0], "compact twice: prefix still the original object")
    # router: used only when it returns something shorter that keeps the heading
    r_ok = lambda p: cp.MARK + " short]\nFiles touched:\n- pager.py"
    n4, i4 = cp.compact(msgs, window, keep_prefix=2, router=r_ok)
    ok(i4["method"] == "router" and "Files touched" in n4[2]["content"], "router: a shorter answer is used", i4)
    n5, i5 = cp.compact(msgs, window, keep_prefix=2, router=lambda p: "x" * 100000)
    ok(i5["method"] == "extractive" and n5[2]["content"] == dg, "router: a longer/odd answer is ignored")

    def boom(p):
        raise RuntimeError("down")
    n6, i6 = cp.compact(msgs, window, keep_prefix=2, router=boom)
    ok(i6["method"] == "extractive", "router: a failing router falls back to code")
    # nothing old enough
    short = msgs[:3]
    n7, i7 = cp.compact(short, 10, keep_tail=5)
    ok(not i7["compacted"] and n7 == short, "compact: nothing to fold -> unchanged")
    turns = [m["content"] for m in msgs[2:]]
    t2, ti = cp.compact_turns(turns, int(cp.messages_tokens(msgs[2:]) / 0.9))
    ok(ti["compacted"] and len(t2) < len(turns) and t2[-1] == turns[-1], "compact_turns: the squad loop's strings shrink, newest kept", ti)
    e = types.SimpleNamespace(say=lambda m: None)
    cp.record(e, info)
    ok(e.compactions and e.compactions[0]["after_tokens"] == info["after_tokens"], "record: the event lands on the run")
    c = cp.conf({"compact": {"pct": 500, "keep_tail": "x", "enabled": "off"}})
    ok(c["pct"] == 95 and c["keep_tail"] == 4 and c["enabled"] is False, "conf: values are clamped, junk falls back", c)
    ok(cp.window_tokens({"limits": {"num_ctx": 8192}}, "nosuchrole") == 8192, "window_tokens: falls back to limits.num_ctx")


def delegate_checks(tmp):
    import arch_core as core
    import arch_squad as sq
    import arch_tools
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "app.py"), "w") as f:
        f.write("def add(a, b):\n    return a + b\n")
    srv, over = sq.start_fake()
    cfg = core.deep_merge(core.load_config(proj), over)
    dd = os.path.join(tmp, "data")
    wf = os.path.join(tmp, "child.yaml")
    with open(wf, "w") as f:
        f.write("name: child\ndescription: write one file\nphases:\n  - title: Build\n    agent: builder\n    owns: \"{owns}\"\n"
                "    task: FAKE:WRITE gen.py\n")
    bad_wf = os.path.join(tmp, "bad.yaml")
    with open(bad_wf, "w") as f:
        f.write("name: bad\ndescription: x\nphases:\n  - title: Build\n    agent: builder\n    owns: secret.py\n    task: FAKE:WRITE secret.py\n")
    seen = []

    def parent_engine(budget=None, writes=True, depth=0, owns=("gen.py", "tests")):
        crew = sq.Crew(proj, "g", cfg=cfg, data_dir=dd, workflow="parent", budget=budget)
        e = sq._owned_engine_class()(proj, "crew", "g", cfg, review="auto", say=sq._quiet, persist=False)
        e.owns = tuple(owns)
        e.crew, e.agent_def, e.delegate_depth = crew, {"name": "coder", "writes": writes}, depth
        return e, crew

    try:
        e, crew = parent_engine(budget={"calls": 100, "tokens": 400000, "seconds": 600})
        out, good = arch_tools.call(e, "delegate", {"workflow": wf, "goal": "make gen.py", "owns": ["gen.py"]})
        # the child wrote gen.py and nothing tested it: not_sure (crews are judged by code since 2026-11-09)
        ok(good and "delegated" in out and "not_sure" in out, "delegate: runs the workflow and returns the digest (an untested write: not_sure)", out)
        ok(os.path.isfile(os.path.join(proj, "gen.py")) and "gen.py" in out, "delegate: the child wrote the file it owns and the digest names it", out)
        ok(crew.budget.spent["calls"] > 0, "delegate: the child's spend is charged to the caller's budget", crew.budget.spent)
        ok(any(ev.get("type") == "delegated" for ev in [json.loads(x) for x in open(os.path.join(crew.dir, "journal.jsonl"), encoding="utf-8")]),
           "delegate: a 'delegated' event is journaled")
        out, good = arch_tools.call(e, "delegate", {"workflow": wf, "goal": "x", "owns": ["other.py"]})
        ok("do not own other.py" in out, "delegate: files the caller does not own are refused", out)
        out, good = arch_tools.call(e, "delegate", {"workflow": bad_wf, "goal": "x", "owns": ["gen.py"]})
        ok("secret.py" in out and "outside" in out and not os.path.exists(os.path.join(proj, "secret.py")),
           "delegate: a workflow with a literal owns outside the request is refused", out)
        ok("no workflow" in arch_tools.call(e, "delegate", {"workflow": "nope_nothing", "goal": "x", "owns": ["gen.py"]})[0],
           "delegate: an unknown workflow is explained")
        ro, _ = parent_engine(writes=False)
        out, _ = arch_tools.call(ro, "delegate", {"workflow": wf, "goal": "x", "owns": []})
        ok("read-only" in out, "delegate: refused for a read-only role", out)
        deep, _ = parent_engine(depth=1)
        out, _ = arch_tools.call(deep, "delegate", {"workflow": wf, "goal": "x", "owns": ["gen.py"]})
        ok("cannot delegate again" in out, "delegate: depth limit 1", out)
        gone, _ = parent_engine(budget={"calls": 1})
        gone.crew.budget.charge(calls=5)
        out, _ = arch_tools.call(gone, "delegate", {"workflow": wf, "goal": "x", "owns": ["gen.py"]})
        ok("budget" in out, "delegate: a spent budget refuses", out)

        # the child's engines are one level deeper and never wider than the request
        def spy(d, owns):
            c = sq._owned_engine_class()(proj, "crew", "g", cfg, review="auto", say=sq._quiet, persist=False)
            c.owns = tuple(owns)
            seen.append(c)
            return c
        pe, pc = parent_engine(owns=("gen.py", "tests"))
        pc.engine_factory = spy
        arch_tools.call(pe, "delegate", {"workflow": wf, "goal": "make gen.py", "owns": ["gen.py"]})
        ok(seen and all(getattr(c, "delegate_depth", 0) == 1 and set(c.owns) <= {"gen.py"} for c in seen[1:] or seen),
           "delegate: child engines are depth 1 and own only the requested files", [(getattr(c, "delegate_depth", None), c.owns) for c in seen])
        # a read-only agent definition cannot hold the tool at all
        d = sq.load_agent("auditor")
        d = dict(d, tools=list(d["tools"]) + ["delegate"])
        d.pop("_source", None)
        ok(any("writes is false" in p for p in sq.validate(d)), "delegate: a read-only agent definition cannot list it")
        ok("delegate" in arch_tools.TOOLS and arch_tools.TOOLS["delegate"]["writes"], "delegate: registered as a writing tool")
        # plan mode refuses it
        pm, _ = parent_engine()
        pm.cfg = core.deep_merge(cfg, {"run": {"permission_mode": "plan"}})
        out, good = arch_tools.call(pm, "delegate", {"workflow": wf, "goal": "x", "owns": ["gen.py"]})
        ok(not good and "plan mode" in out, "delegate: refused in plan mode", out)
    finally:
        srv.shutdown()
    return proj, cfg


def permission_checks(tmp, proj, cfg):
    import arch_core as core
    import arch_permissions as pm
    import arch_squad as sq
    import arch_tools

    def with_mode(m, **extra):
        return core.deep_merge(cfg, {"run": dict({"permission_mode": m}, **extra)})
    ok(pm.mode({}, None) == "auto" and pm.mode(cfg, None) == "auto", "mode: default is auto")
    ok(pm.mode(with_mode("plan")) == "plan" and pm.mode(with_mode("ask")) == "ask", "mode: plan and ask are read from run.permission_mode")
    ok(pm.mode(with_mode("bogus")) == "ask", "mode: an unknown value takes the careful side")
    ok(pm.mode(cfg, types.SimpleNamespace(permission_mode="plan")) == "plan", "mode: an attribute on the run wins")

    Gated = core.Engine        # the permission gate is now built into Engine._commit itself

    def engine(mode_cfg):
        e = Gated(proj, "crew", "g", mode_cfg, review="auto", say=lambda m: None, persist=False)
        return e
    block = "FILE: made.py\n```python\nX = 1\n```\n"
    target = os.path.join(proj, "made.py")
    # auto
    if os.path.exists(target):
        os.remove(target)
    e = engine(with_mode("auto"))
    r = e.apply(block, "t")
    ok(r[0]["status"] == "applied" and os.path.isfile(target), "auto: the change is written as before", r)
    os.remove(target)
    # plan
    e = engine(with_mode("plan"))
    r = e.apply(block, "t")
    ok(r[0]["status"] == "error" and not os.path.exists(target) and "plan mode" in r[0]["msg"], "plan: Engine.apply writes nothing", r)
    out, good = arch_tools.call(e, "apply", {"blocks": block})
    ok(not good and "plan mode" in out and not os.path.exists(target), "plan: the apply tool is refused", out)
    out, good = arch_tools.call(e, "edit_lines", {"path": "app.py", "start": 1, "end": 1, "text": "x"})
    ok(not good and "plan mode" in out, "plan: edit_lines is refused")
    out, good = arch_tools.call(e, "ls", {})
    ok(good, "plan: read-only tools still work", out)
    names = pm.tools_allowed(e, list(arch_tools.TOOLS))
    ok(names and not any(arch_tools.TOOLS[n]["writes"] for n in names) and "read" in names, "plan: tools_allowed removes every writing tool", names)
    ok(pm.tools_allowed(engine(with_mode("auto")), ["read", "apply"]) == ["read", "apply"], "auto: tools_allowed unchanged")
    # ask: approved by the hub
    e = engine(with_mode("ask", permission_timeout_s=5))
    res = {}
    t = threading.Thread(target=lambda: res.update(r=e.apply(block, "t")))
    t.start()
    got = []
    for _ in range(100):
        got = pm.pending()
        if got:
            break
        time.sleep(0.05)
    ok(len(got) == 1 and got[0]["rel"] == "made.py" and "X = 1" in got[0]["diff"] and not os.path.exists(target),
       "ask: the change waits, nothing is written, the request shows the diff", got)
    ok(pm.answer(got[0]["run"], got[0]["id"], True), "ask: the hub answers through answer()")
    t.join(10)
    ok(res["r"][0]["status"] == "applied" and os.path.isfile(target), "ask: approved -> written", res)
    ok(not pm.pending(), "ask: a closed request is no longer pending")
    os.remove(target)
    # ask: rejected
    e = engine(with_mode("ask", permission_timeout_s=5))
    res = {}
    t = threading.Thread(target=lambda: res.update(r=e.apply(block, "t")))
    t.start()
    got = []
    for _ in range(100):
        got = pm.pending()
        if got:
            break
        time.sleep(0.05)
    # the hub may also write the answer file directly
    with open(os.path.join(os.path.dirname(os.path.join(core.data_dir(), "approvals", got[0]["run"], "x")), got[0]["id"] + ".answer.json"), "w") as f:
        json.dump({"allow": False}, f)
    t.join(10)
    ok(res["r"][0]["status"] == "declined" and not os.path.exists(target), "ask: rejected through the answer file -> not written", res)
    # ask: timeout rejects
    e = engine(with_mode("ask", permission_timeout_s=0.4))
    t0 = time.time()
    r = e.apply(block, "t")
    ok(r[0]["status"] == "declined" and "no answer" in r[0]["msg"] and not os.path.exists(target) and time.time() - t0 < 5,
       "ask: no answer in time = rejected", r)
    ok(not pm.pending(), "ask: a timed-out request is closed")
    d, why, rid = pm.gate_apply(engine(with_mode("ask")), {"rel": "z.py", "diff": "d"})
    ok(d == "wait" and rid and pm.pending(), "gate_apply: ask returns wait with a request id")
    ok(pm.gate_apply(engine(with_mode("plan")), {"rel": "z.py"})[0] == "deny" and pm.gate_apply(engine(with_mode("auto")), {"rel": "z.py"})[0] == "allow",
       "gate_apply: plan denies, auto allows")


def run():
    N[0] = N[1] = 0
    tmp = tempfile.mkdtemp(prefix="harness3_st_")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_HOME", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")
    old_env = {k: os.environ.get(k) for k in keys}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    old_home = None
    try:
        import arch_core as core
        old_home = core.HOME
        core.set_home(os.path.join(tmp, "home"))
        print("-- auto-compaction")
        compact_checks()
        print("-- delegate")
        proj, cfg = None, None
        srv_cfg = delegate_checks(tmp)
        proj, cfg = srv_cfg
        print("-- permission modes")
        permission_checks(tmp, proj, cfg)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        ok(False, f"suite crashed: {type(ex).__name__}: {ex}")
    finally:
        try:
            import arch_core as core
            if old_home:
                core.set_home(old_home)
        except Exception:
            pass
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nharness3: {N[0]} passed, {N[1]} failed")
    return 0 if N[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
