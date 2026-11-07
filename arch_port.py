"""Moving this install to another computer, and fitting itself to the one it lands on.

Two halves of the same idea.

**Choosing a team from what is actually here.** The hardware profiles name models like `qwen2.5-coder:7b`, which is a sensible default
and frequently not what is installed. This picks the four tiers out of whatever models this machine really has -- a community fine-tune,
an abliterated build, a mixture-of-experts, anything -- by measuring each one against this card rather than matching a name:

    worker   the largest model whose weights AND conversation memory fit entirely on the graphics card. Fully on the card is the
             single biggest speed difference on a laptop, so it outranks being a bigger model.
    router   the smallest capable model, on the processor. Picking a flow is a one-word answer; it does not deserve graphics memory.
    heavy    the most capable model that still fits in system memory, used only when the worker has already failed.
    draft    a very small model of the SAME family as the worker, which guesses its next words. A different family cannot: the two
             must share a tokenizer, so this is left empty far more often than it is filled.

Every pick comes with a sentence saying why, and the numbers behind it.

**Carrying your work across.** Some of what the app knows is about you -- your settings, the notes it has written about your projects,
your skills, your saved commands, your hooks, your training data. Some of it is about one specific computer -- which models were picked,
how fast they ran, what fitted in 6 GB. The first kind should travel. The second kind is a lie on any other machine, so it is deliberately
left behind and measured again.

    python architect.py port --pack mysetup.zip     on the old machine
    python architect.py port --adopt mysetup.zip    on the new one: restores your half, then re-picks the team for this hardware
    python architect.py port                        just show what this machine would pick, and why
"""
import json
import os
import re
import shutil
import time
import zipfile

import arch_core as core

GB = 1024 ** 3
INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))   # skills/, commands/, flows/, prompts/ and profiles/ live beside the code
CTX_STEPS = (8192, 6144, 4096, 3072, 2048)
HEAVY_MAX_B = 16.0          # the ceiling the design sets: above this, a laptop is renting a model it cannot run
ROUTER_MAX_B = 3.0
DRAFT_MAX_B = 1.2

# What belongs to you and should travel, as paths relative to the data folder (plus the settings file, handled separately).
PORTABLE = [
    "memory/project.md", "memory/decisions.md", "memory/lessons.md", "memory/notes.md",
    "memory/models.md", "hooks.json", "skills.json", "ledger.json", "datasets", "evals", "evals.jsonl", "backlog.json",
]
# What is true of one computer only. Carrying these across is how an install ends up insisting a 6 GB plan on a 24 GB card.
MACHINE_ONLY = [
    "profile.json", "model_stats.json", "modelopt.json", "ready.json", "results.json",
    "server", "passports", "cache", "memory/machine.md", "memory/tuning.md", "jobs.db", "STOP", "trusted_hooks.json",
    "tune_variants.json", "lora_out/train_stats.json",     # measured server variants and training speed: true of this card only
]
# Settings that describe hardware rather than preference.
MACHINE_KEYS = ["tiers", "roles", "role_hosts", "hosts", "model_overrides", "ollama_host",
                "server.binary", "server.port", "server.ctx", "server.parallel", "limits.num_ctx"]

_CODER = re.compile(r"cod(er|e)|starcoder|codellama|codestral|devstral|granite.*code", re.I)
_EMBED = re.compile(r"embed|bge|gte-|e5-|nomic-embed|minilm", re.I)

# Roughly how many points of a task set a quantisation costs, from arch_modelmath's table. Squeezing a model smaller is not free, and
# below about 4 bits it stops being a bargain: a 9B squeezed to 2 bits is a worse coder than an 8B at 4 bits, despite being "bigger".
_LOSS_PREFIX = [("F16", 0.0), ("BF16", 0.0), ("Q8", 0.0), ("Q6", 0.5), ("Q5", 1.2), ("MXFP4", 2.0),
                ("Q4_K_M", 2.5), ("Q4_K_S", 3.5), ("IQ4", 4.0), ("Q4", 4.5), ("IQ3", 9.0), ("Q3", 10.0),
                ("IQ2", 22.0), ("Q2", 20.0), ("IQ1", 40.0), ("Q1", 40.0)]


