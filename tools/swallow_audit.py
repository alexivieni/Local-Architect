"""Do-nothing exception handlers: `except:` / `except Exception:` / `except BaseException:` whose whole body is `pass` (or `...`).

    py -3.12 tools/swallow_audit.py                   # count per file, worst first, against the baseline
    py -3.12 tools/swallow_audit.py --list FILE.py    # each one with its line and function
    py -3.12 tools/swallow_audit.py --write-baseline  # record today's counts (only ever after LOWERING them)

Such a handler hides every error, including the ones nobody expected; the 2026-11-08 audit counted ~370 and two of that day's bugs
hid behind them. The ratchet (arch_selftest_swallow) fails when any file has MORE than its baseline, so new ones cannot appear;
existing ones are turned into a report (`_tel_swallow(where, ex)`: local telemetry, rate-limited) as the code is touched.

A handler that must stay silent says why on its `except` line or in its body: a comment containing "intentional" exempts it
(e.g. the telemetry recorder itself, which must never report through itself).
"""
import ast
import glob
import json
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(APP, "data", "swallow_baseline.json")
BROAD = {"Exception", "BaseException"}


def _broad(t):
    if t is None:
        return True
    if isinstance(t, ast.Name):
        return t.id in BROAD
    if isinstance(t, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in BROAD for e in t.elts)
    return False


def _empty(body):
    return all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in body)


def handlers(path):
    """-> [(line, function name)] of the do-nothing handlers in one file (exempt ones left out). [] for a file that does not parse."""
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return []
    lines = src.splitlines()
    out = []

    def walk(node, fn):
        for ch in ast.iter_child_nodes(node):
            name = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else fn
            if isinstance(ch, ast.ExceptHandler) and _broad(ch.type) and _empty(ch.body):
                end = max(getattr(s, "end_lineno", s.lineno) for s in ch.body)
                text = "\n".join(lines[ch.lineno - 1:end]).lower()
                if "intentional" not in text:
                    out.append((ch.lineno, fn or "<module>"))
            walk(ch, name)
    walk(tree, None)
    return out


def files(app=APP):
    """Product code: the app's modules and tools. Self-tests and the hub check are left out (they test failures on purpose)."""
    out = []
    for p in sorted(glob.glob(os.path.join(app, "*.py")) + glob.glob(os.path.join(app, "tools", "*.py"))):
        b = os.path.basename(p)
        if b.startswith(("arch_selftest", "arch_hub_check")):
            continue
        out.append(p)
    return out


def counts(app=APP):
    return {os.path.relpath(p, app).replace("\\", "/"): n for p in files(app) if (n := len(handlers(p)))}


def baseline():
    try:
        with open(BASELINE, encoding="utf-8") as f:
            return json.load(f).get("files", {})
    except (OSError, ValueError):
        return {}


def over(app=APP):
    """-> {file: (now, allowed)} for every file with more do-nothing handlers than its baseline (a new file is allowed 0)."""
    base = baseline()
    return {f: (n, base.get(f, 0)) for f, n in counts(app).items() if n > base.get(f, 0)}


def main():
    if "--list" in sys.argv:
        f = sys.argv[sys.argv.index("--list") + 1]
        for line, fn in handlers(os.path.join(APP, f)):
            print(f"  {f}:{line}  in {fn}")
        return 0
    c = counts()
    if "--write-baseline" in sys.argv:
        old = baseline()
        up = {f: (n, old.get(f, 0)) for f, n in c.items() if n > old.get(f, 0)}
        if old and up and "--force" not in sys.argv:
            print("refused: these files have MORE than their baseline (fix them, or --force if you really mean it):")
            for f, (n, a) in sorted(up.items()):
                print(f"  {f}: {n} (baseline {a})")
            return 1
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump({"about": "do-nothing exception handlers per file (tools/swallow_audit.py); may only go down", "total": sum(c.values()),
                       "files": dict(sorted(c.items()))}, fh, indent=1)
        print(f"baseline written: {sum(c.values())} in {len(c)} files")
        return 0
    base = baseline()
    for f, n in sorted(c.items(), key=lambda kv: -kv[1]):
        a = base.get(f, 0)
        print(f"  {n:4}  {f}" + ("" if n == a else f"   (baseline {a})"))
    bad = over()
    print(f"\n{sum(c.values())} do-nothing handlers in {len(c)} files; baseline {sum(base.values())}" + (f"; OVER in {len(bad)} file(s)" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
