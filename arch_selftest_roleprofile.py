"""Self-test for arch_roleprofile: precedence, off = unchanged, caps applied and traced, tool refusal, presets apply/undo.

    py -3.12 arch_selftest_roleprofile.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    tmp = tempfile.mkdtemp(prefix="roleprofile_")
    saved = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    import arch_core as core
    import arch_roles
    import arch_roleprofile as rp
    old_home, real_sc = core.HOME, core.stream_chat
    core.set_home(os.path.join(tmp, "_home"))
    os.makedirs(core.HOME, exist_ok=True)
    seen = []

    def fake(model, msgs, options, th, ka, to, fmt, host=None, **k):
        seen.append((model, json.loads(json.dumps(msgs)), dict(options), th))
        return {"text": "ok", "thinking": "", "tok_s": 10.0, "gen_tokens": 2, "prompt_tokens": 10, "cached_tokens": 0,
                "done_reason": "stop", "prefill_tok_s": None, "load_s": 0.0, "transport": "fake"}

    def engine(**over):
        root = tempfile.mkdtemp(dir=tmp)
        c = core.load_config(root)
        c["roles"] = {r: "fake:7b" for r in arch_roles.ROLES}
        c["behavior"]["battery_saver"] = "off"
        c["behavior"]["learn_from_runs"] = "off"
        c = core.deep_merge(c, over)
        return core.Engine(root, "eval", "x", c, review="auto", say=lambda m: None, persist=False)

    def traces(e):
        p = os.path.join(e.run_dir, "trace.jsonl")
        try:
            return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
        except OSError:
            return []

    core.stream_chat = fake
    try:
        big = "=== a.py\n" + ("x = 1\n" * 4000) + "=== b.py\n" + ("y = 2\n" * 4000)
        # --- off = unchanged
        e0 = engine()
        e0._contexts = [big]
        e0.call("planner", "PLAN:\n" + big)
        e1 = engine(behavior={"role_profiles": "off"}, role_profiles={"planner": {"reply_max": 20, "thinking": "off"}})
        e1._contexts = [big]
        e1.call("planner", "PLAN:\n" + big)
        check("off: identical request (messages, options, thinking) even with a person's profile set", seen[0] == seen[1])
        check("off: no profile key in the trace", all("profile" not in r for r in traces(e1) if r.get("type") == "call"))
        # --- shipped profile on
        seen.clear()
        e2 = engine(behavior={"role_profiles": "on"})
        e2._contexts = [big]
        e2.call("router", "ROUTE:\n" + big)
        m, msgs, opt, th = seen[-1]
        check("router: reply capped to 60, thinking off, temperature 0", opt["num_predict"] == 60 and th is False and opt["temperature"] == 0.0, opt)
        check("router: the prompt was cut to its 2048-token ctx_max (cut_context)", len(msgs[-1]["content"]) < len(big) // 2,
              len(msgs[-1]["content"]))
        tr = [r for r in traces(e2) if r.get("type") == "call"][-1].get("profile") or {}
        check("trace: profile {role, applied, source}", tr.get("role") == "router" and tr["applied"].get("reply_max") == 60
              and tr["source"].get("ctx_max") == "shipped", tr)
        # --- precedence
        seen.clear()
        e3 = engine(behavior={"role_profiles": "on"}, role_profiles={"router": {"reply_max": 40}})
        e3.call("router", "a")
        e3.call("router", "b", num_predict=200, temperature=0.5, think=True)
        check("person > shipped", seen[0][2]["num_predict"] == 40)
        check("explicit call args > person profile", seen[1][2]["num_predict"] == 200 and seen[1][2]["temperature"] == 0.5 and seen[1][3] is True)
        tr = [r for r in traces(e3) if r.get("type") == "call"]
        check("trace names the source of each key and what an argument kept", tr[0]["profile"]["source"]["reply_max"] == "person"
              and tr[1]["profile"]["kept"].get("reply_max") == "call argument", tr[1].get("profile"))
        seen.clear()
        e4 = engine(behavior={"role_profiles": "on"}, role_overrides={"router": {"sampling": {"temperature": 0.7}}})
        e4.call("router", "c")
        check("your older role override beats a SHIPPED profile value", seen[0][2]["temperature"] == 0.7, seen[0][2])
        # --- budget
        e5 = engine(behavior={"role_profiles": "on"}, role_profiles={"router": {"budget": {"calls": 1}}})
        e5.call("router", "d")
        try:
            e5.call("router", "e")
            check("per-run role budget stops the next call", False)
        except core.BudgetExceeded:
            check("per-run role budget stops the next call", True)
        # --- tools
        cfg = {"behavior": {"role_profiles": "on"}}
        al, gone = rp.tool_allow(cfg, "planner", ["ls", "read", "apply"])
        check("planner tool loop: writing tools removed", al == ["ls", "read"] and gone == ["apply"])
        check("tester may apply to tests only", rp.check_tool(cfg, "tester", "apply", {"blocks": "FILE: tests/test_a.py\n"})[0]
              and not rp.check_tool(cfg, "tester", "apply", {"blocks": "FILE: app.py\n"})[0])
        check("tools unchanged when off", rp.tool_allow({}, "planner", ["apply"]) == (["apply"], []) and rp.check_tool({}, "planner", "apply", {})[0])
        # --- presets apply / undo
        import model_hub as mh
        mh.CONFIG_FILE = core.CONFIG_FILE
        mh.write_config({"role_profiles": {"router": {"reply_max": 33}}})
        check("four presets ship", set(rp.presets()) >= {"fast", "balanced", "quality", "battery"})
        rp.apply_preset("Fast")
        raw = mh.read_config()
        check("preset Fast: profiles + settings written", raw["behavior"]["role_profiles"] == "on" and raw["server"]["fast_lane"] == "on"
              and raw["role_profiles"]["planner"]["thinking"] == "off", raw)
        rp.apply_preset("balanced")
        check("preset Balanced = shipped (no person values)", mh.read_config()["role_profiles"] == {})
        rp.undo()
        rp.undo()
        raw = mh.read_config()
        check("undo twice puts everything back", raw.get("role_profiles") == {"router": {"reply_max": 33}}
              and "server" not in raw and "role_profiles" not in (raw.get("behavior") or {}), raw)
        try:
            rp.write("bad", role_profiles={"router": {"thinking": "maybe"}})
            check("a bad profile value is refused", False)
        except ValueError:
            check("a bad profile value is refused", True)
    finally:
        core.stream_chat = real_sc
        core.set_home(old_home)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"{'ALL' if n == len(results) else n} {len(results)} ROLEPROFILE CHECKS {'PASSED' if n == len(results) else 'PASSED (some FAILED)'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
