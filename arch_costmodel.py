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
_TRY = re.compile(r"\s*(?:try|attempt|round)\s*\d+\s*$", re.I)


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
    -> [{model, flow, tasks, passed, not_sure, t, p, C}] sorted by C."""
    agg = collections.defaultdict(lambda: {"n": 0, "pass": 0, "fail": 0, "secs": 0.0})
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("fake") and not include_fake:
            continue
        for r in d.get("results") or []:
            models = r.get("models") or {}
            if not models:
                continue
            main = max(models.items(), key=lambda kv: kv[1])[0]
            a = agg[(main, r.get("flow") or "?")]
            a["n"] += 1
            a["secs"] += float(r.get("secs") or 0)
            if r.get("verdict") == "pass":
                a["pass"] += 1
            elif r.get("verdict") == "fail":
                a["fail"] += 1
    out = []
    for (model, flow), a in agg.items():
        decided = a["pass"] + a["fail"]
        p = a["pass"] / decided if decided else 0.0
        out.append({"model": model, "flow": flow, "tasks": a["n"], "passed": a["pass"], "not_sure": a["n"] - decided,
                    "t": round(a["secs"] / a["n"], 1), "p": round(p, 2),
                    "C": round(a["secs"] / a["pass"], 1) if a["pass"] else None})
    return sorted(out, key=lambda r: (r["C"] is None, r["C"] or 0))


def report_evals(rows):
    if not rows:
        return "No live eval reports yet (python architect.py eval --quick)."
    lines = [f"{'model':<40} {'flow':<10} {'tasks':>5} {'pass':>5} {'t (s)':>7} {'p':>5} {'C (s)':>8}"]
    for r in rows:
        lines.append(f"{str(r['model'])[:40]:<40} {r['flow'][:10]:<10} {r['tasks']:>5} {r['passed']:>5} {r['t']:>7} {r['p']:>5} {str(r['C']):>8}")
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
