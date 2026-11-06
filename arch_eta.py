"""How long is left: an estimate for any job or queue from seconds MEASURED per task on this machine.

Where the seconds per task come from, best first (the answer always says which):
  1. the run itself, once it has finished a few tasks (its own pace beats any history);
  2. arch_costmodel points from the saved eval reports, matched on task family and model (then family alone, then model alone);
  3. the supervisor's own finished jobs (jobs.db secs), for queue work;
  4. a stated prior (PRIOR_S per task, said out loud as a guess) when nothing has been measured yet.

The answer is a range, not a promise: p25-p75 of the per-task seconds times the tasks left, with the median in the middle.

    eta = arch_eta.estimate(done=12, total=40, family="distributed", model="qwen3:8b")
    eta["line"]   ->  "12/40, ~1.4 h left (1.1-1.9 h), ends 03:40"
"""
import glob
import os
import time

PRIOR_S = 240.0            # a stated guess per task when nothing is measured: one local 8B build-and-test task on a 6 GB laptop
MIN_OWN = 3                # tasks this run must have finished before its own pace is trusted over history
_MEMO = {"t": 0.0, "rows": None}


def _pct(xs, q):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _eval_rows(ttl=120.0):
    """arch_costmodel.from_evals over the saved reports, memoised (they are re-read at most every ttl seconds)."""
    if _MEMO["rows"] is not None and time.time() - _MEMO["t"] < ttl:
        return _MEMO["rows"]
    rows = []
    try:
        import arch_core as core
        import arch_costmodel
        rows = arch_costmodel.from_evals(sorted(glob.glob(os.path.join(core.data_dir(), "evals", "*.json"))))
    except Exception:
        rows = []
    _MEMO.update(t=time.time(), rows=rows)
    return rows


def _supervisor_secs(flow=None):
    try:
        import arch_supervisor as sv
        jobs = sv.listing(states=("done", "failed", "not_sure", "dead"), limit=200)
    except Exception:
        return []
    return [float(j["secs"]) for j in jobs if (j.get("secs") or 0) > 0 and (not flow or j.get("flow") == flow)]


def per_task(family=None, model=None, flow=None, own=None, rows=None):
    """Seconds per task -> {"p25", "p50", "p75", "n", "source"}. `own` = seconds of the tasks this run has already finished."""
    own = [float(x) for x in (own or []) if x]
    if len(own) >= MIN_OWN:
        return _stats(own, "this run's own pace")
    rows = _eval_rows() if rows is None else rows
    fam, mod = (family or "").lower(), (model or "").lower()
    tiers = []
    if fam and mod:
        tiers.append(([r for r in rows if str(r.get("kind", "")).lower() == fam and str(r.get("model", "")).lower() == mod],
                      f"measured: {family} tasks on {model}"))
    if fam:
        tiers.append(([r for r in rows if str(r.get("kind", "")).lower() == fam], f"measured: {family} tasks, any model"))
    if mod:
        tiers.append(([r for r in rows if str(r.get("model", "")).lower() == mod], f"measured: {model}, any task family"))
    for picked, why in tiers:
        secs = [x.get("secs") for r in picked for x in (r.get("runs") or []) if x.get("secs")]
        if secs:
            return _stats(secs + own, why)
    sv = _supervisor_secs(flow)
    if sv:
        return _stats(sv + own, "measured: finished queue tasks" + (f" ({flow})" if flow else ""))
    secs = [x.get("secs") for r in rows for x in (r.get("runs") or []) if x.get("secs")]
    if secs:
        return _stats(secs + own, "measured: every saved eval task")
    if own:
        return _stats(own, "this run's own pace (few tasks yet)")
    return {"p25": PRIOR_S * 0.6, "p50": PRIOR_S, "p75": PRIOR_S * 1.6, "n": 0,
            "source": f"a guess: about {int(PRIOR_S // 60)} min per task (nothing measured yet)"}


def _stats(xs, source):
    return {"p25": _pct(xs, 0.25), "p50": _pct(xs, 0.5), "p75": _pct(xs, 0.75), "n": len(xs), "source": source}


def human(secs):
    secs = max(0.0, float(secs or 0))
    if secs < 90:
        return f"{int(round(secs))} s"
    if secs < 5400:
        return f"{int(round(secs / 60))} min"
    return f"{secs / 3600:.1f} h"