def quant_loss(quant):
    """Points of accuracy this quantisation is expected to cost, from the measured table in arch_modelmath (unknown -> a cautious 5)."""
    q = (quant or "").upper().strip()
    if not q:
        return 5.0
    try:
        import arch_modelmath
        if q in arch_modelmath.QUALITY_LOSS:
            return float(arch_modelmath.QUALITY_LOSS[q])
    except Exception:
        pass
    for pre, loss in sorted(_LOSS_PREFIX, key=lambda x: -len(x[0])):
        if q.startswith(pre):
            return loss
    return 5.0


def quality(m):
    """One number for "how good is this model likely to be here", so the choice can be ranked and explained.

    Capability grows with size but with sharply diminishing returns -- 14B is not twice 7B -- so it is counted on a log scale. Then the
    quantisation's known cost is subtracted. That is the whole formula, and it is why a 9B squeezed to 2 bits loses to an 8B at 4 bits.
    """
    import math
    eff = float(m.get("eff_b") or 0)
    if eff <= 0:
        return 0.0
    cap = 30.0 * math.log10(eff + 1.0)
    return round(cap - quant_loss(m.get("quant")) + (1.5 if m.get("coder") else 0.0), 1)


# ---------------------------------------------------------------- facts about each installed model

def facts(say=None):
    """Every installed model with what is known about it. Uses the passports, building any that are missing."""
    import arch_passport
    try:
        arch_passport.refresh(say=(say or (lambda _m: None)))
    except Exception:
        pass
    out = []
    d = arch_passport.passports_dir()
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        p = core.load_json(os.path.join(d, fn)) or {}
        name = p.get("name")
        if not name or p.get("error"):
            continue
        a = p.get("arch") or {}
        eff = p.get("eff_b") or a.get("params_b")
        if not eff and p.get("size_gb"):
            eff = round(p["size_gb"] * 1.7, 1)          # a rough stand-in from file size when the header did not say
        out.append({"name": name, "size_gb": p.get("size_gb") or 0, "quant": p.get("quant"), "family": p.get("family") or "",
                    "layers": a.get("layers") or 0, "kv_heads": a.get("kv_heads"), "head_dim": a.get("head_dim"),
                    "ctx_max": a.get("ctx_max") or 0, "params_b": a.get("params_b"), "eff_b": eff or 0,
                    "active_b": p.get("active_b"), "moe": bool(p.get("moe")), "think": p.get("think_style"),
                    "caps": p.get("capabilities") or [], "arch": a,
                    "embed": bool(_EMBED.search(name)) or (p.get("family") or "") == "bert",
                    "coder": bool(_CODER.search(name))})
    return out


def _fit(m, hw, ctx, kv="q8_0"):
    """-> (layers_on_gpu, gb_on_gpu, fits_entirely). Uses the same arithmetic the server planner uses, so the two never disagree."""
    import arch_server
    if not m.get("layers") or not m.get("size_gb"):
        return (0, 0.0, False)
    return arch_server._ngl(int(m["size_gb"] * GB), m["arch"], hw, ctx, kv)


# ---------------------------------------------------------------- choosing the four tiers

