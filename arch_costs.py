"""What your runs cost, counted from their traces: dollars (Claude calls only), tokens per model, passes per day.

Read-only. Looks at `.arch/runs/*/trace.jsonl` in every portfolio project plus the current folder, newest runs first, and stops
as soon as a run is older than the window. Local-model calls cost $0, so `local_share` says how much of the work stayed on this machine.
"""
import json
import os
import time


def _roots(roots):
    out, seen = [], set()

    def add(p):
        p = os.path.abspath(str(p))
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    if roots is not None:
        for r in roots:
            add(r)
        return out
    try:
        import arch_core
        with open(os.path.join(arch_core.HOME, "portfolio.json"), encoding="utf-8") as f:
            for a in (json.load(f).get("apps") or []):
                if isinstance(a, dict) and a.get("path"):
                    add(a["path"])
    except (OSError, ValueError, ImportError, AttributeError):
        pass
    add(os.getcwd())
    return out


def _read_run(path):
    """-> (calls, verdict, when). A call is the fields we need; a broken line is skipped."""
    calls, verdict, when = [], None, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type": "call"' not in line and '"type": "verdict"' not in line and '"type":"call"' not in line \
                        and '"type":"verdict"' not in line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(j, dict):
                    continue
                t = j.get("type")
                if t == "call":
                    if j.get("done_reason") == "cached":
                        continue
                    calls.append({k: j.get(k) for k in ("model", "prompt_tokens", "gen_tokens", "usd", "transport")})
                    when = when or j.get("t")
                elif t == "verdict":
                    verdict, when = j.get("verdict"), j.get("t") or when
    except OSError:
        pass
    return calls, verdict, when


def summary(days=7, roots=None):
    days = max(1, int(days or 7))
    cutoff = time.time() - days * 86400
    runs = []
    for root in _roots(roots):
        rd = os.path.join(root, ".arch", "runs")
        try:
            names = os.listdir(rd)
        except OSError:
            continue
        for n in names:
            tp = os.path.join(rd, n, "trace.jsonl")
            try:
                m = os.path.getmtime(tp)
            except OSError:
                continue
            runs.append((m, tp))
    runs.sort(reverse=True)
    by_model, by_day = {}, {}
    n_runs = passed = n_calls = local = 0
    total = 0.0
    for m, tp in runs:
        if m < cutoff:
            break                                  # newest first: everything after this is older too
        calls, verdict, when = _read_run(tp)
        day = time.strftime("%Y-%m-%d", time.localtime(float(when) if isinstance(when, (int, float)) else m))
        usd = 0.0
        for c in calls:
            u = float(c.get("usd") or 0)
            usd += u
            row = by_model.setdefault(c.get("model") or "?", {"model": c.get("model") or "?", "calls": 0, "tokens_in": 0,
                                                            "tokens_out": 0, "usd": 0.0})
            row["calls"] += 1
            row["tokens_in"] += int(c.get("prompt_tokens") or 0)
            row["tokens_out"] += int(c.get("gen_tokens") or 0)
            row["usd"] += u
            n_calls += 1
            if c.get("transport") != "claude":
                local += 1
        ok = verdict == "pass"
        n_runs += 1
        passed += 1 if ok else 0
        total += usd
        d = by_day.setdefault(day, {"date": day, "runs": 0, "passed": 0, "usd": 0.0})
        d["runs"] += 1
        d["passed"] += 1 if ok else 0
        d["usd"] += usd
    for r in by_model.values():
        r["usd"] = round(r["usd"], 5)
    for d in by_day.values():
        d["usd"] = round(d["usd"], 5)
    return {"days": days, "runs": n_runs, "passed": passed, "cost_usd": round(total, 5),
            "per_task_usd": round(total / passed, 5) if passed else 0.0,
            "local_share": round(local / n_calls, 3) if n_calls else 0.0,
            "by_model": sorted(by_model.values(), key=lambda r: -r["usd"]),
            "by_day": [by_day[k] for k in sorted(by_day)]}


def main(argv=None):
    import sys
    a = list(sys.argv[1:] if argv is None else argv)
    s = summary(int(a[0]) if a and a[0].isdigit() else 7)
    print(json.dumps(s, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
