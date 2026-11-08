"""Offload roles to other machines on YOUR network that run Ollama (a desktop with a GPU, a home server).

Nothing is remote in the cloud sense: you add each machine by hand, a role is routed there only when you say so (or accept the plan), and if the
machine is unreachable the role falls back to a model on this computer. Generated code still runs in the local sandbox; only model calls travel.

Config keys: `hosts` {name: "http://addr:11434"} and `role_hosts` {role: name}. Measured speeds are kept in hub_data/hosts.json.

`role_hosts[role] = "fastest"` (FASTEST) asks for the fastest machine that answers right now, among the machines you added and measured
with that role's model (`bench_host`). Which one is decided from measurements only (`rank_hosts`); a quick health check (`health`, a
short timeout, remembered for HEALTH_TTL seconds) skips a machine that is off, and when none answers the role runs on this computer.
A machine found down in the middle of a run is handled by the Engine exactly as for a named machine (`Engine.down_hosts`).
Nothing here looks for machines: only the addresses in `hosts` are ever contacted, and only for a health check, a benchmark you asked
for, or a role you routed there.
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
FASTEST = "fastest"          # role_hosts value: the fastest machine that answers (see rank_hosts)
HEALTH_TIMEOUT = 3.0         # seconds a health check waits; a machine that takes longer counts as down for now
HEALTH_TTL = 60.0            # seconds a health answer is trusted inside this process before routing asks again
TINY_PROMPT = "Reply with the single word: ready"
_LIVE = {}                   # (name, url) -> last health answer in this process
_IN_FLIGHT = {}              # url -> active concurrent requests
_FL_LOCK = __import__("threading").Lock()   # best-of-N and crews change _IN_FLIGHT from several threads
_LLAMA_HOSTS = set()         # urls detected as llama-server (/v1/models)


def mark_down(name, url=None):
    """Mark a remote host unreachable in this process's health cache immediately so parallel calls skip it."""
    for (n, u), rec in list(_LIVE.items()):
        if n == name or (url and u == url):
            _LIVE[(n, u)] = dict(rec, ok=False, error="found unreachable in this run", ts=time.time())


def is_llama_host(url):
    u = (url or "").rstrip("/")
    return u in _LLAMA_HOSTS or u.endswith("/v1")


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


