"""Route sense: what past runs on THIS machine say about which way of working suits a request.

Harness-level learning only: it counts outcomes, it never trains weights and it never decides a pass. It reads (never writes):
  <project>/.arch/runs/*/trace.jsonl + run.json   every portfolio project and the current folder (as arch_costs does)
  data_dir()/logbook.db                           the Logbook (arch_learn.py): Claude Code runs, fleets, ingested traces
A run seen in both places is counted once (the trace wins: it has times, tiers and per-call transports).

A sample is one finished run: (request, flow, main model + its tier, Claude mode, verdict, seconds, dollars).
  Claude mode: off (every call local) | plan (Claude only for planning roles) | escalation (some calls) | all (every call)
  | code (a Claude Code run from the hub) | fleet (several Claude Code agents). Inferred from the trace's per-call transport,
  because the setting at the time is not recorded; a run.json that carries "claude_mode" is taken at its word.
Verdicts have three outcomes, as everywhere in the app: pass, fail, and unknown (not_sure, stopped, unreadable). Unknown runs
are counted and shown, but excluded from the success rate in both directions.

learn()           -> per task kind (the flow, and the flow + the keyword that picked it, e.g. "bugfix:crash"), for each
                     (flow, model, Claude mode): runs, passed, failed, unknown, success rate, median seconds, median dollars and a
                     confidence flag: "ok" from MIN_OK judged runs, "low" from MIN_LOW, else "none" (no opinion).
advise(goal)      -> the best (flow, model, Claude mode) for this request, with the numbers and a plain-words why. Evidence is
                     taken from the narrowest group that has a confident answer: requests with the same words (any flow), then
                     the same kind, then the same flow. Options are ranked by the lower bound of their success rate (so 5 of 5
                     does not lose to 1 of 1), then by median cost, then by median time.
suggest_for(goal) -> the router's hook: None when there is no opinion, else {"flow", "model", "tier", "claude", "confidence",
                     "why", "hint"}. Needs no setting; never raises; results are kept in memory for CACHE_S seconds.

Integration (not wired in; one line in arch_flows.route, after `hint = ...` is built and before the router model is asked):
    hint += (__import__("arch_routelearn").suggest_for(goal, cfg=e.cfg) or {}).get("hint", "")
The router model still decides and code still checks its pick; the hint is evidence, labelled "measured on this machine".

    python arch_routelearn.py learn | advise "goal"
"""
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time

MIN_OK = 5          # judged runs (pass + fail) before an option has an opinion worth acting on
MIN_LOW = 3         # below this: no opinion at all
Z = 1.28            # ~80% one-sided: the success-rate lower bound used for ranking
SIMILAR_MIN = 2     # shared request words for "a request like this one"
CACHE_S = 300
PLAN_ROLES = {"planner", "architect", "designer", "plan", "router"}
CLAUDE_MODES = ("off", "plan", "escalation", "all", "code", "fleet")
_CACHE = {"t": 0.0, "key": None, "samples": None}


# ---------------------------------------------------------------- small helpers
def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _verdict(v):
    s = str(v if v is not None else "").strip().lower()
    if s in ("pass", "passed", "ok", "done", "success", "true", "completed"):
        return "pass"
    if s in ("fail", "failed", "error", "incomplete", "false", "aborted", "timeout"):
        return "fail"
    return "unknown"                                   # not_sure, stopped, running, anything unreadable


def _terms(text):
    try:
        import arch_learn
        return arch_learn._terms(text)
    except Exception:
        return [w for w in re.findall(r"[a-z0-9_]{3,}", str(text or "").lower())][:12]


def _flows_mod():
    try:
        import arch_flows
        return arch_flows
    except Exception:
        return None


def known_flows():
    f = _flows_mod()
    if f is None:
        return set()
    return set(getattr(f, "FLOWS", {}) or {}) | set(getattr(f, "KEYWORDS", {}) or {})


def guess_flow(goal):
    """-> (flow, keyword): the keyword rule the router uses as its hint (no model, no project folder). ("", "") when nothing matches."""
    f = _flows_mod()
    kw = getattr(f, "KEYWORDS", None) if f else None
    pr = getattr(f, "PRIORITY", None) if f else None
    if not kw or not pr:
        return "", ""
    g = str(goal or "").lower()
    score = {k: len(re.findall(p, g)) for k, p in kw.items()}
    if "Traceback" in str(goal or "") or re.search(r'File ".*", line \d+', str(goal or "")):
        score["bugfix"] = score.get("bugfix", 0) + 3
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], pr.index(kv[0]) if kv[0] in pr else len(pr)))
    if not ranked or ranked[0][1] <= 0:
        return "", ""
    top = ranked[0][0]
    return top, keyword_of(top, goal)


