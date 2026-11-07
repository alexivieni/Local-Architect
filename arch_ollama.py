"""One place for talking to the Ollama SERVER itself (not to a model): is it up, on which port, what is loaded, free the GPU, warm the builder.

"Closing other sessions" means unloading models that are sitting in memory but are not part of your team: Ollama keeps the last models resident
for `keep_alive`, and two resident models on one GPU is the quiet reason a run is slow or spills to the CPU. Unloading is Ollama's own mechanism
(a request with keep_alive 0); nothing is killed and no other program is touched. Everything here is reversible: the next call reloads a model.
"""
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

import arch_core as core


def _url(host=None):
    return (host or core.ollama_host()).rstrip("/")


def _json(url, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}


def port_of(host=None):
    """('127.0.0.1', 11434) from the configured host. Any form Ollama accepts: host, host:port, [::1]:11434, a URL."""
    import urllib.parse
    u = urllib.parse.urlsplit(_url(host) if "://" in _url(host) else "http://" + _url(host))
    try:
        port = u.port or 11434
    except ValueError:
        port = 11434
    return (u.hostname or "127.0.0.1"), port


def port_open(hostname, port, timeout=1.0):
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_exe():
    """Where the Ollama program is, or None: on PATH, else its default Windows / macOS / Linux install location."""
    import shutil
    for cand in (shutil.which("ollama"), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                 "/Applications/Ollama.app/Contents/Resources/ollama", "/usr/local/bin/ollama", "/usr/bin/ollama"):
        if cand and os.path.isfile(cand):
            return cand
    return None


def ensure_running(say=print, wait_s=20):
    """Start `ollama serve` in the background when nothing answers on the local port, as its own process (not tied to the window or
    tool that started Architect: seen live, an Ollama started from a terminal stopped with that terminal, and every call to it
    failed). Loopback only unless OLLAMA_HOST says otherwise. -> True when Ollama answers."""
    st = status(timeout=2)
    if st.get("up"):
        return True
    hn, port = port_of()
    if hn not in ("127.0.0.1", "localhost", "::1"):
        return False                                              # a remote Ollama (env or Settings) is somebody else's to start
    if st.get("port_open"):                                       # something holds the port: another program, or Ollama still starting
        say(f"[ollama] not starting a second one: {st.get('error') or 'the port is in use'}")
        return False
    exe = find_exe()
    if not exe:
        say("[ollama] not installed here; models it would serve are unavailable (llama-server still serves the tiers).")
        return False
    import subprocess
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        env = dict(os.environ, OLLAMA_HOST=f"{'[' + hn + ']' if ':' in hn else hn}:{port}")   # the port this install is set to use
        env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
        env.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")
        proc = subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=(os.name != "nt"), close_fds=True, env=env)
    except OSError as e:
        say(f"[ollama] could not start it ({e})")
        return False
    _mark_started(proc.pid, exe)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if status(timeout=2).get("up"):
            say("[ollama] started in the background")
            return True
        time.sleep(1.0)
    say(f"[ollama] started, but it did not answer within {wait_s}s")
    return False


def _marker():
    return os.path.join(core.data_dir(), "ollama_started.json")


