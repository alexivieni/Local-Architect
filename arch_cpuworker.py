"""Host CPU worker: the receiver another machine runs so this one can send it CPU work (standard library only).

    py -3.12 arch_cpuworker.py --port 8790 --token SECRET [--host 0.0.0.0]

The client is arch_hosts.run_cpu: POST /arch/cpu {"kind", "argv", "files"} -> {"ok", "code", "out"}.  GET /health (and /api/tags, which is what
the hosts health check asks every machine) answer without a token.

What it will and will not do (it runs code a caller posted, so it is deliberately narrow):
  * kinds: tests (python -m pytest | unittest), lint (python -m pyflakes | flake8 | ruff | py_compile | compileall), index (a file manifest,
    built in), scan (a syntax scan of the .py files, built in). Any other kind is refused; argv is checked per kind, never run through a shell.
  * every request needs the token (header X-Arch-Token or Authorization: Bearer); no token configured = the server does not start.
  * only loopback and private (LAN / link-local) client addresses are served; --host must itself be loopback or private (or 0.0.0.0, still
    filtered per client).
  * the posted files go to a fresh temp folder (relative, no '..', no absolute path, size and count capped) that is removed afterwards;
    the child gets a minimal environment, a hard timeout and a capped output; one job at a time per slot (--jobs, default 2), else 503.
"""
import argparse
import hmac
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 2_000_000          # same ceiling the client uses (arch_hosts.CPU_MAX_BYTES)
MAX_FILES = 500
MAX_FILE_BYTES = 512_000
MAX_ARGV = 40
MAX_OUT = 64_000
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 900
KINDS = ("tests", "lint", "index", "scan")
TEST_MODULES = ("pytest", "unittest")
LINT_MODULES = ("pyflakes", "flake8", "ruff", "py_compile", "compileall")
_PY_NAMES = ("python", "python3", "python.exe", "py", "py.exe", "py -3.12")
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}


class Refused(Exception):
    pass


