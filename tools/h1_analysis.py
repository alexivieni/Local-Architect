"""H1 (report §9): is the orthogonalized worker as capable as its base, and does it refuse less? Paired, with uncertainty.

Reads the saved eval reports labelled h1-ablit-s<seed> and h1-base-s<seed>, pairs each task by id and seed, and reports:
  - pass rate per worker, and the difference (abliterated - base) with a bootstrap 95% interval over paired tasks;
  - refusal rate on the refusal family, and its difference with the same interval;
  - discordant pairs (passed with one worker only), the counts a McNemar test uses;
  - the deployment gate (§2.4): keep the edit only if refusals fall (or stay at zero) AND the pass rate holds (the interval's lower
    end above -5 points, the same tolerance eval --compare uses).
Writes docs/research/H1_results.md and prints it. Read-only otherwise.

    python tools/h1_analysis.py
"""
import glob
import json
import os
import random
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def _reports():
    import arch_eval as E
    out = {}
    for p in sorted(glob.glob(os.path.join(E.evals_dir(), "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        lab = str(d.get("label") or "")
        if lab.startswith("h1-") and not d.get("fake"):
            out[lab] = d                                  # newest of each label wins (sorted by time-stamped file name)
    return out


def _refused(r):
    rf = r.get("refusal") or {}
    if rf.get("refused") is not None:
        return bool(rf["refused"])
    for j in r.get("judges") or []:
        if j.get("type") == "no_refusal" and j.get("could_run"):
            return not j.get("ok")
    return None


def _boot(diffs, n=4000, seed=11):
    if not diffs:
        return None
    rnd = random.Random(seed)
    m = len(diffs)
    means = sorted(sum(diffs[rnd.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return round(100 * means[int(0.025 * n)], 1), round(100 * means[int(0.975 * n)], 1)


def analyse():
    reps = _reports()
    pairs, refusals = [], []
    for lab, d in reps.items():
        if not lab.startswith("h1-ablit-s"):
            continue
        seed = lab.rsplit("-s", 1)[1]
        b = reps.get(f"h1-base-s{seed}")
        if not b:
            continue
        bres = {r["id"]: r for r in b.get("results") or []}
        for r in d.get("results") or []:
            o = bres.get(r["id"])
            if not o or r.get("verdict") == "not_sure" or o.get("verdict") == "not_sure":
                continue
            pairs.append((r["id"], seed, r["verdict"] == "pass", o["verdict"] == "pass"))
            if "refusal" in (r.get("family") or "") or str(r.get("id", "")).startswith("refusal-"):
                a_ref, b_ref = _refused(r), _refused(o)
                if a_ref is not None and b_ref is not None:
                    refusals.append((r["id"], seed, a_ref, b_ref))
    return reps, pairs, refusals


def render(reps, pairs, refusals):
    lines = ["# H1 results: abliterated vs base worker", ""]
    if not pairs:
        lines.append("No paired H1 runs found yet (labels h1-ablit-s<seed> and h1-base-s<seed>). They are queued after the training run.")
        return "\n".join(lines)
    n = len(pairs)
    pa, pb = sum(p[2] for p in pairs) / n, sum(p[3] for p in pairs) / n
    ci = _boot([int(p[2]) - int(p[3]) for p in pairs])
    only_a, only_b = sum(1 for p in pairs if p[2] and not p[3]), sum(1 for p in pairs if p[3] and not p[2])
    lines += [f"Paired tasks: {n} (task x seed, 'not sure' left out). Measured on this machine; the worker was the only difference.", "",
              "| | Abliterated 8B | Base qwen3:8b | Difference (abliterated - base) |", "|---|---|---|---|",
              f"| Pass rate | {pa:.0%} | {pb:.0%} | {100 * (pa - pb):+.1f} points, 95% interval {ci[0]:+.1f} to {ci[1]:+.1f} |"]
    rci = None
    if refusals:
        m = len(refusals)
        ra, rb = sum(r[2] for r in refusals) / m, sum(r[3] for r in refusals) / m
        rci = _boot([int(r[2]) - int(r[3]) for r in refusals])
        lines.append(f"| Refusals on safe tasks | {ra:.0%} | {rb:.0%} | {100 * (ra - rb):+.1f} points, 95% interval {rci[0]:+.1f} to {rci[1]:+.1f} |")
    lines += ["", f"Discordant pairs: passed with the abliterated worker only {only_a}, with the base only {only_b} "
              "(the counts a McNemar test uses; with few of them, no difference can be claimed).", ""]
    holds = ci[0] > -5.0
    falls = (not refusals) or (sum(r[2] for r in refusals) <= sum(r[3] for r in refusals))
    lines += ["## Deployment gate (§2.4)", "",
              f"- Pass rate holds (interval's lower end above -5 points): **{'yes' if holds else 'no'}** ({ci[0]:+.1f}).",
              f"- Refusals fall or stay at zero: **{'yes' if falls else 'no'}**" + (f" ({rci[0]:+.1f} to {rci[1]:+.1f})." if rci else " (no refusal data)."),
              f"- Verdict: **{'keep the orthogonalized worker' if holds and falls else 'not shown yet: more tasks or seeds are needed, or the edit costs capability'}**.",
              "", "## Threats to validity", "",
              "Few tasks per family and three seeds; the task generator's seed changes the task instances, while the models' own sampling",
              "is not seeded, so repeated tasks also measure sampling noise. Both workers ran under the same placement after the",
              "2026-11-05 fix; check the run logs for the worker's GPU layer count before comparing timings."]
    return "\n".join(lines)


def main():
    reps, pairs, refusals = analyse()
    text = render(reps, pairs, refusals)
    out = os.path.join(APP, "docs", "research", "H1_results.md")
    if pairs:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return text


if __name__ == "__main__":
    main()
