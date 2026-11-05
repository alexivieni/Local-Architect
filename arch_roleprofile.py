"""Role profiles: per role, how big its prompt may be, how long its reply, whether it thinks, its sampling, the tools a tool loop may
give it, a per-run budget, and (recorded only) its speculation choice.

Precedence for every key: an explicit call argument > the person's role profile (settings `role_profiles`) > their older
role/model overrides (`role_overrides`, `model_overrides`, which a SHIPPED profile never overrides) > the shipped profile
(data/role_profiles.json) > the model family defaults (behavior.model_defaults) > what the code did before.

`behavior.role_profiles` = off (the default): resolve() answers None and nothing in the call path changes, byte for byte. It is off by
default because the shipped profile changes thinking, temperature and reply caps -- results would move without the person asking.
A preset (`architect.py preset Balanced`) turns it on, with an undo record.
"""
import copy
import fnmatch
import hashlib
import itertools
import json
import os
import re
import statistics
import time

import arch_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED_FILE = os.path.join(HERE, "data", "role_profiles.json")
PRESET_DIR = os.path.join(HERE, "data", "presets")
KEYS = ("model", "ctx_max", "reply_max", "thinking", "temperature", "top_p", "tools", "budget", "speculation")
BUDGET_KEYS = ("calls", "tokens", "seconds")
MAX_VARIANTS = 8
_SHIP = {}


# ------------------------------------------------------------------ data
def shipped():
    if "p" not in _SHIP:
        try:
            with open(SHIPPED_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        _SHIP["p"] = {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, dict)}
    return _SHIP["p"]


def mode(cfg):
    return ((cfg or {}).get("behavior") or {}).get("role_profiles", "off")


def resolve(cfg, role):
    """-> None when the feature is off, else {"role", "values": {key: v}, "source": {key: "person"|"shipped"}} (None values dropped)."""
    if mode(cfg) != "on":
        return None
    ship = shipped().get(role) or {}
    mine = ((cfg or {}).get("role_profiles") or {}).get(role) or {}
    vals, src = {}, {}
    for k in KEYS:
        if k == "budget":
            b, bs = {}, {}
            for bk in BUDGET_KEYS:
                for name, d in (("person", mine), ("shipped", ship)):
                    v = (d.get("budget") or {}).get(bk)
                    if v is not None:
                        b[bk], bs[bk] = v, name
                        break
            if b:
                vals["budget"], src["budget"] = b, bs
            continue
        if k in mine and mine[k] is not None:
            vals[k], src[k] = mine[k], "person"
        elif ship.get(k) is not None:
            vals[k], src[k] = ship[k], "shipped"
    return {"role": role, "values": vals, "source": src}


# ------------------------------------------------------------------ the call path (Engine._call)
def apply_call(prof, options, th, explicit, ro, mo, served_ctx=None):
    """Apply a resolved profile to one call. Mutates `options`; -> (think, fit_ctx, trace).
    explicit: {"temperature", "think", "num_predict"} -> bool (the caller passed it: it wins).
    ro/mo: the person's role/model overrides; a SHIPPED value never overrides a key they set there."""
    vals, src = prof["values"], prof["source"]
    applied, source, kept = {}, {}, {}
    mine_samp = set((ro.get("sampling") or {})) | set((mo.get("sampling") or {}))

    def ok(key, argname=None, override_key=None):
        if argname and explicit.get(argname):
            kept[key] = "call argument"
            return False
        if src.get(key) == "shipped" and override_key and override_key in mine_samp | set(ro) | set(mo):
            kept[key] = "your role/model override"
            return False
        return True

    for key in ("temperature", "top_p"):
        if key in vals and ok(key, "temperature" if key == "temperature" else None, key):
            options[key] = float(vals[key])
            applied[key], source[key] = options[key], src[key]
    t = vals.get("thinking")
    if t in ("on", "off") and ok("thinking", "think", "think"):
        th = t == "on"
        applied["thinking"], source["thinking"] = t, src["thinking"]
    if vals.get("reply_max") and ok("reply_max", "num_predict", "num_predict"):
        cap = int(vals["reply_max"])
        if cap < int(options["num_predict"]):
            options["num_predict"] = cap
        applied["reply_max"], source["reply_max"] = cap, src["reply_max"]
    fit_ctx = int(options["num_ctx"])
    if vals.get("ctx_max"):
        cap = int(vals["ctx_max"])
        if served_ctx:
            cap = min(cap, int(served_ctx))
        fit_ctx = min(fit_ctx, cap)
        applied["ctx_max"], source["ctx_max"] = fit_ctx, src["ctx_max"]
    for k in ("model", "budget", "speculation", "tools"):
        if k in vals:
            applied[k], source[k] = vals[k], src[k]
    tr = {"role": prof["role"], "applied": applied, "source": source}
    if kept:
        tr["kept"] = kept
    return th, fit_ctx, tr


