"""Self-test for the offline seal: with every non-loopback socket connection recorded (and refused), the default path -- settings, the hub page,
a doctor repair, an eval run, the catalog -- never tries to leave this machine. The online switch is off by default and the app behaves the
same with it off. No model, no GPU, no network."""
import os
import shutil
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOOPBACK = ("127.0.0.1", "::1", "localhost", "0.0.0.0")


class Recorder:
    """Wraps socket connects: loopback passes, anything else is recorded and refused."""

    def __init__(self):
        self.attempts = []
        self._orig = socket.socket.connect, socket.socket.connect_ex

    def __enter__(self):
        rec = self
        oc, ox = self._orig

        def _host(addr):
            try:
                return addr[0] if isinstance(addr, tuple) else str(addr)
            except Exception:
                return str(addr)

        def connect(s, addr):
            h = _host(addr)
            if h not in LOOPBACK and not h.startswith("127."):
                rec.attempts.append(h)
                raise OSError(f"blocked by the offline self-test: {h}")
            return oc(s, addr)

        def connect_ex(s, addr):
            h = _host(addr)
            if h not in LOOPBACK and not h.startswith("127."):
                rec.attempts.append(h)
                return 111
            return ox(s, addr)
        socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
        return self

    def __exit__(self, *a):
        socket.socket.connect, socket.socket.connect_ex = self._orig


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_params as params
    import architect
    home = tempfile.mkdtemp(prefix="archoff_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    try:
        d = core.DEFAULTS
        check("offline is the default for code the harness runs, and the online switch is off", d["behavior"]["network"] == "offline" and d["online"]["enabled"] is False and d["online"]["allow_pip"] is False)
        check("the online switch is a real, validated setting (schema and defaults agree)", params.is_known_setting("online.enabled") and params.is_known_setting("online.allow_pip")
              and not params.selftest_defaults_match({k: v for k, v in _flatten(d).items() if not isinstance(v, dict)}))
        page = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        import re
        ext = re.findall(r'<(?:link|script|img|iframe)[^>]+(?:href|src)="(https?://[^"]+)"', page)
        check("the hub page references no web resource (no fonts, scripts or images from the internet)", not ext, ext)
        import model_hub
        src = open(os.path.join(HERE, "model_hub.py"), encoding="utf-8").read()
        m = re.search(r'Content-Security-Policy", "([^"]+)"', src)
        check("the hub's Content-Security-Policy allows 'self' only (no external hosts)", m and "http" not in m.group(1), m and m.group(1))

        with Recorder() as rec:
            import arch_doctor
            calls = []
            orig_run, orig_pip = arch_doctor.run, arch_doctor.pip_install
            arch_doctor.run = lambda root=None, hub_port=8766: {"ok": False, "root": root, "checks": [{"name": "pytest installed", "ok": False, "detail": "missing", "advisory": False}]}
            arch_doctor.pip_install = lambda mod, timeout=120: (calls.append(mod), (True, "installed"))[1]
            try:
                out = arch_doctor.fix(home)
            finally:
                arch_doctor.run, arch_doctor.pip_install = orig_run, orig_pip
            check("doctor --fix with pytest missing does NOT pip install by default; it says so and names the command", not calls and out["actions"] and "skipped" in out["actions"][0]["detail"] and "pip install pytest" in out["actions"][0]["detail"], out["actions"])
            cfg = {"online": {"allow_pip": True}}
            core.deep_merge(core.DEFAULTS, cfg)
            arch_doctor.run = lambda root=None, hub_port=8766: {"ok": False, "root": root, "checks": [{"name": "pytest installed", "ok": False, "detail": "missing", "advisory": False}]}
            arch_doctor.pip_install = lambda mod, timeout=120: (calls.append(mod), (True, "installed"))[1]
            try:
                out = arch_doctor.fix(home, install_missing=True)
            finally:
                arch_doctor.run, arch_doctor.pip_install = orig_run, orig_pip
            check("with the pip switch on the repair runs (the switch is real, not decoration)", calls == ["pytest"], calls)
            rc = architect.main(["catalog", "--verify"])
            check("catalog --verify is refused while online.enabled is off, without touching the network", rc == 1 and not rec.attempts, rec.attempts)
            import arch_eval
            rep = arch_eval.run(flows=["route"], fake=True, n=2, label="offline", say=lambda m: None)
            check("an oracle eval run opens no connection off this machine", rep["n"] == 2 and not rec.attempts, rec.attempts)
            import arch_hw
            arch_hw.detect()
            check("hardware detection opens no connection off this machine", not rec.attempts, rec.attempts)
        check("nothing on the default path tried to leave this machine (every connect was loopback)", not rec.attempts, rec.attempts)
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' OFFLINE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' OFFLINE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _flatten(d, p=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v and all(isinstance(x, (dict, list, str, int, float, bool, type(None))) for x in v.values()) and k not in ("roles", "hosts", "role_hosts", "role_overrides", "model_overrides", "tiers"):
            out.update(_flatten(v, p + k + "."))
        else:
            out[p + k] = v
    return out


if __name__ == "__main__":
    sys.exit(run())
