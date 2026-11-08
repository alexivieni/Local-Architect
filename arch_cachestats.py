"""Every cache, one ledger: what each layer holds, how often it is used, what it saved, and why an unused one is unused.

Read-only. Seven layers, in the order a request meets them, plus both ends of Claude:
  route     <data>/route_cache.json             a router decision reused (no model call)
  reply     <data>/cache/responses.db           a local reply reused (arch_cache; temperature 0 or a seed)
  cloud     <data>/cloud_cache/                 a Claude reply reused ($0)
  prefix    run traces (cache_n / prefix_reused_est)   the KV cache served rho of a local prompt (Eq. 15)
  slots     <data>/server/slots/<tier>/*.bin    a project's KV prefix saved on disk, restored on reopen
  lookup    <data>/server/lookup/*.bin          the n-gram lookup cache behind speculative decoding
  claude    Claude Code runs (<data>/cc_runs) and this machine's Claude Code transcripts (~/.claude/projects):
            prompt-cache reads vs writes vs uncached input, and the dollars the cache saved (arch_cloud.cost).

    python arch_cachestats.py            # plain-words report
"""
import collections
import glob
import json
import os
import sqlite3
import time

PROMPT_CACHE_GAP = (300, 3600)          # Anthropic prompt-cache lifetimes: 5 minutes, 1 hour


def _dir_stats(path, pattern="*"):
    files = [f for f in glob.glob(os.path.join(path, "**", pattern), recursive=True) if os.path.isfile(f)]
    st = [os.stat(f) for f in files]
    return {"files": len(files), "bytes": sum(s.st_size for s in st),
            "oldest": min((s.st_mtime for s in st), default=None), "newest": max((s.st_mtime for s in st), default=None)}


def _data():
    import arch_core
    return arch_core.data_dir()


# ------------------------------------------------------------------------------------------------ local layers
def route_layer(data):
    p = os.path.join(data, "route_cache.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"id": "route", "name": "Route decisions", "where": p, "entries": len(d),
            "bytes": os.path.getsize(p) if os.path.isfile(p) else 0, "hits": None, "misses": None}


def reply_layer(data):
    p = os.path.join(data, "cache", "responses.db")
    out = {"id": "reply", "name": "Local replies", "where": p, "entries": 0, "bytes": os.path.getsize(p) if os.path.isfile(p) else 0,
           "hits": 0, "misses": 0, "saved_tokens": 0, "saved_secs": 0.0}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        tot = dict(c.execute("select k, v from totals").fetchall())
        out["hits"], out["misses"] = int(tot.get("hits", 0)), int(tot.get("misses", 0))
        out["entries"] = c.execute("select count(*) from responses").fetchone()[0]
        # the running totals (what arch_cache.stats() and the CLI report), not a sum over surviving rows: a trimmed row's savings were real
        out["saved_tokens"], out["saved_secs"] = int(float(tot.get("tokens_saved", 0) or 0)), round(float(tot.get("secs_saved", 0) or 0), 1)
        c.close()
    except (sqlite3.Error, ValueError):
        pass
    return out


def cloud_layer(data):
    p = os.path.join(data, "cloud_cache")
    return dict({"id": "cloud", "name": "Claude replies", "where": p, "hits": None, "misses": None}, **{"entries": _dir_stats(p)["files"],
                "bytes": _dir_stats(p)["bytes"]})


def store_layer(data, kind):
    p = os.path.join(data, "server", "slots" if kind == "slots" else "lookup")
    s = _dir_stats(p, "*.bin")
    name = "KV slots on disk" if kind == "slots" else "N-gram lookup"
    out = {"id": kind, "name": name, "where": p, "entries": s["files"], "bytes": s["bytes"], "oldest": s["oldest"], "newest": s["newest"],
           "hits": None, "misses": None}
    if kind == "slots":
        eval_like = [f for f in glob.glob(os.path.join(p, "**", "*.bin"), recursive=True)
                     if os.path.basename(f).startswith(("eval_", "live_"))]
        out["orphans"] = len(eval_like)
        out["orphan_bytes"] = sum(os.path.getsize(f) for f in eval_like)
    return out


