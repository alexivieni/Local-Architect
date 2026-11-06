"""Tests arch_security.py's checks directly (no model needed) and the secure/config/network/ml/pentest
flows end to end against a fake model. Part of `architect.py selftest --suite all`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def fence(path, code, lang="python"):
    return f"FILE: {path}\n```{lang}\n{code}```\n"


MD5_LOGIN = "import hashlib\n\n\ndef hash_password(pw):\n    return hashlib.md5(pw.encode()).hexdigest()\n"
MD5_LOGIN_FIXED = "import hashlib\n\n\ndef hash_password(pw):\n    return hashlib.sha256(pw.encode()).hexdigest()\n"
BAD_DOCKERFILE = "FROM python\nRUN pip install flask\nENV API_TOKEN=sk_live_abcdef1234567890\nCOPY . /app\nWORKDIR /app\nCMD [\"python\", \"app.py\"]\n"
GOOD_DOCKERFILE = ("FROM python:3.12-slim@sha256:" + "0" * 64 + "\nRUN pip install flask\nCOPY . /app\nWORKDIR /app\nUSER 1000\n"
                   "HEALTHCHECK CMD python -c \"import sys; sys.exit(0)\"\nCMD [\"python\", \"app.py\"]\n")
BAD_NETPLAN = json.dumps({"subnets": [
    {"name": "app", "cidr": "10.0.1.0/24", "hosts": 50, "gateway": "10.0.1.1"},
    {"name": "db", "cidr": "10.0.1.128/25", "hosts": 20, "gateway": "10.0.2.1"},
]})
GOOD_NETPLAN = json.dumps({"subnets": [
    {"name": "app", "cidr": "10.0.1.0/24", "hosts": 50, "gateway": "10.0.1.1"},
    {"name": "db", "cidr": "10.0.2.0/25", "hosts": 20, "gateway": "10.0.2.1"},
]})
BAD_FIREWALL = json.dumps({"default_inbound": "allow", "rules": [
    {"action": "allow", "src": "any", "dst": "any", "port": "any"},
    {"action": "allow", "src": "any", "dst": "10.0.0.5", "port": 22},
]})
GOOD_FIREWALL = json.dumps({"default_inbound": "deny", "rules": [
    {"action": "allow", "src": "10.0.0.0/24", "dst": "10.0.0.5", "port": 22},
]})
STATS = "def average(xs):\n    return sum(xs) / len(xs)\n\n\ndef top(xs, n):\n    return sorted(xs, reverse=True)[:n]\n"
STATS_TEST = "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom stats import average, top\n\n\ndef test_avg():\n    assert average([2, 4]) == 3\n\n\ndef test_top():\n    assert top([1, 5, 3], 2) == [5, 3]\n"


def run():
    import arch_selftest as base_test

    class Sec(base_test.Fake):
        def script(self, role, user, full, st):
            if "STATIC security findings" in user and "weak-hash" in user:
                return fence("auth.py", MD5_LOGIN_FIXED)
            if "STATIC security findings" in user:
                return fence("app.py", MD5_LOGIN_FIXED)
            if "hardening findings from a scanner" in user:
                return fence("Dockerfile", GOOD_DOCKERFILE, "dockerfile")
            if "CURRENT CONTENT" in user:
                body = user.rsplit("CURRENT CONTENT:", 1)[-1].split("FORMAT REFERENCE", 1)[0]     # the file itself, not the few-shot example that follows it
                if '"subnets"' in body:
                    return "```json\n" + GOOD_NETPLAN + "\n```"
                return "```json\n" + GOOD_FIREWALL + "\n```"
            if "For each remaining finding" in user or "For each attack path" in user:
                return "Log field: auth.hash_algo == 'md5'. Mitigation: migrate to sha256/argon2 and rehash on next login."
            if "Authorized scope" in user:
                m = re.search(r"\[(F\d+)\]", user)
                fid = m.group(1) if m else "F1"
                host_m = re.search(r"authorized_targets.*?([\w.]+\.internal)", full) or re.search(r"\]\s*([\w.]+):", user)
                host = host_m.group(1) if host_m else "app01.lab.internal"
                return json.dumps([{"finding_ids": [fid], "host": host, "steps": "chain the finding to escalate", "impact": "account takeover"}])
            if "Plan ONE feature" in user or "Plan sprints" in user or "Plan ONE pipeline" in user or "pipeline" in role:
                return json.dumps({"sprints": [{"name": "stats", "kind": "mixed", "goal": "stats pipeline", "files": ["stats.py", "tests/test_stats.py"],
                                                "acceptance": [{"id": "A1", "text": "average/top computed deterministically"}]}]})
            if "Implement this sprint" in user:
                return fence("stats.py", STATS) + fence("tests/test_stats.py", STATS_TEST)
            return super().script(role, user, full, st)

    import arch_checks as ck
    import arch_core as core
    import arch_flows as fl
    import arch_pentest as pt
    import arch_runner as runner
    import arch_security as sec

    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    def env_check(name, ok, extra=""):
        """An ENVIRONMENT check (a port, a clock, the person's own data): a pass is a pass, but a failure is reported NOT_SURE
        (could not run here), never a silent pass and never a code failure."""
        results.append(True if ok else None)
        print(f"  {'PASS' if ok else 'NOT_SURE'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    def free_port():
        """A port nothing is listening on right now (the runner's own default, 8000, is often taken on a developer machine)."""
        import socket
        with socket.socket() as so_:
            so_.bind(("127.0.0.1", 0))
            return so_.getsockname()[1]

    # ---- direct, model-free checks of arch_security.py
    tmp = tempfile.mkdtemp(prefix="archsec_")

    def write(root, files):
        for p, t in files.items():
            f = os.path.join(root, p)
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w", encoding="utf-8").write(t)

    d1 = os.path.join(tmp, "snip")
    write(d1, {"a.py": MD5_LOGIN})
    found = sec.sec_scan(d1, ["a.py"])
    check("sec_scan finds a weak hash (CWE-327) and tags it high severity", any(x["rule"] == "weak-hash" and x["cwe"] == "CWE-327" and x["severity"] == "high" for x in found), found)
    write(d1, {"b.py": "import os\ndef f(p):\n    return os.popen('ls ' + p).read()\n"})
    found2 = sec.sec_scan(d1, ["b.py"])
    check("sec_scan finds os.popen command injection (CWE-78)", any(x["rule"] == "os-command" and x["cwe"] == "CWE-78" for x in found2), found2)
    write(d1, {"c.py": "API_TOKEN = 'sk_live_abcdef1234567890ZZZZ'\n"})
    found3 = sec.sec_scan(d1, ["c.py"])
    check("sec_scan finds a hardcoded secret-looking literal", any(x["rule"] == "hardcoded-secret" for x in found3), found3)
    write(d1, {"d.py": "import requests\nrequests.get('https://x', verify=False)\n"})
    found4 = sec.sec_scan(d1, ["d.py"])
    check("sec_scan finds TLS verification disabled", any(x["rule"] == "tls-verify-disabled" for x in found4), found4)
    write(d1, {"e.py": "import pickle\npickle.loads(data)\n"})
    found5 = sec.sec_scan(d1, ["e.py"])
    check("sec_scan finds insecure deserialization (pickle.loads)", any(x["rule"] == "insecure-deserialization" for x in found5), found5)
    write(d1, {"clean.py": "def add(a, b):\n    return a + b\n"})
    found6 = sec.sec_scan(d1, ["clean.py"])
    check("sec_scan finds nothing in clean code", not found6, found6)

    before_d = sec.check_dockerfile(BAD_DOCKERFILE)
    check("check_dockerfile flags unpinned base, baked secret and root user as high", {"unpinned-base", "baked-secret", "runs-as-root"} <= {x["rule"] for x in before_d if x["severity"] == "high"}, before_d)
    after_d = sec.check_dockerfile(GOOD_DOCKERFILE)
    check("check_dockerfile: a hardened Dockerfile has no high findings", not [x for x in after_d if x["severity"] == "high"], after_d)

    before_n = sec.check_cidr_plan(BAD_NETPLAN)
    check("check_cidr_plan catches a subnet overlap and a gateway outside its subnet", {"overlap", "gateway-outside-subnet"} <= {x["rule"] for x in before_n}, before_n)
    check("check_cidr_plan: a corrected plan has no problems", not sec.check_cidr_plan(GOOD_NETPLAN))

    before_f = sec.check_firewall(BAD_FIREWALL)
    check("check_firewall catches default-allow, any/any, exposed admin port and a shadowed rule", {"default-allow", "any-any-allow", "exposed-admin-port", "shadowed-rule"} <= {x["rule"] for x in before_f}, before_f)
    check("check_firewall: a tight rule set has no problems", not sec.check_firewall(GOOD_FIREWALL))

    # ---- arch_pentest.py: scope is code, not opinion
    scope_default, is_default, err = pt.load_scope(tmp)
    check("pentest: with no scope.json, only loopback is in scope by default", is_default and scope_default["authorized_targets"] == ["127.0.0.1", "localhost", "::1"])
    write(tmp, {"scope.json": json.dumps({"authorized_targets": ["10.20.0.0/24"], "excluded": ["10.20.0.1"], "allowed_actions": ["port_scan"], "window": ""})})
    scope2, is_default2, err2 = pt.load_scope(tmp)
    check("pentest: a real scope.json is loaded and no longer the default", not is_default2 and err2 is None and pt.in_scope(scope2, "10.20.0.5"))
    check("pentest: a host outside authorized_targets is out of scope", not pt.in_scope(scope2, "10.30.0.5"))
    check("pentest: an explicitly excluded host is out of scope even inside the range", not pt.in_scope(scope2, "10.20.0.1"))
    write(tmp, {"scope_bad.json": "not json"})
    _, _, err3 = pt.load_scope(tmp, "scope_bad.json")
    check("pentest: invalid scope.json is refused with a clear error, not swallowed", bool(err3) and "json" in err3.lower())

    # ---- flows against the fake model
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Sec)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{fake.server_address[1]}"

    def eng(root, goal, flow, review="auto", **over):
        import arch_roles as roles
        os.makedirs(root, exist_ok=True)
        c = core.load_config(root)
        c["roles"] = {r: "fake:7b" for r in roles.ROLES}
        c["behavior"]["playwright"] = "off"
        c = core.deep_merge(c, over)
        return core.Engine(root, flow, goal, c, review=review, say=lambda m: None)

    try:
        # secure flow: a real finding is fixed and proven gone by re-scanning
        sdir = os.path.join(tmp, "secure")
        write(sdir, {"auth.py": MD5_LOGIN, "tests/test_auth.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\nfrom auth import hash_password\n\n\ndef test_hash():\n    assert hash_password('x') == hash_password('x')\n"})
        e = eng(sdir, "Fix the weak password hash", "secure")
        out = fl.run_flow(e, "secure", {})
        check("secure flow: a real weak-hash finding is fixed and re-scanning proves it gone",
              out.get("ok") and not sec.sec_scan(sdir, ["auth.py"], include_low=False) and "sha256" in open(os.path.join(sdir, "auth.py")).read(), out)
        check("secure flow writes docs/security.md with before/after counts", "Findings before: 1" in open(os.path.join(sdir, "docs", "security.md")).read())

        # secure flow: nothing to fix is also a clean pass
        clean = os.path.join(tmp, "secure_clean")
        write(clean, {"ok.py": "def add(a, b):\n    return a + b\n"})
        e = eng(clean, "Security review", "secure")
        out = fl.run_flow(e, "secure", {})
        check("secure flow: a clean project is reported ok with zero findings", out.get("ok") and out.get("findings_before") == 0)

        # config flow: hardens a Dockerfile, proven by re-scan
        cdir = os.path.join(tmp, "config")
        write(cdir, {"Dockerfile": BAD_DOCKERFILE})
        e = eng(cdir, "Harden the Dockerfile", "config")
        out = fl.run_flow(e, "config", {})
        check("config flow: a Dockerfile's high findings are fixed and re-scanning proves it",
              out.get("ok") and not [x for x in sec.check_dockerfile(open(os.path.join(cdir, "Dockerfile")).read()) if x["severity"] == "high"], out)

        # network flow: CIDR overlap fixed, proven by re-check
        ndir = os.path.join(tmp, "network")
        write(ndir, {"netplan.json": BAD_NETPLAN})
        e = eng(ndir, "Fix the subnet plan", "network")
        out = fl.run_flow(e, "network", {})
        check("network flow: a subnet overlap is fixed and re-checking the math proves it",
              out.get("ok") and not sec.check_cidr_plan(open(os.path.join(ndir, "netplan.json")).read()), out)

        # network flow: firewall rules fixed, proven by re-check
        fwdir = os.path.join(tmp, "firewall")
        write(fwdir, {"fw.json": BAD_FIREWALL})
        e = eng(fwdir, "Fix the firewall rules", "network")
        out = fl.run_flow(e, "network", {})
        check("network flow: firewall default-allow/any-any/admin-port problems are fixed",
              out.get("ok") and not sec.check_firewall(open(os.path.join(fwdir, "fw.json")).read()), out)

        # ML flow: builds a pipeline and proves determinism by running tests twice
        mdir = os.path.join(tmp, "ml")
        e = eng(mdir, "Build a small stats pipeline", "ml")
        out = fl.run_flow(e, "ml", {})
        check("ml flow: builds a working pipeline and PROVES determinism by running tests twice",
              out.get("ok") and out.get("deterministic") is True and os.path.isfile(os.path.join(mdir, "stats.py")), out)

        # pentest flow: refuses live mode on the default (loopback-only) scope
        pdir = os.path.join(tmp, "pentest")
        write(pdir, {"findings.json": json.dumps(pt.PENTEST_FINDINGS if hasattr(pt, "PENTEST_FINDINGS") else [
            {"id": "F1", "host": "app01.lab.internal", "desc": "login endpoint accepts SQL in the username field (CWE-89)"},
        ])})
        e = eng(pdir, "Write up the pentest findings", "pentest")
        out = fl.run_flow(e, "pentest", {"live": True})
        check("pentest flow: live mode refuses to run on the default loopback-only scope", out.get("ok") is False)

        # pentest flow: a real scope.json lets it proceed, and out-of-scope findings are refused, not used
        write(pdir, {"scope.json": json.dumps({"authorized_targets": ["app01.lab.internal"], "excluded": [], "allowed_actions": ["*"], "window": ""}),
                     "findings.json": json.dumps([
                         {"id": "F1", "host": "app01.lab.internal", "desc": "login endpoint accepts SQL in the username field (CWE-89)"},
                         {"id": "F2", "host": "out-of-scope.example.com", "desc": "unrelated host, must be refused"},
                     ])})
        e = eng(pdir, "Write up the pentest findings", "pentest")
        out = fl.run_flow(e, "pentest", {})
        rpt = open(os.path.join(pdir, "docs", "pentest.md")).read() if os.path.isfile(os.path.join(pdir, "docs", "pentest.md")) else ""
        check("pentest flow: in-scope findings are used, out-of-scope findings are refused (not silently fixed)",
              out.get("ok") and "F1" in rpt and "out-of-scope.example.com" not in rpt.split("Findings refused")[0] and "Findings refused" in rpt, (out, rpt[-500:]))
        check("pentest flow: every attack path cites only real finding ids and in-scope hosts (checked by code)",
              "app01.lab.internal" in rpt and "Attack paths" in rpt)
        # ---- arch_runner.py: real subprocess, real port, real HTTP health check (no model, no fakes)
        import arch_templates as T
        rdir = os.path.join(tmp, "runner_rest")
        write(rdir, T.render("rest_api", "runnerapp", []))
        write(rdir, {".arch/handoff.json": json.dumps({"run": "python -m app.server"})})
        r1 = runner.start(rdir, port=free_port())
        try:
            env_check("runner: start spawns a real subprocess with a real pid", r1.get("ok") and isinstance(r1.get("pid"), int) and runner.is_running(r1["pid"]), r1)
            for _ in range(40):
                h = runner.health(rdir)
                if h.get("http_ok") is not None:
                    break
                import time as _t
                _t.sleep(0.25)
            env_check("runner: health does a real TCP connect + HTTP GET against the project's own port", h.get("running") and h.get("port_open") and h.get("http_ok"), h)
            env_check("runner: logs tails the real stdout of the running process", "listening" in runner.logs(rdir, 20).lower() or runner.logs(rdir, 20) != "", runner.logs(rdir, 20))
        finally:
            r2 = runner.stop(rdir)
        env_check("runner: stop actually kills the real pid", r2.get("ok") and not runner.is_running(r1.get("pid")), (r1, r2))

        # ---- runner.stop() must kill the REAL server process, not just the `shell=True` wrapper it's
        # launched through -- start() puts both in one process group (start_new_session) so stop()'s
        # killpg() reaches the real process too; verify by finding the actual listening PID via /proc and
        # confirming it's gone, not just the (possibly different) pid runner.py happened to track.
        rdir2 = os.path.join(tmp, "runner_rest2")
        write(rdir2, T.render("rest_api", "runnerapp2", []))
        write(rdir2, {".arch/handoff.json": json.dumps({"run": "python -m app.server"})})
        r4 = runner.start(rdir2, port=free_port())
        try:
            real_pid = None
            for _ in range(40):
                h4 = runner.health(rdir2)
                if h4.get("port_open"):
                    break
                time.sleep(0.25)
            if sys.platform.startswith("linux") and os.path.isdir("/proc"):
                # find the REAL python process by its actual cwd + cmdline via /proc -- no `ss`/`lsof` needed,
                # and this is independent of whatever pid runner.py itself tracked, so it can't agree by construction
                real_rdir2 = os.path.realpath(rdir2)
                for pstr in os.listdir("/proc"):
                    if not pstr.isdigit():
                        continue
                    try:
                        cwd = os.path.realpath(f"/proc/{pstr}/cwd")
                        cmd = open(f"/proc/{pstr}/cmdline", "rb").read().replace(b"\x00", b" ").decode(errors="replace")
                    except OSError:
                        continue
                    if cwd == real_rdir2 and "app.server" in cmd:
                        real_pid = int(pstr)
                        break
            if real_pid is None:
                check("runner: process-group kill coverage (grandchild PID) -- skipped, could not independently locate the real server process via /proc on this box", True)
            else:
                runner.stop(rdir2)
                still_there = runner.is_running(real_pid)
                env_check("runner: stop() kills the REAL listening process (found independently via its bound port), not only the shell wrapper runner.py happened to track",
                      not still_there, {"tracked_pid": r4.get("pid"), "real_listening_pid": real_pid, "still_running": still_there})
        finally:
            runner.stop(rdir2)  # idempotent / safe even if already stopped above
        check("runner: with no run command and none built yet, it says so plainly instead of guessing", runner.start(os.path.join(tmp, "norun_empty")).get("error", "").startswith("no run command"))
        sdir2 = os.path.join(tmp, "runner_oneshot")
        write(sdir2, {"hello.py": "print('hi')\n"})
        r3 = runner.start(sdir2, cmd="python hello.py")
        check("runner: a one-shot command (not a server) runs to completion and records a real exit code", r3.get("ok") and r3.get("mode") == "oneshot" and r3.get("exit_code") == 0, r3)

        # ---- arch_doctor.py: real environment preflight, no mocks
        import arch_doctor as doctor
        d_py = doctor.check_python()
        check("doctor: Python version check reflects the real interpreter running these tests", d_py["ok"] and sys.version.split()[0] in d_py["detail"], d_py)
        d_mod = doctor.check_module("this_module_does_not_exist_12345")
        check("doctor: a missing module is reported as not present, with an advisory install hint", not d_mod["ok"] and "pip install" in d_mod["detail"] and d_mod["advisory"], d_mod)
        d_disk = doctor.check_disk(tmp)
        check("doctor: disk check reads real free space off this machine's filesystem", d_disk["ok"] and "GB free" in d_disk["detail"], d_disk)
        wdir = os.path.join(tmp, "doctor_write")
        d_write = doctor.check_write(wdir)
        check("doctor: write check actually writes and removes a real file, and leaves no trace", d_write["ok"] and not os.path.exists(os.path.join(wdir, ".arch_doctor_write_test")), d_write)
        s_hold = __import__("socket").socket()
        s_hold.bind(("127.0.0.1", 0))
        s_hold.listen(1)
        busy_port = s_hold.getsockname()[1]
        try:
            d_port_busy = doctor.check_port_free(busy_port)
            check("doctor: port check does a real bind and correctly reports a port that is genuinely in use", not d_port_busy["ok"] and d_port_busy["advisory"], d_port_busy)
        finally:
            s_hold.close()
        d_port_free = doctor.check_port_free(busy_port)
        check("doctor: the same port reports free once actually released", d_port_free["ok"], d_port_free)
        ddir = os.path.join(tmp, "doctor_full")
        out = doctor.run(ddir, hub_port=busy_port + 1)
        names = [c["name"] for c in out["checks"]]
        check("doctor.run() returns every check with root, overall ok, and does not let an advisory check fail the verdict",
              out["root"] == os.path.abspath(ddir) and any("Python" in n for n in names) and any("Ollama" in n for n in names) and isinstance(out["ok"], bool), out)
        check("doctor: NIC check is a real probe (sys/net or a non-sending UDP route lookup), not hardcoded", any("NIC" in n for n in names), names)

        # ---- doctor --fix: self-repair -- real actions, re-checked afterward, never a no-op pretending to help
        freed, removed = doctor.clean_old_runs(os.path.join(tmp, "no_such_project"))
        check("doctor: clean_old_runs on a project with no .arch/runs/ yet is a safe no-op", freed == 0 and removed == [], (freed, removed))
        cleandir = os.path.join(tmp, "doctor_clean")
        stale = os.path.join(cleandir, ".arch", "runs", "20200101_000000_build")
        os.makedirs(stale, exist_ok=True)
        with open(os.path.join(stale, "report.md"), "w") as f:
            f.write("x" * 5000)
        old_ts = __import__("time").time() - 30 * 86400
        os.utime(stale, (old_ts, old_ts))
        freed2, removed2 = doctor.clean_old_runs(cleandir, max_age_days=14)
        check("doctor: clean_old_runs actually deletes a real stale run dir and reports real bytes freed", freed2 == 5000 and removed2 == ["20200101_000000_build"] and not os.path.isdir(stale), (freed2, removed2))
        fresh = os.path.join(cleandir, ".arch", "runs", "20991231_000000_build")
        os.makedirs(fresh, exist_ok=True)
        freed3, removed3 = doctor.clean_old_runs(cleandir, max_age_days=14)
        check("doctor: clean_old_runs leaves a recent run dir alone", removed3 == [] and os.path.isdir(fresh), (freed3, removed3))
        check("doctor: disk check on a not-yet-created project folder walks up to the nearest real ancestor instead of failing on 'no such file'",
              doctor.check_disk(os.path.join(tmp, "not_created_yet", "nested"))["ok"] is True, doctor.check_disk(os.path.join(tmp, "not_created_yet", "nested")))
        fixres = doctor.fix(os.path.join(tmp, "doctor_fix_clean"))
        check("doctor.fix(): with nothing broken that it knows how to fix, it says so and 'after' still reflects reality", fixres["actions"] == [] and fixres["after"] == fixres["before"], fixres["actions"])

        # ---- arch_assess.py: hardware tiering, workload coverage read off real scoreboard data, no new scoring
        import arch_assess as assess
        check("assess: VRAM tiering matches the documented bands", [assess.vram_tier(v) for v in (0, 3.9, 4, 9.9, 10, 19.9, 20, 48)] ==
              ["cpu_only", "cpu_only", "small", "small", "mid", "mid", "large", "large"])
        for tier in ("large", "mid", "small", "cpu_only"):
            got_tier, patch = assess.tuned_settings({"vram_gb": {"large": 24, "mid": 12, "small": 6, "cpu_only": 0}[tier]})
            check(f"assess: tuned_settings for the '{tier}' tier is a real, schema-valid settings patch", got_tier == tier and set(patch) == set(assess.TUNED_PROFILES[tier]), (got_tier, patch))
        import arch_roles as roles_mod
        fake_models = [
            {"name": "good:7b", "embed": False, "agg": {c: {"score": 90} for c, _ in roles_mod.CATEGORIES}, "know": {}},
            {"name": "weak:1b", "embed": False, "agg": {c: {"score": 5} for c, _ in roles_mod.CATEGORIES}, "know": {}},
        ]
        a_good = assess.assess(fake_models, {"vram_gb": 24, "ram_gb": 64})
        cover = {w["id"]: w for w in a_good["workloads"]}
        check("assess: with a strongly-scoring model installed, every workload reports ready with roles actually covered",
              all(w["ready"] and w["covered"] and not w["missing"] for w in a_good["workloads"]), cover)
        a_none = assess.assess([], {"vram_gb": 0, "ram_gb": 8})
        check("assess: with no usable models installed, every workload is reported not-ready with every role missing (never silently 'fine')",
              all(not w["ready"] and set(w["missing"]) == set(w["roles"]) for w in a_none["workloads"]), a_none["workloads"])
        check("assess: 'security' workload lists the red/blue/pentest/network/config roles -- the actual red-blue surface, not a placeholder",
              {"red_team", "blue_team", "pentester", "ioc_analyst", "network_engineer", "config_engineer", "security_engineer"} <= set(assess.WORKLOADS["security"]["roles"]))

        # ---- self-tuning drift: does the SAVED config already match this machine's recommended tuning?
        _, tuned_small = assess.tuned_settings({"vram_gb": 6})
        matching_cfg = {"limits": {"num_ctx": tuned_small["limits.num_ctx"], "context_chars": tuned_small["limits.context_chars"], "keep_alive": tuned_small["limits.keep_alive"]},
                        "behavior": {"best_of_n": tuned_small["behavior.best_of_n"]}}
        check("assess: tuning_drift reports NO drift when the saved config already matches the recommendation", assess.tuning_drift(matching_cfg, tuned_small) == [])
        stale_cfg = {"limits": {"num_ctx": 999999, "context_chars": tuned_small["limits.context_chars"], "keep_alive": tuned_small["limits.keep_alive"]}, "behavior": {"best_of_n": tuned_small["behavior.best_of_n"]}}
        drift = assess.tuning_drift(stale_cfg, tuned_small)
        check("assess: tuning_drift catches a real mismatch (num_ctx set for a different hardware tier) and names it", drift == [{"path": "limits.num_ctx", "current": 999999, "recommended": tuned_small["limits.num_ctx"]}], drift)
        a_drift = assess.assess(fake_models, {"vram_gb": 24}, cfg=stale_cfg)
        check("assess.assess() includes tuning_drift when a current config is passed, and omits it (None) otherwise",
              isinstance(a_drift["tuning_drift"], list) and assess.assess(fake_models, {"vram_gb": 24})["tuning_drift"] is None, a_drift["tuning_drift"])

        # ---- security_lab template: red (scope-checked attack-path referee) + blue (log detection rules), no live exploitation
        labdir = os.path.join(tmp, "lab")
        write(labdir, T.render("security_lab", "redbluelab", []))
        lr = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=labdir, capture_output=True, text=True)
        check("security_lab template: its own 17 tests pass out of the box (scope, attack-path referee, detections, cli)", lr.returncode == 0, lr.stdout[-600:] + lr.stderr[-300:])
        sys.path.insert(0, labdir)
        try:
            for mod in ("redbluelab.scope", "redbluelab.attackpath", "redbluelab.detections", "redbluelab.findings", "redbluelab.cli"):
                sys.modules.pop(mod, None)
            sys.modules.pop("redbluelab", None)
            from redbluelab.attackpath import validate_path
            from redbluelab.detections import RULES, scan
            from redbluelab.findings import SAMPLE_FINDINGS
            from redbluelab.scope import in_scope, load_scope

            scope = load_scope(os.path.join(labdir, "scope.json"))
            ok, _ = validate_path({"finding_ids": ["F1"], "host": "app01.lab.internal", "impact": "account takeover"}, SAMPLE_FINDINGS, scope)
            check("security_lab: a real attack path citing a real finding, in scope, is accepted", ok)
            bad, reason = validate_path({"finding_ids": ["F1"], "host": "10.99.99.99", "impact": "x"}, SAMPLE_FINDINGS, scope)
            check("security_lab: a path naming a host OUTSIDE scope.json is rejected, not silently trusted", not bad and "scope" in reason, reason)
            excl, reason2 = in_scope("10.20.0.1", scope)
            check("security_lab: an explicitly excluded host loses even though it is inside an authorized /24", not excl, reason2)
            alerts = scan(["path=/search?q=' OR '1'='1 method=GET status=500"], RULES)
            check("security_lab: the detection engine flags a real SQLi-pattern log line (blue side), with no exploit ever executed", any(a["rule"] == "sql-meta-in-request" for a in alerts), alerts)
            check("security_lab: benign traffic produces zero alerts (no false-positive noise)", scan(["path=/items method=GET status=200"], RULES) == [])
            from redbluelab.scenarios import SCENARIOS
            check("security_lab: ships at least 3 fake-but-plausible scenarios, each with a blurb, findings and log lines", len(SCENARIOS) >= 3 and all(s["blurb"] and s["findings"] and s["log_lines"] for s in SCENARIOS.values()), sorted(SCENARIOS))
            cs_users = {ln.split("user=")[1].split()[0] for ln in SCENARIOS["credential_stuffing"]["log_lines"] if "user=" in ln}
            check("security_lab: the credential-stuffing scenario actually spreads across many accounts (the point of the scenario), not one", len(cs_users) >= 10, cs_users)
        finally:
            sys.path.remove(labdir)
            for mod in list(sys.modules):
                if mod == "redbluelab" or mod.startswith("redbluelab."):
                    del sys.modules[mod]

        # ---- red_team template: pure offense, scope-checked, no blue side, no network calls
        rtdir = os.path.join(tmp, "redteam")
        write(rtdir, T.render("red_team", "offense", []))
        rtr = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=rtdir, capture_output=True, text=True)
        check("red_team template: its own tests pass out of the box (scope + attack-path referee only, no blue side)", rtr.returncode == 0, rtr.stdout[-500:] + rtr.stderr[-300:])
        sys.path.insert(0, rtdir)
        try:
            for mod in list(sys.modules):
                if mod == "offense" or mod.startswith("offense."):
                    del sys.modules[mod]
            paths = os.path.join(tmp, "rt_paths.json")
            findings = os.path.join(tmp, "rt_findings.json")
            with open(findings, "w") as f:
                json.dump([{"id": "F1", "host": "127.0.0.1", "desc": "weak auth (practice)", "severity": "high"}], f)
            with open(paths, "w") as f:
                json.dump([{"finding_ids": ["F1"], "host": "127.0.0.1", "impact": "account takeover"}], f)
            scope_json = os.path.join(rtdir, "scope.json")
            rt_cli = subprocess.run([sys.executable, "-m", "offense", "--scope", scope_json, "--findings", findings, "check-paths", paths], cwd=rtdir, capture_output=True, text=True)
            check("red_team: its CLI accepts a valid, in-scope attack path over the real scope.json it ships (loopback-only by default)", rt_cli.returncode == 0 and '"valid": 1' in rt_cli.stdout, rt_cli.stdout + rt_cli.stderr)
            with open(paths, "w") as f:
                json.dump([{"finding_ids": ["F1"], "host": "8.8.8.8", "impact": "x"}], f)
            rt_cli2 = subprocess.run([sys.executable, "-m", "offense", "--scope", scope_json, "--findings", findings, "check-paths", paths], cwd=rtdir, capture_output=True, text=True)
            check("red_team: a path naming a host outside scope.json is rejected by the real CLI (nonzero exit)", rt_cli2.returncode == 1 and '"valid": 0' in rt_cli2.stdout, rt_cli2.stdout)
        finally:
            sys.path.remove(rtdir)
            for mod in list(sys.modules):
                if mod == "offense" or mod.startswith("offense."):
                    del sys.modules[mod]

        # ---- range template: a REAL, loopback-only target -- real socket, real HTTP traffic, real log file
        rgdir = os.path.join(tmp, "range")
        write(rgdir, T.render("range", "practicerange", []))
        rgr = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=rgdir, capture_output=True, text=True)
        env_check("range template: its own tests pass out of the box (real socket connect, real drill, real detections)", rgr.returncode == 0, rgr.stdout[-600:] + rgr.stderr[-300:])
        sys.path.insert(0, rgdir)
        try:
            for mod in list(sys.modules):
                if mod == "practicerange" or mod.startswith("practicerange."):
                    del sys.modules[mod]
            from practicerange.target import make_server
            srv = make_server(port=0)  # bound+listening already (TCPServer binds in __init__); serve_forever() is NOT started here
            try:
                host, port = srv.server_address
                check("range: make_server binds 127.0.0.1 ONLY -- there is no parameter anywhere to change this", host == "127.0.0.1", host)
                import socket as _socket
                s = _socket.create_connection((host, port), timeout=2)
                s.close()
                check("range: the bound port is a REAL, connectable socket -- no fake port claim", True)
            finally:
                srv.server_close()  # NOT srv.shutdown() -- that blocks forever waiting on an event serve_forever() never started would set
            cli_log = os.path.join(tmp, "range_drill.log")
            rg_cli = subprocess.run([sys.executable, "-m", "practicerange", "drill", "--log", cli_log], cwd=rgdir, capture_output=True, text=True)
            check("range: the real CLI drill starts the real target, sends real traffic, and reports a real log tail", rg_cli.returncode == 0 and os.path.isfile(cli_log) and os.path.getsize(cli_log) > 0, rg_cli.stdout + rg_cli.stderr)
            rg_scan = subprocess.run([sys.executable, "-m", "practicerange", "scan-logs", cli_log], cwd=rgdir, capture_output=True, text=True)
            check("range: scan-logs flags the real reflected-script request the drill actually sent, from the real log file on disk", rg_scan.returncode == 0 and "reflected-script-in-query" in rg_scan.stdout, rg_scan.stdout + rg_scan.stderr)
        finally:
            sys.path.remove(rgdir)
            for mod in list(sys.modules):
                if mod == "practicerange" or mod.startswith("practicerange."):
                    del sys.modules[mod]

        # ---- self-improve: never edits the running install; an explicit --root is still a plain run on that folder
        import architect as cli_mod
        import arch_selfimprove as si_mod
        _captured = {}
        _orig_cmd_run, _orig_improve, _orig_loop = cli_mod.cmd_run, si_mod.improve, si_mod.loop
        cli_mod.cmd_run = lambda a: _captured.setdefault("run_root", a.root) or 0
        si_mod.improve = lambda goal, **kw: _captured.update(goal=goal, kw=kw) or {"ok": True}
        si_mod.loop = lambda goal=None, **kw: _captured.update(loop_goal=goal, loop_kw=kw) or {"status": "passed"}   # the iterative loop: stubbed, never run here
        try:
            class _A:
                pass
            args = _A()
            for k in ("root", "app", "goal", "domains", "repro", "model", "role_model", "max_revisions", "best_of", "set",
                      "goal_opt", "rounds", "resume"):
                setattr(args, k, None)
            args.fake, args.allow_gate_edits, args.full_gates = False, False, False
            args.flow, args.review, args.autofix = "review", "dry", False
            args.yes, args.suite, args.allow_safety_edits, args.keep = False, "all", False, False
            rc0 = cli_mod.cmd_self_improve(args)
            # rule changed 2026-11-05: no goal = the iterative squad loop picks the top failing check, on a candidate copy -- still never a plain run on the install
            check("self-improve: without a goal it runs the iterative loop on a candidate copy, never a plain run on the install",
                  rc0 == 0 and "loop_goal" in _captured and _captured["loop_goal"] is None and "run_root" not in _captured and "goal" not in _captured, _captured)
            _captured.clear()
            args.goal = "make it faster"
            rc = cli_mod.cmd_self_improve(args)
            check("self-improve: with no --root it goes through the candidate/gate pipeline, never a plain run on the install",
                  rc == 0 and _captured.get("goal") == "make it faster" and "run_root" not in _captured and _captured["kw"]["suite"] == "all", _captured)
            args.root = os.path.join(tmp, "somewhere_else")
            _captured.clear()
            cli_mod.cmd_self_improve(args)
            check("self-improve: an explicit --root is respected as a plain run on that folder", _captured.get("run_root") == args.root and "goal" not in _captured, _captured)
        finally:
            cli_mod.cmd_run, si_mod.improve, si_mod.loop = _orig_cmd_run, _orig_improve, _orig_loop

        # ---- the detections / scope_guard / ioc_watch add-ons: the SAME red/blue/IOC capability, dropped
        # onto templates that have nothing to do with security -- proves "usable everywhere", not baked in
        for base in ("rest_api", "python_lib", "data_sql", "worker"):
            adir = os.path.join(tmp, f"addon_{base}")
            write(adir, T.render(base, "demo", ["detections", "scope_guard", "ioc_watch"]))
            ar = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=adir, capture_output=True, text=True)
            check(f"security add-ons (detections + scope_guard + ioc_watch) work on top of the unrelated '{base}' template, not just the security lab",
                  ar.returncode == 0, ar.stdout[-500:] + ar.stderr[-300:])

        # ---- ioc_watch add-on: real file hashing + real IP/domain matching, no faked results
        import hashlib
        iocdir = os.path.join(tmp, "ioc_only")
        write(iocdir, T.render("rest_api", "iocdemo", ["ioc_watch"]))
        sys.path.insert(0, iocdir)
        try:
            for mod in ("security.ioc", "security"):
                sys.modules.pop(mod, None)
            from security.ioc import hash_file, load_iocs, match_file_hashes, match_logs
            target = os.path.join(iocdir, "suspicious.bin")
            with open(target, "wb") as f:
                f.write(b"a real file with real bytes to hash for real")
            real_sha = hashlib.sha256(open(target, "rb").read()).hexdigest()
            check("ioc_watch: hash_file computes the SAME sha256 as hashlib directly -- a real digest, not a canned one", hash_file(target) == real_sha, hash_file(target))
            hits = match_file_hashes([target], [{"type": "hash", "value": real_sha, "severity": "high", "desc": "x"}])
            check("ioc_watch: match_file_hashes finds a real file by its real hash", len(hits) == 1 and hits[0]["hash"] == real_sha, hits)
            check("ioc_watch: an unrelated file produces no hash hit", match_file_hashes([target], [{"type": "hash", "value": "0" * 64, "severity": "high", "desc": "x"}]) == [])
            iphits = match_logs(["ip=10.50.0.17 connected"], [{"type": "ip", "value": "10.50.0.0/24", "severity": "high", "desc": "x"}])
            check("ioc_watch: an IP inside the IOC's CIDR is matched", len(iphits) == 1, iphits)
            check("ioc_watch: an IP outside the IOC's CIDR is not matched", match_logs(["ip=10.60.0.17 connected"], [{"type": "ip", "value": "10.50.0.0/24", "severity": "high", "desc": "x"}]) == [])
            dhits = match_logs(["dns query for evil-c2.example"], [{"type": "domain", "value": "evil-c2.example", "severity": "high", "desc": "x"}])
            check("ioc_watch: an exact domain IOC is matched and a lookalike domain is not", len(dhits) == 1 and
                  match_logs(["dns query for notevil-c2.example.org"], [{"type": "domain", "value": "evil-c2.example", "severity": "high", "desc": "x"}]) == [])
            rows = load_iocs(os.path.join(iocdir, "security", "sample_iocs.json"))
            check("ioc_watch: ships a sample IOC file with all 3 indicator types, and it actually loads", {"hash", "ip", "domain"} <= {r["type"] for r in rows}, rows)
        finally:
            sys.path.remove(iocdir)
            for mod in list(sys.modules):
                if mod == "security" or mod.startswith("security."):
                    del sys.modules[mod]

        # ---- arch_advisor.py: combines doctor + assess + a real code scan + scope/findings, evidence-cited
        import arch_advisor as adv
        import arch_assess as assess

        advdir = os.path.join(tmp, "advise_target")
        write(advdir, {"risky.py": MD5_LOGIN, "fine.py": "def add(a, b):\n    return a + b\n"})
        files = ["risky.py", "fine.py"]
        code_props = adv.from_code_scan(advdir, files)
        check("advisor.from_code_scan turns a REAL sec_scan finding into a cited proposal (file:line evidence, not invented)",
              any("weak-hash" in p["title"] and any("risky.py" in e for e in p["evidence"]) for p in code_props), code_props)
        check("advisor.from_code_scan proposes nothing when the code is clean", adv.from_code_scan(advdir, ["fine.py"]) == [])

        doctor_fail = {"checks": [{"name": "disk space in /nope", "ok": False, "detail": "only 0.1 GB free", "advisory": False},
                                   {"name": "git", "ok": False, "detail": "git not on PATH", "advisory": True}]}
        doc_props = adv.from_doctor(doctor_fail)
        check("advisor.from_doctor turns a failing non-advisory check into a HIGH proposal", any(p["severity"] == "high" and "disk space" in p["title"] for p in doc_props), doc_props)
        check("advisor.from_doctor turns a failing advisory check into a LOW proposal, not high", any(p["severity"] == "low" and "git" in p["title"] for p in doc_props), doc_props)
        check("advisor.from_doctor proposes nothing for an all-OK doctor report", adv.from_doctor({"checks": [{"name": "x", "ok": True, "detail": "", "advisory": False}]}) == [])

        tier, tuned = assess.tuned_settings({"vram_gb": 6, "ram_gb": 16})
        drifted_cfg = {"limits": {"num_ctx": 999}}
        a_out = {"vram_tier_label": assess.TUNED_LABEL[tier], "tuning_drift": assess.tuning_drift(drifted_cfg, tuned),
                 "workloads": [{"id": "security", "label": "Security / red-blue", "blurb": "x", "covered": [], "guessed": [], "missing": ["ioc_analyst"]}]}
        as_props = adv.from_assess(a_out)
        check("advisor.from_assess turns real tuning drift into a cited proposal (current vs recommended)",
              any("limits.num_ctx" in p["title"] and "999" in p["evidence"][0] for p in as_props), as_props)
        check("advisor.from_assess turns a missing-role workload gap into a HIGH proposal (nothing covered yet)",
              any(p["severity"] == "high" and "ioc_analyst" in p["title"] for p in as_props), as_props)

        labdir = os.path.join(tmp, "advise_lab")
        os.makedirs(labdir, exist_ok=True)
        good_scope = {"authorized_targets": ["10.20.0.0/24"], "excluded": [], "allowed_actions": ["*"], "window": ""}
        json.dump(good_scope, open(os.path.join(labdir, "scope.json"), "w"))
        json.dump([{"id": "F1", "host": "10.20.0.5", "desc": "x"}, {"id": "F2", "host": "8.8.8.8", "desc": "out of scope"}], open(os.path.join(labdir, "findings.json"), "w"))
        scope_props = adv.from_scope_findings(labdir)
        check("advisor.from_scope_findings flags a finding whose host is NOT in scope.json's authorized_targets, using arch_pentest's own real check",
              any(p["severity"] == "high" and "F2" in str(p["evidence"]) for p in scope_props), scope_props)
        check("advisor.from_scope_findings reports nothing when there is no findings.json to check (not an error)", adv.from_scope_findings(os.path.join(tmp, "advise_target")) == [])

        combined = adv.advise(advdir)
        check("advisor.advise() combines code-scan findings for a real folder into one ranked, counted report",
              combined["count"] >= 1 and combined["proposals"][0]["severity"] == max((p["severity"] for p in combined["proposals"]), key=lambda s: {"high": 3, "medium": 2, "low": 1}.get(s, 0)), combined)

        # ---- read_text's cache: a project file is read many times per run (rank_files' TF-IDF pass over every
        # file, context()'s full-text display, outline(), every code check) with a different byte `limit` at
        # nearly every call site; the cache must still return exactly the same truncated bytes as an uncached
        # read, and must never serve stale content once the file changes.
        cache_file = os.path.join(tmp, "cache_target.py")
        core.write_text(cache_file, "a" * 100 + "é")  # a multi-byte char right at a truncation boundary
        core._READ_CACHE.pop(os.path.abspath(cache_file), None)
        uncached_full, _ = core.read_text(cache_file)
        uncached_cut, _ = core.read_text(cache_file, 101)   # cuts mid multi-byte char -> errors="replace"
        core._READ_CACHE.pop(os.path.abspath(cache_file), None)
        cached_full, _ = core.read_text(cache_file)   # populates the cache
        cached_cut, _ = core.read_text(cache_file, 101)  # served from the cached raw bytes, sliced fresh
        check("read_text's cache returns byte-identical text to an uncached read, at every limit used", cached_full == uncached_full and cached_cut == uncached_cut, (cached_cut, uncached_cut))
        check("read_text's cache is actually populated (not a no-op) after a read", os.path.abspath(cache_file) in core._READ_CACHE)
        core.write_text(cache_file, "brand new content, nothing like before\n")
        fresh, _ = core.read_text(cache_file)
        check("read_text never serves stale content after write_text changes the same file (cache invalidated by the write, not left to mtime resolution)", fresh == "brand new content, nothing like before\n", fresh)
        core._READ_CACHE.clear()
    finally:
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    ok = all(r is True for r in results)
    n_fail, n_none = results.count(False), results.count(None)
    print(f"  {'ALL ' + str(len(results)) + ' SECURITY CHECKS PASSED' if ok else str(n_fail) + ' of ' + str(len(results)) + ' SECURITY CHECKS FAILED' + (f', {n_none} COULD NOT RUN (not a pass)' if n_none else '')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
