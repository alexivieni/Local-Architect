"""Turn tools/ci_parallel.py's result into GitHub annotations and a job summary (GitHub Actions only; reads, never runs a suite).

    python tools/ci_annotate.py <ci_parallel output file>

GitHub shows a job's log only to signed-in users, but annotations and the job summary are public on a public repository. So every
failing suite becomes an error annotation (its summary line and first failing checks), every NOT_SURE or LOAD suite a warning,
and the whole table goes to $GITHUB_STEP_SUMMARY. With no report (the runner itself crashed), the last lines of its output are
the error. Exit 0 always: the suite step already failed or passed on its own.
"""
import glob
import json
import os
import sys

MAX_ERRORS = 10          # GitHub keeps at most 10 error annotations per step


def esc(s):
    """Annotation text: %, CR and LF must be escaped (GitHub workflow command syntax)."""
    return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def latest_report(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, "ci", "*.json")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def lines_for(rows):
    """-> (annotations, summary markdown) for ci_parallel's rows."""
    ann, md = [], ["| Status | Suite | Seconds | Summary |", "|---|---|---:|---|"]
    order = {"FAIL": 0, "TIMEOUT": 0, "NOT_SURE": 1, "LOAD": 2, "PASS": 3}
    for r in sorted(rows, key=lambda r: (order.get(r.get("status"), 1), r.get("suite", ""))):
        st, name = r.get("status") or "?", r.get("suite") or "?"
        summ = (r.get("summary") or "").replace("|", "/")
        md.append(f"| {st} | {name} | {r.get('secs', '')} | {summ[:160]} |")
        if st in ("FAIL", "TIMEOUT"):
            alone = r.get("alone") or {}
            body = "\n".join([f"{name}: {summ}"] + list(r.get("fails") or [])[:8] + ([f"alone: {alone.get('summary')}"] if alone else []))
            ann.append(("error", f"{st} {name}", body[:1800]))
        elif st in ("NOT_SURE", "LOAD"):
            ann.append(("warning", f"{st} {name}", f"{name}: {summ}"[:600]))
    errors = [a for a in ann if a[0] == "error"]
    if len(errors) > MAX_ERRORS:
        ann = [a for a in ann if a[0] != "error"] + errors[:MAX_ERRORS - 1] + [
            ("error", "more failures", f"{len(errors) - MAX_ERRORS + 1} more failing suites: see the job summary table")]
    good = sum(1 for r in rows if r.get("status") in ("PASS", "LOAD", "NOT_SURE"))
    md.insert(0, f"**{good}/{len(rows)} suites green** (NOT_SURE = some checks could not run on this machine; LOAD = passed alone)\n")
    return ann, "\n".join(md) + "\n"


def main(argv):
    out_file = argv[1] if len(argv) > 1 else ""
    data_dir = os.environ.get("ARCHITECT_DATA_DIR") or ""
    rows = latest_report(data_dir) if data_dir else None
    if rows is None:
        tail = ""
        try:
            with open(out_file, encoding="utf-8", errors="replace") as f:
                tail = "\n".join(f.read().splitlines()[-30:])
        except OSError:
            tail = "(no output file either)"
        print(f"::error title=ci_parallel wrote no report::{esc(tail[-3000:])}")
        summary = "**ci_parallel wrote no report**\n\n```\n" + tail + "\n```\n"
    else:
        ann, summary = lines_for(rows)
        for level, title, body in ann:
            print(f"::{level} title={esc(title)}::{esc(body)}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(summary)
    else:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