def budget_check(prof, used):
    """-> a reason string when this role's per-run budget is spent, else ''. used: {calls, tokens, seconds}."""
    b = (prof or {}).get("values", {}).get("budget") or {}
    for k in BUDGET_KEYS:
        if b.get(k) is not None and float(used.get(k, 0)) >= float(b[k]):
            return f"the {prof['role']} role's budget of {b[k]} {k} per run is spent (role profile)"
    return ""


# ------------------------------------------------------------------ tool loops (arch_dataflow)
def _tool_rules(cfg, role):
    prof = resolve(cfg, role)
    if not prof or prof["values"].get("tools") is None:
        return None
    rules = {}
    for item in prof["values"]["tools"]:
        name, _, globs = str(item).partition(":")
        rules[name.strip()] = [g for g in globs.split("|") if g] if globs else None
    return rules


def tool_allow(cfg, role, allowed):
    """-> (allowed list for the step, names the profile removed). Unchanged when off or the profile names no tools list."""
    rules = _tool_rules(cfg, role)
    if rules is None:
        return list(allowed), []
    return [t for t in allowed if t in rules], [t for t in allowed if t not in rules]


_PATH_RX = re.compile(r"^(?:FILE|EDIT|DEF):\s*([^\s:]+)", re.M)


def check_tool(cfg, role, name, args):
    """-> (ok, why). Refuses a tool outside the role's allow-list, and a write outside the paths a 'tool:glob|glob' entry names."""
    rules = _tool_rules(cfg, role)
    if rules is None:
        return True, ""
    if name not in rules:
        return False, f"'{name}' is not allowed for the {role} role (role profile). Allowed: {', '.join(rules) or 'none'}"
    globs = rules[name]
    if globs:
        args = args if isinstance(args, dict) else {}
        paths = _PATH_RX.findall(str(args.get("blocks") or "")) + ([str(args["path"])] if args.get("path") else [])
        bad = [p for p in paths if not any(fnmatch.fnmatch(p.replace("\\", "/"), g) for g in globs)]
        if bad or not paths:
            return False, (f"the {role} role may use '{name}' only on {', '.join(globs)} (role profile); refused: "
                           f"{', '.join(bad) or 'no path given'}")
    return True, ""


# ------------------------------------------------------------------ the person's settings, with undo
def _mh():
    import model_hub as mh
    mh.CONFIG_FILE = core.CONFIG_FILE
    return mh


def _undo_path():
    return os.path.join(core.data_dir(), "role_profiles", "undo.json")


def _load_undo():
    try:
        with open(_undo_path(), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, list) else []
    except (OSError, ValueError):
        return []


def _push_undo(rec):
    st = _load_undo()
    st.append(rec)
    os.makedirs(os.path.dirname(_undo_path()), exist_ok=True)
    core._atomic_write(_undo_path(), json.dumps(st[-50:], indent=1))


_MISSING = "__missing__"


def _get(raw, path):
    cur = raw
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _set(raw, path, v):
    parts = path.split(".")
    cur = raw
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    if v == _MISSING:
        cur.pop(parts[-1], None)
        if not cur and len(parts) == 2:          # a section this write created is removed again
            raw.pop(parts[0], None)
    else:
        cur[parts[-1]] = v


def validate_profile(d):
    """-> a cleaned profile dict; raises ValueError on a bad key or value."""
    import arch_params
    return arch_params.validate_role_profile(d)


