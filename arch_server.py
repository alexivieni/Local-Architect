"""The resident model server: one manager that starts, watches and stops llama-server (llama.cpp) processes for the model tiers, and
falls back to Ollama when the binary is not there. Nothing is downloaded here; nothing leaves the machine.

    python architect.py server start|stop|status|plan   (the hub and `architect.py start` call the same functions)

How it fits:
  * `profile_for(hw)` names the machine class (gpu6, gpu8, gpu12, gpu16, gpu24, cpu8, cpu16, cpu32); `profiles/<class>.json` carries the
    researched defaults (tier model, context, KV cache type, draft on/off, flags) -- a starting point that `tune` confirms by measurement.
  * `plan(cfg, hw)` turns the person's tier assignments (cfg["tiers"]: any installed model, public or niche) into launch plans per process:
    which GGUF (an Ollama tag is resolved to its blob file, so nothing is downloaded twice), how many layers on the GPU (fit math with the
    model's real architecture), the context, the KV type, the draft model when it shares the tokenizer. Flags are checked against the
    binary's own --help (caps) so a build that lacks one is never given it.
  * `start()` launches one process per resident tier (router/worker, draft inside the worker's process) and keeps `heavy` on demand; a
    watchdog restarts a crashed process up to 3 times in 10 minutes. State: hub_data/server.json. Logs: hub_data/server/<tier>.log.
  * `serving(model)` answers the transport's question "who serves this model name?" so Engine.call routes without caring.
Everything is reversible: `stop()` ends the processes; the Ollama path is untouched.
"""
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import arch_core as core
import arch_hw

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(HERE, "profiles")
TIERS = ("router", "worker", "heavy", "draft")
GB = 1024 ** 3
_WATCH = {}


# ------------------------------------------------------------------------------------------------------------ paths and state
def state_path():
    return os.path.join(core.data_dir(), "server.json")


def server_dir():
    return os.path.join(core.data_dir(), "server")


def load_state():
    return core.load_json(state_path()) or {}


def save_state(st):
    os.makedirs(core.data_dir(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, state_path())


def resolve_binary(cfg=None):
    """The llama-server executable: the configured path, then <HOME>/bin/, then PATH. None = not installed (Ollama is used)."""
    cfg = cfg or core.load_config()
    want = ((cfg.get("server") or {}).get("binary") or "").strip()
    cands = [want] if want else []
    exe = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
    cands += [os.path.join(core.HOME, "bin", exe), os.path.join(core.HOME, "bin", "llama", exe), os.path.join(HERE, "bin", exe), os.path.join(HERE, "bin", "llama", exe)]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which("llama-server")


def _parse_help(text):
    """-> (flags this build accepts, its --spec-type values). A build lists flags it has REMOVED in its help too ("--draft-max ...
    the argument has been removed"): passing one made llama-server refuse to start. Newer builds speculate only when told how
    ("--spec-type none,draft-simple,...,ngram-cache", default none): without it a loaded draft and the n-gram cache both sat idle,
    and the worker ran 5-6x slower on edits than it could."""
    removed = set()
    for ln in text.splitlines():
        if "has been removed" in ln:
            removed |= set(re.findall(r"(?<![\w-])(--?[a-zA-Z][\w-]*)", ln.split("has been removed")[0]))
    flags = sorted(set(re.findall(r"(?<![\w-])(--?[a-zA-Z][\w-]*)", text)) - removed)
    st_ = re.search(r"--spec-type\s+([a-z0-9][\w,-]*)", text)
    return flags, (st_.group(1).split(",") if st_ else [])


def caps(binary):
    """Which flags this build understands (from its own --help), cached by the binary's size+mtime. -> {"flags": [..], "version", "fa_mode"}."""
    if not binary:
        return {"key": "", "flags": [], "version": None, "fa_mode": None, "error": "llama-server is not installed here"}
    st = os.stat(binary)
    cache = os.path.join(server_dir(), "caps.json")
    c = core.load_json(cache)
    key = f"{binary}|{st.st_size}|{int(st.st_mtime)}|caps3"           # caps2: removed flags left out; caps3: --spec-type values read
    if c.get("key") == key:
        return c
    out = {"key": key, "flags": [], "version": None, "fa_mode": None}
    try:
        r = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=20)
        text = r.stdout + r.stderr
        out["flags"], out["spec_types"] = _parse_help(text)
        m = re.search(r"--flash-attn\s*(?:FA|\[?(on\|off\|auto)\]?)", text)
        out["fa_mode"] = "value" if (m and m.group(1)) or re.search(r"--flash-attn\s+\[?on", text) else "switch"
        try:
            v = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
            out["version"] = (v.stdout + v.stderr).strip().splitlines()[0][:80] if (v.stdout + v.stderr).strip() else None
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
    try:
        os.makedirs(server_dir(), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------------------------------------------ Ollama blobs -> GGUF
def ollama_models_dir():
    return arch_hw.models_dir()


def adopt_ollama(tag, models_dir=None):
    """Resolve an Ollama tag to the GGUF blob Ollama already downloaded. -> {"gguf", "size", "digest", "manifest"} or None.
    manifests/<host>/<namespace>/<name>/<tag> is JSON; the layer with mediaType ...image.model is the weights file blobs/sha256-<digest>."""
    base = models_dir or ollama_models_dir()
    name, _, ver = tag.partition(":")
    ver = ver or "latest"
    if "/" in name:
        ns, _, nm = name.rpartition("/")
        parts = ns.split("/")
        if len(parts) == 1:
            parts = ["registry.ollama.ai"] + parts
    else:
        parts, nm = ["registry.ollama.ai", "library"], name
    man = os.path.join(base, "manifests", *parts, nm, ver)
    if not os.path.isfile(man):
        return None
    j = core.load_json(man)
    layers = [l for l in (j.get("layers") or []) if (l.get("mediaType") or "").endswith("image.model")]
    if not layers:
        return None
    digest = layers[0].get("digest", "")
    blob = os.path.join(base, "blobs", digest.replace(":", "-"))
    if not os.path.isfile(blob):
        return None
    return {"gguf": blob, "size": layers[0].get("size") or os.path.getsize(blob), "digest": digest, "manifest": man}


def local_tags(models_dir=None):
    """Every Ollama tag present on disk (from the manifests folder), whether or not Ollama is running. -> ["name:tag", ...]."""
    base = os.path.join(models_dir or ollama_models_dir(), "manifests")
    out = []
    for dp, dn, fn in os.walk(base):
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), base).replace(os.sep, "/").split("/")
            if len(rel) < 4:
                continue
            host, ns, name, tag = rel[0], rel[1], "/".join(rel[2:-1]), rel[-1]
            prefix = "" if ns == "library" else ns + "/"
            out.append(f"{prefix}{name}:{tag}")
    return sorted(out)


def gguf_models(cfg=None):
    """Raw GGUF files named in the tier assignments (paths) -> [{"name", "path"}] so passports can describe them."""
    cfg = cfg or core.load_config()
    out = []
    for t, m in (cfg.get("tiers") or {}).items():
        if isinstance(m, str) and m.lower().endswith(".gguf") and os.path.isfile(m):
            out.append({"name": os.path.basename(m), "path": m})
    return out


# ------------------------------------------------------------------------------------------------------------ machine class and profiles
def profile_for(hw):
    """One name for the machine class. gpuN = about N GB of VRAM (rounded down to the known classes); cpuN = no usable GPU, N GB of RAM."""
    vram, ram = float(hw.get("vram_gb") or 0), float(hw.get("ram_gb") or 0)
    if hw.get("unified"):
        vram = ram * 0.7
    if vram >= 5:
        for cls in (24, 16, 12, 8, 6):
            if vram >= cls - 0.5:
                return f"gpu{cls}"
        return "gpu6"
    for cls in (32, 16, 8):
        if ram >= cls - 1:
            return f"cpu{cls}"
    return "cpu8"


def load_profile(name):
    p = core.load_json(os.path.join(PROFILES_DIR, name + ".json"))
    return p or {}


