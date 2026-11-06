"""Self-test for arch_swap: placement math, score labelling, lint rules, fake trial, apply/undo. No GPU, isolated home.

    python arch_selftest_swap.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HW = {"vram_gb": 6.0, "ram_gb": 32.0}
BW = {"gpu_bw": 158.0, "ram_bw": 19.5, "source": "fixture"}
DENSE8 = {"name": "fit:8b", "size_gb": 4.7, "eff_b": 8.2, "quant": "Q4_K_M", "caps": ["completion"], "moe": False,
          "arch": {"arch": "qwen3", "layers": 36, "kv_heads": 8, "head_dim": 128, "params_b": 8.2}}
DENSE14 = {"name": "big:14b", "size_gb": 9.0, "eff_b": 14.8, "quant": "Q4_K_M", "caps": ["completion"], "moe": False,
           "arch": {"arch": "qwen3", "layers": 40, "kv_heads": 8, "head_dim": 128, "params_b": 14.8}}
MOE = {"name": "moe:35b", "size_gb": 13.5, "eff_b": 14.9, "quant": "Q4_K_M", "caps": ["completion"], "moe": True,
       "arch": {"arch": "qwen35moe", "layers": 40, "kv_heads": 2, "head_dim": 256, "experts": 256, "experts_used": 8, "params_b": 34.7}}
Q2 = {"name": "tiny2bit:9b", "size_gb": 3.0, "eff_b": 9.0, "quant": "Q2_K", "caps": ["completion"], "moe": False,
      "arch": {"arch": "qwen3", "layers": 32, "kv_heads": 8, "head_dim": 128, "params_b": 9.0}}
DRAFT = {"name": "draft:0.6b", "size_gb": 0.5, "eff_b": 0.6, "quant": "Q8_0", "caps": ["completion"], "moe": False,
         "arch": {"arch": "qwen3", "layers": 28, "kv_heads": 8, "head_dim": 128, "params_b": 0.6}}
FACTS = [DENSE8, DENSE14, MOE, Q2, DRAFT]
TOK = {"fit:8b": ("gpt2", "qwen2", 151936), "big:14b": ("gpt2", "qwen2", 151936), "draft:0.6b": ("gpt2", "qwen2", 151936),
       "moe:35b": ("gpt2", "qwen35", 248320), "tiny2bit:9b": ("gpt2", "qwen2", 151936)}


def run():
    home = tempfile.mkdtemp(prefix="selftest_swap_")
    prev = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST")}
    os.environ.update(ARCHITECT_DATA_DIR=os.path.join(home, "data"), ARCHITECT_NO_CACHE="1", ARCHITECT_SELFTEST="1")
    import arch_core as core
    old_home = core.HOME
    core.set_home(home)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    try:
        import arch_swap as sw
        import arch_jobs
        import model_hub as mh
        mh.CONFIG_FILE = core.CONFIG_FILE
        cfg = {"tiers": {"router": "draft:0.6b", "worker": "fit:8b", "heavy": "moe:35b", "draft": ""},
               "roles": {"planner": "moe:35b", "architect": "moe:35b", "generator": "tiny2bit:9b"},
               "server": {"kv": "q8_0", "ollama_gpu": "auto"}, "limits": {"num_ctx": 12288}, "model_overrides": {}}
        mh.write_config(json.loads(json.dumps(cfg)))
        sp = {"desk_gb": 0.2, "processes": [
            {"tier": "router", "name": "draft:0.6b", "ctx": 4096, "ram_gb": 0.5, "resident": True},
            {"tier": "worker", "name": "fit:8b", "ctx": 8192, "ram_gb": 0.0, "resident": True},
            {"tier": "heavy", "name": "moe:35b", "ctx": 8192, "ram_gb": 8.6, "resident": False}]}

        def E(c=cfg, **k):
            base = dict(hw=HW, desk_gb=0.2, bw=BW, vm={"vm_gb": 2.0}, facts=FACTS, results=[], server_plan=sp, tokenizer=lambda n: TOK.get(n, (None, None, None)))
            base.update(k)
            return sw.env(c, **base)

        # ---- placement
        p = sw.placement(DENSE8, HW, 8192, desk_gb=0.2, bw=BW)
        check("placement: an 8B Q4 fits whole at 8k q8_0", p["full"] and p["where"] == "card" and p["ngl"] == 36, p)
        check("placement: KV bytes at q8_0 = 2*L*kvh*hd*1.0625", p["kv_bytes_per_token"] == round(2 * 36 * 8 * 128 * 1.0625), p["kv_bytes_per_token"])
        check("placement: tok/s from the bandwidth model ~ gpu_bw / size", abs(p["est_tok_s"] - round(158.0 / 4.7, 1)) < 0.2, p["est_tok_s"])
        p = sw.placement(DENSE14, HW, 8192, desk_gb=0.2, bw=BW)
        check("placement: a 14B does not fit whole -> split, slower", not p["full"] and p["where"] == "split" and p["est_tok_s"] < 10, p)
        p = sw.placement(MOE, HW, 8192, desk_gb=0.2, bw=BW, vm={"vm_gb": 2.0})
        check("placement: MoE gets --n-cpu-moe with experts in RAM", p["where"] == "moe_offload" and p["n_cpu_moe"] > 0 and p["ram_gb"] > 5, p)
        check("placement: RAM headroom counts Windows + Docker/WSL", abs(p["ram_headroom_gb"] - round(32 - 6 - 2 - p["ram_gb"], 2)) < 0.02, p["ram_headroom_gb"])
        check("placement: VRAM headroom >= 300 MiB kept", p["vram_headroom_gb"] >= 0.29, p["vram_headroom_gb"])

        # ---- plan + score labelling
        pl = sw.plan(target={"job": "code"}, e=E())
        o = {x["model"]: x for x in pl["options"]}
        check("plan: score is labelled estimate when nothing is measured", o["fit:8b"]["score_source"].startswith("estimate"), o["fit:8b"]["score_source"])
        check("plan: the 14B (split) is not offered as builder", not o["big:14b"]["eligible"])
        check("plan: Q2 is not offered for code", not o["tiny2bit:9b"]["eligible"] and any("2-bit" in c["msg"] for c in o["tiny2bit:9b"]["checks"]))
        check("plan: eligible options rank first", pl["options"][0]["eligible"])
        check("plan: draft with identical tokenizer at <= 1/3 size is found", o["fit:8b"]["speculation"]["drafts"] == ["draft:0.6b"], o["fit:8b"]["speculation"])
        check("plan: MoE with another tokenizer falls back to ngram", o["moe:35b"]["speculation"]["use"] == "ngram", o["moe:35b"]["speculation"])
        check("plan: code settings say thinking off", o["fit:8b"]["settings"]["thinking"] is False)
        rows = [{"kind": "task", "job": "code", "model": "fit:8b", "verdict": "pass" if i % 4 else "fail", "secs": 40.0} for i in range(10)]
        o2 = {x["model"]: x for x in sw.plan(target={"job": "code"}, e=E(results=rows))["options"]}
        check("plan: n >= 8 measured -> score is measured C", o2["fit:8b"]["score_source"] == "measured" and o2["fit:8b"]["score"] == o2["fit:8b"]["measured"]["C"], o2["fit:8b"]["score_source"])
        o3 = {x["model"]: x for x in sw.plan(target={"job": "code"}, e=E(results=rows[:3]))["options"]}
        check("plan: a few measured tasks -> prior-adjusted estimate", o3["fit:8b"]["score_source"].startswith("prior-adjusted"), o3["fit:8b"]["score_source"])
        check("plan: tier target maps to its job", sw.plan(target={"tier": "heavy"}, e=E())["job"] == "plan")

        # ---- lint
        lf = sw.lint(e=E())
        rules = {x["rule"] for x in lf}
        check("lint: flags the CPU-only MoE via Ollama", "moe_cpu_ollama" in rules, rules)
        check("lint: flags Q2 for code", any(x["rule"] == "code_quant" and "tiny2bit" in x["msg"] and x["level"] == "error" for x in lf), lf)
        check("lint: flags planner ctx too large", any(x["rule"] == "ctx_per_role" and "planner" in x["msg"] for x in lf))
        check("lint: flags a role model outside the tiers (swap churn)", "swap_churn" in rules)
        check("lint: every finding has a reason and a fix", all(x["why"] and x["fix"] for x in lf))
        good = dict(cfg, roles={}, limits={"num_ctx": 8192}, model_overrides={})
        sp_ok = dict(sp, processes=[dict(x, resident=True) for x in sp["processes"]])
        lg = sw.lint(e=E(good, server_plan=sp_ok))
        check("lint: a clean config gives no errors", not [x for x in lg if x["level"] == "error"], lg)

        # ---- cache / load lint checks
        cc = {x["rule"]: x for x in sw.cache_checks(e=E())}
        check("cache_checks: one row per cache/load feature, each with why, evidence tier, verify and fix",
              set(cc) == set(sw.CACHE_RULES) and all(x["why"] and x["evidence"][:3] in ("[M]", "[B]", "[E]", "[C]") and x["verify"] and x["fix"] and x["state"] in ("on", "off") for x in cc.values()), sorted(cc))
        check("cache_checks: every row says whether the feature is on", all(("ON" in x["msg"]) == (x["state"] == "on") for x in cc.values()))
        check("cache_checks: defaults (cache_reuse 256, stable_context, tool_cache) are on; prewarm is opt-in/off",
              cc["cache_reuse"]["state"] == "on" and cc["cache_stable_context"]["state"] == "on" and cc["cache_tool_cache"]["state"] == "on" and cc["cache_prewarm"]["state"] == "off")
        check("cache_checks: verify names the verify_machine --gpu step for server features", "verify_machine.py --gpu" in cc["cache_reuse"]["verify"])
        bad = dict(cfg, server=dict(cfg["server"], cache_reuse=0, slot_persist="off", mlock="off", ollama_keep_alive="30m", ubatch=32, heavy_beside="off"),
                   behavior={"stable_context": False, "tool_cache": "off"}, squad={"digest": "off"})
        cb = {x["rule"]: x for x in sw.cache_checks(e=E(bad))}
        check("cache_checks: turned-off features are reported off",
              all(cb[r]["state"] == "off" for r in ("cache_reuse", "cache_slot_persist", "load_mode", "cache_ollama_keep_alive", "cache_batch", "cache_heavy_beside",
                                                    "cache_stable_context", "cache_tool_cache", "cache_squad_prefix")), {r: x["state"] for r, x in cb.items()})
        lb = sw.lint(e=E(bad))
        check("lint: includes the off cache features with evidence and verify", any(x["rule"] == "cache_reuse" and x["evidence"] and x["verify"] for x in lb))
        check("lint: no cache finding for a feature that is on", not any(x["rule"] == "cache_tool_cache" for x in lf))
        check("lint_text prints evidence and verify", "verify:" in sw.lint_text(lb))
        check("lint(cache=False) keeps the old findings only", not any(x["rule"].startswith("cache_") for x in sw.lint(e=E(bad), cache=False)))

        # ---- per-call swap
        wfs = {"w.yaml": {"phases": [{"agent": "planner"}, {"agent": "coder"}, {"panel": "verifier", "n": 3}, {"parallel": [{"agent": "auditor"}, {"agent": "auditor"}]}]}}
        check("agent_runs: counts phases, parallel lists and panels", sw.agent_runs(wfs["w.yaml"]) == {"planner": 1, "coder": 1, "verifier": 3, "auditor": 2})
        heavy_of = lambda a: "heavy" if a in ("planner", "verifier") else "worker"
        check("heavy_repeat_agents: only a heavy agent that runs more than once", sw.heavy_repeat_agents(wfs, heavy_of) == {"verifier": (3, "w.yaml")})
        pc = {x["rule"]: x for x in sw.cache_checks(e=E(), workflows=wfs, model_of=heavy_of)}
        check("per_call_swap: flagged while heavy is not resident beside the worker", pc["per_call_swap"]["state"] == "off" and "verifier runs 3x" in pc["per_call_swap"]["msg"], pc["per_call_swap"])
        sp_res = dict(sp, processes=[dict(x, resident=True) for x in sp["processes"]])
        pc2 = {x["rule"]: x for x in sw.cache_checks(e=E(server_plan=sp_res), workflows=wfs, model_of=heavy_of)}
        check("per_call_swap: fine when heavy is resident beside the worker", pc2["per_call_swap"]["state"] == "on")
        pc3 = {x["rule"]: x for x in sw.cache_checks(e=E(), workflows=wfs, model_of=lambda a: "worker")}
        check("per_call_swap: fine when the repeating agent is on the worker", pc3["per_call_swap"]["state"] == "on")
        real = sw.heavy_repeat_agents()
        check("shipped agents: the verifier is on the worker, so no shipped workflow repeats a heavy agent", "verifier" not in real and sw._agent_model("verifier") == "worker" and sw._agent_model("planner") == "heavy", real)
        import arch_roleprofile as rp
        check("roleprofile: verifier -> worker, planner stays heavy", rp.agent_tier("verifier") == "worker" and rp.agent_tier("planner") == "heavy")

        # ---- swap advisor on new downloads
        NEW = {"name": "new:8b", "size_gb": 4.9, "eff_b": 8.0, "quant": "Q4_K_M", "caps": ["completion"], "moe": False,
               "arch": {"arch": "qwen3", "layers": 36, "kv_heavy": 8, "kv_heads": 8, "head_dim": 128, "params_b": 8.0}}
        built = []
        r0 = sw.on_new_models(e=E(facts=FACTS + [NEW]), installed=["fit:8b", "new:8b"], have_passport=lambda n: n != "new:8b",
                              build_passports=lambda names: built.extend(names) or {}, identify=lambda n: {"family": "qwen3", "line": "qwen3", "modifiers": [], "matched_by": "test"})
        rec = r0["suggestions"][0] if r0["suggestions"] else {}
        check("on_new_models: only the model without a passport is new, and its passport is built", r0["new"] == ["new:8b"] and built == ["new:8b"], r0["new"])
        check("on_new_models: identifies the family and plans every job", rec.get("family") == "qwen3" and {j["job"] for j in rec["jobs"]} == set(arch_jobs.JOBS), rec.get("jobs"))
        check("on_new_models: writes a suggestion record and applies nothing", rec.get("applied") is False and os.path.exists(sw._suggestions_path())
              and mh.read_config()["tiers"]["worker"] == "fit:8b")
        lines = [json.loads(x) for x in open(sw._suggestions_path(), encoding="utf-8")]
        check("on_new_models: the record on disk matches", lines[-1]["model"] == "new:8b" and lines[-1]["applied"] is False)
        check("on_new_models: suggestions point at swap --trial, never --apply", all("--trial" in x and "--apply" not in x for x in rec["next"]) and "Nothing was changed" in r0["text"])
        r1 = sw.on_new_models(e=E(), installed=["fit:8b"], have_passport=lambda n: True, write=False)
        check("on_new_models: nothing new -> nothing written", r1["new"] == [] and r1["suggestions"] == [])
        r2 = sw.on_new_models(e=E(), installed=["ghost:1b"], have_passport=lambda n: False, build_passports=lambda names: {}, write=False)
        check("on_new_models: a model whose passport cannot be built is reported, not guessed", r2["suggestions"][0]["jobs"] == [] and "no usable passport" in r2["suggestions"][0]["note"])

        # ---- trial (fake)
        tasks = [{"id": f"t{i}", "family": "build"} for i in range(5)]
        tr = sw.trial("worker", "fit:8b", fake=True, cfg=cfg, tasks=tasks, facts=FACTS,
                      runner=lambda m, j, t, s: {"verdict": "pass", "secs": 10.0, "tok_s": 40.0}, say=lambda *_: None)
        check("trial: fake run passes with 3 tasks and a speed", tr["verdict"] == "pass" and len(tr["tasks"]) == 3 and tr["tok_s"] == 40.0, tr)
        tr2 = sw.trial("worker", "noedit:7b", fake=True, cfg=cfg, tasks=tasks, facts=FACTS, say=lambda *_: None)
        check("trial: failed edit format -> fail, no tasks", tr2["verdict"] == "fail" and not tr2["tasks"], tr2)
        tr3 = sw.trial("worker", "slow:8b", fake=True, cfg=cfg, tasks=tasks, facts=FACTS,
                       runner=lambda m, j, t, s: {"verdict": "pass", "secs": 10.0, "tok_s": 3.0}, say=lambda *_: None)
        check("trial: too slow for the worker -> not_sure", tr3["verdict"] == "not_sure", tr3)

        # ---- apply / undo
        r = sw.apply("worker", "big:14b", yes=True)
        check("apply: refuses without a passed trial", not r["ok"] and "no passed trial" in r["msg"], r)
        r = sw.apply("worker", "fit:8b", yes=False)
        check("apply: refuses without --yes and changes nothing", not r["ok"] and mh.read_config()["tiers"]["worker"] == "fit:8b", r)
        mh.write_config(dict(mh.read_config(), tiers=dict(cfg["tiers"], worker="old:7b")))
        r = sw.apply("worker", "fit:8b", yes=True)
        check("apply: writes the tier after a passed trial", r["ok"] and mh.read_config()["tiers"]["worker"] == "fit:8b", r)
        u = sw.undo()
        check("undo: restores the tier", u and mh.read_config()["tiers"]["worker"] == "old:7b", mh.read_config()["tiers"])
        sw.trial("code", "fit:8b", fake=True, cfg=cfg, tasks=tasks, facts=FACTS,
                 runner=lambda m, j, t, s: {"verdict": "pass", "secs": 10.0, "tok_s": 40.0}, say=lambda *_: None)
        r = sw.apply("job:code", "fit:8b", yes=True)
        roles = mh.read_config()["roles"]
        check("apply: a job writes every role of the job", r["ok"] and all(roles.get(x) == "fit:8b" for x in arch_jobs.JOBS["code"]["roles"]), roles)
        sw.undo()
        roles = mh.read_config()["roles"]
        check("undo: restores the job's roles", roles.get("generator") == "tiny2bit:9b" and "fullstack" not in roles, roles)
        check("undo: nothing left -> None", sw.undo() is None)
        r = sw.apply("worker", "big:14b", yes=True, force=True)
        check("apply: --force overrides the trial requirement, recorded", r["ok"] and r["record"]["forced"], r)
        sw.undo()
    finally:
        core.set_home(old_home)
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    print(f"swap: {sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
