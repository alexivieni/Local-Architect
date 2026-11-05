"""Self-test for arch_cleanup: close what the app started, never what a running job uses, never anything that is not ours.

    python arch_selftest_cleanup.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    import arch_cleanup as C
    import arch_server
    import arch_heavy
    saved = (arch_server.load_state, arch_server._alive, arch_server._kill, arch_server.stop, C._our_llama_pids, C._ollama,
             arch_heavy.holder, C._known_roots)
    calls = {"stop": 0, "kill": [], "unload": []}
    tmp = tempfile.mkdtemp(prefix="cleanup_selftest_")
    try:
        arch_server.load_state = lambda: {"processes": {"worker": {"pid": 101, "port": 8082}, "router": {"pid": 102, "port": 8081}}}
        arch_server._alive = lambda pid: pid in (101, 102, 999)
        arch_server._kill = lambda pid: calls["kill"].append(pid)
        arch_server.stop = lambda *a, **k: calls.__setitem__("stop", calls["stop"] + 1)
        C._our_llama_pids = lambda: [101, 102, 555]
        C._ollama = lambda path, body=None, timeout=10: ({"models": [{"name": "moe:35b"}]} if path == "/api/ps"
                                                         else calls["unload"].append(body["model"]) or {})
        C._known_roots = lambda: []
        arch_heavy.holder = lambda: None
        real_temp = tempfile.gettempdir
        tempfile.gettempdir = lambda: tmp
        old = os.path.join(tmp, "eval_build_old")
        os.makedirs(os.path.join(old, ".arch"))
        fresh = os.path.join(tmp, "eval_build_new")
        os.makedirs(os.path.join(fresh, ".arch"))
        nomark = os.path.join(tmp, "eval_notours")
        os.makedirs(nomark)
        other = os.path.join(tmp, "user_stuff")
        os.makedirs(other)
        t = time.time() - 3 * 3600
        for d in (old, nomark, other):
            os.utime(d, (t, t))
        try:
            p = C.plan()
            kinds = sorted({(it["kind"], it["how"]) for it in p["items"]})
            check("the plan lists our servers, a stray llama-server, Ollama's model and an old marked eval folder",
                  ("servers", "stop") in kinds and ("strays", "kill") in kinds and ("ollama", "unload") in kinds and ("temp", "delete") in kinds, kinds)
            temps = [it["path"] for it in p["items"] if it["kind"] == "temp"]
            check("only old eval_* folders that carry our .arch marker are offered (not new ones, not unmarked, not other folders)",
                  temps == [old], temps)
            check("without yes nothing happens", C.run(p, yes=False) == [] and calls["stop"] == 0 and os.path.isdir(old))
            done = C.run(p, yes=True, say=lambda m: None)
            check("with yes: servers stopped, the stray killed (tracked ones are left to stop()), Ollama unloaded, the folder deleted",
                  calls["stop"] == 1 and calls["kill"] == [555] and calls["unload"] == ["moe:35b"] and not os.path.isdir(old)
                  and os.path.isdir(fresh) and os.path.isdir(nomark) and os.path.isdir(other), (done, calls))
            arch_heavy.holder = lambda: {"job": "eval", "pid": 999}
            calls.update(stop=0, kill=[], unload=[])
            p2 = C.plan()
            C.run(p2, yes=True, say=lambda m: None)
            check("while an eval holds the job lock in another live process, its servers and Ollama models are kept",
                  p2["busy"] and calls["stop"] == 0 and calls["kill"] == [] and calls["unload"] == []
                  and all(it["how"] == "keep" for it in p2["items"] if it["kind"] in ("servers", "strays", "ollama")))
            arch_heavy.holder = lambda: None
            check("a temp path outside the temp folder or not named eval_* is never deleted, even if handed in",
                  C.run({"busy": None, "items": [{"kind": "temp", "how": "delete", "what": "x", "path": other}]}, yes=True, say=lambda m: None) == []
                  and os.path.isdir(other))
            hooked = []
            C._HOOKED["on"] = False
            real_run = C.run
            C.run = lambda p, yes=False, say=print: hooked.append(yes) or []
            C.install_exit_hook(lambda: {"behavior": {"close_on_exit": "auto"}}, say=lambda m: None)
            import atexit
            atexit._run_exitfuncs()
            atexit._run_exitfuncs()
            check("the exit hook runs the cleanup once (atexit), with yes", hooked == [True], hooked)
            C._HOOKED["on"] = False
            hooked.clear()
            C.install_exit_hook(lambda: {"behavior": {"close_on_exit": "off"}}, say=lambda m: None)
            atexit._run_exitfuncs()
            check("close_on_exit = off leaves everything running", hooked == [], hooked)
            C.run = real_run
        finally:
            tempfile.gettempdir = real_temp
    finally:
        (arch_server.load_state, arch_server._alive, arch_server._kill, arch_server.stop, C._our_llama_pids, C._ollama,
         arch_heavy.holder, C._known_roots) = saved
        shutil.rmtree(tmp, ignore_errors=True)
    import arch_core
    check("the setting exists in DEFAULTS and the schema; the hub installs the hook; start clears strays",
          arch_core.DEFAULTS["behavior"].get("close_on_exit") == "auto"
          and "behavior.close_on_exit" in open(os.path.join(os.path.dirname(arch_core.__file__), "arch_params.py"), encoding="utf-8").read()
          and "arch_cleanup.install_exit_hook" in open(os.path.join(os.path.dirname(arch_core.__file__), "architect_hub.py"), encoding="utf-8").read()
          and 'it["kind"] in ("strays", "temp")' in open(os.path.join(os.path.dirname(arch_core.__file__), "architect.py"), encoding="utf-8").read())
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CLEANUP CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CLEANUP CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
