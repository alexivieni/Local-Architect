"""Self-test for the 2026-11-07 architecture review, wave 4 (docs/ADR-001): the hub. A finished job never writes into the next job's
output, job epochs are never reused after a restart, a settings file that cannot be read is never saved over, every read-change-write
of the settings holds one lock, a restart relaunches the hub itself on its own port, a route that answered never gets a second
response, the event stream carries ids so a reconnect does not replay the run, and the run report is a real dialog. No model, no
network, no real hub (the HTTP layer is covered by arch_hub_check)."""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archrev4_")
_KEYS = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST")
_OLD = {k: os.environ.get(k) for k in _KEYS}


def run():
    os.environ["ARCHITECT_HOME"] = os.path.join(_TMP, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "data")
    os.environ["ARCHITECT_NO_CACHE"] = os.environ["ARCHITECT_SELFTEST"] = "1"
    for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR"):
        os.makedirs(os.environ[k], exist_ok=True)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    old_home = core.HOME
    core.set_home(os.environ["ARCHITECT_HOME"])
    import model_hub as mh
    old_cfg = mh.CONFIG_FILE
    try:
        # ---- jobs
        j = mh.Job()
        check("job epochs start from the clock, so a number is never reused after the hub restarts", j.epoch > 10 ** 12, j.epoch)
        old = subprocess.Popen([sys.executable, "-c", "print('the old run said this')"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        old.wait()
        new = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            j.proc, j.epoch, j.buf, j.code = new, j.epoch + 1, "", None             # a new job has started...
            j._pump(old, j.epoch - 1)                                              # ...when the old job's pump reads its last output
            check("a finished job's last output never lands in the next job's buffer", "old run" not in j.buf, j.buf)
            check("and never sets the running job's exit code", j.code is None, j.code)
        finally:
            new.kill()
            new.wait()

        # ---- settings
        cfgp = os.path.join(_TMP, "architect_config.json")
        mh.CONFIG_FILE = cfgp
        with open(cfgp, "w", encoding="utf-8") as f:
            f.write('{"roles": {"generator": "x"}, ')                        # half written / hand-broken
        try:
            mh.read_config(strict=True)
            raised = False
        except ValueError as ex:
            raised = "not valid JSON" in str(ex)
        check("a settings file that is not valid JSON is refused for saving (it used to read as {} and be written over)", raised)
        check("an ordinary read of the same file still answers {} (pages keep working)", mh.read_config() == {})
        os.remove(cfgp)
        check("no settings file at all is simply empty, also for a save", mh.read_config(strict=True) == {})
        import architect_hub as ah
        check("every read-change-write settings route holds the one settings lock",
              {"/api/arch/settings", "/api/arch/hosts", "/api/arch/assign", "/api/arch/model_override", "/api/arch/preset", "/api/config"} <= mh.CONFIG_ROUTES,
              sorted(mh.CONFIG_ROUTES))
        hsrc = open(os.path.join(HERE, "architect_hub.py"), encoding="utf-8").read()
        check("those routes read the settings strictly", hsrc.count("cfg = mh.read_config(strict=True)") >= 6)

        # ---- restart
        cmd = ah.restart_command(["architect.py", "start"], port=8771)
        check("a hub started through `architect.py start` restarts as the hub itself, on the port it holds, with no new browser tab",
              os.path.basename(cmd[1]) == "architect_hub.py" and cmd[-3:] == ["--port", "8771", "--no-browser"], cmd)
        cmd2 = ah.restart_command([os.path.join(HERE, "architect_hub.py"), "--port", "8766", "--root", "x"], port=8770)
        check("a hub started directly keeps its own arguments and the port it really holds", "--root" in cmd2 and cmd2[-3:] == ["--port", "8770", "--no-browser"], cmd2)

        # ---- one response per request
        msrc = open(os.path.join(HERE, "model_hub.py"), encoding="utf-8").read()
        check("a route that already answered (`return self._send(...)`) never gets a second response",
              "self._responded = True" in msrc and "if self._responded:" in msrc)
        check("doctor_fix returns its result instead of answering twice", "return doctor.fix(root, hub_port)" in hsrc)
        check("the trace and why endpoints use the one run-id rule", hsrc.count("_RUN_RE.fullmatch(run_name)") >= 1 and "if not _RUN_RE.fullmatch(rid or \"\"):" in hsrc)

        # ---- the event stream
        check("the event stream sends an id with every frame and honours Last-Event-ID, so a reconnect does not replay the run",
              "id: {feed['epoch']}:{feed['next']}" in hsrc and 'self.headers.get("Last-Event-ID")' in hsrc)

        # ---- the page
        ui = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        rep = ui[ui.index("function showReport"):][:700]
        # 2026-11-08: every window is the one native-dialog component (modal(): showModal gives Esc, the focus trap and focus back)
        check("the run report is a real dialog (Esc closes it, focus moves into it and is trapped)",
              'modal("Run report: "' in rep and "d.showModal()" in ui[ui.index("function modal("):][:1200])
        # ---- a suite run on its own never sees the person's settings
        probe_dir = tempfile.mkdtemp(prefix="archrev4probe_")
        probe = os.path.join(probe_dir, "arch_selftest_probe_rev4.py")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("import sys, json\nsys.path.insert(0, %r)\nimport arch_core, model_hub\n"
                    "print(json.dumps({'cfg': arch_core.CONFIG_FILE, 'data': arch_core.data_dir(), 'mh_data': model_hub.DATA_DIR}))\n" % HERE)
        env = {k: v for k, v in os.environ.items() if k not in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST")}
        out = subprocess.run([sys.executable, probe], capture_output=True, text=True, timeout=60, env=env).stdout.strip().splitlines()
        got = json.loads(out[-1]) if out else {}
        real_cfg = os.path.join(HERE, "architect_config.json")
        check("a self-test started on its own gets a throwaway home: not the install's architect_config.json or hub_data",
              got and os.path.normcase(got["cfg"]) != os.path.normcase(real_cfg)
              and not os.path.normcase(got["data"]).startswith(os.path.normcase(os.path.join(HERE, "hub_data"))), got)
        check("the hub module follows the engine's data folder from the start", got and os.path.normcase(got["mh_data"]) == os.path.normcase(got["data"]), got)
        plain = os.path.join(probe_dir, "not_a_selftest.py")
        with open(plain, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.path.insert(0, %r)\nimport arch_core\nprint(arch_core.CONFIG_FILE)\n" % HERE)
        out2 = subprocess.run([sys.executable, plain], capture_output=True, text=True, timeout=60, env=env).stdout.strip().splitlines()
        check("an ordinary program (the app itself) still uses the install's own settings", out2 and os.path.normcase(out2[-1]) == os.path.normcase(real_cfg), out2)

        hud = open(os.path.join(HERE, "static", "hud", "architect_harness_hud.html"), encoding="utf-8").read()
        check("the HUD lights the closest node for the shrink guard, missing imports and the scanners, not pytest",
              "[/shrink|lost/i, 'replace_def']" in hud and "[/import|deps/i, 'wire_contract']" in hud)
    finally:
        mh.CONFIG_FILE = old_cfg
        core.set_home(old_home)
        for k, v in _OLD.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' REVIEW4 CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' REVIEW4 CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
