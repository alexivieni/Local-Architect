"""Self-test for the Markets pack (M1): the price-data checks, the backtest kernel and its statistics, the bt_* gates, and the two flows
(backtest_audit, backtest_fix) run end to end with a scripted model.

What matters here is that CODE decides, in three outcomes: a planted look-ahead is found, a clean strategy is not accused, a check that
cannot run (no price file, a missing library, no strategy) is "could not check" and never a pass, and a "fix" that switches the strategy
off or edits the data is refused. Real worker processes run the strategies; no network, no GPU, no real model, nothing of the person's.
"""
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "evals", "families"))

LEAKY = "def signals(bars):\n    c = [b['close'] for b in bars]\n    n = len(c)\n    return [1.0 if i + 1 < n and c[i + 1] > c[i] else -1.0 for i in range(n)]\n"
CLEAN = "def signals(bars):\n    c = [b['close'] for b in bars]\n    return [1.0 if i > 0 and c[i] > c[i - 1] else -1.0 for i in range(len(c))]\n"
OFF = "def signals(bars):\n    return [0.0] * len(bars)\n"
WANDER = "import random\n\n\ndef signals(bars):\n    return [random.choice([-1.0, 1.0]) for _ in bars]\n"
TOOBIG = "def signals(bars):\n    return [2.0] * len(bars)\n"
NOFUNC = "def other(bars):\n    return [0.0] * len(bars)\n"
BROKEN = "def signals(bars):\n    return 1 / 0\n"
SYNTAX = "def signals(bars:\n    return []\n"
NEEDS_LIB = "import a_library_nobody_has_xyz\n\n\ndef signals(bars):\n    return [0.0] * len(bars)\n"
SHORT = "def signals(bars):\n    return [0.0]\n"
# profitable only when k == 5 (it peeks ahead k bars; at any other k it stays flat): a knife-edge parameter
KNIFE = "PARAMS = {'k': 5}\n\n\ndef signals(bars, k=5):\n    c = [b['close'] for b in bars]\n    n = len(c)\n    if k != 5:\n        return [0.0] * n\n    return [1.0 if i + k < n and c[i + k] > c[i] else -1.0 for i in range(n)]\n"
STEADY = "PARAMS = {'k': 5}\n\n\ndef signals(bars, k=5):\n    c = [b['close'] for b in bars]\n    n = len(c)\n    return [1.0 if i + k < n and c[i + k] > c[i] else -1.0 for i in range(n)]\n"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== the Markets pack: price data, kernel, gates, flows ==")
    import arch_core as core
    import arch_flows  # noqa: F401  (arch_flows_pro only imports cleanly after arch_flows)
    import arch_dataflow as df
    import arch_eval
    import arch_gates as G
    import arch_roles
    import arch_selftest
    import markets_data as md
    import markets_gates as M
    import markets_kernel as mk

    home = tempfile.mkdtemp(prefix="archmk_")
    old_home = core.HOME
    core.set_home(home)
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    mode = {"fix": CLEAN}

    class Scripted(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            if role == "quant_dev":
                return f"FILE: strategy.py\n```python\n{mode['fix']}```\n"
            return "The audit says what it says."

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    try:
        _data_checks(check, md, home)
        _kernel_checks(check, mk, md)
        _gate_checks(check, core, G, M, md, home)
        _gap_checks(check, core, G, md, arch_roles, home)
        _flow_checks(check, core, df, G, M, md, arch_roles, home, mode)
        _registry_checks(check, G, arch_roles, arch_eval, df)
    finally:
        srv.shutdown()
        core.set_home(old_home)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' MARKETS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' MARKETS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


# ------------------------------------------------------------------------------------------------------------------------ data
def _write_csv(path, bars):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("timestamp,open,high,low,close,volume\n")
        for b in bars:
            f.write(f"{b['t']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']}\n")


def _data_checks(check, md, home):
    d = os.path.join(home, "csv")
    os.makedirs(d)
    good = md.synthetic_bars(400, seed=1)
    p = os.path.join(d, "good.csv")
    _write_csv(p, good)
    bars, probs = md.load_bars(p)
    check("a well-formed price file loads in full with no complaints", len(bars) == 400 and not probs and md.check_bars(bars) == [], (len(bars), probs, md.check_bars(bars)))
    check("synthetic bars are repeatable for one seed and differ for another", md.synthetic_bars(50, seed=3) == md.synthetic_bars(50, seed=3) and md.synthetic_bars(50, seed=3) != md.synthetic_bars(50, seed=4))
    check("a file with too few bars is an error, not a quiet pass", any(s == "error" and "at least" in t for s, t in md.check_bars(good[:50])))
    swapped = [dict(b) for b in good]
    swapped[100], swapped[101] = swapped[101], swapped[100]
    check("timestamps out of order are an error", any(s == "error" and "increasing" in t for s, t in md.check_bars(swapped)))
    inv = [dict(b) for b in good]
    inv[10]["high"], inv[10]["low"] = inv[10]["low"], inv[10]["high"]
    check("a bar whose high is below its low is an error", any(s == "error" and "high is below low" in t for s, t in md.check_bars(inv)))
    neg = [dict(b) for b in good]
    neg[5]["close"] = -1.0
    check("a negative price is an error", any(s == "error" and "zero, negative" in t for s, t in md.check_bars(neg)))
    gap = [dict(b) for b in good]
    for i in range(200, 400):
        gap[i]["t"] = str(int(gap[i]["t"]) + 90000)
    check("a long gap is a warning (weekends are normal), not an error", any(s == "warn" and "gap" in t for s, t in md.check_bars(gap)) and not any(s == "error" for s, t in md.check_bars(gap)))
    q = os.path.join(d, "nocol.csv")
    with open(q, "w", encoding="utf-8") as f:
        f.write("when,price\n1,2\n")
    check("a file without the needed columns says which are missing", "missing column" in md.load_bars(q)[1][0], md.load_bars(q))
    r = os.path.join(d, "badrow.csv")
    with open(r, "w", encoding="utf-8") as f:
        f.write("Date,Open,High,Low,Close,Volume\n2024-01-01,1,2,0.5,1.5,10\n2024-01-02,x,2,0.5,1.5,10\n")
    b2, p2 = md.load_bars(r)
    check("headers in any case and ISO dates are read, and an unreadable row is skipped and counted", len(b2) == 1 and "skipped" in p2[0], (len(b2), p2))
    check("epoch seconds, epoch milliseconds and ISO text all read as seconds",
          md.ts_seconds("1700000000") == 1700000000.0 and md.ts_seconds("1700000000000") == 1700000000.0 and md.ts_seconds("2023-11-14T22:13:20Z") == 1700000000.0)
    check("a missing file is a problem report, not a crash", md.load_bars(os.path.join(d, "nope.csv"))[0] == [])


# ---------------------------------------------------------------------------------------------------------------------- kernel
def _bar(o):
    return {"t": "0", "open": o, "high": o, "low": o, "close": o, "volume": 1}


def _kernel_checks(check, mk, md):
    bars = [_bar(100.0), _bar(110.0), _bar(121.0), _bar(121.0)]
    r = mk.run(bars, [1.0, 1.0, 1.0, 1.0], 0, 0)
    check("a signal decided at bar i is filled at bar i+1's open and held to bar i+2's open (no fill at a price already seen)",
          len(r) == 2 and abs(r[0][0] - (121.0 / 110.0 - 1)) < 1e-12 and abs(r[1][0] - 0.0) < 1e-12, r)
    r = mk.run(bars, [1.0, 1.0, 1.0, 1.0], 50, 50)
    check("entering a position pays fee + slippage once, holding it pays nothing more", abs(r[0][2] - 1.0) < 1e-12 and abs(r[0][1] - (r[0][0] - 0.01)) < 1e-12 and r[1][2] == 0.0, r)
    r = mk.run(bars, [1.0, -1.0, 1.0, 1.0], 100, 0)
    check("flipping from long to short trades two units and pays for both", abs(r[1][2] - 2.0) < 1e-12 and abs(r[1][1] - (r[1][0] - 0.02)) < 1e-9, r)
    check("None and NaN signals are flat", mk.run(bars, [None, float("nan"), None, None], 5, 5) == [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)])
    s = mk.stats(mk.run(md.synthetic_bars(500, seed=2), [0.0] * 500, 5, 5))
    check("a flat strategy returns exactly zero with no trades", s["total"] == 0.0 and s["trades"] == 0 and s["max_drawdown"] == 0.0, s)
    flip = [1.0 if i % 2 else -1.0 for i in range(500)]
    s = mk.stats(mk.run(md.synthetic_bars(500, seed=2), flip, 5, 5))
    check("trading on every bar gives up most of the edge to costs, and the statistics say how many trades there were", s["trades"] >= 497 and s["total"] < s["gross_total"], s)
    check("an empty slice has well-defined statistics", mk.stats([])["bars"] == 0)
    check("PSR against zero for a zero Sharpe is one half", abs(mk.psr(0.0, 1000, 0.0, 3.0, 0.0) - 0.5) < 1e-9)
    check("PSR is undefined (None) rather than invented when the variance term is not positive", mk.psr(10.0, 1000, 1.0, 1.0, 0.0) is None and mk.psr(0.1, 2, 0.0, 3.0, 0.0) is None)
    check("one trial has nothing to deflate; more trials raise the bar the Sharpe must clear", mk.expected_max_sr([0.05]) == 0.0 and mk.expected_max_sr([0.01, 0.05, 0.03, -0.02, 0.0]) > 0.0)
    a = mk.dsr(0.06, 1000, 0.0, 3.0, [0.06])[0]
    b = mk.dsr(0.06, 1000, 0.0, 3.0, [0.06, 0.01, 0.03, -0.02, 0.04, 0.0, 0.02])[0]
    check("the same Sharpe is less believable after many variants were tried", a is not None and b is not None and b < a, (a, b))


