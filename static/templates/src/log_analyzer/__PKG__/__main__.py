import argparse
import json
import sys

from .parse import open_log, read
from .stats import summarize


def as_text(r):
    out = [f"{r['requests']} requests, {r['malformed_lines']} malformed lines, {r['bytes']} bytes",
           "status: " + ", ".join(f"{k} {v}" for k, v in r["status"].items()),
           f"5xx error rate: {r['error_rate']:.2%}",
           "latency: " + ", ".join(f"{k} {v if v is not None else '-'}s" for k, v in r["latency_s"].items()),
           "top paths:"] + [f"  {n:6} {p}" for p, n in r["top_paths"]] + ["top clients:"] + [f"  {n:6} {ip}" for ip, n in r["top_clients"]]
    if r["spike_minutes"]:
        out.append("spikes: " + ", ".join(r["spike_minutes"]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: access logs to a report")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)
    hits, bad = [], 0
    for path in args.files:
        try:
            with open_log(path) as f:
                h, b = read(f)
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        hits += h
        bad += b
    report = summarize(hits, bad, args.top)
    print(json.dumps(report, indent=2) if args.json else as_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
