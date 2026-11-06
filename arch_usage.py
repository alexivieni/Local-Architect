"""Usage meter: one small JSON for the hub top bar and a page. Read-only, local, no model call.

usage(window='day'|'week') -> {local tokens in/out by model and by tier, gpu_hours by tier, runs, passed, pass_rate, cache hit rates
(exact reply cache, llama prefix cache), claude spend (arch_costs)}. Sources: the runs' trace.jsonl files (calls, verdicts), the
telemetry stream's server.* events plus the live server state (GPU hours = time a tier held layers on the card), arch_costs for dollars.
Cached for 60 s per (window, roots, data folder); pass fresh=True to bypass. Never raises: a missing source is a zero.
"""
import json
import os
import time

WINDOWS = {"day": 86400, "week": 7 * 86400}
TTL = 60.0
_CACHE = {}


def _data_dir():
    try:
        import arch_core
        return arch_core.data_dir()
    except Exception:
        return os.environ.get("ARCHITECT_DATA_DIR") or "hub_data"


def _roots(roots):
    try:
        import arch_costs
        return arch_costs._roots(roots)
    except Exception:
        return [os.path.abspath(r) for r in (roots or [])]


RECENT_FILE = "recent_roots.json"


def remember_root(path):
    """Keeps a project folder the person worked in (hub_data/recent_roots.json, newest first, 30 kept) so the meter reads it later.
    Never raises; a folder that does not exist is not kept."""
    try:
        p = os.path.abspath(str(path or ""))
        if not os.path.isdir(p):
            return False
        f = os.path.join(_data_dir(), RECENT_FILE)
        try:
            with open(f, encoding="utf-8") as fh:
                cur = [x for x in (json.load(fh) or []) if isinstance(x, str)]
        except (OSError, ValueError):
            cur = []
        if cur and cur[0] == p:
            return True
        cur = [p] + [x for x in cur if x != p]
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(cur[:30], fh)
        return True
    except Exception:
        return False


def known_roots(extra=None):
    """Every project folder this install knows about: the portfolio, the folders the hub remembered (recent_roots.json) plus the page's
    own recent list (`extra`), the practice project, and the eval run folders (<data>/evals/runs/train|bench). Only folders that exist."""
    out, seen = [], set()

    def add(p):
        try:
            p = os.path.abspath(str(p))
        except Exception:
            return
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    for r in _roots(None):
        add(r)
    try:
        with open(os.path.join(_data_dir(), RECENT_FILE), encoding="utf-8") as fh:
            for r in json.load(fh) or []:
                add(r)
    except (OSError, ValueError, TypeError):
        pass
    for r in extra or []:
        add(r)
    d = _data_dir()
    add(os.path.join(d, "practice", "textkit"))
    for b in ("train", "bench"):
        add(os.path.join(d, "evals", "runs", b))
    return out


