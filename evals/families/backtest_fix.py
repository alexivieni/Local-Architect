"""backtest_fix: a strategy that looks into its own future (planted) is repaired; the look-ahead check re-run by code must pass, the strategy must
still trade (a switched-off strategy passes most checks and fixes nothing), and the price data must be untouched."""
import _bank as B
import _markets_bank as M

FLOW = "backtest_fix"
DEFAULT_N = 4
GOAL = "Fix the look-ahead in strategy.py. Audit it on prices.csv."


def generate(n, seed=0):
    rng = B.rng_for(seed, "backtest_fix")
    prices = M.prices_csv()
    names = sorted(M.STRATEGIES)
    out = []
    for i, name in enumerate(B.pick(rng, names, n)):
        bad, good = M.STRATEGIES[name]
        out.append({"id": f"backtest_fix-{name}-{i:02d}", "tags": ["markets"], "goal": GOAL,
                    "fixture": {"files": {"strategy.py": bad, "prices.csv": prices}},
                    "budget": {"calls": 12, "tokens": 60000, "seconds": 900},
                    "judge": [{"type": "bt_gate", "gate": "bt_leak", "expect": True, "goal": "audit strategy.py on prices.csv", "min_trades": 20},
                              {"type": "files_unchanged", "paths": ["prices.csv"]}],
                    "solution": {"files": {"strategy.py": good}},
                    "wrong": {"files": {"strategy.py": M.SWITCHED_OFF if i % 2 else bad}}})
    return out
