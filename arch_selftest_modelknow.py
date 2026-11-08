"""Self-test for arch_modelknow: family identification, modifiers, job priors, settings. Reads passports; no model, no network."""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import arch_modelknow as k  # noqa: E402


def run():
    fails, ran = [], []

    def check(name, ok, extra=""):
        ran.append(name)
        print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra else ""))
        if not ok:
            fails.append(name)

    data = k.kb()
    check("family file loads with >= 20 families", len(data["families"]) >= 20, len(data["families"]))
    need = {"qwen3", "qwen3.5", "qwen3.6", "qwen3.8", "qwen2.5-coder", "qwen3-coder", "llama3", "hermes", "dolphin", "deepseek",
            "deepseek-r1-distill", "deepseek-coder-v2", "mistral", "devstral", "gemma", "phi", "granite", "glm", "gpt-oss", "openhermes",
            "nemotron", "bonsai"}
    check("required families present", need <= {f["id"] for f in data["families"]}, sorted(need - {f["id"] for f in data["families"]}))
    for f in data["families"]:
        for j, v in f.get("jobs", {}).items():
            if not (0 <= v[0] <= 1):
                check(f"prior in 0-1 ({f['id']}/{j})", False, v)

    pdir = os.path.join(HERE, "hub_data", "passports")
    unknown = []
    rows = []
    for fn in sorted(glob.glob(os.path.join(pdir, "*.json"))):
        with open(fn, encoding="utf-8") as fh:
            p = json.load(fh)
        i = k.identify(p)
        if not i["family"]:
            unknown.append(p.get("name"))
        rows.append((p.get("name"), i["family"], i["modifiers"]))
    check("every installed model identified", not unknown, unknown or f"{len(rows)} models")

    for tag in ["hermes3:8b", "hermes3", "nous-hermes2:10.7b", "NousResearch/Hermes-3-Llama-3.1-8B-GGUF", "Hermes-4-14B-Q4_K_M.gguf",
                {"name": "x.gguf", "arch": {"arch": "llama"}, "lineage": {"model_name": "Hermes 3 Llama 3.1 8B", "finetune": "Hermes-3"}}]:
        check(f"Hermes matched: {tag if isinstance(tag, str) else 'GGUF name'}", k.identify(tag)["family"] == "hermes", k.identify(tag)["family"])
    check("OpenHermes is not Hermes", k.identify("openhermes:7b-mistral")["family"] == "openhermes")
    check("dolphin matched", k.identify("dolphin3:8b")["family"] == "dolphin")
    check("qwen3-coder before qwen3", k.identify("qwen3-coder:30b")["family"] == "qwen3-coder")
    check("R1 distill before qwen", k.identify("deepseek-r1:7b")["family"] == "deepseek-r1-distill")

    ab = k.identify("huihui_ai/qwen3-abliterated:8b")
    check("abliterated modifier", "abliterated" in ab["modifiers"], ab["modifiers"])
    check("mlabonne abliterated tag", "abliterated" in k.identify("mlabonne/NeuralDaredevil-8B-abliterated")["modifiers"])
    moe = k.identify({"name": "eriktonon/qwen3.6-moe-uncensored:latest", "moe": True, "arch": {"arch": "qwen35moe", "experts": 256}})
    check("MoE detected", "moe" in moe["modifiers"], moe["modifiers"])
    check("MoE from name", "moe" in k.identify("qwen3-coder:30b-a3b")["modifiers"])
    check("MTP detected", "mtp" in k.identify("s1gnature/gemma4-uncensored-mtp:e4b-q4_k_m")["modifiers"])
    gr = k.identify({"name": "g:8b", "lineage": {"base_model": "Ibm Granite/Granite 4.1 8b Base"}})
    check("base modifier not taken from the base_model field", "base" not in gr["modifiers"], gr["modifiers"])

    coder = {"name": "qwen2.5-coder:7b", "eff_b": 7.6, "quant": "Q4_K_M"}
    gen = {"name": "llama3.2:3b", "eff_b": 3.2, "quant": "Q4_K_M"}
    gen8 = {"name": "llama3.1:8b", "eff_b": 8.0, "quant": "Q4_K_M"}
    check("coder ranks above general for code", k.job_prior(coder, "code")[0] > k.job_prior(gen8, "code")[0] > k.job_prior(gen, "code")[0],
          (k.job_prior(coder, "code")[0], k.job_prior(gen8, "code")[0], k.job_prior(gen, "code")[0]))
    h = {"name": "hermes3:8b", "eff_b": 8.0, "quant": "Q4_K_M"}
    check("hermes ranks route above code", k.job_prior(h, "route")[0] > k.job_prior(h, "code")[0])
    check("quant lowers prior", k.job_prior(dict(coder, quant="Q2_K"), "code")[0] < k.job_prior(coder, "code")[0])
    check("reasons given", len(k.job_prior(coder, "code")[1]) >= 2)

    u = k.identify({"name": "zorblax-coder:7b", "arch": {"arch": "weirdarch"}})
    check("unknown model says so", u["family"] is None and "unknown family" in u["note"] and "measure it" in u["note"], u.get("note"))
    s, r = k.job_prior({"name": "zorblax-coder:7b", "eff_b": 7}, "code")
    check("unknown coder gets a name-word nudge and an honest reason", s > 0.4 and any("unknown" in x for x in r), (s, r[:2]))
    check("unknown explain says unknown", "unknown family" in k.explain({"name": "zorblax:7b"}))

    st = k.settings_for(h, "route")
    check("settings_for Hermes gives <tool_call> format", "<tool_call>" in st["tool_format"], st["tool_format"])
    check("route runs cold", st["sampling"].get("temperature") == 0.0, st["sampling"])
    q = k.settings_for({"name": "qwen3:8b"}, "plan")
    check("qwen3 plan thinks", q["thinking"] is True)
    check("qwen3 route does not think", k.settings_for({"name": "qwen3:8b"}, "route")["thinking"] is False)
    check("explain mentions tool calls for Hermes", "tool_call" in k.explain(h))

    try:
        import arch_select
        order = arch_select.screen_order(["llama3.2:3b", "qwen2.5-coder:7b"], ["code"], {"llama3.2:3b": gen, "qwen2.5-coder:7b": coder})
        check("arch_select screens the coder first for code", order[0] == "qwen2.5-coder:7b", order)
    except Exception as ex:
        check("arch_select wiring", False, ex)
    try:
        import arch_passport
        p = arch_passport.build("hermes3:8b", show={"details": {"family": "llama"}, "model_info": {}, "capabilities": ["completion", "tools"]})
        check("passport carries the family line", "Hermes" in (p.get("family_line") or ""), p.get("family_line"))
    except Exception as ex:
        check("passport wiring", False, repr(ex))

    print("\nInstalled models:")
    for name, fam, mods in rows:
        print(f"  {name:<48} {str(fam):<14} {', '.join(mods)}")
    print(f"\nMODELKNOW: PASSED ({len(ran)} checks)" if not fails else f"\nMODELKNOW: FAILED {len(fails)} of {len(ran)}: {fails}")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
