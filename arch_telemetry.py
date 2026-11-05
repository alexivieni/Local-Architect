"""One local telemetry stream: <data>/telemetry/events.jsonl.

Every record has one envelope: {t, kind, sid, parent, run, ...fields}. Spans also carry t0, t1, secs, status.
Cheap and safe: writes are buffered and flushed in batches (fsync per batch), nothing here ever raises, the file rolls at
behavior.telemetry_mb (one .1 generation kept) and behavior.telemetry = off makes every call a no-op.
ARCHITECT_TELEMETRY=off (env) wins over the setting. stdlib only.
"""
import contextlib
import contextvars
import itertools
import json
import os
import threading
import time

_LOCK = threading.RLock()
_BUF = []
_LAST_FLUSH = [0.0]
_SEQ = itertools.count(1)
_PARENT = contextvars.ContextVar("arch_tel_parent", default=None)
_RUN = contextvars.ContextVar("arch_tel_run", default="")
_SWALLOW_SEEN = {}
FLUSH_N = 32
FLUSH_S = 2.0
_STATE = {"enabled": None, "cap_mb": None, "drops": 0, "diag_n": 0}
# Large free-text fields stay in trace.jsonl; the stream keeps sizes only.
_HEAVY = ("messages", "reply", "thinking", "prompt", "diff", "output", "stdout", "stderr")


def _data_dir():
    try:
        import arch_core
        return arch_core.data_dir()
    except Exception:
        return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_data")


def path():
    return os.path.join(_data_dir(), "telemetry", "events.jsonl")


def _cfg_value(key, default):
    try:
        import arch_core
        cfg = arch_core.load_config() if hasattr(arch_core, "load_config") else {}
        cur = cfg
        for part in key.split("."):
            cur = cur[part]
        return cur
    except Exception:
        return default


def configure(enabled=None, cap_mb=None):
    """Set from a loaded config (Engine init) or tests. None leaves a value to be read lazily."""
    with _LOCK:
        if enabled is not None:
            _STATE["enabled"] = bool(enabled)
        if cap_mb is not None:
            _STATE["cap_mb"] = float(cap_mb)


def configure_from(cfg):
    try:
        b = (cfg or {}).get("behavior", {})
        configure(str(b.get("telemetry", "on")).lower() != "off", b.get("telemetry_mb", 20))
    except Exception:
        pass


def enabled():
    if os.environ.get("ARCHITECT_TELEMETRY", "").lower() in ("off", "0", "false"):
        return False
    if _STATE["enabled"] is None:
        _STATE["enabled"] = str(_cfg_value("behavior.telemetry", "on")).lower() != "off"
    return _STATE["enabled"]


def _cap_bytes():
    if _STATE["cap_mb"] is None:
        try:
            _STATE["cap_mb"] = float(_cfg_value("behavior.telemetry_mb", 20))
        except Exception:
            _STATE["cap_mb"] = 20.0
    return max(0.05, _STATE["cap_mb"]) * 1024 * 1024


def new_id():
    return f"{os.getpid():x}-{int(time.time() * 1000) % 10 ** 9:x}-{next(_SEQ):x}"


def set_run(run_id):
    """Tag every later record in this context with run_id. Returns a token for reset."""
    return _RUN.set(str(run_id or ""))