def pick_tiers(hw=None, cfg=None, models=None, say=None):
    """-> {"profile", "ctx", "tiers": {tier: {...}}, "notes": [...], "considered": n}.

    Nothing here looks at a model's reputation. It measures each installed model against this machine and says what it found.
    """
    import arch_hw
    import arch_server
    cfg = cfg or core.load_config()
    hw = hw or arch_hw.detect()
    ms = [m for m in (models if models is not None else facts(say)) if not m["embed"] and m["size_gb"]]
    prof_name = arch_server.profile_for(hw)
    prof = arch_server.load_profile(prof_name) or {}
    kv = (cfg.get("server") or {}).get("kv") or prof.get("kv") or "q8_0"
    want_ctx = int((cfg.get("server") or {}).get("ctx") or prof.get("ctx") or 8192)
    vram = float(hw.get("vram_gb") or 0)
    ram = float(hw.get("ram_gb") or 0)
    out = {"profile": prof_name, "ctx": want_ctx, "kv": kv, "tiers": {}, "notes": [], "considered": len(ms),
           "hardware": {"gpu": hw.get("gpu"), "vram_gb": vram, "ram_gb": ram, "cores": hw.get("cpu_cores")}}
    if not ms:
        out["notes"].append("No models are installed yet, so there is nothing to choose between. Install one and run this again.")
        return out

    # ---- worker: the largest model that fits ENTIRELY on the card, stepping the window down before giving that up
    worker, w_ctx, w_why = None, want_ctx, ""
    if vram >= 2:
        for ctx in [c for c in CTX_STEPS if c <= want_ctx] or [want_ctx]:
            fits = []
            for m in ms:
                ngl, gb, full = _fit(m, hw, ctx, kv)
                if full:
                    fits.append((m, gb))
            if fits:
                fits.sort(key=lambda x: (-quality(x[0]), x[1]))
                worker, w_ctx = fits[0][0], ctx
                w_why = (f"the best model that fits entirely on the card: {worker['eff_b']}B at {worker['quant'] or 'an unknown quantisation'}, "
                         f"scoring {quality(worker)} once its {quant_loss(worker['quant'])}-point quantisation cost is taken off, in about "
                         f"{fits[0][1]} GB of {round(vram, 1)} GB with a {ctx}-token memory")
                if ctx < want_ctx:
                    w_why += f". The window had to come down from {want_ctx} to {ctx} tokens for it to fit"
                break
    if not worker:
        ms_by_size = sorted(ms, key=lambda m: m["size_gb"])
        worker = ms_by_size[0]
        ngl, gb, _ = _fit(worker, hw, want_ctx, kv)
        w_ctx = want_ctx if vram >= 2 else min(4096, want_ctx)
        w_why = ("nothing installed fits entirely on this card, so the smallest model runs partly from system memory and will be slow"
                 if vram >= 2 else "this machine has no usable graphics card, so the smallest installed model runs on the processor")
    out["tiers"]["worker"] = _tier(worker, w_ctx, w_why, hw, kv)

    # ---- router: the smallest capable model, on the processor
    small = [m for m in ms if (m["eff_b"] or 99) <= ROUTER_MAX_B and m["name"] != worker["name"]]
    if small:
        r = sorted(small, key=lambda m: (m["eff_b"] or 99, m["size_gb"]))[0]
        out["tiers"]["router"] = _tier(r, min(4096, w_ctx), f"the smallest model here ({r['eff_b']}B): picking which flow to run is a "
                                       "one-word answer, so it runs on the processor and costs no graphics memory", hw, kv, cpu=True)
    else:
        out["tiers"]["router"] = {"model": worker["name"], "cpu": False, "ctx": w_ctx,
                                  "why": "no small model is installed, so the worker answers routing questions too. A 1B model would "
                                         "free it up; this costs about a second and a half per run as it is."}

    # ---- heavy: the most capable model that still fits in system memory, used only after the worker fails
    unified = bool((hw or {}).get("unified"))          # Apple/APU shared memory: the GPU and the system share one pool
    max_hvy_gb = max(0.0, (ram * 0.70 if unified else ram - 4))
    bigger = [m for m in ms
              if quality(m) > quality(worker) + 1 and (m["eff_b"] or 99) <= HEAVY_MAX_B and m["size_gb"] <= max_hvy_gb]
    if bigger:
        hvy = sorted(bigger, key=lambda m: -quality(m))[0]
        ngl, gb, full = _fit(hvy, hw, w_ctx, kv)
        share = int(100 * ngl / max(1, hvy["layers"])) if hvy["layers"] else 0
        out["tiers"]["heavy"] = _tier(hvy, w_ctx, (f"the most capable model that still fits in {round(ram)} GB of memory "
                                                   f"({hvy['eff_b']}B). About {share}% of it sits on the card and the rest in RAM, so it "
                                                   "is slow: it is only called after the worker has already failed a step"), hw, kv)
    else:
        out["tiers"]["heavy"] = {"model": "", "why": ("nothing installed is both bigger than the worker and small enough for this "
                                                      "machine's memory, so there is no step up to make. The worker's failures stay failures.")}

    # ---- draft: only a model of the worker's own family can guess its words
    fam = (worker.get("family") or "").lower()
    tiny = [m for m in ms if (m["eff_b"] or 99) <= DRAFT_MAX_B and m["name"] != worker["name"]
            and (m.get("family") or "").lower() == fam and fam]
    if tiny:
        d = sorted(tiny, key=lambda m: m["size_gb"])[0]
        out["tiers"]["draft"] = _tier(d, w_ctx, (f"a {d['eff_b']}B model of the same family as the worker, so it shares its vocabulary "
                                                 "and can guess its next words. Kept only if measuring shows it is actually faster"), hw, kv)
    else:
        out["tiers"]["draft"] = {"model": "", "why": ("speculative decoding needs a tiny model from the SAME family as the worker, "
                                                      f"and there is no small {fam or 'matching'} model installed. Nothing is lost: it is a "
                                                      "speed trick, not a quality one.")}

    if vram and vram < 6:
        out["notes"].append(f"{round(vram, 1)} GB of graphics memory is tight. Closing other graphics-heavy windows frees roughly half a "
                            "gigabyte, which is often the difference between the worker fitting and not.")
    if out["tiers"]["worker"].get("model") and not out["tiers"]["worker"].get("full_gpu") and vram >= 2:
        out["notes"].append("The worker does not fit entirely on the card at this window. A smaller quantisation of the same model, or a "
                            "shorter window, is usually worth more than a bigger model that spills into RAM.")
    return out


