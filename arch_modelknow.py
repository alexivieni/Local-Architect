"""What a model is likely good at, from its FAMILY (data/model_families.json), before anything is measured.

identify(passport_or_tag) -> which family/line it belongs to, with modifiers (abliterated, MoE, MTP, coder...) and how it was matched.
profile(model) -> family facts + the quantisation's known cost (arch_port.quant_loss) + what was MEASURED here (labelled [M]; it wins).
job_prior(model, job) -> (0-1, reasons): a starting point used only to ORDER screening and break ties; measurement decides suggestions.
settings_for(model, job) -> sampling, thinking on/off, tool-call format.  explain(model) -> one plain paragraph.

Evidence tiers in every reason: [M] measured here > [B] vendor card/benchmark > [A] community > [E] estimate. An unknown model is said
to be unknown ("unknown family; using <arch> defaults; measure it"), never silently guessed. Reads only; stdlib only; no network.
"""
import json
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "model_families.json")
JOBS = ("route", "plan", "code", "test", "fix", "review", "security", "infra", "docs", "draft")
CODE_JOBS = {"code", "fix", "test", "infra"}
THINK_JOBS = {"plan", "review", "fix", "security"}
_KB = None


def kb(path=None):
    global _KB
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if _KB is None:
        with open(DATA, encoding="utf-8") as f:
            _KB = json.load(f)
    return _KB


def _family(fid):
    return next((f for f in kb()["families"] if f["id"] == fid), None)


def _passport(model):
    if isinstance(model, dict):
        return model
    try:
        import arch_passport
        p = arch_passport.load(model)
        if p:
            return p
    except Exception:
        pass
    return {"name": str(model)}


def _texts(p):
    """-> (own, full): own = the model's own name words (for modifiers); full = own + base model (for the family)."""
    lin = p.get("lineage") or {}
    own = " ".join(str(x) for x in [p.get("name"), lin.get("model_name"), lin.get("finetune"), " ".join(lin.get("tags") or [])] if x)
    full = own + " " + str(lin.get("base_model") or "")
    return own.lower(), full.lower()


def identify(model):
    p = _passport(model)
    own, full = _texts(p)
    arch = str((p.get("arch") or {}).get("arch") or p.get("family") or "").lower()
    fam, by, conf = None, None, 0.0
    for f in kb()["families"]:
        m = f.get("match") or {}
        if any(re.search(x, full) for x in m.get("exclude") or []):
            continue
        hit = next((x for x in m.get("name") or [] if re.search(x, full)), None)
        if hit:
            fam, by, conf = f, f"name /{hit}/", 0.9 if re.search(hit, own) else 0.75
            break
    if fam is None and arch:
        for f in kb()["families"]:
            if any(re.fullmatch(x, arch) for x in (f.get("match") or {}).get("arch") or []):
                fam, by, conf = f, f"GGUF architecture '{arch}'", 0.6
                break
    mods = [k for k, v in kb()["modifiers"].items() if any(re.search(x, own) for x in v["match"])]
    if "abliterated" in mods and "uncensored" in mods:
        mods.remove("uncensored")
    ab = p.get("arch") or {}
    if p.get("moe") or (ab.get("experts") or 0) > 1 or re.search(r"\bmoe\b|-a\d+b|\d+x\d+b", own):
        mods.append("moe")
    if p.get("quant"):
        mods.append("quant:" + str(p["quant"]).upper())
    else:
        q = re.search(r"(iq\d_\w+|q\d_k_[sml]|q\d_\d|q\d_k|f16|bf16|mxfp4)", own)
        if q:
            mods.append("quant:" + q.group(1).upper())
    caps = p.get("capabilities") or []
    if "embedding" in caps and not fam:
        fam, by, conf = _family("embed"), "Ollama capability 'embedding'", 0.8
    out = {"name": p.get("name"), "family": fam["id"] if fam else None, "line": fam["line"] if fam else None, "modifiers": mods,
           "matched_by": by or "nothing matched", "confidence": conf, "arch": arch or None}
    if not fam:
        hint = [w for w in ("coder", "instruct", "hermes", "dolphin", "chat", "reason", "vision") if w in own]
        out["note"] = (f"unknown family; using {arch or 'generic'} defaults" + (f" (name words: {', '.join(hint)})" if hint else "") + "; measure it")
    return out


