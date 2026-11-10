"""Per-model optimisation for the models you actually have installed.

`tune` measures the machine and sets context size and timeouts. This module covers the rest of what matters for a local agent loop:

  * SAMPLING per model family. Without it every model runs at the role's default temperature (0.2), which the vendors say is wrong for
    several families (Qwen3 thinking mode must not run greedy; Gemma wants about 1.0). The values come from `model_hub.KNOWLEDGE`.
  * SERVER SETTINGS that Ollama reads from its environment (flash attention, KV-cache type, parallel slots, loaded-model limit). They cannot be
    written from here, so the plan gives the exact lines to set; it never touches the server.
  * MoE ADVICE: when a mixture-of-experts model does not fit the GPU, how to keep the speed (this is where llama.cpp beats Ollama).
  * FRAMEWORK / HARNESS notes, each marked with where the statement comes from (vendor / measured / inference).

Nothing here deletes, hides, swaps or reassigns a model. Apply writes only the sampling keys that are EMPTY for that model, and records
what it wrote so `undo` puts it back. A value you set yourself is never overwritten.
"""
import json
import os
import time

import arch_autotune as at
import arch_core as core
import arch_profiles
import model_hub as mh

SAMPLING_KEYS = ("temperature", "top_p", "top_k", "repeat_penalty")
# vendor guidance is for the model's own default mode; agent loops need exact tool-call and edit syntax, so cap randomness for the
# families whose vendor value is high AND which are not reasoning models (their thinking mode needs the vendor value).
EDIT_TEMP_CAP = 1.0


def undo_path():
    return os.path.join(core.data_dir(), "modelopt.json")


def _load():
    return core.load_json(undo_path()) or {}