def _tier(m, ctx, why, hw, kv, cpu=False):
    ngl, gb, full = _fit(m, hw, ctx, kv)
    return {"model": m["name"], "ctx": ctx, "cpu": bool(cpu), "ngl": 0 if cpu else ngl, "est_gpu_gb": 0.0 if cpu else gb,
            "score": quality(m), "quant_loss": quant_loss(m.get("quant")),
            "full_gpu": bool(full) and not cpu, "size_gb": m["size_gb"], "eff_b": m["eff_b"], "quant": m["quant"],
            "family": m["family"], "moe": m["moe"], "thinks": m.get("think") == "tags", "why": why}


def render(plan):
    """The plan in plain words, numbers after the sentence that explains them."""
    hw = plan.get("hardware") or {}
    out = [f"This machine: {hw.get('gpu') or 'no dedicated graphics card'}, {round(hw.get('vram_gb') or 0, 1)} GB of graphics memory, "
           f"{round(hw.get('ram_gb') or 0)} GB of system memory.",
           f"Looked at {plan.get('considered', 0)} installed model(s) and measured each one against it.", ""]
    for tier in ("worker", "router", "heavy", "draft"):
        t = (plan.get("tiers") or {}).get(tier) or {}
        if not t.get("model"):
            out.append(f"  {tier:<7} none.  {t.get('why', '')}")
            out.append("")
            continue
        head = f"  {tier:<7} {t['model']}"
        out.append(head)
        out.append(f"          {t['why']}.")
        bits = []
        if t.get("size_gb"):
            bits.append(f"{t['size_gb']} GB on disk")
        if t.get("quant"):
            bits.append(str(t["quant"]))
        if t.get("eff_b"):
            bits.append(f"{t['eff_b']}B effective" + (" (mixture-of-experts)" if t.get("moe") else ""))
        if t.get("score") is not None:
            bits.append(f"scores {t['score']} after a {t.get('quant_loss')}-point quantisation cost")
        if not t.get("cpu") and t.get("est_gpu_gb"):
            bits.append(f"{t['est_gpu_gb']} GB on the card" + (" (all of it)" if t.get("full_gpu") else ", the rest from RAM"))
        if t.get("cpu"):
            bits.append("on the processor")
        if t.get("ctx"):
            bits.append(f"{t['ctx']}-token memory")
        if t.get("thinks"):
            bits.append("reasons before answering")
        out.append("          " + "; ".join(bits))
        out.append("")
    for n in plan.get("notes") or []:
        out.append("  Note: " + n)
    return "\n".join(out)


def apply_plan(plan):
    """Write the chosen tiers into the settings. -> what changed, so it can be reported and undone."""
    mh = _mh()
    raw = mh.read_config()
    before = dict(raw.get("tiers") or {})
    tiers = {t: (plan["tiers"].get(t) or {}).get("model", "") for t in ("router", "worker", "heavy", "draft")}
    raw["tiers"] = tiers
    mh.write_config(raw)
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        core._atomic_write(os.path.join(core.data_dir(), "port.undo.json"),
                           json.dumps({"tiers": before, "at": time.time()}, indent=1))
    except (OSError, AttributeError):
        pass
    return {"before": before, "after": tiers,
            "changed": {t: (before.get(t, ""), v) for t, v in tiers.items() if before.get(t, "") != v}}


# ---------------------------------------------------------------- carrying it to another machine

