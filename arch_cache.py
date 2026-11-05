"""A reply the model already gave, kept so the same question never costs twice (standard library; SQLite; nothing leaves the machine).

Why this exists: a run that fails on step 4 is retried from step 1, an eval set is run before and after a change, a person asks the same
thing twice. On a laptop those repeats are the most expensive thing the harness does. When a request is *deterministic* -- temperature at
zero, so the model would give the same answer again -- the reply can be remembered and handed back in microseconds.

The honest rules, because a cache that lies is worse than no cache:
  * only at temperature 0. A creative call (temperature above 0.01) is never stored and never served: it is supposed to differ.
  * the key includes the model, every message byte, the sampling options that change the answer, and the JSON schema. Change any of them
    and it is a different question. `fingerprint` lets a caller add the model file's own hash, so re-quantizing a model empties its answers.
  * off during `eval`. An eval that scores cached answers measures the cache, not the models. `ARCHITECT_EVAL=1` disables it outright.
  * a hit carries the verdict the step reached last time (`pass`/`fail`/`not_sure`): the caller learns "you have asked this, and it did not
    pass" rather than silently repeating a failure.
  * `why_not()` always gives a plain reason a call was not cached, so a person can see why instead of guessing.

What it reports: `stats()` returns hits, misses and the tokens and seconds actually saved -- a measured number, not an estimate, because
every stored row keeps what the original call cost.

Storage: `<data_dir>/cache/responses.db`, one row per answer, pruned by age and by count (least recently used first). Every operation opens
and closes its own connection: a long-lived handle on Windows locks the file and breaks the harness's own cleanup.
"""
import hashlib
import json
import os
import sqlite3
import time

import arch_core as core

MAX_ROWS = 4000                    # about 40 MB of replies at the sizes this harness produces
MAX_DAYS = 30
MAX_REPLY = 200000                 # a reply longer than this is not worth storing
TEMP_EPS = 0.01                    # at or below this, the model is deterministic enough to reuse

SCHEMA = """
create table if not exists responses (
  k           text primary key,
  model       text not null,
  tier        text    default '',
  reply       text not null,
  tokens_in   integer default 0,
  tokens_out  integer default 0,
  secs        real    default 0,
  schema_name text    default '',
  verdict     text    default '',
  created     real,
  used        real,
  hits        integer default 0
);
create index if not exists responses_used on responses(used);
create table if not exists totals (k text primary key, v real);
"""


def db_path():
    return os.path.join(core.data_dir(), "cache", "responses.db")


def _open(create=True):
    """A fresh connection, schema in place. Returns None when SQLite cannot be used at all (the harness then just runs uncached)."""
    p = db_path()
    try:
        if create:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        elif not os.path.isfile(p):
            return None
        c = sqlite3.connect(p, timeout=3.0)
        c.executescript(SCHEMA)
        return c
    except (sqlite3.Error, OSError):
        return None


def _bump(c, name, by=1.0):
    c.execute("insert into totals(k, v) values(?, ?) on conflict(k) do update set v = v + ?", (name, by, by))


# ---------------------------------------------------------------- what may be cached

def deterministic(opts):
    """True when the sampler would give the same answer again.

    Two ways that happens. Temperature at zero: the model always takes its most likely next word, so the reply is fixed. Or a seed is
    set: the dice are the same dice, so the same prompt gives the same reply. An unstated temperature with no seed counts as varying --
    the harness does not guess.

    Why a retry is not harmed by this: a retry appends the failure to the prompt, so it is a different question with a different key, and
    the model is free to answer differently. Only a genuinely identical question is answered from memory.
    """
    opts = opts or {}
    if opts.get("seed") is not None:
        return True
    if "temperature" not in opts and "temp" not in opts:
        return False
    try:
        t = float(opts.get("temperature", opts.get("temp")))
    except (TypeError, ValueError):
        return False
    return t <= TEMP_EPS


def enabled(cfg=None):
    if os.environ.get("ARCHITECT_EVAL") == "1" or os.environ.get("ARCHITECT_NO_CACHE") == "1":
        return False
    cfg = cfg or {}
    return bool(((cfg.get("cache") or {}).get("responses", True)))


