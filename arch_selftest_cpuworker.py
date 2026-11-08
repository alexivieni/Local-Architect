"""Self-test for the host CPU worker (arch_cpuworker.py) against a local instance, and for arch_hosts.run_cpu talking to it.
Loopback only, no model, no GPU. run() returns True when every check passed."""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TOKEN = "t0ken-for-tests"


def _req(base, method, path, body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Arch-Token"] = token
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except ValueError:
            return e.code, {}
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
        return "closed", {"error": str(e)}          # the server refused and closed while we were still sending (an oversized body)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return "closed", {"error": str(e.reason)}
        raise


def run():
    import arch_cpuworker as W
    import arch_hosts as H
    fails, ran = [], []

    def check(name, ok):
        ran.append(name)
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    srv = W.make_server("127.0.0.1", 0, TOKEN, timeout=60, jobs=2)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s, b = _req(base, "GET", "/health", token=None)
        check("GET /health answers without a token", s == 200 and b.get("ok") is True)
        s, b = _req(base, "GET", "/api/tags", token=None)
        check("GET /api/tags answers (the hosts health check)", s == 200 and b.get("models") == [])
        test_file = {"test_ok.py": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1 + 1, 2)\n"}
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "tests", "argv": ["-m", "unittest", "-v"], "files": test_file}, token=None)
        check("no token -> 401", s == 401)
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "tests", "argv": ["-m", "unittest"], "files": test_file}, token="wrong")
        check("wrong token -> 401", s == 401)
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "tests", "argv": ["-m", "unittest", "-v"], "files": test_file})
        check("unittest passes with the posted files", s == 200 and b["ok"] is True and b["code"] == 0 and "test_a" in b["out"])
        bad = {"test_bad.py": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 2)\n"}
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "tests", "argv": ["python", "-m", "unittest"], "files": bad})
        check("a failing test returns ok False, code 1", s == 200 and b["ok"] is False and b["code"] == 1)
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "lint", "argv": ["-m", "py_compile", "m.py"], "files": {"m.py": "x = 1\n"}})
        check("lint (py_compile) passes", s == 200 and b["ok"] is True)
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "lint", "argv": ["-m", "py_compile", "m.py"], "files": {"m.py": "def (:\n"}})
        check("lint catches a syntax error", s == 200 and b["ok"] is False)
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "index", "argv": [], "files": {"a.py": "x\ny\n", "d/b.txt": "z"}})
        check("index lists the posted files", s == 200 and b["ok"] and "2 files" in b["out"] and "a.py" in b["out"])
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "scan", "argv": [], "files": {"a.py": "def (:\n"}})
        check("scan reports a syntax error", s == 200 and b["ok"] is False and "a.py" in b["out"])
        for name, body in (("unknown kind", {"kind": "shell", "argv": ["echo", "hi"], "files": {}}),
                           ("python -c", {"kind": "tests", "argv": ["-c", "print(1)"], "files": {}}),
                           ("other module", {"kind": "tests", "argv": ["-m", "http.server"], "files": {}}),
                           ("lint kind with a test module", {"kind": "lint", "argv": ["-m", "pytest"], "files": {}}),
                           ("absolute path argument", {"kind": "tests", "argv": ["-m", "unittest", "/etc/passwd"], "files": {}}),
                           ("parent-dir argument", {"kind": "tests", "argv": ["-m", "pytest", "../x"], "files": {}}),
                           ("pytest plugin option", {"kind": "tests", "argv": ["-m", "pytest", "-p", "evil"], "files": {}}),
                           ("file escaping the temp dir", {"kind": "index", "argv": [], "files": {"../evil.py": "x"}}),
                           ("absolute file name", {"kind": "index", "argv": [], "files": {"/tmp/evil.py": "x"}}),
                           ("too many files", {"kind": "index", "argv": [], "files": {f"f{i}.txt": "x" for i in range(W.MAX_FILES + 1)}}),
                           ("file over the cap", {"kind": "index", "argv": [], "files": {"big.txt": "x" * (W.MAX_FILE_BYTES + 1)}})):
            s, b = _req(base, "POST", "/arch/cpu", body)
            check(f"refused: {name}", s == 400 and b.get("ok") is False)
        s, b = _req(base, "POST", "/arch/cpu", raw=b"{" + b" " * (W.MAX_BODY + 10))
        check("oversized body -> 413 (or the connection closed before the client finished sending it)", s in (413, "closed"))
        s, b = _req(base, "POST", "/arch/cpu", raw=b"not json")
        check("bad JSON -> 400", s == 400)
        s, b = _req(base, "POST", "/nope", {})
        check("unknown path -> 404", s == 404)
        slow = {"s.py": "import time\ntime.sleep(30)\n", "test_s.py": "import time\ntime.sleep(30)\n"}
        s, b = _req(base, "POST", "/arch/cpu", {"kind": "lint", "argv": ["-m", "compileall", "s.py"], "files": slow})
        check("compileall of a file does not run it", s == 200 and b["ok"] is True)
        res = W.execute("tests", ["-m", "unittest", "test_s"], slow, timeout=2)
        check("a job past the timeout is stopped", res["ok"] is False and res["code"] is None and "timed out" in res["out"])
        check("loopback and LAN clients allowed", all(W.client_allowed(a) for a in ("127.0.0.1", "192.168.1.5", "10.0.0.2", "::1")))
        check("public clients refused", not W.client_allowed("8.8.8.8") and not W.client_allowed("not-an-ip"))
        check("a public --host is refused", not W.host_allowed("8.8.8.8") and W.host_allowed("0.0.0.0"))
        try:
            W.make_server("127.0.0.1", 0, "")
            check("no token -> server refuses to start", False)
        except ValueError:
            check("no token -> server refuses to start", True)
        check("main() without a token exits 2", W.main(["--port", "0", "--token", ""]) == 2)
        cfg = {"hosts": {"box": base}, "role_hosts": {"cpu": "box"}}
        os.environ["ARCHITECT_CPU_TOKEN"] = TOKEN
        try:
            def local(p):
                return {"ok": False, "code": 9, "out": "local"}
            r = H.run_cpu("tests", {"argv": ["-m", "unittest"], "files": test_file}, local, cfg)
            check("arch_hosts.run_cpu runs it on the worker", r["where"] == "box" and r["ok"] is True and r["fallback"] is None)
            os.environ["ARCHITECT_CPU_TOKEN"] = "wrong"
            H._LIVE.clear()
            r = H.run_cpu("tests", {"argv": ["-m", "unittest"], "files": test_file}, local, cfg)
            check("a refused token falls back to local", r["where"] == "local" and bool(r["fallback"]))
        finally:
            os.environ.pop("ARCHITECT_CPU_TOKEN", None)
    finally:
        srv.shutdown()
        srv.server_close()
    print(f"CPUWORKER: PASSED ({len(ran)} checks)" if not fails else f"CPUWORKER: FAILED {len(fails)} of {len(ran)}: {fails}")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
