"""Self-test for per-model optimisation: family matching on real-world names (merges, abliterated builds), only-empty-keys apply, undo,
server settings, MoE advice, and the CLI/hub paths. No model and no internet needed."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _row(name, gb, params, experts=None, used=None, verdict="fast", embed=False):
    arch = {"params": float(params) * 1e9 if params else None}
    if experts:
        arch.update(experts=experts, experts_used=used)
    return {"name": name, "size_gb": gb, "params": f"{params}B", "embed": embed, "arch": arch, "agg": {}, "know": {},
            "rec": {"verdict": verdict, "est_tok_s": 20, "num_ctx": 16384}}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_autotune as at
    import arch_core as core
    import arch_modelopt as mo
    import arch_params as params
    import model_hub as mh

    home = tempfile.mkdtemp(prefix="archopt_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_HOME", "ARCHITECT_DATA_DIR")}
    core.set_home(home)
    os.environ.pop("ARCHITECT_DATA_DIR", None)
    hw = {"gpu": "T", "vendor": "nvidia", "vram_gb": 16.0, "ram_gb": 32.0, "cpu_cores": 8, "disk_free_gb": 100.0, "notes": [], "fingerprint": "f"}
    cpu = dict(hw, gpu=None, vendor=None, vram_gb=0.0)
    rows = [_row("huihui_ai/qwen3-coder-abliterated:30b", 18.6, 30.5, 128, 8, verdict="split"), _row("gemma4:26b", 13.5, 26, 128, 8),
            _row("qwen3:8b", 5.2, 8), _row("mystery-model:7b", 4.5, 7), _row("nomic-embed-text", 0.3, 0.137, embed=True)]
    try:
        k, s = mo.sampling_for("huihui_ai/qwen3-coder-abliterated:30b")
        check("an abliterated merge gets its base family's guidance (Qwen3-Coder), not the generic Qwen3 one",
              k["family"].startswith("Qwen3-Coder") and s == {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05}, (k["family"], s))
        check("plain Qwen3 and Gemma resolve to their own families", mo.sampling_for("qwen3:8b")[1]["temperature"] == 0.6 and mo.sampling_for("gemma4:26b")[1]["temperature"] == 1.0)
        check("an unknown family gets no invented numbers", mo.sampling_for("mystery-model:7b")[1] is None)

        cfg = {"default_model": "qwen3:8b", "roles": {}, "model_overrides": {"qwen3:8b": {"sampling": {"temperature": 0.3}}}}
        p = mo.plan(hw, rows, cfg, None)
        by = {m["name"]: m for m in p["models"]}
        check("embedding models are not listed", "nomic-embed-text" not in by and len(by) == 4, list(by))
        q = by["qwen3:8b"]
        check("a value you set yourself is kept, the rest is proposed", q["kept"] == {"temperature": 0.3} and "sampling.temperature" not in q["proposed"]
              and q["proposed"] == {"sampling.top_p": 0.95, "sampling.top_k": 20}, q)
        check("an unknown family proposes nothing and says so", not by["mystery-model:7b"]["proposed"] and by["mystery-model:7b"]["notes"] and not by["mystery-model:7b"]["has_changes"])
        check("every proposed key is a valid override field", all(_ok(params, k_, v) for m in p["models"] for k_, v in m["proposed"].items()))
        check("a MoE model that does not fit the GPU is told about expert offload; one that fits is not",
              "--cpu-moe" in (by["huihui_ai/qwen3-coder-abliterated:30b"]["moe_tip"] or "") and by["gemma4:26b"]["moe_tip"] is None, by["gemma4:26b"]["moe_tip"])
        check("the uncensored build is labelled as such and kept in the list", "uncensored" in by["huihui_ai/qwen3-coder-abliterated:30b"]["tags"])
        check("every note names where it comes from", all(src in ("vendor", "inference", "measured") for m in p["models"] for src, _ in m["notes"]))

        # ---- apply / undo (writes the real config file in the temp home)
        at._wire()
        base = {"default_model": "qwen3:8b", "roles": {"generator": "gemma4:26b"}, "model_overrides": {"qwen3:8b": {"sampling": {"temperature": 0.3}, "num_ctx": 8192}}}
        mh.write_config(base)
        ch = mo.apply(rows, hw, say=lambda m: None)
        after = mh.read_config()
        ov = after["model_overrides"]
        check("apply writes the family sampling for every model that has guidance", len(ch) == 4 + 3 + 2 and ov["gemma4:26b"]["sampling"]["top_k"] == 64, ch)
        check("apply keeps your own temperature and context size", ov["qwen3:8b"]["sampling"]["temperature"] == 0.3 and ov["qwen3:8b"]["num_ctx"] == 8192, ov["qwen3:8b"])
        check("apply never touches roles or the default model", after["roles"] == base["roles"] and after["default_model"] == "qwen3:8b")
        check("a second apply has nothing left to write", mo.apply(rows, hw, say=lambda m: None) == [])
        mo.undo(say=lambda m: None)
        check("undo restores the config exactly as it was before", mh.read_config()["model_overrides"] == base["model_overrides"] and mh.read_config()["roles"] == base["roles"], mh.read_config()["model_overrides"])
        try:
            mo.undo(say=lambda m: None)
            check("a second undo says there is nothing to undo", False)
        except at.TuneError as e:
            check("a second undo says there is nothing to undo", "nothing to undo" in str(e))
        try:
            mo.apply(rows, hw, models=["not-installed:1b"], say=lambda m: None)
            check("naming a model that is not installed is an error, not a silent skip", False)
        except at.TuneError as e:
            check("naming a model that is not installed is an error, not a silent skip", "not-installed:1b" in str(e))
        mh.write_config({"model_overrides": {}})
        ch = mo.apply(rows, hw, models=["gemma4:26b"], say=lambda m: None)
        o2 = mh.read_config()["model_overrides"]
        check("--model limits apply to the named model", set(o2) == {"gemma4:26b"} and len(ch) == 3, o2)
        mo.undo(say=lambda m: None)
        check("undo removes an override block it created", mh.read_config()["model_overrides"] == {})

        # ---- server settings
        srv = {x["env"]: x for x in mo.server_plan(mo.at.owned_models(rows, {"default_model": "qwen3:8b"}), {"default_model": "qwen3:8b"}, hw)}
        check("the plan sets one parallel slot and flash attention on a GPU machine", srv["OLLAMA_NUM_PARALLEL"]["value"] == "1" and srv["OLLAMA_FLASH_ATTENTION"]["value"] == "1")
        check("a KV-cache type is only suggested when tune found the window too tight", "OLLAMA_KV_CACHE_TYPE" not in srv)
        tight = {"models": {"qwen3:8b": {"kv_tip": "x"}}}
        srv2 = {x["env"]: x for x in mo.server_plan(mo.at.owned_models(rows, {}), {}, hw, tight)}
        check("...and then it says to check quality first", "q8_0" == srv2["OLLAMA_KV_CACHE_TYPE"]["value"] and "Probe" in srv2["OLLAMA_KV_CACHE_TYPE"]["why"])
        srv3 = {x["env"]: x for x in mo.server_plan(mo.at.owned_models(rows, {}), {}, cpu)}
        check("a CPU-only machine gets no GPU-only setting", "OLLAMA_FLASH_ATTENTION" not in srv3)
        team_fit = {"roles": {"generator": "qwen3:8b", "planner": "mystery-model:7b"}}
        team_big = {"roles": {"generator": "gemma4:26b", "planner": "huihui_ai/qwen3-coder-abliterated:30b"}}
        own = mo.at.owned_models(rows, {})
        f = {x["env"]: x["value"] for x in mo.server_plan(own, team_fit, hw)}
        b = {x["env"]: x["value"] for x in mo.server_plan(own, team_big, hw)}
        check("loaded-model limit follows whether the team fits together (2 small fit; 2 big do not)", f["OLLAMA_MAX_LOADED_MODELS"] == "2" and b["OLLAMA_MAX_LOADED_MODELS"] == "1", (f, b))
        check("server lines are produced for systemd and for Windows",
              any("Environment=" in ln for ln in mo.server_lines(srv.values(), windows=False)) and any(ln.startswith("setx ") for ln in mo.server_lines(srv.values(), windows=True)))
        check("the stack advice recommends llama.cpp only when a MoE model is split", any("llama-server" in a["advice"] for a in p["stack"])
              and not any("llama-server" in a["advice"] for a in mo.plan(hw, rows[1:], {}, None)["stack"]))

        # ---- Hugging Face names, your own track record, edit style, quantisation
        hfk, hfs = mo.sampling_for("hf.co/mradermacher/Qwen3-Coder-30B-A3B-Instruct-abliterated-GGUF:Q4_K_M")
        check("a Hugging Face model name resolves to its family (repo name, quant suffix ignored)", hfk["family"].lower().startswith("qwen3") and hfs and hfs["temperature"] is not None, hfk["family"])
        check("a Hugging Face MoE model is sized from its A3B tag", mo.at.owned_models([_row("hf.co/u/Qwen3-Coder-30B-A3B-GGUF:Q4_K_M", 17.0, 30)], {})[0]["active_b"] == 3.0
              or "MoE" in mo.at.owned_models([_row("hf.co/u/Qwen3-Coder-30B-A3B-GGUF:Q4_K_M", 17.0, 30, experts=128, used=8)], {})[0]["tags"])
        o = dict(mo.at.owned_models([_row("qwen3:8b", 5.0, 8)], {})[0], quant="Q4_K_M", track={"n": 12, "ok": 5, "rate": 42, "by_role": {}}, edit_style="whole")
        adv = mo.advise_model(o, {}, hw)
        txt = " ".join(n[1] for n in adv["notes"])
        check("your own run record is shown with its count, and flagged low only with enough attempts", "5 of 12 attempts accepted (42%)" in txt and "Low for a builder" in txt)
        o2 = dict(o, track={"n": 3, "ok": 1, "rate": 33, "by_role": {}})
        t2 = " ".join(n[1] for n in mo.advise_model(o2, {}, hw)["notes"])
        check("with few attempts it says to treat the number as a hint and does not call it low", "treat this as a hint" in t2 and "Low for a builder" not in t2)
        check("an edit format other than patches that worked better is reported", "'whole' blocks" in txt)
        qa = mo.quant_advice(o, hw)
        check("a higher quant is suggested only when it fits the GPU with room for context, labelled an estimate",
              qa and "Q5_K_M" in qa and "estimate" in qa and mo.quant_advice(dict(o, size_gb=11.5), hw) is None and mo.quant_advice(dict(o, quant="Q8_0"), hw) is None
              and mo.quant_advice(dict(o, quant="weird"), hw) is None and mo.quant_advice(o, {"vram_gb": 0}) is None)

        import arch_intel as intel
        import urllib.error as ue
        check("only hf.co / huggingface.co names are looked up", intel.hf_repo("hf.co/u/r:Q4_K_M") == ("u/r", "Q4_K_M") and intel.hf_repo("huggingface.co/u/r") == ("u/r", None)
              and intel.hf_repo("qwen3:8b") is None and intel.hf_repo("huihui_ai/x:30b") is None)
        fx = intel.facts("hf.co/u/r:Q4_K_M", fetch=lambda url: {"lastModified": "2026-09-30T10:00:00Z", "downloads": 12345, "likes": 9, "gated": False,
                                                                 "tags": ["gguf", "license:apache-2.0"], "pipeline_tag": "text-generation"} if url.endswith("/u/r") else {})
        check("the metadata is read from the repo's API page and summarised, labelled as vendor-card evidence", fx["license"] == "apache-2.0" and fx["last_modified"] == "2026-09-30"
              and "12,345 downloads" in intel.notes_for(fx)[0][1] and intel.notes_for(fx)[0][0] == "vendor card")

        def nf(url):
            raise ue.HTTPError(url, 404, "nf", {}, None)

        def down(url):
            raise ue.URLError("offline")
        check("a missing repo or no internet is reported plainly and never raises", "not found" in intel.facts("hf.co/u/r", fetch=nf)["error"] and "could not reach" in intel.facts("hf.co/u/r", fetch=down)["error"]
              and intel.notes_for({"error": "x"})[0][0] == "unavailable")
        check("lookup skips models that are not from Hugging Face", list(intel.lookup(["qwen3:8b", "hf.co/u/r:Q4_K_M"], fetch=lambda u: {})) == ["hf.co/u/r:Q4_K_M"])

        # ---- CLI and hub paths against a scripted Ollama
        import arch_selftest_tune as t
        srv_ = ThreadingHTTPServer(("127.0.0.1", 0), t.FakeOllama)
        threading.Thread(target=srv_.serve_forever, daemon=True).start()
        env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{srv_.server_address[1]}", ARCHITECT_HOME=home)
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "optimize", "--json"], capture_output=True, text=True, env=env, timeout=60, cwd=home)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            j = {}
        check("`optimize --json` runs end to end and lists the installed models", r.returncode == 0 and {m["name"] for m in j.get("models", [])} == {"fake-small:8b", "fake-big:30b"}, (r.stdout[-200:], r.stderr[-300:]))
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "optimize"], capture_output=True, text=True, env=env, timeout=60, cwd=home)
        check("`optimize` prints a readable plan and says nothing is written yet", r.returncode == 0 and "nothing is written" in r.stdout and "OLLAMA_NUM_PARALLEL" in r.stdout, r.stdout[-300:] + r.stderr[-200:])
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "optimize", "--undo"], capture_output=True, text=True, env=env, timeout=60, cwd=home)
        check("`optimize --undo` with nothing applied exits 1 with a plain message", r.returncode == 1 and "nothing to undo" in r.stdout.lower(), r.stdout)
        srv_.shutdown()
        import architect_hub as hub
        src = open(os.path.join(HERE, "architect_hub.py"), encoding="utf-8").read()
        check("the hub exposes optimize and returns the plan in tune_info", '"/api/arch/optimize"' in src and '"opt": opt' in src and callable(hub.tune_info))
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    bad = results.count(False)
    print(f"\n  {'ALL ' + str(len(results)) + ' MODEL-OPTIMISER CHECKS PASSED' if not bad else str(bad) + ' of ' + str(len(results)) + ' CHECKS FAILED'}")
    return 0 if not bad else 1


def _ok(params, field, value):
    try:
        params.validate_override(field, value)
        return True
    except params.ValidationError:
        return False


if __name__ == "__main__":
    sys.exit(run())
