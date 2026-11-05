"""What happened overnight, on one page: the training-trace runs, kept traces, the dataset they produced, the whole-project
re-measure, H1 (base vs abliterated), which Cost-map points moved, and what the cache ledger says. Read-only.

    python tools/morning_report.py            # prints it and writes <data>/morning_<date>.md
    python tools/morning_report.py --since 12  # look back 12 hours (default 14)
"""
import glob
import json
import os
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def _reports(since):
    import arch_eval as E
    out = []
    for p in sorted(glob.glob(os.path.join(E.evals_dir(), "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not d.get("fake") and (d.get("ts") or 0) >= since:
            out.append(d)
    return out


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def build(hours=14.0):
    import arch_core as core
    import arch_eval as E
    since = time.time() - hours * 3600
    data = core.data_dir()
    out = [f"# Overnight report, {time.strftime('%Y-%m-%d %H:%M')}", "",
           f"Everything below is read from this machine's own records for the last {hours:g} hours. Measured, not estimated, unless it says so.", ""]

    reps = _reports(since)
    out += ["## Live evaluation runs", ""]
    if reps:
        out += ["| Run | Tasks | Passed | Not sure | Refusals | Tokens | Hours |", "|---|---|---|---|---|---|---|"]
        for r in reps:
            s = r.get("summary") or {}
            rf = s.get("refusal") or {}
            out.append(f"| {r.get('label')} | {s.get('n')} | {s.get('passed')} ({s.get('pass_rate')}%) | {s.get('not_sure')} | "
                       f"{(str(rf.get('refused')) + ' of ' + str(rf.get('measured'))) if rf else '-'} | {s.get('tokens')} | {round((s.get('secs') or 0) / 3600, 2)} |")
    else:
        out.append("No live evaluation finished in this window.")
    out.append("")

    out += ["## Kept traces and training data", ""]
    for b in ("train", "bench"):
        runs = glob.glob(os.path.join(E.kept_runs_dir(b), ".arch", "runs", "*", "trace.jsonl"))
        new = [r for r in runs if os.path.getmtime(r) >= since]
        out.append(f"- {b}: {len(runs)} kept run(s), {len(new)} new tonight" + (" (only this bucket becomes training data)" if b == "train" else " (benchmark: never trained on)"))
    ds = os.path.join(data, "datasets")
    sets = [(os.path.basename(p), _lines(p)) for p in sorted(glob.glob(os.path.join(ds, "*.jsonl")))]
    out.append("- Dataset files: " + (", ".join(f"{n} ({k} samples)" for n, k in sets) if sets else "none yet (python architect.py dataset builds them)"))
    out.append("")

    out += ["## Cost map: points that moved", ""]
    try:
        import arch_costmodel as cm
        rows = cm.from_evals(sorted(glob.glob(os.path.join(E.evals_dir(), "*.json"))))
        moved = [r for r in rows if len(r.get("history") or []) > 1 and (r["history"][-1].get("ts") or 0) >= since]
        if moved:
            for r in moved:
                a, b = r["history"][-2], r["history"][-1]
                fmt = lambda h: "never passed" if h.get("C") is None else f"{h['C']:.0f} s"
                out.append(f"- {r['model'].split('/')[-1]} on {r['kind']}: {fmt(a)} -> {fmt(b)} per accepted change "
                           f"({a['passed']}/{a['tasks']} -> {b['passed']}/{b['tasks']} passed)")
        else:
            out.append("No point has a new measurement in this window.")
    except Exception as ex:
        out.append(f"(could not read the cost model: {ex})")
    out.append("")

    h1 = os.path.join(E.evals_dir(), "h1_summary.md")
    out += ["## H1: base vs abliterated worker", ""]
    if os.path.isfile(h1) and os.path.getmtime(h1) >= since:
        out += [ln for ln in open(h1, encoding="utf-8").read().splitlines() if not ln.startswith("# ")]
    else:
        out.append("Not finished yet (it runs after the training run and the whole-project re-measure).")
    out.append("")

    out += ["## Caches", ""]
    try:
        import arch_cachestats as cs
        led = cs.ledger()
        for f in led["findings"]:
            out.append(f"- [{f['severity']}] {f['text']}")
    except Exception as ex:
        out.append(f"(could not read the cache ledger: {ex})")
    lk = os.path.join(data, "server", "lookup", "worker.bin")
    out.append(f"- N-gram lookup cache file: {'present, ' + str(round(os.path.getsize(lk) / 1e6, 1)) + ' MB' if os.path.isfile(lk) else 'still absent (it is lost at every server restart)'}")
    out.append("")
    text = "\n".join(out)
    path = os.path.join(data, f"morning_{time.strftime('%Y%m%d')}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        path = None
    return text, path


if __name__ == "__main__":
    h = float(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else 14.0
    text, path = build(h)
    print(text)
    if path:
        print(f"\n(written to {path})")
