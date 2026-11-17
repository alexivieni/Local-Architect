"""Markets pack: the backtest kernel and the statistics the gates use. Standard library only, deterministic, and small enough to read.

The strategy contract (docs/claude/markets.md): a module with  signals(bars) -> list  of target positions, one per bar, each in [-1, 1]
(None or NaN means flat). `signals` sees the bars it is given and nothing else, so it can be called on a prefix of the data: that is how
the look-ahead check works (a signal for bar i must not change when the bars after i are removed).

Timing is fixed here, not by the strategy: the target decided on bar i's close is filled at bar i+1's OPEN and held to bar i+2's open, and
every change of position pays (fee + slippage) basis points per unit traded. So a backtest through this kernel cannot fill at a price the
strategy had already seen, and cannot be run without costs.

This file is also the worker: `python markets_kernel.py <request.json> <result.json>` imports the strategy in its own process (so its code
never runs inside the harness), calls signals() on the slices asked for, and writes the signals back. Nothing here reads the network.
"""
import json
import math
import os
import sys
from statistics import NormalDist

EULER = 0.5772156649015329
_N = NormalDist()


# ------------------------------------------------------------------------------------------------------------- the kernel
def run(bars, sig, fee_bps, slip_bps):
    """-> list of (gross, net, turnover), one per tradable bar i (0 .. n-3): position decided at bar i, held from open[i+1] to open[i+2]."""
    n = len(bars)
    rate = (float(fee_bps) + float(slip_bps)) / 1e4
    pos = [0.0 if (s is None or s != s) else float(s) for s in sig]
    out, prev = [], 0.0
    for i in range(max(0, n - 2)):
        p = pos[i]
        gross = p * (bars[i + 2]["open"] / bars[i + 1]["open"] - 1.0)
        t = abs(p - prev)
        out.append((gross, gross - t * rate, t))
        prev = p
    return out


def _moments(xs):
    n = len(xs)
    m = sum(xs) / n
    d2 = sum((x - m) ** 2 for x in xs)
    sd = math.sqrt(d2 / (n - 1)) if n > 1 else 0.0
    if n < 4 or d2 <= 0:
        return m, sd, 0.0, 3.0
    s2 = d2 / n
    skew = (sum((x - m) ** 3 for x in xs) / n) / s2 ** 1.5
    kurt = (sum((x - m) ** 4 for x in xs) / n) / s2 ** 2
    return m, sd, skew, kurt


def stats(rets, ppy=252):
    """Summary of a slice of run() output. Returns are per bar; `sharpe` is per bar, `sharpe_annual` scales it by sqrt(periods per year)."""
    if not rets:
        return {"bars": 0, "trades": 0, "total": 0.0, "gross_total": 0.0, "sharpe": 0.0, "sharpe_annual": 0.0, "max_drawdown": 0.0, "skew": 0.0, "kurt": 3.0}
    net = [r[1] for r in rets]
    eq, peak, dd, g = 1.0, 1.0, 0.0, 1.0
    for r in rets:
        eq = max(0.0, eq * (1.0 + r[1]))
        g = max(0.0, g * (1.0 + r[0]))
        peak = max(peak, eq)
        dd = max(dd, 1.0 - eq / peak if peak > 0 else 0.0)
    m, sd, sk, ku = _moments(net)
    sr = m / sd if sd > 0 else 0.0
    return {"bars": len(rets), "trades": sum(1 for r in rets if r[2] > 1e-9), "total": eq - 1.0, "gross_total": g - 1.0, "sharpe": sr,
            "sharpe_annual": sr * math.sqrt(ppy), "max_drawdown": dd, "skew": sk, "kurt": ku}


# --------------------------------------------------------------------------------------- deflated Sharpe (Bailey, Lopez de Prado)
def psr(sr, t, skew, kurt, benchmark=0.0):
    """Probability that the true Sharpe (per bar) exceeds `benchmark`, given t observations and the returns' skew and kurtosis. None when undefined."""
    if t < 3:
        return None
    den = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if den <= 0:
        return None
    return _N.cdf((sr - benchmark) * math.sqrt(t - 1.0) / math.sqrt(den))


def expected_max_sr(trial_srs):
    """The Sharpe the BEST of N pure-noise trials is expected to reach, from the spread of the trials actually run. 0 for a single trial."""
    n = len(trial_srs)
    if n < 2:
        return 0.0
    m = sum(trial_srs) / n
    var = sum((x - m) ** 2 for x in trial_srs) / (n - 1)
    return math.sqrt(var) * ((1.0 - EULER) * _N.inv_cdf(1.0 - 1.0 / n) + EULER * _N.inv_cdf(1.0 - 1.0 / (n * math.e)))


def dsr(sr, t, skew, kurt, trial_srs):
    """Deflated Sharpe: PSR against the Sharpe that luck alone would have produced after len(trial_srs) tries. -> (dsr or None, benchmark)."""
    b = expected_max_sr(trial_srs)
    return psr(sr, t, skew, kurt, b), b


# --------------------------------------------------------------------------------------------------------------- the worker
def _clean(v):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "bad"
    if v != v:
        return None
    if v in (float("inf"), float("-inf")):
        return 1e308 if v > 0 else -1e308
    return v


def _as_list(x):
    for attr in ("tolist",):                                   # numpy arrays and pandas Series
        if hasattr(x, attr):
            x = getattr(x, attr)()
    return list(x)


def worker(req_path, out_path):
    import importlib.util
    import traceback
    req = json.load(open(req_path, encoding="utf-8"))
    res = {"loaded": False, "has_signals": False, "params": None, "slices": {}}

    def finish():
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res, f, allow_nan=False)

    path = req["strategy"]
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec = importlib.util.spec_from_file_location("markets_strategy_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (ImportError, ModuleNotFoundError) as ex:           # something this machine lacks: the strategy was not judged
        res["import_error"] = {"kind": "missing", "msg": f"{type(ex).__name__}: {ex}"}
        return finish()
    except BaseException as ex:                                 # the strategy's own code is broken
        res["import_error"] = {"kind": "broken", "msg": "".join(traceback.format_exception_only(type(ex), ex)).strip()[-400:]}
        return finish()
    res["loaded"] = True
    fn = getattr(mod, "signals", None)
    res["has_signals"] = callable(fn)
    p = getattr(mod, "PARAMS", None)
    res["params"] = p if isinstance(p, dict) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p.values()) else None
    if not res["has_signals"]:
        return finish()
    bars, params = req["bars"], req.get("params") or {}
    for n in req["slices"]:
        try:
            out = _as_list(fn([dict(b) for b in bars[:n]], **params))
        except BaseException as ex:
            res["slices"][str(n)] = {"error": "".join(traceback.format_exception_only(type(ex), ex)).strip()[-300:]}
            continue
        if len(out) != n:
            res["slices"][str(n)] = {"error": f"signals() returned {len(out)} value(s) for {n} bar(s); it must return exactly one per bar"}
            continue
        clean = [_clean(v) for v in out]
        if any(c == "bad" for c in clean):
            res["slices"][str(n)] = {"error": "signals() returned something that is not a number"}
            continue
        res["slices"][str(n)] = {"signals": clean}
    finish()


if __name__ == "__main__":
    worker(sys.argv[1], sys.argv[2])
