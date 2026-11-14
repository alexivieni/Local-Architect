"""Three outcomes where they were lost (debug audit 2026-11-10): a check that could not run is None, never a pass and never a fail.

What is proven here, without a model, a GPU or the network (throwaway ARCHITECT_HOME / ARCHITECT_DATA_DIR):
  - core.repro_check: a reproduction that fails by assertion is the proof (True); one that passes, cannot load, has no test or TIMES
    OUT is not (False: a hang proves nothing); with a test command that runs every test, a failure whose output does not name the
    file could be another test's (None);
  - the lint gate: pyflakes that could not start or timed out is None, not a pass; a finding in ANY file counts, not only the last;
  - the ship pipeline: a stage whose checks could not run makes the pipeline not_sure (not "pass"), a real failure still fails;
  - log_checks lists a check that could not run apart from the failing ones;
  - arch_costs.summary: no finished task / no call is None, not $0.00 / 0%.

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
        check("CI workflow: one check per step (in a multi-line PowerShell step only the last exit code counts)",
              not re.search(r"run: \|\n(?:\s+python [^\n]+\n){2,}", wf))

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
