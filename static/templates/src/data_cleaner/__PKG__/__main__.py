import argparse
import csv
import json
import sys

from .clean import clean_rows


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: clean a CSV by rules")
    ap.add_argument("csv")
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out", default="clean.csv")
    ap.add_argument("--audit", default=None, help="write the audit as JSON here (default: print a summary)")
    args = ap.parse_args(argv)
    try:
        with open(args.rules, encoding="utf-8") as f:
            rules = json.load(f)
        with open(args.csv, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            rows = list(reader)
        clean, audit = clean_rows(rows, rules)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(clean)
    if args.audit:
        with open(args.audit, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)
    print(f"{audit['rows_in']} rows in, {audit['rows_out']} out: {audit['changed']} values fixed, "
          f"{len(audit['rejected'])} rejected, {len(audit['duplicates'])} duplicates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
