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
