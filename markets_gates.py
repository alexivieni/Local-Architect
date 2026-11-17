"""Markets pack: the gates that decide whether a trading strategy's backtest means anything. Registered into arch_gates.GATES.

Each gate is deterministic code and answers True, False or None (could not be checked; never a pass and never a fail):

    bt_data         the price file is usable (order, gaps, impossible bars); no file given is not a failure
    bt_load         the strategy file follows the contract: signals(bars) -> one position in [-1, 1] per bar
    bt_leak         look-ahead: a signal must not change when the bars after it are removed (and must repeat run to run)
    bt_costs        the result survives fees and slippage; a backtest with zero costs is refused
    bt_oos          the last part of the data, which the strategy has not been tuned on, is still profitable with enough trades
    bt_trials       the Sharpe is deflated for how many variants of this strategy were tried (the trial ledger)
    bt_sensitivity  nudging each declared parameter by +-20% does not flip the result
    bt_scan/bt_fewer  all of the above before a fix and again after it. A fix is judged only on code defects (the file loads, no look-ahead):
                    fewer of them, none introduced, and the strategy still trades. Costs, out-of-sample and trials are never its target.

No price data means the cost, out-of-sample and trial checks answer None, so an audit without data can say "could not check" at best.
The strategy's own code runs only in a separate process (markets_kernel.py), with the harness's offline guard on.
"""
import hashlib
import json
import os
import sys
import tempfile
import time

import arch_core as core
import markets_data as md
import markets_kernel as mk
from arch_gates import GATES, R

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"fee_bps": 5.0, "slippage_bps": 5.0, "oos_share": 0.3, "min_trades": 20, "dsr_min": 0.95, "periods_per_year": 252,
            "sens_pct": 0.2, "sens_min_share": 0.75, "leak_tol": 1e-9, "timeout": 180, "synthetic_bars": 2000, "leak_tail": 3000, "max_variants": 12}
_CACHE = {}
_DATA = {}


# (low, high) each value must lie in, both ends included; a value outside, or one that is not a number, is refused by name rather than guessed at
_RANGE = {"fee_bps": (0, 10000), "slippage_bps": (0, 10000), "oos_share": (0.05, 0.95), "min_trades": (0, 1000000), "dsr_min": (0.5, 0.999),
          "periods_per_year": (1, 100000), "sens_pct": (0.01, 0.9), "sens_min_share": (0.1, 1.0), "leak_tol": (0, 1), "timeout": (1, 86400),
          "synthetic_bars": (200, 50000), "leak_tail": (200, 1000000), "max_variants": (1, 200)}


def _cfg(e, step):
    """The gates' settings: DEFAULTS, then the config's `markets` section, then the step's own keys. A bad value raises ValueError naming it
    (a flow turns that into 'could not check' with the reason), so a typo never silently falls back to a default."""
    c = dict(DEFAULTS)
    for src, label in ((((getattr(e, "cfg", None) or {}).get("markets")) or {}, "markets."), (step or {}, "step ")):
        for k, v in src.items():
            if k not in DEFAULTS:
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{label}{k} must be a number, got {v!r}") from None
            lo, hi = _RANGE[k]
            if isinstance(v, bool) or not (num == num and lo <= num <= hi):
                raise ValueError(f"{label}{k} must be between {lo} and {hi}, got {v!r}")
            c[k] = int(num) if isinstance(DEFAULTS[k], int) else num
    return c


def _pro():
    import arch_flows  # noqa: F401  (arch_flows_pro only imports cleanly after arch_flows)
    import arch_flows_pro
    return arch_flows_pro


def _pc(x):
    return f"{x * 100:+.1f}%"