# ----------------------------------------------------------------------------------------------------------------------- gates
def _project(home, name, strategy, prices=True, seed=5, n=800, extra=None):
    proj = os.path.join(home, name)
    os.makedirs(proj, exist_ok=True)
    if strategy is not None:
        with open(os.path.join(proj, "strategy.py"), "w", encoding="utf-8") as f:
            f.write(strategy)
    if prices:
        import markets_data as md
        _write_csv(os.path.join(proj, "prices.csv"), md.synthetic_bars(n, seed=seed))
    for rel, text in (extra or {}).items():
        with open(os.path.join(proj, rel), "w", encoding="utf-8") as f:
            f.write(text)
    return proj


def _gate(core, G, proj, gate, goal="audit strategy.py on prices.csv", cfg_over=None, step=None):
    cfg = core.deep_merge(core.load_config(proj), cfg_over or {})
    e = core.Engine(proj, "backtest_audit", goal, cfg, review="auto", say=lambda *_a, **_k: None)
    return G.GATES[gate](e, {"goal": goal, "params": {}}, step or {}, [])


def _gate_checks(check, core, G, M, md, home):
    p = _project(home, "leaky", LEAKY)
    r = _gate(core, G, p, "bt_leak")
    check("look-ahead is found: a signal that changes when later bars are removed fails, naming the bar", r["ok"] is False and "look-ahead" in r["text"] and r["metrics"].get("first_leak_bar") is not None, r)
    p = _project(home, "clean", CLEAN)
    r = _gate(core, G, p, "bt_leak")
    check("a strategy that uses only bars up to its own is not accused", r["ok"] is True, r)
    check("the data and contract gates pass on a good file and a well-formed strategy",
          _gate(core, G, p, "bt_data")["ok"] is True and _gate(core, G, p, "bt_load")["ok"] is True)
    p = _project(home, "wander", WANDER)
    r = _gate(core, G, p, "bt_leak")
    check("a strategy that is random from run to run fails as not repeatable (it cannot be told apart from a leak)", r["ok"] is False and "not repeatable" in r["text"], r)
    p = _project(home, "nodata", CLEAN, prices=False)
    r = _gate(core, G, p, "bt_leak")
    check("with no price file the look-ahead check still runs, on synthetic bars, and says so", r["ok"] is True and "synthetic" in r["text"], r)
    check("with no price file costs, out-of-sample and trials are 'could not check', never a pass",
          all(_gate(core, G, p, g)["ok"] is None for g in ("bt_costs", "bt_oos", "bt_trials", "bt_sensitivity")))
    check("no price file is not itself a failure of the data gate, but it says what could not run", (lambda r: r["ok"] is True and "could not run" in r["text"])(_gate(core, G, p, "bt_data")))
    p = _project(home, "baddata", CLEAN, prices=False, extra={"prices.csv": "timestamp,open,high,low,close,volume\n1,1,2,3,4,5\n"})
    check("a price file with too few bars fails the data gate, and the cost gate cannot run on it", _gate(core, G, p, "bt_data")["ok"] is False and _gate(core, G, p, "bt_costs")["ok"] is None)

    for name, src, want, what in (("off", OFF, False, "never traded"), ("clean", CLEAN, False, "does not cover")):
        p = _project(home, "c_" + name, src)
        r = _gate(core, G, p, "bt_costs")
        check(f"costs: the {name} strategy does not pass, and the reason is stated ({what})", r["ok"] is want and what in r["text"], r)
    p = _project(home, "leaky2", LEAKY)
    check("costs: a strategy with a (leaky) edge big enough to cover costs passes this gate, which is why the leak gate exists", _gate(core, G, p, "bt_costs")["ok"] is True)
    r = _gate(core, G, p, "bt_costs", cfg_over={"markets": {"fee_bps": 0, "slippage_bps": 0}})
    check("costs: a backtest with zero fees and zero slippage is refused outright", r["ok"] is False and "not accepted" in r["text"], r)
    r = _gate(core, G, p, "bt_costs", step={"fee_bps": 5000, "slippage_bps": 0})
    check("costs: the step can set its own costs, and enormous ones fail the same strategy", r["ok"] is False, r)

    p = _project(home, "oos", CLEAN)
    r = _gate(core, G, p, "bt_oos")
    check("out-of-sample: a strategy that loses on the last 30% fails and the report gives both numbers", r["ok"] is False and r["metrics"].get("oos_total", 0) < 0, r)
    p = _project(home, "oos_few", OFF)
    check("out-of-sample: too few trades to judge is 'could not check', not a fail", _gate(core, G, p, "bt_oos")["ok"] is None)

    p = _project(home, "trials", LEAKY)
    a = _gate(core, G, p, "bt_trials")
    _gate(core, G, p, "bt_trials")
    ledger = os.path.join(p, ".arch", "markets", "trials.jsonl")
    n1 = len(open(ledger, encoding="utf-8").read().splitlines())
    check("trials: the first trial has nothing to deflate, and auditing the SAME strategy and data again does not count as a new variant", a["metrics"].get("trials") == 1 and n1 == 1, (a, n1))
    with open(os.path.join(p, "strategy.py"), "w", encoding="utf-8") as f:
        f.write(LEAKY + "\n# an edit\n")
    b = _gate(core, G, p, "bt_trials")
    check("trials: an edited strategy is a second variant, and the ledger keeps both", b["metrics"].get("trials") == 2 and len(open(ledger, encoding="utf-8").read().splitlines()) == 2, b)
    for i in range(6):
        with open(os.path.join(p, "strategy.py"), "w", encoding="utf-8") as f:
            f.write(CLEAN + f"\n# variant {i}\n" if i % 2 else LEAKY + f"\n# variant {i}\n")
        last = _gate(core, G, p, "bt_trials")
    check("trials: after many variants the headline number is deflated (the deflated probability falls as variants are added)", last["metrics"].get("trials", 0) >= 8, last)

    bad = {"big": TOOBIG, "nofunc": NOFUNC, "broken": BROKEN, "syntax": SYNTAX, "short": SHORT}
    want = {"big": "outside [-1, 1]", "nofunc": "no function signals", "broken": "ZeroDivisionError", "syntax": "fails to load", "short": "returned 1 value"}
    for k, src in bad.items():
        p = _project(home, "b_" + k, src, prices=False)
        r = _gate(core, G, p, "bt_load")
        check(f"contract: the {k} strategy fails to load and says why", r["ok"] is False and want[k] in r["text"], r)
    p = _project(home, "b_lib", NEEDS_LIB, prices=False)
    r = _gate(core, G, p, "bt_load")
    check("contract: a strategy that needs a library this machine lacks is 'could not check', not a failure of the strategy", r["ok"] is None and "does not have" in r["text"], r)
    p = _project(home, "b_none", None, prices=False)
    check("with no strategy file there is nothing to check: could not check", _gate(core, G, p, "bt_load")["ok"] is None)

    p = _project(home, "sens_knife", KNIFE)
    r = _gate(core, G, p, "bt_sensitivity")
    check("sensitivity: a strategy that only works at one exact parameter value fails", r["ok"] is False and r["metrics"].get("still_profitable") == 0, r)
    p = _project(home, "sens_steady", STEADY)
    r = _gate(core, G, p, "bt_sensitivity")
    check("sensitivity: nudging the parameter by +-20% leaves a steady strategy profitable", r["ok"] is True and r["metrics"].get("tested", 0) >= 2, r)
    p = _project(home, "sens_none", LEAKY)
    r = _gate(core, G, p, "bt_sensitivity")
    check("sensitivity: no PARAMS declared is 'not applicable' (a pass that tested nothing, labelled so), not a failure", r["ok"] is True and r["metrics"].get("tested") == 0, r)

    # the audit as a whole
    p = _project(home, "scan_leaky", LEAKY)
    r = _gate(core, G, p, "bt_scan")
    check("audit: a leaky strategy is reported as one code defect, with the other checks run", r["ok"] is True and r["metrics"]["findings"] >= 1 and r["metrics"]["defects"] == 1, r)
    p = _project(home, "scan_nodata", CLEAN, prices=False)
    r = _gate(core, G, p, "bt_scan")
    check("audit: with no price file four checks are 'could not check' and the audit cannot pass", r["metrics"]["unknown"] == 4 and r["metrics"]["findings"] == 0, r)
    p = _project(home, "scan_broken", BROKEN)
    r = _gate(core, G, p, "bt_scan")
    check("audit: a strategy that does not load blocks the checks that need its signals instead of running them on nothing", r["metrics"]["defects"] == 1 and r["metrics"]["unknown"] >= 4, r)


