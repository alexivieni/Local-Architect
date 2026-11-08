"""Environment preflight: the thing to run before trusting any flow to work, so a failure surfaces as
"Ollama isn't running" instead of a confusing error three steps into a run. Every check is a REAL probe --
a real HTTP call, a real socket bind, a real disk-usage syscall, a real file write -- never asserted from
memory or config alone.

    python architect.py doctor                check this folder + the hub's default port
    python architect.py doctor --root path     check a specific project
    python architect.py doctor --port 9000     check a different hub port

Each check carries "advisory": True when it should inform you but never fail the overall verdict (git is
optional unless you turned on behavior.git=commit; a slow Ollama reply isn't a hard failure).

    python architect.py doctor --fix           apply the safe, real repairs below, then re-check

A FEW failures are self-repairable without touching anything risky: a missing `pytest` is pip-installed
(real subprocess, real package), and low disk space is recovered by deleting the Architect's own stale
`.arch/runs/` history older than 14 days (real directories, real bytes freed). Nothing else is auto-fixed:
a closed port might be someone else's service, a missing NIC is a hardware fact, and Ollama has to be
started by you -- `--fix` reports those plainly instead of guessing at a repair.
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

MIN_PYTHON = (3, 9)


def _ok(name, ok, detail="", advisory=False):
    return {"name": name, "ok": bool(ok), "detail": detail, "advisory": advisory}


def check_python():
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return _ok(f"Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}", ok, f"running {v.major}.{v.minor}.{v.micro}")


def check_test_python():
    """Which Python runs the tests. On Windows `py -3` picks the NEWEST Python, which may have no pytest (seen here: 3.15 without it,
    3.12 with it); the launcher already switches (start.python_for_tests), but a suite run by hand with `py -3` just did less."""
    import contextlib
    import io
    try:
        import start
        with contextlib.redirect_stdout(io.StringIO()):
            exe = start.python_for_tests()
    except Exception as ex:
        return _ok("Python for tests", False, f"could not tell: {ex}", advisory=True)
    v = sys.version_info
    here = os.path.normcase(os.path.abspath(exe)) == os.path.normcase(os.path.abspath(sys.executable))
    if here and importlib.util.find_spec("pytest"):
        return _ok("Python for tests", True, f"this Python ({v.major}.{v.minor}) has pytest and runs the tests", advisory=True)
    if not here:
        return _ok("Python for tests", False, f"this Python ({v.major}.{v.minor}) has no pytest; the tests run with {exe} "
                   f"(the launcher picks it). Run suites by hand with that one, e.g. py -3.12 arch_selftest_x.py", advisory=True)
    return _ok("Python for tests", False, "no installed Python has pytest: checks that run tests will do less "
               "(python -m pip install pytest, with your yes)", advisory=True)


def check_module(mod, advisory=True):
    present = importlib.util.find_spec(mod) is not None
    hint = "" if present else f"pip install {mod}" + (" --break-system-packages" if sys.prefix == sys.base_prefix else "")
    return _ok(f"{mod} installed", present, hint, advisory=advisory)


def check_ollama(host=None, timeout=3):
    host = (host or os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")).strip()
    url = (host if "://" in host else "http://" + host).rstrip("/") + "/api/tags"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
        n = len(data.get("models") or [])
        detail = f"{n} model(s) installed, {round(time.time() - t0, 2)}s"
        return _ok(f"Ollama reachable at {host}", True, detail + ("" if n else " -- none pulled yet: `ollama pull <model>`"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return _ok(f"Ollama reachable at {host}", False, f"{e} -- start it with `ollama serve`, or set OLLAMA_HOST")


def check_self_code():
    """Every Architect module compiles and imports (catches a half-applied update or a bad self-improvement before it bites)."""
    import arch_deps
    bad = arch_deps.self_check()
    return _ok("Architect's own code compiles and imports", not bad, "; ".join(f"{b['module']}: {b['error']}" for b in bad)[:300] + (" -- run `python architect.py self-rollback` to go back to the last good version" if bad else ""))


def check_ollama_port(host=None):
    """Is the configured port really Ollama, and is anything else loaded on it that is not yours?"""
    import arch_ollama
    st = arch_ollama.status(host)
    if st["up"]:
        ld = ", ".join(f"{m['name']} ({m['size_gb']} GB)" for m in st["loaded"]) or "nothing"
        return _ok(f"Ollama port {st['port']}", True, f"version {st['version'] or '?'}; loaded now: {ld}", advisory=True)
    if st["port_open"]:
        return _ok(f"Ollama port {st['port']}", False, st["error"] + " -- another program may own that port; set OLLAMA_HOST or stop it")
    return _ok(f"Ollama port {st['port']}", False, st["error"] + ". " + arch_ollama.start_hint(), advisory=True)


def check_disk(root, min_gb=1.0):
    # root may not exist yet (a project about to be created): walk up to the nearest existing ancestor,
    # since that's the filesystem/volume the new folder will actually land on.
    probe = root
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        u = shutil.disk_usage(probe)
        free_gb = u.free / (1024 ** 3)
        return _ok(f"disk space in {root}", free_gb >= min_gb, f"{free_gb:.1f} GB free (need at least {min_gb:.0f} GB)" + (f", measured at {probe} (doesn't exist yet)" if probe != root else ""))
    except OSError as e:
        return _ok(f"disk space in {root}", False, str(e))


def check_write(root, label=None):
    try:
        os.makedirs(root, exist_ok=True)
        p = os.path.join(root, ".arch_doctor_write_test")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(p)
        return _ok(f"can write to {label or root}", True)
    except OSError as e:
        return _ok(f"can write to {label or root}", False, str(e))


def check_port_free(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return _ok(f"port {port} is free for the hub", True, advisory=True)
    except OSError as e:
        return _ok(f"port {port} is free for the hub", False, f"in use ({e}); pass --port to use another, or it's already running there", advisory=True)
    finally:
        s.close()


def check_git():
    path = shutil.which("git")
    return _ok("git available (for behavior.git=commit checkpoints)", bool(path), path or "only needed if you turn on git checkpoints", advisory=True)


def check_network_policy(cfg):
    mode = (cfg.get("behavior") or {}).get("network", "open")
    detail = {"open": "unrestricted -- your machine's normal network access", "practice": "loopback + your net_allow list only", "offline": "loopback only"}.get(mode, mode)
    return _ok(f"network policy for code the Architect runs: {mode}", True, detail, advisory=True)


def detect_nic():
    """Real NIC presence check -- a network CARD, not connectivity (this returns the same answer whether
    or not the network actually reaches anywhere). Linux: reads /sys/class/net for a non-loopback
    interface. Elsewhere (or if that's empty): a UDP "connect" that never sends a packet -- the OS just
    picks a local route/address for it -- which works even fully offline, so it is safe to run with any
    network policy. -> (has_nic: bool, detail: str)."""
    ifaces = []
    base = "/sys/class/net"
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if name == "lo":
                continue
            try:
                state = open(os.path.join(base, name, "operstate")).read().strip()
            except OSError:
                state = "unknown"
            ifaces.append((name, state))
    if ifaces:
        up = [n for n, st in ifaces if st == "up"]
        return bool(up or ifaces), ", ".join(f"{n} ({st})" for n, st in ifaces)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != "0.0.0.0":
            return True, f"interface detected (local address {ip})"
    except OSError:
        pass
    return False, "no network interface found"


def check_nic():
    has_nic, detail = detect_nic()
    extra = (" -- flows that reach out (secure/network/pentest live checks, `ollama pull`, a remote Ollama host) need one; "
             "build/test/optimize and anything already on disk do not." if not has_nic else "")
    return _ok("network interface (NIC) present", has_nic, detail + extra, advisory=True)


def check_integrity():
    """Is this install still what was last verified (MANIFEST.sha256, rewritten by every self-improve promotion)? Advisory: a hand edit is allowed."""
    try:
        import arch_selfimprove as si
        v = si.verify()
    except Exception as e:                                   # a broken checker must not break the doctor
        return _ok("install integrity", False, f"could not check: {e}", advisory=True)
    if not v["has_manifest"]:
        return _ok("install integrity (manifest)", False, "no manifest yet: run `python architect.py self-verify --write` to record this install as the verified state", advisory=True)
    if v["ok"]:
        return _ok("install integrity (manifest)", True, "matches the last verified state")
    n = len(v["modified"]) + len(v["missing"])
    return _ok("install integrity (manifest)", False, f"{n} file(s) differ from the last verified state ({', '.join((v['modified'] + v['missing'])[:3])}): "
               "expected after a manual edit; `self-verify --write` records the current state", advisory=True)


def run(root=None, hub_port=8766):
    root = os.path.abspath(root or os.getcwd())
    import arch_core as core
    cfg = core.load_config(root) if os.path.isdir(root) else core.load_config()
    checks = [
        check_python(),
        check_module("pytest"),
        check_test_python(),
        check_ollama(cfg.get("ollama_host")),
        check_disk(root),
        check_write(root, root),
        check_write(os.path.join(root, ".arch"), ".arch/ (runs, backups, undo)"),
        check_port_free(hub_port),
        check_git(),
        check_nic(),
        check_network_policy(cfg),
        check_integrity(),
        check_self_code(),
        check_ollama_port(cfg.get("ollama_host")),
    ]
    ok = all(c["ok"] for c in checks if not c["advisory"])
    return {"ok": ok, "root": root, "checks": checks}


def pip_install(mod, timeout=120):
    """Real `pip install`, not a simulation -- returns (ok, tail of combined stdout/stderr)."""
    cmd = [sys.executable, "-m", "pip", "install", mod]
    if sys.prefix == sys.base_prefix:  # not inside a venv: PEP 668 managed environments need this flag
        cmd.append("--break-system-packages")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-500:]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def clean_old_runs(root, max_age_days=14):
    """Deletes the Architect's own .arch/runs/<timestamp>_<flow> directories older than max_age_days --
    disposable run history (diffs, logs, the undo trail for runs long since confirmed good), never
    project files. -> (bytes_freed, [dir names removed])."""
    runs_dir = os.path.join(root, ".arch", "runs")
    if not os.path.isdir(runs_dir):
        return 0, []
    cutoff = time.time() - max_age_days * 86400
    freed, removed = 0, []
    for name in sorted(os.listdir(runs_dir)):
        p = os.path.join(runs_dir, name)
        try:
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(p) for f in fs if os.path.isfile(os.path.join(dp, f)))
                shutil.rmtree(p)
                freed += size
                removed.append(name)
        except OSError:
            continue
    return freed, removed


def fix(root=None, hub_port=8766, install_missing=None, clean_runs=True, max_run_age_days=14):
    """Runs the checks, applies whichever SAFE real repairs apply (see module docstring), then re-runs
    the checks so 'after' reflects what's actually true now, not a claim. -> {"before", "actions", "after"}."""
    root = os.path.abspath(root or os.getcwd())
    before = run(root, hub_port)
    by_name = {c["name"]: c for c in before["checks"]}
    actions = []
    if install_missing is None:                       # the default follows the ONE online switch: offline installs never reach PyPI on their own
        try:
            import arch_core
            install_missing = bool((arch_core.load_config(root).get("online") or {}).get("allow_pip"))
        except Exception:
            install_missing = False
    if not by_name.get("pytest installed", {}).get("ok", True):
        if install_missing:
            ok, log = pip_install("pytest")
            actions.append({"what": "pip install pytest", "ok": ok, "detail": log.strip()})
        else:
            actions.append({"what": "pip install pytest", "ok": False, "detail": "skipped: online.allow_pip is off (Settings > Online), so nothing reaches PyPI. Install it yourself: python -m pip install pytest"})
    disk_check = next((c for c in before["checks"] if c["name"].startswith("disk space in")), None)
    if clean_runs and disk_check is not None and not disk_check["ok"]:
        freed, removed = clean_old_runs(root, max_run_age_days)
        actions.append({"what": f"clean .arch/runs/ older than {max_run_age_days}d", "ok": True,
                        "detail": f"freed {freed / 1024 / 1024:.1f} MB across {len(removed)} run(s)" if removed else "nothing old enough to clean -- disk is still low for another reason"})
    after = run(root, hub_port) if actions else before
    return {"before": before, "actions": actions, "after": after}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--fix", action="store_true", help="apply the safe, real repairs (pip install pytest if missing; clean stale .arch/runs/ if disk is low), then re-check")
    a = ap.parse_args(argv)

    def show(out):
        width = max(len(c["name"]) for c in out["checks"])
        for c in out["checks"]:
            mark = "OK  " if c["ok"] else ("--  " if c["advisory"] else "FAIL")
            line = f"  {mark}  {c['name']:<{width}}"
            if c["detail"]:
                line += "   " + c["detail"]
            print(line)

    if a.fix:
        res = fix(a.root, a.port)
        print("Before:")
        show(res["before"])
        print()
        if res["actions"]:
            print("Repairs applied:")
            for act in res["actions"]:
                print(f"  {'OK  ' if act['ok'] else 'FAIL'}  {act['what']}   {act['detail']}")
            print()
            print("After:")
            show(res["after"])
        else:
            print("Nothing here was safely auto-fixable.")
        out = res["after"]
    else:
        out = run(a.root, a.port)
        show(out)
    print()
    print("Ready to run." if out["ok"] else "Not ready: fix the FAIL line(s) above first.")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