def list_profiles():
    out = []
    try:
        for f in sorted(os.listdir(PROFILES_DIR)):
            if f.endswith(".json"):
                out.append(core.load_json(os.path.join(PROFILES_DIR, f)))
    except OSError:
        pass
    return [p for p in out if p]


# ------------------------------------------------------------------------------------------------------------ the launch plan
def _arch_for(tag_or_path, show=None):
    import arch_passport
    if tag_or_path.lower().endswith(".gguf") or os.path.isfile(tag_or_path):       # a .gguf file, or an Ollama blob (no extension): read the header
        meta = arch_passport.gguf_meta(tag_or_path)
        return arch_passport.arch_from_gguf(meta) if "error" not in meta else {}
    pp = arch_passport.load(tag_or_path)
    if pp and pp.get("arch", {}).get("layers"):
        return pp["arch"]
    if show:
        import model_hub as mh
        return mh.model_arch(show)
    return {}


def _ngl(size_bytes, arch, hw, ctx, kv, slots=1, headroom_gb=0.35, desktop_gb=0.7):
    """Layers to put on the GPU so weights + KV at `ctx` fit with headroom; -> (ngl, est_gpu_gb, fits_fully).

    `slots` is the number of conversations the server keeps at once (--parallel N). Each slot needs its own key/value memory for the
    whole window, so asking for four parallel candidates costs four times the cache -- the single most common way a plan that looked
    like it fit stops fitting. It is counted here rather than discovered as a crash.
    """
    import model_hub as mh
    layers = int(arch.get("layers") or 0)
    vram = max(float(hw.get("vram_gb") or 0) - desktop_gb - headroom_gb, 0)
    if not layers or vram <= 0:
        return 0, 0.0, False
    kvt = mh.kv_bytes_per_token(arch, kv) or 0
    total = size_bytes / GB + kvt * ctx * max(1, int(slots or 1)) / GB
    per_layer = total / layers
    on = min(layers, int(vram / per_layer)) if per_layer > 0 else layers
    return on, round(on * per_layer, 2), on >= layers


MOE_SHARED = 0.12          # estimate: share of a mixture-of-experts model's weights outside its experts (attention, embeddings, router)
RAM_RESERVE_GB = 6.0       # what Windows, the desktop, a browser and this harness keep for themselves while a model runs from RAM


def _moe_split(size_bytes, arch, hw, ctx, kv, slots=1, headroom_gb=0.35, desktop_gb=0.7):
    """A mixture-of-experts model that does not fit the card: every layer goes on the GPU EXCEPT the expert weights of the first `n`
    layers, which stay in RAM (llama-server --n-cpu-moe n). Attention and the shared weights run on the card, and per word only the few
    experts in use are read from RAM, so this is far faster than splitting whole layers. -> (n, gpu_gb, ram_gb) or None (no room even
    for the shared part)."""
    import model_hub as mh
    layers = int(arch.get("layers") or 0)
    vram = max(float(hw.get("vram_gb") or 0) - desktop_gb - headroom_gb, 0)
    if not layers or vram <= 0:
        return None
    w = size_bytes / GB
    kvg = (mh.kv_bytes_per_token(arch, kv) or 0) * ctx * max(1, int(slots or 1)) / GB
    shared, per_layer = w * MOE_SHARED, w * (1 - MOE_SHARED) / layers
    room = vram - shared - kvg
    if room < 0:
        return None
    on = min(layers, int(room / per_layer)) if per_layer > 0 else layers
    return layers - on, round(shared + kvg + on * per_layer, 2), round((layers - on) * per_layer, 2)


def ram_note(tier, name, ram_part_gb, hw):
    """A plain warning when the part of a model that runs from RAM will not fit beside everything else, or "" when it will."""
    ram = float(hw.get("ram_gb") or 0)
    if not ram or ram_part_gb <= 0 or ram_part_gb <= ram - RAM_RESERVE_GB:
        return ""
    return (f"{tier}: about {round(ram_part_gb, 1)} GB of {name} would run from RAM, and this machine has {round(ram)} GB with about "
            f"{RAM_RESERVE_GB:.0f} GB taken by Windows and your apps, so it would page to disk and crawl. Pick a smaller model for this tier, "
            "or add RAM.")


_TOK = {}


def _tokenizer(gguf):
    """(tokenizer model, pre-tokenizer, vocabulary size) from a GGUF header, cached. Two models can share speculative decoding only
    when all three match: a draft must propose exactly the tokens the worker would."""
    if gguf not in _TOK:
        try:
            import arch_passport
            m = arch_passport.gguf_meta(gguf)
            t = m.get("tokenizer.ggml.tokens")
            _TOK[gguf] = (m.get("tokenizer.ggml.model"), m.get("tokenizer.ggml.pre"), t.get("n") if isinstance(t, dict) else None)
        except Exception:
            _TOK[gguf] = (None, None, None)
    return _TOK[gguf]


def pick_draft(worker, worker_arch, worker_gpu_gb, hw, cfg=None, ctx=8192, slots=1, desktop_gb=0.7):
    """The best draft model already installed for this worker, or None: the smallest model with exactly the worker's tokenizer, at
    most a third of its size, dense, that fits on the card beside it. Nothing is downloaded and nothing is assumed from names."""
    import arch_passport
    wp = float(worker_arch.get("params_b") or 0)
    want = _tokenizer(worker["gguf"])
    room = max(float(hw.get("vram_gb") or 0) - desktop_gb - 0.35 - float(worker_gpu_gb or 0), 0)
    if not wp or not want[2] or room <= 0:
        return None
    best = None
    try:
        names = [f for f in os.listdir(arch_passport.passports_dir()) if f.endswith(".json")]
    except OSError:
        names = []
    for fn in names:
        p = core.load_json(os.path.join(arch_passport.passports_dir(), fn)) or {}
        a = p.get("arch") or {}
        pb, size = float(a.get("params_b") or 0), float(p.get("size_gb") or 0)
        if not pb or a.get("experts") or pb > wp / 3 or size + 0.3 > room or p.get("name") == worker.get("name"):
            continue
        rm = resolve_model(p.get("name") or "", cfg)
        if not rm:
            continue
        import model_hub as mh                              # llama-server gives the draft its own full-window cache, at f16
        need = rm["size"] / GB + (mh.kv_bytes_per_token(a, "f16") or 0) * int(ctx or 0) * max(1, int(slots or 1)) / GB + 0.3
        if need <= room and _tokenizer(rm["gguf"]) == want and (best is None or pb < best[0]):
            best = (pb, rm)
    return best[1] if best else None


def kv_gb_per_slot(arch, ctx, kv):
    """What one extra parallel conversation costs in graphics memory, in GB. Plain arithmetic a person can check."""
    import model_hub as mh
    return round((mh.kv_bytes_per_token(arch, kv) or 0) * max(0, int(ctx or 0)) / GB, 3)


_FORCED_THINK = ("<|im_start|>assistant\\n<think>\\n", "<|im_start|>assistant\n<think>\n")      # as written in a Jinja template, and as rendered


def nothink_template(template, tier):
    """Some converted models (Ollama's qwen3 tags, for one) embed a chat template that OPENS a <think> block at the end of every prompt, so the
    model reasons no matter what the request says. For a tier that must answer directly, write a copy that closes the block at once (the
    official no-think form) and -> its path; None when the template needs no change."""
    if not template:
        return None
    patched = template
    for form in _FORCED_THINK:
        closed = form + ("\\n</think>" if "\\n" in form else "\n</think>")
        if form in patched and closed not in patched:
            patched = patched.replace(form, closed + ("\\n\\n" if "\\n" in form else "\n\n"))
    if patched == template:
        return None
    os.makedirs(server_dir(), exist_ok=True)
    path = os.path.join(server_dir(), f"{tier}-nothink.jinja")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(patched)
    return path


