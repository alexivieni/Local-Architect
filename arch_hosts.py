"""Offload roles to other machines on YOUR network that run Ollama (a desktop with a GPU, a home server).

Nothing is remote in the cloud sense: you add each machine by hand, a role is routed there only when you say so (or accept the plan), and if the
machine is unreachable the role falls back to a model on this computer. Generated code still runs in the local sandbox; only model calls travel.

Config keys: `hosts` {name: "http://addr:11434"} and `role_hosts` {role: name}. Measured speeds are kept in hub_data/hosts.json.
"""
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

import arch_core as core

HEAVY_ROLES = ("planner", "reviewer", "architect", "judge", "reasoner", "security_auditor")     # thinking roles gain most from a bigger model
STEP_UP = 1.25


def path():
    return os.path.join(core.data_dir(), "hosts.json")


def normalize(addr):
    a = (addr or "").strip().rstrip("/")
    if not a:
        raise ValueError("an address is required, for example 192.168.1.20 or http://desktop:11434")
    if not a.startswith("http"):
        a = "http://" + a
    if not re.search(r":\d+$", a):
        a += ":11434"
    return a


def hosts_of(cfg):
    return dict(cfg.get("hosts") or {})


def route_of(cfg, role):
    """-> (name, url) when this role is routed to a reachable-by-config host, else None."""
    name = (cfg.get("role_hosts") or {}).get(role)
    url = hosts_of(cfg).get(name) if name else None
    return (name, url) if url else None


def _get(url, timeout=6):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe(url, get=None):
    """One machine -> {"ok","ms","models":[{name,size_gb}],"loaded":[names],"version"}; never raises."""
    get = get or _get
    t0 = time.time()
    try:
        tags = get(url + "/api/tags")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"not reachable ({str(getattr(e, 'reason', e))[:80]})"}
    ms = round((time.time() - t0) * 1000)
    models = [{"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1024 ** 3, 1)} for m in (tags.get("models") or []) if m.get("name")]
    try:
        loaded = [m.get("name") for m in (get(url + "/api/ps").get("models") or [])]
    except (urllib.error.URLError, OSError, ValueError):
        loaded = []
    try:
        ver = (get(url + "/api/version") or {}).get("version")
    except (urllib.error.URLError, OSError, ValueError):
        ver = None
    return {"ok": True, "ms": ms, "models": models, "loaded": loaded, "version": ver}


def size_b(name):
    """Dense-equivalent size in billions from a model name (MoE 'A3B' tags use sqrt(total*active)*1.3). None when the name says nothing."""
    n = (name or "").lower()
    tot = re.search(r"(\d+(?:\.\d+)?)\s*b(?![a-z])", n)
    if not tot:
        return None
    t = float(tot.group(1))
    act = re.search(r"a(\d+(?:\.\d+)?)b", n)
    if act and float(act.group(1)) < t:
        return round(math.sqrt(t * float(act.group(1))) * 1.3, 1)
    return t


def bench(url, model, say=print):
    """Real call on that machine -> tok/s (generation) and prompt tok/s. Stored in hosts.json."""
    msgs = [{"role": "user", "content": "Write a Python function that returns the sum of a list. Code only."}]
    t0 = time.time()
    r = core.stream_chat(model, msgs, {"num_ctx": 4096, "num_predict": 96, "temperature": 0}, False, "5m", {"first": 600, "idle": 120, "total": 600}, host=url)
    res = {"gen_tok_s": r.get("tok_s"), "prefill_tok_s": r.get("prefill_tok_s"), "load_s": r.get("load_s"), "secs": round(time.time() - t0, 1), "ts": time.time()}
    data = core.load_json(path()) or {}
    data.setdefault("bench", {})[f"{url}|{model}"] = res
    os.makedirs(core.data_dir(), exist_ok=True)
    with open(path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    say(f"    {model} on {url}: {res['gen_tok_s']} tok/s generating")
    return res


def benched(url, model):
    return ((core.load_json(path()) or {}).get("bench") or {}).get(f"{url}|{model}")


def plan(cfg, local_models, probes):
    """Suggest which roles to send where. Only suggestions: nothing is changed. Each: {role, host, model, why}.
    A role moves only when a reachable machine has a clearly larger model (>= STEP_UP x) than the one it uses here."""
    out = []
    for hname, p in probes.items():
        if not p.get("ok"):
            continue
        ranked = sorted(((size_b(m["name"]) or 0, m["name"]) for m in p["models"] if not re.search(r"embed|nomic|bge|minilm", m["name"], re.I)), reverse=True)
        if not ranked or not ranked[0][0]:
            continue
        top_b, top = ranked[0]
        for role in HEAVY_ROLES:
            if (cfg.get("role_hosts") or {}).get(role):
                continue
            here = (cfg.get("roles") or {}).get(role) or cfg.get("default_model")
            here_b = size_b(here) or 0
            if here_b and top_b >= here_b * STEP_UP and role in (core.arch_roles.ROLES if hasattr(core, "arch_roles") else HEAVY_ROLES):
                out.append({"role": role, "host": hname, "model": top, "why": f"{top} (~{top_b}B) on {hname} is clearly larger than {here} (~{here_b}B) here; {p['ms']} ms to reach."})
    seen, uniq = set(), []
    for s in out:
        if (s["role"]) not in seen:
            seen.add(s["role"])
            uniq.append(s)
    return uniq


def route(cfg, role, host, model=None):
    """Route one role to a named host (and optionally set its model there). Returns the previous values so the caller can undo."""
    if host not in hosts_of(cfg):
        raise ValueError(f"no host named '{host}'; add it first")
    prev = {"role_host": (cfg.get("role_hosts") or {}).get(role), "model": (cfg.get("roles") or {}).get(role)}
    cfg.setdefault("role_hosts", {})[role] = host
    if model:
        cfg.setdefault("roles", {})[role] = model
    return prev


def unroute(cfg, role):
    (cfg.get("role_hosts") or {}).pop(role, None)
