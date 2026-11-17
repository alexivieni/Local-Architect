"""Markets pack: price data in, with the checks a backtest needs before it can mean anything.

A bar is a dict {"t", "open", "high", "low", "close", "volume"} (t is the timestamp text exactly as the file had it). Everything here is
plain standard library and deterministic: `check_bars` says what is wrong with a file as a list of facts, never an opinion.

No network. A dataset arrives as a CSV the person supplies; fetching is a later, online-gated step (CLAUDE.md, docs/claude/markets.md).
"""
import csv
import hashlib
import math
import os
import random
from datetime import datetime, timezone

MIN_BARS = 200
_ALIASES = {"t": ("timestamp", "time", "date", "datetime", "t", "ts"), "open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"),
            "close": ("close", "c", "adj close", "adj_close"), "volume": ("volume", "vol", "v")}


def _col(header, key):
    names = [h.strip().lower() for h in header]
    for a in _ALIASES[key]:
        if a in names:
            return names.index(a)
    return None


def load_bars(path, max_rows=2_000_000):
    """-> (bars, problems). `problems` are facts about the file as a whole (missing column, unreadable rows); a bad row is skipped and counted."""
    problems, bars = [], []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            if not header:
                return [], ["the file is empty"]
            idx = {k: _col(header, k) for k in _ALIASES}
            missing = [k for k in ("t", "open", "high", "low", "close") if idx[k] is None]
            if missing:
                return [], [f"missing column(s): {', '.join(missing)} (found: {', '.join(h.strip() for h in header)[:120]})"]
            bad = 0
            for row in rd:
                if len(bars) >= max_rows:
                    problems.append(f"only the first {max_rows} rows were read")
                    break
                try:
                    vol = float(row[idx["volume"]]) if idx["volume"] is not None and row[idx["volume"]].strip() else 0.0
                    bars.append({"t": row[idx["t"]].strip(), "open": float(row[idx["open"]]), "high": float(row[idx["high"]]),
                                 "low": float(row[idx["low"]]), "close": float(row[idx["close"]]), "volume": vol})
                except (ValueError, IndexError):
                    bad += 1
            if bad:
                problems.append(f"{bad} row(s) could not be read as numbers and were skipped")
    except (OSError, UnicodeDecodeError, csv.Error) as ex:
        return [], [f"could not read the file: {type(ex).__name__}: {ex}"]
    return bars, problems


def ts_seconds(text):
    """Epoch seconds from a timestamp as written in a file (epoch s or ms, or ISO 8601; no zone means UTC). None when unreadable."""
    s = str(text).strip()
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def check_bars(bars):
    """-> list of (severity, text); severity 'error' means a backtest on this file would be meaningless, 'warn' means look at it."""
    out = []
    n = len(bars)
    if n < MIN_BARS:
        out.append(("error", f"only {n} bar(s); a backtest needs at least {MIN_BARS}"))
        return out
    badp = sum(1 for b in bars if not all(math.isfinite(b[k]) and b[k] > 0 for k in ("open", "high", "low", "close")))
    if badp:
        out.append(("error", f"{badp} bar(s) with a price that is zero, negative or not a number"))
    inv = sum(1 for b in bars if b["high"] < b["low"])
    if inv:
        out.append(("error", f"{inv} bar(s) where high is below low"))
    outside = sum(1 for b in bars if b["high"] >= b["low"] and any(not (b["low"] - 1e-9 * abs(b["low"]) <= b[k] <= b["high"] + 1e-9 * abs(b["high"])) for k in ("open", "close")))
    if outside:
        out.append(("error", f"{outside} bar(s) where the open or close lies outside the bar's own high-low range"))
    secs = [ts_seconds(b["t"]) for b in bars]
    if any(s is None for s in secs):
        out.append(("warn", "some timestamps could not be read; the ordering and gap checks were skipped"))
    else:
        back = sum(1 for a, b in zip(secs, secs[1:]) if b < a)
        dup = sum(1 for a, b in zip(secs, secs[1:]) if b == a)
        if back or dup:
            out.append(("error", f"timestamps are not strictly increasing: {back} out of order, {dup} repeated"))
        else:
            steps = sorted(b - a for a, b in zip(secs, secs[1:]))
            med = steps[len(steps) // 2]
            gaps = sum(1 for s in steps if med > 0 and s > 5 * med)
            if gaps:
                out.append(("warn", f"{gaps} gap(s) longer than 5x the usual spacing (weekends and holidays are normal; missing data is not)"))
    zero = sum(1 for b in bars if b["volume"] <= 0)
    if zero > n // 2:
        out.append(("warn", f"{zero} of {n} bar(s) have no volume"))
    return out


def synthetic_bars(n=2000, seed=7, start=1_700_000_000, step=3600):
    """A seeded random walk with gaps and wicks. Only for tests that need *a* series (the look-ahead check does not care what the prices are);
    any result about profit on it means nothing and is never reported as such."""
    rnd = random.Random(seed)
    px, bars = 100.0, []
    for i in range(n):
        o = px * (1.0 + rnd.gauss(0, 0.0015))
        c = o * (1.0 + rnd.gauss(0, 0.004))
        hi = max(o, c) * (1.0 + abs(rnd.gauss(0, 0.002)))
        lo = min(o, c) * (1.0 - abs(rnd.gauss(0, 0.002)))
        bars.append({"t": str(start + i * step), "open": round(o, 6), "high": round(hi, 6), "low": round(lo, 6), "close": round(c, 6),
                     "volume": round(1000 + abs(rnd.gauss(0, 300)), 3)})
        px = c
    return bars


def fingerprint(path):
    """sha256 of the file's bytes, for the trial ledger (the same data counts as the same data)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()[:16]