def agent_steps_layer(data):
    """arch_squad's step cache (crew agents reused by a later run with identical inputs); None when arch_squad has no cache_stats."""
    try:
        import arch_squad
        fn = getattr(arch_squad, "cache_stats", None)
        st = fn(data) if fn else None
    except Exception:
        st = None
    if not st:
        return None
    return {"id": "agent_steps", "name": "Agent steps", "where": st.get("path"), "entries": int(st.get("entries") or 0),
            "bytes": int(st.get("bytes") or 0), "hits": None, "misses": None, "why_not": st.get("why_not")}


def prefix_layer(roots=None):
    """Exact prefix reuse from run traces: rho = cached prompt tokens / prompt tokens (Eq. 15), per model and per flow."""
    if roots is None:
        try:
            import arch_costmodel
            roots = arch_costmodel.default_roots()
        except Exception:
            roots = [os.getcwd()]
    tot = {"calls": 0, "prompt": 0, "cached": 0, "exact": 0, "reply_hits": 0}
    by_model, by_flow = collections.defaultdict(lambda: [0, 0, 0]), collections.defaultdict(lambda: [0, 0, 0])
    runs = 0
    for root in roots:
        for t in glob.glob(os.path.join(root, ".arch", "runs", "*", "trace.jsonl")):
            runs += 1
            try:
                for line in open(t, encoding="utf-8", errors="replace"):
                    if '"type": "call"' not in line and '"type":"call"' not in line:
                        continue
                    try:
                        c = json.loads(line)
                    except ValueError:
                        continue
                    if c.get("transport") == "claude":
                        continue
                    if c.get("done_reason") == "cached":
                        tot["reply_hits"] += 1
                        continue
                    pt = int(c.get("prompt_tokens") or 0)
                    ca = min(pt, int(c.get("prefix_reused_est") or 0))
                    tot["calls"] += 1
                    tot["prompt"] += pt
                    tot["cached"] += ca
                    tot["exact"] += 1 if c.get("cached_exact") else 0
                    for key, d in ((c.get("model") or "?", by_model), (c.get("flow") or "?", by_flow)):
                        d[key][0] += 1
                        d[key][1] += pt
                        d[key][2] += ca
            except OSError:
                continue

    def rows(d):
        return sorted(({"name": k, "calls": v[0], "rho": round(v[2] / v[1], 3) if v[1] else None} for k, v in d.items()),
                      key=lambda r: -r["calls"])
    return {"id": "prefix", "name": "KV prefix (in memory)", "where": "llama-server, per call", "runs": runs, "calls": tot["calls"],
            "prompt_tokens": tot["prompt"], "cached_tokens": tot["cached"], "exact_share": round(tot["exact"] / tot["calls"], 3) if tot["calls"] else None,
            "rho": round(tot["cached"] / tot["prompt"], 3) if tot["prompt"] else None, "reply_hits_in_traces": tot["reply_hits"],
            "by_model": rows(by_model), "by_flow": rows(by_flow), "hits": None, "misses": None}


# ------------------------------------------------------------------------------------------------ Claude, both ends
_TX_MEMO = {}


