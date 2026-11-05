"""The loop cost model of the technical report (docs/research, Section 5), computed from this machine's own run traces.

    Eq. (15)  t = (1 - rho) * n_in / r_p + n_out * t_tok        call time; rho = share of the prompt served from a cached prefix
    Eq. (16)  C = t / p                                          time per accepted change, constant pass rate (independent of the cap k)
    Eq. (17)  C = t / p_bar,  p_bar = sum Q_{i-1} p_i / sum Q_{i-1},  Q_i = prod_{j<=i} (1 - p_j)
                                                                 attempt-weighted pass rate when feedback changes p from try to try
    Eq. (18)  J = sum_r c_r t_r / p_r + lambda * S               expected run time of a role assignment with S swaps
    Eq. (19)  moving the planner to a larger model pays when  c_b t_b (1/p_b - 1/p_b') > c_r (t_l/p_l - t_s/p_s) + lambda * dS

measure() reads `.arch/runs/*/trace.jsonl` (call records + the code checks' marks): per (role, model) it estimates p_i for the i-th try
of a step, the mean call time t, the prefix share rho, C = t / p_bar, the first-try bound t / p_1 (the gap to C is the value of
feedback, as the report says), and E = P * C when the average power draw is known. "Model proposes; code decides": p comes from the
marks the checks wrote, never from a model's opinion. A mark of None (could not run) is left out, as everywhere else in the harness.

    python arch_costmodel.py [project folder ...]
"""
import collections
import glob
import json
import os
import re
import sys


def call_time(n_in, n_out, rho, r_p, t_tok):
    """Eq. (15): seconds for one call with n_in prompt tokens (rho of them cached), n_out generated, prompt speed r_p tok/s,
    t_tok seconds per generated token."""
    return (1.0 - rho) * n_in / max(r_p, 1e-9) + n_out * t_tok


def cost_constant(t, p):
    """Eq. (16): C = t / p (None when p is 0: the step never passes)."""
    return t / p if p > 0 else None


def p_bar(ps):
    """Eq. (17): the attempt-weighted mean pass rate for per-attempt pass probabilities p_1..p_k."""
    q, num, den = 1.0, 0.0, 0.0
    for p in ps:
        num += q * p
        den += q
        q *= (1.0 - p)
    return num / den if den else 0.0


def cost(t, ps):
    """Eq. (17): C = t / p_bar. -> (C or None, p_bar)."""
    pb = p_bar(ps)
    return (t / pb if pb > 0 else None), pb


def run_time(roles, lam=0.0, swaps=0):
    """Eq. (18): J = sum_r c_r t_r / p_r + lambda * S. roles = [(c_r, t_r, p_r), ...]."""
    return sum(c * t / p for c, t, p in roles if p > 0) + lam * swaps


def planner_pays(c_b, t_b, p_b, p_b_new, c_r, t_l, p_l, t_s, p_s, lam_dS=0.0):
    """Eq. (19): does moving the planner role from the small model s to the large model l pay?
    -> (pays, left_side_seconds, right_side_seconds)."""
    left = c_b * t_b * (1.0 / p_b - 1.0 / p_b_new)
    right = c_r * (t_l / p_l - t_s / p_s) + lam_dS
    return left > right, left, right


# ---------------------------------------------------------------------------------------------------- measured from traces
_TRY = re.compile(r"[-_\s]*(?:try|attempt|round)\s*\d+(?:[-_]?(?:sample|cand)\d+)?\s*$", re.I)       # "fix-try2", "sprint1-try1", "build try 3"


def default_roots():
    """Every place run traces live: the portfolio projects (or this folder) and the kept live-eval runs, both buckets."""
    out = []
    try:
        import arch_costs
        out = list(arch_costs._roots(None))
    except Exception:
        pass
    if not out:
        out = [os.getcwd()]
    try:
        import arch_eval
        out += [arch_eval.kept_runs_dir(b) for b in ("train", "bench") if os.path.isdir(arch_eval.kept_runs_dir(b))]
    except Exception:
        pass
    return out


