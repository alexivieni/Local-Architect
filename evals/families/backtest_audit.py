"""backtest_audit: read-only. The audit must name a planted look-ahead, say "could not check" when there is no price file instead of passing, and
change nothing."""
import _bank as B
import _markets_bank as M

FLOW = "backtest_audit"
DEFAULT_N = 4
GOAL = "Audit strategy.py: is its backtest trustworthy?"
REPORT = "# Backtest audit\n\n| check | result | detail |\n|---|---|---|\n{needle} |\n"


def generate(n, seed=0):
    rng = B.rng_for(seed, "backtest_audit")
    prices = M.prices_csv()
    out = []
    kinds = ["leak", "nodata", "clean"]
    names = sorted(M.STRATEGIES)
    for i in range(n):
        kind = kinds[i % len(kinds)]
        name = names[(i + rng.randrange(len(names))) % len(names)]
        bad, good = M.STRATEGIES[name]
        files = {"strategy.py": bad if kind == "leak" else good}
        if kind != "nodata":
            files["prices.csv"] = prices
        needle = {"leak": "| leak | did not pass |", "nodata": "| costs | could not check |", "clean": "| leak | passed |"}[kind]
        out.append({"id": f"backtest_audit-{kind}-{name}-{i:02d}", "tags": ["markets"], "goal": GOAL,
                    "fixture": {"files": files},
                    "budget": {"calls": 6, "tokens": 40000, "seconds": 600},
                    "judge": [{"type": "file_exists", "paths": ["docs/backtest_audit.md"]},
                              {"type": "file_contains", "path": "docs/backtest_audit.md", "needles": [needle]},
                              {"type": "files_unchanged", "paths": list(files)}],
                    "solution": {"files": {"docs/backtest_audit.md": REPORT.format(needle=needle)}},
                    "wrong": {"files": {"strategy.py": M.SWITCHED_OFF}}})
    return out
