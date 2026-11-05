"""Close what the app started, so the next run begins on a free machine.

What it closes (only things that are ours):
  servers   our llama-server tiers (router, worker, heavy) recorded in the server state
  strays    llama-server processes from OUR bin/llama folder that the state no longer knows (a crash, a killed hub)
  ollama    models Ollama is holding in memory (unloaded with keep_alive 0; the Ollama service itself keeps running)
  launch    projects started from the Launch page that are still running (arch_runner)
  temp      throwaway eval folders (%TEMP%/eval_*) older than two hours that still carry our .arch marker
It never touches a run in progress: while a heavy job (eval, selftest, livetest, tune, self-improve) holds the job lock in another
live process, servers and Ollama models are left alone. Nothing else on the machine is touched.

    python architect.py cleanup          # what would be closed
    python architect.py cleanup --yes    # close it
The hub runs the same thing when it exits (behavior.close_on_exit = auto; off keeps everything running).
"""
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
TEMP_AGE_S = 2 * 3600


def _job_running():
    try:
        import arch_heavy
        import arch_server
        h = arch_heavy.holder() or {}
        pid = int(h.get("pid") or 0)
        return h if pid and pid != os.getpid() and arch_server._alive(pid) else None
    except Exception:
        return None


def _ollama(path, body=None, timeout=10):
    host = OLLAMA if OLLAMA.startswith("http") else "http://" + OLLAMA
    if not ("127.0.0.1" in host or "localhost" in host):
        return None                                       # only a local Ollama
    req = urllib.request.Request(host.rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _our_llama_pids():
    """PIDs of llama-server.exe processes started from this install's bin folder (Windows; elsewhere: none)."""
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name = 'llama-server.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    mine = os.path.normcase(os.path.join(HERE, "bin"))
    pids = []
    for line in out.splitlines():
        pid, _, exe = line.partition("|")
        if pid.strip().isdigit() and os.path.normcase(exe.strip()).startswith(mine):
            pids.append(int(pid))
    return pids


def plan(cfg=None, roots=None):
    """-> {"busy": job or None, "items": [{"kind", "what", "how"}]}. Reads only."""
    import arch_server
    items = []
    busy = _job_running()
    st = arch_server.load_state()
    tracked = set()
    for t, rec in (st.get("processes") or {}).items():
        if rec.get("pid") and arch_server._alive(rec["pid"]):
            tracked.add(int(rec["pid"]))
            items.append({"kind": "servers", "what": f"model server '{t}' (pid {rec['pid']}, port {rec.get('port')})", "how": "stop"})
    for pid in _our_llama_pids():
        if pid not in tracked:
            items.append({"kind": "strays", "what": f"llama-server pid {pid} that no record knows", "how": "kill", "pid": pid})
    try:
        for m in (_ollama("/api/ps") or {}).get("models") or []:
            items.append({"kind": "ollama", "what": f"Ollama is holding {m.get('name')} in memory", "how": "unload", "model": m.get("name")})
    except Exception:
        pass
    try:
        import arch_runner
        for root in roots or _known_roots():
            s = arch_runner._load(root) or {}
            if s.get("pid") and arch_runner.is_running(s["pid"]):
                items.append({"kind": "launch", "what": f"project started from Launch: {root} (pid {s['pid']})", "how": "stop", "root": root})
    except Exception:
        pass
    now = time.time()
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "eval_*")):
        try:
            if os.path.isdir(os.path.join(d, ".arch")) and now - os.path.getmtime(d) > TEMP_AGE_S:
                items.append({"kind": "temp", "what": f"throwaway eval folder {os.path.basename(d)}", "how": "delete", "path": d})
        except OSError:
            continue
    if busy:
        for it in items:
            if it["kind"] in ("servers", "strays", "ollama"):
                it["how"] = "keep"
                it["why"] = f"a {busy.get('job')} run (pid {busy.get('pid')}) is using it"
    return {"busy": busy, "items": items}


def _known_roots():
    roots = set()
    try:
        import arch_core
        pf = arch_core.load_json(os.path.join(arch_core.HOME, "portfolio.json")) or {}
        for a in pf.get("apps") or []:
            if isinstance(a, dict) and a.get("path"):
                roots.add(a["path"])
    except Exception:
        pass
    roots.add(HERE)
    return [r for r in roots if os.path.isdir(r)]


def run(p, yes=False, say=print):
    """Apply a plan. Without yes, nothing happens. -> list of what was done."""
    if not yes:
        return []
    import arch_server
    done = []
    if any(it["kind"] == "servers" and it["how"] == "stop" for it in p["items"]):
        arch_server.stop(say=lambda m: None)
        done.append("stopped our model servers")
    for it in p["items"]:
        try:
            if it["how"] == "kill" and it["kind"] == "strays":
                arch_server._kill(it["pid"])
                done.append(f"stopped stray llama-server pid {it['pid']}")
            elif it["how"] == "unload":
                _ollama("/api/generate", {"model": it["model"], "keep_alive": 0}, timeout=30)
                done.append(f"unloaded {it['model']} from Ollama")
            elif it["how"] == "stop" and it["kind"] == "launch":
                import arch_runner
                arch_runner.stop(it["root"])
                done.append(f"stopped the Launch run in {it['root']}")
            elif it["how"] == "delete" and it["kind"] == "temp":
                tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
                if os.path.normcase(os.path.abspath(it["path"])).startswith(tmp) and os.path.basename(it["path"]).startswith("eval_"):
                    shutil.rmtree(it["path"], ignore_errors=True)
                    done.append(f"deleted {os.path.basename(it['path'])}")
        except Exception as ex:
            done.append(f"could not {it['how']} {it['what']}: {ex}")
    for d in done:
        say("[cleanup] " + d)
    return done


def report(p):
    if not p["items"]:
        return "Nothing to close: no model server, Ollama model, Launch run or leftover eval folder."
    lines = [f"  {it['how']:<7} {it['what']}" + (f"  ({it['why']})" if it.get("why") else "") for it in p["items"]]
    head = "What the app has running or left behind:" + (f"\n  (a {p['busy'].get('job')} run is in progress: its servers are kept)" if p["busy"] else "")
    return head + "\n" + "\n".join(lines)


_HOOKED = {"on": False}


def install_exit_hook(load_cfg, say=print):
    """Run the cleanup when the hub exits: normal exit, Ctrl+C, and closing its console window (Windows gives a few seconds)."""
    if _HOOKED["on"]:
        return
    _HOOKED["on"] = True
    state = {"done": False}

    def go(*_a):
        if state["done"]:
            return True
        state["done"] = True
        try:
            cfg = load_cfg()
            if str((cfg.get("behavior") or {}).get("close_on_exit", "auto")) != "off":
                run(plan(cfg), yes=True, say=say)
        except Exception:
            pass
        return False
    import atexit
    atexit.register(go)
    if os.name == "nt":
        try:
            import ctypes
            HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def handler(event):
                if event in (0, 1, 2, 5, 6):               # Ctrl+C, Ctrl+Break, console closed, log off, shut down
                    go()
                return False
            _HOOKED["handler"] = HANDLER(handler)          # keep a reference: the OS calls it later
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_HOOKED["handler"], True)
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    p = plan()
    print(report(p))
    if "--yes" in sys.argv:
        run(p, yes=True)
