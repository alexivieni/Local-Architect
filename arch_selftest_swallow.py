"""Self-test: do-nothing exception handlers can only go down (tools/swallow_audit.py, baseline data/swallow_baseline.json).

`except Exception: pass` hides every error, the expected and the unexpected alike. No file may have more of them than its baseline,
and a new file may have none. The engine, the model servers and the hub have none left (2026-11-08): they report what they swallow
to local telemetry (`_tel_swallow` / `arch_server._swallow`, rate-limited). The scanner itself is proven on fixtures.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    import arch_core  # noqa: F401
    import swallow_audit as sa

    d = tempfile.mkdtemp(prefix="swallow_")
    p = os.path.join(d, "m.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("def a():\n    try:\n        x()\n    except Exception:\n        pass\n"
                "def b():\n    try:\n        x()\n    except:\n        ...\n"
                "def c():\n    try:\n        x()\n    except (ValueError, BaseException):\n        pass\n"
                "def d():\n    try:\n        x()\n    except OSError:\n        pass\n"                      # narrow: fine
                "def e():\n    try:\n        x()\n    except Exception as ex:\n        log(ex)\n"           # reports: fine
                "def f():\n    try:\n        x()\n    except Exception:  # intentional: the recorder must never report through itself\n        pass\n")
    found = sa.handlers(p)
    check("the scanner finds broad do-nothing handlers (Exception, bare, BaseException in a tuple) and names their function",
          [fn for _, fn in found] == ["a", "b", "c"], found)
    check("a narrow handler, one that does something, and one marked 'intentional' are not counted", len(found) == 3, found)
    with open(p, "w", encoding="utf-8") as f:
        f.write("def broken(:\n")
    check("a file that does not parse counts nothing (and never raises)", sa.handlers(p) == [])

    base = sa.baseline()
    check("the baseline exists and names files with counts", base and all(isinstance(v, int) for v in base.values()), base)
    bad = sa.over()
    check("no file has more do-nothing handlers than its baseline (new code reports what it swallows: _tel_swallow)",
          not bad, {f: f"{n} now, {a} allowed" for f, (n, a) in bad.items()})
    c = sa.counts()
    for f in ("arch_core.py", "arch_server.py", "architect_hub.py", "model_hub.py"):
        check(f"{f} has no do-nothing handler left", c.get(f, 0) == 0, sa.handlers(os.path.join(HERE, f)))
    check("the self-tests are not scanned (they provoke failures on purpose)", not any(k.startswith("arch_selftest") for k in c))
    total = sum(c.values())
    print(f"  ({total} do-nothing handlers left in {len(c)} files; baseline {sum(base.values())})")

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SWALLOW CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SWALLOW CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
