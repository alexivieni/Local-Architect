"""The combined lessons: what we learned building this app, each with its evidence, the code that enforces it and the check that proves
the enforcement still exists. data/lessons.json is the source; docs/PRINCIPLES.md prints the same table.

    py -3.12 arch_lessons.py          # print the table and check every target (exit 1 on a missing one)

enforced_by / checked_by entries are strings with a kind prefix:  py:module.attr[.attr]  (imported and looked up, never called),
setting:section.name (in arch_params.SETTINGS), file:relative/path (exists), rule:text (a working rule with no code to look up).
trains_via is a curriculum family or tag (or null). Nothing here changes behaviour; it only keeps the claims honest."""
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "data", "lessons.json")
FIELDS = ("id", "lesson", "evidence", "enforced_by", "checked_by", "trains_via", "status")
STATUS = ("enforced", "partial", "planned")


def load(path=None):
    with open(path or FILE, encoding="utf-8") as f:
        d = json.load(f)
    return [x for x in d.get("lessons", []) if isinstance(x, dict)]


def _targets(v):
    return [v] if isinstance(v, str) else list(v or [])


def target_ok(t):
    """-> (ok, why) for one enforced_by / checked_by entry."""
    kind, _, rest = str(t).partition(":")
    rest = rest.strip()
    if kind == "rule":
        return bool(rest), "rule text"
    if kind == "file":
        return os.path.exists(os.path.join(HERE, rest)), "file missing: " + rest
    if kind == "setting":
        try:
            import arch_params
            return ('"%s"' % rest) in open(arch_params.__file__, encoding="utf-8").read(), "setting not in arch_params: " + rest
        except Exception as ex:
            return False, f"arch_params: {ex}"
    if kind == "py":
        parts = rest.split(".")
        for i in range(len(parts), 0, -1):
            try:
                obj = importlib.import_module(".".join(parts[:i]))
            except Exception:
                continue
            for a in parts[i:]:
                if not hasattr(obj, a):
                    return False, f"{'.'.join(parts[:i])} has no {a}"
                obj = getattr(obj, a)
            return True, ""
        return False, "cannot import " + parts[0]
    return False, f"unknown target kind {kind!r} in {t!r}"


def check(lessons=None):
    """-> list of problems (empty = every lesson is complete and every target exists)."""
    lessons = lessons if lessons is not None else load()
    bad, seen = [], set()
    for L in lessons:
        i = L.get("id")
        miss = [k for k in FIELDS if k not in L or (k != "trains_via" and not L[k])]
        if miss:
            bad.append(f"{i}: missing {', '.join(miss)}")
        if i in seen:
            bad.append(f"{i}: duplicate id")
        seen.add(i)
        if L.get("status") not in STATUS:
            bad.append(f"{i}: status one of {', '.join(STATUS)}")
        for k in ("enforced_by", "checked_by"):
            for t in _targets(L.get(k)):
                ok, why = target_ok(t)
                if not ok:
                    bad.append(f"{i}: {k} {t}: {why}")
    return bad


def table(lessons=None):
    rows = lessons if lessons is not None else load()
    out = ["| id | lesson | evidence | enforced by | checked by | trains via | status |", "|---|---|---|---|---|---|---|"]
    for L in rows:
        out.append("| " + " | ".join([L["id"], L["lesson"], L["evidence"], ", ".join(f"`{t}`" for t in _targets(L["enforced_by"])),
                                       ", ".join(f"`{t}`" for t in _targets(L["checked_by"])), L.get("trains_via") or "-",
                                       L["status"]]) + " |")
    return "\n".join(out)


def main():
    rows = load()
    print(table(rows))
    bad = check(rows)
    print(f"\n{len(rows)} lessons, {len(bad)} problems")
    for b in bad:
        print("  " + b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