def _mark_started(pid, exe=None):
    """Record that ARCHITECT started this `ollama serve` (pid + time): only that one may be stopped on hub exit (arch_cleanup)."""
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        with open(_marker(), "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "exe": exe, "t": time.time()}, f)
    except (OSError, ValueError, TypeError):
        pass


def started_by_us():
    """-> the marker dict {pid, exe, t} when Architect started the Ollama service and that pid is still alive; else None."""
    try:
        with open(_marker(), encoding="utf-8") as f:
            rec = json.load(f)
        pid = int(rec.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not pid:
        return None
    try:
        import arch_server
        alive = arch_server._alive(pid)
    except Exception:
        alive = True
    if not alive:
        clear_started()
        return None
    return rec


def clear_started():
    try:
        os.remove(_marker())
    except OSError:
        pass


def loaded_view(cfg=None, host=None):
    """-> {"running", "models": [{name, size_gb, vram_gb, ours, expires}], "ram_gb"} for the hub. `ours` = a model this app is set to use."""
    import arch_cleanup
    st = status(host, timeout=2)
    ours = arch_cleanup._our_models(cfg)
    models, ram = [], 0.0
    for m in st.get("loaded") or []:
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        models.append({"name": m.get("name"), "size_gb": round(size / 1024 ** 3, 2), "vram_gb": round(vram / 1024 ** 3, 2),
                       "ours": bool(ours is not None and m.get("name") in ours), "expires": m.get("until")})
        ram += size
    return {"running": bool(st.get("up")), "models": models, "ram_gb": round(ram / 1024 ** 3, 2)}


def status(host=None, timeout=3):
    """-> {"up", "host", "port", "port_open", "version", "models": [names], "loaded": [{name, size_gb, vram_pct, until}], "error"}.
    port_open but not up means something else answers on that port (or Ollama is still starting)."""
    hn, port = port_of(host)
    out = {"up": False, "host": _url(host), "port": port, "port_open": port_open(hn, port), "version": None, "models": [], "loaded": [], "error": None}
    if not out["port_open"]:
        out["error"] = f"nothing is listening on {hn}:{port}"
        return out
    try:
        tags = _json(_url(host) + "/api/tags", timeout=timeout)
        out["models"] = [m.get("name") for m in tags.get("models") or [] if m.get("name")]
        out["up"] = True
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = f"port {port} is open but it is not answering like Ollama ({str(getattr(e, 'reason', e))[:80]})"
        return out
    try:
        out["version"] = (_json(_url(host) + "/api/version", timeout=timeout) or {}).get("version")
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        for m in (_json(_url(host) + "/api/ps", timeout=timeout) or {}).get("models") or []:
            size = m.get("size") or 0
            out["loaded"].append({"name": m.get("name") or m.get("model"), "size_gb": round(size / 1024 ** 3, 1),
                                  "vram_pct": (round(100 * (m.get("size_vram") or 0) / size) if size else None), "until": m.get("expires_at"),
                                  "size": size, "size_vram": m.get("size_vram") or 0})
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return out


def _placement(m):
    """Where a loaded Ollama model sits, from /api/ps sizes: card, ram, or split (with the GB on each side)."""
    size, vram = (m or {}).get("size") or 0, (m or {}).get("size_vram") or 0
    where = "ram" if not vram else ("card" if vram >= size else "split")
    return {"where": where, "size_gb": round(size / 1024 ** 3, 2) if size else None,
            "card_gb": round(vram / 1024 ** 3, 2), "ram_gb": round(max(size - vram, 0) / 1024 ** 3, 2)}


def _tel(kind, model, **fields):
    try:
        import arch_telemetry
        arch_telemetry.emit(kind, model=model, runtime="ollama", **fields)
    except Exception:
        pass


def tel_unload(model, trigger="unload", loaded=None):
    """Telemetry for a model leaving Ollama's memory (`loaded` = its /api/ps entry when known). Never raises."""
    try:
        _tel("model.unload", model, trigger=trigger, **(_placement(loaded) if loaded else {"where": None}))
    except Exception:
        pass


def _loaded_entry(model, host=None):
    try:
        for m in (_json(_url(host) + "/api/ps", timeout=2) or {}).get("models") or []:
            if (m.get("name") or m.get("model")) == model:
                return m
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def unload(model, host=None, timeout=20, trigger=None):
    """Ask Ollama to drop one model from memory now. -> True when the server accepted it."""
    if trigger is None:                                 # who asked (free_gpu, come_back, _make_room, ...) when the caller did not say
        try:
            trigger = sys._getframe(1).f_code.co_name
        except Exception:
            trigger = "unload"
    was = _loaded_entry(model, host)
    try:
        _json(_url(host) + "/api/generate", {"model": model, "keep_alive": 0}, timeout=timeout)
        tel_unload(model, trigger=trigger, loaded=was)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def free_gpu(keep, host=None, say=print):
    """Unload every resident model that is not in `keep` (your team for this run). -> list of names unloaded."""
    keep = set(keep or [])
    st = status(host)
    gone = []
    for m in st["loaded"]:
        if m["name"] and m["name"] not in keep:
            if unload(m["name"], host):
                gone.append(m["name"])
                say(f"[ollama] unloaded {m['name']} ({m['size_gb']} GB) to free memory for your team")
    return gone


def warm(model, host=None, keep_alive=None, timeout=600):
    """Load the model now so the first real call does not pay the load time. -> seconds taken, or None on failure."""
    if keep_alive in (None, ""):
        keep_alive = core.ollama_keep_alive()
    t0 = time.time()
    try:
        _json(_url(host) + "/api/generate", {"model": model, "keep_alive": keep_alive}, timeout=timeout)
        secs = round(time.time() - t0, 1)
        if keep_alive in (0, "0"):
            tel_unload(model, trigger="keep_alive 0")
        else:
            _tel("model.load", model, load_s=secs, keep_alive=keep_alive, trigger="warm", **_placement(_loaded_entry(model, host)))
        return secs
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_up(host=None, seconds=20, say=print):
    """Give a just-started Ollama a moment. -> True when it answers in time."""
    t0 = time.time()
    said = False
    while time.time() - t0 < seconds:
        if status(host, timeout=2)["up"]:
            return True
        if not said:
            say("[ollama] waiting for Ollama to answer...")
            said = True
        time.sleep(1.0)
    return False


def start_hint():
    """How to start Ollama on this OS, as one plain line."""
    import sys
    if sys.platform.startswith("win"):
        return "Open the Ollama app from the Start menu (it shows an icon in the tray), or run `ollama serve` in a terminal."
    if sys.platform == "darwin":
        return "Open the Ollama app (menu bar icon), or run `ollama serve` in a terminal."
    return "Run `ollama serve` (or `systemctl start ollama` if it was installed as a service)."


def prepare(cfg, team, host=None, say=print):
    """Before a run: check the server, free memory from models outside the team, and report. -> status dict with 'freed'. Never raises."""
    st = status(host)
    st["freed"] = []
    if not st["up"]:
        say(f"[ollama] {st['error']}. {start_hint()}")
        return st
    if (cfg.get("behavior") or {}).get("exclusive_gpu", "auto") == "auto":
        st["freed"] = free_gpu(team, host, say)
    return st