def _scan_traces(roots, cutoff):
    """-> (calls, runs) inside the window. A call = dict of the fields we use; a broken line is skipped. runs = {run folder: verdict}."""
    calls, runs = [], {}
    for root in roots:
        rd = os.path.join(root, ".arch", "runs")
        try:
            names = os.listdir(rd)
        except OSError:
            continue
        for n in names:
            tp = os.path.join(rd, n, "trace.jsonl")
            try:
                if os.path.getmtime(tp) < cutoff:
                    continue
                f = open(tp, "r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                for line in f:
                    if '"call"' not in line and '"verdict"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(j, dict):
                        continue
                    t = j.get("t")
                    t = float(t) if isinstance(t, (int, float)) else None
                    if t is not None and t < cutoff:
                        continue
                    if j.get("type") == "call":
                        calls.append({k: j.get(k) for k in ("model", "tier", "transport", "prompt_tokens", "gen_tokens", "cache_n",
                                                            "cached", "done_reason")})
                    elif j.get("type") == "verdict":
                        runs[os.path.join(root, n)] = j.get("verdict")
    return calls, runs


def _gpu_hours(now, cutoff):
    """Hours each tier held layers on the card in the window, from server.ready ... stop/crash/aside telemetry, plus tiers still
    running (server.json). -> {tier: hours}"""
    ivs, open_ = {}, {}
    try:
        import arch_telemetry
        evs = arch_telemetry.read(since=cutoff - 7 * 86400, kinds=("server",), limit=0)
    except Exception:
        evs = []
    for r in evs:
        k, t, tier = r.get("kind"), float(r.get("t") or 0), r.get("tier")
        if not tier:
            continue
        on_card = int(r.get("layers_on_card") or 0) > 0 and not r.get("on_cpu")
        if k == "server.ready":
            if on_card:
                open_.setdefault(tier, t)
            else:
                open_.pop(tier, None)
        elif k in ("server.stop", "server.crash", "server.aside") and tier in open_:
            ivs.setdefault(tier, []).append([open_.pop(tier), t])
        elif k in ("server.back", "server.restart") and r.get("up", True) and on_card and tier not in open_:
            open_[tier] = t
    try:
        import arch_server
        procs = arch_server.load_state().get("processes") or {}
    except Exception:
        procs = {}
    for tier, t0 in list(open_.items()):
        if tier in procs:
            ivs.setdefault(tier, []).append([t0, now])
    for tier, rec in procs.items():            # up, but no ready event in the stream: from its own start time
        if tier not in open_ and not ivs.get(tier) and isinstance(rec, dict) and rec.get("started") \
                and int(rec.get("ngl") or 0) > 0 and not rec.get("on_cpu"):
            ivs.setdefault(tier, []).append([float(rec["started"]), now])
    out = {}
    for tier, lst in ivs.items():
        secs = sum(max(0.0, min(b, now) - max(a, cutoff)) for a, b in lst)
        if secs > 0:
            out[tier] = round(secs / 3600.0, 4)
    return out


def compute(window="day", roots=None, now=None):
    now = now or time.time()
    window = window if window in WINDOWS else "day"
    secs = WINDOWS[window]
    cutoff = now - secs
    rs = _roots(roots) if roots is not None else known_roots()
    calls, runs = _scan_traces(rs, cutoff)
    by_model, by_tier = {}, {}
    exact_hit = exact_total = pre_hit = pre_total = 0
    claude_in = claude_out = 0
    for c in calls:
        cached = bool(c.get("cached")) or c.get("done_reason") == "cached" or c.get("transport") == "cache"
        exact_total += 1
        if cached:
            exact_hit += 1
            continue                           # a reply served from the cache used no model
        if c.get("cache_n") is not None:
            pre_total += 1
            if int(c.get("cache_n") or 0) > 0:
                pre_hit += 1
        ti, to = int(c.get("prompt_tokens") or 0), int(c.get("gen_tokens") or 0)
        model, tier = str(c.get("model") or "?"), str(c.get("tier") or "?")
        if c.get("transport") == "claude" or model.startswith("claude:"):
            claude_in += ti
            claude_out += to
            continue
        for d, key in ((by_model, model), (by_tier, tier)):
            row = d.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            row["calls"] += 1
            row["tokens_in"] += ti
            row["tokens_out"] += to
    n_runs = len(runs)
    passed = sum(1 for v in runs.values() if v == "pass")
    claude = {"usd": 0.0, "tokens_in": claude_in, "tokens_out": claude_out, "by_model": []}
    try:
        import arch_costs
        s = arch_costs.summary(max(1, -(-secs // 86400)), roots=rs)
        claude["usd"] = s.get("cost_usd", 0.0)
        claude["by_model"] = [m for m in s.get("by_model", []) if m.get("usd")]
    except Exception:
        pass
    gpu = _gpu_hours(now, cutoff)
    tot_in = sum(r["tokens_in"] for r in by_model.values())
    tot_out = sum(r["tokens_out"] for r in by_model.values())
    srt = lambda d, k: [dict({k: n}, **v) for n, v in sorted(d.items(), key=lambda kv: -(kv[1]["tokens_in"] + kv[1]["tokens_out"]))]
    return {
        "window": window, "since": cutoff, "generated": now,
        "local": {"tokens_in": tot_in, "tokens_out": tot_out, "calls": sum(r["calls"] for r in by_model.values()),
                  "by_model": srt(by_model, "model"), "by_tier": srt(by_tier, "tier")},
        "gpu_hours": {"total": round(sum(gpu.values()), 4), "by_tier": gpu},
        "runs": n_runs, "passed": passed, "pass_rate": round(passed / n_runs, 3) if n_runs else None,
        "cache": {"exact_hit_rate": round(exact_hit / exact_total, 3) if exact_total else None, "exact_hits": exact_hit,
                  "prefix_hit_rate": round(pre_hit / pre_total, 3) if pre_total else None, "prefix_hits": pre_hit},
        "claude": claude,
        "headline": f"{tot_in + tot_out:,} local tokens, {n_runs} runs, ${claude['usd']:.2f} Claude",
    }


def usage(window="day", roots=None, fresh=False):
    """Cached (60 s) usage meter. roots=None reads every known project (known_roots()); a list reads exactly those. Never raises."""
    try:
        rs = _roots(roots) if roots is not None else known_roots()
    except Exception:
        rs = []
    key = (window, tuple(sorted(rs)), _data_dir())
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not fresh and now - hit[0] < TTL:
        return hit[1]
    try:
        out = compute(window, rs, now)
        out["roots"] = len(rs)
    except Exception as ex:
        out = {"window": window, "error": f"{type(ex).__name__}: {ex}", "runs": 0, "headline": "usage unavailable"}
    _CACHE[key] = (now, out)
    return out


def main(argv=None):
    import sys
    a = list(sys.argv[1:] if argv is None else argv)
    print(json.dumps(usage(a[0] if a else "day", fresh=True), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