# ------------------------------------------------------------------------------------------------- the edges (Phase 0 of the debt review)
FUTURE_HIGH = "def signals(bars):\n    n = len(bars)\n    return [1.0 if i + 1 < n and bars[i + 1]['high'] > bars[i]['high'] else -1.0 for i in range(n)]\n"
SPINS = "def signals(bars):\n    while True:\n        pass\n"


def _gap_checks(check, core, G, md, arch_roles, home):
    p = _project(home, "slow", SPINS, prices=False)
    r = _gate(core, G, p, "bt_load", step={"timeout": 3})
    check("a strategy that never finishes is stopped, and the answer is 'could not check', not a fail or a pass", r["ok"] is None and "did not finish" in r["text"], r)

    p = _project(home, "badcfg", CLEAN)
    for over, word in (({"markets": {"fee_bps": "abc"}}, "markets.fee_bps must be a number"), ({"markets": {"oos_share": 5}}, "markets.oos_share must be between"),
                       ({"markets": {"min_trades": True}}, "markets.min_trades must be between")):
        try:
            _gate(core, G, p, "bt_costs", cfg_over=over)
            got = "no error"
        except ValueError as ex:
            got = str(ex)
        check(f"a bad setting is refused by name, never replaced by a default ({word})", word in got, got)
    cfg_ok = _gate(core, G, p, "bt_costs", cfg_over={"markets": {"fee_bps": "2.5", "slippage_bps": 2.5}})
    check("a number written as text is read as a number", cfg_ok["ok"] is False and "5 bps" in cfg_ok["text"], cfg_ok)

    p = _project(home, "ledger", LEAKY)
    _gate(core, G, p, "bt_trials")
    ledger = os.path.join(p, ".arch", "markets", "trials.jsonl")
    with open(ledger, "a", encoding="utf-8") as f:
        f.write("this is not json\n{\"family\": 1}\n\n")
    with open(os.path.join(p, "strategy.py"), "a", encoding="utf-8") as f:
        f.write("\n# edit\n")
    r = _gate(core, G, p, "bt_trials")
    check("a damaged ledger keeps its good lines: the trial count is not reset (forgetting trials would flatter the result)",
          r["metrics"].get("trials") == 2 and r["metrics"].get("ledger_skipped") == 2, r)

    p = _project(home, "partial", LEAKY)
    e = _engine(core, arch_roles, p, "backtest_fix", "Fix the look-ahead in strategy.py on prices.csv")
    ctx = {"goal": "Fix the look-ahead in strategy.py on prices.csv", "params": {}}
    G.GATES["bt_scan"](e, ctx, {}, [])
    with open(os.path.join(p, "strategy.py"), "w", encoding="utf-8") as f:
        f.write(FUTURE_HIGH)
    r = G.GATES["bt_fewer"](e, ctx, {}, ["strategy.py"])
    check("a 'repair' that is a different leak is refused: the defect is still there", r["ok"] is False and "still remain" in r["text"], r)

    p = _project(home, "introduce", CLEAN)
    e = _engine(core, arch_roles, p, "backtest_fix", "Fix strategy.py on prices.csv")
    ctx = {"goal": "Fix strategy.py on prices.csv", "params": {}}
    G.GATES["bt_scan"](e, ctx, {}, [])
    with open(os.path.join(p, "strategy.py"), "w", encoding="utf-8") as f:
        f.write(LEAKY)
    r = G.GATES["bt_fewer"](e, ctx, {}, ["strategy.py"])
    check("a change that introduces look-ahead into a clean strategy is refused", r["ok"] is False and "introduced look-ahead" in r["text"], r)