# ------------------------------------------------------------------------------------------------------------------ finding the files
def _strategy(e, ctx):
    """The strategy file: named in the run's params or goal, else the one project file that defines signals()."""
    pro = _pro()

    def has(rel):
        try:
            return "def signals" in core.read_text(os.path.join(e.root, rel), 400000)[0]
        except OSError:
            return False
    want = (ctx.get("params") or {}).get("strategy")
    if want and os.path.isfile(os.path.join(e.root, want)):
        return want.replace("\\", "/")
    named = [r for r in pro.explicit_files(e, ctx.get("goal", ""), (".py",)) if not r.startswith(("tests/", "bench/")) and "/tests/" not in r]
    for rel in named:
        if has(rel):
            return rel
    if len(named) == 1:                  # the one file the goal names, even without signals(): the contract check then says what is missing
        return named[0]
    found = [f for f in core.list_files(e.root, e.cfg) if f.endswith(".py") and not f.startswith(("tests/", "bench/")) and "/tests/" not in f and has(f)]
    return found[0] if len(found) == 1 else None


def _data(e, ctx):
    """-> {"rel", "bars", "problems", "issues", "fp"} for the price file, or {"rel": None, "why": ...}. Read once per file version."""
    pro = _pro()
    rel = None
    want = (ctx.get("params") or {}).get("data")
    if want and os.path.isfile(os.path.join(e.root, want)):
        rel = want.replace("\\", "/")
    if not rel:
        ex = pro.explicit_files(e, ctx.get("goal", ""), (".csv",))
        rel = ex[0] if ex else None
    if not rel:
        found = []
        for d in ("", "data"):
            base = os.path.join(e.root, d)
            if os.path.isdir(base):
                found += [(d + "/" + f) if d else f for f in sorted(os.listdir(base)) if f.lower().endswith(".csv")]
        if len(found) > 1:
            return {"rel": None, "why": "several CSV files are here (" + ", ".join(found[:4]) + "): name the price file in the goal"}
        rel = found[0] if found else None
    if not rel:
        return {"rel": None, "why": "no price file was given (a CSV with timestamp, open, high, low, close)"}
    path = os.path.join(e.root, rel)
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if key not in _DATA:
        if len(_DATA) > 8:
            _DATA.clear()
        bars, problems = md.load_bars(path)
        _DATA[key] = {"rel": rel, "bars": bars, "problems": problems, "issues": md.check_bars(bars) if bars else [], "fp": md.fingerprint(path)}
    return _DATA[key]


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------------------------------------------- running the strategy
def _signals(e, rel, bars, tag, slices, params, cfg, fresh=False):
    """Signals for each slice length from the strategy, run in its own process. -> the worker's result dict, or {"run_error": text, "kind": ...}."""
    path = os.path.join(e.root, rel)
    key = (os.path.abspath(path), _sha(path), tag, tuple(slices), json.dumps(params or {}, sort_keys=True))
    if not fresh and key in _CACHE:
        return _CACHE[key]
    tmp = tempfile.mkdtemp(prefix="mk_")
    req, outp = os.path.join(tmp, "req.json"), os.path.join(tmp, "out.json")
    try:
        with open(req, "w", encoding="utf-8") as f:
            json.dump({"strategy": os.path.abspath(path), "bars": bars, "slices": list(slices), "params": params or {}}, f)
        rc, out = core.run_cmd([sys.executable, os.path.join(HERE, "markets_kernel.py"), req, outp], e.root, int(cfg["timeout"]))
        if rc == 124:
            return {"run_error": f"the strategy did not finish within {cfg['timeout']} s", "kind": "timeout"}
        if not os.path.isfile(outp):
            return {"run_error": "the strategy process stopped without a result: " + (out or "")[-300:].strip(), "kind": "crash"}
        with open(outp, encoding="utf-8") as f:
            res = json.load(f)
    except (OSError, ValueError) as ex:
        return {"run_error": f"could not run the strategy: {type(ex).__name__}: {ex}", "kind": "crash"}
    finally:
        for n in ("req.json", "out.json"):
            try:
                os.remove(os.path.join(tmp, n))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = res
    return res


