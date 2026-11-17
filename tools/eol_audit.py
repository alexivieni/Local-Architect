"""Line endings: a file's style (CRLF or LF) must not change by accident.

    py -3.12 tools/eol_audit.py                 # files changed against HEAD (and new, untracked ones)
    py -3.12 tools/eol_audit.py --base HEAD~1   # what the last commit changed (what CI runs)

Why: `.gitattributes` says `* -text`, so git never converts, and the tree is mixed on purpose-less history (about 90 CRLF files, the rest LF).
A tool that rewrites a file in text mode on Windows (a Python script, an editor) turns LF into CRLF or the other way, and the whole file
shows as changed: a 45-line edit became a 1,400-line diff on 2026-11-17 and hid what really changed. Rules, all checked from the bytes:
  - a changed file keeps the line-ending style it had (CRLF stays CRLF, LF stays LF); a file that was mixed may become pure;
  - a new text file is LF (a .bat or .cmd file may be CRLF);
  - a file that was pure may not become mixed (a new line written with the other style).
Exit 0 = clean, 1 = a problem, 2 = could not check (no git, or the base is not in this checkout). Never rewrites anything.
"""
import os
import subprocess
import sys

TEXT = (".py", ".md", ".yaml", ".yml", ".json", ".html", ".txt", ".css", ".js", ".toml", ".cfg", ".ini", ".sh", ".bat", ".cmd")
CRLF_OK_NEW = (".bat", ".cmd")
IGNORE = ("docs/research/",)         # not part of the program; somebody else's documents


def style(b):
    crlf = b.count(b"\r\n")
    lf = b.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    return "crlf" if crlf else "lf" if lf else "none"


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True)


def audit(root, base="HEAD", ignore=IGNORE):
    """-> (problems, status): problems are (path, text); status is "ok" or a reason the audit could not run (then problems is [])."""
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
    except OSError as ex:
        return [], f"git is not available: {ex}"
    if top.returncode:
        return [], "this is not a git checkout"
    root = top.stdout.decode().strip()
    if _git(root, "rev-parse", "--verify", "--quiet", base + "^{commit}").returncode:
        return [], f"the base {base!r} is not in this checkout (a shallow clone needs fetch-depth 2)"
    probs = []
    diff = _git(root, "diff", "--name-status", "--no-renames", base)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if diff.returncode or untracked.returncode:
        return [], "git could not list the changes"
    items = []
    for line in diff.stdout.decode().splitlines():
        st, _, path = line.partition("\t")
        items.append((st[:1], path))
    items += [("?", p) for p in untracked.stdout.decode().splitlines()]
    for st, path in items:
        if not path.lower().endswith(TEXT) or any(path.startswith(i) for i in ignore) or st == "D":
            continue
        try:
            with open(os.path.join(root, path), "rb") as f:
                now = style(f.read())
        except OSError:
            continue
        if st in ("A", "?"):
            if now in ("crlf", "mixed") and not path.lower().endswith(CRLF_OK_NEW):
                probs.append((path, f"a new file must use LF line endings, this one is {now}"))
            continue
        old_blob = _git(root, "show", f"{base}:{path}")
        if old_blob.returncode:
            continue
        old = style(old_blob.stdout)
        if old == "none" or old == now or (old == "mixed" and now != "mixed"):
            continue
        if now == "none":
            continue
        probs.append((path, f"line endings changed from {old} to {now} (the whole file shows as changed; keep its style)"))
    return probs, "ok"


def main(argv):
    base = "HEAD"
    if "--base" in argv:
        base = argv[argv.index("--base") + 1]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probs, status = audit(here, base)
    if status != "ok":
        print(f"[eol] could not check: {status}")
        return 2
    for p, why in probs:
        print(f"  {p}: {why}")
    print(f"{len(probs)} line-ending problem(s) against {base}")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
