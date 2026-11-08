"""Cache hygiene: what the harness keeps on disk to go faster, how big it has grown, and what may safely go (standard library only).

Why this exists: the worker's saved prompt memory (llama-server --slot-save-path, 0.2-0.4 GB a project) was written at the end of every
run and never removed. Seen 2026-11-05 on a 6 GB laptop: 35 files, 8.3 GB, 19 of them (4.9 GB) from thrown-away eval folders that no run
can ever restore. Nothing capped or expired that store.

The stores it knows (all under the data folder, nothing else is ever touched):
  * slots   server/slots/<tier>/<name>.bin   saved prompt memory; capped by storage.slot_cache_gb, stale after storage.slot_max_age_days
  * lookup  server/lookup/                   the n-gram lookup cache llama-server grows (reported only)
  * replies cache/responses.db               arch_cache: rows capped by cache.max_rows / cache.max_days (rows, never the file)
  * cloud   cloud_cache/*.json               Claude replies, 30-day rule (arch_cloud)
  * route   route_cache.json                 the router's past decisions, 500 entries, 30 days (arch_flows; reported only)

The rules, because a cleaner that deletes the wrong thing is worse than a full disk:
  * `inventory()` and `plan_prune()` only read. `prune(plan, yes=False)` deletes nothing; with yes=True it deletes exactly the plan,
    refuses any path outside the cache folders (and anything that is not a cache file), and writes every deletion to
    <data>/cache_hygiene.log.
  * an orphan is a slot file nobody can restore: an eval_*/live_* folder's, or one whose project folder is known and gone. A slot whose
    project was never seen is "unknown owner" and is kept (it only leaves under the size cap, least recently used first).
  * never the slot a running server is using (the newest file of a tier whose server is up, or anything touched in the last minutes),
    never a model file.
  * the size cap is applied automatically when a slot is saved (`enforce_slot_cap`, the person set the cap); orphans and stale files
    that are within the cap go only through the explicit `prune --yes`.
"""
import hashlib
import json
import os
import re
import sqlite3
import time

import arch_core as core

GB = 1024 ** 3
THROWAWAY_PREFIXES = ("eval_build_", "eval_", "live_")
RECENT_SECS = 15 * 60          # a slot file touched this recently may be in the middle of a save or restore: never removed
CLOUD_MAX_AGE = 30 * 86400     # arch_cloud.CACHE_MAX_AGE (repeated so this module never imports the cloud client)
ROUTE_MAX, ROUTE_AGE = 500, 30 * 86400
MODEL_SUFFIXES = (".gguf", ".safetensors", ".bin.part")


# ------------------------------------------------------------------------------------------------ paths and names
def slots_dir():
    return os.path.join(core.data_dir(), "server", "slots")


def lookup_dir():
    return os.path.join(core.data_dir(), "server", "lookup")


def replies_dir():
    return os.path.join(core.data_dir(), "cache")


def cloud_dir():
    return os.path.join(core.data_dir(), "cloud_cache")


def route_file():
    return os.path.join(core.data_dir(), "route_cache.json")


def log_path():
    return os.path.join(core.data_dir(), "cache_hygiene.log")


def deletable_dirs():
    """The only folders a file may be deleted from, and the file endings allowed in each."""
    return {slots_dir(): (".bin",), cloud_dir(): (".json",)}


def _clean(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s))


def _root_hash(path):
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8", "replace")).hexdigest()[:8]


def legacy_slot_name(project):
    """The name used until 2026-11-05: the folder's base name only (two projects named `app` shared one file)."""
    p = str(project)
    base = os.path.basename(os.path.normpath(p)) if os.path.isabs(p) else p
    return _clean(base)[:60] + ".bin"


def slot_name(project):
    """The file name for a project's saved prompt memory. A full path gets its base name plus a short hash of the path, so two
    projects with the same folder name no longer share one file; a bare name (older callers) keeps the old name."""
    p = str(project)
    if not os.path.isabs(p):
        return legacy_slot_name(p)
    return _clean(os.path.basename(os.path.normpath(p)))[:51] + "_" + _root_hash(p) + ".bin"


# ------------------------------------------------------------------------------------------------ settings
def _cfg(cfg):
    if cfg is None:
        try:
            cfg = core.load_config()
        except Exception:
            cfg = {}
    return cfg or {}


def slot_cap_bytes(cfg=None):
    v = ((_cfg(cfg).get("storage") or {}).get("slot_cache_gb", core.DEFAULTS["storage"]["slot_cache_gb"]))
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = float(core.DEFAULTS["storage"]["slot_cache_gb"])
    return int(v * GB) if v > 0 else 0