def _mh():
    """The settings reader/writer, pointed at THIS engine's settings file at the moment of use."""
    import model_hub as mh
    mh.CONFIG_FILE = core.CONFIG_FILE
    return mh


def _portable_settings(raw):
    out = json.loads(json.dumps(raw or {}))
    for key in MACHINE_KEYS:
        if "." in key:
            sec, k = key.split(".", 1)
            if isinstance(out.get(sec), dict):
                out[sec].pop(k, None)
        else:
            out.pop(key, None)
    return out


def pack(dest, root=None):
    """Write a zip holding the half of this install that is about YOU. -> a summary of what went in and what was left behind."""
    mh = _mh()
    dd = core.data_dir()
    added, skipped = [], []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        settings = _portable_settings(mh.read_config())
        z.writestr("settings.json", json.dumps(settings, indent=1))
        added.append("settings.json")
        for rel in PORTABLE:
            p = os.path.join(dd, rel)
            if os.path.isfile(p):
                z.write(p, "data/" + rel.replace("\\", "/"))
                added.append(rel)
            elif os.path.isdir(p):
                n = 0
                for base, _dirs, files in os.walk(p):
                    for fn in files:
                        full = os.path.join(base, fn)
                        z.write(full, "data/" + os.path.relpath(full, dd).replace("\\", "/"))
                        n += 1
                if n:
                    added.append(f"{rel} ({n} files)")
        for folder in ("skills", "commands", "flows", "prompts", "profiles"):
            src = os.path.join(INSTALL_DIR, folder)       # these sit beside the code, not in the data folder
            if os.path.isdir(src):
                n = 0
                for base, _dirs, files in os.walk(src):
                    for fn in files:
                        full = os.path.join(base, fn)
                        z.write(full, folder + "/" + os.path.relpath(full, src).replace("\\", "/"))
                        n += 1
                added.append(f"{folder}/ ({n} files)")
        if root:
            pm = os.path.join(root, ".arch", "memory")
            if os.path.isdir(pm):
                for fn in sorted(os.listdir(pm)):
                    if fn.endswith(".md"):
                        z.write(os.path.join(pm, fn), "project_memory/" + fn)
                        added.append("project memory/" + fn)
        skipped = [m for m in MACHINE_ONLY if os.path.exists(os.path.join(dd, m))]
        z.writestr("port_manifest.json", json.dumps({
            "made": time.strftime("%Y-%m-%d %H:%M"), "from_profile": core.load_json(os.path.join(dd, "profile.json")).get("class")
            if os.path.isfile(os.path.join(dd, "profile.json")) else None,
            "added": added, "left_behind": skipped,
            "why_left_behind": "these describe one computer's hardware and measurements; on another machine they would be wrong, "
                               "so the new machine measures its own",
        }, indent=1))
    return {"path": dest, "added": added, "left_behind": skipped,
            "bytes": os.path.getsize(dest) if os.path.isfile(dest) else 0}


def adopt(src, root=None):
    """Restore the portable half here. Never touches this machine's own measurements. -> a summary of what was restored."""
    mh = _mh()
    dd = core.data_dir()
    os.makedirs(dd, exist_ok=True)
    restored, manifest = [], {}
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if "port_manifest.json" in names:
            manifest = json.loads(z.read("port_manifest.json").decode("utf-8"))
        if "settings.json" in names:
            incoming = json.loads(z.read("settings.json").decode("utf-8"))
            raw = mh.read_config()
            merged = core.deep_merge(incoming, {k: v for k, v in raw.items() if k in ("tiers", "roles", "ollama_host", "hosts")})
            mh.write_config(merged)
            restored.append("your settings (this machine's own model choices were kept)")
        for n in names:
            if n.startswith("data/"):
                target = os.path.join(dd, n[5:].replace("/", os.sep))
            elif n.split("/")[0] in ("skills", "commands", "flows", "prompts", "profiles"):
                target = os.path.join(INSTALL_DIR, n.replace("/", os.sep))
            elif n.startswith("project_memory/") and root:
                target = os.path.join(root, ".arch", "memory", n.split("/", 1)[1])
            else:
                continue
            if n.endswith("/"):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(n) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            restored.append(n)
    return {"restored": restored, "manifest": manifest,
            "note": "Nothing about this computer's hardware was restored: the team is chosen again below, from what is installed here."}
