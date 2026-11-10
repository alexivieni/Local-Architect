"""`tune`: measure THIS machine with the models you actually use, then write settings that fit.

    python architect.py tune                 probe every model assigned to a role (or the recommended team if none is assigned)
    python architect.py tune --no-probe      use the fit math only (no model calls)
    python architect.py tune --deep          also fill most of the context window once, to prove nothing gets truncated
    python architect.py tune --no-apply      report only; change nothing

For each model: fit math (weights + KV cache vs VRAM/RAM) -> a context window that fits; a short real call -> measured
generation speed, prompt-processing speed, load time and how much of the model is actually on the GPU; a canary the model
must repeat back, proving the start of the prompt survived. From those it writes, and records in hub_data/profile.json:
  * per-model context size (model_overrides[model].num_ctx) and the ceiling the auto-grow may reach (limits.num_ctx_max);
  * the harness tier of the BUILDER model and its settings (arch_profiles), softened when the model is slow here;
  * keep_alive and a reply-timeout ceiling that fit the measured speeds;
  * a speed calibration for the catalog's estimates.
It never changes a role you assigned (it only fills empty roles, and not at all when you run on a single default model), never
deletes or replaces a model, and never downloads anything. `tune --undo` puts back what the last tune changed.
"""
import json
import os
import re
import time

import arch_catalog
import arch_core as core
import arch_hw
import arch_params as params
import arch_profiles
import arch_roles as roles
import arch_tune
import model_hub as mh

VERSION = 1
CTX_CAP = {"tiny": 8192, "small": 16384, "mid": 32768, "large": 65536}
MIN_USEFUL_CTX = 8192
CANARY = "MANGO-7421"
FILLER = ("def area(w, h):\n    \"\"\"Return the area of a w by h rectangle.\"\"\"\n    if w < 0 or h < 0:\n        raise ValueError('negative size')\n    return w * h\n\n")


class TuneError(Exception):
    pass


def profile_path():
    return os.path.join(core.data_dir(), "profile.json")


def load_profile():
    return core.load_json(profile_path())


def _wire():
    """The hub module keeps its own copies of these; point them at this install's config/host so tune works standalone too."""
    mh.CONFIG_FILE = core.CONFIG_FILE
    mh.ollama_host = core.ollama_host
    mh.DATA_DIR = core.data_dir()
    mh.RESULTS_FILE = os.path.join(mh.DATA_DIR, "results.json")


def _scores(row):
    return {c: v["score"] for c, v in (row.get("agg") or {}).items() if isinstance(v, dict) and "score" in v}


def _param_b(row):
    p = row.get("params") or ""
    try:
        return float(str(p).lower().replace("b", "").strip())
    except ValueError:
        pass
    n = (row.get("arch") or {}).get("params")
    return round(n / 1e9, 1) if n else None


_UNCENSORED = re.compile(r"abliterat|uncensor|huihui|dolphin|heretic|lexi", re.I)


def _sizes(row, cat=None):
    """-> (total_b, active_b or None, effective_b, catalog entry) for any installed model, any family. A mixture-of-experts model is sized
    from its own expert counts (and the catalog when it knows the tag), so a 30B-A3B is not treated as a dense 30B."""
    c = arch_catalog.lookup(row["name"], cat) or {}
    total = c.get("params_b") or _param_b(row)
    active = c.get("active_b") or arch_profiles.active_from_arch(total, row.get("arch"))
    return total, active, arch_profiles.effective_b(row["name"], total, active), c


def owned_models(rows, cfg=None, profile=None):
    """Everything you have installed, described (never filtered by what the catalog knows): size, kind, tier, fit, speed, roles."""
    cfg = cfg or {}
    cat = arch_catalog.load()
    used = {}
    for r, m in (cfg.get("roles") or {}).items():
        if m:
            used.setdefault(m, []).append(r)
    out = []
    try:
        import arch_ledger
    except ImportError:                                  # pragma: no cover
        arch_ledger = None
    for row in rows:
        if row.get("embed"):
            continue
        total, active, eff, c = _sizes(row, cat)
        tier, _ = arch_profiles.tier_for(row["name"], total, active, _scores(row))
        pm = ((profile or {}).get("models") or {}).get(row["name"]) or {}
        tags = []
        if active and total and active < total:
            tags.append("MoE")
        if _UNCENSORED.search(row["name"]):
            tags.append("uncensored")
        if "coder" in row["name"].lower() or (row.get("know") or {}).get("kind") == "coder":
            tags.append("coder")
        out.append({"name": row["name"], "size_gb": row.get("size_gb"), "params_b": total, "active_b": active, "eff_b": eff, "tags": tags, "tier": tier,
                    "quant": row.get("quant"), "verdict": (row.get("rec") or {}).get("verdict"), "est_tok_s": (row.get("rec") or {}).get("est_tok_s"),
                    "measured_tok_s": (pm.get("measured") or {}).get("gen_tok_s"), "ctx": pm.get("ctx") or (row.get("rec") or {}).get("num_ctx"),
                    "roles": sorted(used.get(row["name"], [])), "default": cfg.get("default_model") == row["name"],
                    "scores": _scores(row),
                    "track": (arch_ledger.summary(row["name"]) if arch_ledger else None),
                    "edit_style": (pm.get("modes") or {}).get("recommend_edit_style")})
    return out


