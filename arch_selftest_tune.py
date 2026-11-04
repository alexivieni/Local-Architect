"""Self-test for hardware detection, harness profiles, `tune`, and the model catalog. A scripted fake Ollama answers
/api/tags, /api/show, /api/ps, /api/chat (with real timing fields) and /api/pull, so everything runs with no model and no internet."""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GB = 1024 ** 3


class FakeOllama(BaseHTTPRequestHandler):
    cfg = {"canary_ok": True, "vram_frac": 1.0, "gen_s": 2.0, "pulled": []}

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        d = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [
                {"name": "fake-small:8b", "digest": "a", "size": 5 * GB, "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M", "family": "llama"}},
                {"name": "fake-big:30b", "digest": "b", "size": 19 * GB, "details": {"parameter_size": "30B", "quantization_level": "Q4_K_M", "family": "llama"}},
                {"name": "nomic-embed-text", "digest": "c", "size": GB // 4, "details": {"parameter_size": "137M", "family": "nomic-bert"}}]})
        if self.path == "/api/ps":
            return self._json({"models": [{"name": n, "size": s, "size_vram": int(s * self.cfg["vram_frac"])} for n, s in (("fake-small:8b", 5 * GB), ("fake-big:30b", 19 * GB))]})
        self._json({}, 404)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/show":
            return self._json({"model_info": {"general.architecture": "llama", "llama.block_count": 36, "llama.attention.head_count": 32,
                                              "llama.attention.head_count_kv": 8, "llama.embedding_length": 4096, "llama.context_length": 32768,
                                              "general.parameter_count": 8_000_000_000}, "capabilities": ["completion"]})
        if self.path == "/api/pull":
            self.cfg["pulled"].append(b.get("model"))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for done in (0, 50, 100):
                self.wfile.write((json.dumps({"status": "pulling abc", "total": 100, "completed": done}) + "\n").encode())
            self.wfile.write((json.dumps({"status": "success"}) + "\n").encode())
            return
        if self.path != "/api/chat":
            return self._json({}, 404)
        text = "\n".join(m["content"] for m in b["messages"])
        reply = "MANGO-7421" if (self.cfg["canary_ok"] and "MANGO-7421" in text) else "I do not know."
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": reply}, "done": False}) + "\n").encode())
        pt = max(len(text) // 3, 1)
        rate = 2000 * (self.cfg.get("batch_gain") or {}).get((b.get("options") or {}).get("num_batch", 512), 1.0)
        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": pt,
                                      "prompt_eval_duration": int(pt / rate * 1e9), "eval_count": 100, "eval_duration": int(self.cfg["gen_s"] * 1e9),
                                      "load_duration": 3 * 10 ** 9}) + "\n").encode())


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_autotune as at
    import arch_catalog as cat
    import arch_core as core
    import arch_hw
    import arch_params as params
    import arch_profiles as prof
    import arch_roles as roles
    import model_hub as mh

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_env = {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_HOME", "ARCHITECT_DATA_DIR")}
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    home = tempfile.mkdtemp(prefix="archhome_")
    old_home = core.HOME
    core.set_home(home)
    os.environ.pop("ARCHITECT_DATA_DIR", None)
    quiet = lambda m: None
    hw16 = {"gpu": "Test GPU", "vendor": "nvidia", "vram_gb": 16.0, "vram_free_gb": 16.0, "ram_gb": 32.0, "ram_free_gb": 24.0, "cpu": "cpu", "os": "linux",
            "notes": [], "gpus": [], "unified": False, "cpu_cores": 8, "disk_free_gb": 200.0, "models_dir": "/x", "fingerprint": "fp-16"}
    try:
        # ---- hardware parsing
        nv = arch_hw.detect(run=lambda cmd, timeout=8: (0, "NVIDIA RTX 4080, 16376, 15000, 551.1\n") if cmd[0] == "nvidia-smi" else (1, ""))
        check("NVIDIA GPUs are read from nvidia-smi output", nv["vendor"] == "nvidia" and abs(nv["vram_gb"] - 16.0) < 0.1 and nv["gpu"].startswith("NVIDIA RTX 4080"), nv)
        amd_json = json.dumps({"card0": {"Card Series": "Radeon RX 7900 XTX", "VRAM Total Memory (B)": str(24 * GB), "VRAM Total Used Memory (B)": str(2 * GB)}})
        amd = arch_hw.detect(run=lambda cmd, timeout=8: (0, amd_json) if cmd[0] == "rocm-smi" else (1, ""))
        check("AMD GPUs are read from rocm-smi output", amd["vendor"] == "amd" and abs(amd["vram_gb"] - 24.0) < 0.1, amd)
        none = arch_hw.detect(run=lambda cmd, timeout=8: (1, ""))
        check("no GPU is reported plainly, with a fingerprint and core count", none["vram_gb"] == 0 and none["fingerprint"] and none["cpu_cores"], none)
        other = dict(nv, vram_gb=24.0)
        check("the hardware fingerprint changes when the machine changes", arch_hw.fingerprint(nv) != arch_hw.fingerprint(other))
        fit0 = mh.fit_model(5 * GB, {"layers": 36, "kv_heads": 8, "head_dim": 128, "ctx_max": 32768}, {"vram_gb": 0, "ram_gb": 16})
        check("a machine with no GPU is not planned as if the model ran on one", all(r["gpu_ratio"] == 0 for r in fit0["rows"]), fit0["rows"][:1])

        # ---- profiles
        check("every harness-tier setting is a real, valid setting", all(params.is_known_setting(k) for t in prof.SETTINGS.values() for k in t)
              and all(params.validate_settings_patch(t) for t in prof.SETTINGS.values()))
        check("model size maps to tiers (4B tiny, 8B small, 24B mid, 32B large)",
              [prof.tier_for(n)[0] for n in ("llama3.2:3b", "qwen3:8b", "devstral:24b", "qwen3:32b")] == ["tiny", "small", "mid", "large"])
        check("a mixture-of-experts model is sized between its active and total parameters", prof.tier_for("qwen3-coder:30b", 30.5, 3.3)[0] == "mid")
        t, why = prof.tier_for("qwen3:8b", scores={"toolcall": 30})
        check("a model that scores badly on tool calls is demoted one tier, with the reason", t == "tiny" and "toolcall" in why, (t, why))
        check("a slow model loses sampled retries and per-sprint judging", prof.settings_for("large", 2.0)["behavior.best_of_n"] == 1
              and prof.settings_for("large", 2.0)["behavior.evaluator"] == "final" and prof.settings_for("large", 40)["behavior.best_of_n"] == 2)
        plan = {"generator": "a", "planner": "b", "tester": "a", "reviewer": "b"}
        check("solo puts every role on the builder; duo keeps thinking roles on a second model; crew keeps the recommendation",
              set(prof.crew_assignment(plan, "tiny", ["a", "b"]).values()) == {"a"}
              and prof.crew_assignment(plan, "small", ["a", "b"])["planner"] == "b" and prof.crew_assignment(plan, "small", ["a", "b"])["tester"] == "a"
              and prof.crew_assignment(plan, "mid", ["a", "b"]) == plan)

        # ---- tune (real calls to the fake server)
        FakeOllama.cfg.update(canary_ok=True, vram_frac=1.0, gen_s=2.0)
        lines = []
        p = at.tune(probe=True, apply=True, say=lines.append, hw=hw16)
        small = p["models"].get("fake-small:8b") or next(iter(p["models"].values()))
        check("tune measures speed, prompt speed and load time with real calls", small["measured"]["gen_tok_s"] == 50.0 and small["measured"]["prefill_tok_s"] and small["measured"]["load_s"] == 3.0, small)
        check("tune checks that the start of the prompt survived (canary)", small["measured"]["canary_ok"] is True)
        cfg = mh.read_config()
        check("tune writes the context window per model and the auto-grow ceiling", cfg["model_overrides"][p["builder"]]["num_ctx"] == p["models"][p["builder"]]["ctx"]
              and cfg["limits"]["num_ctx_max"] >= cfg["limits"]["num_ctx"], cfg.get("limits"))
        check("tune applies the builder's harness tier and a timeout ceiling that fits the measured speeds",
              cfg["behavior"]["edit_style"] == prof.SETTINGS[p["tier"]]["behavior.edit_style"] and 300 <= cfg["limits"]["request_timeout"] <= 7200, cfg.get("behavior"))
        import arch_assess
        a_out = arch_assess.assess([], hw16, mh.read_config())
        check("after a measured tune, assess takes its numbers from the tune and sees no drift", a_out["tuned_from"] == "tune" and a_out["tuning_drift"] == [] and "limits.context_chars" not in a_out["tuned_settings"], a_out)
        check("with no team assigned, tune assigns the recommended one", bool([m for m in cfg.get("roles", {}).values() if m]) and any("team" in c for c in p["changes"]), p["changes"])
        check("the profile is saved with the hardware fingerprint", at.load_profile().get("fingerprint") == "fp-16")
        check("after a tune on this hardware, no re-tune is due", at.status(hw16)["needs_tune"] is False, at.status(hw16))
        check("a changed machine makes a re-tune due, with the reason", at.status(dict(hw16, fingerprint="fp-24"))["needs_tune"] and "hardware" in at.status(dict(hw16, fingerprint="fp-24"))["reason"])

        before = json.dumps(mh.read_config(), sort_keys=True)
        at.tune(probe=False, apply=False, say=quiet, hw=dict(hw16, fingerprint="x"))
        check("--no-apply changes nothing in the settings file", json.dumps(mh.read_config(), sort_keys=True) == before)
        cfg = mh.read_config()
        cfg["roles"]["generator"] = "fake-big:30b"
        mh.write_config(cfg)
        at.tune(probe=False, apply=True, say=quiet, hw=hw16)
        check("a role you assigned yourself is never reassigned by tune", mh.read_config()["roles"]["generator"] == "fake-big:30b")

        hw24 = dict(hw16, vram_gb=24.0, vram_free_gb=24.0, ram_gb=64.0)
        # ---- your own models are sized properly, never hidden, never replaced, and tune is reversible
        check("a mixture-of-experts model is sized from its own expert counts, a dense one is not",
              abs(prof.active_from_arch(30.5, {"experts": 128, "experts_used": 8}) - 4.2) < 0.2 and prof.active_from_arch(32.8, {}) is None
              and prof.active_from_arch(30.5, {"experts": 8, "experts_used": 8}) is None)
        moe_row = {"name": "huihui_ai/qwen3-coder-abliterated:30b", "embed": False, "size_gb": 18.6, "params": "30.5B", "quant": "Q4_K_M",
                   "arch": {"experts": 128, "experts_used": 8, "params": 30_500_000_000}, "rec": {"verdict": "split", "est_tok_s": 14.0, "num_ctx": 16384},
                   "know": {"kind": "coder"}, "agg": {}}
        dense_row = {"name": "huihui_ai/qwen3-abliterated:32b", "embed": False, "size_gb": 19.8, "params": "32.8B", "quant": "Q4_K_M", "arch": {},
                     "rec": {"verdict": "split", "est_tok_s": 9.0, "num_ctx": 8192}, "know": {}, "agg": {}}
        small_row = {"name": "huihui_ai/llama3.2-abliterated:3b", "embed": False, "size_gb": 2.0, "params": "3.2B", "quant": "Q4_K_M", "arch": {},
                     "rec": {"verdict": "fast", "est_tok_s": 60.0, "num_ctx": 16384}, "know": {}, "agg": {}}
        own = {o["name"]: o for o in at.owned_models([moe_row, dense_row, small_row], {"roles": {"generator": moe_row["name"]}}, None)}
        m = own[moe_row["name"]]
        check("your own MoE and abliterated models are described by what they are, not by what the catalog happens to know",
              m["tags"] == ["MoE", "uncensored", "coder"] and 13.5 < m["eff_b"] < 16 and m["tier"] == "mid" and m["roles"] == ["generator"]
              and own[dense_row["name"]]["tier"] == "large" and own[dense_row["name"]]["tags"] == ["uncensored"] and len(own) == 3, m)
        r_big = at.suggest(hw24, [moe_row, dense_row], {}, None)
        check("with a 32B you already own, nothing is suggested to 'upgrade' your builder or planner", not {s["slot"] for s in r_big["suggestions"]} & {"builder", "planner"}, r_big["suggestions"])
        check("with a small fast model you already own, no 'fast' model is suggested", at.suggest(hw24, [moe_row, dense_row, small_row], {}, None)["suggestions"] == [])
        r_moe = at.suggest(hw24, [moe_row], {}, None)
        check("with only a 30B-A3B, every builder/planner suggestion is at least 25% larger (dense-equivalent) and is added alongside",
              all(prof.effective_b(x["tag"], cat.lookup(x["tag"])["params_b"], cat.lookup(x["tag"]).get("active_b")) >= 1.25 * 14.0 for x in r_moe["suggestions"] if x["slot"] != "fast")
              and all(any("none are replaced" in w for w in x["why"]) for x in r_moe["suggestions"] if x["slot"] != "fast"), r_moe["suggestions"])
        check("with nothing installed the suggestions are unchanged (builder, planner and fast)", {x["slot"] for x in cat.recommend(hw24, [])["suggestions"]} == {"builder", "planner", "fast"})

        full_before = mh.read_config()
        mh.write_config({"default_model": "fake-big:30b"})
        p_d = at.tune(probe=False, apply=True, say=quiet, hw=hw16)
        c_d = mh.read_config()
        check("with only a default model chosen, tune keeps it as the builder and assigns no team over it",
              c_d.get("default_model") == "fake-big:30b" and not [x for x in (c_d.get("roles") or {}).values() if x] and p_d["builder"] == "fake-big:30b", c_d.get("roles"))
        mh.write_config({"roles": {"planner": "gone:1b"}})
        at.tune(probe=False, apply=True, say=quiet, hw=hw16)
        c_g = mh.read_config()
        check("a role that points at a model you no longer have is kept as it is; only EMPTY roles are filled", c_g["roles"]["planner"] == "gone:1b" and c_g["roles"].get("generator"), c_g["roles"])
        mh.write_config({"limits": {"max_revisions": 7}, "custom_note": "keep me"})
        snap = json.dumps(mh.read_config(), sort_keys=True)
        at.tune(probe=False, apply=True, say=quiet, hw=hw16)
        changed = json.dumps(mh.read_config(), sort_keys=True) != snap
        done = at.undo_last(say=quiet)
        check("undo puts the settings file back exactly as it was before the last tune (values, new keys and your own keys)", changed and done and json.dumps(mh.read_config(), sort_keys=True) == snap, (changed, done))
        try:
            at.undo_last(say=quiet)
            twice = False
        except at.TuneError:
            twice = True
        check("a second undo has nothing to undo", twice)
        mh.write_config(full_before)

        # ---- hardware sweep: batch size is changed only when it is clearly faster, and undo restores it
        FakeOllama.cfg.update(canary_ok=True, vram_frac=1.0, gen_s=2.0, batch_gain={1024: 1.6, 256: 0.8})
        before_sweep = json.dumps(mh.read_config(), sort_keys=True)
        sl = []
        ps = at.tune(probe=True, apply=True, deep=True, say=sl.append, hw=hw16)
        sw = ps["models"][ps["builder"]].get("sweep") or {}
        check("tune --deep measures batch sizes for the builder and finds the faster one", sw.get("num_batch") == 1024 and set(sw["results"]["batch"]) == {512, 256, 1024}, sw)
        check("...and writes it as that model's override, with the gain stated", mh.read_config()["model_overrides"][ps["builder"]].get("num_batch") == 1024
              and any("num_batch -> 1024" in c for c in ps["changes"]), ps["changes"])
        check("a GPU-resident model is not threads-swept (the CPU is not involved)", sw.get("num_thread") is None and "threads" not in sw["results"], sw)
        at.undo_last(say=quiet)
        check("undo removes the batch setting again", "num_batch" not in (mh.read_config().get("model_overrides", {}).get(ps["builder"]) or {}))
        FakeOllama.cfg.update(batch_gain={1024: 1.05, 256: 0.9})
        ps2 = at.tune(probe=True, apply=True, deep=True, say=quiet, hw=hw16)
        check("a gain under 10% is not worth changing: the default stays", ps2["models"][ps2["builder"]]["sweep"]["num_batch"] is None
              and "num_batch" not in (mh.read_config()["model_overrides"][ps2["builder"]]))
        at.undo_last(say=quiet)
        calls = []

        def timed(extra, chars=9000):
            calls.append(dict(extra))
            return (1000.0, 40.0 * (1.5 if extra.get("num_thread") == 4 else 1.0))
        cpu_sw = at.sweep("m", 4096, {"vram_gb": 0, "cpu_cores": 8}, None, say=quiet, timed=timed)
        check("with no GPU the sweep also tries CPU thread counts and adopts a clearly faster one", cpu_sw["num_thread"] == 4 and cpu_sw["num_batch"] is None, cpu_sw)
        check("a model that does not report prompt speed is skipped, not guessed", at.sweep("m", 4096, {"vram_gb": 8, "cpu_cores": 8}, 100, say=quiet, timed=lambda e, c=9000: (0, 0))["num_batch"] is None)
        check("the new settings are real, validated override fields (clamped to a sane range)", params.validate_override("num_batch", 99999) == 8192 and params.validate_override("num_thread", 0) == 1)
        FakeOllama.cfg.pop("batch_gain", None)
        mh.write_config(full_before)

        FakeOllama.cfg.update(canary_ok=False, vram_frac=0.6, gen_s=100.0)
        p2 = at.tune(models=["fake-small:8b"], probe=True, apply=False, say=quiet, hw=hw16)
        w = " | ".join(p2["warnings"])
        check("a model that forgets the start of its prompt is flagged", "did not repeat" in w, w)
        check("a model spilling onto the CPU is flagged with the share on the GPU", "60% on the GPU" in w, w)
        check("a very slow model is flagged and loses sampled retries", "generates only 1.0 tok/s" in w and "switched off" in w, w)
        FakeOllama.cfg.update(canary_ok=True, vram_frac=1.0, gen_s=2.0)
        p3 = at.tune(models=["fake-small:8b"], probe=False, apply=False, say=quiet, hw=dict(hw16, vram_gb=0.0, vram_free_gb=0.0, fingerprint="cpu"))
        check("a CPU-only machine is told plainly that the model runs from RAM", p3["models"]["fake-small:8b"]["fit_verdict"] == "slow" and any("RAM" in x for x in p3["warnings"]), (p3["models"]["fake-small:8b"]["fit_verdict"], p3["warnings"]))

        # ---- catalog
        c = cat.load()
        check("the catalog is dated and every entry has what the fit math needs",
              c["as_of"] and all(m["arch"]["layers"] and m["arch"]["kv_heads"] and m["arch"]["head_dim"] and m["size_gb"] > 0 and m["slots"] for m in c["models"]))
        check("an installed tag is found in the catalog", cat.lookup("qwen3:8b")["params_b"] == 8.2 and cat.lookup("nope:1b") is None)
        hw24 = dict(hw16, vram_gb=24.0, vram_free_gb=24.0, ram_gb=64.0)
        rec = cat.recommend(hw24, ["qwen3:8b"])
        tags = [s["tag"] for s in rec["suggestions"]]
        check("recommendations never include a model you already have", "qwen3:8b" not in tags and tags, tags)
        check("every suggestion fits this machine and says how to get it", all(s["verdict"] != "too_big" and s["pull"].startswith("ollama pull ") and s["size_gb"] for s in rec["suggestions"]))
        small_hw = dict(hw16, vram_gb=0.0, vram_free_gb=0.0, ram_gb=8.0)
        check("a machine too small for a model is not offered it", not any(s["size_gb"] > 8 for s in cat.recommend(small_hw, [])["suggestions"]))
        tight = cat.recommend(dict(hw24, disk_free_gb=3.0), [])["suggestions"]
        check("disk space is respected: nothing bigger than the free space is offered", all(s["size_gb"] * 1.1 <= 3.0 for s in tight), tight)
        unv = [s for s in cat.recommend(hw24, [])["suggestions"] if not s["verified"]]
        check("an unverified catalog entry is flagged and points at --verify", bool(unv) and all(any("catalog --verify" in x for x in s["caveats"]) for s in unv), unv)
        try:
            cat.pull("qwen3:8b")
            check("a download without an explicit yes is refused", False)
        except PermissionError:
            check("a download without an explicit yes is refused", True)
        FakeOllama.cfg["pulled"].clear()
        ok = cat.pull("qwen3:4b", confirm=True, say=lines.append)
        check("a confirmed download streams progress through Ollama", ok and FakeOllama.cfg["pulled"] == ["qwen3:4b"] and any("50%" in str(l) for l in lines), lines[-4:])

        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body
        good = cat.verify_tag("qwen3:8b", opener=lambda req, timeout: Resp(json.dumps({"layers": [{"size": 4 * GB}, {"size": GB // 2}], "config": {"size": 500}}).encode()))
        check("verify reads the real download size from a registry manifest", good["ok"] and 4.4 < good["size_gb"] < 4.6, good)

        def nf(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        check("verify reports a tag the registry does not have", cat.verify_tag("nope:1b", opener=nf) == {"ok": False, "error": "not found in the registry"})
        ov = cat.verify_all(tags=["devstral-small-2:24b"], opener=nf, say=quiet)
        check("a failed verification is remembered (outside the shipped file) and lowers the entry", cat.lookup("devstral-small-2:24b")["verified"] is False and "devstral-small-2:24b" in ov)
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' TUNE/CATALOG CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' TUNE/CATALOG CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