def client_allowed(addr):
    """Loopback and private/link-local addresses only."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def host_allowed(host):
    if host in ("0.0.0.0", "::", ""):
        return True
    return client_allowed(host)


def _safe_rel(rel):
    if not isinstance(rel, str) or not rel or len(rel) > 300 or "\x00" in rel:
        raise Refused("bad file name")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":") or any(p in ("..", "") for p in norm.split("/")):
        raise Refused(f"unsafe path {rel[:60]!r}")
    return norm


def write_files(tmp, files):
    if not isinstance(files, dict):
        raise Refused("files must be an object {path: text}")
    if len(files) > MAX_FILES:
        raise Refused(f"too many files (limit {MAX_FILES})")
    for rel, text in files.items():
        norm = _safe_rel(rel)
        if not isinstance(text, str):
            raise Refused("file contents must be text")
        data = text.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise Refused(f"{norm}: file too large (limit {MAX_FILE_BYTES} bytes)")
        full = os.path.join(tmp, *norm.split("/"))
        if os.path.commonpath([os.path.realpath(tmp), os.path.realpath(os.path.dirname(full))]) != os.path.realpath(tmp):
            raise Refused(f"unsafe path {rel[:60]!r}")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)


def _check_args(args):
    for a in args:
        if not isinstance(a, str) or len(a) > 300 or "\x00" in a:
            raise Refused("bad argument")
        n = a.replace("\\", "/")
        if n.startswith("/") or (len(n) > 1 and n[1] == ":") or ".." in n.split("/") or n.startswith("~"):
            raise Refused(f"argument {a[:60]!r} points outside the posted files")
        if n.split("=")[0] in ("-c", "--rootdir", "--basetemp", "-p", "--import-mode", "--pyargs", "--config-file", "--cache-dir"):
            raise Refused(f"option {a.split('=')[0]} is not allowed")


def build_command(kind, argv):
    """-> argv list to run (None for the built-in kinds). Raises Refused."""
    if kind not in KINDS:
        raise Refused(f"unknown kind {kind!r}")
    argv = [str(a) if not isinstance(a, str) else a for a in (argv or [])] if isinstance(argv, list) else None
    if argv is None or len(argv) > MAX_ARGV:
        raise Refused("argv must be a short list")
    if kind in ("index", "scan"):
        return None
    if argv and argv[0].lower() in _PY_NAMES:
        argv = argv[1:]
    if argv and argv[0] == "-3.12":
        argv = argv[1:]
    allowed = TEST_MODULES if kind == "tests" else LINT_MODULES
    if argv[:1] == ["-m"] and len(argv) >= 2:
        mod, rest = argv[1], argv[2:]
    elif argv and argv[0] in allowed:
        mod, rest = argv[0], argv[1:]
    else:
        raise Refused(f"{kind}: only python -m {' | '.join(allowed)} is allowed")
    if mod not in allowed:
        raise Refused(f"{kind}: module {mod!r} is not allowed")
    _check_args(rest)
    return [sys.executable, "-m", mod] + rest


def _env(tmp):
    keep = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL")}
    keep.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONPATH=tmp, PYTHONUTF8="1", HOME=tmp, USERPROFILE=tmp)
    return keep


def _tail(text, n=MAX_OUT):
    return text if len(text) <= n else "[...cut...]\n" + text[-n:]


def _walk(tmp):
    for dp, dns, fns in os.walk(tmp):
        dns[:] = sorted(d for d in dns if d not in _SKIP_DIRS)
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            yield os.path.relpath(p, tmp).replace(os.sep, "/"), p


def _builtin_index(tmp):
    rows = []
    for rel, p in _walk(tmp):
        try:
            with open(p, "rb") as f:
                b = f.read()
            rows.append(f"{rel}\t{len(b)}\t{b.count(b'\\n') + (1 if b and not b.endswith(b'\\n') else 0)}")
        except OSError:
            continue
    return True, 0, f"{len(rows)} files\n" + "\n".join(rows)


def _builtin_scan(tmp):
    import ast
    bad, n = [], 0
    for rel, p in _walk(tmp):
        if not rel.endswith(".py"):
            continue
        n += 1
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                ast.parse(f.read(), rel)
        except SyntaxError as e:
            bad.append(f"{rel}:{e.lineno}: {e.msg}")
    return (not bad), (0 if not bad else 1), f"{n} python files scanned, {len(bad)} with syntax errors" + ("".join("\n" + b for b in bad))


def execute(kind, argv, files, timeout=DEFAULT_TIMEOUT):
    """Run one job. -> {"ok", "code", "out"}. Raises Refused for anything not allowed (nothing is run then)."""
    cmd = build_command(kind, argv)
    tmp = tempfile.mkdtemp(prefix="arch_cpu_")
    try:
        write_files(tmp, files)
        if cmd is None:
            ok, code, out = _builtin_index(tmp) if kind == "index" else _builtin_scan(tmp)
            return {"ok": ok, "code": code, "out": _tail(out)}
        try:
            p = subprocess.run(cmd, cwd=tmp, env=_env(tmp), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            partial = ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace")
            return {"ok": False, "code": None, "out": _tail(partial + f"\n[timed out after {timeout}s]")}
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return {"ok": p.returncode == 0, "code": p.returncode, "out": _tail(out)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_handler(token, timeout, slots):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body):
            d = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(d)
            self.close_connection = True

        def _gate(self):
            if not client_allowed(self.client_address[0]):
                self._send(403, {"ok": False, "error": "only loopback and LAN clients are served"})
                return False
            return True

        def do_GET(self):
            if not self._gate():
                return
            if self.path.split("?")[0] in ("/health", "/api/tags"):
                return self._send(200, {"ok": True, "worker": "architect-cpu", "kinds": list(KINDS), "models": []})
            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._gate():
                return
            if self.path.split("?")[0] != "/arch/cpu":
                return self._send(404, {"ok": False, "error": "not found"})
            got = self.headers.get("X-Arch-Token") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                got = got or auth[7:].strip()
            if not hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8")):
                return self._send(401, {"ok": False, "error": "bad token"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"ok": False, "error": f"body must be 1..{MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except ValueError:
                return self._send(400, {"ok": False, "error": "body is not a JSON object"})
            if not slots.acquire(blocking=False):
                return self._send(503, {"ok": False, "error": "busy"})
            try:
                t = body.get("timeout")
                t = min(MAX_TIMEOUT, max(1, int(t))) if isinstance(t, (int, float)) else timeout
                res = execute(body.get("kind"), body.get("argv"), body.get("files") or {}, min(t, timeout) if timeout else t)
                return self._send(200, res)
            except Refused as e:
                return self._send(400, {"ok": False, "code": None, "out": f"refused: {e}", "error": str(e)})
            except Exception as e:                             # a worker fault is reported to the caller, never kills the server
                return self._send(500, {"ok": False, "code": None, "out": f"worker error: {type(e).__name__}", "error": str(e)[:200]})
            finally:
                slots.release()
    return H


def make_server(host, port, token, timeout=DEFAULT_TIMEOUT, jobs=2):
    if not token:
        raise ValueError("a token is required")
    if not host_allowed(host):
        raise ValueError(f"--host {host} is not a loopback or LAN address")
    return ThreadingHTTPServer((host, int(port)), make_handler(token, int(timeout), threading.BoundedSemaphore(max(1, int(jobs)))))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Architect host CPU worker (tests / lint / index / scan for another machine).")
    ap.add_argument("--host", default="127.0.0.1", help="bind address; loopback or LAN only (default 127.0.0.1; 0.0.0.0 = all, clients still filtered)")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--token", default=os.environ.get("ARCHITECT_CPU_TOKEN", ""), help="shared secret (or env ARCHITECT_CPU_TOKEN)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds a job may run")
    ap.add_argument("--jobs", type=int, default=2, help="jobs at once")
    a = ap.parse_args(argv)
    try:
        srv = make_server(a.host, a.port, a.token, a.timeout, a.jobs)
    except ValueError as e:
        print(f"cpu worker: {e}", file=sys.stderr)
        return 2
    print(f"cpu worker on http://{a.host}:{a.port}  kinds: {', '.join(KINDS)}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
