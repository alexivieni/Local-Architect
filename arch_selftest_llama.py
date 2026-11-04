"""Self-test for the resident-server layer: the llama transport (SSE, schema, exact cache accounting, timeouts), the server plan (profiles,
Ollama blob adoption, flag caps, fit math), passports (GGUF header reader on a synthetic file), and tier resolution in the engine.
No GPU, no llama-server binary, no network: a scripted OpenAI-style fake and a stub binary stand in."""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class FakeLlama(BaseHTTPRequestHandler):
    """Answers like llama-server's OpenAI endpoint. state: reject_schema -> 400 on response_format once; slow -> sleep before the first token."""
    state = {"reject_schema": False, "slow": 0, "calls": []}

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = {"/health": {"status": "ok"}, "/v1/models": {"data": [{"id": "worker"}]}, "/slots": [{"id": 0, "is_processing": False}]}.get(self.path, {})
        d = json.dumps(body).encode()
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.state["calls"].append(b)
        if self.state.get("reject_schema") and "response_format" in b:
            self.state["reject_schema"] = False
            d = json.dumps({"error": {"message": "json_schema is not supported by this build"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(d)))
            self.end_headers()
            self.wfile.write(d)
            return
        if self.state.get("slow"):
            import time
            time.sleep(self.state["slow"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        text = '{"flow": "build", "why": "fake"}' if "response_format" in b else "FILE: a.py\n```python\nx = 1\n```\n"
        if self.state.get("window_full"):                    # the window fills up mid-reply: llama-server ends the stream with an error
            self.state["window_full"] = False
            text = "FILE: a.py\n```python\nx = 1\n```\n\nFILE: b.py\n```python\ny = "
        for i in range(0, len(text), 9):
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": text[i:i + 9]}}]}) + "\n\n").encode())
        if text.endswith("y = "):
            self.wfile.write(("data: " + json.dumps({"error": {"code": 500, "message": "Context size has been exceeded.", "type": "server_error"}}) + "\n\n").encode())
            return
        last = {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 300, "completion_tokens": 12},
                "timings": {"prompt_n": 50, "cache_n": 250, "predicted_per_second": 33.3, "prompt_per_second": 800.0, "predicted_n": 12}}
        self.wfile.write(("data: " + json.dumps(last) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _gguf(path, arch="qwen2", layers=28, kvh=4, hd=128, ctx=32768, size_label="7B", file_type=15, template="{% if enable_thinking %}<think>{% endif %}"):
    """Write a minimal GGUF v3 header (no tensors) so the reader can be tested without a real model."""
    def s(v):
        b = v.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [("general.architecture", 8, s(arch)), ("general.name", 8, s("Test Model")), ("general.size_label", 8, s(size_label)), ("general.file_type", 4, struct.pack("<I", file_type)),
          (f"{arch}.block_count", 4, struct.pack("<I", layers)), (f"{arch}.attention.head_count", 4, struct.pack("<I", 28)), (f"{arch}.attention.head_count_kv", 4, struct.pack("<I", kvh)),
          (f"{arch}.attention.key_length", 4, struct.pack("<I", hd)), (f"{arch}.context_length", 4, struct.pack("<I", ctx)), ("tokenizer.chat_template", 8, s(template)),
          ("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 3) + s("a") + s("b") + s("c"))]
    with open(path, "wb") as f:
        f.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kv)))
        for k, t, v in kv:
            f.write(s(k) + struct.pack("<I", t) + v)
        f.write(b"\0" * 4096)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_passport as P
    import arch_server as S
    import arch_transport as T
    home = tempfile.mkdtemp(prefix="archllama_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        # ---- transport
        tr = T.LlamaTransport(url)
        r = tr.chat("worker", [{"role": "user", "content": "x"}], {"num_predict": 64, "temperature": 0.1, "top_p": 0.9}, False, None, {"first": 10, "idle": 10, "total": 30},
                    {"type": "object", "properties": {"flow": {"type": "string"}}})
        sent = FakeLlama.state["calls"][-1]
        check("llama transport: SSE stream is assembled, usage and exact cache count are read", r["text"].startswith("{") and r["prompt_tokens"] == 300 and r["cached_tokens"] == 250 and r["gen_tokens"] == 12 and r["tok_s"] == 33.3, r)
        check("llama transport: the request carries cache_prompt, max_tokens, sampling, thinking off and a strict json_schema",
              sent.get("cache_prompt") is True and sent.get("max_tokens") == 64 and sent.get("temperature") == 0.1 and sent.get("chat_template_kwargs") == {"enable_thinking": False}
              and sent.get("response_format", {}).get("type") == "json_schema" and sent["response_format"]["json_schema"]["strict"] is True, sent)
        FakeLlama.state["reject_schema"] = True
        r = tr.chat("worker", [{"role": "user", "content": "x"}], {"num_predict": 64}, None, None, 10, {"type": "object"})
        check("llama transport: a build that rejects json_schema is retried without it (validated in code later), not failed", r["text"] and "response_format" not in FakeLlama.state["calls"][-1])
        FakeLlama.state["window_full"] = True
        try:
            r = tr.chat("worker", [{"role": "user", "content": "x"}], {"num_predict": 64}, None, None, 10)
        except core.LLMError as e:
            r = {"error": str(e)}
        blocks = core.parse_blocks(r.get("text") or "")
        check("llama transport: a window that fills up mid-reply returns the reply cut off at its limit (not an error that ends the run); "
              "the finished block applies, the cut one is refused",
              r.get("done_reason") == "length" and r.get("gen_tokens", 0) > 0 and [b["path"] for b in blocks if not b.get("truncated")] == ["a.py"]
              and any(b.get("truncated") for b in blocks), (r, blocks))
        FakeLlama.state["slow"] = 3
        try:
            tr.chat("worker", [{"role": "user", "content": "x"}], {}, None, None, {"first": 1, "idle": 1, "total": 5})
            check("llama transport: the first-token timeout is enforced", False)
        except core.LLMError as e:
            check("llama transport: the first-token timeout is enforced and explained", "first token" in str(e), e)
        FakeLlama.state["slow"] = 0
        try:
            T.LlamaTransport("http://127.0.0.1:1").chat("worker", [{"role": "user", "content": "x"}], {}, None, None, 3)
            check("llama transport: an unreachable server is a plain error", False)
        except core.LLMError as e:
            check("llama transport: an unreachable server is a plain error naming the fix", "not reachable" in str(e) and "server start" in str(e), e)
        check("route(): a remote host is always Ollama; an unknown local model is local Ollama", isinstance(T.route("m", "http://10.0.0.2:11434"), T.OllamaTransport) and isinstance(T.route("nope:1b"), T.OllamaTransport))
        real_fns = (S.plan, S.status, S.start)
        started = []
        S.plan = lambda c, *a, **k: {"mode": "llama", "tiers": {"worker": {"model": "D:/m/big.gguf"}},
                                     "processes": [{"tier": "worker", "name": "big:4b", "gguf": "D:/m/big.gguf", "resident": True},
                                                   {"tier": "router", "name": "r:1b", "resident": True}]}
        S.status = lambda c: {"processes": []}
        S.start = lambda c, tiers=None, say=None: started.append(sorted(tiers)) or {"processes": []}
        try:
            S.ensure({}, say=lambda *a: None, wanted={"fake:7b"})
            S.ensure({}, say=lambda *a: None, wanted={"big:4b"})
            S.ensure({}, say=lambda *a: None)
            S.ensure({}, say=lambda *a: None, wanted={"D:/m/big.gguf"})
            S.ensure({}, say=lambda *a: None, wanted={"tier:worker"})
        finally:
            S.plan, S.status, S.start = real_fns
        check("ensure(): a resident tier starts only when the run will call its model (the scripted benchmark used to start the real "
              "servers and leave 3.7 GB of graphics memory idle); not knowing starts what is planned", started[:2] == [["worker"], ["router", "worker"]], started)
        check("ensure(): a tier is matched the way the transport matches it (a .gguf path, tier:worker), so a run that names its model "
              "by path still gets its server", started[2:] == [["worker"], ["worker"]], started)
        check("core.stream_chat and core.LLMError are the single entry point and error for every caller", core.stream_chat.__doc__ and "arch_transport" in core.stream_chat.__doc__ and T.LLMError is core.LLMError)

        # ---- serving / state: a planned process answers to its tier, tag and file name
        S.save_state({"mode": "llama", "processes": {"worker": {"tier": "worker", "name": "qwen2.5-coder:7b", "gguf": os.path.join(home, "w.gguf"), "port": srv.server_address[1], "pid": os.getpid(), "url": url, "ctx": 8192}}})
        check("serving(): the tier alias, the Ollama tag, the GGUF path and 'tier:worker' all map to the running process", all(S.serving(m) and S.serving(m)["url"] == url for m in ("worker", "qwen2.5-coder:7b", os.path.join(home, "w.gguf"), "tier:worker")) and S.serving("other") is None)
        check("route(): a model the server serves goes to the llama transport", isinstance(T.route("qwen2.5-coder:7b"), T.LlamaTransport))
        check("list_models() includes what llama-server serves", "qwen2.5-coder:7b" in core.list_models())
        r = core.stream_chat("qwen2.5-coder:7b", [{"role": "user", "content": "x"}], {"num_predict": 8}, None, "30m", 10)
        check("core.stream_chat reaches llama-server for a served model and reports transport='llama' with exact cache tokens", r["transport"] == "llama" and r["cached_tokens"] == 250, r)
        S.save_state({})

        # ---- GGUF header + passport
        g = os.path.join(home, "test-7b-Q4_K_M.gguf")
        _gguf(g)
        meta = P.gguf_meta(g)
        arch = P.arch_from_gguf(meta)
        check("GGUF header reader: architecture, layers, kv heads, head size, context, quant and size label come out of a v3 header", arch["layers"] == 28 and arch["kv_heads"] == 4 and arch["head_dim"] == 128 and arch["ctx_max"] == 32768 and arch["quant"] == "Q4_K_M" and arch["params_b"] == 7.0, arch)
        check("GGUF header reader: a non-GGUF file is refused plainly", "error" in P.gguf_meta(__file__))
        p = P.build("test-7b-Q4_K_M.gguf", gguf=g)
        check("passport: a raw GGUF gets size, quant, family, a thinking hint and plain-words notes", p["quant"] == "Q4_K_M" and p["family"] == "qwen2" and p["think_style"] == "tags" and p["eff_b"] == 7.0 and len(p["notes"]) >= 3, p)
        check("passport: every note starts with a plain sentence, not a number or an acronym", all(n[0].isalpha() and n[0].isupper() for n in p["notes"]), p["notes"])
        P.save(p)
        check("passport: saved under hub_data/passports and loadable by name", P.load("test-7b-Q4_K_M.gguf")["eff_b"] == 7.0)

        # ---- Ollama blob adoption on a fake models dir
        md = os.path.join(home, "ollama")
        os.makedirs(os.path.join(md, "manifests", "registry.ollama.ai", "library", "qwen2.5-coder"), exist_ok=True)
        os.makedirs(os.path.join(md, "manifests", "registry.ollama.ai", "someone", "niche-model"), exist_ok=True)
        os.makedirs(os.path.join(md, "blobs"), exist_ok=True)
        _gguf(os.path.join(md, "blobs", "sha256-aaa"))
        for man in (os.path.join(md, "manifests", "registry.ollama.ai", "library", "qwen2.5-coder", "7b"), os.path.join(md, "manifests", "registry.ollama.ai", "someone", "niche-model", "latest")):
            with open(man, "w") as f:
                json.dump({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:aaa", "size": 4096}, {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:bbb"}]}, f)
        a = S.adopt_ollama("qwen2.5-coder:7b", md)
        b = S.adopt_ollama("someone/niche-model", md)
        check("adopt_ollama: a library tag and a niche namespaced tag both resolve to the GGUF blob Ollama already has (no second download)", a and a["gguf"].endswith("sha256-aaa") and b and b["gguf"] == a["gguf"], (a, b))
        check("adopt_ollama: an unknown tag is None, never a guess", S.adopt_ollama("nope:latest", md) is None)

        # ---- profiles and plan
        hw6 = {"gpu": "RTX 4050", "vram_gb": 6.0, "ram_gb": 16.0, "cpu_cores": 12, "vendor": "nvidia", "unified": False}
        check("profile_for: 6 GB -> gpu6, 12 GB -> gpu12, no GPU 16 GB -> cpu16, Apple 32 GB unified (~22 GB usable) -> gpu16", S.profile_for(hw6) == "gpu6" and S.profile_for(dict(hw6, vram_gb=12.2)) == "gpu12"
              and S.profile_for({"vram_gb": 0, "ram_gb": 16}) == "cpu16" and S.profile_for({"vram_gb": 0, "ram_gb": 32, "unified": True}) == "gpu16")
        profs = S.list_profiles()
        check("every shipped profile names the four tiers, a context, a KV type, budgets and a plain-words line", len(profs) >= 8 and all(set(p["tiers"]) >= {"router", "worker", "heavy", "draft"} and p.get("ctx") and p.get("kv") and p.get("budgets") and p.get("plain") for p in profs), [p.get("name") for p in profs])
        cfg = core.load_config()
        cfg["tiers"] = {"worker": g, "router": "", "heavy": "", "draft": ""}
        cfg["server"]["binary"] = os.path.join(home, "nope.exe")
        real_bin = S.resolve_binary
        S.resolve_binary = lambda c=None: None
        try:
            pl = S.plan(cfg, hw6)
        finally:
            S.resolve_binary = real_bin
        check("plan(): without the llama-server program every tier stays on Ollama and says so in plain words", pl["mode"] == "ollama" and pl["notes"] and "not installed" in pl["notes"][0], pl)
        pl0 = S.plan(core.deep_merge(cfg, {"server": {"kind": "ollama"}}), hw6)
        check("plan(): Settings > Server = ollama never starts llama-server even when it is installed", pl0["mode"] == "ollama" and "Ollama" in pl0["notes"][0], pl0["notes"])
        stub = os.path.join(home, "llama-server" + (".exe" if sys.platform.startswith("win") else ""))
        with open(stub, "w") as f:
            f.write("stub")
        cfg["server"]["binary"] = stub
        fake_caps = {"flags": ["--host", "--port", "-m", "-c", "-ngl", "--alias", "-t", "--jinja", "--flash-attn", "-fa", "--cache-type-k", "--cache-type-v", "-ub", "--model-draft", "-ngld", "--draft-max"], "fa_mode": "value"}
        cfg["tiers"]["draft"] = g
        pl = S.plan(cfg, hw6, caps_=fake_caps)
        w = next(p for p in pl["processes"] if p["tier"] == "worker")
        check("plan(): a 7B-shaped GGUF on a 6 GB card gets every layer on the GPU with q8_0 cache, flash attention, jinja and the draft beside it",
              pl["mode"] == "llama" and w["ngl"] == 28 and w["full_gpu"] and "--cache-type-k" in w["args"] and "-fa" in w["args"] and "--jinja" in w["args"] and "--model-draft" in w["args"] and w["draft"], w)
        pl2 = S.plan(cfg, hw6, caps_={"flags": ["--host", "--port", "-m", "-c", "-ngl", "--alias", "-t"], "fa_mode": "switch"})
        w2 = next(p for p in pl2["processes"] if p["tier"] == "worker")
        check("plan(): a build without flash-attention/draft flags is never given them (caps decide)", "--cache-type-k" not in w2["args"] and "--model-draft" not in w2["args"] and "-fa" not in w2["args"], w2["args"])
        hf, hk = S._parse_help("--draft, --draft-max N   the argument has been removed. use --spec-draft-n-max\n--spec-draft-n-max N  how many\n"
                               "--lookup-cache-dynamic FNAME  path\n--spec-type none,draft-simple,ngram-mod,ngram-cache  list (default: none)\n")
        check("caps: a flag the build's help marks as removed is never used (it made llama-server refuse to start), and the speculation "
              "kinds are read", "--draft-max" not in hf and "--spec-draft-n-max" in hf and hk == ["none", "draft-simple", "ngram-mod", "ngram-cache"], (hf, hk))
        spec_caps = dict(fake_caps, flags=fake_caps["flags"] + ["--spec-type", "--spec-draft-n-max", "--lookup-cache-dynamic"], spec_types=hk)
        cfg_nd = core.deep_merge(cfg, {"tiers": {"draft": ""}, "server": {"draft": "off"}})
        wn = next(p for p in S.plan(cfg_nd, hw6, caps_=spec_caps)["processes"] if p["tier"] == "worker")["args"]
        wd = next(p for p in S.plan(cfg, hw6, caps_=spec_caps)["processes"] if p["tier"] == "worker")["args"]
        check("a build that speculates only when told (default none) is told: n-gram lookup without a draft (measured 5-6x faster on "
              "edits here), draft-simple with one, never both, and the draft size uses the current spelling",
              wn[wn.index("--spec-type") + 1] == "ngram-cache" and "--lookup-cache-dynamic" in wn and "--model-draft" not in wn
              and wd[wd.index("--spec-type") + 1] == "draft-simple" and "--lookup-cache-dynamic" not in wd and "--spec-draft-n-max" in wd
              and "--draft-max" not in wd, (wn, wd))
        par_caps = dict(fake_caps, flags=fake_caps["flags"] + ["--parallel"])

        def c_and_np(pl_):
            a = next(p for p in pl_["processes"] if p["tier"] == "worker")["args"]
            return int(a[a.index("-c") + 1]), (int(a[a.index("--parallel") + 1]) if "--parallel" in a else None), a
        one = c_and_np(S.plan(cfg, hw6, caps_=par_caps))
        two = c_and_np(S.plan(core.deep_merge(cfg, {"server": {"parallel": 2}}), hw6, caps_=par_caps))
        check("plan(): the slot count is always said (a build left to choose opens 4 slots that share one window), and each slot gets "
              "the whole window (-c is the total)", one[1] == 1 and two[1] == 2 and two[0] == 2 * one[0], (one[:2], two[:2]))

        # ---- the machine audit (2026-11-03): threads, prompt cache, RAM budget, Ollama
        import arch_hw
        import arch_ollama
        pc = arch_hw.perf_cores()
        check("hw: the number of performance cores is read (P-cores on a hybrid chip), within the logical CPU count, or None when unknown",
              pc is None or 1 <= pc <= (os.cpu_count() or 1), pc)
        cram_caps = dict(fake_caps, flags=fake_caps["flags"] + ["--cache-ram"])

        def tier_args(pl_, tier):
            return next(p for p in pl_["processes"] if p["tier"] == tier)["args"]
        p16 = S.plan(cfg, dict(hw6, cpu_perf_cores=4, ram_gb=16.0), caps_=cram_caps)
        p32 = S.plan(cfg, dict(hw6, cpu_perf_cores=4, ram_gb=32.0), caps_=cram_caps)
        w16, w32 = tier_args(p16, "worker"), tier_args(p32, "worker")
        check("plan(): about one thread per performance core (was logical CPUs - 1: 11 per server on a 4 P + 4 E-core laptop)",
              w16[w16.index("-t") + 1] == "4", w16)
        check("plan(): the RAM prompt cache is capped by the machine (1 GB per server on 16 GB, 2 GB on 32 GB; llama-server's own default is 8 GB)",
              w16[w16.index("--cache-ram") + 1] == "1024" and w32[w32.index("--cache-ram") + 1] == "2048", (w16, w32))
        tight = S.plan(cfg, dict(hw6, cpu_perf_cores=4, ram_gb=16.0, ram_free_gb=1.0), caps_=cram_caps)
        check("plan(): when the models kept loaded need more RAM than is really free, it says so in plain words (it used to assume a fixed 6 GB "
              "for Windows and apps; 9.3 GB was measured in use with no model loaded)",
              any("kept loaded need about" in n for n in tight["notes"]) and not any("kept loaded need about" in n for n in
              S.plan(cfg, dict(hw6, cpu_perf_cores=4, ram_gb=16.0, ram_free_gb=12.0), caps_=cram_caps)["notes"]), tight["notes"])
        real_status, real_find = arch_ollama.status, arch_ollama.find_exe
        said = []
        try:
            arch_ollama.status = lambda host=None, timeout=3: {"up": True}
            up_already = arch_ollama.ensure_running(say=said.append)
            arch_ollama.status = lambda host=None, timeout=3: {"up": False}
            arch_ollama.find_exe = lambda: None
            missing = arch_ollama.ensure_running(say=said.append, wait_s=1)
        finally:
            arch_ollama.status, arch_ollama.find_exe = real_status, real_find
        check("Ollama is started as its own process only when nothing answers, and a missing install is said plainly (seen live: an Ollama "
              "started from a terminal stopped with it)", up_already is True and missing is False and any("not installed" in x for x in said), said)
        said2 = []
        try:
            arch_ollama.status = lambda host=None, timeout=3: {"up": False, "port_open": True, "error": "port 11434 is open but it is not answering like Ollama"}
            busy = arch_ollama.ensure_running(say=said2.append, wait_s=1)
        finally:
            arch_ollama.status = real_status
        check("a port already held by another program never gets a second Ollama, and the real cause is said",
              busy is False and any("not starting a second one" in x for x in said2), said2)
        check("any host form Ollama accepts is parsed without crashing (IPv6 crashed `start`)",
              arch_ollama.port_of("[::1]:11500") == ("::1", 11500) and arch_ollama.port_of("127.0.0.1") == ("127.0.0.1", 11434)
              and arch_ollama.port_of("http://localhost:11434") == ("localhost", 11434))

        # ---- B: the crash recorder; C: models sized from the graphics memory really free
        rec_dir = tempfile.mkdtemp(prefix="rec_", dir=home)
        rec_path = os.path.join(rec_dir, "live.log")
        fake_smi = lambda cmd, timeout=8: (0, "37, 2900, 6141, 61, [N/A]\n") if cmd and cmd[0] == "nvidia-smi" else (1, "")
        row = arch_hw.record_once(rec_path, run=fake_smi, extra={"servers": ["worker"]})
        with open(rec_path, "w", encoding="utf-8") as f:
            f.write("x" * 50)
        arch_hw.record_once(rec_path, run=fake_smi, max_bytes=10)
        saved = [json.loads(l) for l in open(rec_path, encoding="utf-8") if l.strip()]
        check("B: the recorder writes one line per sample with GPU use, memory, temperature, power source and running servers; an [N/A] "
              "column no longer blanks the rest; the log rolls over at its size limit",
              row["gpu_pct"] == 37 and row["vram_used_gb"] == round(2900 / 1024, 2) and row["gpu_temp_c"] == 61 and row["gpu_power_w"] is None
              and row["servers"] == ["worker"] and "on_ac" in row and os.path.isfile(rec_path + ".1") and len(saved) == 1, (row, saved))
        c_heavy = os.path.join(home, "c_heavy.gguf")
        _gguf(c_heavy, layers=48, kvh=8, size_label="14B")
        cfg_c = core.deep_merge(cfg, {"tiers": {"heavy": c_heavy}})
        real_res2 = S.resolve_model
        S.resolve_model = lambda spec, c=None: (dict(real_res2(spec, c) or {}, size=9 * 1024 ** 3) if str(spec).endswith("c_heavy.gguf") else real_res2(spec, c))
        try:
            roomy = S.plan(cfg_c, dict(hw6, vram_free_gb=5.3), caps_=fake_caps)
            busy = S.plan(cfg_c, dict(hw6, vram_free_gb=3.0), caps_=fake_caps)
        finally:
            S.resolve_model = real_res2
        h_roomy = next(p for p in roomy["processes"] if p["tier"] == "heavy")
        h_busy = next(p for p in busy["processes"] if p["tier"] == "heavy")
        check("C: the models are sized from the graphics memory really free (a busy desktop or external monitor), not a fixed 0.7 GB, "
              "and a desktop holding over 1 GB is said in plain words",
              h_busy["ngl"] < h_roomy["ngl"] and any("hold 3.0 GB of the graphics card" in n for n in busy["notes"])
              and not any("of the graphics card right now" in n for n in roomy["notes"]), (h_roomy["ngl"], h_busy["ngl"], busy["notes"]))

        # ---- A: Docker / WSL awareness (the person's dev setup is planned around, never changed)
        wslc = os.path.join(home, ".wslconfig")
        with open(wslc, "w", encoding="utf-8") as f:
            f.write("[wsl2]\nmemory=6GB # cap\nprocessors=4\n")
        lim6 = arch_hw.wsl_limit_gb(15.6, path=wslc)
        with open(wslc, "w", encoding="utf-8") as f:
            f.write("[wsl2]\nmemory=8192MB\n")
        lim8 = arch_hw.wsl_limit_gb(15.6, path=wslc)
        with open(wslc, "w", encoding="utf-8") as f:
            f.write("[wsl2]\nprocessors=4\n")
        lim_default = arch_hw.wsl_limit_gb(16.0, path=wslc)
        vm_run = lambda cmd, timeout=8: (0, '"vmmemWSL","4321","Services","0","4,194,304 K"\n') if cmd[0] == "tasklist" else (1, "")
        vm_none = lambda cmd, timeout=8: (0, "INFO: No tasks are running which match the specified criteria.\n") if cmd[0] == "tasklist" else (1, "")
        vr, vn = arch_hw.vm_memory(vm_run, ram_gb=16.0), arch_hw.vm_memory(vm_none, ram_gb=16.0)
        check("A: the WSL2/Docker VM's RAM is read (4 GB from tasklist, nothing when it is not running) and its limit from .wslconfig "
              "(6GB, 8192MB, or WSL's default of half the RAM)",
              lim6 == 6.0 and lim8 == 8.0 and lim_default == 8.0 and (os.name != "nt" or (vr["running"] and vr["vm_gb"] == 4.0)) and not vn["running"],
              (lim6, lim8, lim_default, vr, vn))
        cram_caps2 = dict(fake_caps, flags=fake_caps["flags"] + ["--cache-ram"])
        with_vm = S.plan(cfg, dict(hw6, cpu_perf_cores=4, ram_gb=16.0, ram_free_gb=4.0, vm={"running": True, "vm_gb": 3.1, "limit_gb": 6.0}), caps_=cram_caps2)
        no_vm = S.plan(cfg, dict(hw6, cpu_perf_cores=4, ram_gb=16.0, ram_free_gb=4.0, vm={"running": False}), caps_=cram_caps2)
        check("A: a running Docker/WSL VM is planned around: its room to grow is kept free and said in plain words; when it is not running "
              "nothing is said", any("Docker/WSL is using 3.1 GB" in n for n in with_vm["notes"])
              and not any("Docker/WSL" in n for n in no_vm["notes"]), (with_vm["notes"], no_vm["notes"]))
        _gguf(os.path.join(home, "big.gguf"), layers=48, kvh=8, size_label="14B")
        with open(os.path.join(home, "big.gguf"), "ab") as f:
            f.write(b"\0" * (9 * 1024 * 1024))
        cfg["tiers"]["heavy"] = os.path.join(home, "big.gguf")
        real_resolve = S.resolve_model
        S.resolve_model = lambda spec, c=None: dict(real_resolve(spec, c) or {}, size=9 * 1024 ** 3) if spec.endswith("big.gguf") else real_resolve(spec, c)   # a 9 GB file without writing one
        try:
            pl3 = S.plan(cfg, hw6, caps_=fake_caps)
        finally:
            S.resolve_model = real_resolve
        hv = next(p for p in pl3["processes"] if p["tier"] == "heavy")
        check("plan(): a model that does not fit the card gets a partial offload and says 'the rest runs from RAM'", 0 < hv["ngl"] < 48 and not hv["full_gpu"] and "RAM" in hv["why"], hv)
        real_arch, real_res = S._arch_for, S.resolve_model                  # a 30B mixture-of-experts model (128 experts, 8 used) at 18.6 GB
        S._arch_for = lambda spec, show=None: (dict(real_arch(spec, show) or {}, experts=128, experts_used=8, layers=48)
                                               if str(spec).endswith("big.gguf") else real_arch(spec, show))
        S.resolve_model = lambda spec, c=None: (dict(real_res(spec, c) or {}, size=int(18.6 * 1024 ** 3)) if str(spec).endswith("big.gguf")
                                                else real_res(spec, c))
        moe_caps = dict(fake_caps, flags=fake_caps["flags"] + ["--n-cpu-moe"])
        try:
            c_off = core.deep_merge(cfg, {"server": {"heavy_beside": "off"}})
            pm32, pm16 = S.plan(c_off, dict(hw6, ram_gb=32.0), caps_=moe_caps), S.plan(c_off, dict(hw6, ram_gb=16.0), caps_=moe_caps)
            pb32, pb16 = S.plan(cfg, dict(hw6, ram_gb=32.0), caps_=moe_caps), S.plan(cfg, dict(hw6, ram_gb=16.0), caps_=moe_caps)
        finally:
            S._arch_for, S.resolve_model = real_arch, real_res
        import arch_passport
        pdir = arch_passport.passports_dir()
        os.makedirs(pdir, exist_ok=True)
        dfile = {}
        for nm, pb, sz, tok in (("wk", 4.0, 2.3, ("gpt2", "qwen2", 151936)), ("d1", 0.6, 0.5, ("gpt2", "qwen2", 151936)),
                                ("d2", 0.5, 0.4, ("llama", "default", 32000)), ("d3", 2.0, 1.3, ("gpt2", "qwen2", 151936))):
            dfile[nm] = os.path.join(home, nm + ".gguf")
            _gguf(dfile[nm])
            S._TOK[dfile[nm]] = tok
            if nm != "wk":
                with open(os.path.join(pdir, nm + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"name": dfile[nm], "arch": {"params_b": pb}, "size_gb": sz}, f)
        got = S.pick_draft({"gguf": dfile["wk"], "name": "wk"}, {"params_b": 4.0}, 2.9, hw6)
        full_card = S.pick_draft({"gguf": dfile["wk"], "name": "wk"}, {"params_b": 4.0}, 4.9, hw6)
        check("draft: the smallest installed model with exactly the worker's tokenizer, at most a third of its size, that fits beside it "
              "(a smaller one with another tokenizer, a bigger one, or no room on the card: none)",
              got and got["gguf"] == dfile["d1"] and full_card is None, (got, full_card))
        wtok = S._TOK[dfile["wk"]]
        S._TOK[dfile["d2"]] = ("llama", "default", 32000)
        S._TOK[w["gguf"]] = wtok
        pdx = S.plan(core.deep_merge(cfg, {"tiers": {"draft": dfile["d2"]}}), hw6, caps_=fake_caps)
        wdx = next(p for p in pdx["processes"] if p["tier"] == "worker")
        check("a draft model you set whose tokenizer differs from the worker's is not launched, and the plan says why",
              "--model-draft" not in wdx["args"] and any("different tokenizer" in n for n in pdx["notes"]), (wdx["args"], pdx["notes"]))
        wfull = next(p for p in S.plan(cfg, hw6, caps_=fake_caps)["processes"] if p["tier"] == "worker")
        check("'all layers on the GPU' includes the output layer (llama.cpp counts it as layer n+1)",
              wfull["full_gpu"] and wfull["args"][wfull["args"].index("-ngl") + 1] == str(wfull["ngl"] + 1), wfull["args"])
        hb = next(p for p in pb32["processes"] if p["tier"] == "heavy")
        hb16 = next(p for p in pb16["processes"] if p["tier"] == "heavy")
        check("plan(): a mixture-of-experts heavy model stays loaded beside the worker (resident, experts in RAM, said in plain words) when "
              "RAM allows (32 GB), and loads only when needed when it does not (16 GB)",
              hb["resident"] and "beside the worker" in hb["why"] and hb["moe_cpu_layers"] > 0 and not hb16["resident"], (hb["why"], hb16["why"]))
        hm = next(p for p in pm32["processes"] if p["tier"] == "heavy")
        check("plan(): a mixture-of-experts model that does not fit the card keeps every layer on the GPU and only some layers' experts in "
              "RAM (--n-cpu-moe), said in plain words; with 32 GB of RAM it fits, with 16 GB the plan says plainly it would page to disk",
              0 < hm["moe_cpu_layers"] < 48 and hm["args"][hm["args"].index("--n-cpu-moe") + 1] == str(hm["moe_cpu_layers"]) and hm["ngl"] == 48
              and "mixture-of-experts" in hm["why"] and not any("page to disk" in n for n in pm32["notes"])
              and any(n.startswith("heavy:") and "page to disk" in n for n in pm16["notes"]), (hm, pm32["notes"], pm16["notes"]))
        check("status(): with nothing started it reports the mode and no processes, without touching the network", S.status(cfg)["processes"] == [])
        forced = "{%- if add_generation_prompt %}{{- '<|im_start|>assistant" + chr(92) + "n<think>" + chr(92) + "n' }}{%- endif %}"
        tf = S.nothink_template(forced, "worker")
        body = open(tf, encoding="utf-8").read() if tf else ""
        check("a chat template that forces an open <think> gets a copy that closes it at once (the no-think form), written under hub_data/server", tf and "</think>" in body and "nothink" in tf, body[-80:])
        check("a template that does not force thinking is left alone (None)", S.nothink_template("{{ messages }}", "worker") is None and S.nothink_template(body, "worker") is None)
        _gguf(os.path.join(home, "think.gguf"), template=forced)
        cfg["tiers"]["worker"] = os.path.join(home, "think.gguf")
        plw = next(p for p in S.plan(cfg, hw6, caps_=dict(fake_caps, flags=fake_caps["flags"] + ["--reasoning-budget", "--chat-template-file", "--reasoning-format"]))["processes"] if p["tier"] == "worker")
        check("plan(): the worker runs with reasoning off (budget 0) and the patched template; a build without those flags simply gets neither", "--reasoning-budget" in plw["args"] and "--chat-template-file" in plw["args"] and "--reasoning-budget" not in w2["args"], plw["args"][-8:])
        cfg["tiers"]["worker"] = g

        # ---- model math: plain words first, numbers second, formula third
        import arch_modelmath as mm
        c7 = {"name": "coder-7b", "size_gb": 4.4, "arch": {"layers": 28, "kv_heads": 4, "head_dim": 128}, "params_b": 7.6, "eff_b": 7.6, "quant": "Q4_K_M", "think": False, "measured": {"gen_tok_s": 31.0}}
        c14 = {"name": "big-14b", "size_gb": 9.0, "arch": {"layers": 48, "kv_heads": 8, "head_dim": 128}, "params_b": 14.8, "eff_b": 14.8, "quant": "Q4_K_M", "think": True}
        moe = {"name": "moe-30b-a3b", "size_gb": 18.6, "arch": {"layers": 48, "kv_heads": 4, "head_dim": 128, "experts": 128, "experts_used": 8}, "params_b": 30.5, "active_b": 3.3, "eff_b": 13.0, "quant": "Q4_K_M"}
        adv = mm.advise(hw6, [c14, c7, moe], "edits", 8192)
        rows = {r["option"]: r for r in adv["rows"]}
        check("advise: the model that fits the card is listed first, the partial-offload one after, with the fit named in plain words",
              adv["rows"][0]["option"] == "coder-7b" and rows["coder-7b"]["fit"] == "full" and rows["big-14b"]["fit"] == "partial" and "ordinary RAM" in rows["big-14b"]["plain"], [(r["option"], r["fit"]) for r in adv["rows"]])
        check("advise: every row leads with a plain sentence, carries benefit, weakness, numbers, a formula and its evidence label",
              all(r["plain"][0].isupper() and r["benefit"] and r["weakness"] and r["numbers"]["total_gb"] and "weights" in r["formula"] and r["speed_evidence"] in ("measured", "estimate") for r in adv["rows"]))
        check("advise: a measured speed is labelled measured; an estimate is labelled estimate", rows["coder-7b"]["speed_evidence"] == "measured" and rows["big-14b"]["speed_evidence"] == "estimate")
        check("advise: a mixture-of-experts model is explained (quick for its size, needs room for all of it) and does not fit a 6 GB + 16 GB machine", "part of the model" in rows["moe-30b-a3b"]["benefit"] and rows["moe-30b-a3b"]["fit"] in ("partial", "no"), rows["moe-30b-a3b"])
        check("advise: a thinking model's trade-off is stated both ways", "thinks before answering" in rows["big-14b"]["benefit"] and "thinking" in rows["big-14b"]["weakness"])
        qs = mm.compare_quants(c14, hw6, 8192)
        check("quant table: smaller formats are smaller and the plain line says what each costs; Q8 of a 14B does not fit the card, Q3 lands more layers on it",
              qs[0]["numbers"]["total_gb"] > qs[-1]["numbers"]["total_gb"] and all(r["plain"] for r in qs) and qs[0]["fit"] != "full" and int(qs[-1]["numbers"]["layers_on_gpu"].split("/")[0]) > int(qs[0]["numbers"]["layers_on_gpu"].split("/")[0]), [(r["option"], r["fit"], r["numbers"]["layers_on_gpu"]) for r in qs])
        sp = mm.speculation(c7, {"name": "d-0.5b", "size_gb": 0.5, "arch": {"layers": 24, "kv_heads": 2, "head_dim": 64}, "params_b": 0.5, "quant": "Q8_0"}, hw6, 8192)
        check("speculation: the draft's expected speed-up is explained in plain words with the formula and whether both fit", "helper" in sp["plain"] and sp["numbers"]["expected_speedup"] > 1 and "speed-up" in sp["formula"] and sp["numbers"]["fits"] in (True, False), sp)
        check("advise: the glossary explains VRAM, KV cache, quant, MoE, draft and offload in plain words", {"VRAM", "KV cache", "quant", "MoE", "draft model", "offload"} <= set(adv["glossary"]))
        check("render: the text output never leads a row with a number or an acronym", all(not ln.strip()[0].isdigit() for ln in mm.render(adv).splitlines() if ln.startswith("    ") and ln.strip()))

        # ---- make room: the worker steps off the graphics card for a model that does not fit beside it, and comes back after
        procs_mr = {
            "worker": {"tier": "worker", "name": "coder:4b", "gguf": os.path.join(home, "w4.gguf"), "port": 1, "pid": os.getpid(), "url": "http://127.0.0.1:1", "ctx": 8192, "ngl": 37, "size_gb": 2.5},
            "router": {"tier": "router", "name": "tiny:1b", "gguf": os.path.join(home, "r1.gguf"), "port": 2, "pid": os.getpid(), "url": "http://127.0.0.1:2", "ctx": 4096, "ngl": 0}}
        S.save_state({"mode": "llama", "processes": json.loads(json.dumps(procs_mr))})
        real_res3 = S.resolve_model
        sizes = {"big:14b": 9, "mid:7b": 3, "small:1b": 1}
        S.resolve_model = lambda spec, c=None: ({"name": spec, "gguf": "", "size": sizes[spec] * 1024 ** 3} if spec in sizes else real_res3(spec, c))
        full = {"vram_gb": 6.0, "vram_used_gb": 4.2}
        idle = lambda u: False
        G = 1024 ** 3
        try:
            d_big = S.step_aside_for("big:14b", {"tiers": {"worker": "coder:4b"}}, live=full, busy_=idle, held_=[])
            d_mid = S.step_aside_for("mid:7b", {"tiers": {"worker": "coder:4b"}}, live=full, busy_=idle, held_=[])
            d_small = S.step_aside_for("small:1b", {}, live=full, busy_=idle, held_=[])
            d_busy = S.step_aside_for("mid:7b", {}, live=full, busy_=lambda u: True, held_=[])
            d_unknown = S.step_aside_for("mid:7b", {}, live=full, busy_=lambda u: None, held_=[])
            d_served = S.step_aside_for("coder:4b", {}, live=full, busy_=idle, held_=[])
            d_nosmi = S.step_aside_for("mid:7b", {}, live={"vram_gb": None, "vram_used_gb": None}, busy_=idle, held_=[])
            d_held = S.step_aside_for("small:1b", {}, live={"vram_gb": 6.0, "vram_used_gb": 5.6}, busy_=idle,
                                      held_=[{"name": "small:1b", "size": 1 * G, "size_vram": 1 * G}])
            d_split = S.step_aside_for("small:1b", {}, live={"vram_gb": 6.0, "vram_used_gb": 4.6}, busy_=idle,
                                       held_=[{"name": "small:1b", "size": 1 * G, "size_vram": int(0.5 * G)}])
        finally:
            S.resolve_model = real_res3
        check("make room: a model that does not fit beside the resident worker but fits the card alone asks only the tier on the card (the "
              "worker) to step aside, never the CPU router; a 14B that would still run mostly from RAM (measured no faster with the card to "
              "itself), a model that fits, a busy worker, one whose state cannot be read, a model our server serves, or an unreadable card move nothing",
              d_mid["tiers"] == ["worker"] and {"coder:4b", "tier:worker", "worker"} <= set(d_mid["names"]["worker"])
              and not d_big["tiers"] and "mostly from RAM" in d_big["why"] and not d_small["tiers"]
              and not d_busy["tiers"] and "another request" in d_busy["why"] and not d_unknown["tiers"] and "cannot be told" in d_unknown["why"]
              and not d_served["tiers"] and not d_nosmi["tiers"],
              (d_mid, d_big, d_small, d_busy, d_unknown, d_served, d_nosmi))
        check("make room: a model Ollama already holds is not a reason to move the worker on its next call (its own share of the card counts "
              "as free; whole on the card = nothing to do)",
              not d_held["tiers"] and "already holds" in d_held["why"] and not d_held["reload"]
              and not d_split["tiers"] and d_split["why"].startswith("it fits") and d_split["reload"],
              (d_held, d_split))
        killed_while_listed = []
        real_kill = S._kill
        S._kill = lambda pid: killed_while_listed.append("worker" in (S.load_state().get("processes") or {}))
        try:
            S.stop(["worker"], say=lambda m: None)
        finally:
            S._kill = real_kill
        check("stop(): the tier leaves the state file before its process is killed, so a watchdog never takes a stopped tier for a crashed "
              "one and starts it again", killed_while_listed == [False] and "router" in (S.load_state().get("processes") or {}), killed_while_listed)

        root_mr = tempfile.mkdtemp(prefix="mr_", dir=home)
        c_mr = core.load_config(root_mr)
        c_mr["tiers"] = {"router": "tiny:1b", "worker": "coder:4b", "heavy": "", "draft": ""}
        c_mr["roles"] = {r: "" for r in c_mr["roles"]}
        c_mr["roles"]["planner"] = "mid:7b"
        log_mr, held_mr, up_mr = [], {}, {"worker": True}
        real_mr = (S.step_aside_for, S.stop, S.ensure, S.status, S.plan, arch_ollama.unload, arch_ollama.status)
        names_w = ["coder:4b", "tier:worker", "worker"]
        S.step_aside_for = lambda m, c=None, **k: ({"tiers": ["worker"], "names": {"worker": names_w}, "why": "it needs about 3.6 GB, 1.8 GB is free and it fits once the worker steps aside"}
                                                   if m == "mid:7b" else {"tiers": [], "names": {}, "why": "fits"})

        def fake_stop(tiers=None, say=None):
            log_mr.append(("stop", list(tiers or [])))
            for t in tiers or []:
                up_mr[t] = False

        def fake_ensure(cfg, say=None, wanted=None, watch=False):
            log_mr.append(("ensure", sorted(wanted or [])))
            up_mr["worker"] = True

        def fake_unload(m, host=None, timeout=20):
            log_mr.append(("unload", m))
            held_mr.pop(m, None)
            return True
        S.stop, S.ensure = fake_stop, fake_ensure
        S.status = lambda cfg=None: {"processes": [{"tier": t, "up": u} for t, u in up_mr.items()]}
        arch_ollama.unload = fake_unload
        arch_ollama.status = lambda timeout=2, host=None: {"loaded": list(held_mr.values())}
        try:
            e_mr = core.Engine(root_mr, "x", "g", c_mr, review="auto", say=lambda m: None, persist=True)
            e_mr._inflight["coder:4b"] = 1
            e_mr._make_room("mid:7b")
            under_call = list(log_mr)
            e_mr._inflight.clear()
            held_mr["mid:7b"] = {"name": "mid:7b", "size": 3 * G, "size_vram": 1 * G}       # split across card and RAM from before
            e_mr._make_room("mid:7b")
            after_aside = list(log_mr)
            on_disk = (S.load_state().get("aside") or {}).get("worker") or {}
            e_mr._bring_back("mid:7b")
            e_mr._bring_back("tiny:1b")
            unchanged = list(log_mr)
            held_mr["mid:7b"] = {"name": "mid:7b", "size": 3 * G, "size_vram": 3 * G}
            e_mr._bring_back("coder:4b")
            back = log_mr[len(unchanged):]
            cleared = not (S.load_state().get("aside") or {})
            report_back = any("came back on the graphics card" in ln for ln in e_mr.report_lines)
            e_mr._make_room("mid:7b")
            log_mr.clear()
            e_mr.finish(True)
            at_end = list(log_mr)
            log_mr.clear()
            e_mr.cfg["server"]["make_room"] = "off"
            e_mr._make_room("mid:7b")
            switched_off = list(log_mr)
            e_studio = core.Engine(root_mr, "x", "g", c_mr, review="auto", say=lambda m: None, persist=False)
            e_studio._make_room("mid:7b")
            studio = list(log_mr)
            # a run that ended without bringing the worker back (Stop button, Ctrl+C): the next ensure() does, after unloading the model
            import subprocess as _sp
            gone = _sp.Popen([sys.executable, "-c", "pass"])
            gone.wait()
            st_or = S.load_state()
            st_or["aside"] = {"worker": {"for": "mid:7b", "owner": gone.pid, "run": "r1", "names": names_w, "t": 0}}
            S.save_state(st_or)
            held_mr["mid:7b"] = {"name": "mid:7b", "size": 3 * G, "size_vram": 3 * G}
            log_mr.clear()
            said_or = []
            S.ensure = real_mr[2]
            up_mr["worker"] = False
            S.plan = lambda cfg=None, hw=None, caps_=None: {"mode": "llama", "notes": [], "tiers": {},
                                                            "processes": [{"tier": "worker", "resident": True, "name": "coder:4b", "gguf": ""}]}
            real_start_mr = S.start

            def fake_start(cfg=None, hw=None, tiers=None, say=None, **k):
                log_mr.append(("start", list(tiers or [])))
                up_mr["worker"] = True
                return {}
            S.start = fake_start
            try:
                S.ensure(c_mr, say=said_or.append)
            finally:
                S.start = real_start_mr
            orphan = list(log_mr)
            orphan_cleared = not (S.load_state().get("aside") or {})
            S.ensure = fake_ensure
            st_ask = S.load_state()
            st_ask["aside"] = {"worker": {"for": "mid:7b", "owner": os.getpid(), "run": "r2", "names": names_w, "t": 0}}
            S.save_state(st_ask)
            real_kill2 = S._kill
            S._kill = lambda pid: None                                         # the records point at this very process: never kill it
            try:
                real_mr[1](None, say=lambda m: None)                           # `architect.py server stop`
            finally:
                S._kill = real_kill2
            stop_forgets = not (S.load_state().get("aside") or {})
            seen_mr = []

            class _Stop(Exception):
                pass

            def spy_room(m):
                seen_mr.append(m)
                raise _Stop()
            e_studio._make_room = spy_room
            try:
                e_studio._call("planner", "plan this")
            except _Stop:
                pass
        finally:
            S.step_aside_for, S.stop, S.ensure, S.status, S.plan, arch_ollama.unload, arch_ollama.status = real_mr
            S.save_state({})
        check("make room (engine): never while a call is waiting on a model; the worker stops for the 7B, the step is written to the servers' "
              "state with this process as owner, and Ollama's copy split across card and RAM is loaded again",
              under_call == [] and after_aside == [("stop", ["worker"]), ("unload", "mid:7b")]
              and on_disk.get("owner") == os.getpid() and on_disk.get("for") == "mid:7b" and on_disk.get("run") == e_mr.run_id,
              (under_call, after_aside, on_disk))
        check("make room (engine): the worker comes back only when a call needs it, with the 7B unloaded first; the record is cleared and the "
              "report says it came back only after it did; it also comes back at the end of the run",
              unchanged == after_aside and back == [("unload", "mid:7b"), ("ensure", names_w)] and cleared and report_back
              and at_end[:1] == [("unload", "mid:7b")] and at_end[-1][0] == "ensure" and not e_mr._aside,
              (unchanged, back, cleared, report_back, at_end))
        check("make room (engine): Make room = off keeps the worker where it is; a Studio reply or a proposal (an engine that never calls "
              "finish) never moves a server; every local call asks for room before it is sent",
              switched_off == [] and studio == [] and seen_mr == ["mid:7b"], (switched_off, studio, seen_mr))
        check("make room: a worker whose run ended without bringing it back (the Stop button, Ctrl+C, a crash) is brought back by the next "
              "ensure(), with the model it made room for unloaded first, and said in plain words; `server stop` forgets a stepping aside, "
              "so nothing brings the worker back behind the person's back",
              orphan[:1] == [("unload", "mid:7b")] and ("start", ["worker"]) in orphan and orphan_cleared
              and any("never brought back" in m for m in said_or) and stop_forgets, (orphan, said_or, stop_forgets))
        lock_seen = []
        import arch_heavy
        with S._starting(say=lambda m: None):
            f2 = open(os.path.join(S.server_dir(), "start.lock"), "a+")
            lock_seen.append(arch_heavy._try_lock(f2))
            f2.close()
        f3 = open(os.path.join(S.server_dir(), "start.lock"), "a+")
        lock_seen.append(arch_heavy._try_lock(f3))
        arch_heavy._unlock(f3)
        f3.close()
        check("start(): only one process starts model servers at a time (an OS file lock), so a run never kills the worker the hub is "
              "still loading", lock_seen == [False, True], lock_seen)

        glog = os.path.join(home, "gpu_test.log")
        with open(glog, "w", encoding="utf-8") as f:
            f.write("old run: ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected\n")
        off_g = os.path.getsize(glog)
        with open(glog, "a", encoding="utf-8") as f:
            f.write("load_tensors: CUDA0 model buffer size = 2300 MiB\n")
        fine = S._gpu_lost(glog, off_g)
        with open(glog, "a", encoding="utf-8") as f:
            f.write("ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected\n")
        lost = S._gpu_lost(glog, off_g)
        check("GPU check: a server whose log says the card could not be used (a no-MUX laptop's NVIDIA card asleep or resetting) is "
              "caught, and only from this start on (an older failure in the same log does not count)", not fine and lost, (fine, lost))

        fake_ps = json.dumps([
            {"letter": "C", "label": "Windows", "fs": "NTFS", "total": 998_000_000_000, "free": 240_000_000_000, "bus": "NVMe", "name": "CT1000P3PSSD8", "disk_size": 1_000_204_886_016, "removable": False},
            {"letter": "D", "label": "", "fs": "FAT32", "total": 31_981_000_000, "free": 31_900_000_000, "bus": "USB", "name": "Generic STORAGE DEVICE", "disk_size": 31_981_568_000, "removable": True},
            {"letter": "E", "label": "", "fs": "exFAT", "total": 1_048_500_000_000, "free": 1_048_400_000_000, "bus": "USB", "name": "YGS ProductCode", "disk_size": 1_048_576_000_000, "removable": True}])
        drs = arch_hw.drive_roles(arch_hw.drives(run=lambda cmd, timeout=30: (0, fake_ps)) if os.name == "nt" else [], models=r"C:\Users\x\.ollama\models")
        by = {d["mount"]: d for d in drs}
        check("drives: each drive's bus and size are read; the NVMe holding the models is the model store, a card reader is an archive "
              "only, and a 1 TB stick reporting exactly 1,000,000 MiB is flagged as a likely fake and given no role",
              os.name != "nt" or (by["C:\\"]["role"] == "models" and by["C:\\"]["bus"] == "nvme" and by["D:\\"]["role"] == "archive"
                                  and not by["D:\\"]["suspect"] and by["E:\\"]["role"] == "none" and "1,000,000 MiB" in by["E:\\"]["suspect"]), drs)
        md_dir = tempfile.mkdtemp(prefix="drv_", dir=home)
        md = arch_hw.measure_drive(md_dir, mb=8)
        check("drives: a speed test writes one temporary file, reads it back byte for byte, and leaves nothing behind",
              md.get("ok") and md.get("write_mb_s", 0) > 0 and md.get("read_mb_s", 0) > 0 and os.listdir(md_dir) == [], (md, os.listdir(md_dir)))

        bw = arch_hw.bandwidth_from(67.9, 2.33, 5.1, 4.6, 3.77)
        check("bandwidth: worked out from real runs (a model fully on the card gives the card's, a split model gives RAM's): this laptop's "
              "4B at 67.9 tok/s and 14B at 5.1 tok/s give about 158 GB/s and 22-23 GB/s, not the 192 / 50 GB/s guesses",
              155 <= bw["gpu_bw"] <= 160 and 21 <= bw["ram_bw"] <= 24 and arch_hw.bandwidth_from(None, 2.3)["gpu_bw"] is None, bw)

        seen4 = []
        real_sc = core.stream_chat
        core.stream_chat = lambda model, msgs, options, th, ka, to, fmt, host=None, **k: seen4.append((model, dict(options), th)) or {
            "text": "ok", "thinking": "", "tok_s": 10.0, "gen_tokens": 2, "prompt_tokens": 10, "cached_tokens": 0, "done_reason": "stop",
            "prefill_tok_s": None, "load_s": 0.0, "transport": "fake"}
        try:
            r4 = tempfile.mkdtemp(prefix="s4_", dir=home)
            c4 = core.load_config(r4)
            c4["roles"] = {r: "qwen3:4b" for r in c4["roles"]}
            c4["behavior"].update(thinking="plan_debug_only", sampling="model_card", cache="off", learn_from_runs="off")
            e4 = core.Engine(r4, "x", "g", c4, review="auto", say=lambda m: None, persist=False)
            e4.call("planner", "plan A")
            e4.call("reviewer", "review B")
        except Exception as ex4:
            seen4.append(("error", str(ex4), None))
        finally:
            core.stream_chat = real_sc
        pl4 = next((x for x in seen4 if x[0] == "qwen3:4b"), None)
        rv4 = seen4[1] if len(seen4) > 1 else None
        check("reasoning and sampling options: with 'plan_debug_only' the planner thinks and a reviewer answers straight away; with "
              "'model_card' a Qwen3 model gets its makers' settings (0.6/0.95/20 thinking, 0.7/0.8/20 otherwise)",
              pl4 and pl4[2] is True and pl4[1].get("temperature") == 0.6 and pl4[1].get("top_k") == 20
              and rv4 and rv4[2] is False and rv4[1].get("temperature") == 0.7 and rv4[1].get("top_p") == 0.8, seen4)

        import arch_archive
        arc = tempfile.mkdtemp(prefix="arc_", dir=home)
        rdir = tempfile.mkdtemp(prefix="run_", dir=home)
        for fn in ("run.json", "trace.jsonl", "report.md"):
            with open(os.path.join(rdir, fn), "w", encoding="utf-8") as f:
                f.write("x")
        gone_drive = os.path.join(home, "unplugged_card")
        arch_archive._save_pending([])
        arch_archive.archive_run({"storage": {"archive": gone_drive}}, rdir, "proj:1", "r1", wait=True)
        pend = arch_archive._load_pending()
        arch_archive.archive_run({"storage": {"archive": arc}}, rdir, "proj:1", "r2", wait=True)
        landed = sorted(os.listdir(os.path.join(arc, "architect_archive", "proj_1")))
        check("archive: a finished run is copied to the archive drive in the background; an unplugged drive is skipped and remembered, "
              "and the next run copies what was left; names are made safe for memory cards",
              len(pend) == 1 and landed == ["r1", "r2"] and arch_archive._load_pending() == []
              and arch_archive.archive_run({"storage": {"archive": ""}}, rdir, "p", "r3") is None, (pend, landed))

        seen16 = []
        real_sc16 = core.stream_chat
        core.stream_chat = lambda model, msgs, options, th, ka, to, fmt, host=None, **k: seen16.append((msgs, dict(options))) or {
            "text": "ok", "thinking": "", "tok_s": 10.0, "gen_tokens": 2, "prompt_tokens": 10, "cached_tokens": 0, "done_reason": "stop",
            "prefill_tok_s": None, "load_s": 0.0, "transport": "fake"}
        try:
            r16 = tempfile.mkdtemp(prefix="s16_", dir=home)
            c16 = core.load_config(r16)
            c16["roles"] = {r: "fake:7b" for r in c16["roles"]}
            c16["limits"]["num_ctx"] = 4096
            c16["behavior"].update(cache="off", learn_from_runs="off")
            e16 = core.Engine(r16, "x", "g", c16, review="auto", say=lambda m: None, persist=False)
            log16 = "FEEDBACK: your last change failed these checks:\n" + ("Traceback line that repeats\n" * 3000) + "AssertionError: expected 3 got 4\n"
            e16.call("generator", "Fix the bug.\n\n" + log16)
        finally:
            core.stream_chat = real_sc16
        u16 = seen16[0][0][-1]["content"] if seen16 else ""
        check("context shrinking: a huge failure log is cut in the middle so the prompt fits the window, keeping its start and the actual "
              "error at the end, and saying what was cut", seen16 and len(u16) < 20000 and "AssertionError: expected 3 got 4" in u16
              and "characters of the failure log cut" in u16 and u16.startswith("Fix the bug."), len(u16))

        tb_caps = dict(fake_caps, flags=fake_caps["flags"] + ["--reasoning-budget", "--reasoning-budget-message"])
        tb_plan = S.plan(core.deep_merge(cfg, {"tiers": {"heavy": cfg["tiers"].get("heavy") or "big.gguf"}}), hw6, caps_=tb_caps)
        tb_heavy = next((p for p in tb_plan["processes"] if p["tier"] == "heavy"), None)
        tb_worker = next((p for p in tb_plan["processes"] if p["tier"] == "worker"), None)
        check("thinking budget: the thinking tier (the planner) gets a token budget for its reasoning and a message to answer when it runs "
              "out; the worker still answers straight away",
              tb_heavy is None or (tb_heavy["args"][tb_heavy["args"].index("--reasoning-budget") + 1] == "800"
                                   and "--reasoning-budget-message" in tb_heavy["args"]
                                   and tb_worker["args"][tb_worker["args"].index("--reasoning-budget") + 1] == "0"),
              (tb_heavy and tb_heavy["args"][-12:], tb_worker and tb_worker["args"][-12:]))

        seenq = []
        real_scq = core.stream_chat
        core.stream_chat = lambda model, msgs, options, th, ka, to, fmt, host=None, **k: seenq.append((msgs, dict(options))) or {
            "text": "{}" if fmt else "ok", "thinking": "", "tok_s": 10.0, "gen_tokens": 2, "prompt_tokens": 10, "cached_tokens": 0,
            "done_reason": "stop", "prefill_tok_s": None, "load_s": 0.0, "transport": "fake"}
        try:
            rq = tempfile.mkdtemp(prefix="sq_", dir=home)
            with open(os.path.join(rq, "page.html"), "w", encoding="utf-8") as f:
                f.write("\n".join(f"<p>filler line {i}</p>" for i in range(5000)) + "\n<p>Work > Task shows the same run</p>\n" + "<p>end</p>\n")
            cq = core.load_config(rq)
            cq["roles"] = {r: "fake:7b" for r in cq["roles"]}
            cq["behavior"].update(cache="off", learn_from_runs="off")
            eq = core.Engine(rq, "bugfix", "Change 'Work > Task shows the same run' to the new name", cq, review="auto", say=lambda m: None, persist=False)
            eq.call("generator", "Fix it.")
            eq.ask_json("planner", "Plan it.", schema={"type": "object"})
        finally:
            core.stream_chat = real_scq
        uq = seenq[0][0][-1]["content"] if seenq else ""
        check("a goal that quotes exact text gets the lines containing it shown to the model with line numbers, however big the file "
              "(the line a self-fix had to change had been cut from a 1.4 MB file)",
              "EXACT LINES THE REQUEST QUOTES" in uq and "5001: <p>Work > Task shows the same run</p>" in uq, uq[-400:])
        check("reply caps fit the call: a structured answer (plan, routing) at most 1536 tokens, code at most 3072, not half the window",
              len(seenq) >= 2 and seenq[0][1]["num_predict"] <= 3072 and seenq[1][1]["num_predict"] <= 1536, [s[1].get("num_predict") for s in seenq])

        # ---- tier resolution in the engine
        root = tempfile.mkdtemp(prefix="p_", dir=home)
        c2 = core.load_config(root)
        c2["tiers"] = {"router": "tiny:1b", "worker": "coder:7b", "heavy": "", "draft": "d:0.5b"}
        c2["roles"] = {r: "" for r in c2["roles"]}
        c2["roles"]["reviewer"] = "tier:heavy"
        e = core.Engine(root, "x", "g", c2, review="auto", say=lambda m: None, persist=False)
        check("model_for: an unassigned coding role runs on the worker tier, the router role on the router tier", e.model_for("generator") == "coder:7b" and e.model_for("router") == "tiny:1b")
        check("model_for: 'tier:heavy' with no heavy model falls back to the worker; a draft never answers a role", e.model_for("reviewer") == "coder:7b" and e.tier_model("draft") == "d:0.5b")
        c2["roles"]["generator"] = "mine:8b"
        check("model_for: a role you assigned a model to always wins over the tier", e.model_for("generator") == "mine:8b")

        # ---- the optional Claude cloud tier (arch_cloud.py), with a fake SDK client: no key, no network, no cost
        import arch_cloud
        import arch_dataset

        class _FakeStream:
            def __init__(self, stop="end_turn", parts=("def f():\n", "    return 1\n")):
                self.parts, self.stop, self.kw = parts, stop, None
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            @property
            def text_stream(self):
                return iter(self.parts)
            def get_final_message(self):
                return type("M", (), {"stop_reason": self.stop, "usage": type("U", (), {"input_tokens": 1000, "output_tokens": 500})()})()

        class _FakeClient:
            def __init__(self, stop="end_turn"):
                self.sent, self.stop = [], stop
                self.messages = self
            def stream(self, **kw):
                self.sent.append(kw)
                return _FakeStream(self.stop)

        off = {"online": {"enabled": False, "cloud_tier": "escalation"}, "tiers": {"cloud": "claude:claude-opus-5-5"}, "limits": {}}
        on = {"online": {"enabled": True, "cloud_tier": "escalation"}, "tiers": {"cloud": "claude-sonnet-5-5"}, "limits": {"cloud_usd_per_run": 0.5}}
        try:
            arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "hi"}], cfg=off, client=_FakeClient())
            refused = False
        except core.LLMError as ex:
            refused = "online features are off" in str(ex)
        check("cloud tier: with online features off a claude: model is refused before any client is made, and escalation never offers it",
              refused and arch_cloud.cloud_model(off) == "" and arch_cloud.cloud_model({"online": {"enabled": True}, "tiers": {"cloud": "x"}}) == "")
        fc, toks = _FakeClient(), []
        arch_cloud._SPENT.update(usd=0.0, calls=0)
        r = arch_cloud.chat(arch_cloud.cloud_model(on), [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
                            {"role": "assistant", "content": "prefill"}], {"num_predict": 3000}, cfg=on, client=fc, on_token=toks.append)
        kw = fc.sent[0]
        check("cloud tier: system text goes in `system`, a trailing assistant turn is dropped (no prefill), no temperature is sent, "
              "effort is set, tokens stream to on_token, the reply is tagged transport=claude with its cost",
              kw["system"] == "S" and kw["messages"][-1]["role"] == "user" and "temperature" not in kw and kw["model"] == "claude-sonnet-5-5"
              and kw["output_config"]["effort"] == "high" and "".join(toks) == r["text"] and r["transport"] == "claude"
              and abs(r["usd"] - (1000 * 2 + 500 * 10) / 1e6) < 1e-9)
        try:
            arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "x"}], cfg=on, client=_FakeClient("refusal"))
            ref = False
        except core.LLMError as ex:
            ref = "refusal" in str(ex)
        arch_cloud._SPENT["usd"] = 0.5
        try:
            arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "x"}], cfg=on, client=_FakeClient())
            capped = False
        except core.LLMError as ex:
            capped = "budget" in str(ex)
        arch_cloud._SPENT.update(usd=0.0, calls=0)
        check("cloud tier: a refusal is 'could not run' (an error), never an answer; the per-run spend cap refuses before the request", ref and capped)
        tp = os.path.join(home, "cloudtrace.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "call", "id": 1, "transport": "claude"}) + "\n" + json.dumps({"type": "call", "id": 2, "transport": "llama"}) + "\n")
        check("cloud tier: Claude's replies never reach the training-data export", list(arch_dataset.read_full(tp)["calls"]) == [2])
        allc = {"online": {"enabled": True, "cloud_tier": "all", "cloud_model": "claude:claude-opus-5-5"}, "tiers": {"worker": "w:4b", "router": "r:1b"}, "roles": {}, "behavior": {}}
        e3 = core.Engine(root, "x", "g", allc, review="auto", say=lambda m: None, persist=False)
        e3.cfg = allc
        gen_all, router_local = e3.model_for("generator"), e3.model_for("router")
        allc["online"]["enabled"] = False
        check("cloud modes: 'all' sends every coding role to Claude but keeps the router local; with online off it falls back to your local models",
              gen_all == "claude:claude-opus-5-5" and router_local != gen_all and e3.model_for("generator") != gen_all
              and arch_cloud.mode({"online": {"enabled": True, "cloud_tier": "chat", "cloud_model": "claude:x"}}) == "chat")
        offmode = {"online": {"enabled": True, "cloud_tier": "off"}, "limits": {}}
        got = []
        try:
            arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "x"}], cfg=offmode, client=_FakeClient())
            blocked = False
        except core.LLMError:
            blocked = True
        with arch_cloud.asked_for(got.append):
            r2 = arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "x"}], cfg=offmode, client=_FakeClient())
        try:
            arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "x"}], cfg={"online": {"enabled": False}, "limits": {}}, client=_FakeClient())
            leak = True
        except core.LLMError:
            leak = False
        arch_cloud._SPENT.update(usd=0.0, calls=0)
        check("chat Local/Claude switch: a message the person sent to Claude works with Use Claude off (online features on), streams to the chat, "
              "and only for that message; with online features off it is still refused",
              blocked and r2["transport"] == "claude" and "".join(got) == r2["text"] and not leak)
    finally:
        srv.shutdown()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SERVER/TRANSPORT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SERVER/TRANSPORT CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
