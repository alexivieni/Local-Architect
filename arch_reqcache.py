"""A semantic request cache: when you ask for (nearly) the same thing again on (nearly) the same project, the plan the last validated run
made and the files it showed the model come back at once, so the planner call and the context ranking are not paid for twice.

    py -3.12 arch_reqcache.py [--stats]

What makes a hit (all three, never fewer):
  1. the normalised request has a token-set Jaccard similarity >= SIMILARITY (0.8) with a stored request (1.0 = the same words),
  2. the set of files the request names ("touched files", read from the words of the request) is identical, and
  3. nothing the stored plan depended on changed: every context file and every file the plan touches has the same content hash, and
     no file was added to or removed from the project. An edit to an unrelated file does not invalidate; any change to a dependency does.

A hit returns the stored plan and context file list to REUSE. It never decides a pass: the caller still runs every gate (tests, the
verifier, the fake-code gate) on whatever the reused plan produces, exactly as on a fresh plan. Only a plan from a run whose gates
passed (validated=True) is ever stored. Stored per project under <data>/reqcache/. Off under ARCHITECT_NO_CACHE and during eval.

API: normalize, tokens, jaccard, touched_in, snapshot, store, lookup, stats, invalidate, enabled."""
import copy
import hashlib
import json
import os
import re
import sys
import time

SIMILARITY = 0.8
MAX_ENTRIES = 200            # per project, oldest-used dropped first
MAX_HASH_BYTES = 2_000_000   # a dependency bigger than this is hashed by size + mtime instead of content
SKIP_DIRS = {".git", ".arch", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea", ".vscode"}
STOP = frozenset("a an the to of in on for and or please can you could would me my is it this that with into from be do make".split())
PATH_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}\b")


def _data_dir():
    import arch_core
    return arch_core.data_dir()


def enabled(cfg=None):
    """behavior.request_cache (on|off, default off until the setting is registered); never under ARCHITECT_NO_CACHE or eval."""
    if os.environ.get("ARCHITECT_NO_CACHE") or os.environ.get("ARCHITECT_EVAL"):
        return False
    v = ((cfg or {}).get("behavior") or {}).get("request_cache", "off")
    return str(v).lower() in ("on", "true", "1")


def normalize(text):
    """Lower-case, punctuation to spaces, words joined by one space. Paths keep their dots and slashes."""
    t = str(text or "").lower()
    t = re.sub(r"[^\w./\\-]+", " ", t)
    t = re.sub(r"(?<![\w])[./\\-]+|[./\\-]+(?![\w])", " ", t)      # a dot or dash at a word edge is punctuation
    return " ".join(t.split())


def tokens(text):
    return frozenset(w for w in normalize(text).split() if w not in STOP and len(w) > 1)