def write(what, role_profiles=None, settings=None, merge_role=None):
    """Write the person's settings with an undo record. role_profiles: replaces cfg.role_profiles; merge_role: (role, {k: v}) merged
    into one role; settings: {dotted.path: value} (validated against arch_params). -> the undo record."""
    import arch_params
    mh = _mh()
    raw = mh.read_config()
    before = {"role_profiles": copy.deepcopy(raw.get("role_profiles", _MISSING)), "settings": {}}
    if settings:
        settings = arch_params.validate_settings_patch(settings)
        for p, v in settings.items():
            before["settings"][p] = _get(raw, p)
            _set(raw, p, v)
    if role_profiles is not None:
        raw["role_profiles"] = {r: validate_profile(v) for r, v in role_profiles.items()}
    if merge_role:
        role, vals = merge_role
        cur = dict((raw.get("role_profiles") or {}).get(role) or {})
        cur.update(vals)
        raw.setdefault("role_profiles", {})[role] = validate_profile(cur)
    mh.write_config(raw)
    rec = {"what": what, "before": before, "at": time.time()}
    _push_undo(rec)
    return rec


def undo():
    """Put back what the last write() changed. -> the record, or None."""
    st = _load_undo()
    if not st:
        return None
    rec = st.pop()
    mh = _mh()
    raw = mh.read_config()
    rp = rec["before"]["role_profiles"]
    if rp == _MISSING:
        raw.pop("role_profiles", None)
    else:
        raw["role_profiles"] = rp
    for p, v in rec["before"]["settings"].items():
        _set(raw, p, v)
    mh.write_config(raw)
    core._atomic_write(_undo_path(), json.dumps(st, indent=1))
    return rec


# ------------------------------------------------------------------ presets
def presets():
    out = {}
    try:
        names = sorted(os.listdir(PRESET_DIR))
    except OSError:
        names = []
    for n in names:
        if n.endswith(".json"):
            try:
                with open(os.path.join(PRESET_DIR, n), encoding="utf-8") as f:
                    out[n[:-5].lower()] = json.load(f)
            except (OSError, ValueError):
                pass
    return out


def apply_preset(name):
    ps = presets()
    p = ps.get(str(name).lower())
    if not p:
        raise KeyError(f"no preset {name!r} (have: {', '.join(ps)})")
    return write(f"preset {name}", role_profiles=p.get("role_profiles") or {}, settings=p.get("settings") or {})


def table(cfg):
    """-> rows {role, key: value (source)} for every role that has a profile, for `architect.py role-profile`."""
    c = dict(cfg or {})
    c["behavior"] = dict(c.get("behavior") or {}, role_profiles="on")
    roles = sorted(set(shipped()) | set((cfg or {}).get("role_profiles") or {}))
    return [resolve(c, r) for r in roles]


# ------------------------------------------------------------------ tune-role
def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def parse_tries(tries):
    """['temperature=0,0.2', 'reply_max=512,1024'] -> [{...}, ...] (cartesian, capped at MAX_VARIANTS) and a note when capped."""
    keys, vals = [], []
    for t in tries or []:
        k, _, v = str(t).partition("=")
        k = k.strip()
        if k not in KEYS or k in ("tools", "budget"):
            raise ValueError(f"--try {k}: not a key tune-role can vary ({', '.join(x for x in KEYS if x not in ('tools', 'budget'))})")
        opts = []
        for x in v.split(","):
            x = x.strip()
            if not x:
                continue
            opts.append(validate_profile({k: x})[k])
        if not opts:
            raise ValueError(f"--try {k}: no values")
        keys.append(k)
        vals.append(opts)
    combos = [dict(zip(keys, c)) for c in itertools.product(*vals)] if keys else [{}]
    note = ""
    if len(combos) > MAX_VARIANTS:
        note = f"{len(combos)} combinations; only the first {MAX_VARIANTS} were run"
        combos = combos[:MAX_VARIANTS]
    return combos, note