def suggest(hw, rows, cfg=None, profile=None):
    """Catalog suggestions judged against ALL the models you own. Never downloads."""
    own = owned_models(rows, cfg, profile)
    scale = (profile or {}).get("speed_scale") or 1.0
    return arch_catalog.recommend(hw, [o["name"] for o in own], speed_scale=scale,
                                  owned=[{"name": o["name"], "eff_b": o["eff_b"], "est_tok_s": o["measured_tok_s"] or o["est_tok_s"]} for o in own])


def _prompt(target_chars):
    body = FILLER * max(1, int(target_chars // len(FILLER)))
    return (f"The secret word is {CANARY}.\n\nBelow is repeated code. Ignore it.\n\n{body}\n"
            f"Question: what was the secret word on the first line? Reply with just the word.")


def _probe(name, ctx, deep, hw, stats, say):
    """One real call at the chosen window. -> dict of measurements (never raises for a model-side failure)."""
    cpt = arch_tune.cpt_for(stats, name)
    if deep:
        chars = int(ctx * 0.70 * cpt)
        est_s = chars / cpt / (stats.get(name, {}).get("prefill_tok_s") or arch_tune.UNKNOWN_PREFILL_TOK_S)
        if est_s > 900:
            say(f"    deep check skipped for {name}: filling {ctx} tokens would take about {int(est_s / 60)} min at this speed")
            chars, deep = 4500, False
    else:
        chars = 4500
    msgs = [{"role": "user", "content": _prompt(chars)}]
    opts = {"num_ctx": ctx, "num_predict": 48, "temperature": 0}
    t0 = time.time()
    limits = {"first": 900, "idle": 300, "total": 1200}
    try:
        r = core.stream_chat(name, msgs, opts, False, "10m", limits)           # thinking off: we are timing plain generation
    except core.LLMError:
        try:                                                                  # a model that rejects think=false: retry plain
            r = core.stream_chat(name, msgs, opts, None, "10m", limits)
        except core.LLMError as e:
            return {"error": str(e)[:200]}
    out = {"secs": round(time.time() - t0, 1), "gen_tok_s": r["tok_s"], "prefill_tok_s": r.get("prefill_tok_s"), "load_s": r.get("load_s"),
           "prompt_tokens": r["prompt_tokens"], "canary_ok": CANARY in r["text"], "deep": deep, "chars": len(msgs[0]["content"])}
    arch_tune.observe(stats, name, prompt_chars=out["chars"], prompt_tokens=r["prompt_tokens"], num_ctx=ctx, gen_tok_s=r["tok_s"],
                      prefill_tok_s=r.get("prefill_tok_s"), load_s=r.get("load_s"))
    try:                                         # how much of the model is really on the GPU right now
        for m in mh.oll("/api/ps", timeout=5).get("models", []):
            if m.get("name") == name or m.get("model") == name:
                if m.get("size"):
                    out["gpu_pct"] = round(100 * m.get("size_vram", 0) / m["size"])
    except mh.OllamaError:
        pass
    return out


def undo_last(say=print):
    """Put back every value the last `tune` changed (settings, per-model context, roles it filled). Values you changed by hand since are
    overwritten with what they were before the tune, so look at the profile's `changes` first. -> list of what was restored."""
    prof = load_profile()
    u = (prof or {}).get("undo")
    if not u:
        raise TuneError("There is nothing to undo: the last tune made no changes (or none has been run).")
    _wire()
    cfg = mh.read_config()
    done = []
    for path_, rec in u["settings"].items():
        d, parts = cfg, path_.split(".")
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        if rec["had"]:
            d[parts[-1]] = rec["value"]
        else:
            d.pop(parts[-1], None)
        done.append(path_)
    for name, rec in u["overrides"].items():
        ov = cfg.setdefault("model_overrides", {})
        if rec["had"]:
            ov[name] = rec["value"]
        else:
            ov.pop(name, None)
        done.append(f"model {name}")
    for r, old in u["roles"].items():
        if old:
            cfg.setdefault("roles", {})[r] = old
        else:
            (cfg.get("roles") or {}).pop(r, None)
        done.append(f"role {r}")
    for k in u.get("created", []):                        # containers the tune had to create are removed again once empty
        if cfg.get(k) == {}:
            cfg.pop(k)
    mh.write_config(cfg)
    prof["undo"] = None
    prof["undone_at"] = time.time()
    with open(profile_path(), "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=1)
    say(f"restored {len(done)} setting(s) to what they were before the last tune")
    return done


def status(hw=None):
    """Is a (re-)tune due? -> {"tuned": bool, "needs_tune": bool, "reason": str, "profile": dict|None}"""
    prof = load_profile()
    if not prof:
        return {"tuned": False, "needs_tune": True, "reason": "never tuned on this machine", "profile": None}
    hw = hw or arch_hw.detect()
    if prof.get("fingerprint") != hw.get("fingerprint"):
        return {"tuned": True, "needs_tune": True, "reason": "the hardware changed since the last tune", "profile": prof}
    cfg = core.load_config()
    assigned = {m for m in (cfg.get("roles") or {}).values() if m} | ({cfg["default_model"]} if cfg.get("default_model") else set())
    missing = sorted(m for m in assigned if m not in (prof.get("models") or {}))
    if missing:
        return {"tuned": True, "needs_tune": True, "reason": "not tuned yet: " + ", ".join(missing), "profile": prof}
    age = (time.time() - prof.get("ts", 0)) / 86400
    return {"tuned": True, "needs_tune": False, "reason": f"tuned {age:.1f} days ago for this hardware", "profile": prof}


SWEEP_MIN_GAIN = 1.10          # adopt a hardware setting only when it is at least 10% faster than the default
DEFAULT_BATCH = 512            # Ollama's own default


def _timed(name, ctx, chars, extra, npred=24):
    """One call with extra options -> (prefill_tok_s, gen_tok_s) or None when the server refuses or fails."""
    opts = dict({"num_ctx": ctx, "num_predict": npred, "temperature": 0}, **extra)
    try:
        r = core.stream_chat(name, [{"role": "user", "content": _prompt(chars)}], opts, False, "10m", {"first": 900, "idle": 300, "total": 1200})
    except core.LLMError:
        return None
    return (r.get("prefill_tok_s") or 0), (r.get("tok_s") or 0)


def sweep(name, ctx, hw, gpu_pct, say=print, timed=None):
    """Measure the knobs that depend on this machine: prompt batch size (reading speed) and CPU threads (only when the CPU does part of the work).
    -> {"num_batch": n|None, "num_thread": n|None, "results": {...}}. A value is returned only when it beats the default by SWEEP_MIN_GAIN."""
    timed = timed or (lambda extra, chars=9000: _timed(name, ctx, chars, extra))
    out = {"num_batch": None, "num_thread": None, "results": {}}
    base = timed({"num_batch": DEFAULT_BATCH})
    if base is None or not base[0]:
        say(f"    sweep skipped for {name}: the model did not report its prompt speed")
        return out
    out["results"]["batch"] = {DEFAULT_BATCH: round(base[0], 1)}
    best_b, best_v = DEFAULT_BATCH, base[0]
    for nb in (256, 1024):
        r = timed({"num_batch": nb})
        if r and r[0]:
            out["results"]["batch"][nb] = round(r[0], 1)
            if r[0] > best_v:
                best_b, best_v = nb, r[0]
    if best_b != DEFAULT_BATCH and best_v >= base[0] * SWEEP_MIN_GAIN:
        out["num_batch"] = best_b
        say(f"    {name}: prompt batch {best_b} reads {round(100 * (best_v / base[0] - 1))}% faster than the default {DEFAULT_BATCH} ({round(base[0])} -> {round(best_v)} tok/s)")
    else:
        say(f"    {name}: the default prompt batch ({DEFAULT_BATCH}) is already within 10% of the best; unchanged")
    cores = hw.get("cpu_cores") or 0
    cpu_involved = (not hw.get("vram_gb")) or (gpu_pct is not None and gpu_pct < 100)
    if cpu_involved and cores >= 4:
        base_g = timed({"num_batch": out["num_batch"] or DEFAULT_BATCH}, 3000)
        out["results"]["threads"] = {}
        best_t, best_g = None, (base_g[1] if base_g else 0)
        ref = best_g
        for nt in sorted({max(1, cores // 2), max(1, cores - 2)}):
            r = timed({"num_batch": out["num_batch"] or DEFAULT_BATCH, "num_thread": nt}, 3000)
            if r and r[1]:
                out["results"]["threads"][nt] = round(r[1], 1)
                if r[1] > best_g:
                    best_t, best_g = nt, r[1]
        if best_t and ref and best_g >= ref * SWEEP_MIN_GAIN:
            out["num_thread"] = best_t
            say(f"    {name}: {best_t} CPU threads generate {round(100 * (best_g / ref - 1))}% faster than the automatic choice")
    return out


def tune(models=None, probe=True, apply=True, deep=False, kv="f16", say=print, hw=None):
    _wire()
    hw = hw or arch_hw.detect()
    for n in hw.get("notes", []):
        say(f"  note: {n}")
    say(f"[1/4] hardware: {hw.get('gpu') or 'no GPU'}  {hw['vram_gb']:.1f} GB VRAM, {hw['ram_gb']:.1f} GB RAM, {hw.get('cpu_cores')} CPU threads, "
        f"{(hw.get('disk_free_gb') or 0):.0f} GB free for models")
    try:
        rows = mh.model_list(hw, kv, 192.0, 50.0)
    except mh.OllamaError as e:
        raise TuneError(e.msg)
    usable = {r["name"]: r for r in rows if not r["embed"]}
    if not usable:
        raise TuneError("No chat models are installed yet. Run `architect.py ready` (or `catalog`) to pick and download one.")
    cfg = mh.read_config()
    changes, warnings = [], []
    assigned = {r: m for r, m in (cfg.get("roles") or {}).items() if m and m in usable}
    default = cfg.get("default_model") if cfg.get("default_model") in usable else None
    auto_team = False
    if not assigned and not models:
        if default:
            say(f"  every role uses your default model {default}: leaving that as it is (Team > Roles > Apply recommended team builds a team if you want one)")
        else:
            _, plan = roles.recommend(rows)
            assigned, auto_team = dict(plan["assignment"]), True
            say(f"  no model and no team chosen yet: proposing the recommended one ({len(set(assigned.values()))} model(s)); only empty roles are filled")
    names = [m for m in (models or sorted(set(assigned.values()) | ({cfg["default_model"]} if cfg.get("default_model") in usable else set()))) if m in usable]
    if not names:
        raise TuneError("None of the models you named is installed.")
    builder = assigned.get("generator") or cfg.get("default_model") or names[0]
    stats = arch_tune.load_stats()
    cat = arch_catalog.load()
    prof_models, ratios = {}, []

    say(f"[2/4] fit and measure: {', '.join(names)}")
    for name in names:
        row = usable[name]
        total_b, active_b, eff_b, c = _sizes(row, cat)
        tier, why = arch_profiles.tier_for(name, total_b, active_b, _scores(row))
        rec = row["rec"]
        fit_ctx = rec.get("num_ctx")
        ctx_cap = min(CTX_CAP[tier], row.get("ctx_max") or 10 ** 9)
        ctx = max(4096, min(fit_ctx or 4096, ctx_cap))
        fast_rows = [r for r in row["fit"]["rows"] if r["fits_ram"] and r["gpu_ratio"] >= 0.9 and r["ctx"] <= ctx_cap]
        grow_max = max([ctx] + [r["ctx"] for r in fast_rows])
        entry = {"tier": tier, "tier_why": why, "moe": bool(active_b and total_b and active_b < total_b), "active_b": active_b, "ctx": ctx, "ctx_max_fast": grow_max, "fit_verdict": rec["verdict"], "est_tok_s": rec.get("est_tok_s")}
        if rec["verdict"] == "too_big":
            warnings.append(f"{name} does not fit this machine (needs more than RAM + VRAM): pick a smaller model or quant.")
        elif ctx < MIN_USEFUL_CTX:
            warnings.append(f"{name} only fits {ctx} tokens of context here: too small for reliable agent loops (aim for 8192+).")
        if rec["verdict"] in ("split", "slow"):
            warnings.append(f"{name}: {rec['text']}")
        q8 = mh.recommend(mh.fit_model(int(row["size_gb"] * mh.GB), row["arch"], hw, "q8_0"), hw)
        if q8.get("num_ctx") and q8["num_ctx"] > ctx and ctx < ctx_cap:
            entry["kv_tip"] = (f"A quantised KV cache (server settings OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0) would fit about "
                               f"{min(q8['num_ctx'], ctx_cap)} tokens instead of {ctx}. Check output quality on your own tasks first: it is not free.")
        if probe:
            m = _probe(name, ctx, deep, hw, stats, say)
            entry["measured"] = m
            if m.get("error"):
                warnings.append(f"{name}: the probe call failed ({m['error']})")
            else:
                say(f"    {name}: {m['gen_tok_s']} tok/s generating, {m.get('prefill_tok_s') or '?'} tok/s reading the prompt, "
                    f"{m.get('gpu_pct', '?')}% on GPU, load {m.get('load_s') or 0}s" + ("" if m["canary_ok"] else "  [canary NOT repeated]"))
                if not m["canary_ok"]:
                    warnings.append(f"{name} did not repeat a fact from the start of the prompt: it may be ignoring early instructions (small model, or the window is too tight).")
                if m.get("gpu_pct") is not None and hw.get("vram_gb") and m["gpu_pct"] < 100:
                    warnings.append(f"{name} is only {m['gpu_pct']}% on the GPU at {ctx} tokens: part runs on the CPU and is much slower. Lower the context or pick a smaller quant.")
                if m["gen_tok_s"] and m["gen_tok_s"] < arch_profiles.SLOW_TOK_S:
                    warnings.append(f"{name} generates only {m['gen_tok_s']} tok/s here: sampled retries and per-sprint judging are switched off for it.")
                if m["gen_tok_s"] and rec.get("est_tok_s"):
                    ratios.append(m["gen_tok_s"] / rec["est_tok_s"])
                if deep and name == builder:                       # the hardware sweep is only for the model that does the building
                    say(f"    measuring batch size and threads for {name} (a few extra calls)")
                    entry["sweep"] = sweep(name, ctx, hw, m.get("gpu_pct"), say)
        prof_models[name] = entry
    if probe:
        arch_tune.save_stats(stats)

    # ---- what to write
    say("[3/4] settings")
    b_entry = prof_models.get(builder) or next(iter(prof_models.values()))
    b_tok = (b_entry.get("measured") or {}).get("gen_tok_s")
    tier = b_entry["tier"]
    patch = arch_profiles.settings_for(tier, b_tok)
    patch["limits.num_ctx"] = b_entry["ctx"]
    patch["limits.num_ctx_max"] = max(b_entry["ctx"], b_entry["ctx_max_fast"])
    in_use = set(assigned.values()) | {builder}
    patch["limits.keep_alive"] = "30m" if len(in_use) <= 1 else "5m"
    worst = 0.0
    for name, e in prof_models.items():
        t = arch_tune.timeouts_for(stats.get(name), int(e["ctx"] * 0.8), e["ctx"] // 2, 7200, first_call=True)
        e["timeouts_at_full_context"] = t
        worst = max(worst, t["total"])
    patch["limits.request_timeout"] = int(min(7200, max(300, -(-worst // 60) * 60)))
    coerced = params.validate_settings_patch(patch)
    scale = round(sum(ratios) / len(ratios), 2) if ratios else None
    undo = {"settings": {}, "overrides": {}, "roles": {}, "created": [k for k in ("model_overrides", "roles", "limits", "behavior") if k not in cfg]}
    if apply:
        for path_, v in coerced.items():
            d, parts = cfg, path_.split(".")
            for part in parts[:-1]:
                d = d.setdefault(part, {})
            undo["settings"][path_] = {"had": parts[-1] in d, "value": d.get(parts[-1])}
            if d.get(parts[-1]) != v:
                changes.append(f"{path_}: {d.get(parts[-1])!r} -> {v!r}")
            d[parts[-1]] = v
        cfg.setdefault("model_overrides", {})
        for name, e in prof_models.items():
            had = name in cfg["model_overrides"]
            undo["overrides"][name] = {"had": had, "value": json.loads(json.dumps(cfg["model_overrides"].get(name)))}
            o = dict(cfg["model_overrides"].get(name) or {})
            before = o.get("num_ctx")
            params.apply_override(o, "num_ctx", e["ctx"])
            sw = e.get("sweep") or {}
            for hk in ("num_batch", "num_thread"):
                if sw.get(hk):
                    params.apply_override(o, hk, sw[hk])
                    changes.append(f"model {name}: {hk} -> {sw[hk]} (measured faster than the default)")
            cfg["model_overrides"][name] = o
            if before != e["ctx"]:
                changes.append(f"model {name}: context {before} -> {e['ctx']}")
        if auto_team:
            ranked_names = sorted(names, key=lambda n: -(roles.score_for_role("generator", usable[n])[0]))
            proposal = arch_profiles.crew_assignment(assigned, tier, ranked_names)
            cur = cfg.setdefault("roles", {})
            filled = {r: m for r, m in proposal.items() if not cur.get(r)}      # only EMPTY roles: nothing you chose is overwritten
            undo["roles"] = {r: cur.get(r) for r in filled}
            cur.update(filled)
            changes.append(f"team: filled {len(filled)} empty role(s) with the recommended {arch_profiles.INFO[tier]['crew']} ({len(set(filled.values()))} model(s))")
        mh.write_config(cfg)
    pw = hw.get("power") or {}
    if pw.get("present") and pw.get("on_battery"):
        warnings.append("This tune ran on battery: laptops usually lower clocks then, so the speeds and timeouts are conservative. Run `tune` again plugged in for the speed you get on AC.")
    prof = {"version": VERSION, "ts": time.time(), "fingerprint": hw["fingerprint"],
            "hw": {k: hw.get(k) for k in ("gpu", "vendor", "vram_gb", "ram_gb", "cpu_cores", "disk_free_gb", "unified")},
            "builder": builder, "tier": tier, "tier_label": arch_profiles.INFO[tier]["label"], "crew": arch_profiles.INFO[tier]["crew"],
            "tier_why": b_entry["tier_why"], "models": prof_models, "settings": coerced, "applied": bool(apply), "changes": changes,
            "warnings": warnings, "speed_scale": scale, "power": hw.get("power"), "undo": undo if apply else None}
    os.makedirs(core.data_dir(), exist_ok=True)
    with open(profile_path(), "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=1)
    say(f"[4/4] profile: {prof['tier_label']} -> {prof['crew']} crew, {b_entry['ctx']} tokens of context, builder {builder}" +
        (f", about {b_tok} tok/s" if b_tok else ""))
    for c in changes:
        say(f"  changed  {c}")
    if not apply:
        say("  (--no-apply: nothing was written to your settings)")
    for w in warnings:
        say(f"  WARNING  {w}")
    for e in prof_models.values():
        if e.get("kv_tip"):
            say(f"  tip      {e['kv_tip']}")
    return prof


# ================================================================== the "improve my worker" plan
# One ordered list a person can follow (or the hub can show): measure today's worker, export what this machine verified, look at it,
# train, convert, measure the candidate the same way, switch over only on no regression. Every step names its command and what it needs;
# every time and size says whether it was measured here or is an estimate. Nothing here runs a step.

PLAN_MIN_QUALITY = 0.6         # first-try or second-try, not cut off: arch_dataset.quality_score puts those at 0.7-1.0
TRAIN_TOK_S_GUESS = 400.0      # community estimate for a 4B QLoRA on a 6 GB card; replaced by train_stats.json after one real run
CONVERT_MIN_GUESS = 10
MIN_TRAIN_SAMPLES = 50         # the same floor train_lora.py refuses below
# A base checkpoint must be the SAME model the worker runs, or the adapter does not fit it. Only well-known pairs; anything else asks.
BASES = [("qwen2.5-coder:7b", "unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit"), ("qwen2.5-coder:3b", "unsloth/Qwen2.5-Coder-3B-Instruct-bnb-4bit"),
         ("qwen2.5-coder:1.5b", "unsloth/Qwen2.5-Coder-1.5B-Instruct-bnb-4bit"), ("qwen3:4b", "unsloth/Qwen3-4B-Instruct-2507"),
         ("qwen3:8b", "unsloth/Qwen3-8B-bnb-4bit"), ("llama3.2:3b", "unsloth/Llama-3.2-3B-Instruct-bnb-4bit"),
         ("llama3.1:8b", "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit")]
DEFAULT_BASE = "unsloth/Qwen3-4B-Instruct-2507"


def base_for(worker):
    """-> (base checkpoint, known: bool) for the worker model name."""
    w = (worker or "").lower()
    for tag, base in BASES:
        if w.startswith(tag):
            return base, True
    return DEFAULT_BASE, False


def _base_b(base):
    m = re.search(r"(\d+(?:\.\d+)?)B", base or "", re.I)
    return float(m.group(1)) if m else 4.0


def _cli_has(flag):
    """Does architect.py accept this flag yet? (The dataset export may gain options before its command line does.)"""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "architect.py"), encoding="utf-8") as f:
            return f'"{flag}"' in f.read()
    except OSError:
        return False


def _dataset_size(path, min_quality):
    """-> (usable samples, tokens) after train_lora's own curation, or (0, 0) when there is no file."""
    try:
        import train_lora
        rows, _broken = train_lora.read_jsonl(path)
        cur = train_lora.curate(rows, min_quality, curriculum=True)
        return cur["unique"], cur["tokens"]
    except (OSError, ImportError):
        return 0, 0


def _gpu_free_gb(live=None):
    if live is None:
        try:
            live = arch_hw.live()
        except Exception:
            live = {}
    if live and live.get("vram_gb") is not None and live.get("vram_used_gb") is not None:
        return round(float(live["vram_gb"]) - float(live["vram_used_gb"]), 1)
    return None


def _disk_free_gb(path):
    import shutil
    p = path
    while p and not os.path.isdir(p):
        p = os.path.dirname(p)
    try:
        return round(shutil.disk_usage(p or ".").free / 1024 ** 3, 1)
    except OSError:
        return None


def tune_plan(cfg=None, data_dir=None, worker=None, live=None, disk_free_gb=None, last_eval=None, baseline=None,
              train_stats=None, servers_up=None, min_quality=PLAN_MIN_QUALITY, candidate="my-worker"):
    """The "improve my worker" plan. -> {"worker", "base", "samples", "steps": [...], "next", "lines"}.

    Each step: {"id", "title", "words", "commands": [...], "needs": {"gpu", "gpu_free_gb", "disk_gb", "minutes", "evidence"},
    "state": "done" | "ready" | "blocked" | "after", "why"}. Order: baseline, export, check, train, convert, eval, promote.
    Every argument can be passed in (the self-test does); left out, it is read from this machine. Runs nothing."""
    cfg = cfg if cfg is not None else core.load_config()
    dd = data_dir or core.data_dir()
    worker = worker or (cfg.get("tiers") or {}).get("worker") or (cfg.get("roles") or {}).get("generator") or cfg.get("default_model") or ""
    base, known = base_for(worker)
    b = _base_b(base)
    ds_path = os.path.join(dd, "datasets", "tier-worker.sft.jsonl")
    out_dir = os.path.join(dd, "lora_out")
    if train_stats is None:
        try:
            import train_lora
            train_stats = train_lora.load_stats(out_dir)
        except ImportError:
            train_stats = {}
    if baseline is None or last_eval is None:
        try:
            import arch_eval
            baseline = arch_eval.latest(label="baseline") if baseline is None else baseline
            last_eval = arch_eval.latest(fake=False) if last_eval is None else last_eval
        except Exception:
            baseline, last_eval = baseline or {}, last_eval or {}
    gpu_free = _gpu_free_gb(live)
    disk = disk_free_gb if disk_free_gb is not None else _disk_free_gb(dd)
    if servers_up is None:
        try:
            import arch_server
            servers_up = [p["tier"] for p in arch_server.status(cfg).get("processes", []) if p.get("up")]
        except Exception:
            servers_up = []
    n, tokens = _dataset_size(ds_path, min_quality)

    eval_min = max(1, round(float(last_eval["secs"]) / 60)) if (last_eval or {}).get("secs") else None
    eval_ev = "measured (your last live eval)" if eval_min is not None else "unknown until the first live eval"
    tps = (train_stats or {}).get("tok_per_s")
    epochs = 2.0
    train_min = max(1, round(tokens * epochs / (tps or TRAIN_TOK_S_GUESS) / 60)) if tokens else None
    train_ev = "measured (your last training run)" if tps else f"estimate ({int(TRAIN_TOK_S_GUESS)} tokens/s, community QLoRA reports)"
    train_vram = round(b * 0.6 + 3.0, 1)          # 4-bit weights + LoRA state + activations at batch 1 (community: ~6 GB for 4B)
    train_disk = round(b * 0.6 + b * 2.0 + b * 0.6 + 1.0, 1)     # the 4-bit base, the f16 merge for GGUF, the q4 GGUF, the adapter
    lora_cmd = f"python train_lora.py --base {base} --min-quality {min_quality:g} --curriculum --weight repeat"
    steps = []

    def step(id_, title, words, commands, needs, state, why=""):
        steps.append({"id": id_, "title": title, "words": words, "commands": commands, "needs": needs, "state": state, "why": why})

    step("baseline", "Measure today's worker",
         f"Run the fixed task set with {worker or 'your current worker'} so there is something to beat. Skipped when a 'baseline' exists.",
         ["python architect.py eval --quick --label baseline"],
         {"gpu": "the worker's server", "gpu_free_gb": None, "disk_gb": 0.1, "minutes": eval_min, "evidence": eval_ev},
         "done" if baseline else ("ready" if worker else "blocked"),
         "" if (baseline or worker) else "no worker model is set: run `python architect.py port --apply` first")
    step("export", "Export what this machine verified",
         "Every reply the code checks accepted, one sample per task (near-duplicates keep the best), scored for quality and tagged by "
         "difficulty. Cloud replies and replies whose checks could not run are never exported.",
         ["python architect.py dataset" + (f" --min-quality {min_quality:g}" if _cli_has("--min-quality") else "")],
         {"gpu": "none", "gpu_free_gb": None, "disk_gb": 0.1, "minutes": 1, "evidence": "estimate (reads traces, writes JSONL)"},
         "ready")
    step("check", "Look at what would be trained on",
         "A dry run: counts, the quality floor, the easy-to-hard order, and what was left out and why. No GPU, nothing written.",
         [lora_cmd + " --dry-run"],
         {"gpu": "none", "gpu_free_gb": None, "disk_gb": 0.0, "minutes": 1, "evidence": "estimate"},
         "ready" if n else "after", "" if n else "after the export")
    blocked = []
    if n < MIN_TRAIN_SAMPLES:
        blocked.append(f"{n} usable samples; at least {MIN_TRAIN_SAMPLES} are needed (keep using the app on real work)")
    if gpu_free is not None and gpu_free < train_vram and not servers_up:
        blocked.append(f"{gpu_free} GB of graphics memory is free, about {train_vram} GB is needed")
    if disk is not None and disk < train_disk:
        blocked.append(f"{disk} GB of disk is free, about {train_disk} GB is needed")
    if not known:
        blocked.append(f"no known training checkpoint for '{worker or '?'}': pass --base with the exact model your worker runs")
    step("train", "Train a LoRA adapter",
         "Teaches your worker this project's conventions and the reply format, from your verified work only. The model servers must be "
         "stopped so the card is free" + (f" (running now: {', '.join(servers_up)})" if servers_up else "") + ".",
         (["python architect.py server stop"] if servers_up else []) + [lora_cmd],
         {"gpu": "free: training needs the whole card", "gpu_free_gb": train_vram, "disk_gb": train_disk, "minutes": train_min,
          "evidence": train_ev + "; memory and disk are estimates"},
         "blocked" if blocked else "ready", "; ".join(blocked))
    step("convert", "Make it a model this app can run",
         f"Write a q4_k_m GGUF of the trained model and register it as '{candidate}'. Your current worker is kept.",
         [f"python train_lora.py --export-only --name {candidate}", f"cd {out_dir} && ollama create {candidate} -f Modelfile"],
         {"gpu": "free", "gpu_free_gb": train_vram, "disk_gb": round(b * 2.6, 1), "minutes": CONVERT_MIN_GUESS, "evidence": "estimate"},
         "after", "after the training")
    step("eval", "Measure the candidate the same way",
         f"Set the worker tier to '{candidate}' for this run only (Models page), run the same task set, then set it back. "
         "The --compare part prints flow by flow and exits 1 on any regression.",
         ["python architect.py server start", "python architect.py eval --quick --label candidate --compare baseline"],
         {"gpu": "the worker's server", "gpu_free_gb": None, "disk_gb": 0.1, "minutes": eval_min, "evidence": eval_ev},
         "after", "after the conversion")
    step("promote", "Switch over only if nothing got worse",
         "Refuses unless the candidate is no worse than the baseline: pass rate, tokens and time per flow. Nothing is deleted; the old "
         "worker stays installed.",
         [f"python architect.py promote-model --tier worker --model {candidate} --compare baseline"],
         {"gpu": "none", "gpu_free_gb": None, "disk_gb": 0.0, "minutes": 1, "evidence": "estimate"},
         "after", "only when the eval shows no regression")
    nxt = next((s for s in steps if s["state"] in ("ready", "blocked")), None)
    lines = [f"Improve the worker ({worker or 'not set'}) from {n} verified sample(s), training from {base}" +
             ("" if known else " (a guess: pick the checkpoint your worker really is)") + "."]
    for i, s in enumerate(steps, 1):
        nd = s["needs"]
        bits = [f"{nd['minutes']} min" if nd.get("minutes") is not None else "time unknown"]
        if nd.get("gpu_free_gb"):
            bits.append(f"{nd['gpu_free_gb']} GB graphics memory free")
        if nd.get("disk_gb"):
            bits.append(f"{nd['disk_gb']} GB disk")
        lines.append(f"{i}. {s['title']} [{s['state']}] - {s['words']}")
        lines.append(f"   needs: {', '.join(bits)} ({nd['evidence']})" + (f"; {s['why']}" if s["why"] else ""))
        lines.extend(f"   $ {c}" for c in s["commands"])
    return {"worker": worker, "base": base, "base_known": known, "samples": n, "tokens": tokens, "gpu_free_gb": gpu_free,
            "disk_free_gb": disk, "steps": steps, "next": nxt["id"] if nxt else None, "lines": lines}


# ================================================================== best settings for this PC
# What the measurements already on disk say, in one place the hub can show. Reads only; measures nothing unless measure_variants is
# called. Each item carries its evidence: "measured" (this machine), "plan" (the fit math / the server planner), "rule" or "none".

def variants_path():
    return os.path.join(core.data_dir(), "tune_variants.json")


def measure_variants(cfg=None, hw=None, say=print):
    """Restart the worker with each server variant (n-gram vs draft, KV type) and keep the measured rows for best_settings.
    Stops the worker's server while it runs, so call it from `tune`, never mid-run."""
    import arch_server
    cfg = cfg or core.load_config()
    hw = hw or arch_hw.detect()
    rows = arch_server.bench_variants(cfg, hw, "worker", say=say)
    try:
        core._atomic_write(variants_path(), json.dumps({"ts": time.time(), "fingerprint": hw.get("fingerprint"), "rows": rows}, indent=1))
    except (OSError, AttributeError):
        pass
    return rows


def best_settings(profile=None, plan=None, variants=None, hw=None):
    """-> {"worker", "speedup", "kv", "threads", "lines", "missing"}; each item has "value", "why" and "evidence".
    Arguments are read from this machine when left out (the self-test passes them in)."""
    hw = hw if hw is not None else arch_hw.detect()
    if profile is None:
        profile = load_profile() or {}
    if plan is None:
        try:
            import arch_server
            plan = arch_server.plan(None, hw)
        except Exception:
            plan = {}
    if variants is None:
        v = core.load_json(variants_path()) or {}
        variants = v.get("rows") if v.get("fingerprint") in (None, hw.get("fingerprint")) else []
    variants = [r for r in (variants or []) if r.get("gen_tok_s")]
    models = profile.get("models") or {}
    wp = next((p for p in (plan.get("processes") or []) if p.get("tier") == "worker"), {})
    missing, out = [], {}

    # ---- worker: the fastest measured model that ran fully on the card, at the largest window that kept it there
    on_card = [(n, e) for n, e in models.items() if (e.get("measured") or {}).get("gen_tok_s")
               and (e.get("measured") or {}).get("gpu_pct") == 100]
    if on_card:
        n, e = max(on_card, key=lambda ne: ne[1]["measured"]["gen_tok_s"])
        ctx = max(e.get("ctx") or 0, e.get("ctx_max_fast") or 0)
        out["worker"] = {"value": n, "ctx": ctx, "full_gpu": True, "gen_tok_s": e["measured"]["gen_tok_s"], "evidence": "measured",
                         "why": f"the fastest model measured entirely on the card here ({e['measured']['gen_tok_s']} tok/s); "
                                f"{ctx} tokens is the largest window the fit math keeps on it"}
    elif wp:
        out["worker"] = {"value": wp.get("name"), "ctx": wp.get("ctx"), "full_gpu": bool(wp.get("full_gpu")), "gen_tok_s": None,
                         "evidence": "plan", "why": "the server planner's pick; " + ("all layers on the card" if wp.get("full_gpu")
                                                                                     else f"{wp.get('ngl')} layers on the card, the rest in RAM")}
        missing.append("a measured speed: run `python architect.py tune`")
    else:
        out["worker"] = {"value": None, "ctx": None, "full_gpu": False, "gen_tok_s": None, "evidence": "none",
                         "why": "no worker is set up yet"}
        missing.append("a worker: run `python architect.py port --apply`, then `tune`")
    out["worker"]["quant_file"] = (os.path.basename(wp.get("gguf") or "") or None) if wp and out["worker"]["value"] == wp.get("name") else None

    # ---- speculative decoding: what was measured beats what the planner chose
    def kind(row):
        if "draft_used" in row:                  # what the server really loaded
            return "draft" if row.get("draft_used") else "ngram"
        return "draft asked for" if (row.get("variant") or {}).get("draft") else "ngram"     # an older row: whether one loaded is not recorded
    if variants:
        best = max(variants, key=lambda r: r["gen_tok_s"])
        other = [r["gen_tok_s"] for r in variants if kind(r) != kind(best)]
        out["speedup"] = {"value": kind(best), "gen_tok_s": best["gen_tok_s"], "evidence": "measured",
                          "why": f"{best['gen_tok_s']} tok/s with " + {"draft": f"the draft model {best.get('draft_used')}",
                                                                       "ngram": "n-gram lookup",
                                                                       "draft asked for": "a draft model asked for (whether one loaded was not recorded)"}[kind(best)] +
                                 (f" vs {max(other)} tok/s with the other" if other else "")}
        kv_best = best.get("kv_used") or (best.get("variant") or {}).get("kv")
        out["kv"] = ({"value": kv_best, "evidence": "measured", "why": "the cache type of the fastest variant"} if kv_best else
                     {"value": None, "evidence": "none", "why": "the fastest variant does not say which cache type it used"})
    else:
        args = " ".join(str(a) for a in (wp.get("args") or []))
        val = "draft" if wp.get("draft") else ("ngram" if ("ngram" in args or "lookup-cache" in args) else "none")
        out["speedup"] = {"value": val, "gen_tok_s": None, "evidence": "plan" if wp else "none",
                          "why": {"draft": f"the planner pairs the worker with {wp.get('draft')} (same tokenizer)",
                                  "ngram": "n-gram lookup: costs no memory, and repeated code in edits is where it pays",
                                  "none": "this server build offers no speculative decoding, or the worker runs through Ollama"}[val]}
        out["kv"] = {"value": wp.get("kv") or "f16", "evidence": "plan" if wp else "none", "why": "the server planner's cache type"}
        missing.append("n-gram vs draft, measured: arch_autotune.measure_variants (restarts the worker)")

    # ---- CPU threads: a measured sweep, else one per performance core
    swept = next((e["sweep"]["num_thread"] for e in models.values() if (e.get("sweep") or {}).get("num_thread")), None)
    if swept:
        out["threads"] = {"value": swept, "router": max(1, swept // 2), "evidence": "measured",
                          "why": f"{swept} threads generated at least 10% faster in the tune sweep"}
    else:
        try:
            pc = arch_hw.perf_cores()
        except Exception:
            pc = None
        pc = pc or hw.get("cpu_cores")
        out["threads"] = {"value": pc, "router": max(1, (pc or 2) // 2), "evidence": "rule",
                          "why": "one thread per performance core is fastest when the CPU does part of the work; the router gets half"}
    out["missing"] = missing
    w, s, t = out["worker"], out["speedup"], out["threads"]
    out["lines"] = [
        f"Worker: {w['value'] or 'not set'}" + (f" at {w['ctx']} tokens" if w.get("ctx") else "") +
        (", fully on the card" if w.get("full_gpu") else "") + f" - {w['why']} ({w['evidence']}).",
        f"Speed-up: {s['value']} - {s['why']} ({s['evidence']}).",
        f"KV cache: {out['kv']['value']} ({out['kv']['evidence']}).",
        f"CPU threads: {t['value']} - {t['why']} ({t['evidence']}).",
    ] + [f"Not measured yet: {m}" for m in missing]
    return out