def _quant_loss(q):
    try:
        import arch_port
        return arch_port.quant_loss(q)
    except Exception:
        return 5.0


def profile(model):
    p = _passport(model)
    ident = identify(p)
    f = _family(ident["family"]) if ident["family"] else None
    q = next((m[6:] for m in ident["modifiers"] if m.startswith("quant:")), None)
    meas = {k: v for k, v in (p.get("measured") or {}).items() if v not in (None, 0)}
    ab = {k: v for k, v in (p.get("abilities") or {}).items() if v is not None}
    out = {"identity": ident, "family": f, "quant": q, "quant_loss_points": _quant_loss(q) if q else None,
           "eff_b": p.get("eff_b"), "capabilities": p.get("capabilities") or [],
           "measured": {"tier": "[M]", **meas, **ab} if (meas or ab) else {},
           "modifier_effects": {m: kb()["modifiers"][m]["effect"] for m in ident["modifiers"] if m in kb()["modifiers"]}}
    if f and p.get("think_style") is None and f["thinking"]["mode"] != "none" and p.get("name"):
        out["thinking_note"] = "family can think, but this file's template shows no thinking tags [M]"
    return out


def job_prior(model, job):
    p = _passport(model)
    pr = profile(p)
    f, ident = pr["family"], pr["identity"]
    reasons = []
    if f and f["id"] == "embed":
        return 0.0, ["embedding model: cannot do chat jobs"]
    if f and job in f.get("jobs", {}):
        s, why = f["jobs"][job]
        reasons.append(f"{f['line']}: {why or 'family prior'}")
    else:
        s = 0.4
        reasons.append(ident.get("note") or f"{f['line'] if f else 'unknown'}: no prior for {job} [E]")
        if not f:
            own = _texts(p)[0]
            if "coder" in own and job in CODE_JOBS:
                s += 0.1
                reasons.append("name says coder [E]")
    for m in ident["modifiers"]:
        d = (kb()["modifiers"].get(m) or {}).get("prior_delta", {}).get(job)
        if d:
            s += d
            reasons.append(f"{m}: {'+' if d > 0 else ''}{d}")
    eff = float(p.get("eff_b") or 0)
    if eff > 0 and job != "draft":
        d = round(0.08 * math.log2(eff / 8.0), 3) if job != "route" else round(-0.04 * math.log2(max(eff, 1) / 4.0), 3)
        d = max(-0.2, min(0.12, d))
        if d:
            s += d
            reasons.append(f"size {eff}B: {'+' if d > 0 else ''}{d} [E]")
    if pr["quant_loss_points"]:
        d = -round(pr["quant_loss_points"] / 100.0 * (1.5 if job in CODE_JOBS else 1.0), 3)
        s += d
        reasons.append(f"quant {pr['quant']} costs ~{pr['quant_loss_points']} points: {d}")
    me = pr["measured"]
    if me.get("schema_json") is False and job in ("route", "plan"):
        s -= 0.1
        reasons.append("[M] failed the strict-JSON probe: -0.1")
    if me.get("schema_json") is True and job in ("route", "plan"):
        s += 0.05
        reasons.append("[M] passed the strict-JSON probe: +0.05")
    if "tools" not in pr["capabilities"] and pr["capabilities"] and job in ("route", "infra"):
        s -= 0.05
        reasons.append("[M] Ollama lists no tool capability: -0.05")
    return round(max(0.0, min(1.0, s)), 3), reasons