def _records(roots):
    for root in roots:
        for t in sorted(glob.glob(os.path.join(root, ".arch", "runs", "*", "trace.jsonl"))):
            calls, marks = {}, {}
            try:
                with open(t, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        if j.get("type") == "call" and j.get("id") is not None:
                            calls[j["id"]] = j
                        elif j.get("type") == "mark" and j.get("id") is not None:
                            marks[j["id"]] = j
            except OSError:
                continue
            yield os.path.basename(os.path.dirname(t)), calls, marks


def measure(roots, power_w=None, min_calls=3):
    """-> [{role, model, calls, t, rho, p_i, p_bar, C, C_first_bound, feedback_gain_s, E_joules}] sorted by C (best first)."""
    stats = collections.defaultdict(lambda: {"t": [], "rho": [], "try": collections.defaultdict(lambda: [0, 0])})
    for run, calls, marks in _records(roots):
        steps = collections.defaultdict(list)
        for cid, c in calls.items():
            m = marks.get(cid)
            ok = m.get("accepted", m.get("ok")) if m else None     # Engine.mark writes "accepted"
            if ok is None or c.get("done_reason") == "cached":     # unmarked / "could not run" / a replayed reply: not evidence
                continue
            key = (run, c.get("role") or "?", _TRY.sub("", str(c.get("label") or c.get("role") or "")))
            steps[key].append((c.get("t") or 0, c, bool(ok)))
        for (run, role, _label), seq in steps.items():
            seq.sort(key=lambda x: x[0])
            for i, (_t, c, ok) in enumerate(seq, 1):
                s = stats[(role, c.get("model") or "?")]
                s["t"].append(float(c.get("secs") or 0))
                pt = float(c.get("prompt_tokens") or 0)                # n_in: the whole prompt, cached part included
                cached = float(c.get("prefix_reused_est") or 0)        # exact cache_n from llama-server, else the engine's estimate
                if pt > 0:
                    s["rho"].append(min(1.0, cached / pt))
                s["try"][i][0] += int(ok)
                s["try"][i][1] += 1
                if ok:
                    break                                          # attempts after an accepted one belong to another step
    out = []
    for (role, model), s in stats.items():
        n = len(s["t"])
        if n < min_calls:
            continue
        t = sum(s["t"]) / n
        ks = sorted(s["try"])
        ps = [s["try"][i][0] / s["try"][i][1] for i in ks if s["try"][i][1]]
        c, pb = cost(t, ps)
        bound = cost_constant(t, ps[0]) if ps else None
        out.append({"role": role, "model": model, "calls": n, "t": round(t, 1),
                    "rho": round(sum(s["rho"]) / len(s["rho"]), 2) if s["rho"] else None,
                    "p_i": [round(p, 2) for p in ps], "p_bar": round(pb, 3), "C": round(c, 1) if c else None,
                    "C_first_bound": round(bound, 1) if bound else None,
                    "feedback_gain_s": round(bound - c, 1) if bound and c else None,
                    "E_joules": round(power_w * c) if power_w and c else None})
    return sorted(out, key=lambda r: (r["C"] is None, r["C"] or 0))


def from_evals(paths, include_fake=False):
    """Task-level C from saved eval reports (hub_data/evals/*.json): per (model doing most of the calls, flow),
    p = passed / decided tasks, t = mean seconds per task, C = total seconds / passed tasks = t / p (Eq. 16 at task level).
    A 'not sure' task is left out of p (it is neither a pass nor a fail) but its seconds still count: they were spent.
    -> [{model, flow, tasks, passed, not_sure, t, p, C, orthogonalized, runs, history}] sorted by C.
    `runs` lists the tasks behind the point (newest first); `history` is the same point per saved report, oldest first, so a
    change that moved it can be seen."""
    def new():
        return {"n": 0, "pass": 0, "fail": 0, "secs": 0.0}

    def add(a, r):
        a["n"] += 1
        a["secs"] += float(r.get("secs") or 0)
        if r.get("verdict") == "pass":
            a["pass"] += 1
        elif r.get("verdict") == "fail":
            a["fail"] += 1

    def point(a):
        decided = a["pass"] + a["fail"]
        p = a["pass"] / decided if decided else 0.0
        return {"tasks": a["n"], "passed": a["pass"], "not_sure": a["n"] - decided, "t": round(a["secs"] / a["n"], 1) if a["n"] else 0.0,
                "p": round(p, 2), "C": round(a["secs"] / a["pass"], 1) if a["pass"] else None}

    agg = collections.defaultdict(new)
    per = collections.defaultdict(dict)            # (model, flow) -> {report path: aggregate}
    runs = collections.defaultdict(list)
    meta, flow_of = {}, {}
    fails = collections.defaultdict(dict)       # (model, kind) -> {failure family: count} (arch_triage)
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("fake") and not include_fake:
            continue
        meta[path] = (d.get("ts") or 0, d.get("label") or os.path.basename(path))
        for r in d.get("results") or []:
            models = r.get("models") or {}
            if not models:
                continue
            main = max(models.items(), key=lambda kv: kv[1])[0]
            # the kind of task is its family (bugfix, e2e, distributed, ...): one flow serves several families of very different
            # difficulty (whole-project e2e builds and the distributed-systems training tasks both run the build flow)
            key = (main, r.get("family") or r.get("flow") or "?")
            if key not in flow_of:
                flow_of[key] = r.get("flow") or "?"
            add(agg[key], r)
            add(per[key].setdefault(path, new()), r)
            try:
                import arch_triage
                fam = arch_triage.classify(r)
            except Exception:
                fam = None
            if fam:
                fails[key][fam] = fails[key].get(fam, 0) + 1
            runs[key].append({"id": r.get("id"), "verdict": r.get("verdict"), "secs": r.get("secs"), "calls": r.get("calls"), "why": fam,
                              "label": meta[path][1], "ts": meta[path][0], "detail": str(r.get("detail") or "")[:160]})
    out = []
    for (model, kind), a in agg.items():
        hist = [dict(point(v), label=meta[pth][1], ts=meta[pth][0]) for pth, v in sorted(per[(model, kind)].items(), key=lambda kv: meta[kv[0]][0])]
        low = model.lower()
        out.append(dict(point(a), model=model, kind=kind, flow=flow_of[(model, kind)], history=hist, failures=dict(fails[(model, kind)]),
                        orthogonalized=any(w in low for w in ("abliterat", "uncensor", "orthogon")),
                        runs=sorted(runs[(model, kind)], key=lambda x: -(x["ts"] or 0))[:40]))
    return sorted(out, key=lambda r: (r["C"] is None, r["C"] or 0))


_STOP_MEMO = {"t": 0.0, "stats": None}
STOP_MIN_SAMPLES = 10


def _try_counts(roots=None, ttl=600.0):
    """{(role, model): {try i: [passed, attempts]}} from the kept traces, memoised for ttl seconds."""
    import time as _t
    if _STOP_MEMO["stats"] is not None and _t.time() - _STOP_MEMO["t"] < ttl:
        return _STOP_MEMO["stats"]
    stats = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    for run, calls, marks in _records(roots or default_roots()):
        steps = collections.defaultdict(list)
        for cid, c in calls.items():
            m = marks.get(cid)
            ok = m.get("accepted", m.get("ok")) if m else None
            if ok is None or c.get("done_reason") == "cached":
                continue
            steps[(run, c.get("role") or "?", _TRY.sub("", str(c.get("label") or c.get("role") or "")))].append((c.get("t") or 0, c, bool(ok)))
        for (_run, role, _label), seq in steps.items():
            seq.sort(key=lambda x: x[0])
            for i, (_t2, c, ok) in enumerate(seq, 1):
                s2 = stats[(role, c.get("model") or "?")][i]
                s2[0] += int(ok)
                s2[1] += 1
                if ok:
                    break
    _STOP_MEMO.update(t=_t.time(), stats=stats)
    return stats


def retry_worth(role, model, try_no, cfg=None, roots=None):
    """Optimal stopping, guarded (Eq. 16-17): is try `try_no` of a step worth its seconds? False only when this role+model has at
    least STOP_MIN_SAMPLES measured attempts at that position, none passed, and no later position ever passed either -- then the
    expected cost t / p is unbounded and the time is better spent moving on. True whenever the data is thin. Off in benchmarks and
    self-tests (they measure a fixed policy) and with behavior.retry_stop = off."""
    import os as _os
    if _os.environ.get("ARCHITECT_EVAL") == "1" or _os.environ.get("ARCHITECT_SELFTEST") == "1":
        return True
    if str(((cfg or {}).get("behavior") or {}).get("retry_stop", "auto")) == "off":
        return True
    try:
        st = _try_counts(roots).get((role, model)) or {}
    except Exception:
        return True
    here = st.get(try_no)
    if not here or here[1] < STOP_MIN_SAMPLES or here[0] > 0:
        return True
    return any(v[0] > 0 for k, v in st.items() if k > try_no)     # worth it only if some LATER try has ever passed


def p_cheapest(rows, draws=2000, seed=7):
    """How sure is "cheapest"? For each kind of task: draw every model's pass rate from Beta(passed + 1, failed + 1) and its
    C = t / p, count how often each model is cheapest. -> {kind: {model: probability}}. With 3 tasks a point, 'cheapest' is often
    luck; this says how much (Thompson sampling, the same draw a bandit router would make)."""
    import random
    rnd = random.Random(seed)
    by = collections.defaultdict(list)
    for r in rows:
        by[r.get("kind") or r.get("flow")].append(r)
    out = {}
    for kind, rs in by.items():
        wins = collections.Counter()
        for _ in range(draws):
            best, bm = None, None
            for r in rs:
                fails = max(0, (r.get("tasks") or 0) - (r.get("passed") or 0) - (r.get("not_sure") or 0))
                p = rnd.betavariate((r.get("passed") or 0) + 1, fails + 1)
                c = (r.get("t") or 0) / max(p, 1e-6)
                if best is None or c < best:
                    best, bm = c, r["model"]
            wins[bm] += 1
        out[kind] = {m: round(n / draws, 3) for m, n in wins.items()}
    return out


def report_evals(rows):
    if not rows:
        return "No live eval reports yet (python architect.py eval --quick)."
    lines = [f"{'model':<40} {'kind of task':<12} {'tasks':>5} {'pass':>5} {'t (s)':>7} {'p':>5} {'C (s)':>8}"]
    for r in rows:
        lines.append(f"{str(r['model'])[:40]:<40} {str(r.get('kind') or r['flow'])[:12]:<12} {r['tasks']:>5} {r['passed']:>5} {r['t']:>7} {r['p']:>5} {str(r['C']):>8}")
    lines.append("C = seconds spent per task that PASSED its hidden tests (t / p, Eq. 16). A fast model that fails costs more than a slow one that passes.")
    return "\n".join(lines)


def report(rows):
    if not rows:
        return "No marked model calls yet: run a few tasks first (the checks mark every reply)."
    lines = [f"{'role':<12} {'model':<34} {'calls':>5} {'t (s)':>7} {'rho':>5} {'p_bar':>6} {'C (s)':>8} {'t/p1':>8}  feedback saves"]
    for r in rows:
        lines.append(f"{r['role'][:12]:<12} {str(r['model'])[:34]:<34} {r['calls']:>5} {r['t']:>7} {str(r['rho']):>5} {r['p_bar']:>6} "
                     f"{str(r['C']):>8} {str(r['C_first_bound']):>8}  {str(r['feedback_gain_s']) + ' s' if r['feedback_gain_s'] is not None else '-'}")
    lines.append("C = expected seconds per ACCEPTED change (Eq. 17). Lower is better. A model twice as fast that passes half as often costs the same.")
    return "\n".join(lines)


if __name__ == "__main__":
    roots = sys.argv[1:] or [os.getcwd()]
    print(report(measure(roots)))
