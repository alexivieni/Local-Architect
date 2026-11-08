"""The numbers the docs state about the code (roles, flows, suites), computed from the code itself.

    py -3.12 tools/doc_counts.py            # list every stated number that no longer matches the code (exit 1 if any)
    py -3.12 tools/doc_counts.py --write    # rewrite those numbers in README.md and CLAUDE.md

Only the CURRENT claims are listed in SPOTS (each a regex over one file). Release notes and dated sections keep the numbers that were
true on their day and are never touched. arch_selftest_docs runs check() so a count can no longer drift unnoticed.
"""
import glob
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def counts():
    """{roles, groups: {group: n}, flows (registered, as a run sees them), yaml (files in flows/), suites (what CI runs)}."""
    import arch_roles
    import arch_flows
    import arch_dataflow
    arch_dataflow.register(arch_flows.FLOWS, "legacy", say=lambda *a: None)
    groups = {}
    for r in arch_roles.ROLES.values():
        g = r.get("group") or "Other"
        groups[g] = groups.get(g, 0) + 1
    suites = len(glob.glob(os.path.join(APP, "arch_selftest*.py"))) + 1      # + arch_hub_check, as tools/ci_parallel.suites() counts
    return {"roles": len(arch_roles.ROLES), "groups": groups, "flows": len(arch_flows.FLOWS),
            "yaml": len(glob.glob(os.path.join(APP, "flows", "*.yaml"))), "suites": suites}


def _groups(c):
    order = ["Core", "Builders", "Quality", "Security", "Infra"]
    names = order + sorted(g for g in c["groups"] if g not in order)
    return ", ".join(f"{g} {c['groups'][g]}" for g in names if g in c["groups"])


# (file, pattern, replacement(counts, match) -> the text the whole match should be)
SPOTS = [
    ("CLAUDE.md", r"\d+ roles and \d+ flows", lambda c, m: f"{c['roles']} roles and {c['flows']} flows"),
    ("README.md", r"## Flows \(\d+, each defined", lambda c, m: f"## Flows ({c['yaml']}, each defined"),
    ("README.md", r"## Roles \(\d+\)", lambda c, m: f"## Roles ({c['roles']})"),
    ("README.md", r"All \d+ roles by group: [A-Za-z0-9, ]*[A-Za-z0-9]", lambda c, m: f"All {c['roles']} roles by group: {_groups(c)}"),
    ("README.md", r"checks in \d+ suites", lambda c, m: f"checks in {c['suites']} suites"),
]


def check(write=False, app=APP):
    """-> [(file, found, should_be)] for every stated number that does not match the code; with write=True the files are fixed.
    A spot whose pattern is missing from its file is reported too (found = None): a renamed heading must not silently stop the check."""
    c = counts()
    out = []
    texts = {}
    for name, pat, fn in SPOTS:
        path = os.path.join(app, name)
        if name not in texts:
            texts[name] = open(path, encoding="utf-8", newline="").read()
        m = re.search(pat, texts[name])
        if not m:
            out.append((name, None, pat))
            continue
        want = fn(c, m)
        if m.group(0) != want:
            out.append((name, m.group(0), want))
            texts[name] = texts[name][:m.start()] + want + texts[name][m.end():]
    if write:
        for name, t in texts.items():
            with open(os.path.join(app, name), "w", encoding="utf-8", newline="") as f:
                f.write(t)
    return out


def main():
    bad = check(write="--write" in sys.argv)
    for name, found, want in bad:
        print(f"  {name}: " + (f"pattern missing: {want}" if found is None else f"'{found}' -> '{want}'"))
    if not bad:
        print("every stated count matches the code")
    elif "--write" in sys.argv:
        print("fixed" if all(f is not None for _, f, _ in bad) else "fixed what was found; a missing pattern needs a hand edit")
    return 0 if not bad or ("--write" in sys.argv and all(f is not None for _, f, _ in bad)) else 1


if __name__ == "__main__":
    sys.exit(main())
