"""Self-test for the cache extras (2026-11-05): chunk-level cache reuse and the static lookup cache in the server plan, role pre-warming,
the shared squad header, the within-run tool cache, the Claude cache TTL choice, the read-only model default and batch routing.
No GPU, no binary, no network, no Claude: a recording HTTP stand-in, stub binaries and fake clients."""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class Rec(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        d = json.dumps([{"id": 0, "is_processing": False}]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Rec.calls.append((self.path, b))
        if "action=save" in self.path:                       # the real server writes the file into --slot-save-path
            import arch_server as S
            with open(os.path.join(S.server_dir(), "slots", "worker", b["filename"]), "wb") as f:
                f.write(b"x" * 100)
        d = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)


class FakeBatches:
    def __init__(self):
        self.made = []

    def create(self, requests):
        self.made.append(requests)
        return types.SimpleNamespace(id="b1")


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_selftest_fastlane as FL
    import arch_server as S
    import arch_tools as T
    import arch_cloud
    import arch_batch
    import arch_squad as sq
    home = tempfile.mkdtemp(prefix="cacheplus_")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_EVAL", "ANTHROPIC_API_KEY")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in keys}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ.pop("ARCHITECT_EVAL", None)
    real = FL._patch(S)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        # ---- 2/3: the plan
        stub = os.path.join(home, "llama-server" + (".exe" if sys.platform.startswith("win") else ""))
        with open(stub, "w") as f:
            f.write("stub")
        S.save_state({})
        base = core.load_config()
        base["server"]["binary"] = stub
        base["tiers"] = {"router": "fr:1b", "worker": "fw:4b", "heavy": "", "draft": ""}
        caps = dict(FL.CAPS, flags=FL.CAPS["flags"] + ["--cache-reuse", "--lookup-cache-static"])
        pl = S.plan(base, dict(FL.HW), caps_=caps)
        w = next(p for p in pl["processes"] if p["tier"] == "worker")["args"]
        r = next(p for p in pl["processes"] if p["tier"] == "router")["args"]
        check("plan: --cache-reuse 256 for the worker when the build lists it, not for the router, and a plan note says so",
              w[w.index("--cache-reuse") + 1] == "256" and "--cache-reuse" not in r and any("cache reuse" in n for n in pl["notes"]), w)
        off = copy.deepcopy(base)
        off["server"]["cache_reuse"] = 0
        w0 = next(p for p in S.plan(off, dict(FL.HW), caps_=caps)["processes"] if p["tier"] == "worker")["args"]
        w1 = next(p for p in S.plan(base, dict(FL.HW), caps_=FL.CAPS)["processes"] if p["tier"] == "worker")["args"]
        check("plan: cache_reuse 0 or a build without the flag = no flag", "--cache-reuse" not in w0 and "--cache-reuse" not in w1)
        # static lookup: off by default; when on and a file was built it is passed with the n-gram cache
        proj = os.path.join(home, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "a.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        on = copy.deepcopy(base)
        on["server"]["static_lookup"] = "on"
        built = []
        bin_ = lambda cmd: (built.append(cmd), open(cmd[cmd.index("-lcs") + 1], "wb").write(b"cache"))
        S.caps = lambda b: caps                                    # the stub binary has no --help
        got = S.refresh_static_lookup(on, proj, gguf="m.gguf", say=lambda m: None, runner=bin_)
        again = S.refresh_static_lookup(on, proj, gguf="m.gguf", say=lambda m: None, runner=bin_)
        with open(os.path.join(proj, "b.py"), "w") as f:
            f.write("x = 1\n")
        S.refresh_static_lookup(on, proj, gguf="m.gguf", say=lambda m: None, runner=bin_)
        check("static lookup: built from the project's sources, kept while the tree is unchanged, rebuilt when it changes",
              got and got == again and len(built) == 2, (got, len(built)))
        wl = next(p for p in S.plan(on, dict(FL.HW), caps_=caps)["processes"] if p["tier"] == "worker")["args"]
        wo = next(p for p in S.plan(base, dict(FL.HW), caps_=caps)["processes"] if p["tier"] == "worker")["args"]
        check("static lookup: passed to the worker only when the setting is on", "--lookup-cache-static" in wl and "--lookup-cache-static" not in wo, wl)

        # ---- 1: pre-warm
        Rec.calls.clear()
        cfg = copy.deepcopy(base)
        cfg["server"].update(prewarm="on", prewarm_roles=["generator", "reviewer", "nonexistent_role"], prewarm_max=2)
        os.makedirs(os.path.join(S.server_dir(), "slots", "worker"), exist_ok=True)
        os.environ["ARCHITECT_EVAL"] = "1"
        skipped = S.prewarm_roles(cfg, url=url, say=lambda m: None) + S.prewarm_roles(cfg, url=url, say=lambda m: None, force=False)
        os.environ.pop("ARCHITECT_EVAL")
        skipped += S.prewarm_roles(cfg, url=url, say=lambda m: None)           # ARCHITECT_NO_CACHE is set: the self-test case
        done = S.prewarm_roles(cfg, url=url, say=lambda m: None, force=True)
        saves = [b["filename"] for p, b in Rec.calls if "action=save" in p]
        chats = [b for p, b in Rec.calls if p.endswith("/chat/completions")]
        check("prewarm: skipped in eval; else one request and one slot save per role (capped, unknown roles dropped), system prompt first",
              skipped == [] and done == ["generator", "reviewer"] and len(saves) == 2 and len(chats) == 2
              and chats[0]["messages"][0]["role"] == "system" and chats[0]["max_tokens"] == 1, (done, saves))
        again = S.prewarm_roles(cfg, url=url, say=lambda m: None, force=True)
        check("prewarm: a role already saved is not warmed again", again == [])
        Rec.calls.clear()
        ok_r = S.restore_role(cfg, "generator", url=url)
        miss = S.restore_role(cfg, "debugger", url=url)
        check("prewarm: the role's first call restores its saved slot; a role never warmed restores nothing",
              ok_r and not miss and any("action=restore" in p for p, _ in Rec.calls))
        capped = copy.deepcopy(cfg)
        capped["server"].update(prewarm_gb=0.0000001, prewarm_roles=["tester"])
        check("prewarm: the disk cap stops it", S.prewarm_roles(capped, url=url, say=lambda m: None, force=True) == [])

        # ---- 4: shared squad header
        dd = os.path.join(home, "data")
        c = sq.Crew(proj, "build the thing", cfg=cfg, data_dir=dd, workflow="hdr")
        defs = sq.list_agents()
        sysm = []
        for nm in ("auditor", "verifier", "builder"):
            s_, b_ = c._brief(defs[nm], f"task for {nm}", ["a.py"] if nm == "builder" else [], None, None)
            sysm.append(s_)
        hdr = c.shared_header()
        check("squad: every agent's system prompt starts with the same byte-identical header; its own text follows",
              hdr and all(s.encode().startswith(hdr.encode()) for s in sysm) and len(set(sysm)) == 3
              and all(s[len(hdr):].strip() for s in sysm), hdr[:80])
        cfg2 = copy.deepcopy(cfg)
        cfg2["squad"] = {"shared_prefix": "off"}
        c2 = sq.Crew(proj, "g", cfg=cfg2, data_dir=dd, workflow="hdr2")
        check("squad: shared_prefix off = no header", c2.shared_header() == "" and not c2._brief(defs["auditor"], "t", [], None, None)[0].startswith("[crew team"))

        # ---- 5: tool cache
        e = types.SimpleNamespace(root=proj, cfg=core.load_config(proj))
        T.call(e, "ls", {})
        T.call(e, "ls", {})
        T.call(e, "grep", {"pattern": "add"})
        T.call(e, "grep", {"pattern": "add"})
        T.call(e, "read", {"path": "a.py"})
        T.call(e, "read", {"path": "a.py", "start": 1, "end": 1})
        T.call(e, "outline", {"path": "a.py"})
        T.call(e, "outline", {"path": "a.py"})
        st = T.cache_stats(e)
        check("tool cache: ls/grep/outline repeats and file re-reads are hits, first calls misses", st["hit"] >= 4 and st["miss"] >= 4, st)
        old_apply = T.TOOLS["apply"]["fn"]

        def fake_apply(e_, blocks="", path="a.py", **_):
            with open(os.path.join(e_.root, path), "w") as f:
                f.write("def add(a, b):\n    return a - b\n# changed\n")
            return "applied"
        T.TOOLS["apply"]["fn"] = fake_apply
        try:
            T.call(e, "apply", {"blocks": "x", "path": "a.py"})
        finally:
            T.TOOLS["apply"]["fn"] = old_apply
        h0 = T.cache_stats(e)["hit"]
        out, _ = T.call(e, "grep", {"pattern": "changed"})
        rd, _ = T.call(e, "read", {"path": "a.py", "start": 3, "end": 3})
        check("tool cache: an apply invalidates -> the next grep/read sees the new text (no stale hit)",
              "changed" in out and "changed" in rd and T.cache_stats(e)["hit"] == h0 and T.cache_stats(e)["inval"] >= 1, (out, rd))
        e2 = types.SimpleNamespace(root=proj, cfg=core.deep_merge(core.load_config(proj), {"behavior": {"tool_cache": "off"}}))
        T.call(e2, "ls", {})
        T.call(e2, "ls", {})
        check("tool cache: behavior.tool_cache = off caches nothing", T.cache_stats(e2)["hit"] == 0)

        # ---- 6: TTL
        on_ = lambda v: {"online": {"cloud_cache_ttl": v}}
        check("ttl: auto = 1h for orchestrator/session prompts, 5m for short agents; forced values win; a reuse gap decides when known",
              arch_cloud.choose_ttl(on_("auto"), "orchestrator") == "1h" and arch_cloud.choose_ttl(on_("auto"), "agent") == "5m"
              and arch_cloud.choose_ttl(on_("5m"), "session") == "5m" and arch_cloud.choose_ttl(on_("1h"), "agent") == "1h"
              and arch_cloud.choose_ttl({}, "session") == "1h" and arch_cloud.choose_ttl(on_("auto"), "agent", gap_s=900) == "1h"
              and arch_cloud.choose_ttl(on_("auto"), "session", gap_s=60) == "5m")
        check("ttl: price accounting follows the lifetime (a 1h write is 2x input, 5m 1.25x)",
              abs(arch_cloud.cost("claude:claude-sonnet-5-5", 0, 0, 0, 1_000_000, "1h") - 4.0) < 1e-9
              and abs(arch_cloud.cost("claude:claude-sonnet-5-5", 0, 0, 0, 1_000_000, "5m") - 2.5) < 1e-9)

        # ---- 7: read-only model
        c_ = {"online": {}}
        check("readonly model: auto = Sonnet for read-only, Haiku for a tiny lookup, unchanged for writers; a named model is never changed; same = off",
              arch_cloud.readonly_model(c_, None, True)[0] == "claude-sonnet-5-5" and arch_cloud.readonly_model(c_, None, True, tiny=True)[0] == "claude-haiku-5-5"
              and arch_cloud.readonly_model(c_, None, False)[0] is None and arch_cloud.readonly_model(c_, "claude-opus-5-5", True)[0] == "claude-opus-5-5"
              and arch_cloud.readonly_model({"online": {"readonly_model": "same"}}, None, True)[0] is None)
        import arch_fleet
        plan = [{"name": "look", "brief": "list the modules", "files": ["notes.md"], "budget_usd": 0.2, "mode": "plan"},
                {"name": "writer", "brief": "x" * 800, "files": ["a.py"], "budget_usd": 1.0},
                {"name": "named", "brief": "y", "files": ["other.md"], "budget_usd": 0.2, "mode": "plan", "model": "claude-opus-5-5"}]
        started = []
        arch_fleet.launch(plan, proj, 5.0, tests=[], starter=lambda **kw: started.append(kw) or "r%d" % len(started),
                          runner=lambda cmd, cwd, timeout: {"rc": None, "output": "no git"}, poll=0.05, max_wait_s=1)
        check("fleet: a read-only agent gets Haiku (tiny) or Sonnet, a writer keeps the default, a named model is kept",
              [k["model"] for k in started] == ["claude-haiku-5-5", None, "claude-opus-5-5"], [k["model"] for k in started])

        # ---- 8: batch routing
        fb = FakeBatches()
        client = types.SimpleNamespace(messages=types.SimpleNamespace(batches=fb))
        items = [{"id": "audit1", "prompt": "audit", "model": "claude-sonnet-5-5"}]
        bcfg = {"online": {"enabled": True, "batch_jobs": "on"}}
        os.environ["ANTHROPIC_API_KEY"] = "test"
        real_allowed = arch_cloud.allowed
        arch_cloud.allowed = lambda cfg: (True, "")                 # the gate is false inside self-tests; the fake client makes no call
        try:
            r1 = arch_batch.route(items, bcfg, client=client)
            r2 = arch_batch.route(items, bcfg, urgent=True, client=client)
            r3 = arch_batch.route(items, {"online": {"enabled": True}}, client=client)
            r4 = arch_batch.route(items, {"online": {"enabled": False, "batch_jobs": "on"}}, client=client)
            os.environ.pop("ANTHROPIC_API_KEY")
            r5 = arch_batch.route(items, bcfg, client=client)
        finally:
            arch_cloud.allowed = real_allowed
        check("batch: opt-in, online on, key present, not urgent -> one batch; any other case runs now; the fake client was used once",
              r1 == {"via": "batch", "id": "b1"} and all(x["via"] == "now" for x in (r2, r3, r4, r5)) and len(fb.made) == 1, (r1, r2, r3, r4, r5))
    finally:
        srv.shutdown()
        FL._unpatch(S, real)
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CACHE-EXTRAS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CACHE-EXTRAS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
