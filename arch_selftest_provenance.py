"""Self-test for provenance: which weights answered a run (quantization, weight edit, base model, license), recorded once per model per
run in the trace and in run.json, shown on the receipt, with a plain note for a clearly non-commercial license.

A scripted stand-in model answers on loopback (arch_selftest.Fake); passports, GGUF headers and an Ollama manifest are written into a
throwaway data folder. No real model, no GPU, no network.

    python arch_selftest_provenance.py
"""
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _gguf(path, kv):
    """A tiny GGUF v3 file with only a header: kv = [(key, type, value)]. Types: 4 uint32, 10 uint64, 8 string, 9 string array,
    99 = a value type no reader knows (a full read fails there)."""
    def s(x):
        b = x.encode("utf-8")
        return struct.pack("<Q", len(b)) + b
    out = [b"GGUF", struct.pack("<I", 3), struct.pack("<QQ", 0, len(kv))]
    for k, t, v in kv:
        out += [s(k), struct.pack("<I", t)]
        if t == 8:
            out.append(s(v))
        elif t == 4:
            out.append(struct.pack("<I", v))
        elif t == 10:
            out.append(struct.pack("<Q", v))
        elif t == 9:
            out.append(struct.pack("<IQ", 8, len(v)))
            out += [s(x) for x in v]
    with open(path, "wb") as f:
        f.write(b"".join(out))
    return path


ABLIT_KV = [("general.architecture", 8, "qwen2"), ("general.name", 8, "Qwen2.5 7B Instruct Abliterated"),
            ("general.finetune", 8, "Instruct-abliterated"), ("general.license", 8, "apache-2.0"),
            ("general.base_model.count", 4, 1), ("general.base_model.0.name", 8, "Qwen2.5 7B Instruct"),
            ("general.base_model.0.organization", 8, "Qwen"), ("general.tags", 9, ["abliterated", "text-generation"]),
            ("general.parameter_count", 10, 7615616512), ("general.file_type", 4, 15), ("qwen2.block_count", 4, 28),
            ("qwen2.context_length", 4, 32768), ("tokenizer.ggml.model", 8, "gpt2"), ("tokenizer.ggml.bogus", 99, None)]
# The layout llama-quantize writes (14 of the 18 Ollama blobs on the owner's machine): general.file_type and quantization_version come
# AFTER the vocabulary. 120k tokens (~2 MB, so strings straddle the reader's 1 MB blocks) and a 3 MB merge past the first eight.
QUANTIZED_KV = [("general.architecture", 8, "qwen3"), ("general.name", 8, "Qwen3 8B"), ("general.license", 8, "apache-2.0"),
                ("general.base_model.count", 4, 1), ("general.base_model.0.name", 8, "Qwen3 8B Base"),
                ("general.base_model.0.organization", 8, "Qwen"), ("qwen3.block_count", 4, 36), ("tokenizer.ggml.model", 8, "gpt2"),
                ("tokenizer.ggml.tokens", 9, [f"tok{i:06d}" for i in range(120000)]),
                ("tokenizer.ggml.merges", 9, [f"m {i}" for i in range(8)] + ["x" * 3_000_000, "c d"]),
                ("general.quantization_version", 4, 2), ("general.file_type", 4, 30)]
F16_KV = [("general.architecture", 8, "qwen3"), ("general.name", 8, "Qwen3 8B"), ("tokenizer.ggml.model", 8, "gpt2"),
          ("tokenizer.ggml.tokens", 9, ["a", "b", "c"]), ("general.file_type", 4, 1)]     # no size stated anywhere in the file