def slot_max_age_days(cfg=None):
    v = ((_cfg(cfg).get("storage") or {}).get("slot_max_age_days", core.DEFAULTS["storage"]["slot_max_age_days"]))
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return int(core.DEFAULTS["storage"]["slot_max_age_days"])


def _reply_limits(cfg):
    c = _cfg(cfg).get("cache") or {}
    d = core.DEFAULTS.get("cache") or {}
    try:
        rows = int(c.get("max_rows", d.get("max_rows", 4000)) or 0)
    except (TypeError, ValueError):
        rows = 4000
    try:
        days = int(c.get("max_days", d.get("max_days", 30)) or 0)
    except (TypeError, ValueError):
        days = 30
    return rows, days


# ------------------------------------------------------------------------------------------------ who owns a slot
def known_roots():
    """Project folders the install has seen: portfolio.json apps, the work queue's roots, ~/ArchitectProjects/*, the Architect itself."""
    roots = set()
    try:
        p = core.load_json(os.path.join(core.HOME, "portfolio.json")) or {}
        for a in (p.get("apps") or []):
            if isinstance(a, dict) and a.get("path"):
                roots.add(os.path.abspath(str(a["path"])))
    except Exception:
        pass
    jobs = os.path.join(core.data_dir(), "jobs.db")
    if os.path.isfile(jobs):
        try:
            c = sqlite3.connect(f"file:{jobs}?mode=ro", uri=True, timeout=2.0)
            try:
                for (r,) in c.execute("select distinct root from jobs"):
                    if r:
                        roots.add(os.path.abspath(str(r)))
            finally:
                c.close()
        except sqlite3.Error:
            pass
    pd = os.path.join(os.path.expanduser("~"), "ArchitectProjects")
    try:
        for de in os.scandir(pd):
            if de.is_dir():
                roots.add(os.path.abspath(de.path))
    except OSError:
        pass
    roots.add(os.path.abspath(core.HOME))
    return roots


def owner_of(fname, roots):
    """-> (kind, why, root): kind is 'orphan', 'owned' or 'unknown'. Unknown is never treated as an orphan."""
    stem = fname[:-4] if fname.endswith(".bin") else fname
    _fp = re.match(r"^(.*)-[0-9a-f]{8}$", stem)            # slot_io appends "-<model fingerprint>" so a slot never restores into another model
    if _fp:
        stem = _fp.group(1)
        fname = stem + ".bin"
    for pre in THROWAWAY_PREFIXES:
        if stem.startswith(pre):
            return "orphan", f"from a thrown-away {pre.rstrip('_')} folder; no run can restore it", None
    by_new = [r for r in roots if slot_name(r) == fname]
    if by_new:
        r = by_new[0]
        return ("owned", "project folder exists", r) if os.path.isdir(r) else ("orphan", f"its project folder is gone ({r})", r)
    by_old = [r for r in roots if legacy_slot_name(r) == fname]
    if by_old:
        live = [r for r in by_old if os.path.isdir(r)]
        if live:
            return "owned", "project folder exists", live[0]
        return "orphan", f"its project folder is gone ({by_old[0]})", by_old[0]
    return "unknown", "no known project has this name; kept (it leaves only under the size cap)", None


def protected_slots(now=None):
    """Slot files that must not be removed now: the newest file of each tier whose server process is alive (what it holds in
    memory came from there), and anything written or read in the last RECENT_SECS (a save or restore may be under way)."""
    now = now or time.time()
    keep, why = set(), {}
    alive_tiers = set()
    try:
        import arch_server
        st = arch_server.load_state() or {}
        for t, rec in (st.get("processes") or {}).items():
            if isinstance(rec, dict) and rec.get("pid") and arch_server._alive(rec["pid"]):
                alive_tiers.add(t)
    except Exception:
        pass
    sd = slots_dir()
    try:
        tiers = [de for de in os.scandir(sd) if de.is_dir()]
    except OSError:
        tiers = []
    for td in tiers:
        files = []
        try:
            for de in os.scandir(td.path):
                if de.is_file() and de.name.endswith(".bin"):
                    files.append((de.stat().st_mtime, de.path))
        except OSError:
            continue
        for m, p in files:
            if now - m < RECENT_SECS:
                keep.add(os.path.normcase(p))
                why[os.path.normcase(p)] = "used in the last few minutes"
        if td.name in alive_tiers and files:
            p = max(files)[1]
            keep.add(os.path.normcase(p))
            why[os.path.normcase(p)] = f"the running {td.name} server's latest prompt memory"
    return keep, why