def fake_runner(role):
    """Deterministic scripted stand-in (no model): pass chance and cost move with the variant's values."""
    def run(variant, task, seed):
        temp = float(variant.get("temperature", 0.2) or 0)
        p = max(0.05, min(0.95, 0.75 - 0.3 * temp + (0.05 if variant.get("thinking") == "on" else 0)))
        rm = int(variant.get("reply_max") or 1024)
        ok = _h(role, json.dumps(variant, sort_keys=True), task.get("id"), seed) < p
        return {"verdict": "pass" if ok else "fail", "secs": round(2.0 + rm / 400.0 + (3.0 if variant.get("thinking") == "on" else 0), 2),
                "tokens_in": int(variant.get("ctx_max") or 4096) // 2, "tokens_out": int(rm * 0.6)}
    return run


def live_runner(cfg, role):
    import arch_eval

    def run(variant, task, seed):
        mine = dict(((cfg or {}).get("role_profiles") or {}).get(role) or {})
        mine.update(variant)
        over = {"behavior": {"role_profiles": "on", "learn_from_runs": "off", "seed": seed}, "role_profiles": {role: mine}}
        prev = os.environ.get("ARCHITECT_EVAL")
        os.environ["ARCHITECT_EVAL"] = "1"
        try:
            r = arch_eval.run_task(task, over, label="tune-role")
        finally:
            if prev is None:
                os.environ.pop("ARCHITECT_EVAL", None)
            else:
                os.environ["ARCHITECT_EVAL"] = prev
        return {"verdict": r.get("verdict"), "secs": float(r.get("secs") or 0),
                "tokens_in": int(r.get("tokens_in") or 0), "tokens_out": int(r.get("tokens_out") or 0)}
    return run


def probe_tasks(role, n, seed, tasks=None, fake=False):
    import arch_jobs
    import arch_select
    job = arch_jobs.job_of(role)
    pool = arch_select.job_tasks(job, n, seed=seed, tasks=tasks) if job else []
    if not pool and fake:
        pool = [{"id": f"probe-{role}-{i}", "family": "fake"} for i in range(n)]
    return job, pool


def tune_role(role, tries, n=4, fake=False, seed=7, cfg=None, runner=None, tasks=None, apply_best=False, yes=False, say=print):
    import contextlib
    import arch_roles
    import arch_select
    if role not in arch_roles.ROLES:
        raise ValueError(f"unknown role {role!r}")
    cfg = cfg if cfg is not None else core.load_config()
    variants, note = parse_tries(tries)
    job, pool = probe_tasks(role, n, seed, tasks=tasks, fake=fake)
    if not pool:
        raise ValueError(f"no probe tasks for the {role} role (job {job}); nothing to measure")
    runner = runner or (fake_runner(role) if fake else live_runner(cfg, role))
    if fake:
        lock = contextlib.nullcontext()
    else:
        import arch_heavy
        lock = arch_heavy.one_at_a_time("tune-role", cfg, say=say)
    rows = []
    with lock:
        for v in variants:
            res = [runner(v, t, seed) for t in pool]
            passed = sum(1 for r in res if r.get("verdict") == "pass")
            lo, hi = arch_select.wilson(passed, len(res))
            rows.append({"variant": v, "n": len(res), "passed": passed, "pass_rate": round(passed / len(res), 3),
                         "ci": [round(lo, 3), round(hi, 3)], "secs_median": statistics.median([r["secs"] for r in res]),
                         "tokens_in": sum(r.get("tokens_in", 0) for r in res), "tokens_out": sum(r.get("tokens_out", 0) for r in res),
                         "tasks": [{"id": t.get("id"), "verdict": r.get("verdict"), "secs": r.get("secs")} for t, r in zip(pool, res)]})
    best = max(rows, key=lambda r: (r["ci"][0], r["pass_rate"], -r["secs_median"]))
    out = {"role": role, "job": job, "seed": seed, "n": len(pool), "fake": bool(fake), "note": note, "at": time.time(),
           "rows": rows, "best": best["variant"], "applied": False}
    if apply_best:
        if yes and best["variant"]:
            write(f"tune-role {role}", merge_role=(role, best["variant"]))
            out["applied"] = True
        elif not yes:
            out["apply_note"] = "--apply-best needs --yes to write your role profile"
    d = os.path.join(core.data_dir(), "tune_role")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime("%Y%m%d_%H%M%S") + f"_{role}.json")
    core._atomic_write(path, json.dumps(out, indent=1))
    out["path"] = path
    return out


def report(out):
    lines = [f"tune-role {out['role']} (job {out['job']}, {out['n']} probe tasks, seed {out['seed']}{', FAKE' if out['fake'] else ''})"]
    for r in out["rows"]:
        lines.append(f"  {json.dumps(r['variant'], sort_keys=True):<44} pass {r['passed']}/{r['n']} "
                     f"(95% CI {r['ci'][0]:.2f}-{r['ci'][1]:.2f})  median {r['secs_median']}s  tokens {r['tokens_in']} in / {r['tokens_out']} out")
    lines.append(f"  best: {json.dumps(out['best'], sort_keys=True)}" + ("  (written to your role profile; role-profile --undo)" if out["applied"] else ""))
    if out.get("note"):
        lines.append("  note: " + out["note"])
    if out.get("apply_note"):
        lines.append("  " + out["apply_note"])
    lines.append("  saved: " + out["path"])
    return "\n".join(lines)
