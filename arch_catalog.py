"""Model catalog and recommendations: suggests models you do NOT have yet, sized to THIS machine, and pulls them only after
you confirm the download.

`catalog.json` is a dated, editorial starting list (see its "about"), not a benchmark. Three safeguards:
  * every suggestion is run through the same fit math the Models page uses (weights + KV cache vs VRAM/RAM), so nothing is
    recommended that cannot run here at a useful context;
  * entries whose tag/size came from blog posts carry verified=false until `verify` confirms them against the Ollama registry
    (results are kept in hub_data/catalog_overrides.json, never in the shipped file);
  * the Scoreboard on your machine always outranks this list: `tune` and Team show measured scores next to the suggestion.
"""
import json
import os
import urllib.error
import urllib.request

import arch_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
GB = 1024 ** 3
REGISTRY = "https://registry.ollama.ai/v2"


def _overrides_path():
    return os.path.join(core.data_dir(), "catalog_overrides.json")


def load():
    """-> catalog dict with per-model overrides (verified / size_gb from `verify`) merged in."""
    cat = core.load_json(os.path.join(HERE, "catalog.json"))
    ov = core.load_json(_overrides_path())
    for m in cat.get("models", []):
        o = ov.get(m["tag"])
        if o:
            m.update({k: v for k, v in o.items() if k in ("verified", "size_gb", "checked")})
    return cat


def lookup(tag, cat=None):
    """Catalog entry for an installed model name, matching 'qwen3:8b' exactly or ignoring a '-instruct'/quant suffix."""
    cat = cat or load()
    t = (tag or "").lower()
    for m in cat.get("models", []):
        if m["tag"].lower() == t:
            return m
    base, _, ver = t.partition(":")
    for m in cat.get("models", []):
        b2, _, v2 = m["tag"].lower().partition(":")
        if b2 == base and v2 and ver.startswith(v2):
            return m
    return None


def _arch_for(entry):
    a = dict(entry["arch"])
    a["ctx_max"] = entry.get("ctx_max")
    return a


def fit_for(entry, hw, kv="f16", speed_scale=1.0):
    """speed_scale: measured-vs-estimated speed of a model `tune` actually ran here (1.0 = trust the default bandwidth guess)."""
    import model_hub as mh
    fit = mh.fit_model(int(entry["size_gb"] * GB), _arch_for(entry), hw, kv, gpu_bw=192.0 * speed_scale, ram_bw=50.0 * speed_scale)
    return fit, mh.recommend(fit, hw)


def recommend(hw, installed, scores_by_model=None, cat=None, max_per_slot=2, speed_scale=1.0, owned=None):
    """installed: names already on the machine. owned: [{"name", "eff_b", "est_tok_s"}] for ALL of them (any family, merges, MoE), used to judge
    whether a candidate is really a step up; without it, sizes are read from the names. A model you own is never replaced or hidden: a suggestion
    is only made when it is clearly larger than your biggest one (slots builder, planner) or you have nothing fast (slot fast).
    -> {"as_of", "machine", "suggestions": [...]} ordered builder, planner, fast."""
    import arch_profiles
    cat = cat or load()
    have = {n.lower() for n in installed}
    if owned is None:
        owned = [{"name": n, "eff_b": arch_profiles.effective_b(n), "est_tok_s": None} for n in installed]
    owned_eff = max((o.get("eff_b") or 0 for o in owned), default=0)
    have_fast = any((o.get("est_tok_s") or 0) >= 20 and (o.get("eff_b") or 99) <= 10 for o in owned)
    disk = hw.get("disk_free_gb")
    out = []
    for slot, need_ctx, min_tok_s in (("builder", 8192, 8.0), ("planner", 8192, 5.0), ("fast", 4096, 20.0)):
        cands = []
        for e in cat["models"]:
            if slot not in e["slots"] or e["tag"].lower() in have:
                continue
            fit, rec = fit_for(e, hw, speed_scale=speed_scale)
            if rec["verdict"] == "too_big" or (rec.get("num_ctx") or 0) < need_ctx:
                continue
            if rec.get("est_tok_s") is not None and rec["est_tok_s"] < min_tok_s:
                continue                      # would run, but too slowly to drive an agent loop
            if disk is not None and e["size_gb"] * 1.1 > disk:
                continue
            score = e["rank"] + (10 if rec["verdict"] == "fast" else -10 if rec["verdict"] == "slow" else 0)
            score += 4 if (rec.get("num_ctx") or 0) >= 16384 else 0
            score -= 8 if e.get("restricted") else 0
            score -= 3 if not e.get("verified", True) else 0
            cands.append((score, e, rec))
        cands.sort(key=lambda t: -t[0])
        best_have = max((m["rank"] for m in (lookup(n, cat) for n in installed) if m and slot in m["slots"]), default=0)
        kept = []
        for score, e, rec in cands:
            ce = arch_profiles.effective_b(e["tag"], e.get("params_b"), e.get("active_b")) or 0
            if slot in ("builder", "planner") and owned_eff and ce < owned_eff * 1.25:
                continue                      # you already own something about this size or bigger
            if slot == "fast" and have_fast:
                continue
            if e["rank"] <= best_have + 3:
                continue                      # not a meaningful step up from what you already run
            kept.append((score, e, rec))
            if len(kept) >= max_per_slot:
                break
        for score, e, rec in kept:
            why = [f"{rec['text']} At about {rec['num_ctx']} tokens of context." if rec.get("num_ctx") else rec["text"]]
            if rec.get("est_tok_s"):
                why.append(f"Estimated {rec['est_tok_s']} tokens/s (an estimate from memory bandwidth; `tune` measures the real number).")
            if owned_eff and slot in ("builder", "planner"):
                why.append(f"Larger than your biggest model (about {owned_eff}B dense-equivalent). It is added alongside your models; none are replaced.")
            if best_have:
                why.append(f"Ranks above your best installed {slot} model in the catalog (editorial ordering, {cat['as_of']}).")
            cav = [e["note"]]
            if e.get("restricted"):
                cav.append(f"License: {e['license']} - check it allows your use.")
            if not e.get("verified", True):
                cav.append("Tag/size unverified: run `architect.py catalog --verify` first.")
            out.append({"slot": slot, "tag": e["tag"], "name": e["name"], "size_gb": e["size_gb"], "verdict": rec["verdict"],
                        "est_ctx": rec.get("num_ctx"), "est_tok_s": rec.get("est_tok_s"), "verified": e.get("verified", True),
                        "license": e["license"], "why": why, "caveats": cav, "pull": f"ollama pull {e['tag']}"})
    return {"as_of": cat["as_of"], "about": cat["about"],
            "machine": {"vram_gb": round(hw.get("vram_gb") or 0, 1), "ram_gb": round(hw.get("ram_gb") or 0, 1), "disk_free_gb": round(disk, 1) if disk else None},
            "suggestions": out}