def _usage_records(path):
    """(t, model, usage) for every assistant reply in a Claude Code run log or transcript; memoised by size+mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (path, st.st_size, st.st_mtime)
    if key in _TX_MEMO:
        return _TX_MEMO[key]
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            if "cache_read_input_tokens" not in line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            msg = j.get("message") if isinstance(j.get("message"), dict) else j
            u = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(u, dict) or "cache_read_input_tokens" not in u:
                continue
            t = j.get("t")
            if t is None and isinstance(j.get("timestamp"), str):
                try:
                    import datetime
                    t = datetime.datetime.fromisoformat(j["timestamp"].replace("Z", "+00:00")).timestamp()
                except ValueError:
                    t = None
            out.append((t, msg.get("model") or j.get("model"), {k: int(u.get(k) or 0) for k in
                        ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}))
    except OSError:
        pass
    _TX_MEMO[key] = out
    return out


def claude_layer(data, transcripts=True, transcript_dir=None, max_files=400):
    import arch_cloud
    sources = {"app": sorted(glob.glob(os.path.join(data, "cc_runs", "*.jsonl")))}
    if transcripts:
        td = transcript_dir or os.path.join(os.path.expanduser("~"), ".claude", "projects")
        sources["claude_code"] = sorted(glob.glob(os.path.join(td, "**", "*.jsonl"), recursive=True),
                                        key=lambda f: -os.path.getmtime(f))[:max_files]
    res = {}
    for src, files in sources.items():
        tot = collections.Counter()
        usd_paid = usd_nocache = 0.0
        gaps = []
        for f in files:
            recs = _usage_records(f)
            last = None
            for t, model, u in recs:
                tot["replies"] += 1
                for k, v in u.items():
                    tot[k] += v
                m = model or arch_cloud.DEFAULT_MODEL
                try:
                    usd_paid += arch_cloud.cost(m, u["input_tokens"], u["output_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"])
                    usd_nocache += arch_cloud.cost(m, u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"], u["output_tokens"])
                except Exception:
                    pass
                if t is not None and last is not None and t >= last:
                    gaps.append(t - last)
                last = t if t is not None else last
        inp = tot["input_tokens"] + tot["cache_read_input_tokens"] + tot["cache_creation_input_tokens"]
        res[src] = {"files": len(files), "replies": tot["replies"], "uncached_input": tot["input_tokens"],
                    "cache_read": tot["cache_read_input_tokens"], "cache_write": tot["cache_creation_input_tokens"],
                    "output": tot["output_tokens"], "read_share": round(tot["cache_read_input_tokens"] / inp, 3) if inp else None,
                    "usd_paid_est": round(usd_paid, 2), "usd_without_cache_est": round(usd_nocache, 2),
                    "usd_saved_est": round(usd_nocache - usd_paid, 2), "gaps": _gap_profile(gaps)}
    return {"id": "claude", "name": "Claude prompt cache", "where": "Anthropic, per call", "sources": res, "hits": None, "misses": None}


def _gap_profile(gaps):
    """Time between consecutive replies of one session: within 5 min the 5-minute cache holds; 5-60 min only a 1-hour cache does."""
    n = len(gaps)
    if not n:
        return None
    a = sum(1 for g in gaps if g <= PROMPT_CACHE_GAP[0])
    b = sum(1 for g in gaps if PROMPT_CACHE_GAP[0] < g <= PROMPT_CACHE_GAP[1])
    return {"n": n, "within_5m": round(a / n, 3), "5m_to_1h": round(b / n, 3), "over_1h": round((n - a - b) / n, 3)}


# ------------------------------------------------------------------------------------------------ the ledger
def _state(l):
    if l["id"] == "prefix":
        return "no data" if not l["calls"] else ("working" if (l["rho"] or 0) > 0.05 else "not reusing")
    if l["id"] == "claude":
        return "working" if any((s.get("cache_read") or 0) > 0 for s in l["sources"].values()) else "no data"
    if l.get("hits"):
        return "working"
    if l.get("entries"):
        return "stored, never reused" if l.get("hits") == 0 else "holding data"
    return "unused"


def ledger(roots=None, transcripts=True, transcript_dir=None, data=None):
    data = data or _data()
    layers = [route_layer(data), reply_layer(data), cloud_layer(data), prefix_layer(roots), store_layer(data, "slots"),
              store_layer(data, "lookup"), claude_layer(data, transcripts, transcript_dir)]
    steps = agent_steps_layer(data)
    if steps:
        layers.insert(len(layers) - 1, steps)          # before Claude: it is a local layer
    for l in layers:
        l["state"] = _state(l)
    return {"t": time.time(), "layers": layers, "findings": findings(layers)}


def findings(layers):
    L = {l["id"]: l for l in layers}
    out = []

    def add(sev, text):
        out.append({"severity": sev, "text": text})
    r = L["route"]
    if not r["entries"]:
        add("info", "Route cache is empty: it is off during benchmarks and self-tests on purpose, so it only fills from real runs "
                    "and hub route previews. Nothing is wrong; it has simply not been used yet.")
    rp = L["reply"]
    if rp["entries"] and not rp["hits"]:
        add("medium", f"The local reply cache stored {rp['entries']} replies and reused none. It only answers an identical prompt at "
                      "temperature 0 or with a seed; retries change the prompt tail, so it mostly helps repeated runs.")
    p = L["prefix"]
    if not p["calls"]:
        add("high", "No local prefix-reuse data yet: run traces from benchmarks were thrown away until 2026-11-05. The next live "
                    "eval (kept traces) will fill this in; it is the cache that matters most locally (Eq. 15).")
    elif p["rho"] is not None and p["rho"] < 0.5:
        add("medium", f"Local prefix reuse is {p['rho']:.0%} of prompt tokens. Retries should reuse most of the prompt; check that "
                      "the stable part comes first in the flows with the lowest reuse.")
    for row in p["by_flow"]:
        if row["calls"] >= 10 and row["rho"] is not None and row["rho"] < 0.3:
            add("medium", f"Flow '{row['name']}' reuses only {row['rho']:.0%} of its prompts from cache over {row['calls']} calls.")
    s = L["slots"]
    if s.get("orphan_bytes"):
        add("high", f"{s['orphans']} KV slot files ({s['orphan_bytes'] / 1e9:.1f} GB) belong to thrown-away eval or live-test folders "
                    "and can never be restored. `python architect.py caches` lists them; `--prune --yes` removes them.")
    try:
        import arch_core
        cap = float(((arch_core.load_config().get("storage") or {}).get("slot_cache_gb")) or 0)
    except Exception:
        cap = 0.0
    if cap and s["bytes"] > cap * 1e9:
        add("medium", f"KV slots use {s['bytes'] / 1e9:.1f} GB, over the {cap:g} GB cap (storage.slot_cache_gb); the next save trims the "
                      "least recently used files, and `python architect.py caches --prune --yes` clears the orphans now.")
    elif not cap and s["bytes"] > 4e9:
        add("medium", f"KV slots use {s['bytes'] / 1e9:.1f} GB with no cap (storage.slot_cache_gb = 0).")
    for src, c in L["claude"]["sources"].items():
        who = "the app's Claude runs" if src == "app" else "Claude Code on this machine"
        if c["replies"]:
            add("info", f"{who}: {c['read_share']:.0%} of input served from the prompt cache over {c['replies']} replies; "
                        f"estimated {c['usd_saved_est']:.2f} USD saved at API prices (paid about {c['usd_paid_est']:.2f}).")
            g = c.get("gaps")
            if g and g["5m_to_1h"] >= 0.1:
                add("medium", f"{who}: {g['5m_to_1h']:.0%} of the pauses between replies are 5-60 minutes, where only the 1-hour "
                              "cache lifetime keeps the prefix. online.cloud_cache_ttl = 1h pays when that share is high.")
    return out


def report(led):
    lines = ["Caches, in the order a request meets them:"]
    for l in led["layers"]:
        extra = ""
        if l["id"] == "prefix":
            extra = f"rho {l['rho']:.0%} over {l['calls']} calls" if l["rho"] is not None else "no traced calls yet"
        elif l["id"] == "claude":
            extra = "; ".join(f"{k}: {v['read_share']:.0%} read from cache, ~{v['usd_saved_est']} USD saved"
                              for k, v in l["sources"].items() if v["replies"])
        else:
            extra = f"{l.get('entries', 0)} entries, {l.get('bytes', 0) / 1e6:.1f} MB" + (f", {l['hits']} hits" if l.get("hits") else "")
        lines.append(f"  {l['name']:<24} {l['state']:<22} {extra}")
    lines.append("\nFindings:")
    lines += [f"  [{f['severity']}] {f['text']}" for f in led["findings"]] or ["  none"]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(ledger()))