def _problem(res):
    """What stops a worker result from being used. -> (None, "") when usable, else (None | False, why): None = could not check, False = the strategy is at fault."""
    if "run_error" in res:
        return None, res["run_error"]
    ie = res.get("import_error")
    if ie:
        return (None if ie["kind"] == "missing" else False), ("the strategy needs something this machine does not have: " if ie["kind"] == "missing" else "the strategy file fails to load: ") + ie["msg"]
    if not res.get("has_signals"):
        return False, "the file has no function signals(bars) (contract: signals(bars) -> one position in [-1, 1] per bar)"
    return None, ""


def _usable_bars(e, ctx, cfg, tail=None):
    """(bars, tag, data) the strategy is run on: the price file when there is one, else a seeded synthetic series (only the look-ahead check may use it)."""
    d = _data(e, ctx)
    if d.get("bars") and not any(s == "error" for s, _ in d["issues"]):
        b = d["bars"][-tail:] if tail else d["bars"]
        return b, f"{d['fp']}:{tail or 'all'}", d
    return md.synthetic_bars(int(cfg["synthetic_bars"])), f"synthetic:{cfg['synthetic_bars']}", d


def _real(e, ctx, cfg):
    """(bars, tag, data) for the price file only; (None, None, why) when there is none or it is unusable."""
    d = _data(e, ctx)
    if not d.get("rel"):
        return None, None, d.get("why")
    errs = [t for s, t in d["issues"] if s == "error"] + ([] if d["bars"] else d["problems"])
    if errs:
        return None, None, "the price file is not usable: " + "; ".join(errs)[:300]
    return d["bars"], f"{d['fp']}:all", None


def _full(e, ctx, cfg, rel, bars, tag, params=None):
    """-> (signals, None) for the full series, or (None, R) when the strategy could not be run or broke its contract."""
    res = _signals(e, rel, bars, tag, [len(bars)], params, cfg)
    ok, why = _problem(res)
    if why:
        return None, R(ok, why)
    s = res["slices"].get(str(len(bars))) or {}
    if "error" in s:
        return None, R(False, "signals() failed on the full data: " + s["error"])
    sig = s["signals"]
    bad = next((i for i, v in enumerate(sig) if v is not None and abs(v) > 1.0 + 1e-9), None)
    if bad is not None:
        return None, R(False, f"signal {sig[bad]:.4g} at bar {bad} is outside [-1, 1]; leverage is not supported, scale the position into that range")
    return sig, None


# --------------------------------------------------------------------------------------------------------------------------- the gates
def g_data(e, ctx, step, changed):
    d = _data(e, ctx)
    if not d.get("rel"):
        return R(True, d.get("why", "no price file") + ": the look-ahead check uses synthetic bars; cost, out-of-sample and trial checks could not run", {"data": 0})
    errs = [t for s, t in d["issues"] if s == "error"] + ([] if d["bars"] else d["problems"])
    warns = [t for s, t in d["issues"] if s == "warn"] + (d["problems"] if d["bars"] else [])
    if errs:
        return R(False, f"{d['rel']}: " + "; ".join(errs), {"data": len(d["bars"])}, "\n".join(warns))
    return R(True, ("; ".join(warns)) if warns else "", {"data": len(d["bars"]), "warnings": len(warns)})


