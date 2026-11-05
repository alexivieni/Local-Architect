import argparse
import json
import sys

from .page import build


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: JSON metrics to an HTML dashboard")
    ap.add_argument("metrics")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args(argv)
    try:
        with open(args.metrics, encoding="utf-8") as f:
            html = build(json.load(f))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
