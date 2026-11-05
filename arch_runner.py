"""Starts, stops, health-checks and tails the logs of a project's OWN run command -- the same command
you'd type yourself (`python -m uvicorn app.main:app`, `python -m app.server`, ...). This answers "where is
my run.py / how do I actually run the thing the Architect built": there is no run.py because none of the
templates use that filename, but every one of them has a real run command (recorded in .arch/handoff.json
by the 'new' flow, or in portfolio.json for an app you registered), and this module is what the hub's
Run page and `architect.py serve` use to start it as a REAL subprocess, not a simulation:

  start   spawns the command with subprocess.Popen (shell=True, cwd=root), a real PID, a real log file.
  stop    SIGTERM then SIGKILL (POSIX) / taskkill (Windows) on that real PID.
  health  a real TCP connect to the port, then a real HTTP GET of /health, /healthz or / if one answers.
  logs    tails the real stdout/stderr file the process is writing to.

Nothing here opens a port that isn't the project's own, and nothing here fakes a response: health() reports
exactly what the socket/HTTP call returned, including "not running" and "port closed".
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import arch_core as core

STATE_DIR = ".arch/runner"
SERVER_PAT = re.compile(r"\b(uvicorn|app\.server|flask\s+run|manage\.py\s+runserver|gunicorn|hypercorn)\b")


def _state_path(root):
    return os.path.join(root, STATE_DIR, "state.json")


def _load(root):
    p = _state_path(root)
    if os.path.isfile(p):
        try:
            return json.loads(open(p, encoding="utf-8").read())
        except (OSError, ValueError):
            pass
    return {}


def _save(root, state):
    d = os.path.join(root, STATE_DIR)
    os.makedirs(d, exist_ok=True)
    open(_state_path(root), "w", encoding="utf-8").write(json.dumps(state, indent=2))


def default_cmd(root):
    """The project's own run command: from .arch/handoff.json (written by the 'new'/build flows). None if
    the project has never been built by the Architect and no command was passed explicitly."""
    h = os.path.join(root, ".arch", "handoff.json")
    if os.path.isfile(h):
        try:
            j = json.loads(open(h, encoding="utf-8").read())
            if j.get("run"):
                return j["run"]
        except (OSError, ValueError):
            pass
    return None


def is_server_cmd(cmd):
    return bool(SERVER_PAT.search(cmd or ""))


def _port_of(cmd, env):
    m = re.search(r"--port[= ](\d+)", cmd or "")
    if m:
        return int(m.group(1))
    if env and env.get("PORT"):
        try:
            return int(env["PORT"])
        except ValueError:
            pass
    return 8000


def _is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _free_port_from(start_port, tries=50):
    for p in range(start_port, start_port + tries):
        if _is_free(p):
            return p
    return start_port  # give up and let the process itself report the real error


def is_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0 if os.name != "nt" else signal.SIGTERM if False else 0)
        return True
    except OSError:
        return False
    except AttributeError:
        return False


def start(root, cmd=None, port=None, extra_env=None, timeout_oneshot=600, network=None):
    """-> {"ok", "pid"?, "log", "cmd", "mode": "server"|"oneshot", "port"?, "exit_code"?, "error"?, "network"}.

    Runs under the SAME network policy as everything else the Architect executes on your behalf (tests,
    benchmarks, model-written code): behavior.network / behavior.net_allow / behavior.net_block_tools from
    this project's config, enforced by the identical netguard.py mechanism core.guard_env() uses for
    run_tests/run_cmd -- not a separate, unreviewed set of rules for "the thing that happens to be a server".
    Pass network="open" only when you explicitly want this one run unrestricted (e.g. it legitimately calls
    an external API); the default follows the project's own setting, offline unless you changed it."""
    cmd = cmd or default_cmd(root)
    if not cmd:
        return {"ok": False, "error": "no run command for this project: pass one explicitly, or build it with the 'new' flow (which records one in .arch/handoff.json), or register it in portfolio.json with a test_command/run"}
    st = _load(root)
    if st.get("pid") and is_running(st["pid"]):
        return {"ok": False, "error": f"already running (pid {st['pid']}); stop it first"}
    logdir = os.path.join(root, STATE_DIR)
    os.makedirs(logdir, exist_ok=True)
    logpath = os.path.join(logdir, "run.log")
    cfg = core.load_config(root)
    if network:
        cfg = core.deep_merge(cfg, {"behavior": {"network": network}})
    core.set_net(cfg)
    env = core.guard_env(offline=True)  # the one, shared policy -- same call run_tests/run_cmd make
    env.setdefault("PYTHONUNBUFFERED", "1")  # so logs() can see output as it happens, not only on flush/exit
    env.update(extra_env or {})
    server = is_server_cmd(cmd)
    explicit_port = port or (re.search(r"--port[= ](\d+)", cmd or "") and int(re.search(r"--port[= ](\d+)", cmd).group(1)))
    chosen_port = port or _port_of(cmd, env)
    port_note = None
    if server and not explicit_port and not _is_free(chosen_port):
        new_port = _free_port_from(chosen_port + 1)
        port_note = f"port {chosen_port} was already in use; using {new_port} instead"
        chosen_port = new_port
    if server:
        env["PORT"] = str(chosen_port)
    logf = open(logpath, "w", encoding="utf-8")
    # start_new_session puts the shell AND whatever it execs/forks (the real server process) in one new
    # process group, so stop() can kill that whole group -- not just the shell wrapper `shell=True` spawns,
    # which would otherwise orphan the real server (it keeps the real port bound) while claiming "stopped".
    popen_kwargs = {"start_new_session": True} if os.name != "nt" else {}
    try:
        p = subprocess.Popen(cmd, shell=True, cwd=root, env=env, stdout=logf, stderr=subprocess.STDOUT, **popen_kwargs)
    except OSError as e:
        logf.close()
        return {"ok": False, "error": str(e)}
    net_policy = {"mode": core._NET["mode"], "allow": core._NET["allow"], "tools": core._NET["tools"]}
    if server:
        state = {"pid": p.pid, "cmd": cmd, "mode": "server", "port": chosen_port, "started": time.time(), "log": logpath, "network": net_policy}
        if port_note:
            state["note"] = port_note
        _save(root, state)
        return {"ok": True, **state}
    try:
        rc = p.wait(timeout=timeout_oneshot)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = None
    state = {"pid": None, "cmd": cmd, "mode": "oneshot", "port": None, "started": time.time(), "log": logpath, "exit_code": rc, "network": net_policy}
    _save(root, state)
    return {"ok": rc == 0, **state}