def _save(d):
    os.makedirs(os.path.dirname(undo_path()), exist_ok=True)
    with open(undo_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def _override_for(cfg, name):
    return (cfg.get("model_overrides") or {}).get(name) or {}


def sampling_for(name):
    """-> (family record, sampling dict or None). Looks the model up by name, so merges and abliterated builds (huihui_ai/qwen3-coder-abliterated:30b)
    get their base family's guidance."""
    k = mh.knowledge_for(name)
    s = k.get("sampling")
    return k, (dict(s) if s else None)


# Approximate bits per weight of common GGUF quantisations (estimates from the llama.cpp quantisation tables; real files vary a little).
BPW = {"q2_k": 3.0, "q3_k_m": 3.9, "q4_0": 4.5, "q4_k_s": 4.6, "q4_k_m": 4.85, "q5_k_s": 5.5, "q5_k_m": 5.7, "q6_k": 6.6, "q8_0": 8.5, "f16": 16.0}
_ORDER = ["q2_k", "q3_k_m", "q4_k_m", "q5_k_m", "q6_k", "q8_0"]


def quant_key(q):
    """'Q4_K_M' / 'q4_K_M' / 'Q4_0' -> a BPW key, or None when unknown."""
    k = str(q or "").strip().lower().replace("-", "_")
    return k if k in BPW else None


def quant_advice(o, hw):
    """Suggest a higher quantisation only when the file would still fit the GPU with room for the KV cache. Estimates, labelled as such. Never pulls anything."""
    k = quant_key(o.get("quant"))
    size, vram = o.get("size_gb"), hw.get("vram_gb")
    if not k or not size or not vram or k not in _ORDER or k == "q8_0":
        return None
    nxt = _ORDER[_ORDER.index(k) + 1]
    if k in ("q4_0", "q4_k_s"):
        return None
    est = size * BPW[nxt] / BPW[k]
    if est > vram * 0.70:                                # leave 30% of VRAM for the KV cache and the desktop
        return None
    return (f"Quantisation is {k.upper()} ({size:.1f} GB). A {nxt.upper()} build would be about {est:.1f} GB (estimate) and would still fit your {vram:.0f} GB GPU with room for context. "
            f"Higher quants lose less accuracy on exact edits, but cost speed: only switch if it also passes your proof and tests, and keep the smallest quant that does. Nothing is pulled for you.")


def advise_model(o, cfg, hw):
    """One installed model (an `at.owned_models` row) -> what to change for it, and why. `proposed` holds only keys that are empty today."""
    k, samp = sampling_for(o["name"])
    cur = _override_for(cfg, o["name"])
    cur_s = cur.get("sampling") or {}
    proposed, kept, notes = {}, {}, []
    if samp:
        for key in SAMPLING_KEYS:
            if key not in samp:
                continue
            if key in cur_s:
                kept[key] = cur_s[key]                         # yours: left exactly as it is
            else:
                proposed["sampling." + key] = samp[key]
        src = next((t for t, _ in k["notes"] if t == "vendor"), "inference")
        if proposed:
            notes.append((src, f"{k['family']}: {', '.join(f'{p.split(chr(46))[1]} {v}' for p, v in proposed.items())}. Without this the role default (temperature 0.2) is used."))
        if k["kind"] == "reasoning" or (k.get("think") and any(k["think"].values())):
            notes.append(("vendor", "This family uses a thinking mode: do not run it greedy or near-greedy, and leave room in the reply length for the reasoning."))
    else:
        notes.append(("inference", "No sampling guidance is known for this family; the role defaults stay. Probe and the Scoreboard are the evidence."))
    if "uncensored" in o["tags"]:
        notes.append(("inference", "Abliterated/uncensored builds keep the base architecture and sampling; the edit can cost accuracy on exact edits, so judge it by the Scoreboard."))
    moe_tip = None
    if "MoE" in o["tags"]:
        notes.append(("inference", f"Mixture of experts ({o['params_b']}B total, about {o['active_b']}B active): all {o['params_b']}B must be in memory, but only the active part is computed per token, so it is fast for its size when it fits."))
        if o.get("verdict") in ("split", "slow") or (o.get("size_gb") and hw.get("vram_gb") and o["size_gb"] > hw["vram_gb"] * 0.9):
            moe_tip = ("This MoE model does not fit the GPU. Ollama splits it by whole layers, which pushes the hot attention weights to the CPU. "
                       "llama.cpp's llama-server can keep the attention and KV cache on the GPU and put only the expert tensors in system RAM "
                       "(flags --cpu-moe, or --n-cpu-moe N for the first N layers; check `llama-server --help` for your build). "
                       "That is usually much faster than a layer split for the same file. Ollama itself has no such setting.")
    tr = o.get("track")
    if tr and tr.get("n"):
        notes.append(("measured", f"In your own runs: {tr['ok']} of {tr['n']} attempts accepted ({tr['rate']}%)."
                      + (" Fewer than 10 attempts so far: treat this as a hint." if tr["n"] < 10 else "")
                      + (" Low for a builder: try a larger model for this role, or check the edit format." if tr["n"] >= 10 and tr["rate"] < 50 else "")))
    if o.get("edit_style") and o["edit_style"] != "patch":
        notes.append(("measured", f"The edit-format check found this model edits more reliably with '{o['edit_style']}' blocks than with search/replace patches."))
    q = quant_advice(o, hw)
    if q:
        notes.append(("estimate", q))
    return {"name": o["name"], "family": k["family"], "kind": k["kind"], "tier": o["tier"], "tags": o["tags"], "proposed": proposed, "kept": kept,
            "notes": notes, "moe_tip": moe_tip, "has_changes": bool(proposed)}


def server_plan(own, cfg, hw, profile=None):
    """Ollama environment settings that suit THIS machine and THESE models. Each: {env, value, why, source}."""
    out = []
    gpu = bool(hw.get("vram_gb"))
    if gpu and hw.get("vendor") in ("nvidia", "amd", "apple"):
        out.append({"env": "OLLAMA_FLASH_ATTENTION", "value": "1", "source": "vendor",
                    "why": "Less memory and faster attention at long context. Needed for a quantised KV cache."})
    ctx_tight = any((m.get("kv_tip") for m in ((profile or {}).get("models") or {}).values()))
    if ctx_tight:
        out.append({"env": "OLLAMA_KV_CACHE_TYPE", "value": "q8_0", "source": "inference",
                    "why": "tune found that a quantised KV cache fits a larger window here. It is not free: run Probe on your own tasks before keeping it."})
    out.append({"env": "OLLAMA_NUM_PARALLEL", "value": "1", "source": "inference",
                "why": "One agent loop at a time. Every parallel slot multiplies the KV cache, which is memory you would rather spend on context."})
    used = {m for m in (cfg.get("roles") or {}).values() if m} | ({cfg["default_model"]} if cfg.get("default_model") else set())
    sizes = {o["name"]: (o.get("size_gb") or 0) for o in own}
    used_gb = sum(sizes.get(m, 0) for m in used)
    room = (hw.get("vram_gb") or 0) if gpu else (hw.get("ram_gb") or 0) * 0.6
    if len(used) <= 1:
        out.append({"env": "OLLAMA_MAX_LOADED_MODELS", "value": "1", "source": "inference", "why": "One model is in use: nothing else needs to stay loaded."})
    elif used_gb and used_gb <= room * 0.85:
        out.append({"env": "OLLAMA_MAX_LOADED_MODELS", "value": str(len(used)), "source": "inference",
                    "why": f"Your team's {len(used)} models total about {used_gb:.0f} GB and fit together, so swapping them in and out is avoidable."})
    else:
        out.append({"env": "OLLAMA_MAX_LOADED_MODELS", "value": "1", "source": "inference",
                    "why": f"Your team's {len(used)} models total about {used_gb:.0f} GB, more than fits together. Leave one loaded at a time; the harness's keep-alive setting limits the reload cost."})
    return out


def server_lines(plan, windows=None):
    """Copy-paste lines for the OS the machine runs."""
    windows = (os.name == "nt") if windows is None else windows
    if windows:
        return [f'setx {p["env"]} "{p["value"]}"' for p in plan] + ["(then quit and restart Ollama from the tray)"]
    return ["# systemd: sudo systemctl edit ollama   -> add under [Service]:"] + [f'Environment="{p["env"]}={p["value"]}"' for p in plan] + \
           ["# then: sudo systemctl daemon-reload && sudo systemctl restart ollama", "# macOS app: launchctl setenv NAME VALUE, then restart Ollama"]


def stack_advice(own, hw, cfg):
    """Framework and harness notes, each with a source level. Short on purpose; the reasons are in `why`."""
    out = []
    moe_split = [o for o in own if "MoE" in o["tags"] and (o.get("verdict") in ("split", "slow"))]
    if moe_split:
        out.append({"topic": "Inference server", "advice": "Try llama.cpp's llama-server for " + ", ".join(o["name"] for o in moe_split) + " (expert tensors in RAM, the rest on the GPU).",
                    "source": "inference", "why": "Reports from people running local agents measure MoE models at roughly 2.6x the speed of comparable dense models once they run well, and the expert-offload flags are what make that possible on a card smaller than the file."})
    else:
        out.append({"topic": "Inference server", "advice": "Stay on Ollama.",
                    "source": "inference", "why": "Everything installed here fits the way Ollama loads it. llama.cpp gives finer control and earlier model support, but costs a build, a GGUF path per model and a launch command per model."})
    out.append({"topic": "Tool calling", "advice": "Run Probe on each model; trust the score, not the model card.",
                "source": "inference", "why": "Chat templates differ per model, and one reported run had a model that scored 100 on tool use with one template and failed with another. Probe measures the template Ollama actually applies."})
    out.append({"topic": "Harness", "advice": "Use this harness's own loop (plan, edit, run the code checks) and keep the editors you like (Aider, Cline, OpenCode, Qwen Code) for interactive work.",
                "source": "inference", "why": "Those tools are interactive; this one decides pass/fail with code (tests, AST, fake-code gate) and can run unattended. They can point at the same Ollama server."})
    if any(o["tier"] in ("tiny", "small") for o in own):
        out.append({"topic": "Small models", "advice": "Use solo/duo crews, short contexts and the EDIT block format for models under about 12B.",
                    "source": "inference", "why": "Small models lose the thread in long contexts and in multi-role chatter; the harness profile does this for you."})
    return out


def plan(hw, rows, cfg=None, profile=None):
    """The whole per-model picture for the installed models. Never calls a model; reads only what the hub already knows."""
    cfg = cfg or {}
    own = at.owned_models(rows, cfg, profile)
    models = [advise_model(o, cfg, hw) for o in own]
    srv = server_plan(own, cfg, hw, profile)
    return {"models": models, "server": srv, "server_lines": server_lines(srv), "stack": stack_advice(own, hw, cfg),
            "pending": sum(1 for m in models if m["has_changes"]), "last": _load().get("ts")}


def apply(rows, hw, models=None, say=print):
    """Write the proposed sampling keys for the named installed models (all, if none named). Only EMPTY keys are written. -> list of changes."""
    at._wire()
    cfg = mh.read_config()
    p = plan(hw, rows, cfg, at.load_profile())
    chosen = [m for m in p["models"] if m["has_changes"] and (not models or m["name"] in models)]
    if models:
        unknown = sorted(set(models) - {m["name"] for m in p["models"]})
        if unknown:
            raise at.TuneError("Not installed: " + ", ".join(unknown))
    if not chosen:
        say("nothing to change: every model already has the settings this would write (or its family has no guidance)")
        return []
    pending = _load().get("undo")                      # an earlier apply not yet undone: keep ITS "before" so one undo goes back to the start
    undo = pending or {"overrides": {}, "created": [] if "model_overrides" in cfg else ["model_overrides"]}
    changes = []
    ov = cfg.setdefault("model_overrides", {})
    for m in chosen:
        name = m["name"]
        undo["overrides"].setdefault(name, {"had": name in ov, "value": json.loads(json.dumps(ov.get(name)))})
        o = dict(ov.get(name) or {})
        o["sampling"] = dict(o.get("sampling") or {})
        for path_, v in m["proposed"].items():
            o["sampling"][path_.split(".", 1)[1]] = v
            changes.append(f"model {name}: {path_} -> {v}")
        ov[name] = o
    mh.write_config(cfg)
    _save({"ts": time.time(), "undo": undo, "applied": changes})
    for c in changes:
        say("  " + c)
    say(f"wrote {len(changes)} sampling value(s) for {len(chosen)} model(s); `optimize --undo` puts them back")
    return changes


def undo(say=print):
    """Put back exactly what the last `apply` replaced."""
    at._wire()
    rec = _load()
    u = rec.get("undo")
    if not u:
        raise at.TuneError("There is nothing to undo: no optimize has been applied (or it was already undone).")
    cfg = mh.read_config()
    ov = cfg.setdefault("model_overrides", {})
    for name, r in u["overrides"].items():
        if r["had"]:
            ov[name] = r["value"]
        else:
            ov.pop(name, None)
    for k in u.get("created", []):
        if cfg.get(k) == {}:
            cfg.pop(k)
    mh.write_config(cfg)
    rec["undo"], rec["undone_at"] = None, time.time()
    _save(rec)
    say(f"restored {len(u['overrides'])} model(s) to what they were before the last optimize")
    return list(u["overrides"])