# ------------------------------------------------------------------------------------------------ inventory
def _files(top, suffixes=None, recursive=True):
    out = []
    if not os.path.isdir(top):
        return out
    for dp, dns, fns in os.walk(top):
        for fn in fns:
            if suffixes and not fn.endswith(suffixes):
                continue
            p = os.path.join(dp, fn)
            try:
                s = os.stat(p)
            except OSError:
                continue
            out.append({"file": p, "bytes": s.st_size, "mtime": s.st_mtime})
        if not recursive:
            break
    return out


def _store(sid, name, path, files, cap_bytes=None):
    b = sum(f["bytes"] for f in files)
    ms = [f["mtime"] for f in files]
    return {"id": sid, "name": name, "path": path, "files": len(files), "bytes": b,
            "oldest": min(ms) if ms else None, "newest": max(ms) if ms else None,
            "cap_bytes": cap_bytes, "over_cap": bool(cap_bytes) and b > cap_bytes, "orphans": [], "orphan_bytes": 0}


def inventory(cfg=None, roots=None, now=None):
    """-> [store]: {id, name, path, files, bytes, oldest, newest, cap_bytes, over_cap, orphans: [{file, bytes, why}], orphan_bytes, ...}.
    Reads only."""
    cfg = _cfg(cfg)
    now = now or time.time()
    roots = known_roots() if roots is None else {os.path.abspath(r) for r in roots}
    out = []

    # saved prompt memory
    sl = _files(slots_dir(), (".bin",))
    st = _store("slots", "Saved prompt memory (KV slots)", slots_dir(), sl, slot_cap_bytes(cfg))
    age = slot_max_age_days(cfg)
    st["max_age_days"] = age
    st["unknown"], st["owned"], st["stale"] = [], [], []
    for f in sl:
        kind, why, root = owner_of(os.path.basename(f["file"]), roots)
        row = {"file": f["file"], "bytes": f["bytes"], "mtime": f["mtime"], "why": why, "root": root,
               "tier": os.path.basename(os.path.dirname(f["file"]))}
        st[{"orphan": "orphans", "owned": "owned", "unknown": "unknown"}[kind]].append(row)
        if kind != "orphan" and age and now - f["mtime"] > age * 86400:
            st["stale"].append(dict(row, why=f"not used for {int((now - f['mtime']) // 86400)} days (limit {age})"))
    st["orphan_bytes"] = sum(o["bytes"] for o in st["orphans"])
    out.append(st)

    # n-gram lookup cache
    out.append(_store("lookup", "N-gram lookup cache", lookup_dir(), _files(lookup_dir())))

    # local reply cache
    rf = _files(replies_dir())
    rs = _store("replies", "Local reply cache", replies_dir(), rf)
    max_rows, max_days = _reply_limits(cfg)
    rs["rows"], rs["max_rows"], rs["max_days"], rs["rows_to_drop"] = 0, max_rows, max_days, 0
    db = os.path.join(replies_dir(), "responses.db")
    if os.path.isfile(db):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                n = c.execute("select count(*) from responses").fetchone()[0]
                old = c.execute("select count(*) from responses where created < ?",
                                (now - max_days * 86400,)).fetchone()[0] if max_days else 0
            finally:
                c.close()
            rs["rows"] = int(n)
            rs["rows_to_drop"] = int(old) + max(0, (n - old) - max_rows) if max_rows else int(old)
            rs["over_cap"] = bool(max_rows) and n > max_rows
        except sqlite3.Error:
            pass
    out.append(rs)

    # Claude reply cache
    cf = _files(cloud_dir(), (".json",), recursive=False)
    cs = _store("cloud", "Claude reply cache", cloud_dir(), cf)
    cs["orphans"] = [{"file": f["file"], "bytes": f["bytes"], "mtime": f["mtime"], "why": "past the 30-day rule for Claude replies"}
                     for f in cf if now - f["mtime"] > CLOUD_MAX_AGE]
    cs["orphan_bytes"] = sum(o["bytes"] for o in cs["orphans"])
    out.append(cs)

    # route cache
    rp = route_file()
    rt = _store("route", "Router decisions", rp, _files(os.path.dirname(rp), ("route_cache.json",), recursive=False))
    rt["entries"], rt["max_entries"] = 0, ROUTE_MAX
    try:
        with open(rp, encoding="utf-8") as fh:
            d = json.load(fh)
        rt["entries"] = len(d) if isinstance(d, dict) else 0
        rt["over_cap"] = rt["entries"] > ROUTE_MAX
    except (OSError, ValueError):
        pass
    out.append(rt)
    return out


