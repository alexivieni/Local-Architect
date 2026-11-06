"""Which value each setting really has for a role, and which layer set it.

    effective_settings(cfg, "generator") -> {"role", "model", "source", "keys": {key: {"value", "layer", "why"}}}

Layers, strongest first: explicit arg > person role profile (your role/model overrides) > preset > shipped role profile >
model family default > built-in default. When `arch_roleprofile` exists and offers `effective(cfg, role, explicit)` it is the one
source of truth (guarded import); otherwise this mirrors the engine's current resolution (`Engine._model_for` and `Engine._call`).

One honest difference from the list above: with `behavior.model_defaults = auto` the engine lets the model family's sampling fill
every key YOU left unset, which includes keys the shipped role profile sets. The answer reports what the engine really does.
"""
import copy

EXPLICIT, PERSON, PRESET, ROLE, FAMILY, BUILTIN = ("explicit arg", "person role profile", "preset", "shipped role profile",
                                                  "model family default", "built-in default")
LAYERS = [EXPLICIT, PERSON, PRESET, ROLE, FAMILY, BUILTIN]
# global settings worth showing next to a role (all set in limits/behavior, so a preset can touch them)
GLOBAL_KEYS = ("limits.max_calls", "limits.max_revisions", "limits.request_timeout", "behavior.evaluator", "behavior.best_of_n",
               "behavior.mutation", "behavior.network", "behavior.review_mode", "behavior.model_defaults", "behavior.seed")


def _get(d, path):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _model(cfg, role, roles_mod):
    m = ((cfg.get("roles") or {}).get(role) or "").strip()
    tiers = cfg.get("tiers") or {}

    def tier(t):
        for x in {"heavy": ("heavy", "worker"), "router": ("router", "worker")}.get(t, (t,)):
            if (tiers.get(x) or "").strip():
                return tiers[x].strip()
        return ""
    if m:
        return ((tier(m[5:]) or m) if m.startswith("tier:") else m), "assigned to this role"
    t = ((roles_mod.ROLES.get(role) or {}).get("tier") if roles_mod else None) or "worker"
    if tier(t):
        return tier(t), f"the role's own tier ({t})"
    for r in (getattr(roles_mod, "FALLBACK", {}) or {}).get(role, []):
        mm = ((cfg.get("roles") or {}).get(r) or "").strip()
        if mm:
            return mm, f"borrowed from the {r} role"
    if cfg.get("default_model"):
        return cfg["default_model"], "the default model"
    return "", "none set: the first installed model is used"


def _preset_owner(cfg, role, key, value):
    """The name of the preset whose role profile holds exactly this value for this role/key (only presets you applied, the last one first), or ''."""
    try:
        import arch_roleprofile as rp
        names = []
        try:
            names = [r["what"][7:] for r in reversed(rp._load_undo()) if str(r.get("what", "")).startswith("preset ")]
        except Exception:
            pass
        ps = rp.presets()
        for n in names:
            if ((ps.get(n.lower()) or {}).get("role_profiles") or {}).get(role, {}).get(key, object()) == value:
                return n
    except Exception:
        pass
    return ""


def _overlay_profile(cfg, role, keys, put, ro, mo, pname=""):
    """With behavior.role_profiles = on, the engine applies arch_roleprofile.resolve() over the older sampling: report those values
    under their real layer. A SHIPPED value never overrides a key set in your older role/model overrides (arch_roleprofile.apply_call)."""
    try:
        import arch_roleprofile as rp
        prof = rp.resolve(cfg, role)
    except Exception:
        return
    if not prof:
        return
    mine_old = set(ro.get("sampling") or {}) | set((mo.get("sampling") or {}))
    names = {"thinking": "think", "reply_max": "num_predict", "ctx_max": "ctx_max"}
    for k, v in prof["values"].items():
        src = prof["source"].get(k)
        key = names.get(k, k)
        if k == "budget":
            for bk, bv in v.items():
                s = src.get(bk)
                put("budget." + bk, bv, PERSON if s == "person" else ROLE, "role_profiles" if s == "person" else "data/role_profiles.json")
            continue
        if src == "shipped" and ((k in ("temperature", "top_p") and k in mine_old) or (k == "thinking" and "think" in ro | mo)
                                 or (k == "reply_max" and ("num_predict" in ro or "num_predict" in mo))):
            continue
        val = {"on": True, "off": False}.get(v, v) if k == "thinking" else v
        if src == "person":
            owner = _preset_owner(cfg, role, k, v)
            put(key, val, PRESET if owner else PERSON, f"preset {owner}" if owner else "your role profile")
        else:
            put(key, val, ROLE, "data/role_profiles.json")


