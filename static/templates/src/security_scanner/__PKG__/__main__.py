import argparse
import json
import sys

from .scan import load_baseline, new_only, scan_tree, write_baseline


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: scan your own code for secrets and risky calls")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline")
    args = ap.parse_args(argv)
    findings = scan_tree(args.root)
    if args.write_baseline:
        write_baseline(args.write_baseline, findings)
        print(f"baseline written: {len(findings)} findings accepted")
        return 0
    if args.baseline:
        findings = new_only(findings, load_baseline(args.baseline))
    if args.json:
        print(json.dumps({"version": 1, "findings": [f.as_dict() for f in findings]}, indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: {f.severity.upper():6} {f.rule} {f.message}")
        print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
