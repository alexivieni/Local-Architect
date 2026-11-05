import argparse
import os
import sys

from .cells import run, split
from .render import to_html, to_ipynb


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: run '# %%' cells into a report")
    ap.add_argument("source")
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--ipynb")
    ap.add_argument("--hide-source", action="store_true")
    args = ap.parse_args(argv)
    with open(args.source, encoding="utf-8") as f:
        cells = split(f.read())
    ok = run(cells)
    title = os.path.splitext(os.path.basename(args.source))[0]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(to_html(cells, title, not args.hide_source))
    if args.ipynb:
        with open(args.ipynb, "w", encoding="utf-8") as f:
            f.write(to_ipynb(cells))
    print(f"wrote {args.out}" + ("" if ok else " (a cell failed; see the report)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