def verify_tag(tag, opener=urllib.request.urlopen, timeout=15):
    """Ask the public Ollama registry whether `tag` exists and how big its layers are. Needs outbound internet from THIS machine.
    -> {"ok": bool, "size_gb"?: float, "error"?: str}"""
    name, _, ver = tag.partition(":")
    path = name if "/" in name else "library/" + name
    req = urllib.request.Request(f"{REGISTRY}/{path}/manifests/{ver or 'latest'}",
                                 headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with opener(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        layers = j.get("layers") or []
        size = sum(int(x.get("size") or 0) for x in layers) + int((j.get("config") or {}).get("size") or 0)
        return {"ok": bool(layers), "size_gb": round(size / GB, 1) if size else None, **({} if layers else {"error": "manifest has no layers"})}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "not found in the registry" if e.code == 404 else f"registry said HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"could not reach the registry ({getattr(e, 'reason', e)})"}


def verify_all(tags=None, opener=urllib.request.urlopen, say=print):
    import time
    cat = load()
    ov = core.load_json(_overrides_path())
    for m in cat["models"]:
        if tags and m["tag"] not in tags:
            continue
        r = verify_tag(m["tag"], opener)
        say(f"  {'OK  ' if r['ok'] else 'FAIL'}  {m['tag']:<26} " + (f"{r.get('size_gb')} GB" if r["ok"] else r.get("error", "")))
        if r["ok"]:
            ov[m["tag"]] = {"verified": True, "size_gb": r.get("size_gb") or m["size_gb"], "checked": time.strftime("%Y-%m-%d")}
        elif "not found" in (r.get("error") or ""):
            ov[m["tag"]] = {"verified": False, "checked": time.strftime("%Y-%m-%d")}
    os.makedirs(core.data_dir(), exist_ok=True)
    with open(_overrides_path(), "w", encoding="utf-8") as f:
        json.dump(ov, f, indent=1)
    return ov


def pull(tag, confirm=False, say=print, opener=urllib.request.urlopen):
    """Download a model through the running Ollama. Refuses unless `confirm` is True (the caller must have shown the size to the person).
    -> True on success."""
    if not confirm:
        raise PermissionError("a model download needs an explicit yes (it can be many GB): pass confirm=True after showing the size")
    body = json.dumps({"model": tag, "stream": True}).encode()
    req = urllib.request.Request(core.ollama_host() + "/api/pull", data=body, headers={"Content-Type": "application/json"})
    last = -10
    try:
        with opener(req, timeout=3600) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                j = json.loads(line)
                if j.get("error"):
                    say(f"  pull failed: {j['error']}")
                    return False
                tot, done = j.get("total"), j.get("completed")
                if tot and done is not None:
                    pct = int(100 * done / tot)
                    if pct >= last + 10:
                        last = pct
                        say(f"  {j.get('status', 'pulling')}: {pct}% of {tot / GB:.1f} GB")
                elif j.get("status") and j["status"] not in ("pulling manifest",):
                    say(f"  {j['status']}")
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        say(f"  pull failed: {getattr(e, 'reason', e)}")
        return False
