"""Static check: every YAML-flow prompt (prompts/<flow>/*.md) keeps what changes between tries AFTER what stays the same.

llama-server reuses the KV cache only for a byte-identical prefix, so a per-try placeholder ({feedback}, {previous}, {attempt},
{tries}, {last_failure}) placed before a stable one ({goal}, {context}, ...) makes every retry re-read the whole prompt (Eq. 15 of
the report: t = (1 - rho) n_in / r_p + n_out t_tok). arch_selftest_chain covers the Python flow templates; this covers the YAML
prompt files, which it never scanned (found by the 2026-11-05 review). Reads files only.

    python arch_selftest_promptprefix.py
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PER_TRY = {"feedback", "previous", "attempt", "tries", "last_failure"}
_PH = re.compile(r"(?<!\{)\{([A-Za-z_][\w.]*)(?:[!:][^{}]*)?\}(?!\})")


def scan(path):
    """-> (ok, first offending per-try placeholder or '', the stable placeholder that comes after it)."""
    text = open(path, encoding="utf-8").read()
    seen_per_try = None
    for m in _PH.finditer(text):
        name = m.group(1).split(".")[0]
        if name in PER_TRY:
            seen_per_try = seen_per_try or name
        elif seen_per_try:
            return False, seen_per_try, name
    return True, "", ""


def run():
    files = sorted(glob.glob(os.path.join(HERE, "prompts", "*", "*.md")))
    bad = []
    for f in files:
        ok, pt, st = scan(f)
        if not ok:
            bad.append(f"{os.path.relpath(f, HERE)}: {{{pt}}} comes before {{{st}}}")
    results = [bool(files), not bad]
    print(f"  {'PASS' if files else 'FAIL'}  found {len(files)} prompt files")
    print(f"  {'PASS' if not bad else 'FAIL'}  every per-try placeholder comes after every stable one" + ("" if not bad else "\n      " + "\n      ".join(bad)))
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' PROMPT-PREFIX CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' PROMPT-PREFIX CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
