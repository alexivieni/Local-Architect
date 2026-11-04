"""Model passports: what is KNOWN and what was MEASURED about every model you have, public or niche, so the harness adapts to the
model instead of assuming a family.

    passport(name) -> {
      "name", "source": "ollama" | "gguf", "path", "size_gb", "quant", "family", "arch": {layers, kv_heads, head_dim, ctx_max, experts, experts_used, params_b},
      "active_b", "eff_b" (dense-equivalent), "moe", "chat_template" (name hint), "think_style": "tags" | "field" | None,
      "measured": {gen_tok_s, prefill_tok_s, load_s, cpt, gpu_pct, canary_ok, ctx},          # from tune / real calls (arch_tune stats + profile)
      "abilities": {schema_json: bool|None, edit_style: patch|whole|None, acts: bool|None, tool_calls: int|None},   # from the ready probe
      "notes": [plain-words lines], "updated"}

Known facts come from Ollama's /api/show when the model is an Ollama tag, or from the GGUF header itself (read with the standard library:
no llama.cpp needed) when it is a file. Measured facts are copied from hub_data/model_stats.json and hub_data/profile.json so nothing is
measured twice. A passport is a cache in hub_data/passports/<safe name>.json; `refresh` rebuilds it.

The plain-words rule: every note is one sentence a non-technical reader understands first, with the numbers after it.
"""
import json
import os
import re
import struct
import time

import arch_core as core

GB = 1024 ** 3
_GGUF_MAGIC = b"GGUF"
_TYPES = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
_FILE_TYPE = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S",
              15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S",
              25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 38: "MXFP4"}


def passports_dir():
    return os.path.join(core.data_dir(), "passports")


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def path_for(name):
    return os.path.join(passports_dir(), safe_name(name) + ".json")


# ------------------------------------------------------------------------------------------------------------ GGUF header (stdlib)
def gguf_meta(path, max_kv=400):
    """Read the key/value header of a GGUF file (v2/v3) without loading tensors. -> {key: value} for scalar/string keys (arrays summarised),
    or {"error"}. Only the metadata is read; the file is never mapped whole."""
    out = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != _GGUF_MAGIC:
                return {"error": "not a GGUF file"}
            version = struct.unpack("<I", f.read(4))[0]
            if version < 2:
                return {"error": f"GGUF v{version} is too old"}
            n_tensors, n_kv = struct.unpack("<QQ", f.read(16))

            def rd_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", "replace")

            def rd_val(t):
                if t == 8:
                    return rd_str()
                if t == 9:
                    et, n = struct.unpack("<IQ", f.read(12))
                    if et == 8:
                        vals = [rd_str() for _ in range(min(n, 8))]
                        for _ in range(max(0, n - 8)):
                            rd_str()
                        return {"array": "string", "n": n, "head": vals}
                    fmt = _TYPES.get(et)
                    if not fmt:
                        return {"array": "?", "n": n}
                    size = struct.calcsize("<" + fmt)
                    head = [struct.unpack("<" + fmt, f.read(size))[0] for _ in range(min(n, 8))]
                    f.seek(size * max(0, n - 8), 1)
                    return {"array": fmt, "n": n, "head": head}
                fmt = _TYPES.get(t)
                if not fmt:
                    raise ValueError(f"unknown GGUF value type {t}")
                return struct.unpack("<" + fmt, f.read(struct.calcsize("<" + fmt)))[0]
            for _ in range(min(n_kv, max_kv)):
                key = rd_str()
                t = struct.unpack("<I", f.read(4))[0]
                out[key] = rd_val(t)
            out["_tensors"] = n_tensors
            out["_version"] = version
    except (OSError, struct.error, ValueError) as e:
        return {"error": f"could not read the GGUF header: {e}"}
    return out