def g_load(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found: name it in the goal, or keep one file that defines signals(bars)")
    bars, tag, _d = _usable_bars(e, ctx, cfg)
    sig, bad = _full(e, ctx, cfg, rel, bars, tag)
    if bad:
        return bad
    return R(True, "", {"bars": len(bars), "synthetic": tag.startswith("synthetic")})


def g_leak(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found")
    bars, tag, d = _usable_bars(e, ctx, cfg, tail=int(cfg["leak_tail"]))
    n = len(bars)
    cuts = sorted({n // 2, int(n * 0.65), int(n * 0.8), int(n * 0.9), n - 1})
    res = _signals(e, rel, bars, tag, cuts + [n], None, cfg)
    ok, why = _problem(res)
    if why:
        return R(ok, why)
    full = (res["slices"].get(str(n)) or {})
    if "error" in full:
        return R(False, "signals() failed on the full data: " + full["error"])
    full = full["signals"]
    again = _signals(e, rel, bars, tag, [n], None, cfg, fresh=True)       # a second, separate run: a strategy that is random cannot be told from a leaking one otherwise
    s2 = (again.get("slices") or {}).get(str(n)) or {}
    tol = float(cfg["leak_tol"])

    def same(a, b):
        return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= tol)
    if "signals" in s2 and not all(same(a, b) for a, b in zip(full, s2["signals"])):
        i = next(i for i, (a, b) in enumerate(zip(full, s2["signals"])) if not same(a, b))
        return R(False, f"the signals are not repeatable: the same data gave a different signal at bar {i} on a second run (unseeded randomness?). "
                        "A strategy that changes by itself cannot be checked for look-ahead or backtested.", {"first_diff_bar": i})
    leaks, unchecked = [], 0
    for c in cuts:
        s = res["slices"].get(str(c)) or {}
        if "signals" not in s:
            unchecked += 1
            continue
        for i in range(c):
            if not same(full[i], s["signals"][i]):
                leaks.append((i, c, full[i], s["signals"][i]))
                break
    if leaks:
        i, c, a, b = min(leaks)
        fa = "flat" if a is None else f"{a:.4g}"
        fb = "flat" if b is None else f"{b:.4g}"
        return R(False, f"look-ahead: the signal for bar {i} (t={bars[i]['t']}) is {fa} with the full data but {fb} when the {n - c} bar(s) after bar {c - 1} are removed, "
                        "so it uses prices from its own future. A signal may use only bars up to and including its own.",
                 {"first_leak_bar": i, "cuts_leaking": len(leaks), "cuts": len(cuts)}, "\n".join(f"cut at {c}: bar {i} {a} -> {b}" for i, c, a, b in sorted(leaks)[:5]))
    if unchecked == len(cuts):
        return R(None, "signals() raised on every shorter history, so no cut could be compared")
    return R(True, "" if not tag.startswith("synthetic") else "checked on synthetic bars (no price file given)",
             {"cuts": len(cuts) - unchecked, "synthetic": tag.startswith("synthetic")})


def _real_signals(e, ctx, cfg, rel):
    bars, tag, why = _real(e, ctx, cfg)
    if bars is None:
        return None, None, R(None, why or "no price data")
    sig, bad = _full(e, ctx, cfg, rel, bars, tag)
    return (bars, sig, None) if not bad else (None, None, bad)


def g_costs(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found")
    if float(cfg["fee_bps"]) + float(cfg["slippage_bps"]) <= 0:
        return R(False, "fees and slippage are both zero. A backtest without costs is not accepted: set markets.fee_bps and markets.slippage_bps to what you really pay.")
    bars, sig, bad = _real_signals(e, ctx, cfg, rel)
    if bad:
        return bad
    rets = mk.run(bars, sig, cfg["fee_bps"], cfg["slippage_bps"])
    s = mk.stats(rets, cfg["periods_per_year"])
    m = {"gross_total": round(s["gross_total"], 4), "net_total": round(s["total"], 4), "trades": s["trades"], "sharpe_annual": round(s["sharpe_annual"], 2),
         "max_drawdown": round(s["max_drawdown"], 3), "bars": s["bars"]}
    if s["trades"] == 0:
        return R(False, "the strategy never traded on this data", m)
    if s["total"] > 0:
        return R(True, "", m)
    return R(False, f"after {float(cfg['fee_bps']) + float(cfg['slippage_bps']):g} bps per unit traded the strategy returns {_pc(s['total'])} "
                    f"(before costs {_pc(s['gross_total'])}) over {s['bars']} bars and {s['trades']} trades: the edge does not cover what trading costs", m)


def g_oos(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found")
    bars, sig, bad = _real_signals(e, ctx, cfg, rel)
    if bad:
        return bad
    rets = mk.run(bars, sig, cfg["fee_bps"], cfg["slippage_bps"])
    cut = int(len(rets) * (1.0 - float(cfg["oos_share"])))
    a, b = mk.stats(rets[:cut], cfg["periods_per_year"]), mk.stats(rets[cut:], cfg["periods_per_year"])
    m = {"is_total": round(a["total"], 4), "oos_total": round(b["total"], 4), "oos_trades": b["trades"], "oos_bars": b["bars"], "oos_sharpe_annual": round(b["sharpe_annual"], 2)}
    if b["trades"] < int(cfg["min_trades"]):
        return R(None, f"only {b['trades']} trade(s) in the last {int(float(cfg['oos_share']) * 100)}% of the data (at least {cfg['min_trades']} are needed to judge it): too few to call", m)
    if b["total"] > 0:
        return R(True, "", m)
    return R(False, f"in the last {int(float(cfg['oos_share']) * 100)}% of the data the strategy returns {_pc(b['total'])} after costs ({b['trades']} trades) "
                    f"against {_pc(a['total'])} before it: it does not hold up on data it was not shaped on", m)


def _ledger(e):
    return os.path.join(e.root, ".arch", "markets", "trials.jsonl")


def g_trials(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found")
    bars, sig, bad = _real_signals(e, ctx, cfg, rel)
    if bad:
        return bad
    d = _data(e, ctx)
    s = mk.stats(mk.run(bars, sig, cfg["fee_bps"], cfg["slippage_bps"]), cfg["periods_per_year"])
    if s["bars"] < 30:
        return R(None, f"only {s['bars']} bars: too few to deflate a Sharpe ratio")
    key = _sha(os.path.join(e.root, rel)) + ":" + d["fp"]
    path = _ledger(e)
    rows, skipped = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(r, dict) and isinstance(r.get("family"), str) and isinstance(r.get("key"), str) and isinstance(r.get("sr"), (int, float)):
                    rows.append(r)
                else:
                    skipped += 1                       # one damaged line must not erase the history: forgetting trials would make the result look better than it is
    except OSError:
        rows = []
    if not any(r.get("key") == key and r.get("family") == rel for r in rows):
        row = {"family": rel, "key": key, "sr": s["sharpe"], "bars": s["bars"], "ts": int(time.time())}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            rows.append(row)
        except OSError as ex:
            return R(None, f"the trial ledger could not be written: {ex}")
    srs = [r["sr"] for r in rows if r.get("family") == rel]
    v, bench = mk.dsr(s["sharpe"], s["bars"], s["skew"], s["kurt"], srs)
    if v is None:
        return R(None, "the deflated Sharpe is undefined for these returns")
    m = {"trials": len(srs), "ledger_skipped": skipped, "dsr": round(v, 3), "sharpe_annual": round(s["sharpe_annual"], 2), "luck_sharpe_annual": round(bench * (cfg["periods_per_year"] ** 0.5), 2)}
    if v >= float(cfg["dsr_min"]):
        return R(True, "" if len(srs) > 1 else "first trial of this strategy: nothing to deflate yet", m)
    return R(False, f"after {len(srs)} variant(s) tried, the chance that this Sharpe ({s['sharpe_annual']:.2f}/yr) is more than luck is {v * 100:.0f}% "
                    f"(needs {float(cfg['dsr_min']) * 100:.0f}%); the best of {len(srs)} random strategies would reach about {bench * (cfg['periods_per_year'] ** 0.5):.2f}/yr", m)


def g_sensitivity(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found")
    bars, sig, bad = _real_signals(e, ctx, cfg, rel)
    if bad:
        return bad
    tag = _real(e, ctx, cfg)[1]
    base = _signals(e, rel, bars, tag, [len(bars)], None, cfg)
    params = base.get("params")
    if not params:
        return R(True, "no PARAMS declared, so there is nothing to vary", {"tested": 0})
    s0 = mk.stats(mk.run(bars, sig, cfg["fee_bps"], cfg["slippage_bps"]), cfg["periods_per_year"])
    if s0["total"] <= 0:
        return R(None, "not tested: the strategy already loses money after costs at its own parameters")
    pct = float(cfg["sens_pct"])
    variants = []
    for k, v in params.items():
        for f in (1.0 - pct, 1.0 + pct):
            nv = max(1, round(v * f)) if isinstance(v, int) else v * f
            if nv != v:
                variants.append({**params, k: nv})
    variants = variants[:int(cfg["max_variants"])]
    good = tested = 0
    for pv in variants:
        res = _signals(e, rel, bars, tag, [len(bars)], pv, cfg)
        sl = (res.get("slices") or {}).get(str(len(bars))) or {}
        if "signals" not in sl:
            continue
        tested += 1
        good += 1 if mk.stats(mk.run(bars, sl["signals"], cfg["fee_bps"], cfg["slippage_bps"]), cfg["periods_per_year"])["total"] > 0 else 0
    if not tested:
        return R(None, "none of the nudged parameter sets could be run")
    m = {"tested": tested, "still_profitable": good}
    if good / tested >= float(cfg["sens_min_share"]):
        return R(True, "", m)
    return R(False, f"nudging the parameters by +-{int(pct * 100)}% leaves only {good} of {tested} variants profitable after costs: the result depends on exact parameter values (overfit)", m)


# ------------------------------------------------------------------------------------------------------------- the audit as a pair
ORDER = [("data", g_data), ("load", g_load), ("leak", g_leak), ("costs", g_costs), ("oos", g_oos), ("trials", g_trials), ("sensitivity", g_sensitivity)]
SEVERITY = {"data": "high", "load": "high", "leak": "high", "costs": "medium", "oos": "high", "trials": "medium", "sensitivity": "medium"}


def _audit(e, ctx, step):
    """Run every gate once. -> list of {gate, ok, text, metrics}; later gates are skipped (None) when the strategy does not even load."""
    rows, blocked = [], None
    for name, fn in ORDER:
        if blocked and name not in ("data", "load"):
            rows.append({"gate": name, "ok": None, "text": "not run: " + blocked, "metrics": {}})
            continue
        try:
            r = fn(e, ctx, step, [])
        except Exception as ex:                       # a crashing check proves nothing
            r = R(None, f"crashed: {type(ex).__name__}: {str(ex)[:160]}")
        rows.append({"gate": name, "ok": r["ok"], "text": r["text"], "metrics": r["metrics"]})
        if name == "load" and r["ok"] is not True:
            blocked = r["text"] or "the strategy could not be loaded"
    return rows


DEFECTS = ("load", "leak")        # faults in the strategy's code, which a repair can address; the other gates are verdicts about the idea itself


def _publish(ctx, rel, rows):
    """The text the flows' prompts and reports use: bt_findings (everything that did not pass), findings_text (code defects only: all a repair is shown), bt_table."""
    bad = [r for r in rows if r["ok"] is False]
    ctx["bt_findings"] = "\n".join(_fmt(rel, r) for r in bad) or "(nothing failed)"
    ctx["findings_text"] = "\n".join(_fmt(rel, r) for r in bad if r["gate"] in DEFECTS) or "(no defect in the code)"
    ctx["bt_table"] = "\n".join(f"| {r['gate']} | {_word(r['ok'], r)} | {(r['text'] or '').replace('|', '/').replace(chr(10), ' ')[:220]} |" for r in rows)


def _word(ok, row):
    if ok is None:
        return "could not check"
    if ok and (row["metrics"].get("tested") == 0):
        return "not applicable"
    return "passed" if ok else "did not pass"


def _fmt(rel, row):
    return f"- {rel} [{SEVERITY.get(row['gate'], 'medium')}] {row['gate']}: {row['text']}"


def _activity(e, ctx, cfg, rel):
    """Trades the strategy makes on the data (real, else synthetic): the measure a fix must not throw away."""
    bars, tag, _d = _usable_bars(e, ctx, cfg)
    sig, bad = _full(e, ctx, cfg, rel, bars, tag)
    if bad:
        return 0
    return mk.stats(mk.run(bars, sig, cfg["fee_bps"], cfg["slippage_bps"]), cfg["periods_per_year"])["trades"]


def g_scan(e, ctx, step, changed):
    cfg = _cfg(e, step)
    rel = _strategy(e, ctx)
    if not rel:
        return R(None, "no strategy file found: name it in the goal, or keep one file that defines signals(bars)")
    rows = _audit(e, ctx, step)
    bad = [r for r in rows if r["ok"] is False]
    unk = [r for r in rows if r["ok"] is None]
    d = _data(e, ctx)
    defects = [r for r in bad if r["gate"] in DEFECTS]
    ctx.setdefault("scan_rec", {})["bt"] = {"rows": rows, "findings": len(bad), "defects": len(defects), "trades": _activity(e, ctx, cfg, rel), "rel": rel}
    ctx["scope"] = [rel]
    ctx["bt_protect"] = [d["rel"]] if d.get("rel") else []
    ctx.setdefault("explanation", "(not written: the explanation step is switched off)")
    _publish(ctx, rel, rows)
    e.say(f"[audit] {len(bad)} did not pass ({len(defects)} in the code), {len(unk)} could not be checked, {sum(1 for r in rows if r['ok'])} passed")
    e.report(f"- Strategy audited: {rel}; did not pass: {len(bad)}; could not check: {len(unk)}", *[("  " + _fmt(rel, r)) for r in bad], "")
    return R(True, "", {"findings": len(bad), "defects": len(defects), "unknown": len(unk), "ok_count": sum(1 for r in rows if r["ok"]), "files": 1}, ctx["bt_findings"])


def g_fewer(e, ctx, step, changed):
    rec = (ctx.get("scan_rec") or {}).get("bt")
    if not rec:
        return R(None, "no audit was recorded before this step")
    if not changed:
        return R(False, "nothing was applied")
    cfg = _cfg(e, step)
    other = [c for c in changed if c.replace("\\", "/") != rec["rel"]]
    if other:
        return R(False, "only the strategy file may change in this flow (the data, tests and settings are the evidence): " + ", ".join(other[:4]))
    rows = _audit(e, ctx, step)
    bad = [r for r in rows if r["ok"] is False]
    defects = [r for r in bad if r["gate"] in DEFECTS]
    rec["after"] = rows
    gates = {r["gate"]: r["ok"] for r in rows}
    if gates.get("load") is None:
        return R(None, "the audit could not run after the change: " + next(r["text"] for r in rows if r["gate"] == "load"))
    trades = _activity(e, ctx, cfg, rec["rel"])
    why = ""
    leak_before = next((r["ok"] for r in rec["rows"] if r["gate"] == "leak"), None)
    if gates.get("load") is False:
        why = "the strategy no longer loads: " + next(r["text"] for r in rows if r["gate"] == "load")
    elif gates.get("leak") is False and leak_before is not False:
        why = "the change introduced look-ahead: " + next(r["text"] for r in rows if r["gate"] == "leak")
    elif rec["trades"] > 0 and trades < 0.5 * rec["trades"]:
        why = f"the fix made the strategy stop trading ({trades} trades, was {rec['trades']}); removing the signal is not a fix"
    elif len(defects) >= rec["defects"]:
        why = f"{len(defects)} defect(s) in the code still remain (was {rec['defects']}): " + "; ".join(r["text"][:200] for r in defects)
    # Only code defects (the file loads, no look-ahead) are what a fix is judged on. Costs, out-of-sample and trials are verdicts about the idea:
    # tuning the code until they pass would be fitting the audit, so they are reported after the fix, never used as its target.
    _publish(ctx, rec["rel"], rows)
    return R(not why, why, {"before_defects": rec["defects"], "after_defects": len(defects), "after": len(bad), "unknown_after": sum(1 for r in rows if r["ok"] is None),
                            "trades_before": rec["trades"], "trades_after": trades}, ctx["findings_text"])


GATES["bt_data"] = g_data
GATES["bt_load"] = g_load
GATES["bt_leak"] = g_leak
GATES["bt_costs"] = g_costs
GATES["bt_oos"] = g_oos
GATES["bt_trials"] = g_trials
GATES["bt_sensitivity"] = g_sensitivity
GATES["bt_scan"] = g_scan
GATES["bt_fewer"] = g_fewer