def resolve_model(spec, cfg=None):
    """A tier assignment (Ollama tag or .gguf path) -> {"name", "gguf", "size", "source"} or None."""
    if not spec:
        return None
    if spec.lower().endswith(".gguf"):
        return {"name": os.path.basename(spec), "gguf": spec, "size": os.path.getsize(spec) if os.path.isfile(spec) else 0, "source": "file"} if os.path.isfile(spec) else None
    a = adopt_ollama(spec)
    if a:
        return {"name": spec, "gguf": a["gguf"], "size": a["size"], "source": "ollama-blob"}
    return None


def plan(cfg=None, hw=None, caps_=None):
    """-> {"profile", "binary", "mode": "llama" | "ollama", "processes": [{tier, name, gguf, port, ctx, ngl, kv, draft, args, why}], "notes": [plain lines]}.
    A tier whose model cannot be resolved to a GGUF stays on Ollama (the transport falls back per model)."""
    cfg = cfg or core.load_config()
    hw = hw or arch_hw.detect()
    srv = cfg.get("server") or {}
    prof_name = profile_for(hw)
    prof = load_profile(prof_name)
    binary = resolve_binary(cfg) if srv.get("kind", "auto") != "ollama" else None
    notes, procs = [], []
    if srv.get("kind") == "ollama":
        notes.append("Settings > Server is set to Ollama, so every model is served by Ollama; llama-server is not started.")
        return {"profile": prof_name, "binary": None, "mode": "ollama", "processes": [], "notes": notes, "tiers": cfg.get("tiers") or {}}
    if not binary:
        notes.append("llama-server is not installed, so every model is served by Ollama. Models still never reload between calls as long as they fit together; "
                     "speculative decoding and the quantised cache need llama-server (the installer bundles it).")
        return {"profile": prof_name, "binary": None, "mode": "ollama", "processes": [], "notes": notes, "tiers": cfg.get("tiers") or {}}
    cp = caps_ or caps(binary)
    flags = set(cp.get("flags") or [])
    port0 = int(srv.get("port") or 8081)
    kv = srv.get("kv") or prof.get("kv") or "q8_0"
    tiers = dict((prof.get("tiers") or {}))
    for t, m in (cfg.get("tiers") or {}).items():
        if m:
            tiers[t] = dict(tiers.get(t) or {}, model=m)
    draft_spec = (tiers.get("draft") or {}).get("model")
    draft = resolve_model(draft_spec, cfg) if draft_spec and (srv.get("draft", "auto") != "off") else None
    perf = int(hw.get("cpu_perf_cores") or 0) or max(1, int(hw.get("cpu_cores") or 4) // 2)
    ours_up = any(r.get("pid") and _alive(r["pid"]) for r in (load_state().get("processes") or {}).values())
    # What the desktop, the driver and other apps hold on the card right now (total - free, measured by nvidia-smi), instead of a fixed
    # 0.7 GB: an external monitor, a browser or a game can take far more. Only while none of our own servers is on the card, or we would
    # count our own models as "the desktop"; then the fixed estimate stays.
    desk, vt, vf = 0.7, float(hw.get("vram_gb") or 0), hw.get("vram_free_gb")
    desk_measured = False
    if hw.get("vendor") == "nvidia" and vt and vf is not None and not ours_up:
        desk = min(max(vt - float(vf), 0.15), vt)
        desk_measured = True
        if desk > 1.0:
            notes.append(f"The desktop and other apps hold {round(desk, 1)} GB of the graphics card right now (measured): an external "
                         "monitor, a browser or a game. The models are sized for the rest; closing them frees room.")
    elif ours_up and load_state().get("desk_gb") is not None:
        desk = float(load_state()["desk_gb"])           # measured the last time none of ours was on the card (a monitor moved to the iGPU: ~0)
    threads = max(1, perf)                                   # was cpu_cores - 1: 11 threads on a 4 P-core + 4 E-core laptop, per server
    ram_gb = float(hw.get("ram_gb") or 0)
    cache_auto = 1024 if ram_gb <= 16.5 else 2048 if ram_gb <= 32.5 else 4096
    resident_ram = 0.0
    for i, tier in enumerate(("router", "worker", "heavy")):
        spec = (tiers.get(tier) or {}).get("model")
        if not spec or spec.startswith("alias:"):
            continue
        rm = resolve_model(spec, cfg)
        if not rm:
            notes.append(f"{tier}: '{spec}' is not installed here (no local GGUF file, no Ollama download of that name), so this tier is off until you pick a model that is.")
            continue
        arch = _arch_for(spec)
        if not arch.get("layers"):
            arch = _arch_for(rm["gguf"])
        ctx = int(srv.get("ctx") or (tiers.get(tier) or {}).get("ctx") or prof.get("ctx") or 8192)
        ngl_pref = (tiers.get(tier) or {}).get("ngl")
        slots = max(1, int(srv.get("parallel") or 1)) if tier == "worker" else 1      # decided BEFORE the fit math: every slot pays for the window again
        split, ram_part, beside, moe = None, 0.0, None, None
        if tier == "router" and (tiers.get(tier) or {}).get("cpu", prof.get("router_cpu", True)):
            ngl, est, full = 0, 0.0, False
            why = "runs on the CPU: a small router never competes with the worker for graphics memory"
            ram_part = rm["size"] / GB
            warn = ram_note(tier, rm["name"], ram_part, hw)
            if warn:
                notes.append(warn)
        else:
            ngl, est, full = _ngl(rm["size"], arch, hw, ctx, kv, slots=slots, desktop_gb=desk)
            if ngl_pref is not None:                                                  # a hand-set layer count: re-do the arithmetic for THAT count, so the
                ngl = max(0, int(ngl_pref))                                           # explanation and the reported GB describe what is actually launched
                layers = max(1, int(arch.get("layers") or 1))
                _, est_full, _ = _ngl(rm["size"], arch, dict(hw, vram_gb=10 ** 6), ctx, kv, slots=slots)
                est = round(est_full * min(ngl, layers) / layers, 2)
                full = ngl >= layers
            why = (f"all {ngl} layers on the GPU (about {est} GB with a {ctx}-token memory" +
                   (f", {slots} conversations at once" if slots > 1 else "") + ")" if full else
                   f"{ngl} of {arch.get('layers')} layers on the GPU (about {est} GB" +
                   (f", {slots} conversations at once" if slots > 1 else "") + "); the rest runs from RAM, slower")
            if slots > 1:
                notes.append(f"{tier}: {slots} conversations at once, so the {ctx}-token memory is held {slots} times over "
                             f"(about {kv_gb_per_slot(arch, ctx, kv)} GB each). Lower Settings > Server > Parallel if the GPU runs short.")
            moe = (not full and ngl_pref is None and arch.get("experts") and arch.get("experts_used")
                   and ("--n-cpu-moe" in flags or "-ncmoe" in flags))
            split = _moe_split(rm["size"], arch, hw, ctx, kv, slots=slots, desktop_gb=desk) if moe else None
            if split:
                ncmoe, est, ram_part = split
                ngl = int(arch.get("layers") or ngl)
                why = (f"a mixture-of-experts model: every layer on the GPU except the experts of {ncmoe} of {ngl} layers, which stay in RAM "
                       f"(about {est} GB on the card, {ram_part} GB in RAM); only the few experts in use are read per word, so it stays quick")
            elif not full:
                ram_part = max(0.0, rm["size"] / GB - est)
            # A mixture-of-experts heavy model can stay loaded BESIDE the worker: only its shared part goes on the card and its experts
            # stay in RAM. Measured on a 6 GB laptop: 14-16 tok/s with the worker loaded too (15.4 alone), and switching between them
            # takes ~1 s instead of a 15-20 s swap. Off with Settings > Server > Heavy beside worker = off.
            wp = next((p for p in procs if p["tier"] == "worker" and p.get("ngl")), None) if tier == "heavy" else None
            if (wp and moe is not None and arch.get("experts") and arch.get("experts_used") and ngl_pref is None
                    and ("--n-cpu-moe" in flags or "-ncmoe" in flags) and srv.get("heavy_beside", "auto") != "off"):
                b = _moe_split(rm["size"], arch, dict(hw, vram_gb=vt - float(wp["est_gpu_gb"] or 0)), ctx, kv, desktop_gb=desk)
                if b and b[2] + resident_ram <= ram_gb - RAM_RESERVE_GB:
                    beside = split = b
                    ncmoe, est, ram_part = b
                    ngl = int(arch.get("layers") or ngl)
                    why = (f"stays loaded beside the worker: experts of {ncmoe} of {ngl} layers in RAM (about {est} GB on the card, "
                           f"{ram_part} GB in RAM), so planning never waits for a model swap")
            warn = ram_note(tier, rm["name"], ram_part, hw)
            if warn:
                notes.append(warn)
        # llama-server's -c is the TOTAL key/value memory, shared by its slots, and a build left to choose opens 4 of them. Seen live: four
        # runs at once on an 8192 window failed with "Context size has been exceeded". So the slot count is always said, and each slot
        # gets the full window (the fit math above already pays for it `slots` times).
        par = slots if "--parallel" in flags else 1
        ngl_arg = int(arch.get("layers") or ngl) + 1 if (full or split) else ngl    # the output layer too (llama.cpp counts it as layer n+1)
        t_tier = max(1, threads // 2) if (tier == "router" and ngl == 0) else threads
        args = [binary, "--host", "127.0.0.1", "--port", str(port0 + i), "-m", rm["gguf"], "-c", str(ctx * par), "-ngl", str(ngl_arg), "--alias", tier, "-t", str(t_tier)]
        cram = "--cache-ram" if "--cache-ram" in flags else ("-cram" if "-cram" in flags else None)
        cache_mib = 0
        if cram:
            # llama-server keeps saved prompt states in RAM, up to 8 GB per server by default, and up to three servers run: on a 16 GB
            # laptop that alone could push the model files out of memory. Off for the router (short, one-off prompts).
            cache_mib = 0 if tier == "router" else int(srv.get("cache_ram") or cache_auto)
            args += [cram, str(cache_mib)]
        if tier != "heavy" or beside:
            resident_ram += ram_part + cache_mib / 1024
        if split and split[0] > 0:
            args += ["--n-cpu-moe" if "--n-cpu-moe" in flags else "-ncmoe", str(split[0])]
        if "--jinja" in flags:
            args.append("--jinja")
        think = (tiers.get(tier) or {}).get("think", tier == "heavy")              # router/worker answer directly; heavy may reason (profiles can override per tier)
        if not think and "--reasoning-budget" in flags:
            args += ["--reasoning-budget", "0"]                                   # thinking models answer straight away; a per-request switch is not honoured by every template
        elif think and "--reasoning-budget" in flags and int(srv.get("think_budget", 800) or 0) > 0:
            # Measured 2026-11-04: the MoE planner thought ~3000 tokens (2-5 minutes at its speed) before a ~200-token plan. A budget
            # keeps the reasoning that helps and stops the rest; when it runs out, the model is told to answer.
            args += ["--reasoning-budget", str(int(srv.get("think_budget", 800)))]
            if "--reasoning-budget-message" in flags:
                args += ["--reasoning-budget-message", "Enough thinking: write the answer now."]
        if not think and "--chat-template-file" in flags:
            tf = nothink_template(arch.get("template"), tier)                    # a template that forces an open <think> gets a closed one instead
            if tf:
                args += ["--chat-template-file", tf]
        if "--reasoning-format" in flags:
            args += ["--reasoning-format", "auto"]
        if srv.get("flash_attn", True) and ("--flash-attn" in flags or "-fa" in flags) and ngl > 0:
            args += ["-fa", "on"] if cp.get("fa_mode") == "value" else ["-fa"]
            if kv != "f16":                                       # only the spellings this build advertises: that is the whole point of reading --help
                if "--cache-type-k" in flags:
                    args += ["--cache-type-k", kv]
                if "--cache-type-v" in flags:
                    args += ["--cache-type-v", kv]
        if "--parallel" in flags:
            args += ["--parallel", str(par)]
        ub = "--ubatch-size" if "--ubatch-size" in flags else ("-ub" if "-ub" in flags else None)
        if ub:
            args += [ub, str(int(srv.get("ubatch") or 256))]
        if srv.get("slots", True) and "--slot-save-path" in flags and tier != "router":
            sd = os.path.join(server_dir(), "slots", tier)         # the prefill for this tier's long, unchanging prompt start, kept on disk
            try:
                os.makedirs(sd, exist_ok=True)
                args += ["--slot-save-path", sd]
            except OSError:
                pass
        if tier == "worker" and draft and "--model-draft" in flags:
            wt, dt = _tokenizer(rm["gguf"]), _tokenizer(draft["gguf"])
            if wt[2] and dt[2] and wt != dt:                    # a draft must propose exactly the worker's tokens, or it slows it down or fails
                notes.append(f"worker: the draft model {draft['name']} has a different tokenizer from {rm['name']}, so it is not used.")
                draft = None
        if tier == "worker" and not draft and "--model-draft" in flags and srv.get("draft", "auto") in ("auto", "try"):
            cand = pick_draft(rm, arch, est, hw, cfg, ctx=ctx, slots=par, desktop_gb=desk)
            if cand and srv.get("draft") == "try":
                draft = cand                                    # chosen by the person ("try"); never assumed faster
            elif cand:
                notes.append(f"worker: {cand['name']}, already installed, has exactly the worker's tokenizer and fits beside it as a draft "
                             "model (speculative decoding: often faster on code edits, not always). Settings > Server > Speculative "
                             "decoding = try uses it; compare the tokens/s in the console's status strip with and without.")
        if srv.get("ngram", "auto") != "off" and tier == "worker" and not draft:
            # Guessing the next few tokens from the prompt itself. A code edit repeats most of its input, so this is free speed with no
            # second model and no extra graphics memory -- but only where the build offers it, and never beside a real draft model.
            if "--lookup-cache-dynamic" in flags:
                try:
                    os.makedirs(os.path.join(server_dir(), "lookup"), exist_ok=True)
                    args += ["--lookup-cache-dynamic", os.path.join(server_dir(), "lookup", "worker.bin")]
                except OSError:
                    pass
            if "--spec-type" in flags:
                kinds = cp.get("spec_types") or []
                pick = next((k for k in ("ngram-cache", "ngram-mod", "ngram-simple") if k in kinds), None) if kinds else (None if "--lookup-cache-dynamic" in args else "ngram")
                if pick == "ngram-cache" and "--lookup-cache-dynamic" not in args:
                    pick = next((k for k in ("ngram-mod", "ngram-simple") if k in kinds), None)
                if pick:
                    args += ["--spec-type", pick]
        d_used = None
        if tier == "worker" and draft and "--model-draft" in flags:
            args += ["--model-draft", draft["gguf"], "-ngld", "99"]
            kinds = cp.get("spec_types") or []
            if kinds:                                           # a build that needs to be told to use the draft (default: none)
                pick = next((k for k in ("draft-simple",) + tuple(k for k in kinds if k.startswith("draft")) if k in kinds), None)
                if pick:
                    args += ["--spec-type", pick]
            if "--spec-draft-n-max" in flags:                  # the current spelling; older builds took --draft-max
                args += ["--spec-draft-n-max", "16"] + (["--spec-draft-n-min", "4"] if "--spec-draft-n-min" in flags else [])
            elif "--draft-max" in flags:
                args += ["--draft-max", "16"] + (["--draft-min", "4"] if "--draft-min" in flags else [])
            d_used = draft["name"]
        for extra in (tiers.get(tier) or {}).get("extra_args") or []:
            args.append(str(extra))
        procs.append({"tier": tier, "name": rm["name"], "gguf": rm["gguf"], "size_gb": round(rm["size"] / GB, 2), "port": port0 + i, "ctx": ctx, "ngl": ngl, "kv": kv,
                      "draft": d_used, "args": args, "why": why, "resident": tier != "heavy" or bool(beside), "arch": arch, "est_gpu_gb": est, "full_gpu": full,
                      "moe_cpu_layers": split[0] if split else 0, "ram_gb": round(ram_part, 2)})
    free = hw.get("ram_free_gb")
    budget = (float(free) - 1.5) if (free and not ours_up) else (ram_gb - RAM_RESERVE_GB)
    vm, grow = hw.get("vm") or {}, 0.0
    if vm.get("running"):
        # Docker Desktop / WSL2 is part of the person's dev setup: plan around it, never ask them to change it. What it holds now is
        # already out of "free"; what it may still claim (up to its .wslconfig limit) is kept free too.
        now_gb, cap_gb = float(vm.get("vm_gb") or 0), float(vm.get("limit_gb") or 0)
        grow = max(0.0, cap_gb - now_gb)
        budget -= grow
        notes.append(f"Docker/WSL is using {round(now_gb, 1)} GB of RAM and may grow to {round(cap_gb, 1)} GB (its limit); Architect leaves "
                     "that room free and sizes the models for the rest.")
    if procs and ram_gb and resident_ram > max(budget, 0):
        notes.append(f"The models kept loaded need about {round(resident_ram, 1)} GB of RAM together (the router on the CPU, prompt caches and "
                     "any layers that do not fit the card), and "
                     + (f"about {round(float(free), 1)} GB is free now" if (free and not ours_up) else f"about {round(ram_gb - RAM_RESERVE_GB, 1)} GB is usable")
                     + (f", {round(grow, 1)} GB of it kept for Docker/WSL to grow" if grow else "")
                     + "; Windows and open apps use the rest. Close apps you do not need (a browser often frees 1-2 GB), or use a smaller router."
                     + (" When you do not need Docker for a while, quitting Docker Desktop and running `wsl --shutdown` gives its memory back."
                        if vm.get("running") else ""))
    if not procs:
        notes.append("No tier is assigned to a local model yet: run `architect.py ready` or pick models in the hub's Team view.")
    return {"desk_gb": round(desk, 2), "desk_measured": desk_measured, "profile": prof_name, "binary": binary, "mode": "llama" if procs else "ollama", "processes": procs, "notes": notes, "caps": {"version": cp.get("version")},
            "tiers": tiers}


# ------------------------------------------------------------------------------------------------------------ processes
def health(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace") or "{}")
            return j.get("status") in ("ok", None) and r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _alive(pid):
    if not pid:
        return False
    try:
        if sys.platform.startswith("win"):
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=5)
            return str(pid) in r.stdout
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _pid_on_port(port):
    """The pid listening on 127.0.0.1:<port>, or None (netstat on Windows, ss/lsof elsewhere). Used to find a server we lost track of."""
    try:
        if sys.platform.startswith("win"):
            r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                    return int(parts[4])
        else:
            r = subprocess.run(["lsof", "-t", "-iTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True, text=True, timeout=10)
            if r.stdout.strip():
                return int(r.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _spawn(p, log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "ab")
    kw = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": os.path.dirname(p["args"][0]) or None}
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True                     # its own session: Ctrl+C or closing the terminal does not stop a resident tier
    return subprocess.Popen(p["args"], **kw)


def wait_health(url, seconds=180, say=None, proc=None):
    """True once the server answers /health; False after `seconds`, or at once when its process has already exited (a server that
    could not load its model, say, would otherwise be waited on for the full time before the run falls back)."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        if health(url):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(1.0)
    return False


def start(cfg=None, hw=None, tiers=None, say=print, wait=True, watch=True, heavy=False):
    """Start the resident tiers (router, worker; heavy only when asked). -> state dict. Idempotent: a healthy process is left alone."""
    cfg = cfg or core.load_config()
    pl = plan(cfg, hw)
    st = load_state()
    st.setdefault("processes", {})
    if pl["mode"] != "llama":
        for n in pl["notes"]:
            say("[server] " + n)
        st.update(mode="ollama", profile=pl["profile"], plan_ts=time.time())
        save_state(st)
        return st
    want = [p for p in pl["processes"] if (p["resident"] or heavy) and (not tiers or p["tier"] in tiers)]
    with _starting(say):
        st = load_state()
        if pl.get("desk_measured"):
            st["desk_gb"] = pl["desk_gb"]                                  # as it is now: another process may have started these while we waited
        st.setdefault("processes", {})
        _start_procs(cfg, want, st, say, wait)
    st = load_state()
    st.update(mode="llama", profile=pl["profile"], binary=pl["binary"], plan_ts=time.time())
    save_state(st)
    if watch:
        _watchdog(cfg)
    return st


@contextlib.contextmanager
def _starting(say=print, wait_s=300.0):
    """Only one process starts model servers at a time (the OS's own file lock, so a killed process never leaves it held). Seen in
    review: the next run's ensure() found the worker the hub's watchdog was still loading, took it for a dead one and killed it."""
    import arch_heavy
    os.makedirs(server_dir(), exist_ok=True)
    f = open(os.path.join(server_dir(), "start.lock"), "a+")
    t0, told, got = time.time(), False, False
    try:
        while not (got := arch_heavy._try_lock(f)) and time.time() - t0 < wait_s:
            if not told:
                say("[server] another program is starting a model server; waiting for it")
                told = True
            time.sleep(0.5)
        yield
    finally:
        if got:
            arch_heavy._unlock(f)
        f.close()


CUDA_LOST = ("no CUDA-capable device", "failed to initialize CUDA", "ggml_cuda_init: failed", "CUDA error: unknown error")


def _gpu_lost(log_path, offset):
    """True when the server's log, from `offset` on, says the graphics card could not be used. On laptops without a MUX switch the
    NVIDIA card can be asleep or mid-reset when a server starts; llama-server then runs everything on the CPU without failing.
    Seen live 2026-11-04: a worker planned with all 36 layers on the GPU ran on the CPU for hours after a CUDA error restart."""
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            tail = f.read().decode("utf-8", "replace")
        return any(m in tail for m in CUDA_LOST)
    except OSError:
        return False


def _log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _start_procs(cfg, want, st, say, wait):
    if any(p.get("ngl") for p in want):
        try:                                               # sized for the card, so the card must not hold another runtime's models
            import arch_ollama
            serving = {p["name"] for p in want}
            loaded = {m["name"] for m in (arch_ollama.status(timeout=2).get("loaded") or []) if m.get("name")}
            if (cfg.get("behavior") or {}).get("exclusive_gpu", "auto") == "off":
                keep = loaded - serving                    # "leave them": only Ollama's copy of what llama-server now serves goes
            else:
                keep = ({m for m in (cfg.get("roles") or {}).values() if m} | {m for m in (cfg.get("tiers") or {}).values() if m}) - serving
            if loaded - keep:
                arch_ollama.free_gpu(keep, say=say)
        except Exception:
            pass
    for p in want:
        url = f"http://127.0.0.1:{p['port']}"
        cur = st["processes"].get(p["tier"]) or {}
        if cur.get("pid") and _alive(cur["pid"]) and health(url) and cur.get("gguf") == p["gguf"] and cur.get("args") == p["args"]:
            say(f"[server] {p['tier']}: already up on port {p['port']} ({p['name']})")
            continue
        if cur.get("pid") and _alive(cur["pid"]):
            _kill(cur["pid"])
        orphan = _pid_on_port(p["port"])                                           # a server we lost track of (a crash of ours, a stale state file) must not keep the port
        if orphan and orphan != cur.get("pid"):
            say(f"[server] {p['tier']}: port {p['port']} was held by pid {orphan} (not in our records); stopping it")
            _kill(orphan)
            time.sleep(1.0)
        log_path = os.path.join(server_dir(), f"{p['tier']}.log")
        off = _log_size(log_path)
        try:
            proc = _spawn(p, log_path)
        except OSError as e:
            say(f"[server] {p['tier']}: could not start llama-server ({e})")
            continue
        say(f"[server] {p['tier']}: starting {p['name']} on port {p['port']}; {p['why']}")
        rec = {k: p[k] for k in ("tier", "name", "gguf", "port", "ctx", "ngl", "kv", "draft", "args", "size_gb", "resident")}
        rec.update(pid=proc.pid, started=time.time(), url=url, log=log_path, restarts=cur.get("restarts", 0))
        st["processes"][p["tier"]] = rec
        save_state(st)
        if wait:
            ok = wait_health(url, seconds=240, proc=proc)
            if ok and p.get("ngl") and _gpu_lost(log_path, off):
                say(f"[server] {p['tier']}: the graphics card was not available when it started (the laptop's NVIDIA card was asleep or "
                    "resetting), so it ran on the CPU; starting it again on the card")
                _kill(proc.pid)
                time.sleep(3.0)
                off = _log_size(log_path)
                proc = _spawn(p, log_path)
                rec.update(pid=proc.pid, started=time.time())
                save_state(st)
                ok = wait_health(url, seconds=240, proc=proc)
                if ok and _gpu_lost(log_path, off):
                    rec["on_cpu"] = True
                    say(f"[server] {p['tier']}: still no graphics card, so it runs on the CPU (much slower). Restarting the computer usually "
                        "brings the NVIDIA card back; Architect checks again at the next start")
            rec["healthy"] = ok
            exited = proc.poll() is not None
            say(f"[server] {p['tier']}: " + ("ready" if ok else ("stopped while loading" if exited else "did not answer /health in time")
                                              + "; see " + log_path))
            save_state(st)


def _kill(pid):
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass


def stop(tiers=None, say=print):
    st = load_state()
    gone = {t: rec for t, rec in (st.get("processes") or {}).items() if not tiers or t in tiers}
    for t in gone:
        st["processes"].pop(t, None)
    if not tiers:
        st.pop("aside", None)       # "stop the servers" means stopped: a tier that had stepped aside must not come back on its own
    if not st.get("processes") and not st.get("aside"):
        _WATCH["stop"] = True       # the watchdog is shared, so it is only silenced once nothing is left for it to watch
    save_state(st)                  # first: a watchdog reading the state between the kill and the save would start the tier again
    for t, rec in gone.items():
        if rec.get("pid") and _alive(rec["pid"]):
            _kill(rec["pid"])
            say(f"[server] {t}: stopped (pid {rec['pid']})")
        orphan = _pid_on_port(rec.get("port") or 0) if rec.get("port") else None
        if orphan and orphan != rec.get("pid"):
            _kill(orphan)
            say(f"[server] {t}: also stopped pid {orphan}, which still held port {rec['port']}")
    return st


def status(cfg=None):
    """-> {"mode", "binary", "profile", "processes": [{tier, name, port, up, pid, ctx, ngl, kv, draft}], "notes"}. Reads only; no model call."""
    cfg = cfg or core.load_config()
    st = load_state()
    out = {"mode": st.get("mode") or ("llama" if resolve_binary(cfg) else "ollama"), "binary": resolve_binary(cfg), "profile": st.get("profile"), "processes": []}
    for t, rec in (st.get("processes") or {}).items():
        up = bool(rec.get("pid")) and _alive(rec["pid"]) and health(rec.get("url") or f"http://127.0.0.1:{rec['port']}")
        out["processes"].append({"tier": t, "name": rec.get("name"), "port": rec.get("port"), "up": up, "pid": rec.get("pid"), "ctx": rec.get("ctx"), "ngl": rec.get("ngl"),
                                 "kv": rec.get("kv"), "draft": rec.get("draft"), "url": rec.get("url"), "restarts": rec.get("restarts", 0),
                                 "on_cpu": bool(rec.get("on_cpu"))})
    return out


def serving(model, cfg=None):
    """-> {"url", "ctx", "tier"} when a running llama-server process serves this model name (tier alias, Ollama tag, GGUF path or file name)."""
    st = load_state()
    if not model or not st.get("processes"):
        return None
    m = str(model)
    for t, rec in st["processes"].items():
        names = {t, rec.get("name"), rec.get("gguf"), os.path.basename(rec.get("gguf") or ""), f"tier:{t}"}
        if m in names and rec.get("pid") and _alive(rec["pid"]):
            return {"url": rec.get("url") or f"http://127.0.0.1:{rec['port']}", "ctx": rec.get("ctx"), "tier": t}
    return None


def slot_io(action, cfg, project, tier="worker"):
    """Save or restore the worker's prompt memory for one project (llama-server --slot-save-path): reopening a project then skips
    re-reading its context. Best effort: any failure (older build, slot busy, file missing) is silently nothing. -> True/False."""
    try:
        sv = serving(f"tier:{tier}", cfg)
        if not sv or action not in ("save", "restore") or busy(sv["url"]):
            return False
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(project))[:60] + ".bin"
        if action == "restore" and not os.path.isfile(os.path.join(server_dir(), "slots", tier, name)):
            return False
        req = urllib.request.Request(sv["url"].rstrip("/") + f"/slots/0?action={action}", data=json.dumps({"filename": name}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def busy(url, timeout=2.0):
    """True when the server at `url` is answering a request right now (a slot is processing), False when idle, None when it cannot
    tell (an older build, or /slots switched off). Asked before a tier steps aside, so another program's answer is never cut off."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/slots", timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace") or "[]")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(j, list):
        return None
    return any(isinstance(s, dict) and bool(s.get("is_processing")) for s in j)


ROOM_OVERHEAD_GB = 0.6          # what a model needs on the card beyond its file: the conversation memory and CUDA's working space


def _ollama_loaded():
    try:
        import arch_ollama
        return arch_ollama.status(timeout=2).get("loaded") or []
    except Exception:
        return []


def step_aside_for(model, cfg=None, live=None, busy_=None, held_=None):
    """Should our tiers on the graphics card stop so `model` gets the card? It is a model no llama-server here serves, so Ollama loads it
    into whatever memory is left. Yes only when it does not fit now, WOULD fit whole once they stop, and none of them is answering a
    request. -> {"tiers": [...], "names": {tier: [names it answers to]}, "need_gb", "free_gb", "why"}; "tiers" is empty when nothing
    should move, and "why" then says why not."""
    out = {"tiers": [], "names": {}, "need_gb": None, "free_gb": None, "why": "", "reload": False}
    if not model or serving(model, cfg):
        out["why"] = "a llama-server here already serves it"
        return out
    on_card = [(t, rec) for t, rec in (load_state().get("processes") or {}).items()
               if rec.get("ngl") and rec.get("pid") and _alive(rec["pid"])]
    if not on_card:
        out["why"] = "none of our servers holds the card"
        return out
    rm = resolve_model(model, cfg) or {}
    if not rm.get("size"):
        out["why"] = "its size is not known here"
        return out
    out["need_gb"] = need = round(rm["size"] / GB + ROOM_OVERHEAD_GB, 2)
    lv = live if live is not None else arch_hw.live()
    if lv.get("vram_gb") is None or lv.get("vram_used_gb") is None:
        out["why"] = "the graphics memory cannot be read"
        return out
    out["free_gb"] = free = round(float(lv["vram_gb"]) - float(lv["vram_used_gb"]), 2)
    held = next((m for m in (held_ if held_ is not None else _ollama_loaded()) if m.get("name") == model), None)
    if held and held.get("size") and (held.get("size_vram") or 0) >= 0.95 * held["size"]:
        out["why"] = "Ollama already holds it on the card"
        return out
    if held:                                                  # its own share of the card is freed when it is loaded again
        free = round(free + (held.get("size_vram") or 0) / GB, 2)
    if need <= free:
        out["why"] = f"it fits in the {free} GB already free"
        out["reload"] = bool(held)                            # a copy split across card and RAM is loaded again, whole this time
        return out
    tiers = (cfg or {}).get("tiers") or {}
    freed = round(sum(float(rec.get("size_gb") or 0) * 1.15 + 0.3 for _, rec in on_card), 2)   # its file + conversation memory, roughly
    if need > free + freed:
        # Measured on the 4050 laptop (2026-11-03): the 14B wrote 2.45 tok/s beside the worker, 1.9 with the worker stopped (Ollama, 39%
        # on the card) and 2.3-2.7 on our own server with 21 of 40 layers on a full card. The half left in RAM sets the speed, so a model
        # that still does not fit gains nothing and the swap costs ~25 s. Only a model that then fits whole gets the card.
        out["why"] = (f"it would still run mostly from RAM (it needs about {need} GB; the card would have about {round(free + freed, 1)} GB), "
                      "and that measured no faster")
        return out
    check = busy_ or busy
    for t, rec in on_card:
        b = check(rec.get("url") or f"http://127.0.0.1:{rec.get('port')}")
        if b is not False:                                    # None = cannot tell: another program's answer must never be cut off
            out["why"] = f"the {t} is answering another request" if b else f"it cannot be told whether the {t} is answering another request"
            return out
    for t, rec in on_card:
        out["tiers"].append(t)
        out["names"][t] = sorted({t, "tier:" + t, rec.get("name"), rec.get("gguf"), os.path.basename(rec.get("gguf") or ""), tiers.get(t)} - {None, ""})
    out["why"] = f"it needs about {need} GB, {free} GB is free and it fits once the {' and '.join(out['tiers'])} steps aside"
    return out


def step_aside(tiers, model, names, owner=None, run=None, say=print):
    """Stop `tiers` so `model` gets the graphics card, and write down who did it in the servers' state file. If that process ends
    without bringing them back (the Stop button, Ctrl+C, a crash, a supervisor time limit), the next ensure() or the watchdog does."""
    st = load_state()
    aside = st.setdefault("aside", {})
    for t in tiers:
        aside[t] = {"for": model, "owner": owner or os.getpid(), "run": run or "", "names": list((names or {}).get(t) or []), "t": time.time()}
    save_state(st)
    return stop(tiers, say=say)


def _orphaned(a):
    return not (a.get("owner") and _alive(a["owner"]))


def come_back(cfg, say=print, owner=None, run=None, wait_s=15.0):
    """Bring back the tiers that stepped aside: those of `owner` (and `run`), or, with no owner, those whose process has ended. The
    models they made room for leave Ollama first, and we wait until Ollama has let go of them, so the tiers get the card they are
    sized for. -> the tiers up again."""
    aside = load_state().get("aside") or {}
    mine = {t: a for t, a in aside.items()
            if ((a.get("owner") == owner and (run is None or a.get("run") == run)) if owner else _orphaned(a))}
    if not mine:
        return []
    bigs = sorted({a["for"] for a in mine.values() if a.get("for")})
    try:
        import arch_ollama
        for b in bigs:
            arch_ollama.unload(b)
        t0 = time.time()
        while bigs and time.time() - t0 < wait_s:
            if not {m.get("name") for m in (arch_ollama.status(timeout=2).get("loaded") or [])} & set(bigs):
                break
            time.sleep(0.5)
    except Exception:
        pass
    st = load_state()
    for t in mine:
        (st.get("aside") or {}).pop(t, None)
    save_state(st)
    wanted = set().union(*(set(a.get("names") or []) for a in mine.values()))
    ensure(cfg, say=say, wanted=wanted or None)
    return [p["tier"] for p in status(cfg)["processes"] if p["up"] and p["tier"] in mine]


def models():
    """Names the running llama-server processes answer to (for the installed-models list)."""
    st = load_state()
    out = []
    for t, rec in (st.get("processes") or {}).items():
        if rec.get("pid") and _alive(rec["pid"]):
            out.append(rec.get("name") or t)
    return out


def ensure(cfg, say=print, wanted=None, watch=False):
    """Before a run: start the planned resident tiers if llama-server is configured and not up. Never raises; falls back to Ollama.
    `wanted` = the models the run will actually call (None = not known): a tier none of them uses is not started. Seen live: the scripted
    benchmark, whose roles all point at a fake model, started the real router and worker and left 3.7 GB of graphics memory held idle."""
    try:
        pl = plan(cfg)
        if pl["mode"] != "llama":
            return {"mode": "ollama"}
        if any(_orphaned(a) for a in (load_state().get("aside") or {}).values()):
            say("[server] a model server that stepped aside for a bigger model was never brought back (its run ended); bringing it back")
            come_back(cfg, say=say)
        if watch:
            _watchdog(cfg)
        st = status(cfg)
        up = {p["tier"] for p in st["processes"] if p["up"]}
        def names(p):
            return {p["tier"], "tier:" + p["tier"], p.get("name"), p.get("gguf"), os.path.basename(p.get("gguf") or ""),
                    ((pl.get("tiers") or {}).get(p["tier"]) or {}).get("model")} - {None, ""}
        need = [p["tier"] for p in pl["processes"] if p["resident"] and p["tier"] not in up
                and (wanted is None or names(p) & set(wanted))]
        if need:
            return start(cfg, tiers=need, say=say)
        return st
    except Exception as e:                                   # the server layer must never stop a run
        say(f"[server] {type(e).__name__}: {e}; using Ollama for this run")
        return {"mode": "ollama", "error": str(e)}


def _watchdog(cfg, period=15.0, max_restarts=3, window=600.0):
    if _WATCH.get("thread") and _WATCH["thread"].is_alive():
        _WATCH["stop"] = False
        return
    _WATCH["stop"] = False

    def loop():
        hist = {}
        while not _WATCH.get("stop"):
            time.sleep(period)
            try:
                if any(_orphaned(a) for a in (load_state().get("aside") or {}).values()):
                    come_back(core.load_config(), say=lambda m: None)
            except Exception:
                pass
            st = load_state()
            for t, rec in list((st.get("processes") or {}).items()):
                if rec.get("pid") and _alive(rec["pid"]):
                    continue
                if ((load_state().get("processes") or {}).get(t) or {}).get("pid") != rec.get("pid"):
                    continue                     # stopped on purpose (or started again elsewhere) since this loop read the state
                now = time.time()
                hist[t] = [x for x in hist.get(t, []) if now - x < window]

                def keep(rec=rec, t=t):          # into the state as it is NOW: the copy read above may hold tiers stopped since
                    fresh = load_state()
                    fresh.setdefault("processes", {})[t] = rec
                    save_state(fresh)
                if len(hist[t]) >= max_restarts:
                    rec["dead"] = f"crashed {max_restarts} times in {int(window / 60)} minutes; not restarted. See {rec.get('log')}"
                    keep()
                    continue
                hist[t].append(now)
                try:
                    lp = rec.get("log") or os.path.join(server_dir(), f"{t}.log")
                    off = _log_size(lp)
                    proc = _spawn(rec, lp)
                    rec.update(pid=proc.pid, started=now, restarts=rec.get("restarts", 0) + 1)
                    keep()
                    if rec.get("ngl") and wait_health(rec.get("url") or "", seconds=120, proc=proc) and _gpu_lost(lp, off):
                        _kill(proc.pid)                  # restarted after a CUDA crash but the card was not back yet: try once more
                        time.sleep(5.0)
                        off = _log_size(lp)
                        proc = _spawn(rec, lp)
                        rec.update(pid=proc.pid, started=time.time())
                        rec["on_cpu"] = bool(wait_health(rec.get("url") or "", seconds=120, proc=proc) and _gpu_lost(lp, off))
                        keep()
                except OSError as e:
                    rec["dead"] = str(e)
                    keep()
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    _WATCH["thread"] = th


# ------------------------------------------------------------------------------------------------------------ measuring variants (tune)
def bench_variants(cfg, hw, tier="worker", variants=None, say=print, n_tokens=160):
    """Restart the tier with each variant (kv type, draft on/off, ubatch, ctx, quant file) and measure one fixed code prompt.
    -> [{"variant", "gen_tok_s", "prefill_tok_s", "vram_used_gb", "healthy", "error"}]. Keeps nothing running at the end unless `start()` is called again."""
    import arch_transport
    base = plan(cfg, hw)
    proc = next((p for p in base["processes"] if p["tier"] == tier), None)
    if not proc:
        return []
    variants = variants or [{"kv": "q8_0", "draft": True}, {"kv": "q8_0", "draft": False}, {"kv": "f16", "draft": False}]
    prompt = "Write a Python function `parse_duration(text)` that turns strings like '1h30m', '45s', '2h' into seconds, with docstring and three doctests.\n" * 6
    out = []
    for v in variants:
        c2 = core.deep_merge(cfg, {"server": {"kv": v.get("kv", "q8_0"), "draft": "try" if v.get("draft", True) else "off",
                                              "ubatch": v.get("ubatch", 256), "parallel": int(v.get("parallel") or cfg.get("server", {}).get("parallel") or 1)}})
        if v.get("ctx"):
            c2 = core.deep_merge(c2, {"server": {"ctx": int(v["ctx"])}})
        stop([tier], say=lambda m: None)
        st = start(c2, hw, tiers=[tier], say=lambda m: None, watch=False)
        rec = (st.get("processes") or {}).get(tier) or {}
        row = {"variant": v, "healthy": bool(rec.get("healthy")), "gen_tok_s": None, "prefill_tok_s": None, "vram_used_gb": None, "error": None}
        if rec.get("healthy"):
            try:
                tr = arch_transport.LlamaTransport(rec["url"])
                r = tr.chat(tier, [{"role": "user", "content": prompt}], {"num_predict": n_tokens, "temperature": 0}, False, None, {"first": 300, "idle": 120, "total": 600})
                row.update(gen_tok_s=r.get("tok_s"), prefill_tok_s=r.get("prefill_tok_s"))
                g = arch_hw.detect()
                row["vram_used_gb"] = round((g.get("vram_gb") or 0) - (g.get("vram_free_gb") or 0), 2)
            except Exception as e:
                row["error"] = str(e)[:200]
        else:
            row["error"] = "did not start"
        say(f"  variant {json.dumps(v)}: " + (f"{row['gen_tok_s']} tok/s, {row['vram_used_gb']} GB used" if row["gen_tok_s"] else f"failed ({row['error']})"))
        out.append(row)
    stop([tier], say=lambda m: None)
    return out


# ------------------------------------------------------------------------------------------------------------ one-time download of the server program
RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"


def fetch_binary(confirm=False, say=print, dest=None, cuda=None, opener=None):
    """Download llama.cpp's llama-server for this machine into <HOME>/bin/llama/ -- the one-time download that the offline rule allows,
    and only after an explicit yes (confirm=True). Picks the CUDA build (plus its runtime zip) when an NVIDIA card is present, else the
    CPU build. -> {"ok", "binary", "files", "error"}. Nothing else ever reaches the network."""
    import io
    import zipfile
    import urllib.request as ur
    if not confirm:
        return {"ok": False, "error": "a download needs an explicit yes (--yes or the hub's confirm button); nothing was fetched"}
    hw = arch_hw.detect()
    want_cuda = bool(cuda) if cuda is not None else (hw.get("vendor") == "nvidia")
    dest = dest or os.path.join(core.HOME, "bin", "llama")
    opener = opener or (lambda url: ur.urlopen(ur.Request(url, headers={"User-Agent": "architect-local/1", "Accept": "application/vnd.github+json"}), timeout=60))
    try:
        with opener(RELEASES) as r:
            rels = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": f"could not read the llama.cpp release list ({e}); download llama-server by hand into {dest}"}
    rel = next((x for x in (rels if isinstance(rels, list) else [rels]) if any("llama-server" in a.get("name", "") or a.get("name", "").startswith("llama-b") for a in x.get("assets", []))), None)
    if not rel:
        return {"ok": False, "error": "no llama.cpp release with binaries was found in the last ten; download llama-server by hand into " + dest}
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", []) if a.get("name") and a.get("browser_download_url")}
    plat = "win" if sys.platform.startswith("win") else ("macos" if sys.platform == "darwin" else "ubuntu")
    picks = []
    if want_cuda and plat == "win":
        cuda_zip = sorted([n for n in assets if n.startswith("llama-") and "bin-win-cuda-12" in n and n.endswith("-x64.zip")], reverse=True)
        cudart = sorted([n for n in assets if n.startswith("cudart-llama-bin-win-cuda-12") and n.endswith("-x64.zip")], reverse=True)
        if cuda_zip:
            picks.append(cuda_zip[0])
            if cudart:
                picks.append(cudart[0])
    if not picks:
        arch_ = "arm64" if "arm" in (os.uname().machine.lower() if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()) else "x64"
        cpu = sorted([n for n in assets if n.startswith("llama-") and f"bin-{plat}" in n and ("cpu" in n or plat == "macos") and arch_ in n and n.endswith(".zip")], reverse=True)
        if cpu:
            picks.append(cpu[0])
    if not picks:
        return {"ok": False, "error": f"no llama-server build for this platform in release {rel.get('tag_name')}: {', '.join(sorted(assets))[:300]}"}
    os.makedirs(dest, exist_ok=True)
    files = []
    for name in picks:
        say(f"[server] downloading {name} ({rel.get('tag_name')})...")
        try:
            with opener(assets[name]) as r:
                data = r.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(dest)
                files += z.namelist()
        except Exception as e:
            return {"ok": False, "error": f"download of {name} failed: {e}", "files": files}
    exe = "llama-server.exe" if plat == "win" else "llama-server"
    found = None
    for dp, dn, fn in os.walk(dest):
        if exe in fn:
            found = os.path.join(dp, exe)
            break
    if found and not sys.platform.startswith("win"):
        os.chmod(found, 0o755)
    with open(os.path.join(dest, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(f"{rel.get('tag_name')}\n" + "\n".join(picks) + "\n")
    say(f"[server] installed {rel.get('tag_name')} -> {found or dest}")
    return {"ok": bool(found), "binary": found, "files": files, "version": rel.get("tag_name"), "error": None if found else "the archive had no llama-server"}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="the resident model server")
    ap.add_argument("cmd", choices=["start", "stop", "status", "plan", "fetch"])
    ap.add_argument("--heavy", action="store_true", help="also start the heavy tier")
    ap.add_argument("--yes", action="store_true", help="fetch: agree to the one-time download of llama-server (the only time this command goes online)")
    a = ap.parse_args(argv)
    cfg = core.load_config()
    if a.cmd == "fetch":
        if not a.yes:
            print("This downloads llama.cpp's llama-server (one time, from github.com/ggml-org/llama.cpp releases) into bin/llama/. Add --yes to agree.")
            return 1
        out = fetch_binary(confirm=True)
        print(out.get("error") or f"ok: {out['binary']}")
        return 0 if out.get("ok") else 1
    if a.cmd == "plan":
        pl = plan(cfg)
        print(f"machine class: {pl['profile']}; server: {pl['mode']}" + (f" ({pl['binary']})" if pl.get("binary") else ""))
        for p in pl["processes"]:
            print(f"  {p['tier']:7} {p['name']}  port {p['port']}  ctx {p['ctx']}  KV {p['kv']}  {p['why']}" + (f"; draft {p['draft']}" if p.get("draft") else ""))
        for n in pl["notes"]:
            print("  note: " + n)
        return 0
    if a.cmd == "start":
        start(cfg, heavy=a.heavy)
        return 0
    if a.cmd == "stop":
        stop()
        return 0
    st = status(cfg)
    print(f"server: {st['mode']}" + (f" ({st['binary']})" if st.get("binary") else " (llama-server not installed; Ollama serves every model)"))
    for p in st["processes"]:
        print(f"  {p['tier']:7} {p['name']}  port {p['port']}  {'up' if p['up'] else 'DOWN'}  ctx {p['ctx']} ngl {p['ngl']} KV {p['kv']}" + (f"  draft {p['draft']}" if p.get("draft") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