def jaccard(a, b):
    a, b = frozenset(a), frozenset(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def touched_in(request):
    """The files a request names outright, as a frozenset of forward-slash lower-case paths."""
    return frozenset(m.replace("\\", "/").lower().lstrip("./") for m in PATH_RE.findall(str(request or "")))


def _rel(p):
    return str(p).replace("\\", "/").lstrip("./")


def _listing(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for f in sorted(files):
            out.append(_rel(os.path.relpath(os.path.join(d, f), root)))
    return out


def _file_sig(root, rel):
    p = os.path.join(root, rel)
    try:
        st = os.stat(p)
    except OSError:
        return None
    if st.st_size > MAX_HASH_BYTES:
        return f"big:{st.st_size}:{st.st_mtime_ns}"
    try:
        with open(p, "rb") as f:
            return hashlib.sha1(f.read().replace(b"\r\n", b"\n")).hexdigest()      # line endings ignored: CRLF churn is not a change
    except OSError:
        return None


def snapshot(root, deps):
    """What the plan depends on, captured BEFORE the run edits anything: {"tree": hash of the file list, "deps": {rel: content hash}}."""
    deps = sorted({_rel(d) for d in deps or []})
    listing = _listing(root)
    return {"tree": hashlib.sha1("\n".join(listing).encode("utf-8", "replace")).hexdigest(), "n_files": len(listing),
            "deps": {d: _file_sig(root, d) for d in deps}}


def _path(root):
    h = hashlib.sha1(os.path.abspath(root).lower().encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(_data_dir(), "reqcache", h + ".json")


def _load(root):
    try:
        with open(_path(root), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("entries"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"root": os.path.abspath(root), "entries": [], "stats": {"hits": 0, "exact": 0, "near": 0, "misses": 0, "stale": 0, "stored": 0}}


def _save(root, d):
    p = _path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def store(root, request, plan, context_files, snap=None, validated=False, now=None):
    """Remember a plan. Only when the run's gates passed (validated=True); anything else returns None and stores nothing.
    snap = snapshot() taken before the run edited files (default: taken now). -> the entry id."""
    if not validated or not plan:
        return None
    files = [_rel(f) for f in context_files or []]
    touched = touched_in(request)
    plan_files = set()
    for s in (plan.get("sprints") if isinstance(plan, dict) else None) or []:
        if isinstance(s, dict):
            plan_files |= {_rel(f) for f in s.get("files") or [] if isinstance(f, str)}
    snap = snap or snapshot(root, set(files) | touched | plan_files)
    for rel in plan_files | touched:                     # a file the plan touches must be part of what is checked for change
        snap["deps"].setdefault(rel, _file_sig(root, rel))
    norm = normalize(request)
    d = _load(root)
    eid = hashlib.sha1((norm + snap["tree"]).encode()).hexdigest()[:12]
    d["entries"] = [x for x in d["entries"] if x.get("norm") != norm]          # the newest validated plan for these words replaces the old
    d["entries"].append({"id": eid, "norm": norm, "tokens": sorted(tokens(request)), "touched": sorted(touched), "plan": plan,
                         "context_files": files, "snap": snap, "ts": now or time.time(), "used": now or time.time(), "uses": 0})
    d["entries"].sort(key=lambda x: x.get("used", 0))
    d["entries"] = d["entries"][-MAX_ENTRIES:]
    d["stats"]["stored"] = d["stats"].get("stored", 0) + 1
    _save(root, d)
    return eid


def _stale(root, e):
    """-> why the entry no longer applies, or None."""
    snap = e.get("snap") or {}
    for rel, sig in (snap.get("deps") or {}).items():
        if _file_sig(root, rel) != sig:
            return f"{rel} changed since the plan was made" if sig is not None else f"{rel} now exists (the plan assumed it did not)"
    listing = _listing(root)
    if hashlib.sha1("\n".join(listing).encode("utf-8", "replace")).hexdigest() != snap.get("tree"):
        return "files were added to or removed from the project"
    return None


def lookup(root, request, touched=None, cfg=None, now=None):
    """-> {"hit": bool, "why": plain words, "similarity", "kind": exact|near|None, "plan", "context_files", "id", "gates": "still run"}.
    A hit is a plan to REUSE; the caller still runs every gate on its result."""
    miss = {"hit": False, "kind": None, "similarity": 0.0, "plan": None, "context_files": [], "id": None, "gates": "still run"}
    if cfg is not None and not enabled(cfg):
        return dict(miss, why="request cache is off (behavior.request_cache) or disabled for this run")
    if cfg is None and (os.environ.get("ARCHITECT_NO_CACHE") or os.environ.get("ARCHITECT_EVAL")):
        return dict(miss, why="disabled for this run (ARCHITECT_NO_CACHE / eval)")
    d = _load(root)
    toks = tokens(request)
    tch = frozenset(_rel(t).lower() for t in touched) if touched is not None else touched_in(request)
    best, best_sim, why = None, 0.0, "no stored request is similar enough"
    for e in d["entries"]:
        sim = jaccard(toks, e.get("tokens") or [])
        if sim < SIMILARITY:
            continue
        if frozenset(e.get("touched") or []) != tch:
            why = f"a similar request ({sim:.2f}) names different files"
            continue
        bad = _stale(root, e)
        if bad:
            d["stats"]["stale"] = d["stats"].get("stale", 0) + 1
            why = f"a similar request ({sim:.2f}) is stored but stale: {bad}"
            continue
        if sim > best_sim:
            best, best_sim = e, sim
    st = d["stats"]
    if not best:
        st["misses"] = st.get("misses", 0) + 1
        _save(root, d)
        return dict(miss, why=why)
    kind = "exact" if best["norm"] == normalize(request) else "near"
    st["hits"] = st.get("hits", 0) + 1
    st[kind] = st.get(kind, 0) + 1
    best["uses"] = best.get("uses", 0) + 1
    best["used"] = now or time.time()
    _save(root, d)
    return {"hit": True, "kind": kind, "similarity": round(best_sim, 3), "plan": copy.deepcopy(best["plan"]), "context_files": list(best["context_files"]),
            "id": best["id"], "gates": "still run",
            "why": f"{kind} match ({best_sim:.2f}) with a plan that passed its gates; reused. Every gate still runs on the result."}


def stats(root=None):
    """-> {"entries", "hits", "exact", "near", "misses", "stale", "stored", "hit_rate"} for one project (root) or all projects."""
    base = os.path.join(_data_dir(), "reqcache")
    paths = [_path(root)] if root else [os.path.join(base, f) for f in sorted(os.listdir(base))] if os.path.isdir(base) else []
    tot = {"entries": 0, "hits": 0, "exact": 0, "near": 0, "misses": 0, "stale": 0, "stored": 0}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        tot["entries"] += len(d.get("entries") or [])
        for k in ("hits", "exact", "near", "misses", "stale", "stored"):
            tot[k] += int((d.get("stats") or {}).get(k, 0))
    n = tot["hits"] + tot["misses"]
    tot["hit_rate"] = round(tot["hits"] / n, 3) if n else None
    return tot


def invalidate(root, request=None):
    """Drop every entry of the project (or the one for this request). -> how many were dropped."""
    d = _load(root)
    n0 = len(d["entries"])
    norm = normalize(request) if request is not None else None
    d["entries"] = [] if norm is None else [e for e in d["entries"] if e.get("norm") != norm]
    _save(root, d)
    return n0 - len(d["entries"])


def main(argv=None):
    s = stats()
    print(f"request cache: {s['entries']} plan(s) stored, {s['hits']} hit(s) ({s['exact']} exact, {s['near']} near), {s['misses']} miss(es), "
          f"{s['stale']} stale; hit rate {s['hit_rate'] if s['hit_rate'] is not None else 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