def keyword_of(flow, goal):
    """The words in the request that point at this flow ("crash", "slow"), normalised; "" when none."""
    f = _flows_mod()
    pat = (getattr(f, "KEYWORDS", {}) or {}).get(flow) if f else None
    if not pat:
        return ""
    found = [" ".join(m.group(0).split()) for m in re.finditer(pat, str(goal or "").lower())]
    if not found:
        return ""
    w = max(found, key=len)                            # the most specific word: "crash" over "fix" in "fix the crash"
    return re.sub(r"(?<=[a-z]{3})(es|s)$", "", w) if " " not in w else w


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def wilson_low(passed, n, z=Z):
    if n <= 0:
        return 0.0
    p = passed / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d


def confidence(judged):
    return "ok" if judged >= MIN_OK else ("low" if judged >= MIN_LOW else "none")


# ---------------------------------------------------------------- reading samples
def _key(run_dir):
    return "trace:" + os.path.normcase(os.path.abspath(run_dir))


def _load(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_run(run_dir):
    """One run folder -> a sample, or None (no calls, or still running)."""
    state = _load(os.path.join(run_dir, "run.json"))
    state = state if isinstance(state, dict) else {}
    calls, verdict, ts = [], None, []
    try:
        with open(os.path.join(run_dir, "trace.jsonl"), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"call"' not in line and '"verdict"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(j, dict):
                    continue
                t = _num(j.get("t"))
                if t:
                    ts.append(t)
                if j.get("type") == "call":
                    calls.append(j)
                elif j.get("type") == "verdict":
                    verdict = j.get("verdict")
    except OSError:
        return None
    if not calls:
        return None
    if verdict is None:
        if state.get("status") in (None, "running"):
            return None
        verdict = state.get("status")
    weight = {}
    for c in calls:
        m = str(c.get("model") or "?")
        w = weight.setdefault(m, [0.0, 0, ""])
        w[0] += _num(c.get("gen_tokens")) or 0.0
        w[1] += 1
        w[2] = w[2] or str(c.get("tier") or "")
    model = max(weight, key=lambda m: (weight[m][0], weight[m][1]))
    cl = [c for c in calls if c.get("transport") == "claude" or str(c.get("model") or "").startswith("claude:")]
    if state.get("claude_mode") in CLAUDE_MODES:
        mode = state["claude_mode"]
    elif not cl:
        mode = "off"
    elif len(cl) == len(calls):
        mode = "all"
    elif all(str(c.get("role") or "") in PLAN_ROLES for c in cl):
        mode = "plan"
    else:
        mode = "escalation"
    secs = None
    st, up = _num(state.get("started")), _num(state.get("updated"))
    if st and up and up >= st:
        secs = up - st
    elif len(ts) >= 2:
        secs = max(ts) - min(ts)
    else:
        secs = sum(_num(c.get("secs")) or 0.0 for c in calls) or None
    flow = str(state.get("flow") or calls[0].get("flow") or "")
    goal = str(state.get("goal") or calls[0].get("goal") or "")
    return {"id": _key(run_dir), "source": "trace", "goal": goal, "flow": flow, "model": model, "tier": weight[model][2],
            "claude": mode, "verdict": _verdict(verdict), "secs": round(secs, 1) if secs is not None else None,
            "usd": round(sum(_num(c.get("usd")) or 0.0 for c in calls), 6), "when": st or (min(ts) if ts else None)}


def _roots(roots):
    try:
        import arch_costs
        return arch_costs._roots(roots)
    except Exception:
        return [os.path.abspath(r) for r in (roots or [os.getcwd()]) if os.path.isdir(r)]


def trace_samples(roots=None):
    out = []
    for root in _roots(roots):
        rd = os.path.join(root, ".arch", "runs")
        try:
            names = sorted(os.listdir(rd))
        except OSError:
            continue
        for n in names:
            s = read_run(os.path.join(rd, n))
            if s:
                out.append(s)
    return out


def _logbook_db():
    try:
        import arch_learn
        return arch_learn.db_path()
    except Exception:
        return os.path.join(os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_data"),
                            "logbook.db")


def logbook_samples():
    """Logbook rows as samples. Opened read-only: a missing Logbook is not created."""
    path = _logbook_db()
    if not os.path.isfile(path):
        return []
    flows = known_flows()
    out = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, source, title, kind, started, cost, status, model, by_claude FROM tasks").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    for r in rows:
        tid = str(r["id"] or "")
        if tid.startswith("trace:"):
            tid = _key(tid[len("trace:"):])
        title = str(r["title"] or "")
        kind = str(r["kind"] or "")
        flow = kind if kind in flows else guess_flow(title)[0]
        src = str(r["source"] or "")
        if src in ("cc_run", "claude_code"):
            mode = "code"
        elif src == "fleet":
            mode = "fleet"
        elif r["by_claude"]:
            mode = "escalation"
        else:
            mode = "off"
        out.append({"id": tid, "source": "logbook:" + src, "goal": title, "flow": flow, "model": str(r["model"] or "?"),
                    "tier": "", "claude": mode, "verdict": _verdict(r["status"]), "secs": None,
                    "usd": round(_num(r["cost"]) or 0.0, 6), "when": _num(r["started"])})
    return out


def samples(roots=None, logbook=True, days=None, now=None):
    """Every finished run, once. Trace samples win over the Logbook's copy of the same run."""
    got = trace_samples(roots)
    seen = {s["id"] for s in got}
    if logbook:
        got += [s for s in logbook_samples() if s["id"] not in seen]
    if days:
        cut = (now or time.time()) - float(days) * 86400
        got = [s for s in got if not s.get("when") or s["when"] >= cut]
    for s in got:
        s["kw"] = keyword_of(s["flow"], s["goal"]) if s["flow"] else ""
        s["terms"] = _terms(s["goal"])
    return got


# ---------------------------------------------------------------- learning
def options_of(group):
    """[(sample)] -> the options seen, best first: [{flow, model, tier, claude, runs, passed, failed, unknown, rate, low,
    median_secs, median_usd, confidence}]."""
    by = {}
    for s in group:
        by.setdefault((s["flow"], s["model"], s["claude"]), []).append(s)
    out = []
    for (flow, model, mode), ss in by.items():
        p = sum(1 for s in ss if s["verdict"] == "pass")
        f = sum(1 for s in ss if s["verdict"] == "fail")
        tiers = [s["tier"] for s in ss if s.get("tier")]
        out.append({"flow": flow, "model": model, "tier": max(set(tiers), key=tiers.count) if tiers else "", "claude": mode,
                    "runs": len(ss), "passed": p, "failed": f, "unknown": len(ss) - p - f,
                    "rate": round(p / (p + f), 4) if (p + f) else None, "low": round(wilson_low(p, p + f), 4),
                    "median_secs": _median([s["secs"] for s in ss]), "median_usd": _median([s["usd"] for s in ss]),
                    "confidence": confidence(p + f)})
    out.sort(key=lambda o: (o["confidence"] == "none", -o["low"],
                            o["median_usd"] if o["median_usd"] is not None else 1e9,
                            o["median_secs"] if o["median_secs"] is not None else 1e12))
    return out


def learn(sample_list=None, roots=None, logbook=True, days=None):
    """-> {"samples": n, "built": t, "kinds": {kind: {"kind", "flow", "keyword", "runs", "options": [...]}}}.
    A kind is a flow ("bugfix") and, under it, the flow plus the keyword that pointed at it ("bugfix:crash")."""
    ss = samples(roots, logbook, days) if sample_list is None else sample_list
    groups = {}
    for s in ss:
        if not s.get("flow"):
            continue
        groups.setdefault(s["flow"], []).append(s)
        if s.get("kw"):
            groups.setdefault("%s:%s" % (s["flow"], s["kw"]), []).append(s)
    kinds = {}
    for k, g in groups.items():
        flow, _, kw = k.partition(":")
        kinds[k] = {"kind": k, "flow": flow, "keyword": kw, "runs": len(g), "options": options_of(g)}
    return {"samples": len(ss), "built": time.time(), "kinds": kinds}


# ---------------------------------------------------------------- advice
def _claude_ok(cfg):
    """None = do not filter (no settings given); else whether a Claude call may go out now (arch_cloud.mode)."""
    if cfg is None:
        return None
    try:
        import arch_cloud
        return arch_cloud.mode(cfg) != "off"
    except Exception:
        return False


def _fmt_secs(s):
    if s is None:
        return "time not recorded"
    return "%.0f s" % s if s < 90 else "%.1f min" % (s / 60.0)


def _say(o):
    rate = "%d of %d passed (%d%%)" % (o["passed"], o["passed"] + o["failed"], round(100 * (o["rate"] or 0)))
    extra = ", %d not sure" % o["unknown"] if o["unknown"] else ""
    model = o["model"] + (" (%s)" % o["tier"] if o["tier"] else "")
    usd = "$%.2f" % o["median_usd"] if o["median_usd"] else "$0"
    return "flow %s on %s, Claude %s: %s%s; median %s, %s" % (o["flow"], model, o["claude"], rate, extra, _fmt_secs(o["median_secs"]), usd)


def advise(goal, sample_list=None, roots=None, logbook=True, cfg=None):
    """-> {"goal", "pick": option or None, "level", "group", "options", "why", "guess": (flow, keyword)}.
    pick is None when no group has an option with confidence (too few judged runs = no opinion)."""
    ss = samples(roots, logbook) if sample_list is None else sample_list
    flow, kw = guess_flow(goal)
    terms = set(_terms(goal))
    allow = _claude_ok(cfg)
    levels = []
    if terms:
        like = [s for s in ss if len(terms & set(s.get("terms") or ())) >= SIMILAR_MIN]
        levels.append(("similar", "%d earlier requests sharing %d+ words with this one" % (len(like), SIMILAR_MIN), like))
    if flow and kw:
        g = [s for s in ss if s.get("flow") == flow and s.get("kw") == kw]
        levels.append(("kind", "%d earlier %s requests about '%s'" % (len(g), flow, kw), g))
    if flow:
        g = [s for s in ss if s.get("flow") == flow]
        levels.append(("flow", "%d earlier %s runs" % (len(g), flow), g))
    out = {"goal": goal, "guess": (flow, kw), "pick": None, "level": "", "group": "", "options": [], "why": ""}
    skipped = []
    for level, label, g in levels:
        opts = options_of(g)
        usable = [o for o in opts if o["confidence"] != "none"]
        if allow is False:
            skipped += [o for o in usable if o["claude"] != "off"]
            usable = [o for o in usable if o["claude"] == "off"]
        if not usable:
            continue
        best = usable[0]
        out.update(pick=best, level=level, group=label, options=opts[:5])
        why = ["Measured on this machine, from %s: best is %s." % (label, _say(best))]
        if best["confidence"] == "low":
            why.append("Few runs (%d judged): a weak opinion." % (best["passed"] + best["failed"]))
        nxt = [o for o in usable[1:3]]
        if nxt:
            why.append("Next: " + "; ".join(_say(o) for o in nxt) + ".")
        if skipped:
            why.append("Left out because Claude is off now: " + _say(skipped[0]) + ".")
        out["why"] = " ".join(why)
        return out
    out["why"] = ("No opinion: fewer than %d judged runs in any group like this request (%s)." %
                  (MIN_LOW, "; ".join(l for _, l, _ in levels) or "no matching runs"))
    return out


def suggest_for(goal, cfg=None, roots=None):
    """The router's hook. None = no opinion. Never raises; reads the runs at most once per CACHE_S seconds."""
    try:
        if not str(goal or "").strip():
            return None
        key = (tuple(roots) if roots else None)
        now = time.time()
        if _CACHE["samples"] is None or _CACHE["key"] != key or now - _CACHE["t"] > CACHE_S:
            _CACHE.update(samples=samples(roots), key=key, t=now)
        a = advise(goal, sample_list=_CACHE["samples"], cfg=cfg)
        o = a["pick"]
        if not o:
            return None
        hint = ("\nPast runs of requests like this (measured on this machine, %s): flow %s passed %d of %d. "
                "Evidence, not a rule; decide from what the person wants." %
                (a["group"], o["flow"], o["passed"], o["passed"] + o["failed"]))
        return {"flow": o["flow"], "model": o["model"], "tier": o["tier"], "claude": o["claude"],
                "confidence": o["confidence"], "why": a["why"], "hint": hint}
    except Exception:
        return None


def forget():
    _CACHE.update(samples=None, key=None, t=0.0)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "learn"
    if cmd == "learn":
        r = learn()
        print("%d runs" % r["samples"])
        for k in sorted(r["kinds"], key=lambda k: -r["kinds"][k]["runs"]):
            print("\n[%s] %d runs" % (k, r["kinds"][k]["runs"]))
            for o in r["kinds"][k]["options"][:4]:
                print("  %-5s %s" % (o["confidence"], _say(o)))
    elif cmd == "advise":
        print(advise(" ".join(a[1:]))["why"])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