def arch_from_gguf(meta):
    a = meta.get("general.architecture") or ""
    g = lambda k, d=None: meta.get(f"{a}.{k}", d)
    heads, kvh = g("attention.head_count"), g("attention.head_count_kv")
    if isinstance(heads, dict):
        heads = max(heads.get("head") or [0])
    if isinstance(kvh, dict):
        kvh = max(kvh.get("head") or [0])
    kvh = kvh or heads
    emb = g("embedding_length")
    hd = g("attention.key_length") or (emb // heads if emb and heads else None)
    pc = meta.get("general.parameter_count")
    if not pc and meta.get("general.size_label"):
        import re as _re
        m = _re.search(r"(\d+(?:\.\d+)?)\s*[bB]", str(meta["general.size_label"]))
        pc = float(m.group(1)) * 1e9 if m else None
    return {"arch": a, "layers": g("block_count"), "kv_heads": kvh, "head_dim": hd, "ctx_max": g("context_length"), "experts": g("expert_count"),
            "experts_used": g("expert_used_count"), "params": pc, "params_b": round(pc / 1e9, 2) if pc else None,
            "quant": _FILE_TYPE.get(meta.get("general.file_type"), None), "name": meta.get("general.name"), "template": meta.get("tokenizer.chat_template")}


# ------------------------------------------------------------------------------------------------------------ building one passport
def _think_style(template, name):
    t = (template or "") + " " + (name or "")
    if "<think>" in t or "enable_thinking" in t:
        return "tags"
    if re.search(r"deepseek-r1|qwq|reasoning", name or "", re.I):
        return "tags"
    return None


def _notes(p):
    n = []
    eff = p.get("eff_b")
    if p.get("moe"):
        n.append(f"A mixture-of-experts model: it only runs part of itself per word, so it is fast for its size but needs room for all of it (about {p.get('size_gb')} GB). "
                 f"Counts as roughly a {eff}B ordinary model.")
    elif eff:
        n.append(f"A {eff}B model: bigger usually means better answers and slower replies.")
    q = p.get("quant")
    if q:
        bits = {"Q8_0": "8-bit, closest to the original", "Q6_K": "6-bit, near-original", "Q5_K_M": "5-bit, a good balance", "Q4_K_M": "4-bit, the usual choice for a small GPU",
                "Q4_K_S": "4-bit, a little smaller and a little less accurate", "IQ4_XS": "4-bit, compact; slightly less accurate", "Q3_K_M": "3-bit, small but noticeably less accurate",
                "Q2_K": "2-bit, very small, often too lossy for code", "F16": "full precision, large and slow here", "MXFP4": "4-bit float, compact and accurate"}.get(q.upper())
        if bits:
            n.append(f"Stored at {q}: {bits}.")
    if p.get("think_style"):
        n.append("A thinking model: it reasons before answering, which helps planning and costs time on simple edits.")
    m = p.get("measured") or {}
    if m.get("gen_tok_s"):
        n.append(f"Measured here: about {m['gen_tok_s']} words-worth of tokens per second" + (f", {m['gpu_pct']}% of it on the graphics card" if m.get("gpu_pct") is not None else "") + ".")
    a = p.get("abilities") or {}
    if a.get("schema_json") is False:
        n.append("Could not follow a strict JSON shape in the probe: the harness asks it for plain JSON and checks it in code instead.")
    if a.get("edit_style"):
        n.append({"whole": "Edits files best by rewriting the whole file (small files).", "patch": "Edits files reliably with search-and-replace blocks."}.get(a["edit_style"], ""))
    return [x for x in n if x]


def build(name, show=None, gguf=None, stats=None, profile=None, results=None):
    """Assemble a passport from what is known. `show` = Ollama /api/show dict; `gguf` = path to a .gguf file; the rest are the hub's measured stores."""
    import arch_profiles
    p = {"name": name, "source": "gguf" if gguf else "ollama", "path": gguf, "updated": time.time(), "notes": []}
    arch = {}
    if gguf:
        meta = gguf_meta(gguf)
        if "error" in meta:
            p["error"] = meta["error"]
        else:
            arch = arch_from_gguf(meta)
            p["size_gb"] = round(os.path.getsize(gguf) / GB, 2) if os.path.isfile(gguf) else None
            p["quant"] = arch.get("quant")
            p["family"] = (arch.get("arch") or "").lower() or None
            p["chat_template"] = "embedded" if arch.get("template") else None
            p["think_style"] = _think_style(arch.get("template"), name)
    elif show:
        import model_hub as mh
        arch = mh.model_arch(show)
        det = show.get("details") or {}
        p["quant"] = det.get("quantization_level")
        p["family"] = det.get("family")
        p["size_gb"] = None
        tpl = show.get("template") or ""
        p["chat_template"] = "ollama" if tpl else None
        p["think_style"] = _think_style(tpl, name)
        caps = show.get("capabilities") or []
        p["capabilities"] = caps
        if arch.get("params"):
            arch["params_b"] = round(arch["params"] / 1e9, 2)
    p["arch"] = {k: arch.get(k) for k in ("arch", "layers", "kv_heads", "head_dim", "ctx_max", "experts", "experts_used", "params_b")}
    total_b = arch.get("params_b") or arch_profiles.params_b(name)
    active_b = arch_profiles.active_from_arch(total_b, arch) if total_b else None
    p["active_b"] = active_b
    p["moe"] = bool(active_b)
    p["eff_b"] = arch_profiles.effective_b(name, total_b, active_b)
    st = (stats or {}).get(name) or {}
    prof = ((profile or {}).get("models") or {}).get(name) or {}
    meas = prof.get("measured") or {}
    p["measured"] = {"gen_tok_s": st.get("gen_tok_s") or meas.get("gen_tok_s"), "prefill_tok_s": st.get("prefill_tok_s") or meas.get("prefill_tok_s"),
                     "load_s": st.get("load_s") if st.get("load_s") is not None else meas.get("load_s"), "cpt": st.get("cpt"), "gpu_pct": meas.get("gpu_pct"),
                     "canary_ok": meas.get("canary_ok"), "ctx": prof.get("ctx"), "calls": st.get("calls", 0)}
    modes = prof.get("modes") or {}
    p["abilities"] = {"schema_json": modes.get("json"), "edit_style": modes.get("recommend_edit_style"), "acts": modes.get("acts"),
                      "tool_calls": _score(results, name, "toolcall"), "format": _score(results, name, "format")}
    p["notes"] = _notes(p)
    return p


def _score(results, name, cat):
    try:
        import model_hub as mh
        agg = mh.aggregate(results or mh.load_results(), name)
        v = (agg or {}).get(cat)
        return v.get("score") if isinstance(v, dict) else None
    except Exception:
        return None


def save(p):
    os.makedirs(passports_dir(), exist_ok=True)
    tmp = path_for(p["name"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)
    os.replace(tmp, path_for(p["name"]))
    return path_for(p["name"])


def load(name):
    return core.load_json(path_for(name)) or None


def refresh(names=None, say=print):
    """Rebuild passports for every installed Ollama model (and any GGUF the server plan names). -> {name: passport}. Never raises."""
    import arch_tune
    out = {}
    stats = arch_tune.load_stats()
    profile = core.load_json(os.path.join(core.data_dir(), "profile.json"))
    import arch_server
    try:
        import model_hub as mh
        mh.ollama_host = core.ollama_host
        tags = mh.oll("/api/tags", timeout=10).get("models", [])
        up = True
    except Exception:
        tags, up = [], False
    if not tags:                                                 # Ollama not running: its manifests on disk still say what is installed
        tags = [{"name": t} for t in arch_server.local_tags()]
    for m in tags:
        name = m.get("name") or m.get("model")
        if names and name not in names:
            continue
        show = {}
        if up:
            try:
                show = mh.oll("/api/show", {"model": name}, timeout=15)
            except Exception:
                show = {}
        blob = arch_server.adopt_ollama(name)
        if show:
            p = build(name, show=show, stats=stats, profile=profile)
            p["size_gb"] = round((m.get("size") or 0) / GB, 2) if m.get("size") else (round(blob["size"] / GB, 2) if blob else None)
        elif blob:                                               # read the GGUF header straight from the blob
            p = build(name, gguf=blob["gguf"], stats=stats, profile=profile)
            p["source"] = "ollama"
            p["path"] = blob["gguf"]
        else:
            continue
        save(p)
        out[name] = p
    try:
        import arch_server
        for g in arch_server.gguf_models(core.load_config()):
            if names and g["name"] not in names:
                continue
            p = build(g["name"], gguf=g["path"], stats=stats, profile=profile)
            save(p)
            out[g["name"]] = p
    except Exception:
        pass
    say(f"[passport] {len(out)} model(s) described")
    return out


def summary_line(p):
    """One plain line for the hub and `status`."""
    m = p.get("measured") or {}
    bits = [f"{p.get('eff_b') or '?'}B", p.get("quant") or "?"]
    if p.get("moe"):
        bits.append("MoE")
    if m.get("gen_tok_s"):
        bits.append(f"{m['gen_tok_s']} tok/s")
    if m.get("gpu_pct") is not None:
        bits.append(f"{m['gpu_pct']}% GPU")
    return f"{p['name']}: " + ", ".join(bits)