def effective_settings(cfg, role, explicit=None, preset=None, family=None):
    """-> {"role", "model", "model_why", "source", "keys": {key: {"value", "layer", "why"}}, "layers"}.
    `family` = a {sampling:{...}, thinking: bool} dict to use instead of asking arch_modelknow (tests)."""
    explicit = dict(explicit or {})
    try:
        import arch_roleprofile
        if hasattr(arch_roleprofile, "effective"):
            got = arch_roleprofile.effective(cfg, role, explicit)
            if isinstance(got, dict) and got.get("keys"):
                got.setdefault("source", "arch_roleprofile")
                got.setdefault("layers", LAYERS)
                return got
    except Exception:
        pass
    try:
        import arch_core as core
        defaults = core.DEFAULTS
    except Exception:
        defaults = {}
    try:
        import arch_roles as roles_mod
    except Exception:
        roles_mod = None
    cfg = copy.deepcopy(cfg or {})
    spec = (roles_mod.ROLES.get(role) if roles_mod else None) or {}
    if roles_mod and role not in roles_mod.ROLES:
        raise ValueError(f"no such role: {role}")
    model, model_why = _model(cfg, role, roles_mod)
    keys = {}

    def put(k, v, layer, why=""):
        keys[k] = {"value": v, "layer": layer, "why": why}
    ro = (cfg.get("role_overrides") or {}).get(role) or {}
    mos = cfg.get("model_overrides") or {}
    mo = (mos.get(model) or mos.get(model.lower()) or mos.get(model.split(":")[0]) or {}) if model else {}
    # model
    if explicit.get("model"):
        put("model", explicit["model"], EXPLICIT)
    elif ((cfg.get("roles") or {}).get(role) or "").strip():
        put("model", model, PERSON, model_why)
    else:
        put("model", model, BUILTIN, model_why)
    # num_ctx
    if mo.get("num_ctx"):
        put("num_ctx", int(mo["num_ctx"]), PERSON, f"model override for {model}")
    else:
        lim = _get(cfg, "limits.num_ctx")
        put("num_ctx", lim, PERSON if lim != _get(defaults, "limits.num_ctx") else BUILTIN, "limits.num_ctx")
    # sampling: the engine's order
    put("temperature", 0.2, BUILTIN, "the engine's base temperature")
    for k, v in (spec.get("sampling") or {}).items():
        put(k, v, ROLE, f"arch_roles.ROLES[{role!r}]")
    mine = set(mo.get("sampling") or {}) | set(ro.get("sampling") or {})
    if (cfg.get("behavior") or {}).get("model_defaults", "off") == "auto" and not str(model).startswith("claude:"):
        fam = family
        if fam is None:
            try:
                import arch_jobs
                import arch_modelknow
                fam = arch_modelknow.settings_for(model, arch_jobs.job_of(role) or "code") or {}
            except Exception:
                fam = {}
        for k, v in (fam.get("sampling") or {}).items():
            if k not in mine and isinstance(v, (int, float)):
                put(k, v, FAMILY, f"family default ({fam.get('source') or 'arch_modelknow'})")
        if "think" not in ro and "think" not in mo and isinstance(fam.get("thinking"), bool):
            put("think", fam["thinking"], FAMILY, "family default")
    for k, v in (mo.get("sampling") or {}).items():
        put(k, v, PERSON, f"model override for {model}")
    for k, v in (ro.get("sampling") or {}).items():
        put(k, v, PERSON, f"role override for {role}")
    if "think" not in keys:
        put("think", spec.get("think"), ROLE, f"arch_roles.ROLES[{role!r}]")
    for src, why in ((mo, f"model override for {model}"), (ro, f"role override for {role}")):
        if "think" in src:
            put("think", src["think"], PERSON, why)
    npred = ro.get("num_predict") or mo.get("num_predict")
    put("num_predict", int(npred) if npred else "auto", PERSON if npred else BUILTIN,
        "" if npred else "fitted per call: half the window, at most 1536 for structured replies, 3072 for code")
    # global keys
    pname = preset or cfg.get("preset") or ""
    pvals = {}
    try:
        import arch_params
        pvals = (arch_params.PRESETS.get(pname) or {}).get("settings") or {}
    except Exception:
        pass
    for k in GLOBAL_KEYS:
        v, d = _get(cfg, k), _get(defaults, k)
        if k in pvals and v == pvals[k]:
            put(k, v, PRESET, f"preset {pname}")
        elif v != d:
            put(k, v, PERSON, "your settings")
        else:
            put(k, v, BUILTIN, "arch_core.DEFAULTS")
    _overlay_profile(cfg, role, keys, put, ro, mo, pname)
    for k, v in explicit.items():
        if k != "model":
            put(k, v, EXPLICIT, "passed for this call")
    return {"role": role, "model": model, "model_why": model_why, "source": "current resolution (arch_core.Engine)",
            "keys": keys, "layers": LAYERS}