def estimate(done, total, family=None, model=None, flow=None, own=None, now=None, rows=None, until=None):
    """-> {done, total, left, secs_lo, secs_mid, secs_hi, ends, ends_lo, ends_hi, source, n, line}. `until` = an epoch time the work
    stops at regardless (a data night's end): the end time is capped there and the line says how many tasks fit."""
    now = time.time() if now is None else now
    done, total = max(0, int(done or 0)), max(0, int(total or 0))
    left = max(0, total - done)
    pt = per_task(family, model, flow, own, rows)
    lo, mid, hi = (left * (pt[k] or 0) for k in ("p25", "p50", "p75"))
    out = {"done": done, "total": total, "left": left, "secs_lo": round(lo), "secs_mid": round(mid), "secs_hi": round(hi),
           "per_task": {k: (round(pt[k], 1) if pt[k] is not None else None) for k in ("p25", "p50", "p75")},
           "source": pt["source"], "n": pt["n"], "ends": now + mid, "ends_lo": now + lo, "ends_hi": now + hi, "fits": None}
    if until:
        room = max(0.0, float(until) - now)
        out["fits"] = min(left, int(room // pt["p50"])) if pt["p50"] else left
        out["ends"] = min(out["ends"], float(until))
    out["ends_hm"] = time.strftime("%H:%M", time.localtime(out["ends"]))
    if not left:
        out["line"] = f"{done}/{total}, finished"
    else:
        rng = f" ({human(lo)}-{human(hi)})" if hi - lo >= 60 else ""
        out["line"] = f"{done}/{total}, ~{human(mid)} left{rng}, ends {out['ends_hm']}"
        if until and out["fits"] is not None and out["fits"] < left:
            out["line"] = f"{done}/{total}, about {out['fits']} more fit before {time.strftime('%H:%M', time.localtime(until))}"
    return out


def queue_eta(jobs=None, now=None, rows=None):
    """The supervisor queue: pending tasks plus the running one, each from its own flow's measured seconds, each capped by its
    own time limit (a task cannot take longer than its budget). -> estimate()-shaped dict."""
    try:
        import arch_supervisor as sv
        jobs = sv.listing(limit=500) if jobs is None else jobs
    except Exception:
        jobs = jobs or []
    now = time.time() if now is None else now
    pend = [j for j in jobs if j.get("state") == "pending"]
    run_ = [j for j in jobs if j.get("state") == "running"]
    fin = [j for j in jobs if j.get("state") in ("done", "failed", "not_sure", "dead")]
    lo = mid = hi = 0.0
    srcs = set()
    for j in pend + run_:
        pt = per_task(flow=j.get("flow") or None, family=j.get("flow") or None, rows=rows)
        cap = max(60.0, float(j.get("minutes") or 10) * 60)
        a, b, c = (min(cap, pt[k] or cap) for k in ("p25", "p50", "p75"))
        if j.get("state") == "running" and j.get("started"):
            spent = max(0.0, now - float(j["started"]))
            a, b, c = (max(0.0, min(cap, x) - spent) for x in (a, b, c))
        lo, mid, hi = lo + a, mid + b, hi + c
        srcs.add(pt["source"])
    done, total = len(fin), len(fin) + len(pend) + len(run_)
    out = {"done": done, "total": total, "left": len(pend) + len(run_), "secs_lo": round(lo), "secs_mid": round(mid), "secs_hi": round(hi),
           "source": "; ".join(sorted(srcs)) or "nothing waiting", "ends": now + mid}
    out["ends_hm"] = time.strftime("%H:%M", time.localtime(out["ends"]))
    out["line"] = (f"{done}/{total}, ~{human(mid)} left" + (f" ({human(lo)}-{human(hi)})" if hi - lo >= 60 else "") + f", ends {out['ends_hm']}"
                   if out["left"] else f"{done}/{total}, nothing waiting")
    return out


def eval_line(i, total, secs_so_far, family=None, model=None):
    """The suffix an `eval` progress line carries after task i of total: '  [3/40, ~2.1 h left (1.8-2.6 h), ends 04:10]'."""
    try:
        return "  [" + estimate(i, total, family=family, model=model, own=secs_so_far)["line"] + "]"
    except Exception:
        return ""
