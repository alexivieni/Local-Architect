"""Fixtures for the Markets pack's eval families: a deterministic price file and strategies whose defect is KNOWN (the look-ahead is planted,
so a judge can say whether it is gone). Standard library only. Nothing here is a trading recommendation: the series is a seeded random
walk, and "clean" means "does not look ahead", never "makes money"."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import markets_data as md  # noqa: E402


def prices_csv(n=900, seed=11):
    rows = ["timestamp,open,high,low,close,volume"]
    for b in md.synthetic_bars(n, seed=seed):
        rows.append(f"{b['t']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']}")
    return "\n".join(rows) + "\n"


# name -> (leaky source, repaired source). Each repaired version trades about as often and uses only bars up to and including the signal's own.
STRATEGIES = {
    "next-close": (
        "def signals(bars):\n    c = [b['close'] for b in bars]\n    n = len(c)\n    return [1.0 if i + 1 < n and c[i + 1] > c[i] else -1.0 for i in range(n)]\n",
        "def signals(bars):\n    c = [b['close'] for b in bars]\n    return [1.0 if i > 0 and c[i] > c[i - 1] else -1.0 for i in range(len(c))]\n",
    ),
    "whole-series-mean": (
        "def signals(bars):\n    c = [b['close'] for b in bars]\n    m = sum(c) / len(c)\n    return [1.0 if x > m else -1.0 for x in c]\n",
        "def signals(bars):\n    out, total = [], 0.0\n    for i, b in enumerate(bars):\n        total += b['close']\n        out.append(1.0 if b['close'] > total / (i + 1) else -1.0)\n    return out\n",
    ),
    "future-high": (
        "def signals(bars):\n    n = len(bars)\n    return [1.0 if i + 1 < n and bars[i + 1]['high'] > bars[i]['high'] else -1.0 for i in range(n)]\n",
        "def signals(bars):\n    return [1.0 if i > 0 and bars[i]['high'] > bars[i - 1]['high'] else -1.0 for i in range(len(bars))]\n",
    ),
    "whole-series-max": (
        "def signals(bars):\n    c = [b['close'] for b in bars]\n    hi = max(c)\n    return [1.0 if x > 0.97 * hi else -1.0 for x in c]\n",
        "def signals(bars):\n    out, hi = [], 0.0\n    for b in bars:\n        hi = max(hi, b['close'])\n        out.append(1.0 if b['close'] > 0.97 * hi else -1.0)\n    return out\n",
    ),
}
SWITCHED_OFF = "def signals(bars):\n    return [0.0] * len(bars)\n"