# ------------------------------------------------------------------------------------------------ the plan
def plan_prune(cfg=None, inv=None, roots=None, now=None):
    """What `prune(..., yes=True)` would delete, and why. Reads only. -> {items: [{store, file, bytes, why}], bytes, reply_rows,
    kept: [{file, why}], slot_bytes_after}"""
    cfg = _cfg(cfg)
    now = now or time.time()
    inv = inv if inv is not None else inventory(cfg, roots=roots, now=now)
    by = {s["id"]: s for s in inv}
    prot, pwhy = protected_slots(now)
    items, chosen = [], set()
    sl = by.get("slots") or {}
    kept = [{"file": r["file"], "why": pwhy.get(os.path.normcase(r["file"]), "in use")}
            for r in sorted(sl.get("orphans", []) + sl.get("owned", []) + sl.get("unknown", []), key=lambda r: r["file"])
            if os.path.normcase(r["file"]) in prot]

    def add(store, row, why):
        k = os.path.normcase(row["file"])
        if k in chosen or (store == "slots" and k in prot):
            return
        chosen.add(k)
        items.append({"store": store, "file": row["file"], "bytes": row["bytes"], "why": why})

    for o in sl.get("orphans", []):
        add("slots", o, "orphan: " + o["why"])
    for o in sl.get("stale", []):
        add("slots", o, "stale: " + o["why"])
    cap = sl.get("cap_bytes") or 0
    allrows = sorted((sl.get("orphans", []) + sl.get("owned", []) + sl.get("unknown", [])), key=lambda r: r["mtime"])
    remaining = sum(r["bytes"] for r in allrows if os.path.normcase(r["file"]) not in chosen)
    if cap and remaining > cap:
        for r in allrows:                                  # least recently used first
            if remaining <= cap:
                break
            k = os.path.normcase(r["file"])
            if k in chosen or k in prot:
                continue
            add("slots", r, f"beyond the {cap / GB:g} GB cap for saved prompt memory (least recently used)")
            remaining -= r["bytes"]
    for o in (by.get("cloud") or {}).get("orphans", []):
        add("cloud", o, o["why"])
    return {"items": items, "bytes": sum(i["bytes"] for i in items),
            "reply_rows": int((by.get("replies") or {}).get("rows_to_drop") or 0), "kept": kept, "slot_bytes_after": remaining,
            "retention": retention_plan(cfg, roots=roots, now=now)}


