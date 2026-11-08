"""Self-test for the 2026-11-07 architecture review, wave 2 (docs/ADR-001): one truth per fact. Process identity (arch_proc: pid +
creation time, never a signal), the step-aside owner and the supervisor's task owner are recognised as the same process or not, ensure()
leaves a tier another live run stepped aside, one tier record is written at a time, plan() and start() agree on which Ollama models stay
on the card, the KV cache is planned as what the server will really use, AMD/Intel cards report their real memory, a YAML `tier:` no
longer switches the LAN machines off, a dead machine falls back to the role's own model, and a fleet agent's Engine refuses files it
does not own. No model, no GPU, no network."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archrev2_")
_KEYS = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST", "ARCHITECT_OWNS")
_OLD = {k: os.environ.get(k) for k in _KEYS}


def _child(code="import time; time.sleep(60)"):
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if os.name == "nt" else {}
    return subprocess.Popen([sys.executable, "-c", code], **kw)


def run():
    os.environ["ARCHITECT_HOME"] = os.path.join(_TMP, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "data")
    os.environ["ARCHITECT_NO_CACHE"] = os.environ["ARCHITECT_SELFTEST"] = "1"
    os.environ.pop("ARCHITECT_OWNS", None)
    for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR"):
        os.makedirs(os.environ[k], exist_ok=True)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    old_home = core.HOME
    core.set_home(os.environ["ARCHITECT_HOME"])
    procs = []
    try:
        # ---- arch_proc
        import arch_proc
        p = _child()
        procs.append(p)
        time.sleep(0.3)
        st0 = arch_proc.start_time(p.pid)
        check("alive(): a running child is alive, and asking does not stop it", arch_proc.alive(p.pid) is True and p.poll() is None)
        check("start_time(): a number, the same when asked twice", isinstance(st0, float) and arch_proc.start_time(p.pid) == st0, st0)
        check("same(): the recorded identity is still that process", arch_proc.same(p.pid, st0) is True)
        check("same(): a different creation time is a different process (a recycled pid)", arch_proc.same(p.pid, st0 - 1000) is False)
        d = _child("pass")
        d.wait()
        check("alive(): a process that ended is not alive", arch_proc.alive(d.pid) is False)
        check("alive(): no pid, zero or junk is not alive", arch_proc.alive(0) is False and arch_proc.alive(None) is False and arch_proc.alive("x") is False)
        check("identity(): pid and start together", arch_proc.identity(p.pid) == {"pid": p.pid, "start": st0})

        # ---- step-aside owners
        import arch_server as S
        check("_orphaned: an aside whose owner ended is orphaned", S._orphaned({"owner": d.pid, "owner_start": None}) is True)
        check("_orphaned: an aside whose owner is alive and the same process is not", S._orphaned({"owner": p.pid, "owner_start": st0}) is False)
        check("_orphaned: a live pid with another creation time is orphaned (the owner was killed and its pid reused)",
              S._orphaned({"owner": p.pid, "owner_start": st0 - 1000}) is True)
        check("_orphaned: an aside written by an older version (no start time) falls back to liveness", S._orphaned({"owner": p.pid}) is False)

        # ---- ensure() leaves a live aside alone
        calls = {"start": [], "back": 0}
        saved = (S.plan, S.status, S.start, S.come_back, S.load_state)
        S.plan = lambda cfg=None, hw=None, caps_=None: {"mode": "llama", "processes": [{"tier": "worker", "resident": True, "name": "w", "gguf": "w.gguf"}], "tiers": {}}
        S.status = lambda cfg=None: {"processes": [{"tier": "worker", "up": False}]}
        S.start = lambda cfg=None, tiers=None, **k: calls["start"].append(list(tiers or [])) or {"processes": []}
        S.come_back = lambda cfg=None, **k: calls.__setitem__("back", calls["back"] + 1)
        try:
            S.load_state = lambda: {"aside": {"worker": {"owner": p.pid, "owner_start": st0}}}
            S.ensure({}, say=lambda *a: None)
            check("ensure(): a tier another live run stepped aside is not started again behind its back", calls["start"] == [] and calls["back"] == 0, calls)
            S.load_state = lambda: {"aside": {"worker": {"owner": d.pid, "owner_start": None}}}
            S.ensure({}, say=lambda *a: None)
            check("ensure(): an orphaned aside is brought back and the tier started", calls["back"] == 1 and calls["start"] == [["worker"]], calls)
        finally:
            S.plan, S.status, S.start, S.come_back, S.load_state = saved

        # ---- one tier record at a time
        S.save_state({"processes": {"heavy": {"pid": 11}}})
        S._save_tier("worker", {"pid": 22})
        check("_save_tier writes one tier and keeps the others as they are now", set(S.load_state()["processes"]) == {"heavy", "worker"})
        S.save_state({"processes": {"heavy": {"pid": 11}}})                    # someone stopped the worker meanwhile
        check("_save_tier(only_pid): an update to a tier that was stopped meanwhile is not written back",
              S._save_tier("worker", {"pid": 22, "healthy": True}, only_pid=22) is False and "worker" not in S.load_state()["processes"])
        S.save_state({"processes": {"worker": {"pid": 22}}})
        check("_save_tier(only_pid): an update to the same running tier is written",
              S._save_tier("worker", {"pid": 22, "healthy": True}, only_pid=22) is True and S.load_state()["processes"]["worker"].get("healthy"))
        S.save_state({})

        # ---- plan() and start() agree on Ollama's models
        cfg_auto = {"behavior": {"exclusive_gpu": "auto"}, "roles": {"reviewer": "big:14b"}, "tiers": {"worker": "w:8b"}}
        check("ollama_keep (auto): the person's role models stay, what llama-server now serves goes",
              S.ollama_keep(cfg_auto, {"w:8b"}, {"w:8b", "big:14b", "other:3b"}) == {"big:14b"})
        check("ollama_keep (exclusive_gpu off): everything stays except the copy of what llama-server serves",
              S.ollama_keep({"behavior": {"exclusive_gpu": "off"}}, {"w:8b"}, {"w:8b", "big:14b", "other:3b"}) == {"big:14b", "other:3b"})
        src = open(os.path.join(HERE, "arch_server.py"), encoding="utf-8").read()
        check("plan() counts as freed only what start() will unload, through the same rule",
              src.count("ollama_keep(cfg,") >= 2 and "if m.get(\"name\") not in keep" in src)

        # ---- KV cache planned as what the server will use
        import arch_selftest_llama as L
        g = os.path.join(_TMP, "test-7b-Q4_K_M.gguf")
        L._gguf(g)
        stub = os.path.join(_TMP, "llama-server" + (".exe" if os.name == "nt" else ""))
        open(stub, "w").write("stub")
        cfg = core.load_config()
        cfg["tiers"] = {"worker": g, "router": "", "heavy": "", "draft": ""}
        cfg["server"]["binary"] = stub
        hw6 = {"gpu": "RTX 4050", "vram_gb": 6.0, "ram_gb": 16.0, "cpu_cores": 12, "vendor": "nvidia", "unified": False}
        full = {"flags": ["--host", "--port", "-m", "-c", "-ngl", "--alias", "-t", "--jinja", "--flash-attn", "-fa", "--cache-type-k", "--cache-type-v", "--parallel"], "fa_mode": "value"}
        bare = {"flags": ["--host", "--port", "-m", "-c", "-ngl", "--alias", "-t", "--parallel"], "fa_mode": "switch"}
        wf = next(x for x in S.plan(cfg, hw6, caps_=full)["processes"] if x["tier"] == "worker")
        pb = S.plan(cfg, hw6, caps_=bare)
        wb = next(x for x in pb["processes"] if x["tier"] == "worker")
        check("with the cache-type flags the worker's cache is planned and recorded as q8_0", wf["kv"] == "q8_0" and "--cache-type-k" in wf["args"], wf["kv"])
        check("without them it is planned and recorded as f16 (what the server really keeps), and the plan says why",
              wb["kv"] == "f16" and any("planned as f16" in n for n in pb["notes"]), (wb["kv"], pb["notes"]))
        nofa = core.deep_merge(cfg, {"server": {"flash_attn": False}})
        pn = S.plan(nofa, hw6, caps_=full)
        check("with Flash attention off in Settings the cache is f16 too, and the note names the setting",
              next(x for x in pn["processes"] if x["tier"] == "worker")["kv"] == "f16" and any("Flash attention is off" in n for n in pn["notes"]), pn["notes"])

        # ---- AMD / Intel memory
        import arch_hw
        out = json.dumps([{"Name": "AMD Radeon RX 7800 XT", "AdapterRAM": 4293918720, "MemorySize": 16 * 1024 ** 3},
                          {"Name": "Intel Arc A750", "AdapterRAM": 4293918720, "MemorySize": None}])
        gp = arch_hw._win_gpu(lambda cmd, timeout=8: (0, out)) or []
        check("a 16 GB Radeon reads 16 GB (the driver's 64-bit size), not the 4 GB cap of AdapterRAM",
              gp and gp[0]["vram_gb"] == 16.0 and gp[0]["vendor"] == "amd", gp)
        check("a card with no driver size still uses AdapterRAM", len(gp) == 2 and gp[1]["vram_gb"] == round(4293918720 / 1024 ** 3, 2), gp)

        # ---- Engine: tier pins keep the LAN route, escalation pins do not; failover model; owned files
        import arch_roles
        proj = os.path.join(_TMP, "proj")
        os.makedirs(proj, exist_ok=True)
        open(os.path.join(proj, "app.py"), "w").write("X = 1\n")
        ecfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}})
        e = core.Engine(proj, "build", "x", ecfg, review="auto", say=lambda *a: None)
        e.pins["generator"] = "fake:7b"
        e.tier_pins.add("generator")
        check("a role pinned by a step's ordinary tier: is not an escalation (its LAN machine still serves it)", e._escalated("generator") is False)
        e.tier_pins.discard("generator")
        check("a role pinned by an escalation runs locally", e._escalated("generator") is True)
        import arch_dataflow as df
        flow = df.DataFlow({"id": "rev2", "title": "t", "steps": [{"id": "a", "kind": "code", "gate": "scope_files"}], "verdict": {"pass_if": "True"}})
        e2 = core.Engine(proj, "build", "x", core.deep_merge(ecfg, {"roles": {"generator": ""}, "tiers": {"worker": "fake:7b"}}), review="auto", say=lambda *a: None)
        tok = flow._pin_tier(e2, "generator", "worker")
        check("_pin_tier pins the worker's model and marks it as a tier pin, not an escalation",
              tok and e2.pins.get("generator") == "fake:7b" and "generator" in e2.tier_pins and not e2._escalated("generator"), (tok, e2.pins, e2.tier_pins))
        flow._unpin(e2, "generator", tok)
        check("_unpin clears it again", "generator" not in e2.tier_pins)
        e.installed = lambda: ["nomic-embed-text:latest", "big:14b", "fake:7b"]
        check("a role whose machine died falls back to its OWN model, not the first installed tag (an embedding model here)",
              e._local_fallback("reviewer") == "fake:7b", e._local_fallback("reviewer"))
        e3 = core.Engine(proj, "build", "x", core.deep_merge(ecfg, {"roles": {"reviewer": ""}, "default_model": ""}), review="auto", say=lambda *a: None)
        e3._model_for = lambda role: None
        e3.installed = lambda: ["nomic-embed-text:latest", "qwen3:8b"]
        check("with no model for the role and no default, the first installed CHAT model is used", e3._local_fallback("reviewer") == "qwen3:8b")
        os.environ["ARCHITECT_OWNS"] = "app.py|lib/"
        try:
            eo = core.Engine(proj, "build", "x", ecfg, review="auto", say=lambda *a: None)
        finally:
            os.environ.pop("ARCHITECT_OWNS", None)
        r_bad = eo.apply("FILE: other.py\n```python\nY = 2\n```\n")
        check("a fleet agent's Engine refuses a file it does not own, and writes nothing",
              r_bad and r_bad[0]["status"] == "error" and "not owned" in r_bad[0]["msg"] and not os.path.exists(os.path.join(proj, "other.py")), r_bad)
        r_ok = eo.apply("FILE: lib/util.py\n```python\nZ = 3\n```\n")
        check("it writes a file inside a folder it owns", any(x.get("status") == "applied" for x in r_ok) and os.path.exists(os.path.join(proj, "lib", "util.py")), r_ok)
        check("put_file outside its files is refused", eo.put_file("notes.md", "x") is False and not os.path.exists(os.path.join(proj, "notes.md")))
        check("an Engine without an owned list writes anywhere (nothing changed for ordinary runs)", core.Engine(proj, "build", "x", ecfg, review="auto", say=lambda *a: None)._owned("any/where.py"))
        import arch_fleet
        check("the fleet passes each local agent its files", "ARCHITECT_OWNS=" in open(os.path.join(HERE, "arch_fleet.py"), encoding="utf-8").read())

        # ---- supervisor: a locked queue never kills the heartbeat
        import arch_supervisor as sv
        real_open = sv._open
        sv._open = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            ok = sv.beat(1)
        except Exception as ex:
            ok = ex
        finally:
            sv._open = real_open
        check("beat() on a locked queue skips one beat instead of raising (the heartbeat thread used to die)", ok is False, ok)
        jid = sv.add("check heartbeats", proj, "review")
        j = sv.claim()
        row = sv.get(j["id"])
        check("a claimed task records its supervisor (pid and creation time) and a first heartbeat",
              row.get("owner_pid") == os.getpid() and row.get("owner_start") and row.get("heartbeat"), row)
        check("recover() never takes a task whose supervisor is alive", sv.recover(stale_after=0) == 0 and sv.get(jid)["state"] == "running")

        # ================================================================ wave 2b
        # ---- the reply cache keeps machines apart
        import threading
        from http.server import ThreadingHTTPServer
        import arch_selftest
        import arch_cache
        fake = ThreadingHTTPServer(("127.0.0.1", 0), arch_selftest.Fake)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        old_host = os.environ.get("OLLAMA_HOST")
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{fake.server_address[1]}"
        os.environ.pop("ARCHITECT_NO_CACHE", None)
        try:
            routed = core.deep_merge(ecfg, {"role_hosts": {"reviewer": "desk"}, "hosts": {}})
            q1 = "a question asked while the desktop is named but not reachable"
            core.Engine(proj, "review", "a", routed, review="auto", say=lambda *a: None).call("reviewer", q1, temperature=0)
            rb = core.Engine(proj, "review", "b", routed, review="auto", say=lambda *a: None).call("reviewer", q1, temperature=0)
            check("a reply this computer gave for a role routed to another machine is not stored under that machine's key", not rb.get("cached"), rb.get("cached"))
            q2 = "a plain local question"
            core.Engine(proj, "review", "c", ecfg, review="auto", say=lambda *a: None).call("reviewer", q2, temperature=0)
            rd = core.Engine(proj, "review", "d", ecfg, review="auto", say=lambda *a: None).call("reviewer", q2, temperature=0)
            check("a role with no machine route still caches as before", rd.get("cached") is True, rd.get("cached"))
        finally:
            os.environ["ARCHITECT_NO_CACHE"] = "1"
            if old_host is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = old_host
            fake.shutdown()
        csrc = open(os.path.join(HERE, "arch_core.py"), encoding="utf-8").read()
        g0 = csrc.index("self.ctx_floor[model] = bigger")
        check("the bigger-window retry checks the budgets and is never stored under the small window's key",
              "self._check_budgets(model)" in csrc[g0:g0 + 700] and 'ck = ""' in csrc[g0:g0 + 700])
        check("reply-length learning records only this model's own finished or capped replies",
              'if model == ck_model and r.get("done_reason") in ("stop", "length")' in csrc)

        # ---- slot files with the model-fingerprint suffix are recognised
        import arch_cachehygiene as CH
        gone_root = os.path.join(_TMP, "deleted_project")
        fn = CH.slot_name(gone_root)[:-4] + "-0a1b2c3d.bin"
        kind = CH.owner_of(fn, {gone_root})[0]
        check("a slot file named <project>-<model fp>.bin whose project is gone is an orphan (it used to be 'unknown' forever)", kind == "orphan", kind)
        os.makedirs(gone_root, exist_ok=True)
        check("the same file is owned while its project exists", CH.owner_of(fn, {gone_root})[0] == "owned")

        # ---- the cache page and the CLI report the same savings
        import arch_cachestats as CS
        os.environ.pop("ARCHITECT_NO_CACHE", None)
        try:
            k = arch_cache.key("m:1", [{"role": "user", "content": "q"}], {"temperature": 0})
            arch_cache.put(k, "m:1", "answer", tokens_in=100, tokens_out=50, secs=2.0, cfg=ecfg)
            arch_cache.get(k, ecfg)
            dbp = os.path.join(core.data_dir(), "cache", "responses.db")
            c = sqlite3.connect(dbp)
            c.execute("delete from responses")              # trimmed: the row is gone, the saving was real
            c.commit()
            c.close()
            lay = CS.reply_layer(core.data_dir())
            st_ = arch_cache.stats()
            check("Models > Caches and `cache` report the same tokens saved, also after the row was trimmed",
                  lay["saved_tokens"] == st_["tokens_saved"] and lay["saved_tokens"] > 0, (lay["saved_tokens"], st_["tokens_saved"]))
        finally:
            os.environ["ARCHITECT_NO_CACHE"] = "1"

        # ---- crew step cache
        import arch_squad as SQ
        ssrc = open(os.path.join(HERE, "arch_squad.py"), encoding="utf-8").read()
        check("a crew step's cache key includes thinking, sampling, the model's overrides, the window and the tool text",
              all(x in ssrc for x in ('"thinking": b.get("thinking")', '"sampling": b.get("sampling")', '"override":', '"tools": _tools_sig()')))
        check("the tool signature is a real hash", len(SQ._tools_sig()) == 16, SQ._tools_sig())
        big = os.path.join(_TMP, "bigtree")
        os.makedirs(big, exist_ok=True)
        for i in range(6):
            with open(os.path.join(big, f"f{i}.py"), "w") as f:
                f.write(f"X = {i}\n")
        cap = SQ.FP_MAX_FILES
        SQ.FP_MAX_FILES = 3
        try:
            a1, a2 = SQ.files_fingerprint(big), SQ.files_fingerprint(big)
        finally:
            SQ.FP_MAX_FILES = cap
        check("a tree over the fingerprint cap says 'cannot tell' (never equal twice), instead of hashing only the first files",
              a1.startswith("over-cap:") and a1 != a2, a1)
        check("a tree under the cap still has a stable fingerprint", SQ.files_fingerprint(big) == SQ.files_fingerprint(big))
        import arch_plugins
        real = arch_plugins.agent_allowed
        arch_plugins.agent_allowed = lambda d, plugin, root=None: False
        try:
            check("a code: agent from an untrusted plugin is refused", SQ._code_allowed({"code": "spec", "plugin": "someone"}) is False)
        finally:
            arch_plugins.agent_allowed = real
        check("a shipped code: agent (no plugin) still runs", SQ._code_allowed({"code": "spec", "_source": os.path.join(HERE, "agents", "x.md")}) is True)
        check("several requests may go to one LAN machine at once (default 2, was a hard-coded 1)", "Semaphore(max(1, int(_mp)))" in ssrc)
        import arch_hosts
        check("the in-flight counter behind host ranking is changed under a lock", hasattr(arch_hosts, "_FL_LOCK") and "_FL_LOCK" in csrc)

        # ---- memory never keeps a bare secret
        import arch_memory as M
        bare = ["the key is sk-ant-api03-abcdefghijklmn", "use ghp_abcdefghijklmnop1234", "AKIAABCDEFGHIJKLMNOP", "Bearer abcdefghijklmnopqrstuvwx",
                "-----BEGIN RSA PRIVATE KEY-----"]
        check("memory refuses bare tokens, not only key=value", all(M.SECRET_RX.search(x) for x in bare), [x for x in bare if not M.SECRET_RX.search(x)])
        check("an ordinary lesson is not mistaken for a secret", not M.SECRET_RX.search("run pytest with -q; the token budget is 4000 tokens"))

        # ---- supervisor
        alts = sv.ALTERNATES.get("bugfix") or []
        f_, _w = sv.next_flow("bugfix", tried=[a for a, _ in alts] + ["bugfix"], current=(alts[-1][0] if alts else None))
        check("alternates stay anchored to the original flow (no retry of a retry's alternatives)", f_ is None or f_ in [a for a, _ in alts], f_)

        def boom(job, say):
            raise SystemExit("the heavy-job lock gave up waiting")
        sv.add("a task whose runner exits", proj, "review")
        try:
            r1, ok1 = sv.run_one(say=lambda *a: None, runner=boom, cfg=ecfg), True
        except BaseException as ex:
            r1, ok1 = ex, False
        check("a runner that raises SystemExit fails that task and keeps the supervisor alive", ok1, r1)

        def spent():
            c = sqlite3.connect(sv.db_path())
            v = c.execute("select coalesce(sum(runs),0) from spend").fetchone()[0]
            c.close()
            return v
        before = spent()
        sv.add("a task the machine cannot start", proj, "review")
        sv.run_one(say=lambda *a: None, cfg=ecfg,
                   runner=lambda job, say: {"verdict": "fail", "why": "connection refused: model server not reachable", "run_id": "", "flow": "review"})
        check("a task that could not really start (machine problem) is not charged to the daily budget", spent() == before, (before, spent()))

        # ---- server: what counts as ours
        real_img = S._pid_image
        try:
            S._pid_image = lambda pid: "ollama.exe"
            o1 = S._is_llama(1)
            S._pid_image = lambda pid: "llama-server.exe"
            o2 = S._is_llama(1)
        finally:
            S._pid_image = real_img
        check("a port held by ollama.exe is never taken for a llama-server of ours", o1 is False and o2 is True, (o1, o2))
        S._IDLE["seen"].clear()
        unloaded = []
        ps = json.dumps({"models": [{"name": "fake:7b", "size": 1}, {"name": "someone-elses:3b", "size": 1}]}).encode()

        def http(url, data=None):
            if url.endswith("/api/ps"):
                return ps
            if data:
                unloaded.append(json.loads(data)["model"])
            return b"{}"
        icfg = core.deep_merge(ecfg, {"server": {"idle_free_min": 1}})
        S.idle_sweep(icfg, now=1000.0, http=http)
        S.idle_sweep(icfg, now=1000.0 + 120, http=http)
        check("the idle sweeper unloads only models this app uses, never another program's", unloaded == ["fake:7b"], unloaded)
    finally:
        for x in procs:
            try:
                x.kill()
            except Exception:
                pass
        core.set_home(old_home)
        for k, v in _OLD.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' REVIEW2 CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' REVIEW2 CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
