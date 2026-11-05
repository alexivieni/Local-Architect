"""Apply rules row by row. Nothing is silently dropped: every change and rejection lands in the audit."""
from .convert import CONVERTERS, is_empty


def check_rules(rules):
    cols = rules.get("columns") or {}
    bad = [c for c, r in cols.items() if r.get("type", "text") not in CONVERTERS]
    if bad:
        raise ValueError(f"unknown type for columns: {bad}")
    missing_key = [k for k in rules.get("key", []) if k not in cols]
    if missing_key:
        raise ValueError(f"key columns not in rules: {missing_key}")
    return cols


def clean_rows(rows, rules):
    """rows: list of dicts (line numbers start at 2, after the header). -> (clean rows, audit)."""
    cols = check_rules(rules)
    key = rules.get("key", [])
    out, seen = [], {}
    audit = {"changed": 0, "rejected": [], "duplicates": [], "rows_in": len(rows)}
    for line, row in enumerate(rows, start=2):
        new, errors = {}, []
        for col, raw in row.items():
            rule = cols.get(col)
            if rule is None:
                new[col] = raw
                continue
            if is_empty(raw):
                if rule.get("required"):
                    errors.append(f"{col}: required")
                new[col] = ""
                continue
            fn = CONVERTERS[rule.get("type", "text")]
            try:
                new[col] = fn(raw, rule.get("case")) if fn is CONVERTERS["text"] else fn(raw)
            except ValueError as e:
                errors.append(f"{col}: {e}")
                continue
            if new[col] != raw:
                audit["changed"] += 1
        if errors:
            audit["rejected"].append({"line": line, "errors": errors})
            continue
        ident = tuple(new.get(k, "") for k in key) if key else tuple(sorted(new.items()))
        if ident in seen:
            audit["duplicates"].append({"line": line, "same_as": seen[ident]})
            continue
        seen[ident] = line
        out.append(new)
    audit["rows_out"] = len(out)
    return out, audit