import re  # noqa: E402
_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|passwd|authorization|auth|cookie|credential)", re.I)
_SECRET_VAL = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[abp]-[A-Za-z0-9\-]{8,}|AKIA[0-9A-Z]{12,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


def _scrub(v, depth=0):
    if isinstance(v, str):
        return _SECRET_VAL.sub("[redacted]", v) if len(v) < 20000 else v[:200] + "...[truncated]"
    if depth > 4:
        return v
    if isinstance(v, dict):
        return {k: ("[redacted]" if isinstance(k, str) and _SECRET_KEY.search(k) and not k.endswith(("_tokens", "_chars")) and not isinstance(v[k], (int, float, bool, type(None))) else _scrub(v[k], depth + 1)) for k in v}
    if isinstance(v, (list, tuple)):
        return [_scrub(x, depth + 1) for x in v[:200]]
    return v


def _light(fields):
    out = {}
    for k, v in _scrub(dict(fields)).items():
        if k in _HEAVY and isinstance(v, (str, list, dict)):
            try:
                out[k + "_chars"] = len(v) if isinstance(v, str) else len(json.dumps(v, ensure_ascii=False))
            except Exception:
                pass
            continue
        out[k] = v
    return out


def emit(kind, **fields):
    """Append one event. Returns its sid, or None when telemetry is off. Never raises."""
    try:
        if not enabled():
            return None
        rec = {"t": time.time(), "kind": str(kind), "sid": fields.pop("sid", None) or new_id(),
               "parent": fields.pop("parent", None) or _PARENT.get(), "run": fields.pop("run", None) or _RUN.get()}
        rec.update(_light(fields))
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _LOCK:
            _BUF.append(line)
            if len(_BUF) >= FLUSH_N or time.time() - _LAST_FLUSH[0] >= FLUSH_S:
                flush()
        return rec["sid"]
    except Exception:
        return None


def flush():
    """Write the buffer (one fsync per batch). Never raises; a failed write counts drops."""
    with _LOCK:
        if not _BUF:
            return True
        lines, _BUF[:] = list(_BUF), []
        _LAST_FLUSH[0] = time.time()
        try:
            p = path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            _roll(p)
            try:
                if os.path.getsize(p) >= 2 * _cap_bytes():
                    raise OSError("telemetry over cap and roll failed")
            except FileNotFoundError:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            return True
        except Exception:
            _STATE["drops"] += len(lines)
            return False


def _roll(p):
    try:
        if os.path.getsize(p) >= _cap_bytes():
            os.replace(p, p + ".1")
    except OSError:
        pass


def prune(max_age_s=14 * 86400):
    """Drop the rolled generation when it is older than max_age_s. -> number of files removed."""
    n = 0
    try:
        old = path() + ".1"
        if os.path.exists(old) and time.time() - os.path.getmtime(old) > max_age_s:
            os.remove(old)
            n += 1
    except OSError:
        pass
    return n


def drops():
    return _STATE["drops"]


@contextlib.contextmanager
def span(kind, **fields):
    """Time a block. Nested spans get parent = enclosing sid. The yielded dict can be updated (status, extra fields)."""
    sid = new_id()
    attrs = dict(fields)
    tok = _PARENT.set(sid)
    t0 = time.time()
    attrs.setdefault("status", "ok")
    try:
        yield attrs
    except BaseException as e:
        attrs["status"] = "error"
        attrs.setdefault("error", {"kind": type(e).__name__, "msg": str(e)[:300]})
        raise
    finally:
        _PARENT.reset(tok)
        t1 = time.time()
        emit(kind, sid=sid, t0=t0, t1=t1, secs=round(t1 - t0, 4), **attrs)


def swallow(where, exc):
    """Record an exception that a caller deliberately swallows. Rate-limited to one line per (where, type) per minute."""
    try:
        _STATE["diag_n"] += 1
        key = (str(where), type(exc).__name__)
        now = time.time()
        if now - _SWALLOW_SEEN.get(key, 0) < 60:
            return
        _SWALLOW_SEEN[key] = now
        emit("error", where=str(where), exc=type(exc).__name__, msg=str(exc)[:200])
    except Exception:
        pass


def diag_count():
    return _STATE["diag_n"]


# ---- reading ---------------------------------------------------------------------------------------------------------

_LEGACY_KIND = {"type": "kind"}


def upgrade(rec):
    """Legacy records (trace 'type', broker 'ts', ISO 'timestamp') -> the envelope. Old fields stay."""
    if not isinstance(rec, dict):
        return None
    if "kind" not in rec and "type" in rec:
        rec["kind"] = rec.get("type")
    if "t" not in rec:
        for k in ("ts", "timestamp", "logged"):
            v = rec.get(k)
            if isinstance(v, (int, float)):
                rec["t"] = float(v)
                break
            if isinstance(v, str):
                try:
                    import datetime
                    rec["t"] = datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                    break
                except Exception:
                    pass
    return rec


def _iter_file(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = upgrade(json.loads(line))
                except ValueError:
                    continue
                if r:
                    yield r
    except OSError:
        return


def read(since=None, run=None, kinds=None, limit=1000, file=None):
    """Newest-last list of events, filtered. kinds: str or iterable; a kind matches exactly or as a dotted prefix ('server')."""
    flush()
    ks = None
    if kinds:
        ks = (kinds,) if isinstance(kinds, str) else tuple(kinds)
    files = [file] if file else [path() + ".1", path()]
    out = []
    for p in files:
        for r in _iter_file(p):
            if since is not None and float(r.get("t") or 0) < since:
                continue
            if run is not None and r.get("run") != run:
                continue
            if ks and not any(r.get("kind") == k or str(r.get("kind", "")).startswith(k + ".") for k in ks):
                continue
            out.append(r)
    out.sort(key=lambda r: float(r.get("t") or 0))
    return out[-limit:] if limit else out


def summary(window_s=3600):
    """Aggregate the last window_s seconds."""
    evs = read(since=time.time() - window_s, limit=0)
    s = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cache_hit_share": None, "prefix_reuse": None,
         "errors": 0, "gpu_peak_gb": None, "by_kind": {}}
    hits = looked = 0
    pre_n = pre_tot = 0
    for r in evs:
        k = str(r.get("kind", ""))
        s["by_kind"][k] = s["by_kind"].get(k, 0) + 1
        if k == "call":
            s["calls"] += 1
            s["tokens_in"] += int(r.get("prompt_tokens") or 0)
            s["tokens_out"] += int(r.get("gen_tokens") or 0)
            c = r.get("cache") or {}
            st = c.get("state") if isinstance(c, dict) else None
            if r.get("cached"):
                st = "hit"
            if st in ("hit", "miss"):
                looked += 1
                hits += st == "hit"
            pe, pr = r.get("prompt_est"), r.get("prefix_reused_est")
            if isinstance(pe, (int, float)) and pe > 0 and isinstance(pr, (int, float)):
                pre_n += pr
                pre_tot += pe
        if k == "error" or r.get("status") == "error":
            s["errors"] += 1
        for g in (r.get("vram_used_gb"), (r.get("res_after") or {}).get("vram_used_gb") if isinstance(r.get("res_after"), dict) else None):
            if isinstance(g, (int, float)):
                s["gpu_peak_gb"] = g if s["gpu_peak_gb"] is None else max(s["gpu_peak_gb"], g)
    s["cache_hit_share"] = round(hits / looked, 4) if looked else None
    s["prefix_reuse"] = round(pre_n / pre_tot, 4) if pre_tot else None
    return s


def timeline(run_id):
    """Nested spans of one run: [{...event, children:[...]}] roots first by time."""
    evs = read(run=run_id, limit=0)
    by = {}
    for r in evs:
        r = dict(r)
        r["children"] = []
        by[r.get("sid")] = r
    roots = []
    for r in by.values():
        p = r.get("parent")
        if p and p in by and p != r.get("sid"):
            by[p]["children"].append(r)
        else:
            roots.append(r)
    key = lambda r: float(r.get("t0") or r.get("t") or 0)
    for r in by.values():
        r["children"].sort(key=key)
    return sorted(roots, key=key)


import atexit  # noqa: E402
atexit.register(flush)