def why_not(cfg, opts):
    """'' when this call may be cached, else one plain sentence saying why not."""
    if os.environ.get("ARCHITECT_EVAL") == "1":
        return "not cached: an eval must measure the models, not the cache"
    if os.environ.get("ARCHITECT_NO_CACHE") == "1":
        return "not cached: ARCHITECT_NO_CACHE is set"
    if not enabled(cfg):
        return "not cached: the reply cache is turned off in settings"
    if not deterministic(opts):
        t = (opts or {}).get("temperature", (opts or {}).get("temp"))
        tail = " (set a seed in Settings > Behaviour to make whole runs repeatable, and reusable)"
        return (f"not cached: temperature {t} means the model is meant to vary its answer" + tail) if t is not None else \
               ("not cached: this call fixes neither a temperature nor a seed, so the same answer is not guaranteed" + tail)
    return ""


# ---------------------------------------------------------------- the key

def key(model, messages, opts=None, schema=None, fingerprint=""):
    """One hex id for this exact question. Anything that changes the answer goes in; anything that does not is left out."""
    opts = opts or {}
    try:
        raw = opts.get("temperature", opts.get("temp"))
        temp = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        temp = None
    seeded_warm = opts.get("seed") is not None and (temp is None or temp > TEMP_EPS)
    keep = {}
    for k, v in sorted(opts.items()):
        if k in ("stream", "keep_alive", "num_keep", "on_token", "timeout"):
            continue                        # none of these change the reply
        if k == "seed" and not seeded_warm:
            continue                        # at temperature 0 the seed is never consulted, so two seeds ask the same question
        keep[k] = v
    payload = {
        "model": str(model or ""),
        "fp": str(fingerprint or ""),
        "msgs": [{"role": str(m.get("role", "")), "content": str(m.get("content", ""))} for m in (messages or [])],
        "opts": keep,
        "schema": schema if isinstance(schema, (dict, list, str)) else None,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------- read and write

def get(k, cfg=None):
    """The remembered reply, or None. A hit records what it saved, so `stats()` can report a measured number."""
    if not enabled(cfg):
        return None
    c = _open()                        # create: a miss must be counted too, or the hit rate we report is a lie
    if not c:
        return None
    try:
        row = c.execute("select reply, model, tier, tokens_in, tokens_out, secs, verdict, hits, created "
                        "from responses where k = ?", (k,)).fetchone()
        if not row:
            _bump(c, "misses")
            c.commit()
            return None
        now = time.time()
        c.execute("update responses set hits = hits + 1, used = ? where k = ?", (now, k))
        _bump(c, "hits")
        _bump(c, "tokens_saved", float(row[3] or 0) + float(row[4] or 0))
        _bump(c, "secs_saved", float(row[5] or 0))
        c.commit()
        return {"reply": row[0], "model": row[1], "tier": row[2], "tokens_in": int(row[3] or 0),
                "tokens_out": int(row[4] or 0), "secs": float(row[5] or 0), "verdict": row[6] or "",
                "hits": int(row[7] or 0) + 1, "created": float(row[8] or 0), "cached": True}
    except sqlite3.Error:
        return None
    finally:
        c.close()


def limits(cfg=None):
    """-> (max_rows, max_days) from Settings > Reply cache (cache.max_rows / cache.max_days); 0 = that limit is off."""
    c = (cfg or {}).get("cache") or {}
    out = []
    for name, dflt in (("max_rows", MAX_ROWS), ("max_days", MAX_DAYS)):
        try:
            out.append(max(0, int(c.get(name, dflt) if c.get(name, dflt) is not None else dflt)))
        except (TypeError, ValueError):
            out.append(dflt)
    return out[0], out[1]


TRIM_AGE_EVERY = 50                # the age sweep runs on every 50th store; the row cap is checked on every store


def _trim(c, cfg, now):
    """Keep the store within the person's limits as it is written (until 2026-11-05 `prune` existed but nothing called it, so the
    caps in Settings were never applied): past max_rows the least recently used rows go; every TRIM_AGE_EVERY stores, rows older
    than max_days go too."""
    max_rows, max_days = limits(cfg)
    if max_days:
        row = c.execute("select v from totals where k = 'stored'").fetchone()
        if row and int(row[0] or 0) % TRIM_AGE_EVERY == 0:
            c.execute("delete from responses where created < ?", (now - max_days * 86400,))
    if max_rows:
        n = c.execute("select count(*) from responses").fetchone()[0]
        if n > max_rows:
            c.execute("delete from responses where k in (select k from responses order by used asc limit ?)", (n - max_rows,))


def put(k, model, reply, tokens_in=0, tokens_out=0, secs=0.0, tier="", schema_name="", cfg=None):
    """Remember one reply. -> True when stored. Silently declines what is not worth keeping."""
    if not enabled(cfg) or not k or not reply or len(reply) > MAX_REPLY:
        return False
    c = _open()
    if not c:
        return False
    try:
        now = time.time()
        c.execute("insert into responses(k, model, tier, reply, tokens_in, tokens_out, secs, schema_name, verdict, created, used, hits) "
                  "values(?,?,?,?,?,?,?,?,'',?,?,0) "
                  "on conflict(k) do update set reply=excluded.reply, tokens_in=excluded.tokens_in, "
                  "tokens_out=excluded.tokens_out, secs=excluded.secs, used=excluded.used",
                  (k, str(model or ""), str(tier or ""), reply, int(tokens_in or 0), int(tokens_out or 0),
                   float(secs or 0), str(schema_name or ""), now, now))
        _bump(c, "stored")
        _trim(c, cfg, now)
        c.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        c.close()


def attach_verdict(k, verdict):
    """What the step that used this reply concluded. A later hit carries it, so a known failure is not repeated blindly."""
    if not k or not verdict:
        return False
    c = _open(create=False)
    if not c:
        return False
    try:
        c.execute("update responses set verdict = ? where k = ?", (str(verdict), k))
        c.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        c.close()


# ---------------------------------------------------------------- housekeeping and reporting

def prune(max_rows=MAX_ROWS, max_days=MAX_DAYS):
    """Drop what is old or least recently used. -> how many rows went."""
    c = _open(create=False)
    if not c:
        return 0
    try:
        gone = 0
        if max_days:
            cur = c.execute("delete from responses where created < ?", (time.time() - max_days * 86400,))
            gone += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if max_rows:
            n = c.execute("select count(*) from responses").fetchone()[0]
            if n > max_rows:
                cur = c.execute("delete from responses where k in (select k from responses order by used asc limit ?)", (n - max_rows,))
                gone += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
        return gone
    except sqlite3.Error:
        return 0
    finally:
        c.close()


def stats():
    """-> {rows, bytes, hits, misses, stored, tokens_saved, secs_saved, hit_rate, line}. Everything measured, nothing estimated."""
    out = {"rows": 0, "bytes": 0, "hits": 0, "misses": 0, "stored": 0, "tokens_saved": 0, "secs_saved": 0.0}
    p = db_path()
    if os.path.isfile(p):
        try:
            out["bytes"] = os.path.getsize(p)
        except OSError:
            pass
    c = _open(create=False)
    if c:
        try:
            out["rows"] = c.execute("select count(*) from responses").fetchone()[0]
            for k, v in c.execute("select k, v from totals").fetchall():
                if k in out:
                    out[k] = int(v) if k != "secs_saved" else round(float(v), 1)
        except sqlite3.Error:
            pass
        finally:
            c.close()
    seen = out["hits"] + out["misses"]
    out["hit_rate"] = round(100.0 * out["hits"] / seen, 1) if seen else 0.0
    out["line"] = (f"{out['rows']} remembered replies, {out['hit_rate']}% of lookups hit, "
                   f"saving {out['tokens_saved']} tokens and {out['secs_saved']}s of waiting so far")
    return out


def clear():
    """Forget everything. -> True when the file is gone (or was never there)."""
    p = db_path()
    try:
        if os.path.isfile(p):
            os.remove(p)
        return not os.path.isfile(p)
    except OSError:
        return False
