import argparse
import sys

from .report import build, load


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: a PDF report from a CSV")
    ap.add_argument("csv")
    ap.add_argument("--out", default="report.pdf")
    ap.add_argument("--title", default="__NAME__ report")
    args = ap.parse_args(argv)
    try:
        doc = build(load(args.csv), args.title)
    except (OSError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    doc.save(args.out)
    print(f"wrote {args.out} ({len(doc.pages)} page(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
