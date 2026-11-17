"""Three outcomes where they were lost (debug audit 2026-11-10): a check that could not run is None, never a pass and never a fail.

What is proven here, without a model, a GPU or the network (throwaway ARCHITECT_HOME / ARCHITECT_DATA_DIR):
  - core.repro_check: a reproduction that fails by assertion is the proof (True); one that passes, cannot load, has no test or TIMES
    OUT is not (False: a hang proves nothing); with a test command that runs every test, a failure whose output does not name the
    file could be another test's (None);
  - the lint gate: pyflakes that could not start or timed out is None, not a pass; a finding in ANY file counts, not only the last;
  - the ship pipeline: a stage whose checks could not run makes the pipeline not_sure (not "pass"), a real failure still fails;
  - log_checks lists a check that could not run apart from the failing ones;
  - arch_costs.summary: no finished task / no call is None, not $0.00 / 0%.
  - (review 2026-11-17) Get ready, its smoke test and proof, the live test and the Scoreboard keep "could not be checked" apart;
    a GET never writes recent_roots.json; no personal default ships; an impossible bandwidth is dropped; dead routes are gone.

Run: python arch_selftest_outcomes.py   ->   ALL N OUTCOME CHECKS PASSED
"""
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def run():
    tmp = tempfile.mkdtemp(prefix="arch_outc_")
    keys = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")
    old_env = {k: os.environ.get(k) for k in keys}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR"):
        os.makedirs(os.environ[k], exist_ok=True)
    sys.path.insert(0, HERE)
    passed, failed = [], []

    def _err(fn):
        try:
            fn()
        except ValueError as ex:
            return str(ex)
        return ""

    def check(name, ok, extra=""):
        (passed if ok else failed).append(name)
        print(("  ok    " if ok else "  FAIL  ") + name + (f"  ({extra})" if extra and not ok else ""))

    try:
        import arch_core as core
        import arch_gates as G
        import arch_flows as fl

        cfg = core.load_config()
        proj = os.path.join(tmp, "proj")
        os.makedirs(os.path.join(proj, "tests"))

        def write(rel, text):
            p = os.path.join(proj, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)

        # ---- core.repro_check, with scripted test results (no test is really run) ----
        real_rt, real_has = core.run_tests, core.has_module
        try:
            def scripted(status, log):
                core.run_tests = lambda root, cfg_, targets=None: (status, log)
            core.has_module = lambda name: True if name == "pytest" else real_has(name)
            scripted("fail", "tests/test_bug.py F\nE   assert 1 == 2\n1 failed")
            ok, why, _ = core.repro_check(proj, cfg, "tests/test_bug.py")
            check("repro: fails by assertion -> the proof (True)", ok is True, why)
            scripted("pass", "1 passed")
            check("repro: passes already -> not a proof (False)", core.repro_check(proj, cfg, "tests/test_bug.py")[0] is False)
            scripted("fail", "E   ModuleNotFoundError: No module named 'shop'")
            check("repro: fails to import -> wrong reason (False)", core.repro_check(proj, cfg, "tests/test_bug.py")[0] is False)
            scripted("fail", "timed out after 120s (the whole process tree was stopped; ...)\n")
            ok, why, _ = core.repro_check(proj, cfg, "tests/test_bug.py")
            check("repro: a TIMEOUT is not a proof of the bug (it used to count as 'fails')", ok is False and "timed out" in why, why)
            scripted("none", "no tests ran")
            check("repro: no test ran -> False", core.repro_check(proj, cfg, "tests/test_bug.py")[0] is False)
            ccfg = dict(cfg, tests=dict(cfg["tests"], command="make test"))
            scripted("fail", "FAIL: test_other (test_other.T.test_other)\nAssertionError")
            ok, why, _ = core.repro_check(proj, ccfg, "tests/test_bug.py")
            check("repro: the project's command runs everything and names another test -> could not tell (None)", ok is None and "could not check" in why, why)
            scripted("fail", "FAIL: test_x (test_bug.T.test_x)\nAssertionError")
            check("repro: ... and naming this file -> True", core.repro_check(proj, ccfg, "tests/test_bug.py")[0] is True)
            scripted("fail", "tests/test_bug.py .                [ 50%]\ntests/test_other.py F             [100%]\n"
                             "FAILED tests/test_other.py::test_y - assert 0\n1 failed, 1 passed")
            check("repro: pytest's progress line naming a PASSING reproduction is not a proof (only failure lines count)",
                  core.repro_check(proj, ccfg, "tests/test_bug.py")[0] is None)
            scripted("fail", "tests/test_bug.py F\nFAILED tests/test_bug.py::test_x - assert 1 == 2\n1 failed")
            check("repro: pytest's FAILED summary line for this file -> True", core.repro_check(proj, ccfg, "tests/test_bug.py")[0] is True)
            scripted("fail", "tests/test_bugfix_more.py F\nFAILED tests/test_bugfix_more.py::test_z - assert 0")
            check("repro: a file whose name only starts the same is another file", core.repro_check(proj, ccfg, "tests/test_bug.py")[0] is None)
            core.has_module = lambda name: False if name == "pytest" else real_has(name)
            scripted("fail", "FAIL: test_other (test_other.T.test_other)\nAssertionError")
            check("repro: unittest discover (no pytest) is held to the same rule", core.repro_check(proj, cfg, "tests/test_bug.py")[0] is None)
        finally:
            core.run_tests, core.has_module = real_rt, real_has

        class E:                                  # the few Engine attributes the gates and flows read
            def __init__(self):
                self.root, self.cfg, self.reports, self.said = proj, cfg, [], []
                self.changed_files, self.calls, self.review, self.deny_prefixes = [], 0, "auto", []
                self.flow, self.goal, self.cps = "ship", "a goal", []

            def say(self, *a):
                self.said.append(" ".join(str(x) for x in a))

            def report(self, *lines):
                self.reports.extend(lines)

            def checkpoint(self, **kw):
                self.cps.append(kw)

            def put_file(self, rel, text, *a, **k):
                write(rel, text)

        # ---- the repro gate passes None through, and keeps the failing log for the fix ----
        write("tests/test_bug.py", "def test_x():\n    assert 1 == 2\n")
        try:
            core.run_tests = lambda root, cfg_, targets=None: ("fail", "FAIL: test_other (test_other.T.test_other)\nAssertionError")
            ctx, ee = {}, E()
            ee.cfg = dict(cfg, tests=dict(cfg["tests"], command="make test"))     # the project's own command runs every test
            r = G.GATES["repro_fails_right_reason"](ee, ctx, {}, ["tests/test_bug.py"])
            check("repro gate: could not tell -> ok None (not_sure), the reproduction is kept for the fix",
                  r["ok"] is None and ctx.get("repro_path") == "tests/test_bug.py", str(r))
        finally:
            core.run_tests = real_rt

        # ---- the lint gate ----
        write("a.py", "import os\n")
        write("b.py", "x = 1\n")
        real_cmd = core.run_cmd
        try:
            core.has_module = lambda name: True if name == "pyflakes" else real_has(name)
            core.run_cmd = lambda *a, **k: (127, "could not start: no such file")
            r = G.GATES["lint"](E(), {}, {"strict": True}, ["a.py", "b.py"])
            check("lint: pyflakes that could not start is None (it was a pass, even with strict)", r["ok"] is None, str(r))
            core.run_cmd = lambda *a, **k: (124, "timed out after 60s")
            check("lint: a pyflakes timeout is None too", G.GATES["lint"](E(), {}, {"strict": True}, ["a.py"])["ok"] is None)
            seq = iter([(1, "a.py:1:1 'os' imported but unused"), (0, "")])
            core.run_cmd = lambda *a, **k: next(seq)
            r = G.GATES["lint"](E(), {}, {"strict": True}, ["a.py", "b.py"])
            check("lint: a finding in the first file fails a strict lint even when the last file is clean", r["ok"] is False and "unused" in r["text"], str(r))
            core.run_cmd = lambda *a, **k: (0, "")
            check("lint: every file clean -> True", G.GATES["lint"](E(), {}, {"strict": True}, ["a.py", "b.py"])["ok"] is True)
        finally:
            core.run_cmd, core.has_module = real_cmd, real_has

        # ---- the ship pipeline ----
        import arch_pipeline as P

        class Stage:
            def __init__(self, out):
                self.out = out

            def run(self, e, params):
                return dict(self.out)

        real = dict(fl.FLOWS)
        try:
            def ship(outs):
                for k, v in outs.items():
                    fl.FLOWS[k] = Stage(v)
                return P.ShipFlow().run(E(), {"stages": list(outs)})
            built = {"ok": True, "files": ["shop.py"]}
            out = ship({"build": built, "test": {"ok": True}, "review": {"ok": True}})
            check("ship: every stage passed -> ok, not unsure", out["ok"] is True and not out.get("unsure"), str(out))
            out = ship({"build": built, "test": {"ok": False, "unsure": True, "why": "no test ran"}, "review": {"ok": True}})
            check("ship: a stage that could not be checked -> not_sure (it ended as a plain fail)", out.get("unsure") is True and "no test ran" in out.get("why", ""), str(out))
            out = ship({"build": {"ok": True, "unsure": True, "files": ["shop.py"]}, "test": {"ok": True}, "review": {"ok": True}})
            check("ship: a build whose tests could not run is not a pass (it ended as 'pass')",
                  out.get("unsure") is True and out["stages"]["build"]["ok"] is False, str(out))
            out = ship({"build": built, "test": {"ok": False}, "review": {"ok": True, "unsure": True}})
            check("ship: a real failure still fails (unsure never hides it)", out["ok"] is False and not out.get("unsure"), str(out))
            with open(os.path.join(proj, "docs", "ship.md"), encoding="utf-8") as f:
                md = f.read()
            check("ship: docs/ship.md says 'could not be checked' for an unsure stage", "could not be checked" in md and "failed" in md, md[-300:])
        finally:
            fl.FLOWS.clear()
            fl.FLOWS.update(real)

        # ---- log_checks ----
        e = E()
        fl.log_checks(e, "checks", {"ok": False, "passed": 1, "total": 3, "text": "x",
                                    "results": [{"name": "syntax", "ok": True}, {"name": "tests", "ok": False}, {"name": "fakecode", "ok": None}]})
        line = e.reports[-1]
        check("log_checks: the failing list holds only real failures", "failing: tests)" in line and "failing: tests; fakecode" not in line, line)
        check("log_checks: a check that could not run is named as such", "could not run: fakecode" in line, line)

        # ---- arch_costs: unknown is None ----
        import arch_costs
        s = arch_costs.summary(days=7, roots=[os.path.join(tmp, "nothing_here")])
        check("costs: no finished task -> per_task_usd None (shown as 'no finished task yet', not $0.00)", s.get("per_task_usd") is None, str(s))
        check("costs: no model call -> local_share None (not 0% done locally)", s.get("local_share") is None, str(s))

        # ==== Phase 2: a figure is labelled measured only when it was measured HERE ====
        import arch_hw
        b = arch_hw.bandwidth({})
        check("bandwidth: nothing measured -> the estimate, labelled so (not the reference laptop's 158 / 19.5)",
              b["label"] == "estimate" and b["gpu_bw"] == arch_hw.BW_ESTIMATE["gpu_bw"] and b["ram_bw"] != 19.5, str(b))
        b = arch_hw.bandwidth({"gpu_bw": 150.0})
        check("bandwidth: a GPU figure alone does not make the RAM figure 'measured'", b["label"] == "GPU measured here, RAM estimate"
              and b["gpu_measured"] and not b["ram_measured"], str(b))
        src = {n: open(os.path.join(HERE, n), encoding="utf-8").read() for n in
               ("arch_memplan.py", "arch_swap.py", "arch_modelmath.py", "arch_recommend.py", "arch_server.py", "arch_hw.py", "arch_finops.py", "arch_port.py")}
        check("bandwidth: no module falls back to the reference laptop's 158 / 19.5 GB/s",
              not any('or 158.0' in s_ or 'or 19.5' in s_ for s_ in src.values()))
        check("free RAM is read, never guessed (the ram * 0.6 fallback is gone)", "else ram * 0.6" not in src["arch_hw.py"])
        check("recommendations: no fixed sentence of today's numbers labelled measured", "107 million" not in src["arch_recommend.py"])
        check("server plan: the reference laptop's speeds say whose they are",
              "measured 56 -> 306-343 \"\n                                \"tokens/s on edits here" not in src["arch_server.py"] and "reference" in src["arch_server.py"])
        check("port: says it checked by arithmetic, not that it measured", "measured each one against it" not in src["arch_port.py"])
        import arch_broker as B
        busy = {"tiers": {"worker": {"up": True, "busy": True, "on_cpu": False, "tok_s": 40.0, "prefill_tok_s": 1000.0, "size_gb": 2.5}},
                "gpu_pct": 10, "ram_free_gb": 12.0, "ecores": 0, "heavy_job": None, "ours_busy": True, "bandwidth": {}, "cloud": {"ok": False}}
        d = B.route({"kind": "generate", "size_tokens": 400}, state=busy, log=False)
        check("broker: an ETA with a fixed busy wait in it is an estimate, even with a measured speed", d.get("evidence") == "estimate", str(d))
        import arch_finops as fo
        lay = {l["id"]: l for l in fo._optimization_layers({}, {"cpu_perf_cores": 4, "ram_gb": 16}, {}, {}, [])}
        check("finops: 'Measured here' holds that layer's own reading (no RAM size for the performance-core layer)",
              "performance cores" in lay["hybrid_cpu_power"]["measured"] and "GB RAM" not in lay["hybrid_cpu_power"]["measured"]
              and "n-gram" not in lay["role_ctx_trimming"]["measured"], str({k: v["measured"] for k, v in lay.items()}))
        check("finops demo: bars to scale (306 tok/s full, 55 tok/s 18%), no invented speed curve",
              'width:18%"></div></div><b style="text-align:right">55 tok/s' in fo._DEMO_HTML and "polyline" not in fo._DEMO_HTML)
        check("autotune: a 'draft' variant that loaded no draft is reported as n-gram, from what the server loaded",
              '"draft" if row.get("draft_used") else "ngram"' in open(os.path.join(HERE, "arch_autotune.py"), encoding="utf-8").read()
              and '"draft_used": rec.get("draft") or None' in src["arch_server.py"])
        check("modelmath: the label comes from arch_hw.bandwidth (per figure)", "arch_hw.bandwidth(" in src["arch_modelmath.py"])

        # ==== a YAML flow's not_sure reaches the run's record (tech-debt 2026-11-13: it was recorded as "fail") ====
        import json as _json
        import arch_dataflow as df
        import arch_roles as _roles
        G.GATES["_outcomes_cannot_run"] = lambda e_, ctx_, step_, changed_: G.R(None, "could not run here")
        G.GATES["_outcomes_passes"] = lambda e_, ctx_, step_, changed_: G.R(True, "")
        nsproj = os.path.join(tmp, "nsproj")
        os.makedirs(nsproj, exist_ok=True)
        ncfg = core.deep_merge(core.load_config(nsproj), {"roles": {r_: "fake:7b" for r_ in _roles.ROLES}})
        saved_flows = dict(fl.FLOWS)
        try:
            for fid, gname, want in (("_ns_flow", "_outcomes_cannot_run", "not_sure"), ("_ok_flow", "_outcomes_passes", "pass")):
                fl.FLOWS[fid] = df.DataFlow({"id": fid, "title": "t", "steps": [{"id": "s", "kind": "code", "gate": gname}],
                                             "verdict": {"pass_if": "s.ok"}})
                en = core.Engine(nsproj, fid, "g", ncfg, review="auto", say=lambda *a, **k: None)
                out = fl.run_flow(en, fid)
                recs = [_json.loads(x) for x in open(os.path.join(en.run_dir, "trace.jsonl"), encoding="utf-8") if x.strip()]
                vs = [r_.get("verdict") for r_ in recs if r_.get("type") == "verdict"]
                check(f"YAML flow ending {want}: every verdict record says {want} (run_flow wrote 'fail' after the flow's not_sure)",
                      vs and all(v_ == want for v_ in vs) and bool(out.get("unsure")) == (want == "not_sure"), str(vs))
        finally:
            fl.FLOWS.clear()
            fl.FLOWS.update(saved_flows)
            G.GATES.pop("_outcomes_cannot_run", None)
            G.GATES.pop("_outcomes_passes", None)

        # ==== the selftest CLI reads every suite's answer the same way, and runs every suite on disk ====
        import glob as _glob
        import architect as A
        check("selftest CLI: True / None / 0 pass, False / 1 / a count fail (rc |= True made a green run exit 1)",
              [A._suite_rc(x) for x in (True, None, 0, False, 1, 5)] == [0, 0, 0, 1, 1, 1])
        check("selftest CLI: a suite's exit 2 ('0 of N FAILED, k COULD NOT RUN') is not sure, and a failure outranks it (1|2 made 3)",
              A._suite_rc(2) == 2 and A._worst(0, 2) == 2 and A._worst(2, 1) == 1 and A._worst(1, 2) == 1 and A._worst(0, 0) == 0)
        on_disk = {os.path.basename(f_)[:-3] for f_ in _glob.glob(os.path.join(HERE, "arch_selftest*.py"))}
        listed = {A._suite_module(n_) for n_ in A.CORE_SUITES + A.ALL_SUITES + A.FULL_ONLY}
        check("selftest CLI: every suite on disk is either listed or run as its own process under `all`",
              on_disk == listed | set(A.unlisted_suites()) and not (listed - on_disk - {"arch_selftest_sec"}), str(listed - on_disk))
        check("crew's suite returns 0 when it passes (it returned its check count)",
              "return 0" in open(os.path.join(HERE, "arch_selftest_crew.py"), encoding="utf-8").read().split("CREW CHECKS PASSED")[1][:200])

        # ==== CI on GitHub's Windows machines (.github/workflows/ci.yml, tools/ci_manifest_check.py; 2026-11-13) ====
        import importlib.util as _ilu
        import subprocess as _sp
        spec = _ilu.spec_from_file_location("ci_manifest_check_t", os.path.join(HERE, "tools", "ci_manifest_check.py"))
        mc = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mc)
        import arch_selfimprove as _si
        real_verify, real_run = _si.verify, _sp.run

        class _Out:
            def __init__(self, text):
                self.returncode, self.stdout, self.stderr = 0, text, ""
        try:
            _sp.run = lambda *a, **k: _Out("a.py\0b.py\0")
            _si.verify = lambda base: {"has_manifest": True, "modified": [], "missing": ["docs/my_paper.tex"], "unlisted": []}
            check("CI manifest: a manifest file git does not track (the owner's own) is not a failure in a clone", mc.main() == 0)
            _si.verify = lambda base: {"has_manifest": True, "modified": [], "missing": ["b.py"], "unlisted": []}
            check("CI manifest: a tracked file that is missing fails", mc.main() == 1)
            _si.verify = lambda base: {"has_manifest": True, "modified": ["a.py"], "missing": [], "unlisted": []}
            check("CI manifest: a file that differs from the manifest fails", mc.main() == 1)
        finally:
            _si.verify, _sp.run = real_verify, real_run
        wf = open(os.path.join(HERE, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
        steps_ok = all(c in wf for c in ("architect.py flows --validate", "tools/doc_counts.py", "tools/swallow_audit.py", "arch_lessons.py",
                                         "architect.py fakecode", "tools/ci_manifest_check.py", "tools/ci_parallel.py"))
        check("CI workflow: Windows runner, line endings kept before checkout, every handoff check, a throwaway home and data folder",
              "runs-on: windows-latest" in wf and wf.index("core.autocrlf false") < wf.index("actions/checkout") and steps_ok
              and "ARCHITECT_HOME=" in wf and "ARCHITECT_DATA_DIR=" in wf)
        spec2 = _ilu.spec_from_file_location("ci_annotate_t", os.path.join(HERE, "tools", "ci_annotate.py"))
        an = _ilu.module_from_spec(spec2)
        spec2.loader.exec_module(an)
        rows = [{"suite": "arch_selftest_a", "status": "FAIL", "secs": 3, "summary": "1 of 9 FAILED", "fails": ["FAIL  x"],
                 "alone": {"summary": "1 of 9 FAILED"}},
                {"suite": "arch_selftest_b", "status": "NOT_SURE", "secs": 2, "summary": "0 of 5 FAILED, 1 COULD NOT RUN"},
                {"suite": "arch_selftest_c", "status": "PASS", "secs": 1, "summary": "ALL 4 PASSED"}]
        ann_, md_ = an.lines_for(rows)
        check("CI report: a failing suite is a public error annotation with its failing lines, NOT_SURE a warning, PASS nothing",
              [a_[0] for a_ in ann_] == ["error", "warning"] and "FAIL  x" in ann_[0][2] and "2/3 suites green" in md_
              and md_.index("arch_selftest_a") < md_.index("arch_selftest_c"), str(ann_))
        check("CI report: annotation text escapes newlines and percent signs (GitHub's command syntax)",
              an.esc("50%\nx") == "50%25%0Ax")
        many = [{"suite": f"s{i_}", "status": "FAIL", "summary": "x", "fails": []} for i_ in range(14)]
        check("CI report: at most 10 error annotations (GitHub's per-step limit), the rest counted in one",
              sum(1 for a_ in an.lines_for(many)[0] if a_[0] == "error") == 10)
        ign = _sp.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "static/templates/src"], cwd=HERE,
                      capture_output=True, text=True)
        shipped_ignored = [p_ for p_ in ign.stdout.splitlines() if "__pycache__" not in p_ and not p_.endswith(".pyc")]
        check("no file a project starter ships with is git-ignored (samples/access.log was: every clone failed the starter's own tests)",
              ign.returncode == 0 and not shipped_ignored, shipped_ignored)
        check("CI workflow: one check per step (in a multi-line PowerShell step only the last exit code counts)",
              not re.search(r"run: \|\n(?:\s+python [^\n]+\n){2,}", wf))

        # ==== the read cache never hands back an old copy of a file just written (GitHub's runner lost memory counts) ====
        fp = os.path.join(tmp, "cache_probe.md")
        with open(fp, "w", encoding="utf-8") as f_:
            f_.write("- a line (used 1, confirmed 0)\n")
        first = core.read_text(fp)[0]
        ns = os.stat(fp).st_mtime_ns
        with open(fp, "w", encoding="utf-8") as f_:                  # same size, written by plain open() (not write_text)
            f_.write("- a line (used 2, confirmed 0)\n")
        os.utime(fp, ns=(ns, ns))                                    # and the same timestamp: one tick on a fast disk
        check("read cache: a same-size rewrite within one timestamp tick is read again (it returned the old text)",
              "used 1" in first and "used 2" in core.read_text(fp)[0])
        import arch_server as _S
        check("a clean-exit request (Ctrl+C on a console) is never raised for this process or its parent",
              _S._ask_exit(os.getpid()) is False and _S._ask_exit(os.getppid()) is False)

        # ==== Phase 3: features that never did anything ====
        import arch_roles as roles
        role = next(r for r in roles.ROLES if roles.ROLES[r]["latency"] != "must be fast")
        mk = lambda g: {"name": "coder:7b", "agg": {}, "rec": {"verdict": "split", "gpu_ratio": g}}
        check("roles: a model mostly in RAM (40% on the card) loses more than one 85% on the card (the < 0.7 branch never fired)",
              roles.score_for_role(role, mk(0.4))[0] < roles.score_for_role(role, mk(0.85))[0],
              (roles.score_for_role(role, mk(0.4))[0], roles.score_for_role(role, mk(0.85))[0]))
        check("model_hub.recommend hands role scoring the share on the card", '"gpu_ratio": best["gpu_ratio"]' in open(os.path.join(HERE, "model_hub.py"), encoding="utf-8").read())
        import arch_hosts as H
        check("hosts: an OpenAI-style address keeps its path (http://x:8080/v1 became http://x:8080/v1:11434)",
              H.normalize("http://x:8080/v1") == "http://x:8080/v1" and H.normalize("http://x/v1") == "http://x:8080/v1"
              and "port number" in _err(lambda: H.normalize("desk:99999"))
              and H.normalize("192.168.1.20") == "http://192.168.1.20:11434" and H.normalize("http://desk:11434/") == "http://desk:11434")
        H._store("health", "box", {"name": "box", "url": "http://10.0.0.9:8080", "ok": True, "kind": "llama", "ts": 1.0})
        H._KIND_CACHE["ts"] = 0.0
        check("hosts: a llama-server found by an earlier health check (any process) is routed as one, not to the Ollama API",
              H.is_llama_host("http://10.0.0.9:8080") and not H.is_llama_host("http://10.0.0.10:11434"))
        import arch_cloud, arch_compact
        check("dead code removed: arch_cloud._add_spend, arch_compact.should_compact / router_from_engine",
              not hasattr(arch_cloud, "_add_spend") and not hasattr(arch_compact, "should_compact") and not hasattr(arch_compact, "router_from_engine"))
        src_pm = open(os.path.join(HERE, "arch_postmortem.py"), encoding="utf-8").read()
        check("the postmortem step that set_step opens is closed with end_step", src_pm.count("e.end_step(\"postmortem\"") == 2)

        # ---- Phase A of the 2026-11-17 review: three outcomes in Get ready, the live test and the Scoreboard
        import arch_ready, arch_flows, arch_core as core_
        ov = arch_ready.overall
        check("ready: one required check that could not run makes the whole 'not sure', a failed one 'not yet', an advisory one neither",
              ov([{"ok": True}, {"ok": None}]) is None and ov([{"ok": None}, {"ok": False}]) is False
              and ov([{"ok": True}, {"ok": None, "advisory": True}]) is True and ov([]) is False)
        steps_ = {"steps": [], "needs": []}

        def step_(name, ok, detail="", advisory=False):
            steps_["steps"].append({"name": name, "ok": None if ok is None else bool(ok)})

        class _E:
            calls = 0

            def __init__(self, *a, **k):
                pass
        real_eng, real_rf = core_.Engine, arch_flows.run_flow
        try:
            core_.Engine = _E
            arch_flows.run_flow = lambda e, f, p: {"ok": True, "unsure": True}
            arch_ready._smoke({}, steps_, step_, lambda m: None)
            arch_flows.run_flow = lambda e, f, p: {"ok": True}
            arch_ready._smoke({}, steps_, step_, lambda m: None)
        finally:
            core_.Engine, arch_flows.run_flow = real_eng, real_rf
        check("ready smoke test: a run whose checks could not run is not sure (it said OK), a real pass is OK",
              [s["ok"] for s in steps_["steps"]] == [None, True] and any("could not run" in n for n in steps_["needs"]), steps_)
        import arch_livetest as lt

        def sc_bug(p):
            raise KeyError("fixture")

        def sc_model(p):
            raise core_.LLMError("the model gave up")
        lt_out = lt.run(scenarios=[("a", "A", sc_bug), ("b", "B", sc_model), ("c", "C", lambda p: (True, [], None))], say=lambda m: None)
        check("live test: a broken scenario is not judged (None, it was a fail), a model that gave up fails, a pass passes",
              [r["ok"] for r in lt_out["results"]] == [None, False, True] and lt_out["passed"] == 1 and lt_out["unsure"] == 1,
              [r["ok"] for r in lt_out["results"]])
        import model_hub as mh_
        cat_ = next(iter(mh_.TASKS))
        real_chat = mh_.chat
        try:
            mh_.chat = lambda *a, **k: (_ for _ in ()).throw(mh_.OllamaError(0, "Ollama is not reachable at http://127.0.0.1:11434"))
            down = mh_.run_tests("m", [cat_, "toolcall", "speed"])
            mh_.chat = lambda *a, **k: (_ for _ in ()).throw(mh_.OllamaError(0, "Ollama is not reachable (timed out)"))
            slow = mh_.run_tests("m", [cat_])
        finally:
            mh_.chat = real_chat
        check("Scoreboard: Ollama unreachable leaves the categories out (they scored 0 and pulled the model down); a timeout is the model's 0",
              down == {} and slow.get(cat_, {}).get("score") == 0.0 and slow[cat_]["n"] == len(mh_.TASKS[cat_]), (down, slow.get(cat_)))
        src_rd = open(os.path.join(HERE, "arch_ready.py"), encoding="utf-8").read()
        check("ready proof: a category with no judged task is not sure, not a 0/100 failure",
              'ok = None if sc is None else sc >= PROOF_PASS' in src_rd)

        # ---- the person's data: a read never writes it; no personal value ships as a default
        src_hub = open(os.path.join(HERE, "architect_hub.py"), encoding="utf-8").read()
        usage_branch = src_hub.split('if u.path == "/api/arch/usage":', 1)[1].split("if u.path ==", 1)[0]
        check("GET /api/arch/usage reads the folders it is asked about but no longer saves them to recent_roots.json",
              "remember_root" not in usage_branch and "known_roots(extra)" in usage_branch)
        html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        check("no personal GitHub repo is pre-filled on the Agents page", "github.com/alexivieni" not in html)
        check("cloud agents off (the default) is a calm note on the Agents page, not an error; local agent teams open first",
              "if(r.off){" in html and '"off": why_' in src_hub and '["agentsp","crew","Agent teams",["crewbuild",' in html)

        # ---- memory bandwidth: a figure no memory can reach is dropped, never used or called measured
        import json as _json, arch_hw
        with open(os.path.join(os.environ["ARCHITECT_DATA_DIR"], "bandwidth.json"), "w", encoding="utf-8") as f_:
            _json.dump({"gpu_bw": 188.1, "ram_bw": 626.5, "source": "test"}, f_)
        lb, B = arch_hw.load_bandwidth(), arch_hw.bandwidth()
        check("bandwidth: a saved RAM figure of 626.5 GB/s is rejected (laptop DDR5 ~90), the GPU figure kept, RAM labelled an estimate",
              lb.get("ram_bw") is None and lb.get("rejected") == {"ram_bw": 626.5} and lb.get("gpu_bw") == 188.1
              and B["ram_bw"] == arch_hw.BW_ESTIMATE["ram_bw"] and not B["ram_measured"] and B["gpu_measured"], (lb, B))
        check("bandwidth_from: a split run that implies impossible RAM bandwidth gives None (it was saved as measured)",
              arch_hw.bandwidth_from(30.0, 5.0, 28.0, 4.9, 2.0)["ram_bw"] is None        # 2 GB / (1/28 - 4.9/150) s = 656 GB/s
              and arch_hw.bandwidth_from(30.0, 5.0, 5.0, 3.0, 2.0)["ram_bw"] == 11.1)    # 2 GB / (1/5 - 3/150) s
        os.remove(os.path.join(os.environ["ARCHITECT_DATA_DIR"], "bandwidth.json"))

        # ---- dead code: nothing points at a file that does not exist
        src_mh = open(os.path.join(HERE, "model_hub.py"), encoding="utf-8").read()
        check("model_hub: no route or action starts autonomous_agent_optimized.py (it does not exist); /api/run, /api/runs, /api/notes gone",
              "autonomous_agent_optimized" not in src_mh and '"/api/runs"' not in src_mh and '"/api/notes"' not in src_mh
              and '"/api/run"' not in src_mh and all(os.path.isfile(os.path.join(HERE, v[0])) for v in mh_.ACTIONS.values()))
        import importlib.util
        sp_ = importlib.util.spec_from_file_location("_start_menu", os.path.join(HERE, "start.py"))
        st_ = importlib.util.module_from_spec(sp_)
        sp_.loader.exec_module(st_)
        check("start.py: every menu item runs a script that exists and is not a stub", all(os.path.isfile(os.path.join(HERE, c[0])) for _, _, c in st_.MENU)
              and not any(c[0] == "arch_training.py" for _, _, c in st_.MENU))
        src_cli = open(os.path.join(HERE, "architect.py"), encoding="utf-8").read()
        check("fit assumes the KV cache Architect starts the servers with (q8_0, or OLLAMA_KV_CACHE_TYPE), like explain; swap is dispatched once",
              'ft.add_argument("--kv", choices=["f16", "q8_0"], default=None' in src_cli and 'else "q8_0")' in src_cli
              and '"swap": lambda' not in src_cli)
        check("the hub's page router has no unreachable 'advisor' branch (LEGACY sends advisor to doctor)", 'page==="advisor"' not in html)

        # ---- Phase E of the 2026-11-17 review: one run, one story
        rc_, out_ = core_.run_cmd([sys.executable, "-c", "print('\x1b[1m\x1b[31mE   failed\x1b[0m here')"], tmp, 60)
        src_core = open(os.path.join(HERE, "arch_core.py"), encoding="utf-8").read()
        check("command output reaches reports and the model without terminal colour codes; pytest is asked for none",
              rc_ == 0 and "\x1b" not in out_ and "E   failed here" in out_ and '"--color=no"' in src_core, repr(out_[-80:]))
        import io, contextlib, architect as cli_

        class _Run:
            run_dir = tmp
        codes, lines = [], []
        for o_ in ({"ok": True}, {"ok": False}, {"ok": True, "unsure": True}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                codes.append(cli_._done_line(o_, _Run()))
            lines.append(buf.getvalue())
        check("architect.py run ends PASSED / DID NOT PASS / NOT SURE with exit 0 / 1 / 2 (a not-sure run said OK and exited 0)",
              codes == [0, 1, 2] and "PASSED" in lines[0] and "DID NOT PASS" in lines[1] and "NOT SURE" in lines[2], (codes, lines))
        src_sv = open(os.path.join(HERE, "arch_supervisor.py"), encoding="utf-8").read()
        check("the supervisor reads exit 2 as not sure when a run left no verdict record", '"not_sure" if code == 2 else "fail"' in src_sv)
        check("the hub's words: the job badge uses the run's verdict words (it said 'stopped' in red for every run that did not pass), "
              "Home's card and the Console's route line come from the one VERD table",
              "function jobWord(job)" in html and "const jw=jobWord(j)" in html and "const head=verd(v)[2]" in html
              and '"stopped: "' not in html and "did not pass its checks" not in html and "Nothing is kept unless you say so" not in html)
        check("Home's card offers Undo only when the run may have changed files, and says how many (or none)",
              'nf===0?null:h("button",{class:"btn ghost-danger"' in html and "No file was changed." in html)
        check("Home keeps moving: a live line (time, model calls, failed checks) and the steps it showed stay after the run",
              'id:"spLive"' in html and "simpleDraw._seen" in html and 'if(ev.t&&!EV.t0)EV.t0=ev.t' in html
              and 'run.goal="";{const gi=$("#spGoal");if(gi)gi.value="";}' in html)
        import arch_flows as fl_
        check("a best-of attempt says why it scored what it did (Review showed nine bare 'Failed' rows)",
              "could not be applied: app.py EDIT needs" in fl_._bon_why([{"status": "error", "rel": "app.py", "msg": "EDIT needs an existing file"}], [], {})
              and fl_._bon_why([], ["a.py"], {"ok": False, "passed": 2}).startswith("2 check(s) passed")
              and "ValueError" in fl_._bon_why([], [], {}, ValueError("x")) and "const fold=a=>" in html)
        import architect_hub as hub_, json as _j2
        proj = os.path.join(tmp, "rv_proj")
        rd_ = os.path.join(proj, ".arch", "runs", "20261117_000000_bugfix")
        os.makedirs(rd_, exist_ok=True)
        with open(os.path.join(proj, "kept.py"), "w") as f_:
            f_.write("x = 1\n")
        with open(os.path.join(rd_, "run.json"), "w") as f_:
            _j2.dump({"goal": "g", "flow": "bugfix", "status": "incomplete"}, f_)
        with open(os.path.join(rd_, "trace.jsonl"), "w") as f_:
            for rec in ({"type": "diff", "file": "tests/test_repro.py", "added": 6, "new_file": True, "diff": "+x"},
                        {"type": "diff", "file": "kept.py", "added": 1, "new_file": True, "diff": "+x = 1"}):
                f_.write(_j2.dumps(rec) + "\n")
        rv = hub_.review_view(proj, "20261117_000000_bugfix")
        gone_ = {f["file"]: bool(f.get("gone")) for f in rv.get("files") or []}
        check("Review: a file the run made and removed again (a rejected reproduction) is not shown as a change; one still there is",
              gone_ == {"tests/test_repro.py": True, "kept.py": False}, gone_)

        # ---- Phase G (2026-11-17): CI tells the truth
        sp_ci = importlib.util.spec_from_file_location("_ci_par", os.path.join(HERE, "tools", "ci_parallel.py"))
        cip = importlib.util.module_from_spec(sp_ci)
        sp_ci.loader.exec_module(cip)
        hist = os.path.join(tmp, "ci_hist", "load.json")
        first = cip.load_history(hist, [{"suite": "arch_selftest_supervisor", "status": "LOAD"}, {"suite": "arch_selftest_x", "status": "PASS"}])
        second = cip.load_history(hist, [{"suite": "arch_selftest_supervisor", "status": "LOAD"}])
        third = cip.load_history(hist, [{"suite": "arch_selftest_other", "status": "LOAD"}])
        check("CI: a suite that fails only under load twice within ten runs is reported (counted red), a first time is not",
              first == {} and list(second) == ["arch_selftest_supervisor"] and third == {} and cip.load_history("", []) == {}, (first, second, third))
        src_cip = open(os.path.join(HERE, "tools", "ci_parallel.py"), encoding="utf-8").read()
        ci_yml = open(os.path.join(HERE, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
        check("CI: the browser suites must run in full on the runner ('could not run' is red there), and the load history is cached",
              cip._named("arch_selftest_uismoke", ["uismoke", "crawl"]) and not cip._named("arch_selftest_ui", ["uismoke"])
              and 'if mark == "NOT_SURE" and _named(r["suite"], must):' in src_cip
              and "--must-run uismoke,crawl" in ci_yml and "--history .ci_history" in ci_yml and "actions/cache/save@v5" in ci_yml)
        src_hc = open(os.path.join(HERE, "arch_hub_check.py"), encoding="utf-8").read()
        check("hub check without node: balanced brackets are not sure (they were a pass), unbalanced ones a failure",
              'None if js.count("(") == js.count(")")' in src_hc and "COULD NOT RUN" in src_hc)

        # ---- Phase B of the 2026-11-17 review: the hub answers fast (nothing recomputed that did not change)
        import socket, time as _t, arch_passport as ap_, arch_deps as dp_, model_hub as mh2
        fake_gguf = os.path.join(tmp, "m.gguf")
        with open(fake_gguf, "wb") as f_:
            f_.write(b"x" * 64)
        reads, real_read = [], ap_.gguf_meta_read
        try:
            ap_.gguf_meta_read = lambda p, max_kv=400, stop=None: (reads.append(p) or {"general.architecture": "llama", "_n": len(reads)})
            ap_._META_MEMO.clear()
            m1, m2 = ap_.gguf_meta(fake_gguf), ap_.gguf_meta(fake_gguf)
            ap_._META_MEMO.clear()                                   # a new process: the header comes back from disk
            m3 = ap_.gguf_meta(fake_gguf)
            n_same = len(reads)
            os.utime(fake_gguf, (1_700_000_000, 1_700_000_000))      # the file changed: read again
            ap_.gguf_meta(fake_gguf)
            ap_.gguf_meta_read = lambda p, max_kv=400, stop=None: (reads.append(p) or {"error": "bad header"})
            ap_._META_MEMO.clear()
            os.utime(fake_gguf, (1_700_000_100, 1_700_000_100))
            e1 = ap_.gguf_meta(fake_gguf)
            e2 = ap_.gguf_meta(fake_gguf)
        finally:
            ap_.gguf_meta_read = real_read
        check("a model file's header is read once (kept in memory and on disk), again when the file changes, and an error is never kept",
              n_same == 1 and m1 == m2 == m3 and len(reads) == 4 and "error" in e1 and "error" in e2, (n_same, len(reads)))
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        up_port = lsock.getsockname()[1]
        dsock = socket.socket()
        dsock.bind(("127.0.0.1", 0))
        down_port = dsock.getsockname()[1]
        dsock.close()                                                # a port nothing listens on
        t0_ = _t.time()
        down_ = core_.loopback_down(f"127.0.0.1:{down_port}", ttl=0)
        took_ = _t.time() - t0_
        check("a stopped local Ollama is recognised in well under a second; a listening one and a remote host are not 'down'",
              down_ is True and core_.loopback_down(f"http://127.0.0.1:{up_port}", ttl=0) is False
              and core_.loopback_down("192.0.2.1:11434") is False and (True if took_ < 1.0 else None), round(took_, 2))
        lsock.close()
        old_oh = os.environ.get("OLLAMA_HOST")
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{down_port}"
        core_._DOWN_MEMO.clear()
        try:
            try:
                mh2.oll("/api/tags", timeout=30)
                raised = None
            except mh2.OllamaError as ex_:
                raised = ex_
        finally:
            if old_oh is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = old_oh
        check("model_hub.oll says 'Ollama is not reachable ... (nothing is listening there)' at once for a stopped local server",
              raised is not None and raised.code == 0 and "not reachable" in raised.msg and "nothing is listening" in raised.msg)
        d_chk = os.path.join(tmp, "selfcheck")
        os.makedirs(d_chk, exist_ok=True)
        with open(os.path.join(d_chk, "good_mod_b.py"), "w") as f_:
            f_.write("X = 1\n")
        r0 = dp_.self_check(d_chk)
        fresh_kept = dp_._SELF_OK.get(d_chk) is not None             # just written: never remembered (racy timestamp)
        old_t = _t.time() - 30
        os.utime(os.path.join(d_chk, "good_mod_b.py"), (old_t, old_t))
        r1 = dp_.self_check(d_chk)
        remembered = dp_._SELF_OK.get(d_chk) is not None
        with open(os.path.join(d_chk, "good_mod_b.py"), "w") as f_:
            f_.write("X = (\n")                                      # the same size as "X = 1\n", written a moment ago
        r2 = dp_.self_check(d_chk)
        check("the code self-check is remembered for files that are not fresh, never for ones written in the last 2 s, and runs again "
              "when one changes (a same-size rewrite inside one timestamp tick is caught)",
              r0 == [] and not fresh_kept and r1 == [] and remembered and r2 and "SyntaxError" in r2[0]["error"], (r0, fresh_kept, r1, r2))
        calls_hw = []
        real_dh, real_nc = mh2._detect_hardware, os.environ.pop("ARCHITECT_NO_CACHE", None)
        try:
            mh2._detect_hardware = lambda: (calls_hw.append(1) or {"gpu": "g", "notes": []})
            mh2._HW_MEMO.update(t=0.0, v=None)
            mh2.detect_hardware()
            mh2.detect_hardware()
            os.environ["ARCHITECT_NO_CACHE"] = "1"
            mh2.detect_hardware()
        finally:
            mh2._detect_hardware = real_dh
            mh2._HW_MEMO.update(t=0.0, v=None)
            if real_nc is not None:
                os.environ["ARCHITECT_NO_CACHE"] = real_nc
        check("the hardware reading (nvidia-smi) is kept 5 s, and always measured when ARCHITECT_NO_CACHE=1", len(calls_hw) == 2, calls_hw)
        b4 = open(os.path.join(HERE, "arch_core.py"), encoding="utf-8").read()
        i_fb, i_cut = b4.index("# The failure log is trimmed FIRST"), b4.index("keep = max(1500, keep // 500 * 500)")
        check("prompt fitting trims the failure text before cutting the context, and the context cut is rounded (a longer traceback moved the cut "
              "and lost the cached prefix)", i_fb < b4.index("fb = user[fi:]") < i_cut)
        guarded = all(f"if(document.hidden){{{t}=setTimeout({fn}" in html for t, fn in (
            ("CC.timer", "ccPoll"), ("AG.timer", "()=>agentPoll(false)"), ("loadQueue._t", "loadQueue"), ("studioTimer", "pollStudio"),
            ("launchTimer", "pollLaunch"), ("jobTimer", "pollJob")))
        check("the polls that kept asking in a hidden tab (chat, launch, run output, queue, agents) wait; the 1.5 s tick too; bgjobs is not asked twice",
              guarded and "setInterval(()=>{if(document.hidden)return;liveEnsure();" in html
              and "Date.now()-(loadBgJobs._at||0)>1500" in html)
        check("the console strip's 'which servers are up' is kept 4 s (a process-list call and a /health per tier every 5 s)",
              '_ttl("servers_up", 4,' in src_hub)

        import arch_server as srv_
        lg = os.path.join(tmp, "srv", "worker.log")
        os.makedirs(os.path.dirname(lg), exist_ok=True)
        with open(lg, "wb") as f_:
            f_.write(b"x" * 2000)
        srv_._rotate_log(lg, limit=1000)
        rotated = os.path.isfile(lg + ".1") and not os.path.exists(lg)
        with open(lg, "wb") as f_:
            f_.write(b"y" * 10)
        srv_._rotate_log(lg, limit=1000)
        srv_._rotate_log(os.path.join(tmp, "srv", "missing.log"), limit=1)
        check("a model server's log is rotated to <name>.log.1 past its limit before the next start; a small or missing one is left alone",
              rotated and os.path.getsize(lg) == 10 and os.path.getsize(lg + ".1") == 2000 and srv_.LOG_MAX_BYTES == 5 * 1024 * 1024)

        # ---- Phase C, first slice: pages that told you to type a command now have the button
        check("Benchmarks and the Overnight report have buttons (they said 'run architect.py eval ...' / 'python tools/morning_report.py'), "
              "and Train no longer names the retired arch_training.py",
              "function benchButtons()" in html and 'api("/api/arch/eval",{mode})' in html and "Write the report now" in html
              and 'api("/api/arch/morning",{})' in html and "architect.py eval --fake checks the harness" not in html
              and "python tools/morning_report.py writes one" not in html and "python arch_training.py" not in html
              and 'path == "/api/arch/eval"' in src_hub and '"harness": ["--fake", "--quick"]' in src_hub
              and 'mh.JOB.start("Overnight report", ["tools/morning_report.py"])' in src_hub)

        # ---- Phase F of the 2026-11-17 review: the phone (375 px, measured in the browser in light and dark: no sideways scroll)
        check("phone: Home's suggestion chips wrap (all four visible; a fading sideways strip hid 'Explain'), and the Console's message list "
              "is capped so the message box is on the first screen",
              ".sp-try .sp-recipes{flex-wrap:wrap;flex:1 1 12rem;overflow:visible}" in html
              and "mask-image:linear-gradient(to right,var(--mask) 85%" not in html.split("@media (max-width:767px){", 1)[1].split("}", 1)[0]
              and ".thread .msgs{flex:none;max-height:32vh}" in html and ".thread{min-height:0;height:auto;max-height:none}" in html)
    finally:
        sys.path.remove(HERE) if HERE in sys.path else None
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print(f"{len(failed)} of {len(passed) + len(failed)} OUTCOME CHECKS FAILED: " + "; ".join(failed))
        return 1
    print(f"ALL {len(passed)} OUTCOME CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