# ------------------------------------------------------------------------------------------------ deleting
def _log(action, path, nbytes, why):
    try:
        os.makedirs(os.path.dirname(log_path()), exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{action}\t{int(nbytes or 0)}\t{path}\t{why}\n")
    except OSError:
        pass


def allowed_to_delete(path):
    """-> '' when `path` is a cache file inside one of the cache folders, else the plain reason it is refused."""
    try:
        rp = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return "cannot resolve the path"
    if rp.endswith(MODEL_SUFFIXES):
        return "a model file is never deleted here"
    for d, suffixes in deletable_dirs().items():
        rd = os.path.normcase(os.path.realpath(d))
        try:
            inside = os.path.commonpath([rp, rd]) == rd and rp != rd
        except ValueError:
            inside = False
        if inside:
            if not rp.endswith(suffixes):
                return f"only {'/'.join(suffixes)} files are removed from {d}"
            if os.path.isdir(path):
                return "a folder is never deleted"
            return ""
    return "outside the cache folders"


def _delete(path, why, action):
    bad = allowed_to_delete(path)
    if bad:
        _log("refused", path, 0, bad)
        return None, bad
    try:
        n = os.path.getsize(path)
        os.remove(path)
    except FileNotFoundError:
        return 0, ""
    except OSError as e:
        _log("failed", path, 0, f"{why} ({e.__class__.__name__}: {e})")
        return None, f"could not delete: {e}"
    _log(action, path, n, why)
    return n, ""


def prune(plan, yes=False, cfg=None):
    """Apply a plan from `plan_prune`. Without yes=True nothing is deleted. -> {deleted: [{file, bytes, why}], refused: [{file, why}],
    freed, reply_rows, dry_run}"""
    out = {"deleted": [], "refused": [], "freed": 0, "reply_rows": 0, "dry_run": not yes, "retention_done": []}
    if not yes:
        return out
    for it in ((plan or {}).get("retention") or {}).get("items") or []:
        n, bad = _retention_apply(it)
        if n is None:
            out["refused"].append({"file": it.get("file") or it.get("root") or "", "why": bad})
        else:
            out["retention_done"].append({"store": it["store"], "kind": it["kind"], "file": it.get("file") or it.get("root"), "bytes": n,
                                          "count": it.get("count")})
            out["freed"] += n
    prot, _ = protected_slots()
    for it in (plan or {}).get("items") or []:
        p = it.get("file") or ""
        if os.path.normcase(p) in prot:
            out["refused"].append({"file": p, "why": "in use by a running server now"})
            continue
        n, bad = _delete(p, it.get("why") or "", "deleted")
        if n is None:
            out["refused"].append({"file": p, "why": bad})
        else:
            out["deleted"].append({"file": p, "bytes": n, "why": it.get("why") or ""})
            out["freed"] += n
    if (plan or {}).get("reply_rows"):
        try:
            import arch_cache
            rows, days = _reply_limits(cfg)
            gone = arch_cache.prune(max_rows=rows, max_days=days)
            out["reply_rows"] = gone
            if gone:
                _log("rows", arch_cache.db_path(), 0, f"{gone} reply-cache rows past {rows} rows / {days} days")
        except Exception:
            pass
    return out


def enforce_slot_cap(cfg=None, keep=None, now=None):
    """After a slot save: when the saved prompt memory passes storage.slot_cache_gb, delete the least recently used OTHER slot files,
    oldest first, until it fits. Never the one just saved (`keep`), never one a running server uses. Logged. -> [{file, bytes, why}]"""
    cap = slot_cap_bytes(cfg)
    if not cap:
        return []
    now = now or time.time()
    files = sorted(_files(slots_dir(), (".bin",)), key=lambda f: f["mtime"])
    total = sum(f["bytes"] for f in files)
    if total <= cap:
        return []
    prot, _ = protected_slots(now)
    kp = os.path.normcase(os.path.abspath(keep)) if keep else None
    gone = []
    for f in files:
        if total <= cap:
            break
        k = os.path.normcase(os.path.abspath(f["file"]))
        if k == kp or k in prot:
            continue
        why = f"beyond the {cap / GB:g} GB cap for saved prompt memory (least recently used), at a save"
        n, _bad = _delete(f["file"], why, "evicted")
        if n is not None:
            total -= f["bytes"]
            gone.append({"file": f["file"], "bytes": n, "why": why})
    return gone


# ------------------------------------------------------------------------------------------------ retention (storage.retention_days)
# Old LOGS and scratch records only. Never: training datasets, the ledger, settings (architect_config.json, config.*), backups (listed, never
# removed here), the model files, any project's source. Every removal is checked against the explicit allow-list in `retention_allowed`.
RETENTION_DEFAULT = 90
BACKUP_PATTERNS = ("config.backup", "config.good", "config.before", "ledger.backup", "ledger.json.bak", ".bak", "port.undo")


def retention_days(cfg=None):
    v = ((_cfg(cfg).get("storage") or {}).get("retention_days", RETENTION_DEFAULT))
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return RETENTION_DEFAULT


def _ci_dir():
    return os.path.join(core.data_dir(), "ci")


def _orch_test_dir():
    return os.path.join(core.data_dir(), "orchestration", "test_data")


def _tel_dir():
    return os.path.join(core.data_dir(), "telemetry")


def retention_allowed(path):
    """-> '' when `path` is a file retention may remove, else the reason it is refused."""
    try:
        rp = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return "cannot resolve the path"
    data = os.path.normcase(os.path.realpath(core.data_dir()))
    name = os.path.basename(rp)
    if rp.endswith(MODEL_SUFFIXES):
        return "a model file is never deleted here"
    if os.path.isdir(path):
        return "a folder is never deleted"
    if any(p in rp for p in (os.sep + "datasets" + os.sep, os.sep + "evals" + os.sep)) or name.startswith(("ledger", "architect_config")) \
            or any(b in name for b in BACKUP_PATTERNS):
        return "training data, the ledger, settings and backups are never removed"

    def inside(d):
        d = os.path.normcase(os.path.realpath(d))
        try:
            return os.path.commonpath([rp, d]) == d and rp != d
        except ValueError:
            return False
    if inside(_ci_dir()) and name.endswith((".json", ".log")):
        return ""
    if inside(_orch_test_dir()):
        return ""
    if os.path.dirname(rp) == data and name.startswith("ci_run") and name.endswith(".log"):
        return ""
    if os.path.dirname(rp) == os.path.normcase(os.path.realpath(_tel_dir())) and name == "events.jsonl.1":
        return ""
    return "not one of the log/scratch locations retention covers"


def _old_files(top, cutoff, suffixes=None):
    return [f for f in _files(top, suffixes) if f["mtime"] < cutoff]


def _logbook_old(cutoff):
    db = os.path.join(core.data_dir(), "logbook.db")
    if not os.path.isfile(db):
        return 0
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            return int(c.execute("select count(*) from tasks where started is not null and started > 0 and started < ?", (cutoff,)).fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return 0


def _telemetry_old_lines(cutoff):
    p = os.path.join(_tel_dir(), "events.jsonl")
    n = 0
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    t = float(json.loads(line).get("t") or 0)
                except (ValueError, AttributeError, TypeError):
                    continue
                if 0 < t < cutoff:
                    n += 1
    except OSError:
        return 0
    return n


def _ckpt_roots(roots):
    out = []
    for r in sorted(roots):
        if os.path.isdir(os.path.join(r, ".arch", "ckpt")) or os.path.isdir(os.path.join(r, ".git")):
            out.append(r)
    return out


def _ref_env(root):
    gd = os.path.join(root, ".arch", "ckpt", "git")
    if os.path.isdir(gd):
        return {"GIT_DIR": gd}
    return {} if os.path.isdir(os.path.join(root, ".git")) else None


def _old_refs(root, cutoff):
    """-> [refname] under refs/architect/ whose commit is older than cutoff. Reads only; never creates a shadow repo."""
    try:
        import arch_checkpoint as ck
        env = _ref_env(root)
        if env is None:
            return []
        p = ck._run(["for-each-ref", "--format=%(refname) %(committerdate:unix)", "refs/architect/"], env, root, check=False)
        out = []
        for ln in p.stdout.decode("utf-8", "replace").splitlines():
            ref, _, ts = ln.partition(" ")
            if ts.strip().isdigit() and int(ts) < cutoff:
                out.append(ref)
        return out
    except Exception as e:
        core._tel_swallow("cachehygiene._old_refs", e)
        return []


def list_backups():
    """Backups in the data folder (config.backup*/good*, ledger backups, *.bak*): listed for the person, never removed."""
    out = []
    try:
        for fn in sorted(os.listdir(core.data_dir())):
            p = os.path.join(core.data_dir(), fn)
            if os.path.isfile(p) and any(b in fn for b in BACKUP_PATTERNS):
                out.append({"file": p, "bytes": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    except OSError:
        pass
    return out


def retention_plan(cfg=None, roots=None, now=None):
    """What `storage.retention_days` would remove. Reads only. -> {days, items: [{store, kind: file|lines|rows|refs, ...}], bytes, backups}.
    kinds: file {file, bytes}; lines {file, count} (events.jsonl lines older than the cutoff); rows {count} (logbook tasks and their steps);
    refs {root, refs: [..], count} (git refs/architect/* checkpoints of a project)."""
    cfg = _cfg(cfg)
    now = now or time.time()
    days = retention_days(cfg)
    out = {"days": days, "items": [], "bytes": 0, "backups": list_backups()}
    if not days:
        return out
    cutoff = now - days * 86400
    why = f"older than {days} days (storage.retention_days)"
    for store, top, suf in (("ci", _ci_dir(), (".json", ".log")), ("orchestration_test_data", _orch_test_dir(), None)):
        for f in _old_files(top, cutoff, suf):
            out["items"].append({"store": store, "kind": "file", "file": f["file"], "bytes": f["bytes"], "why": why})
    try:
        for fn in os.listdir(core.data_dir()):
            p = os.path.join(core.data_dir(), fn)
            if fn.startswith("ci_run") and fn.endswith(".log") and os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                out["items"].append({"store": "ci", "kind": "file", "file": p, "bytes": os.path.getsize(p), "why": why})
    except OSError:
        pass
    old = os.path.join(_tel_dir(), "events.jsonl.1")
    if os.path.isfile(old) and os.path.getmtime(old) < cutoff:
        out["items"].append({"store": "telemetry", "kind": "file", "file": old, "bytes": os.path.getsize(old), "why": why})
    n = _telemetry_old_lines(cutoff)
    if n:
        out["items"].append({"store": "telemetry", "kind": "lines", "file": os.path.join(_tel_dir(), "events.jsonl"), "count": n, "bytes": 0,
                             "cutoff": cutoff, "why": f"{n} events {why}"})
    n = _logbook_old(cutoff)
    if n:
        out["items"].append({"store": "logbook", "kind": "rows", "file": os.path.join(core.data_dir(), "logbook.db"), "count": n, "bytes": 0,
                             "cutoff": cutoff, "why": f"{n} logged tasks (with their steps) {why}"})
    roots = known_roots() if roots is None else {os.path.abspath(r) for r in roots}
    for r in _ckpt_roots(roots):
        refs = _old_refs(r, cutoff)
        if refs:
            out["items"].append({"store": "checkpoints", "kind": "refs", "root": r, "refs": refs, "count": len(refs), "bytes": 0,
                                 "why": f"{len(refs)} checkpoint refs in {r} {why}"})
    out["bytes"] = sum(i.get("bytes") or 0 for i in out["items"])
    return out


def _retention_apply(item):
    """-> (freed_bytes or None, reason_if_refused)"""
    k = item.get("kind")
    if k == "file":
        p = item.get("file") or ""
        bad = retention_allowed(p)
        if bad:
            _log("refused", p, 0, bad)
            return None, bad
        try:
            n = os.path.getsize(p)
            os.remove(p)
        except FileNotFoundError:
            return 0, ""
        except OSError as e:
            _log("failed", p, 0, f"retention ({e.__class__.__name__})")
            return None, f"could not delete: {e}"
        _log("retention", p, n, item.get("why") or "")
        try:                                              # an emptied dated folder under test_data goes too (folders only, and only when empty)
            d = os.path.dirname(p)
            if os.path.normcase(os.path.realpath(d)).startswith(os.path.normcase(os.path.realpath(_orch_test_dir())) + os.sep):
                os.rmdir(d)
        except OSError:
            pass                                          # intentional: a folder that still holds files stays
        return n, ""
    if k == "lines":
        p, cutoff = item.get("file") or "", float(item.get("cutoff") or 0)
        if os.path.normcase(os.path.realpath(p)) != os.path.normcase(os.path.realpath(os.path.join(_tel_dir(), "events.jsonl"))) or not cutoff:
            return None, "not the telemetry stream"
        if time.time() - os.path.getmtime(p) < 60:
            return None, "the stream is being written now; try again in a minute"
        tmp, kept, dropped = p + ".trim", 0, 0
        try:
            with open(p, encoding="utf-8", errors="replace") as src, open(tmp, "w", encoding="utf-8", newline="") as dst:
                for line in src:
                    try:
                        t = float(json.loads(line).get("t") or 0)
                    except (ValueError, AttributeError, TypeError):
                        t = 0
                    if 0 < t < cutoff:
                        dropped += 1
                    else:
                        dst.write(line)
                        kept += 1
            os.replace(tmp, p)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass                                      # intentional: no temp file to clean
            return None, f"could not rewrite: {e}"
        _log("retention-lines", p, 0, f"{dropped} events dropped, {kept} kept")
        return 0, ""
    if k == "rows":
        p, cutoff = item.get("file") or "", float(item.get("cutoff") or 0)
        if os.path.basename(p) != "logbook.db" or not cutoff:
            return None, "not the logbook"
        ids = []
        try:
            c = sqlite3.connect(p, timeout=10)
            try:
                ids = [r[0] for r in c.execute("select id from tasks where started is not null and started > 0 and started < ?", (cutoff,))]
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    q = ",".join("?" * len(chunk))
                    c.execute(f"delete from steps where task_id in ({q})", chunk)
                    c.execute(f"delete from logbook_fts where task_id in ({q})", chunk)
                    c.execute(f"delete from tasks where id in ({q})", chunk)
                c.commit()
            finally:
                c.close()
        except sqlite3.Error as e:
            return None, f"logbook: {e}"
        _log("retention-rows", p, 0, f"{len(ids)} tasks")
        return 0, ""
    if k == "refs":
        root = item.get("root") or ""
        try:
            import arch_checkpoint as ck
            env = _ref_env(root)
            if env is None:
                return None, "no checkpoint store there"
            done = 0
            for ref in item.get("refs") or []:
                if str(ref).startswith("refs/architect/") and ck._run(["update-ref", "-d", ref], env, root, check=False).returncode == 0:
                    done += 1
            _log("retention-refs", root, 0, f"{done} refs")
            return 0, ""
        except Exception as e:
            core._tel_swallow("cachehygiene._retention_apply.refs", e)
            return None, f"checkpoints: {type(e).__name__}"
    return None, "unknown retention item"


# ------------------------------------------------------------------------------------------------ plain words
def _gb(n):
    n = int(n or 0)
    if n >= GB:
        return f"{n / GB:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} bytes"


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else "-"


def report_lines(inv, plan, result=None):
    L = ["What the Architect keeps on disk to go faster:"]
    for s in inv:
        head = f"  {s['name']}: {s['files']} file(s), {_gb(s['bytes'])}"
        if s["files"]:
            head += f" (oldest {_day(s['oldest'])}, newest {_day(s['newest'])})"
        L.append(head)
        L.append(f"      {s['path']}")
        if s["id"] == "slots":
            cap = s.get("cap_bytes") or 0
            L.append(f"      limit {_gb(cap)}" + (" -- OVER the limit" if s["over_cap"] else "") if cap else "      no size limit set")
            L.append(f"      {len(s['orphans'])} orphan(s), {_gb(s['orphan_bytes'])}; {len(s['owned'])} with a known project; "
                     f"{len(s['unknown'])} of unknown owner (kept); {len(s['stale'])} unused for {s.get('max_age_days')}+ days")
        elif s["id"] == "replies":
            L.append(f"      {s['rows']} replies (limit {s['max_rows']} rows, {s['max_days']} days); {s['rows_to_drop']} past the limits")
        elif s["id"] == "route":
            L.append(f"      {s['entries']} decisions (limit {s['max_entries']}, 30 days; trimmed on each save)")
        elif s["id"] == "cloud":
            L.append(f"      {len(s['orphans'])} past the 30-day rule")
        elif s["id"] == "lookup":
            L.append("      grown by the worker server itself; reported only")
    L.append("")
    ret = plan.get("retention") or {}
    if not plan["items"] and not plan["reply_rows"] and not ret.get("items"):
        L.append("Nothing to remove.")
    else:
        L.append(f"Would remove {len(plan['items'])} file(s), {_gb(plan['bytes'])}"
                 + (f", and {plan['reply_rows']} reply-cache rows" if plan["reply_rows"] else "") + ":")
        for it in plan["items"]:
            L.append(f"  - {os.path.basename(it['file'])}  {_gb(it['bytes'])}  {it['why']}")
        sl = next((s for s in inv if s["id"] == "slots"), None)
        if sl:
            L.append(f"Saved prompt memory afterwards: {_gb(plan.get('slot_bytes_after'))}"
                     + (f" (limit {_gb(sl['cap_bytes'])})" if sl.get("cap_bytes") else ""))
    if ret.get("days"):
        L.append("")
        L.append(f"Logs and old records past storage.retention_days ({ret['days']} days):" + ("" if ret.get("items") else " none"))
        for it in ret.get("items") or []:
            what = os.path.basename(it.get("file") or it.get("root") or "")
            L.append(f"  - [{it['store']}] {what}  " + (f"{it['count']} {it['kind']}" if it.get("count") else _gb(it.get("bytes"))) + f"  {it['why']}")
    elif ret:
        L.append("")
        L.append("storage.retention_days is 0: old logs are kept.")
    for b in ret.get("backups") or []:
        L.append(f"  backup (listed only, never removed): {os.path.basename(b['file'])}  {_gb(b['bytes'])}  {_day(b['mtime'])}")
    for k in plan.get("kept") or []:
        L.append(f"  kept: {os.path.basename(k['file'])} -- {k['why']}")
    if result is None:
        if plan["items"] or plan["reply_rows"] or ret.get("items"):
            L.append("")
            L.append("Nothing was deleted. To remove exactly this list: python architect.py caches --prune --yes")
    elif result.get("dry_run"):
        L.append("")
        L.append("Nothing was deleted (add --yes to remove exactly this list).")
    else:
        L.append("")
        L.append(f"Removed {len(result['deleted']) + len([r for r in result.get('retention_done') or [] if r['kind'] == 'file'])} file(s), freed {_gb(result['freed'])}"
                 + (f"; applied {len(result['retention_done'])} retention item(s)" if result.get("retention_done") else "")
                 + (f"; dropped {result['reply_rows']} reply-cache rows" if result.get("reply_rows") else "")
                 + f". Each deletion is in {log_path()}.")
        for r in result.get("refused") or []:
            L.append(f"  not removed: {os.path.basename(r['file'])} -- {r['why']}")
    return L
