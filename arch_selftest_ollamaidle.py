"""Self-test for Ollama resource hygiene: short keep_alive on every chat call, end-of-run unload of only this run's models, stopping only
an Ollama that Architect started, and the hub's /api/arch/ollama view + unload. Fake Ollama via monkeypatch; no model is ever loaded.

    python arch_selftest_ollamaidle.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(ok)          # three outcomes: True pass, False fail, None could not be told
        tag = "PASS" if ok is True else ("FAIL" if ok is False else "SKIP")
        print(f"  {tag}  {name}" + (f"   ({extra})" if ok is not True and extra else ""))

    tmp = tempfile.mkdtemp(prefix="ollamaidle_selftest_")
    old_env = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = tmp
    import arch_core as core
    import arch_transport as T
    import arch_ollama as O
    import arch_cleanup as C
    import arch_server
    import architect_hub as H
    saved = [(core, "load_config", core.load_config), (T, "route", T.route), (T.urllib.request, "urlopen", T.urllib.request.urlopen),
             (O, "unload", O.unload), (O, "status", O.status), (C, "_job_running", C._job_running), (C, "_our_models", C._our_models),
             (C, "_ollama", C._ollama), (C, "_is_ollama_pid", C._is_ollama_pid), (arch_server, "_alive", arch_server._alive),
             (arch_server, "_kill", arch_server._kill), (arch_server, "load_state", arch_server.load_state),
             (C, "_our_llama_pids", C._our_llama_pids), (C, "_known_roots", C._known_roots), (C, "_slots_busy", C._slots_busy)]
    try:
        # 1. keep_alive: one setting, default 60s, sent on every Ollama chat call
        check("server.ollama_keep_alive defaults to 60s", core.DEFAULTS["server"].get("ollama_keep_alive") == "60s")
        import arch_params
        keys = [r[0] for grp in (arch_params.SETTINGS.values() if isinstance(arch_params.SETTINGS, dict) else [arch_params.SETTINGS])
                for r in (grp if isinstance(grp, (list, tuple)) else []) if isinstance(r, (list, tuple))]
        check("setting is listed in arch_params.SETTINGS", ("server.ollama_keep_alive" in keys) if keys else None,
              "could not read SETTINGS shape")
        cfg = core.deep_merge(core.DEFAULTS, {"server": {"ollama_keep_alive": "45s"}})
        core.load_config = lambda root=None: cfg
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            return _FakeResp(b'{"message":{"content":"ok"},"done":true,"eval_count":1}\n')
        T.urllib.request.urlopen = fake_urlopen
        T.OllamaTransport("http://127.0.0.1:11434").chat("moe:35b", [{"role": "user", "content": "x"}], {"num_gpu": 0}, timeout=30)
        check("an Ollama chat with no keep_alive sends the setting", sent and sent[-1].get("keep_alive") == "45s", str(sent[-1:]))
        T.route = lambda model, host=None, cfg=None: T.OllamaTransport("http://127.0.0.1:11434")
        core.stream_chat("moe:35b", [{"role": "user", "content": "x"}], timeout=30)
        check("stream_chat default sends the setting (not 30m)", sent[-1].get("keep_alive") == "45s", str(sent[-1]))
        src = open(core.__file__, encoding="utf-8").read()
        check("Engine chat calls pass the setting, not limits.keep_alive",
              'lim["keep_alive"]' not in src and src.count("self._ka(), timeouts()") == 4)

        # 2. end of run: unload only what THIS run loaded through Ollama, not while another job holds the lock
        unloaded = []
        O.unload = lambda m, host=None, timeout=20: unloaded.append(m) or True

        def route(model, host=None, cfg=None):
            return T.LlamaTransport("http://127.0.0.1:8082", 8192) if model == "tier:worker" else T.OllamaTransport("http://127.0.0.1:11434")
        T.route = route

        def engine():
            e = core.Engine.__new__(core.Engine)
            e.cfg, e._inflight, e.lines = cfg, __import__("collections").Counter(), []
            e.report = lambda *l: e.lines.extend(l)
            e.say = lambda *a, **k: None
            return e
        C._job_running = lambda: None
        e = engine()
        for m, h in (("moe:35b", None), ("tier:worker", None), ("remote:7b", "http://10.0.0.5:11434"), ("claude:opus", None)):
            e._note_ollama(m, h)
        check("only local-Ollama models are recorded", e.ollama_used == {"moe:35b"}, str(getattr(e, "ollama_used", None)))
        e._bring_back()                                  # run_flow's exception path calls this with no model
        check("end of run unloads this run's Ollama model", unloaded == ["moe:35b"], str(unloaded))
        check("foreign model other-app:7b never unloaded at run end", "other-app:7b" not in unloaded)
        unloaded.clear()
        e._release_ollama()
        check("a second finish unloads nothing more", unloaded == [])
        a, b = engine(), engine()
        a._note_ollama("moe:35b", None)
        b._note_ollama("moe:35b", None)
        a._release_ollama()
        check("a model another live run still uses is kept", unloaded == [], str(unloaded))
        b._release_ollama()
        check("...and freed when that run ends too", unloaded == ["moe:35b"], str(unloaded))
        unloaded.clear()
        C._job_running = lambda: {"job": "eval", "pid": 4242}
        c = engine()
        c._note_ollama("moe:35b", None)
        c._release_ollama()
        check("nothing unloaded while another job holds the lock", unloaded == [], str(unloaded))
        C._job_running = lambda: None

        # 3. Ollama service: stopped on exit only when Architect started it, and only once nothing else is loaded
        killed = []
        arch_server._kill = lambda pid: killed.append(pid)
        arch_server._alive = lambda pid: pid in (777, 888)
        arch_server.load_state = lambda: {"processes": {}}
        C._our_llama_pids = lambda: []
        C._known_roots = lambda: []
        C._slots_busy = lambda: False
        C._our_models = lambda cfg=None: {"moe:35b"}
        C._is_ollama_pid = lambda pid: True
        ps = {"models": []}
        C._ollama = lambda path, body=None, timeout=10: (ps if path == "/api/ps" else unloaded.append(body["model"]) or {})
        p = C.plan(cfg)
        check("no marker: a person's own Ollama is never planned for stopping", not any(i["kind"] == "ollama_service" for i in p["items"]))
        O._mark_started(777, "ollama.exe")
        check("ensure_running's marker is written under the data dir", os.path.isfile(os.path.join(tmp, "ollama_started.json")))
        ps["models"] = [{"name": "other-app:7b"}]
        C.run(C.plan(cfg), yes=True, say=lambda m: None)
        check("foreign model keeps our Ollama running and is not unloaded", killed == [] and "other-app:7b" not in unloaded, f"{killed} {unloaded}")
        ps["models"] = []
        C.run(C.plan(cfg), yes=True, say=lambda m: None)
        check("our own Ollama is stopped once it holds nothing", killed == [777], str(killed))
        check("marker removed after stopping", O.started_by_us() is None)
        O._mark_started(999, "ollama.exe")               # pid no longer alive (or reused): never stopped
        killed.clear()
        C.run(C.plan(cfg), yes=True, say=lambda m: None)
        check("a dead/reused pid in the marker is never killed", killed == [], str(killed))
        O._mark_started(888, "ollama.exe")
        C._is_ollama_pid = lambda pid: False             # the pid now runs something else
        check("a pid that is not ollama any more is not planned", not any(i["kind"] == "ollama_service" for i in C.plan(cfg)["items"]))

        # 4. hub view and unload endpoint
        O.status = lambda host=None, timeout=3: {"up": True, "loaded": [
            {"name": "moe:35b", "size": 20 * 1024 ** 3, "size_vram": 0, "until": "2026-11-05T10:00:00Z"},
            {"name": "other-app:7b", "size": 4 * 1024 ** 3, "size_vram": 4 * 1024 ** 3, "until": None}]}
        v = O.loaded_view(cfg)
        check("view: running, ram_gb totals held memory", v["running"] is True and v["ram_gb"] == 24.0, str(v))
        mm = {m["name"]: m for m in v["models"]}
        check("view: ours flag and fields", mm["moe:35b"]["ours"] is True and mm["other-app:7b"]["ours"] is False
              and set(mm["moe:35b"]) == {"name", "size_gb", "vram_gb", "ours", "expires"}, str(mm))
        unloaded.clear()
        r = H.ollama_unload_request({"model": "moe:35b"})
        check("POST unload: ours is unloaded", r["unloaded"] == ["moe:35b"] and unloaded == ["moe:35b"], str(r))
        unloaded.clear()
        try:
            H.ollama_unload_request({"model": "other-app:7b"})
            refused = False
        except ValueError:
            refused = True
        check("POST unload: foreign model refused without all:true", refused and unloaded == [], str(unloaded))
        r = H.ollama_unload_request({"model": "other-app:7b", "all": True})
        check("POST unload: foreign model only with explicit all:true", unloaded == ["other-app:7b"], str(r))
        src = open(H.__file__, encoding="utf-8").read()
        check("hub routes GET /api/arch/ollama and POST /api/arch/ollama/unload",
              'u.path == "/api/arch/ollama"' in src and 'path == "/api/arch/ollama/unload"' in src)
    finally:
        for mod, name, val in saved:
            setattr(mod, name, val)
        try:
            O.clear_started()
        except Exception:
            pass
        if old_env is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [r for r in results if r is False]
    print(f"ollamaidle: {sum(1 for r in results if r is True)}/{len(results)} passed" + (f", {len(bad)} failed" if bad else ""))
    return not bad


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