def route_of(cfg, role, down=()):
    """-> (name, url) when this role is routed to a reachable-by-config host, else None (this computer).
    For FASTEST: the fastest measured machine that passes a health check, skipping the names in `down`; never raises."""
    name = (cfg.get("role_hosts") or {}).get(role)
    hs = hosts_of(cfg)
    if name == FASTEST and FASTEST not in hs:            # a machine actually named "fastest" is just that machine
        try:
            best = next((c for c in rank_hosts(role, cfg, live=True, down=down, first=True) if c["healthy"]), None)
        except Exception:                                # routing must never break a model call: this computer answers instead
            return None
        return (best["name"], best["url"]) if best else None
    url = hs.get(name) if name else None
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
        models = [{"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1024 ** 3, 1)} for m in (tags.get("models") or []) if m.get("name")]
    except urllib.error.HTTPError as e:
        if e.code == 404 and get is _get:
            try:
                v1 = get(url + "/v1/models") or {}
                models = [{"name": m.get("id"), "size_gb": 0.0} for m in (v1.get("data") or []) if m.get("id")]
                _LLAMA_HOSTS.add(url.rstrip("/"))
            except Exception as e2:
                return {"ok": False, "error": f"not reachable ({str(getattr(e2, 'reason', e2))[:80]})"}
        else:
            return {"ok": False, "error": f"not reachable ({str(getattr(e, 'reason', e))[:80]})"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"not reachable ({str(getattr(e, 'reason', e))[:80]})"}
    ms = round((time.time() - t0) * 1000)
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
    _store("bench", f"{url}|{model}", res)
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
    if host == FASTEST and FASTEST not in hosts_of(cfg):
        if not hosts_of(cfg):
            raise ValueError("'fastest' picks among the machines you added; add one first (hosts --add name=address)")
    elif host not in hosts_of(cfg):
        raise ValueError(f"no host named '{host}'; add it first")
    prev = {"role_host": (cfg.get("role_hosts") or {}).get(role), "model": (cfg.get("roles") or {}).get(role)}
    cfg.setdefault("role_hosts", {})[role] = host
    if model:
        cfg.setdefault("roles", {})[role] = model
    return prev


def unroute(cfg, role):
    (cfg.get("role_hosts") or {}).pop(role, None)


# ------------------------------------------------------------------------------------------------ health, measured speed, ranking
def _cfg(cfg):
    return cfg if cfg is not None else core.load_config()


def _url_of(cfg, name):
    """Only a machine the person added is ever contacted."""
    url = hosts_of(cfg).get(name)
    if not url:
        raise ValueError(f"no host named '{name}'; add it first (hosts --add name=address)")
    return url


def _store(section, key, val):
    """One record into hosts.json (written whole, then swapped in, so a reader never sees half a file). A full disk loses a
    measurement, never a run."""
    data = core.load_json(path()) or {}
    data.setdefault(section, {})[key] = val
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        tmp = path() + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path())
    except OSError:
        pass


def _same_model(a, b):
    def n(x):
        x = (x or "").strip().lower()
        return x if ":" in x else x + ":latest"
    return n(a) == n(b)


def _is_chat(name):
    return not re.search(r"embed|nomic|bge|minilm", name or "", re.I)


def forget():
    """Drop the health answers remembered in this process (the next routing decision asks the machines again)."""
    _LIVE.clear()


def health(name, cfg=None, get=None, timeout=None):
    """Is that machine answering right now? -> {"name","url","ok","ms","error","models","ts"}. One request (/api/tags) with a short
    timeout; never raises for a machine problem (raises ValueError for a name that was never added). Stored in hosts.json."""
    cfg = _cfg(cfg)
    url = _url_of(cfg, name)
    get = get or _get
    timeout = HEALTH_TIMEOUT if timeout is None else timeout
    t0 = time.time()
    try:
        tags = get(url + "/api/tags", timeout=timeout) or {}
        models = [m.get("name") for m in (tags.get("models") or []) if m.get("name")][:200]
        ok, err = True, None
    except urllib.error.HTTPError as e:
        if e.code == 404 and get is _get:
            try:
                v1 = get(url + "/v1/models", timeout=timeout) or {}
                models = [m.get("id") for m in (v1.get("data") or []) if m.get("id")][:200]
                _LLAMA_HOSTS.add(url.rstrip("/"))
                ok, err = True, None
            except Exception as e2:
                ok, models, err = False, None, f"not reachable ({str(getattr(e2, 'reason', e2))[:80]})"
        else:
            ok, models, err = False, None, f"not reachable ({str(getattr(e, 'reason', e))[:80]})"
    except Exception as e:                               # refused, timed out, not Ollama, garbage: all mean "not usable now"
        why = getattr(e, "reason", e)
        slow = isinstance(e, TimeoutError) or isinstance(why, TimeoutError) or "timed out" in str(why).lower()
        ok, models = False, None
        err = f"did not answer within {timeout:g} s" if slow else f"not reachable ({str(why)[:80]})"
    res = {"name": name, "url": url, "ok": ok, "ms": round((time.time() - t0) * 1000), "error": err, "models": models, "ts": time.time()}
    _LIVE[(name, url)] = res
    _store("health", name, res)
    return res


def _live_health(name, cfg, get=None):
    url = hosts_of(cfg).get(name)
    hit = _LIVE.get((name, url))
    if hit and time.time() - hit["ts"] < HEALTH_TTL:
        return hit
    return health(name, cfg, get=get)


def bench_host(name, cfg=None, model=None, chat=None, get=None, say=None):
    """Measure one machine you added: how long it takes to answer at all (latency) and how fast it writes (tok/s), with a tiny prompt.
    Runs only when asked. `model` defaults to the largest chat model on that machine. -> {"ok", ...}; never raises for a machine
    problem. A success is stored with the other benchmarks (hosts.json "bench", key url|model), so rank_hosts can use it."""
    cfg = _cfg(cfg)
    url = _url_of(cfg, name)
    say = say or (lambda m: None)
    h = health(name, cfg, get=get)
    if not h["ok"]:
        say(f"    {name}: {h['error']}")
        return {"ok": False, "name": name, "url": url, "model": model, "error": h["error"]}
    have = [m for m in (h["models"] or []) if _is_chat(m)]
    if not model:
        if not have:
            return {"ok": False, "name": name, "url": url, "model": None, "error": "that machine has no chat model installed"}
        model = max(have, key=lambda m: (size_b(m) or 0, m))
    elif h["models"] is not None and not any(_same_model(model, m) for m in h["models"]):
        return {"ok": False, "name": name, "url": url, "model": model, "error": f"{model} is not installed on {name}"}
    chat = chat or core.stream_chat
    first = {}

    def on_token(piece):
        first.setdefault("t", time.time())

    t0 = time.time()
    try:
        r = chat(model, [{"role": "user", "content": TINY_PROMPT}], {"num_ctx": 2048, "num_predict": 24, "temperature": 0}, False, "5m",
                 {"first": 300, "idle": 60, "total": 300}, None, on_token, url)
    except core.LLMError as e:
        say(f"    {name}: the benchmark failed ({str(e)[:100]})")
        return {"ok": False, "name": name, "url": url, "model": model, "error": str(e)[:200]}
    secs = time.time() - t0
    res = {"gen_tok_s": r.get("tok_s"), "prefill_tok_s": r.get("prefill_tok_s"), "load_s": r.get("load_s"), "secs": round(secs, 2),
           "latency_ms": h["ms"], "first_token_s": round(first["t"] - t0, 2) if "t" in first else None, "kind": "tiny", "ts": time.time()}
    _store("bench", f"{url}|{model}", res)
    say(f"    {model} on {name}: answers in {h['ms']} ms, writes {res['gen_tok_s']} tok/s")
    return dict(res, ok=True, name=name, url=url, model=model)


def _role_model(cfg, role):
    return (cfg.get("roles") or {}).get(role) or cfg.get("default_model")


def rank_hosts(role, cfg=None, live=False, down=(), get=None, first=False):
    """The machines you added, best first, for this role's model: measured writing speed (tok/s, from a benchmark of THAT model on
    THAT machine), then how quickly it answers. Each: {name, url, model, tok_s, latency_ms, healthy, why}; healthy is True (answered),
    False (did not, or was found down in this run: `down`) or None (not checked yet). A machine never measured with the model is
    left out: a guess is not a measurement. live=True checks health now (remembered HEALTH_TTL s); first=True stops at the first
    healthy one (what routing needs)."""
    cfg = _cfg(cfg)
    model = _role_model(cfg, role)
    if not model:
        return []
    data = core.load_json(path()) or {}
    cands = []
    for name, url in hosts_of(cfg).items():
        b = (data.get("bench") or {}).get(f"{url}|{model}") or {}
        if not b.get("gen_tok_s"):
            continue
        rec = ((data.get("health") or {}).get(name) or {})
        rec = rec if rec.get("url") == url else {}
        cands.append({"name": name, "url": url, "model": model, "tok_s": b["gen_tok_s"], "latency_ms": b.get("latency_ms") or rec.get("ms"),
                      "healthy": rec.get("ok") if rec else None, "checked": rec.get("ts"), "error": rec.get("error"), "models": rec.get("models")})
    cands.sort(key=lambda c: (-(c["tok_s"] / (1 + _IN_FLIGHT.get(c["url"], 0))), c["latency_ms"] if c["latency_ms"] is not None else 1e9))
    out = []
    for c in cands:
        if c["name"] in down:
            c["healthy"], c["error"] = False, "found unreachable in this run"
        elif live:
            h = _live_health(c["name"], cfg, get)
            c.update(healthy=h["ok"], checked=h["ts"], error=h["error"], models=h["models"])
            if h["ok"]:
                c["latency_ms"] = h["ms"]
        if c["healthy"] and c["models"] is not None and not any(_same_model(model, m) for m in c["models"]):
            c["healthy"], c["error"] = False, f"{model} is no longer installed there"
        c["why"] = (f"{c['tok_s']} tok/s measured with {model}" + (f", answers in {c['latency_ms']} ms" if c["latency_ms"] is not None else "")
                    + ("" if c["healthy"] else f"; skipped: {c['error']}" if c["healthy"] is False else "; not checked yet"))
        c.pop("models", None)
        out.append(c)
        if first and c["healthy"]:
            break
    order = {True: 0, None: 1, False: 2}
    return sorted(out, key=lambda c: (order[c["healthy"]], -(c["tok_s"] / (1 + _IN_FLIGHT.get(c["url"], 0))), c["latency_ms"] if c["latency_ms"] is not None else 1e9))


# ------------------------------------------------------------------------------------------------ CPU work on another machine
CPU_KINDS = ("tests", "index", "lint", "scan")      # the only kinds ever sent
CPU_MAX_BYTES = 2_000_000                           # more than this to ship and it just runs here
CPU_PATH = "/arch/cpu"


def cpu_host(cfg):
    """The machine the person routed CPU work to (role_hosts['cpu'] = name), or None."""
    name = (cfg.get("role_hosts") or {}).get("cpu")
    return name if name and name in hosts_of(cfg) else None


def _post(url, body, timeout, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:                                        # arch_cpuworker.py refuses CPU work without it
        headers["X-Arch-Token"] = token
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def run_cpu(kind, payload, local, cfg=None, post=None, get=None, timeout=600):
    """Run CPU work (tests / indexing / lint / scan) on the machine routed for 'cpu', else here.
    payload: {"argv": [...], "files": {rel: text}} -- everything the remote needs is shipped in it; local(payload) -> {"ok","code","out"} is the
    same work done on this computer. A machine that is not added, down, slow, answering garbage or refusing falls back to local(payload): the
    run never fails because a second machine is off. -> that dict plus "where" ('local' or the host name) and "fallback" (why, or None)."""
    cfg = _cfg(cfg)
    name = cpu_host(cfg)
    if kind not in CPU_KINDS:
        raise ValueError(f"unknown CPU work '{kind}'")

    def here(why):
        res = dict(local(payload))
        res["where"], res["fallback"] = "local", why
        return res
    if not name:
        return here(None)
    if len(json.dumps(payload, default=str)) > CPU_MAX_BYTES:
        return here("too much to send")
    url = _url_of(cfg, name)
    h = _live_health(name, cfg, get)
    if not h["ok"]:
        return here(f"{name} is down ({h['error']})")
    try:
        tok = str((cfg.get("cpu_worker") or {}).get("token") or os.environ.get("ARCHITECT_CPU_TOKEN") or "")
        send = post or (lambda u, b, t: _post(u, b, t, token=tok))
        r = send(url + CPU_PATH, {"kind": kind, **payload}, timeout)
        if not isinstance(r, dict) or "ok" not in r or "out" not in r:
            raise ValueError("unexpected answer")
    except Exception as e:                           # refused, timed out, not an Architect worker, garbage: do it here
        why = str(getattr(e, "reason", e))[:80]
        _LIVE[(name, url)] = {"name": name, "url": url, "ok": False, "ms": 0, "error": f"CPU work failed ({why})", "models": None, "ts": time.time()}
        return here(f"{name} failed the work ({why})")
    return {"ok": bool(r["ok"]), "code": r.get("code"), "out": str(r.get("out"))[-8000:], "where": name, "fallback": None}


def _ago(ts):
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def status(cfg=None, live=False, get=None):
    """Plain words for the hub: each machine (up/down, when checked, its fastest measured model) and where each routed role runs.
    Reads what was measured; with live=True it checks every machine first (only those you added). ->
    {"lines": [str], "hosts": [{name,url,healthy,checked,error,best}], "roles": [{role,asked,runs_on,why}]}."""
    cfg = _cfg(cfg)
    hs = hosts_of(cfg)
    rh = cfg.get("role_hosts") or {}
    if not hs:
        return {"lines": ["No other machines added: everything runs on this computer."], "hosts": [], "roles": []}
    if live:
        for n in hs:
            health(n, cfg, get=get)
    data = core.load_json(path()) or {}
    lines, hosts, roles = [], [], []
    for n, u in hs.items():
        rec = ((data.get("health") or {}).get(n) or {})
        rec = rec if rec.get("url") == u else {}
        benches = [(k.split("|", 1)[1], v) for k, v in (data.get("bench") or {}).items() if k.startswith(u + "|") and (v or {}).get("gen_tok_s")]
        best = max(benches, key=lambda kv: kv[1]["gen_tok_s"]) if benches else None
        speed = (f"; fastest measured: {best[0]} at {best[1]['gen_tok_s']} tok/s ({_ago(best[1].get('ts'))})" if best
                 else "; not measured yet")
        if not rec:
            lines.append(f"{n}: not checked yet{speed}.")
        elif rec.get("ok"):
            lines.append(f"{n}: up, answered in {rec.get('ms')} ms ({_ago(rec.get('ts'))}){speed}.")
        else:
            mine = [r for r, h_ in rh.items() if h_ == n]
            lines.append(f"{n}: down when checked {_ago(rec.get('ts'))} ({rec.get('error')})" + (f"; {', '.join(mine)} run on this computer until it answers" if mine else "") + ".")
        hosts.append({"name": n, "url": u, "healthy": rec.get("ok") if rec else None, "checked": rec.get("ts"), "error": rec.get("error"),
                      "best": {"model": best[0], "tok_s": best[1]["gen_tok_s"]} if best else None})
    for role, h_ in sorted(rh.items()):
        if h_ == FASTEST and FASTEST not in hs:
            ranked = rank_hosts(role, cfg)
            pick = next((c for c in ranked if c["healthy"] is not False), None)
            model = _role_model(cfg, role)
            if pick:
                rest = [c for c in ranked if c is not pick and c["healthy"] is not False]
                why = f"{pick['name']} ({pick['tok_s']} tok/s measured)" + (f", then {', '.join(c['name'] for c in rest)}" if rest else "") + ", then this computer"
                lines.append(f"{role}: the fastest machine that answers: {why}.")
                roles.append({"role": role, "asked": FASTEST, "runs_on": pick["name"], "why": why})
            else:
                why = f"no machine has been measured with {model} and found up" if model else "the role has no model set"
                lines.append(f"{role}: asks for the fastest machine, but {why}: it runs on this computer.")
                roles.append({"role": role, "asked": FASTEST, "runs_on": None, "why": why})
        elif h_ in hs:
            rec = next(x for x in hosts if x["name"] == h_)
            down = rec["healthy"] is False
            lines.append(f"{role}: runs on {h_}" + (", which was down at the last check: on this computer until it answers." if down else "."))
            roles.append({"role": role, "asked": h_, "runs_on": None if down else h_, "why": "down at the last check" if down else "routed by you"})
        else:
            lines.append(f"{role}: routed to {h_}, which is no longer added: it runs on this computer.")
            roles.append({"role": role, "asked": h_, "runs_on": None, "why": "machine not added"})
    return {"lines": lines, "hosts": hosts, "roles": roles}