def _win_orphans(pid):
    """Windows: processes whose parent was `pid`. When the shell wrapper has already exited, its server keeps running with
    the wrapper's (dead) id as its parent id; taskkill /T on the wrapper can no longer reach it. Seen 2026-11-04: a sample
    `python -m app.server` left running after the security suite. -> list of pids (empty when none or not Windows)."""
    if os.name != "nt" or not pid:
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={int(pid)}').ProcessId"],
                             capture_output=True, text=True, timeout=15).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def stop(root):
    st = _load(root)
    pid = st.get("pid")
    if not pid or not is_running(pid):
        if pid:
            for kid in _win_orphans(pid):                     # the wrapper is gone but what it started may not be
                subprocess.run(["taskkill", "/PID", str(kid), "/T", "/F"], capture_output=True, timeout=10)
            st["pid"] = None
            _save(root, st)
        return {"ok": False, "error": "not running"}
    try:
        if os.name == "nt":
            tree, todo = [], [pid]                             # every descendant first (a launcher in between may already be gone)
            while todo and len(tree) < 64:
                kids = _win_orphans(todo.pop())
                tree += kids
                todo += kids
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
            for kid in tree:
                subprocess.run(["taskkill", "/PID", str(kid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            # signal the WHOLE process group start() created (see start_new_session there), not just pid --
            # with shell=True, pid is the shell wrapper; killing only it can leave the real server process
            # (and its real bound port) running, orphaned, while this call still reports "ok".
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None
            def _sig(s):
                try:
                    os.killpg(pgid, s) if pgid is not None else os.kill(pid, s)
                except OSError:
                    os.kill(pid, s)  # pgid already gone (process exited) but pid itself may still need this
            _sig(signal.SIGTERM)
            for _ in range(20):
                if not is_running(pid):
                    break
                time.sleep(0.25)
            if is_running(pid):
                _sig(signal.SIGKILL)
            try:  # reap a zombie if we happen to be its parent (e.g. start+stop in one process, as in tests);
                os.waitpid(pid, os.WNOHANG)  # a real `serve start` then `serve stop` are separate OS processes,
            except (ChildProcessError, OSError):  # so the kernel reparents and reaps it without our help there.
                pass
    except OSError as e:
        return {"ok": False, "error": str(e)}
    st["pid"] = None
    _save(root, st)
    return {"ok": True}


def health(root, timeout=2.0):
    """-> {"running", "cmd", "mode", and for a server: "port", "port_open", "http_ok", "http_path"?}.
    A real TCP connect, then a real HTTP GET -- never simulated, and 'running' is a real PID check."""
    st = _load(root)
    pid = st.get("pid")
    running = is_running(pid)
    out = {"running": running, "cmd": st.get("cmd"), "mode": st.get("mode"), "network": st.get("network")}
    if st.get("mode") == "oneshot":
        out["exit_code"] = st.get("exit_code")
        return out
    out["port"] = st.get("port")
    if not running or not st.get("port"):
        return out
    try:
        with socket.create_connection(("127.0.0.1", st["port"]), timeout=timeout):
            out["port_open"] = True
    except OSError as e:
        out["port_open"] = False
        out["detail"] = str(e)
        return out
    out["http_ok"] = None
    for path in ("/health", "/healthz", "/"):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{st['port']}{path}", timeout=timeout)
            out["http_ok"] = 200 <= r.status < 500
            out["http_path"] = path
            break
        except urllib.error.URLError:
            continue
    return out


def logs(root, tail=60):
    st = _load(root)
    p = st.get("log")
    if not p or not os.path.isfile(p):
        return ""
    lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    return "\n".join(lines[-tail:])


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["start", "stop", "status", "logs"])
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--cmd", help="run command (default: from .arch/handoff.json)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--tail", type=int, default=60)
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    if a.action == "start":
        print(json.dumps(start(root, a.cmd, a.port), indent=2))
    elif a.action == "stop":
        print(json.dumps(stop(root), indent=2))
    elif a.action == "status":
        print(json.dumps(health(root), indent=2))
    elif a.action == "logs":
        print(logs(root, a.tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
