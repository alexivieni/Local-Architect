import argparse
import sys

from .sync import apply, plan


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: one-way folder sync")
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delete", action="store_true", help="remove files from DEST that are gone from SRC")
    ap.add_argument("--ignore", action="append", default=[], metavar="GLOB")
    args = ap.parse_args(argv)
    try:
        actions = plan(args.src, args.dest, args.ignore, args.delete)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for a in actions:
        print(f"{a.op:6} {a.path}")
    if not actions:
        print("already in sync")
    if args.dry_run:
        return 0
    apply(args.src, args.dest, actions, args.ignore)
    print(f"applied {len(actions)} action(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
