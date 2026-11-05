"""Self-test for swap-aware chains and prefix-stable prompts (arch_chain.py + the arch_core/arch_flows changes):
the swap ledger, prefill-reuse detection (and that it never teaches a wrong chars-per-token ratio), the rule that every
retry prompt keeps its stable part first, judge batching in the build flow, the fit sheet and the `fit` command.
Scripted fake Ollama; no real model."""
# The scripted stand-in model is named chainfix:7b, not fake:7b: the dataset export (rightly) drops every fake: reply as a fixture,
# and this suite checks that export on these runs. Its data dir is throwaway, so nothing here reaches real training data.
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GB = 1024 ** 3
if __name__ == "__main__" and "arch_core" not in sys.modules:
    # Run on its own, this suite must not read the person's real settings: their worker tier is served by a live llama-server, and
    # a role this suite does not override reached it (seen 2026-11-05, during a GPU training run). Same isolation as `selftest`.
    _iso = tempfile.mkdtemp(prefix="chain_selftest_")
    os.environ["ARCHITECT_HOME"] = _iso
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_iso, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"


def _wait(cond, secs=20):
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < secs:
        if cond():
            return True
        _t.sleep(0.2)
    return False


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_chain as ch
    import arch_core as core
    import arch_flows as fl
    import arch_selftest as base
    import arch_tune

    # ---- the swap ledger
    sw = ch.Swaps()
    costs = [sw.note("a:7b", 0.2), sw.note("a:7b", 0.1), sw.note("b:4b", 6.0), sw.note("a:7b", 0.0, {"a:7b": {"load_s": 9.0}}), sw.note(None, 4.0), sw.note("b:4b", None, {"b:4b": {"load_s": 9.0}})]
    check("the ledger counts a model change as a swap, not a repeated call, and a call on another machine as neither", sw.n == 3 and sw.models["a:7b"] == 3 and sw.models["b:4b"] == 2 and None not in sw.models)
    check("a swap's cost is what the server reported (a warm swap on a big card is ~0, never the old cold-load time); only a missing report falls back to the measured load, and that is counted",
          costs == [0.0, 0.0, 6.0, 0.0, 0.0, 9.0] and sw.secs == 15.0 and sw.imputed == 1, (costs, sw.secs, sw.imputed))
    lines = "\n".join(sw.lines(6, {"calls": 2, "tokens": 3000}, run_secs=60.0))
    check("the report block states swaps, seconds, share of the run, where the numbers come from, and prompt reuse as an estimate",
          "Model swaps: 3, about 15 s" in lines and "25% of the run" in lines and "1 swap(s) had no report" in lines and "2 call(s) read far fewer" in lines and "estimate" in lines, lines)
    check("a run on one model says so", "Model swaps: none" in "\n".join(ch.Swaps().lines(3, {})))

    # ---- every retry prompt keeps its stable part first (so the server reuses the cached prefill on a retry)
    import arch_flows_ci, arch_flows_fake, arch_flows_pro, arch_flows_sec
    bad = []
    for mod in (fl, arch_flows_ci, arch_flows_fake, arch_flows_pro, arch_flows_sec):
        for name in dir(mod):
            t = getattr(mod, name)
            if isinstance(t, str) and "{feedback}" in t:
                tail = t[t.index("{feedback}"):]
                if re.search(r"\{(context|code|content|files|findings|failure|survivors)\}", tail):
                    bad.append(f"{mod.__name__}.{name}")
    check("no retry template puts {feedback} before the project context or the code (prefix-stable prompts)", not bad, bad)
    check("the bugfix prompt puts the per-try failure text after the code", fl.BUG_FIX.index("{context}") < fl.BUG_FIX.index("{failure}"))

    class Chain(base.Fake):
        pec_small = False
        loads = {}

        def do_GET(self):
            if self.path == "/api/ps":
                return self._send({"models": []})
            if self.path == "/api/tags":
                return self._send({"models": [{"name": n, "digest": "x", "size": 4 * GB, "details": {"parameter_size": "7B", "quantization_level": "Q4_K_M"}} for n in ("chainfix:7b", "judge:4b")]})
            return base.Fake.do_GET(self)

        def script(self, role, user, full, st):
            if "Plan the work as small SPRINTS" in user and "broken sprint" in user:
                return json.dumps({"sprints": [{"name": "slugify", "kind": "backend", "goal": "slugify function", "files": ["slugify.py", "tests/test_slugify.py"],
                                                "acceptance": [{"id": "A1", "text": "lowercases and hyphenates"}]},
                                               {"name": "broken", "kind": "backend", "goal": "something the model never delivers", "files": ["never.py"],
                                                "acceptance": [{"id": "B1", "text": "exists"}]}]})
            if "Implement this sprint" in user and "never delivers" in user:
                return "I would need more information before I can do that. Would you like me to proceed?"
            return base.Fake.script(self, role, user, full, st)

        def _respond(self, b, txt, pec):
            if Chain.pec_small:
                pec = 12                                        # the server read almost nothing: the prefix came from its cache
            elif Chain.state.get("pec_ratio"):                  # a partial read: this share of the prompt's true size
                pec = int(Chain.state["pec_ratio"] * sum(len(m["content"]) for m in b["messages"]) / 3.0)
            ld = Chain.loads.get(b["model"], 0)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for i in range(0, len(txt), 400):
                self.wfile.write((json.dumps({"message": {"role": "assistant", "content": txt[i:i + 400]}, "done": False}) + "\n").encode())
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": pec,
                                          "eval_count": 50, "eval_duration": 10 ** 9, "load_duration": int(ld * 1e9)}) + "\n").encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Chain)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    tmp = tempfile.mkdtemp(prefix="archchain_")
    quiet = lambda m: None
    try:
        root = os.path.join(tmp, "p")
        os.makedirs(root)
        with open(os.path.join(root, "big.py"), "w") as f:
            f.write("def f(x):\n    return x\n" * 300)
        cfg = core.load_config(root)
        cfg["roles"]["generator"] = "chainfix:7b"
        cfg["behavior"]["learn_from_runs"] = "off"
        # these checks pin the base build loop; the owner's real config must not leak in (a heavy tier to escalate to, the best-of-N
        # rescue round): both have their own checks in arch_selftest_loop / arch_selftest_llama
        cfg["tiers"]["heavy"] = ""
        cfg["behavior"]["stall_samples"] = 1
        for _r in ("planner", "architect", "evaluator"):
            cfg["roles"][_r] = "chainfix:7b"

        # ---- prefill reuse: detected, reported, and kept out of the chars-per-token calibration
        e = core.Engine(root, "build", "g", cfg=cfg, review="auto", say=quiet)
        big = "x " * 6000
        Chain.state["pec"] = None
        Chain.pec_small = False
        e.call("generator", big)
        cpt0 = arch_tune.cpt_for(e.stats, "chainfix:7b")
        Chain.pec_small = True
        r = e.call("generator", big + "\nFEEDBACK: again")
        Chain.pec_small = False
        check("a second call whose server-side prompt count is far below the estimate is counted as a cached-prefix hit",
              e.reuse["calls"] == 1 and e.reuse["tokens"] > 1000, e.reuse)
        check("that call does not teach the chars-per-token ratio (it would have looked like 500 chars per token)", abs(arch_tune.cpt_for(e.stats, "chainfix:7b") - cpt0) < 0.01, (cpt0, arch_tune.cpt_for(e.stats, "chainfix:7b")))
        trace = [json.loads(l) for l in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8")]
        check("the trace records the estimate and the reused prefix per call", trace[-1]["prefix_reused_est"] > 1000 and trace[0]["prefix_reused_est"] == 0 and trace[-1]["prompt_est"] > 1000)
        cfg_small = json.loads(json.dumps(cfg))
        cfg_small["limits"]["num_ctx"] = 2048
        cfg_small["limits"]["ctx_autogrow"] = False
        es = core.Engine(root, "build", "g", cfg=cfg_small, review="auto", say=quiet)
        es.call("generator", big)
        Chain.pec_small = True
        rs = es.call("generator", big + "\nFEEDBACK: again")
        Chain.pec_small = False
        check("a retry whose prompt by estimate fills the window is warned about as a possible truncation, never counted as reuse",
              es.reuse["calls"] == 0 and rs["ctx_overflow"] == "estimated full" and any("by estimate" in n for n in es.ctx_events), (es.reuse, rs["ctx_overflow"], es.ctx_events))
        es.finish(True)
        cpt_before = arch_tune.cpt_for(es.stats, "chainfix:7b")
        Chain.state["pec_ratio"] = 0.75
        e5 = core.Engine(root, "build", "g", cfg=cfg, review="auto", say=quiet, persist=False)
        e5.call("generator", "y " * 3000)
        Chain.state["pec_ratio"] = None
        check("a call that read only part of its prompt (between 60% and 90% of the estimate) teaches nothing about chars-per-token", abs(arch_tune.cpt_for(e5.stats, "chainfix:7b") - cpt_before) < 0.01, (cpt_before, arch_tune.cpt_for(e5.stats, "chainfix:7b")))
        e.finish(True)
        rep = open(os.path.join(e.run_dir, "report.md"), encoding="utf-8").read()
        check("the run report ends with a Run economics block", "## Run economics" in rep and "Prompt reuse: 1 call(s)" in rep, rep[-500:])

        # ---- swaps in a real engine: load_duration from the server is the cost
        Chain.loads = {"judge:4b": 7.5}
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["roles"]["evaluator"] = "judge:4b"
        e2 = core.Engine(root, "build", "g", cfg=cfg2, review="auto", say=quiet)
        e2.call("generator", "a")
        e2.call("evaluator", "b")
        e2.call("generator", "c")
        check("the engine counts the swaps of a run and their loading seconds from the server's load_duration", e2.swaps.n == 2 and 7.0 <= e2.swaps.secs <= 8.0, (e2.swaps.n, e2.swaps.secs))
        Chain.loads = {}

        # ---- judge batching
        data = os.environ.get("ARCHITECT_DATA_DIR") or core.data_dir()
        os.makedirs(data, exist_ok=True)
        prof_path = os.path.join(data, "profile.json")
        old_prof = open(prof_path, encoding="utf-8").read() if os.path.isfile(prof_path) else None

        def set_vram(v):
            with open(prof_path, "w", encoding="utf-8") as f:
                json.dump({"hw": {"vram_gb": v}}, f)

        cfg3 = json.loads(json.dumps(cfg2))
        cfg3["behavior"]["evaluator"] = "each"
        set_vram(6)
        e3 = core.Engine(root, "build", "g", cfg=cfg3, review="auto", say=quiet, persist=False)
        d1, why = ch.defer_judge(e3)
        set_vram(24)
        d2, _ = ch.defer_judge(e3)
        set_vram(6)
        cfg4 = json.loads(json.dumps(cfg3))
        cfg4["roles"]["evaluator"] = "chainfix:7b"
        d3, _ = ch.defer_judge(core.Engine(root, "build", "g", cfg=cfg4, review="auto", say=quiet, persist=False))
        cfg5 = json.loads(json.dumps(cfg3))
        cfg5["behavior"]["swap_aware"] = "off"
        d4, _ = ch.defer_judge(core.Engine(root, "build", "g", cfg=cfg5, review="auto", say=quiet, persist=False))
        cfg6 = json.loads(json.dumps(cfg3))
        cfg6["behavior"]["evaluator"] = "final"
        d5, _ = ch.defer_judge(core.Engine(root, "build", "g", cfg=cfg6, review="auto", say=quiet, persist=False))
        os.remove(prof_path)
        d6, _ = ch.defer_judge(core.Engine(root, "build", "g", cfg=cfg3, review="auto", say=quiet, persist=False))
        set_vram(6)
        check("the judge is batched only when: judge model != builder, small GPU (known), evaluator = each, swap_aware on",
              d1 and "one swap instead of two" in why and not d2 and not d3 and not d4 and not d5 and not d6, (d1, d2, d3, d4, d5, d6))

        # the build flow: with batching, every judge call comes after the last sprint; the verdicts are still reported per sprint
        Chain.state["calls"] = []
        proj = os.path.join(tmp, "build")
        os.makedirs(proj)
        cfg7 = json.loads(json.dumps(cfg3))
        cfg7["behavior"]["fake_code"] = "off"
        cfg7["behavior"]["playwright"] = "off"
        eb = core.Engine(proj, "build", "slugify function", cfg=cfg7, review="auto", say=quiet)
        res = fl.run_flow(eb, "build", {})
        eb.finish(res.get("ok"))
        roles_seq = [c["role"] for c in Chain.state["calls"]]
        rep = open(os.path.join(eb.run_dir, "report.md"), encoding="utf-8").read()
        last_gen = max(i for i, r in enumerate(roles_seq) if r == "generator")
        first_judge = next((i for i, r in enumerate(roles_seq) if r == "evaluator"), None)
        check("in the build flow the batched judge runs after the last builder call, and the report still carries a verdict per sprint",
              res.get("ok") and first_judge is not None and first_judge > last_gen and "Judge batched at the end" in rep and "Judge for sprint 'slugify'" in rep, (roles_seq, rep[-600:]))
        check("the batched judge used the judge model, i.e. one swap for the whole build", Chain.state["calls"][first_judge]["model"] == "judge:4b" and eb.swaps.n <= 2, (eb.swaps.n, eb.swaps.pairs))
        tr = [json.loads(l) for l in open(os.path.join(eb.run_dir, "trace.jsonl"), encoding="utf-8") if '"call"' in l]
        jmsg = next(t for t in tr if t["role"] == "evaluator")["messages"][-1]["content"]
        check("the deferred judge sees the evidence saved when the sprint passed (its files and ITS test result), not a fresh test run", "test result: pass" in jmsg and "slugify" in jmsg, jmsg[-400:])
        Chain.state["calls"] = []
        proj2 = os.path.join(tmp, "build2")
        os.makedirs(proj2)
        cfg8 = json.loads(json.dumps(cfg7))
        cfg8["limits"]["max_revisions"] = 1
        eb2 = core.Engine(proj2, "build", "slugify function then a broken sprint", cfg=cfg8, review="auto", say=quiet)
        res2 = fl.run_flow(eb2, "build", {})
        eb2.finish(res2.get("ok"))
        rep2 = open(os.path.join(eb2.run_dir, "report.md"), encoding="utf-8").read()
        check("when the build stops at a failing sprint, the batched judge is skipped and says so (nothing is judged against a broken tree)",
              not res2.get("ok") and "Deferred judge skipped" in rep2 and not any(c["role"] == "evaluator" for c in Chain.state["calls"]), (res2, rep2[-500:]))
        if old_prof is None:
            os.remove(prof_path)
        else:
            open(prof_path, "w", encoding="utf-8").write(old_prof)

        # ---- the fit sheet
        hw = {"vram_gb": 6.0, "ram_gb": 16.0}
        arch7 = {"layers": 28, "kv_heads": 4, "head_dim": 128, "ctx_max": 32768}
        import model_hub as mh
        rows = [{"name": "coder:7b", "size_gb": 4.7, "quant": "Q4_K_M", "params": "7.6B", "embed": False, "arch": arch7, "fit": mh.fit_model(int(4.7 * GB), arch7, hw), "rec": {}},
                {"name": "small:4b", "size_gb": 2.5, "quant": "Q4_K_M", "params": "4B", "embed": False, "arch": arch7, "fit": mh.fit_model(int(2.5 * GB), arch7, hw), "rec": {}},
                {"name": "nomic-embed-text:latest", "size_gb": 0.27, "quant": "F16", "params": "137M", "embed": True, "arch": {}, "fit": {"rows": []}, "rec": {}}]
        tcfg = {"roles": {"generator": "coder:7b", "planner": "coder:7b", "evaluator": "small:4b"}, "limits": {"num_ctx": 16384}, "model_overrides": {"small:4b": {"num_ctx": 8192}},
                "retrieval": {"mode": "hybrid"}}
        sheet = ch.fit_sheet(tcfg, hw, rows, {"coder:7b": {"load_s": 11.0, "gen_tok_s": 18.0}}, {"runs": 4, "swaps_per_run": 3.5, "swap_secs_per_run": 40})
        m7 = next(m for m in sheet["models"] if m["model"] == "coder:7b")
        m4 = next(m for m in sheet["models"] if m["model"] == "small:4b")
        me = next((m for m in sheet["models"] if m["model"].startswith("nomic")), None)
        check("the fit sheet lists each team model with its roles, weights, KV cache at ITS context and a verdict",
              m7["roles"] == ["generator", "planner"] and m7["ctx"] == 16384 and m7["kv_gb"] and m7["kv_gb"] > 0.4 and m7["verdict"].startswith("mostly on the GPU") and "layers on the CPU" in m7["verdict"] and "fully on the GPU at 8192" in m7["verdict"]
              and m4["ctx"] == 8192 and "fits on the GPU" in m4["verdict"], (m7, m4))
        check("a q8_0 KV cache figure is given next to f16, as an estimate", m7["kv_q8_gb"] and m7["kv_q8_gb"] < m7["kv_gb"])
        check("no embedding model sits on the sheet any more (retrieval is SQLite FTS5) and the sheet says so", me is None and any("FTS5" in t for t in sheet["tips"]), sheet["tips"])
        check("on a 6 GB card the sheet says one chat model is resident and prices the swaps from measurements and the run ledger",
              "one chat model at a time" in sheet["resident"] and "every role change is a swap" in sheet["swap"] and "11 s" in sheet["swap"] and "3.5 swaps" in sheet["swap"], (sheet["resident"], sheet["swap"]))
        check("tips name the partly-offloaded model with the fix, and price the swaps", any("coder:7b" in t and "lower its context" in t for t in sheet["tips"]) and any("Swaps cost about 11 s" in t for t in sheet["tips"]), sheet["tips"])
        hw0 = {"vram_gb": 0.0, "ram_gb": 16.0}
        sheet0 = ch.fit_sheet(tcfg, hw0, [dict(r, fit=mh.fit_model(int(r["size_gb"] * GB), r["arch"], hw0)) for r in rows], {}, {})
        check("with no GPU nothing is called 'spilling' (it all runs from RAM) and no offload tip is given", "CPU only" in sheet0["resident"] and not any("lower its context" in t for t in sheet0["tips"]), sheet0["tips"])
        txt = ch.sheet_text(sheet)
        check("the text sheet labels its numbers as estimates from the model's metadata", "estimate" in txt and "coder:7b" in txt and "Tips:" in txt)
        sheet24 = ch.fit_sheet(tcfg, {"vram_gb": 24.0, "ram_gb": 64.0}, rows, {}, {})
        check("on a 24 GB card the whole team fits together and no swap tip is given", "fits together" in sheet24["resident"] and not any("Swaps cost" in t for t in sheet24["tips"]), sheet24["resident"])
        lg = ch.ledger_swaps(os.path.join(proj, ".arch", "runs"))
        check("the ledger reads swaps per run back from finished run reports", lg.get("runs") == 1 and "swaps_per_run" in lg, lg)

        # ---- the command
        env = dict(os.environ, ARCHITECT_HOME=tmp, ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "fit", "--root", proj, "--json"], capture_output=True, text=True, env=env, timeout=120)
        j = json.loads(r.stdout or "{}")
        check("`fit --json` prints the sheet for the installed models", r.returncode == 0 and isinstance(j.get("models"), list) and "resident" in j, (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "fit"], capture_output=True, text=True, env=env, timeout=120)
        check("`fit` prints the plain-text sheet", r.returncode == 0 and "Machine:" in r.stdout and "Nothing here changes a setting" in r.stdout, r.stdout[-300:])

        # ---- v12: ship pipeline, related tests first, dataset export, Studio
        Chain.state["calls"] = []
        proj3 = os.path.join(tmp, "ship")
        os.makedirs(proj3)
        cfg9 = json.loads(json.dumps(cfg7))
        cfg9["behavior"]["evaluator"] = "off"
        es2 = core.Engine(proj3, "ship", "slugify function", cfg=cfg9, review="auto", say=quiet)
        out = fl.run_flow(es2, "ship", {"stages": "build,review"})
        rep3 = open(os.path.join(es2.run_dir, "report.md"), encoding="utf-8").read()
        check("the ship flow runs its stages as the existing flows (build, then review on the built files) and writes docs/ship.md",
              out.get("ok") and out["stages"]["build"]["ok"] and out["stages"]["review"]["ok"] and os.path.isfile(os.path.join(proj3, "docs", "ship.md"))
              and os.path.isfile(os.path.join(proj3, "docs", "review.md")) and "Pipeline: build -> review" in rep3, (out, rep3[-400:]))
        st3 = json.load(open(os.path.join(es2.run_dir, "run.json")))
        check("every finished stage is checkpointed for resume", set(st3["checkpoints"]["ship_done"]) == {"build", "review"} and st3["checkpoints"]["stages"] == ["build", "review"])
        import arch_pipeline as pp
        check("stages default by tier: a tiny builder gets build+review, a large one the whole pipeline", pp.BY_TIER["tiny"] == ["build", "review"] and pp.BY_TIER["large"] == pp.STAGES)
        check("ship is routable by keyword", fl.FLOWS["ship"].id == "ship" and "ship" in fl.KEYWORDS and "ship" in fl.PRIORITY)
        rel = fl.related_tests(es2, ["slugify.py", "tests/test_slugify.py", "other.py"])
        check("related_tests maps changed modules to their test files and keeps changed tests", rel == ["tests/test_slugify.py"], rel)
        # a failing related test comes back before the whole suite runs
        with open(os.path.join(proj3, "tests", "test_slugify.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef test_broken():\n    assert slugify('x') == 'nope'\n")
        v_fast = fl.verify(es2, ["slugify.py"])
        es2.cfg["behavior"]["fast_tests"] = "off"
        v_full = fl.verify(es2, ["slugify.py"])
        check("with fast_tests on, the failure names the related test file that was run first; off runs the whole suite", not v_fast["ok"] and "related tests first" in v_fast["text"] and not v_full["ok"] and "related tests first" not in v_full["text"], (v_fast["text"][:200], v_full["text"][:120]))
        import arch_dataset as ds
        data = ds.build([proj3, proj])
        out_dir = os.path.join(tmp, "datasets")
        summ = ds.write(data, out_dir)
        check("the dataset export keeps only accepted replies as SFT samples and pairs a rejected try with the accepted one on the same step as DPO",
              summ["stats"]["sft"] >= 1 and summ["stats"]["dpo"] >= 1 and all(r["chosen"] != r["rejected"] for rows in data["dpo"].values() for r in rows)
              and os.path.isfile(os.path.join(out_dir, "RECIPE.md")) and "ollama create" in open(os.path.join(out_dir, "RECIPE.md")).read(), summ["stats"])
        import arch_chat as chat
        chat.reset(root)
        check("Studio refuses a second message while answering, then answers through the traced engine",
              chat.ask(root, "what does f do?") and not chat.ask(root, "again") and _wait(lambda: not chat.snapshot(root)["busy"]) and chat.snapshot(root)["messages"][-1]["who"] == "model" and chat.snapshot(root)["messages"][-1]["text"], chat.snapshot(root))

        import arch_autopilot as ap
        proj4 = os.path.join(tmp, "auto")
        os.makedirs(proj4)
        a1 = ap.add(proj4, "slugify function", "build")
        a2 = ap.add(proj4, "Slugify function ", "auto")
        check("the backlog dedupes an open goal and records flow and source", a1 and a2 is None and ap.load(proj4)["items"][0]["flow"] == "build" and ap.load(proj4)["items"][0]["source"] == "you")
        ap.add(proj4, "second item that will fail")
        ap.add(proj4, "third item, left for the budget")
        calls = []
        def fake_run(item):
            calls.append(item["goal"])
            return {"ok": item["goal"] == "slugify function", "error": None if item["goal"] == "slugify function" else "scripted failure", "run": "r1"}
        summ = ap.run(proj4, hours=1, max_items=2, review="auto", say=quiet, run_one=fake_run)
        bl = ap.load(proj4)
        check("autopilot drains the backlog in order within the item budget, marks done/failed, keeps the rest, and writes docs/autopilot.md",
              calls == ["slugify function", "second item that will fail"] and [i["status"] for i in bl["items"]] == ["done", "failed", "todo"] and summ["stopped"] == "budget"
              and "scripted failure" in open(os.path.join(proj4, "docs", "autopilot.md")).read(), (calls, summ))
        def boom(item):
            raise RuntimeError("crash")
        summ2 = ap.run(proj4, hours=1, max_items=5, review="auto", say=quiet, run_one=boom)
        check("a crashing item is marked failed and does not stop the queue; ids are unique even for items added in the same instant",
              summ2["failed"] and ap.load(proj4)["items"][2]["status"] == "failed" and len({i["id"] for i in ap.load(proj4)["items"]}) == 3)
        with open(os.path.join(proj3, "docs", "review.md"), "a", encoding="utf-8") as f:
            f.write("\n- slugify.py: no test for empty input\n")
        class Prop(Chain):
            def script(self, role, user, full, st):
                if "write the NEXT engineering tasks" in user:
                    return json.dumps([{"goal": "add a test for slugify('') in tests/test_slugify.py", "flow": "test", "why": "review found it"}, {"goal": "x", "flow": "nope"}, {"bad": 1}])
                return Chain.script(self, role, user, full, st)
        srv2 = ThreadingHTTPServer(("127.0.0.1", 0), Prop)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        old_host = os.environ["OLLAMA_HOST"]
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv2.server_address[1]}"
        try:
            added = ap.propose(proj3, n=5, say=quiet, cfg=cfg)
        finally:
            os.environ["OLLAMA_HOST"] = old_host
            srv2.shutdown()
        check("propose turns findings into backlog items through the planner, keeps only well-formed ones (code-checked), and runs nothing",
              len(added) == 1 and added[0]["source"] == "proposed" and added[0]["flow"] == "test" and ap.load(proj3)["items"][0]["status"] == "todo", added)
        import arch_templates as tpl
        check("the agent and MCP templates exist with tests and are picked by keyword", {"agent_tool", "mcp_server"} <= set(tpl.TEMPLATES) and tpl.suggest("an MCP server for my notes")[0] == "mcp_server" and tpl.suggest("a local agent with tool calling")[0] == "agent_tool"
              and all("tests/" in "".join(tpl.TEMPLATES[t]["files"]) for t in ("agent_tool", "mcp_server")))

        # ---- the setting is schema-backed
        import arch_params
        check("behavior.swap_aware is in the settings schema with on/off", any(p == "behavior.swap_aware" and t == "choice:on,off" for sec in arch_params.SETTINGS.values() for p, t, *_ in sec))
    finally:
        srv.shutdown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    n, ok = len(results), sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CHAIN/PREFIX CHECKS PASSED' if ok == n else str(n - ok) + ' of ' + str(n) + ' CHAIN/PREFIX CHECKS FAILED'}\n")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(run())