NC_KV = [("general.architecture", 8, "llama"), ("general.name", 8, "Research Llama"), ("general.license", 8, "cc-by-nc-4.0"),
         ("general.base_model.count", 4, 1), ("general.base_model.0.name", 8, "Llama 3.1 8B"),
         ("general.base_model.0.organization", 8, "Meta Llama"), ("general.file_type", 4, 7), ("llama.block_count", 4, 32),
         ("tokenizer.ggml.model", 8, "llama")]


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if not ok and extra else ""))

    print("\n== provenance: which weights answered, per run ==")
    import arch_core as core
    import arch_passport as P
    import arch_receipt as R
    import arch_roles
    import arch_selftest

    home = tempfile.mkdtemp(prefix="archprov_")
    old_home = core.HOME
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST", "OLLAMA_MODELS", "ARCHITECT_HOME", "ARCHITECT_EVAL")
    old = {k: os.environ.get(k) for k in keys}
    core.set_home(home)                         # never the install's own settings (its tiers point at a live server)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ["OLLAMA_MODELS"] = os.path.join(home, "ollama")     # never this machine's real Ollama folder
    orig_prov, orig_load = core.model_provenance, P.load

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            return "ok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    try:
        # ---- the rules on their own
        check("weight edit: an abliterated / orthogonalized / Heretic name is 'orthogonalized' (any case)",
              core.weight_edit_of("huihui_ai/qwen3-abliterated:8b") == "orthogonalized"
              and core.weight_edit_of("m:7b", "Llama ORTHOGONALIZED") == "orthogonalized"
              and core.weight_edit_of("hf.co/p-e-w/gpt-oss-20b-heretic-GGUF:Q4_K_M", "gpt-oss 20b heretic",
                                      base_model="openai/gpt-oss-20b") == "orthogonalized")
        check("weight edit: 'uncensored' alone does not claim a method (llama2-uncensored is a fine-tune on filtered data)",
              core.weight_edit_of("llama2-uncensored:7b") == "uncensored" and core.weight_edit_of("wizard-vicuna-uncensored:13b") == "uncensored"
              and core.weight_edit_of("x/qwen3.6-moe-uncensored-abliterated:iq3") == "orthogonalized")
        check("weight edit: a fine-tune of an abliterated base inherits the edit (the base model's name is read too)",
              core.weight_edit_of("ft:8b", base_model="huihui-ai/Qwen3-8B-abliterated") == "orthogonalized")
        check("weight edit: 'none' when the file names a base model and no marker; 'unknown' when it names none",
              core.weight_edit_of("qwen3:8b", base_model="Qwen/Qwen3 8B") == "none" and core.weight_edit_of("qwen3:8b") == "unknown")
        check("weight edit: the marker wins over a named base model (an abliterated build names its base too)",
              core.weight_edit_of("q:7b", "Instruct-abliterated", base_model="Qwen/Qwen2.5 7B") == "orthogonalized")
        flagged = ["cc-by-nc-4.0", "CC-BY-NC-SA-4.0", "Attribution-NonCommercial 4.0 International", "non-commercial",
                   "research only", "For research purposes only", "mnpl-0.1", "Mistral AI Non-Production License", "qwen-research",
                   "tongyi-qianwen-research", "Tongyi Qianwen RESEARCH LICENSE AGREEMENT", "Mistral AI Research License", "MRL-0.1"]
        fine = ["llama2", "llama3.1", "LLAMA 3.2 COMMUNITY LICENSE AGREEMENT", "gemma", "apache-2.0", "mit", "other", "cc-by-4.0",
                "openrail", "Tongyi Qianwen LICENSE AGREEMENT", "tongyi-qianwen", "CC-BY-SA-4.0", "", None]
        check("license check: clear non-commercial / research-only terms are flagged",
              all(core.license_terms(x) == "non_commercial" for x in flagged), [x for x in flagged if not core.license_terms(x)])
        check("license check: community, permissive and unnamed licenses are not (conservative)",
              not any(core.license_terms(x) for x in fine), [x for x in fine if core.license_terms(x)])
        qn = {t: core.model_provenance(t)["quant"] for t in ("qwen2.5-coder:7b-instruct-q4_K_M", "llama3.2:3b-instruct-fp16",
              "hf.co/mradermacher/Qwen3-Coder-30B-A3B-Instruct-GGUF:IQ4_XS", "x/qwen3.6-moe:iq3", "qwen3:8b")}
        check("with nothing else known, the quantization is read from the tag (and none is invented when the tag has none)",
              qn == {"qwen2.5-coder:7b-instruct-q4_K_M": "Q4_K_M", "llama3.2:3b-instruct-fp16": "FP16",
                     "hf.co/mradermacher/Qwen3-Coder-30B-A3B-Instruct-GGUF:IQ4_XS": "IQ4_XS", "x/qwen3.6-moe:iq3": "IQ3", "qwen3:8b": None}, qn)

        # ---- reading the model file
        files = os.path.join(home, "files")
        os.makedirs(files)
        ablit = _gguf(os.path.join(files, "ablit.gguf"), ABLIT_KV)
        nc = _gguf(os.path.join(files, "nc.gguf"), NC_KV)
        full = P.gguf_meta(ablit)
        hf = P.header_facts(ablit)
        check("the header read stops at the tokenizer: a file whose vocabulary a full read cannot parse still gives its facts",
              "error" in full and hf.get("quant") == "Q4_K_M" and hf.get("family") == "qwen2" and hf.get("params_b") == 7.62, (full, hf))
        lin = hf.get("lineage") or {}
        check("lineage from general.*: license, base model with its organisation, fine-tune and tags",
              lin.get("license") == "apache-2.0" and lin.get("base_model") == "Qwen/Qwen2.5 7B Instruct"
              and lin.get("finetune") == "Instruct-abliterated" and lin.get("tags") == ["abliterated", "text-generation"], lin)
        check("Hugging Face's 'other' license gives way to the name stored beside it",
              P.lineage({"general.license": "other", "general.license.name": "qwen-research"})["license"] == "qwen-research")
        check("an unreadable file gives no facts rather than an error", P.header_facts(os.path.join(files, "nope.gguf")) == {})
        qz = _gguf(os.path.join(files, "quantized.gguf"), QUANTIZED_KV)
        fq = P.gguf_meta(qz)
        check("the full header read walks a 2 MB vocabulary and a 3 MB string without decoding them, and lands exactly on the next key",
              fq.get("general.file_type") == 30 and fq.get("general.quantization_version") == 2
              and fq["tokenizer.ggml.tokens"] == {"array": "string", "n": 120000, "head": [f"tok{i:06d}" for i in range(8)]}
              and fq["tokenizer.ggml.merges"]["n"] == 10, {k: fq.get(k) for k in ("general.file_type", "error")})
        P._HEADER_MEMO.clear()
        hq = P.header_facts(qz)
        check("llama-quantize's layout (file type AFTER the vocabulary): the quantization is still read from the file",
              hq.get("quant") == "IQ4_XS" and (hq.get("lineage") or {}).get("license") == "apache-2.0", hq.get("quant"))
        check("...and only when it is wanted: a caller that already knows the quantization pays for the short read only",
              P.header_facts(qz, need_quant=False).get("quant") is None and (P.header_facts(qz, need_quant=False).get("lineage")
                                                                              or {}).get("base_model") == "Qwen/Qwen3 8B Base")
        check("a header is read once per process: the second ask is answered from memory", P.header_facts(qz) is hq)
        win_dir = os.path.join(files, "llama-3b-q4_0-experiments")
        os.makedirs(win_dir)
        f16 = _gguf(os.path.join(win_dir, "Qwen3-8B-F16.gguf"), F16_KV).replace("/", "\\")
        wf = core.model_provenance(f16)
        check("a Windows path (backslashes): the quantization comes from the file, never from a folder's name, and no size is guessed",
              wf["source"] == "gguf" and wf["quant"] == "F16" and wf["params_b"] is None, wf)
        wn = core.model_provenance("C:\\models\\q8_0-old\\llama-3b\\Mistral-7B-Instruct-Q4_K_M.gguf")
        check("a .gguf path that is not there: the file name alone is read (Q4_K_M, 7B), not 'q8_0' or '3b' from its folders",
              wn["source"] == "name" and wn["quant"] == "Q4_K_M" and wn["params_b"] == 7.0, wn)
        pp = P.build("fake:7b", gguf=nc)
        check("a passport built from a GGUF keeps its lineage (license, base model)",
              (pp.get("lineage") or {}).get("license") == "cc-by-nc-4.0" and pp["lineage"]["base_model"] == "Meta Llama/Llama 3.1 8B"
              and pp.get("quant") == "Q8_0", pp.get("lineage"))
        ps = P.build("show:1b", show={"details": {"quantization_level": "Q4_K_M", "family": "llama"}, "model_info": {
            "general.architecture": "llama", "general.base_model.count": 1, "general.base_model.0.name": "Some 1B"},
            "license": "\nAttribution-NonCommercial 4.0 International\n\nlong text..."})
        check("a passport from Ollama's /api/show reads model_info, and names the license from the first line of its text",
              ps["lineage"]["license"] == "Attribution-NonCommercial 4.0 International" and ps["lineage"]["base_model"] == "Some 1B",
              ps.get("lineage"))
        P.save(pp)                                                            # fake:7b: passport with a non-commercial license
        os.makedirs(P.passports_dir(), exist_ok=True)
        with open(P.path_for("plain:3b-q4_K_M"), "w", encoding="utf-8") as f:   # a broken passport
            f.write("{not json")
        blob_hex = hashlib.sha256(open(ablit, "rb").read()).hexdigest()       # an Ollama model with no passport: manifest + blob
        om = os.environ["OLLAMA_MODELS"]
        os.makedirs(os.path.join(om, "blobs"))
        shutil.copyfile(ablit, os.path.join(om, "blobs", "sha256-" + blob_hex))
        man = os.path.join(om, "manifests", "registry.ollama.ai", "library", "blobmodel")
        os.makedirs(man)
        with open(os.path.join(man, "7b"), "w", encoding="utf-8") as f:
            json.dump({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + blob_hex,
                                   "size": os.path.getsize(ablit)}]}, f)

        check("a cloud model is recorded as such: no local weights, no weight edit",
              core.model_provenance("claude:claude-opus-5-5") == {"model": "claude:claude-opus-5-5", "quant": None, "family": "claude",
              "params_b": None, "moe": None, "active_b": None, "weight_edit": "none", "base_model": None, "license": None,
              "source": "claude", "digest": None})
        bm = core.model_provenance("blobmodel:7b")
        check("no passport: the blob Ollama already has is read (header facts, the manifest's sha256 as the digest)",
              bm["source"] == "gguf" and bm["digest"] == "sha256:" + blob_hex and bm["quant"] == "Q4_K_M"
              and bm["weight_edit"] == "orthogonalized" and bm["license"] == "apache-2.0", bm)
        qhex = hashlib.sha256(open(qz, "rb").read()).hexdigest()               # a quantized Ollama blob, file type after the vocabulary
        shutil.copyfile(qz, os.path.join(om, "blobs", "sha256-" + qhex))
        qman = os.path.join(om, "manifests", "registry.ollama.ai", "library", "qwenq")
        os.makedirs(qman)
        with open(os.path.join(qman, "8b"), "w", encoding="utf-8") as f:
            json.dump({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + qhex}]}, f)
        qm = core.model_provenance("qwenq:8b")
        check("no passport, a blob in llama-quantize's layout: the record says IQ4_XS read from the file (not a guess from the tag)",
              qm["source"] == "gguf" and qm["quant"] == "IQ4_XS" and qm["weight_edit"] == "none"
              and qm["base_model"] == "Qwen/Qwen3 8B Base", qm)
        old_pp = {"name": "oldpass:8b", "source": "gguf", "path": qz, "quant": "Q4_K_M", "family": "qwen3", "arch": {"params_b": 8.2}}
        P.save(old_pp)                                                       # a passport written before passports kept lineage
        P._HEADER_MEMO.clear()
        op = core.model_provenance("oldpass:8b")
        check("an older passport keeps its own quantization, gets its lineage from the short header read only",
              op["source"] == "passport" and op["quant"] == "Q4_K_M" and op["license"] == "apache-2.0"
              and bool(P._HEADER_MEMO) and all(k[3] is False for k in P._HEADER_MEMO), (op, list(P._HEADER_MEMO)))
        t0 = time.perf_counter()
        for _ in range(20):
            core.model_provenance("fake:7b")
        per = (time.perf_counter() - t0) / 20
        check("one lookup costs milliseconds, not seconds (and a run does it once per model)", per < 0.05, f"{per * 1000:.1f} ms")

        # ---- a run: several models, several calls
        proj = os.path.join(home, "proj")
        os.makedirs(proj)
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                       "behavior": {"battery_saver": "off"},
                                                       "server": {"make_room": "off"}})     # never move a server for a test model
        e = core.Engine(proj, "review", "provenance test", cfg, review="auto", say=lambda *_a, **_k: None)
        looked = []

        def counting(model):
            looked.append(model)
            if model == "crash:2b":
                raise RuntimeError("provenance lookup blew up")
            return orig_prov(model)

        def bad_load(name):
            if name == "boom:1b":
                raise OSError("passport folder unreadable")
            return orig_load(name)
        core.model_provenance, P.load = counting, bad_load
        plan = ["fake:7b", "fake:7b", "huihui_ai/fake-abliterated:8b", "plain:3b-q4_K_M", "fake:7b", "blobmodel:7b", "boom:1b", "crash:2b"]
        replies = []
        for i, m in enumerate(plan):
            e.cfg["roles"]["reviewer"] = m
            try:
                replies.append(e.call("reviewer", f"question {i}: is this fine?")["text"])
            except Exception as ex:                       # a call must never fail because of provenance
                replies.append(f"raised {type(ex).__name__}: {ex}")
        core.model_provenance, P.load = orig_prov, orig_load
        check("every call answered, including the ones whose passport was broken, unreadable, missing, or whose lookup raised",
              replies == ["ok"] * len(plan), replies)
        recs = [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8")]
        prov = [r for r in recs if r.get("type") == "provenance"]
        calls = [r for r in recs if r.get("type") == "call"]
        by = {r["model"]: r for r in prov}
        check("one provenance record per model per run, not per call (8 calls, 6 models, one lookup failed outright)",
              len(calls) == 8 and len(prov) == 5 and set(by) == set(plan) - {"crash:2b"}, [r["model"] for r in prov])
        check("the lookup itself runs once per model (cached on the engine), however many calls it serves",
              looked.count("fake:7b") == 1 and len(looked) == 6, looked)
        first = next(i for i, r in enumerate(recs) if r.get("type") == "call" and r.get("model") == "fake:7b")
        check("the record follows the model's first answer in the trace",
              recs[first + 1].get("type") == "provenance" and recs[first + 1].get("model") == "fake:7b", recs[first + 1].get("type"))
        fields = {"type", "t", "model", "quant", "family", "params_b", "moe", "active_b", "weight_edit", "base_model", "license", "source", "digest"}
        check("each record carries exactly the provenance fields", all(set(r) == fields for r in prov), [sorted(set(r) ^ fields) for r in prov])
        f7 = by.get("fake:7b") or {}
        check("from a passport: quantization, family, base model, license, weight edit 'none', and a file stamp as the digest",
              f7.get("source") == "passport" and f7.get("quant") == "Q8_0" and f7.get("family") == "llama"
              and f7.get("base_model") == "Meta Llama/Llama 3.1 8B" and f7.get("license") == "cc-by-nc-4.0"
              and f7.get("weight_edit") == "none" and str(f7.get("digest") or "").startswith("stat:"), f7)
        ab = by.get("huihui_ai/fake-abliterated:8b") or {}
        check("no passport and no file: the name alone says 'orthogonalized'; nothing else is invented",
              ab.get("source") == "name" and ab.get("weight_edit") == "orthogonalized" and ab.get("license") is None
              and ab.get("base_model") is None and ab.get("params_b") == 8.0 and ab.get("digest") is None, ab)
        pl = by.get("plain:3b-q4_K_M") or {}
        check("a broken passport file is ignored: 'unknown' weight edit, quantization read from the tag",
              pl.get("source") == "name" and pl.get("weight_edit") == "unknown" and pl.get("quant") == "Q4_K_M", pl)
        check("a passport read that raises still leaves a name-only record", (by.get("boom:1b") or {}).get("source") == "name", by.get("boom:1b"))
        check("the Ollama blob's record names the file's own facts and its sha256",
              (by.get("blobmodel:7b") or {}).get("digest") == "sha256:" + blob_hex
              and by["blobmodel:7b"]["base_model"] == "Qwen/Qwen2.5 7B Instruct", by.get("blobmodel:7b"))
        e.finish(True)
        meta = core.load_json(os.path.join(e.run_dir, "run.json"))
        mp = meta.get("provenance") or {}
        check("run.json carries provenance for every model the run used, the same facts as the trace",
              set(mp) == set(by) and all(mp[m] == {k: v for k, v in by[m].items() if k not in ("type", "t")} for m in by),
              sorted(mp))

        # ---- the receipt
        page = R.build(e.run_dir, proj)
        sec = page[page.find("Models and provenance"):]
        check("the receipt has a 'Models and provenance' section listing each model", "Models and provenance" in page
              and all(m in sec for m in ("fake:7b", "huihui_ai/fake-abliterated:8b", "plain:3b-q4_K_M", "blobmodel:7b")))
        check("...with quantization, weight edit, base model and license",
              all(x in sec for x in ("Q8_0", "Q4_K_M", "Meta Llama/Llama 3.1 8B", "cc-by-nc-4.0", "apache-2.0",
                                     "orthogonalized (refusals edited out of the weights)", "unknown (the file names no base model)")))
        check("'none' is shown as what it is: none stated, not a certainty", "none stated (a base model is named" in sec)
        check("a license the file does not state reads 'not recorded in the model file'", "not recorded in the model file" in sec)
        check("the non-commercial license is said in plain words, once, and as a note rather than a stop",
              sec.count("class='lic'") == 1 and "allows non-commercial" in sec and "the run was not stopped for it" in sec)
        check("the receipt stays self-contained: no scripts, nothing from the internet",
              "<script" not in page and "http://" not in page and "https://" not in page)

        # receipts for other runs: older runs, run.json only, a cloud model, a permissive license
        rd = os.path.join(home, "oldrun")
        os.makedirs(rd)
        with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"goal": "an older run", "flow": "review", "status": "done", "started": 1, "updated": 2}, f)
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "call", "id": 1, "model": "x:7b", "secs": 1}) + "\n")
        check("a run recorded before provenance existed still makes a receipt, without the section",
              "Models and provenance" not in R.build(rd) and "an older run" in R.build(rd))
        with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"goal": "only run.json", "flow": "review", "status": "done", "provenance": {
                "q:7b": {"model": "q:7b", "quant": "Q5_K_M", "weight_edit": "none", "base_model": "Q 7B", "license": "apache-2.0", "source": "passport"},
                "llama2-uncensored:7b": core.model_provenance("llama2-uncensored:7b"),
                "claude:claude-opus-5-5": core.model_provenance("claude:claude-opus-5-5")}}, f)
        p2 = R.build(rd)
        check("the receipt falls back to run.json, shows a cloud model as having no model file, and flags nothing permissive",
              "Q5_K_M" in p2 and "a cloud service: no model file" in p2 and "class='lic'" not in p2, p2[p2.find("Models and"):][:600])
        check("an 'uncensored' model reads 'uncensored (method not stated)', not orthogonalized",
              "uncensored (method not stated)" in p2 and "orthogonalized (refusals" not in p2)
        with open(os.path.join(rd, "trace.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "provenance", "model": "odd:1b", "params_b": "lots", "moe": True, "active_b": [1]}) + "\n")
        p3 = R.build(rd)
        check("an odd record (sizes that are not numbers) is still listed, and never costs the person the receipt",
              "odd:1b" in p3 and "only run.json" in p3, p3[p3.find("Models and"):][:400])

        # ---- a remembered reply (the reply cache) is still a model's output: it is recorded once too
        os.environ.pop("ARCHITECT_NO_CACHE", None)
        try:
            e1 = core.Engine(proj, "review", "cache one", cfg, review="auto", say=lambda *_a, **_k: None)
            e1.cfg["roles"]["reviewer"] = "fake:7b"
            e1.call("reviewer", "the same question for both runs", temperature=0)
            e2 = core.Engine(proj, "review", "cache two", cfg, review="auto", say=lambda *_a, **_k: None)
            e2.cfg["roles"]["reviewer"] = "fake:7b"
            r2 = e2.call("reviewer", "the same question for both runs", temperature=0)
            r3 = e2.call("reviewer", "the same question for both runs", temperature=0)
            t2 = [json.loads(x) for x in open(os.path.join(e2.run_dir, "trace.jsonl"), encoding="utf-8")]
            check("a reply served from the cache records the model's provenance, once per run",
                  r2.get("cached") and r3.get("cached") and [r["model"] for r in t2 if r.get("type") == "provenance"] == ["fake:7b"],
                  [(r.get("type"), r.get("transport")) for r in t2])
        finally:
            os.environ["ARCHITECT_NO_CACHE"] = "1"

        # ---- the worker's saved prompt memory (180-380 MB a file) is not written for a folder nothing will ever reopen
        import arch_server
        os.environ.pop("ARCHITECT_EVAL", None)
        check("prompt-memory rule: a real project keeps it; a folder under the temp directory does not",
              core.keeps_prompt_memory(HERE) and not core.keeps_prompt_memory(os.path.join(tempfile.gettempdir(), "eval_build_x1")))
        _gt = tempfile.gettempdir
        tempfile.gettempdir = lambda: os.path.join(home, "some-other-temp")        # this suite's data folder is itself under temp
        try:
            live_rule = (not core.keeps_prompt_memory(os.path.join(core.data_dir(), "live_fix_ab12"))
                         and core.keeps_prompt_memory(os.path.join(core.data_dir(), "myproject")))
        finally:
            tempfile.gettempdir = _gt
        check("prompt-memory rule: a live test's live_* folder in the data folder does not (any other folder there does)", live_rule)
        os.environ["ARCHITECT_EVAL"] = "1"
        check("prompt-memory rule: nothing is kept during an eval run, whatever the folder", not core.keeps_prompt_memory(HERE))
        os.environ.pop("ARCHITECT_EVAL", None)
        slots, orig_slot, orig_tmp = [], arch_server.slot_io, tempfile.gettempdir

        def one_run(goal):
            en = core.Engine(proj, "review", goal, cfg, review="auto", say=lambda *_a, **_k: None)
            en.cfg["roles"]["reviewer"] = "fake:7b"
            en.call("reviewer", f"slot question for {goal}")
            en.finish(True)
            return [s for s in slots if s[0] == "save"]
        arch_server.slot_io = lambda action, cfg_, project, tier="worker", **kw: slots.append((action, project)) or True
        try:
            os.environ["ARCHITECT_EVAL"] = "1"
            in_eval = one_run("an eval task")
            os.environ.pop("ARCHITECT_EVAL", None)
            in_temp = one_run("a project in the temp folder")
            tempfile.gettempdir = lambda: os.path.join(home, "some-other-temp")     # now proj is an ordinary folder
            in_real = one_run("a real project")
        finally:
            arch_server.slot_io, tempfile.gettempdir = orig_slot, orig_tmp
            os.environ.pop("ARCHITECT_EVAL", None)
        check("finish(): no prompt-memory save for an eval run or a temp folder; an ordinary project still saves its own",
              in_eval == [] and in_temp == [] and len(in_real) == 1 and in_real[0][0] == "save" and os.path.basename(in_real[0][1]) == "proj" and os.path.isabs(in_real[0][1]), (in_eval, in_temp, in_real))
    finally:
        core.model_provenance, P.load = orig_prov, orig_load
        srv.shutdown()
        core.set_home(old_home)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' PROVENANCE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' PROVENANCE CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