# ----------------------------------------------------------------------------------------------------------------------- flows
def _engine(core, arch_roles, proj, flow, goal):
    cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                   "behavior": {"battery_saver": "off", "recheck_before_retry": False}, "limits": {"max_revisions": 1}})
    return core.Engine(proj, flow, goal, cfg, review="auto", say=lambda *_a, **_k: None)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _flow_checks(check, core, df, G, M, md, arch_roles, home, mode):
    flows = df.load_all(say=lambda _m: None)
    goal = "Audit strategy.py on prices.csv"
    # audit: read-only, and honest
    p = _project(home, "fa_leaky", LEAKY)
    out = flows["backtest_audit"].run(_engine(core, arch_roles, p, "backtest_audit", goal), {})
    rep = os.path.join(p, "docs", "backtest_audit.md")
    check("audit flow: a leaky strategy does not pass, the report names the look-ahead, and nothing was changed",
          out["verdict"] == "fail" and os.path.isfile(rep) and "| leak | did not pass |" in _read(rep) and _read(os.path.join(p, "strategy.py")) == LEAKY, (out["verdict"], out.get("why")))
    check("audit flow: the plain-words explanation from the model is in the report", "The audit says what it says." in _read(rep), _read(rep)[-200:])
    p = _project(home, "fa_nodata", CLEAN, prices=False)
    out = flows["backtest_audit"].run(_engine(core, arch_roles, p, "backtest_audit", "Audit strategy.py"), {})
    check("audit flow: with no price file the verdict is not_sure, never pass", out["verdict"] == "not_sure", (out["verdict"], out.get("why")))
    p = _project(home, "fa_none", None, prices=False)
    out = flows["backtest_audit"].run(_engine(core, arch_roles, p, "backtest_audit", "Audit the strategy"), {})
    check("audit flow: with no strategy file at all the verdict is not_sure", out["verdict"] == "not_sure", (out["verdict"], out.get("why")))

    p = _project(home, "fa_badcfg", CLEAN)
    e = _engine(core, arch_roles, p, "backtest_audit", goal)
    e.cfg["markets"] = {"fee_bps": "abc"}
    r = flows["backtest_audit"]._gate(e, {"goal": goal, "params": {}}, {"id": "find"}, "bt_scan", [])
    check("audit flow: a bad setting reaches the person as 'could not check' with the setting's name, not as a crash or a default",
          r["ok"] is None and "markets.fee_bps must be a number" in r["text"], r)

    # fix: judged on code defects
    fixgoal = "Fix the look-ahead in strategy.py. Audit it on prices.csv."
    p = _project(home, "ff_ok", LEAKY)
    prices = _read(os.path.join(p, "prices.csv"))
    mode["fix"] = CLEAN
    out = flows["backtest_fix"].run(_engine(core, arch_roles, p, "backtest_fix", fixgoal), {})
    check("fix flow: a model that removes the look-ahead is accepted by the same code check, and the strategy file really changed",
          _read(os.path.join(p, "strategy.py")) == CLEAN and out["steps"].get("fix", {}).get("ok") is True, (out["verdict"], out.get("why"), out["steps"]))
    check("fix flow: a repaired strategy is still judged on costs and fresh data afterwards (verdict is fail, not pass: the idea has no edge)", out["verdict"] == "fail", out["verdict"])
    check("fix flow: the price data was not touched", _read(os.path.join(p, "prices.csv")) == prices)
    check("fix flow: the report says what a fix is and is not", "never tuned for" in _read(os.path.join(p, "docs", "backtest_fix.md")))

    p = _project(home, "ff_off", LEAKY)
    mode["fix"] = OFF
    out = flows["backtest_fix"].run(_engine(core, arch_roles, p, "backtest_fix", fixgoal), {})
    check("fix flow: a 'fix' that switches the strategy off is refused (it still trades)", out["steps"].get("fix", {}).get("ok") is not True and out["verdict"] != "pass", (out["verdict"], out["steps"]))
    p = _project(home, "ff_same", LEAKY)
    mode["fix"] = LEAKY
    out = flows["backtest_fix"].run(_engine(core, arch_roles, p, "backtest_fix", fixgoal), {})
    check("fix flow: handing back the same code does not pass", out["steps"].get("fix", {}).get("ok") is not True and out["verdict"] != "pass", (out["verdict"], out["steps"]))
    p = _project(home, "ff_clean", CLEAN)
    out = flows["backtest_fix"].run(_engine(core, arch_roles, p, "backtest_fix", fixgoal), {})
    check("fix flow: a strategy with no code defect is not touched (the fix step is skipped)", out["steps"].get("fix", {}).get("skipped") and _read(os.path.join(p, "strategy.py")) == CLEAN, out["steps"])

    # bt_fewer on its own: the data file is evidence
    p = _project(home, "ff_data", LEAKY)
    e = _engine(core, arch_roles, p, "backtest_fix", fixgoal)
    ctx = {"goal": fixgoal, "params": {}}
    G.GATES["bt_scan"](e, ctx, {}, [])
    with open(os.path.join(p, "strategy.py"), "w", encoding="utf-8") as f:
        f.write(CLEAN)
    r = G.GATES["bt_fewer"](e, ctx, {}, ["strategy.py", "prices.csv"])
    check("bt_fewer: a change that also touches the price file is refused, even if the strategy is repaired", r["ok"] is False and "only the strategy file" in r["text"], r)
    r = G.GATES["bt_fewer"](e, ctx, {}, ["strategy.py"])
    check("bt_fewer: the same repair on its own is accepted", r["ok"] is True and r["metrics"]["after_defects"] == 0, r)
    r = G.GATES["bt_fewer"](e, ctx, {}, [])
    check("bt_fewer: nothing applied is a failure, not a pass", r["ok"] is False)
    e2 = _engine(core, arch_roles, p, "backtest_fix", fixgoal)
    r = G.GATES["bt_fewer"](e2, {"goal": fixgoal, "params": {}}, {}, ["strategy.py"])
    check("bt_fewer: with no audit recorded first it cannot judge: could not check", r["ok"] is None)