def settings_for(model, job):
    p = _passport(model)
    pr = profile(p)
    f = pr["family"] or {}
    samp = f.get("sampling") or {}
    defaults = kb()["_sampling_defaults"]
    key = "code" if job in CODE_JOBS else ("route" if job in ("route", "draft") else "default")
    tmode = (f.get("thinking") or {}).get("mode", "none")
    think = tmode == "always" or (tmode == "optional" and job in THINK_JOBS)
    s = dict(samp.get(key) or samp.get("default") or defaults[key])
    if tmode == "optional" and not think and samp.get("nothink"):
        s = dict(samp["nothink"])
    if not think and key == "route":
        s.update(defaults["route"]) if not samp.get("route") else s.update(samp["route"])
    tc = f.get("tool_calling") or {"format": "unknown (measure it)", "reliability": "unknown"}
    out = {"sampling": s, "thinking": think, "thinking_mode": tmode, "thinking_tag": (f.get("thinking") or {}).get("tag"),
           "tool_format": tc["format"], "tool_reliability": tc["reliability"],
           "system_role": (f.get("template") or {}).get("system_role", True), "source": f"{f.get('line', 'defaults')} [{'B/A/E per family file'}]"}
    if not f:
        out["note"] = pr["identity"].get("note")
    if any(m in pr["identity"]["modifiers"] for m in ("abliterated", "uncensored")):
        out["note"] = "abliterated/uncensored: check tool-call and JSON output before trusting it"
    return out


def top_jobs(model, n=3):
    sc = sorted(((job_prior(model, j)[0], j) for j in JOBS if j != "draft"), reverse=True)
    return [j for _, j in sc[:n]]


def explain(model):
    p = _passport(model)
    pr = profile(p)
    ident, f = pr["identity"], pr["family"]
    size = f"{p.get('eff_b')}B" if p.get("eff_b") else ""
    if not f:
        return f"{p.get('name')}: {ident['note']}. Matched by: {ident['matched_by']}."
    bits = [f"{p.get('name')} -> {f['line']} {size} ({ident['matched_by']}, confidence {ident['confidence']}): {f['known_for']}."]
    tc = f["tool_calling"]
    if tc["format"] != "none":
        bits.append(f"Tool calls: {tc['format']} (reliability {tc['reliability']} {tc['evidence']}).")
    bits.append(f"Code: {f['code_strength']['level']} {f['code_strength']['evidence']}.")
    if f.get("weaknesses"):
        bits.append("Weak at: " + "; ".join(f["weaknesses"][:2]) + ".")
    tj = top_jobs(p)
    bits.append("Prior best jobs: " + ", ".join(tj) + " (a starting order only; measurement decides).")
    mods = [m for m in ident["modifiers"] if not m.startswith("quant:")]
    if mods:
        bits.append("Modifiers: " + ", ".join(mods) + ".")
    if "abliterated" in mods or "uncensored" in mods:
        bits.append(kb()["modifiers"]["abliterated" if "abliterated" in mods else "uncensored"]["effect"].split(".")[0] + ".")
    if pr["quant"]:
        bits.append(f"Stored at {pr['quant']}: about {pr['quant_loss_points']} points of accuracy lost [E].")
    me = pr["measured"]
    if me:
        mm = [f"{k}={v}" for k, v in me.items() if k != "tier"][:5]
        bits.append("Measured here [M]: " + ", ".join(mm) + ".")
    else:
        bits.append("Nothing measured here yet.")
    return " ".join(bits)


def plain_line(p):
    """One line for the passport (identity + top jobs), never raising."""
    try:
        i = identify(p)
        if not i["family"]:
            return f"{i['note']}."
        return f"{i['line']} ({i['matched_by']}); best jobs by prior: {', '.join(top_jobs(p))}; modifiers: {', '.join(i['modifiers']) or 'none'}."
    except Exception as ex:
        return f"family lookup failed: {ex}"


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:] or []:
        print(explain(a))
