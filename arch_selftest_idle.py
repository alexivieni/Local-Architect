"""Self-test for Settings > Server > Free memory when idle (arch_server.idle_sweep): idle model servers and Ollama copies are
unloaded after N minutes, busy ones never, and 0 keeps everything loaded. No real servers: the HTTP calls are scripted."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ARCHITECT_SELFTEST", "1")


def run():
    import arch_server as s
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    real_state, real_stop = s.load_state, s.stop
    stopped, calls = [], []
    try:
        s.load_state = lambda: {"processes": {"worker": {"url": "http://127.0.0.1:1", "name": "w"}, "router": {"url": "http://127.0.0.1:2", "name": "r"}}}
        s.stop = lambda tiers=None, say=None, **kw: stopped.extend(tiers or [])
        busy = {"http://127.0.0.1:1/slots-busy": False, "http://127.0.0.1:2/slots-busy": True}

        def http(url, data=None):
            calls.append(url)
            if url.endswith("/api/ps"):
                return json.dumps({"models": [{"name": "moe:iq3", "size": 13.7e9}]}).encode()
            return busy.get(url, False)
        s._IDLE["seen"].clear()
        cfg = {"server": {"idle_free_min": 1}}
        a = s.idle_sweep(cfg, now=1000, http=http)
        b = s.idle_sweep(cfg, now=1030, http=http)
        c = s.idle_sweep(cfg, now=1070, http=http)
        check("nothing is freed before the idle time has passed", a == [] and b == [], (a, b))
        check("after it, the idle worker is stopped and the busy router is kept", stopped == ["worker"], stopped)
        check("an idle Ollama copy is asked to unload (keep_alive 0)", any(u.endswith("/api/generate") for u in calls) and any("Ollama" in x for x in c), c)
        stopped.clear()
        s._IDLE["seen"].clear()
        check("0 = keep everything loaded: the sweeper does nothing", s.idle_sweep({"server": {"idle_free_min": 0}}, now=9e9, http=http) == [] and not stopped)
    finally:
        s.load_state, s.stop = real_state, real_stop
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' IDLE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' IDLE CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