# -------------------------------------------------------------------------------------------------------------------- registry
def _registry_checks(check, G, arch_roles, arch_eval, df):
    for g in ("bt_data", "bt_load", "bt_leak", "bt_costs", "bt_oos", "bt_trials", "bt_sensitivity", "bt_scan", "bt_fewer"):
        check(f"gate {g} is registered", g in G.GATES)
    for r in ("quant_dev", "backtest_auditor", "market_data_engineer"):
        check(f"role {r} exists with a fallback chain and a system prompt that states its limits", r in arch_roles.ROLES and r in arch_roles.FALLBACK and arch_roles.system_prompt(r))
    check("the roles that must not promise returns say so", "financial advice" in arch_roles.ROLES["backtest_auditor"]["system"] and "never predict" in arch_roles.ROLES["backtest_auditor"]["system"].lower())
    check("the strategy role is told to keep the strategy trading", "removing the signal is not a fix" in arch_roles.ROLES["quant_dev"]["system"])
    check("the eval judge bt_gate is registered", "bt_gate" in arch_eval.JUDGES)
    import arch_flows
    check("routing: a backtest request goes to the audit and a request to fix the look-ahead goes to the fix flow",
          _top(arch_flows, "audit my trading strategy for look-ahead bias") == "backtest_audit" and _top(arch_flows, "fix the look-ahead in strategy.py") == "backtest_fix")
    check("routing: a lone word like 'sharpe' in an ordinary request does not pull it into the audit", _top(arch_flows, "improve the sharpe of the build") != "backtest_audit")
    check("routing: ordinary requests still go where they went",_top(arch_flows, "add tests for the parser") == "test" and _top(arch_flows, "audit the code for bugs") == "hunt")
    check("the flows load from their YAML files", all(f in df.load_all(say=lambda _m: None) for f in ("backtest_audit", "backtest_fix")))
    # the markets.* settings: in the config defaults and the settings schema, equal to the gates' own defaults, and every schema range equals the gates' range
    import arch_core as core
    import arch_params
    from types import SimpleNamespace
    import markets_gates as mg
    schema = {k: (t, rng) for g in arch_params.SETTINGS.values() for (k, t, _title, _help, rng) in g if k.startswith("markets.")}
    check("markets.* settings: the config defaults equal markets_gates.DEFAULTS, and the schema lists every key with the gates' own range",
          core.DEFAULTS["markets"] == mg.DEFAULTS and set(schema) == {"markets." + k for k in mg.DEFAULTS}
          and all(tuple(schema["markets." + k][1]) == tuple(mg._RANGE[k]) for k in mg.DEFAULTS),
          (sorted(set(schema) ^ {"markets." + k for k in mg.DEFAULTS}), core.DEFAULTS.get("markets")))
    bad = mg._cfg(SimpleNamespace(cfg={"markets": {"fee_bps": 12.5}}), {})["fee_bps"], None
    try:
        mg._cfg(SimpleNamespace(cfg={"markets": {"oos_share": 5}}), {})
    except ValueError as ex:
        bad = (bad[0], str(ex))
    check("a markets.* value from the settings is used, and an out-of-range one is refused by name (never silently defaulted)",
          bad[0] == 12.5 and bad[1] and "oos_share" in bad[1], bad)


def _top(arch_flows, goal):
    import re
    g = goal.lower()
    sc = {k: len(re.findall(p, g)) for k, p in arch_flows.KEYWORDS.items()}
    return sorted(sc.items(), key=lambda kv: (-kv[1], arch_flows.PRIORITY.index(kv[0])))[0][0]


if __name__ == "__main__":
    sys.exit(run())
