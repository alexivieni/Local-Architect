"""Self-test: the hub says only what is true (the 2026-11-09 audit: no stubs, every button works, no misrepresentation).

Each check pins one thing that was shown to the person but was not so, now fixed:
  - Review keeps a check that could not run (it was dropped, so a not-sure run said "no checks ran")
  - Doctor: a check that could not tell is "could not check", never a fail; --fix only cleans runs on a real "disk low"
  - the usage meter gives a real median tokens/s (Home's speed tile read a report with no speed in it: always "-")
  - /api/state says which speed inputs it used and where they came from; the KV type is the person's setting
  - By job: the prior reaches the page
  - the page: Stop says what it does, Free memory unloads Architect's own models, an unfinished step is not "done",
    the speed inputs are not fixed guesses, Tiers says estimate, recommendations show their error, no dead false claims

    python arch_selftest_honesty.py

Everything runs in a throwaway ARCHITECT_HOME / ARCHITECT_DATA_DIR; nothing is started, nothing leaves the machine.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    tmp = tempfile.mkdtemp(prefix="arch_honesty_")
    old = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "OLLAMA_HOST")}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.environ["OLLAMA_HOST"] = "127.0.0.1:9"                 # nothing listens: the state build must not wait for it
    os.makedirs(os.environ["ARCHITECT_HOME"], exist_ok=True)
    try:
        import arch_core as core
        core.set_home(os.environ["ARCHITECT_HOME"])
        import architect_hub as ah
        import arch_doctor
        import arch_usage
        import model_hub as mh

        # ---- a project with one run: two checks passed/failed and one that could not run, three local calls with speeds
        proj = os.path.join(tmp, "proj")
        rd = os.path.join(proj, ".arch", "runs", "20261109-000000")
        os.makedirs(rd)
        now = time.time()
        recs = [{"type": "call", "t": now, "model": "qwen3:8b", "tier": "worker", "transport": "llama", "prompt_tokens": 500,
                 "gen_tokens": 100, "tok_s": s} for s in (40.0, 55.0, 70.0)]
        recs += [{"type": "check", "t": now, "gate": "tests_pass", "ok": True, "text": ""},
                 {"type": "check", "t": now, "gate": "lint", "ok": False, "text": "E1 unused import"},
                 {"type": "check", "t": now, "gate": "e2e_pass", "ok": None, "text": "could not run: no browser"},
                 {"type": "verdict", "t": now, "verdict": "not_sure", "why": "a check could not run"}]
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"goal": "x", "status": "done"}, f)

        rv = ah.review_view(proj, os.path.basename(rd))
        oks = [t["ok"] for t in rv.get("tests") or []]
        check("Review keeps all three outcomes: passed, failed and could not run (it dropped the last one)",
              sorted(oks, key=lambda v: (v is None, v)) == [False, True, None], oks)
        check("the first failure is the real failure, not the check that could not run",
              (rv.get("first_failure") or "").startswith("lint"), rv.get("first_failure"))

        # ---- Doctor: could not tell is None; the overall is None when nothing failed but something could not be checked
        real_du = arch_doctor.shutil.disk_usage
        arch_doctor.shutil.disk_usage = lambda p: (_ for _ in ()).throw(OSError("access denied"))
        try:
            dc = arch_doctor.check_disk(tmp)
        finally:
            arch_doctor.shutil.disk_usage = real_du
        check("Doctor: a disk that could not be read is 'could not check' (None), not a fail", dc["ok"] is None, dc)
        src = open(os.path.join(HERE, "arch_doctor.py"), encoding="utf-8").read()
        check("Doctor --fix cleans old runs only on a real 'disk low', and the overall result can be None",
              'disk_check["ok"] is False' in src and "None if None in hard else True" in src)

        # ---- the usage meter: a real median speed
        u = arch_usage.compute("week", roots=[proj])
        check("the usage meter gives the median tokens/s of local calls (Home's speed tile was always '-')",
              (u.get("local") or {}).get("median_tok_s") == 55.0 and (u.get("local") or {}).get("tok_s_n") == 3, u.get("local"))

        # ---- the state: speed inputs and where they came from; the KV type is the person's setting
        t0 = time.time()
        st = mh.build_state({"part": ["live"]})
        took = time.time() - t0
        si = st.get("speed_inputs") or {}
        check("/api/state says which speed inputs it used and where each came from; KV = the person's setting (q8_0), not f16",
              si.get("kv") == "q8_0" and si.get("gpu_bw_src") in ("measured", "estimate") and si.get("ram_bw_src") in ("measured", "estimate"), si)
        st2 = mh.build_state({"part": ["live"], "gpu_bw": ["300"]})
        check("a typed value is used and labelled 'you'", (st2.get("speed_inputs") or {}).get("gpu_bw") == 300.0
              and (st2.get("speed_inputs") or {}).get("gpu_bw_src") == "you", st2.get("speed_inputs"))
        check("the state build does not wait 2 s for a stopped local Ollama", took < 1.5, f"{took:.2f} s")

        # ---- By job: the prior reaches the page
        hub_src = open(os.path.join(HERE, "architect_hub.py"), encoding="utf-8").read()
        check("By job passes the prior and its reasons through (the page's 'Prior:' line never appeared)",
              '"suggestion", "prior", "prior_reasons")' in hub_src)

        # ---- the page
        ui = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        check("Stop says what it does: the run ends after the model call in progress, then is ended (no 'nothing is half-applied')",
              "Stopping between steps" not in ui and "nothing is half-applied" not in ui
              and "Ends after the model call in progress; Undo puts back any file it changed" in ui and "r.graceful?" in ui)
        check("Free memory unloads Architect's own models and reports what was unloaded (it looped over other apps' models)",
              "O.models.filter(m=>m.ours)" in ui and "O.models.filter(m=>!m.ours)" not in ui and 'toast("Graphics memory freed")' not in ui)
        check("a step that only started is in progress (or has no result once the run ended), never 'done'",
              'st.ok===false?"fail":EV.verdict?"unsure":"now"' in ui and 'st.ok===false?"fail":"done"' not in ui
              and 'x.ok==null?"now":"done"' in ui)
        check("the Models page speed inputs are not fixed guesses (192 / 50 GB/s and f16 overrode the measurement)",
              'const opts={kv:"",gpu_bw:"",ram_bw:"",vram:"",ram:""};' in ui and "opts.gpu_bw=+e.target.value||192" not in ui)
        check("Home's tiles say what they are: a benchmark pass rate, and a measured tokens/s from the usage meter",
              '"Benchmark pass rate"' in ui and '"Tokens per second"' in ui and "l.median_tok_s" in ui and '"Words per second"' not in ui)
        check("Tiers says the fit is an estimate from file size (it said 'measured')",
              '" models measured"' not in ui and "Each installed model was measured" not in ui and "an estimate from their size" in ui)
        check("Review shows 'Could not check' for a check that could not run", '"Could not check"),h("span",{class:"grow"},x.name)' in ui)
        check("recommendations show their error (it showed 'Tuned for this PC') and name the real backup instead of 'can be undone'",
              "Could not read the recommendations" in ui and "every save can be undone" not in ui and "architect_config.json.bak" in ui)
        check("Doctor and Assess: 'could not check' and 'matches the estimate' (not 'fail' and 'already tuned')",
              '"could not check"' in ui and '"Could not check everything"' in ui and '"already tuned"' not in ui)
        check("By job says 'now set' only after the background job finished (it said so as soon as the job started)",
              "j.code===0)u.append" in ui and 'toast("Applying "+model+" for "+job)' not in ui)
        check("no dead code with a false claim ('Twenty-three specialists': there are 46 roles), no stale index labels",
              "function sheet(){" not in ui and "Twenty-three specialists" not in ui and ".arch/index/." not in ui and "the Work page" not in ui)
        check("an unknown GPU reading is not drawn as 0% busy", "push(GPU.gpu_pct||0)" not in ui and 'typeof GPU.gpu_pct==="number"' in ui)
        # ---- found by the button crawl (2026-11-09): every button on 45 pages pressed in a throwaway install
        check("'Run checks' and 'Propose next steps' check again (a press was answered from the 60 s read cache: nothing changed)",
              'onclick:()=>runDoctor(true)},"Run checks"' in ui and 'onclick:()=>runAdvise(true)},"Propose next steps"' in ui
              and '(fresh===true?"&fresh=1":"")' in ui)
        check("an empty Add (queue) or Install (plugin) says what it needs (it only moved focus, silently)",
              'toast("Type the task to queue first")' in ui and 'toast("Type the plugin\'s folder path first")' in ui)
        check("'What will it do?' reads the route without assuming every field is there",
              '(r.domains||[]).join(", ")' in ui and '"+r.domains.join(", ")+"' not in ui)
        check("Home's Recent runs is read again when a run finishes (it showed the new run only if the first read happened to be slow)",
              "simpleDraw._fed!==EV.epoch" in ui and "simpleDraw._fed=EV.epoch;loadHome();" in ui)
        check("the Components page's sample buttons all answer, and say they are samples",
              'toast("Small pressed (a sample)")' in ui and 'toast("Remove pressed (a sample: nothing removed)")' in ui)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' HONESTY CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' HONESTY CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
