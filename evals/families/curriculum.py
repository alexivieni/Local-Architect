"""curriculum: training tasks aimed at the SHAPES the worker fails (arch_curriculum reads the triage and weights them).

Live bench triage (153 tasks): wiring 26, behaviour 43, tests 8, environment 7; the interface_contract / contract_smoke gates fail most.
So these tasks drill: package wiring (__init__ exports, __main__ entry points), contract-first functions (exact signatures and errors),
small CLIs and HTTP handlers judged by hidden tests that drive them like a user, edge-case behaviour, and fixing code against honest tests.

Nothing here is copied from a bench task: every template is new (ids `train-cur-<template>-s<seed>-<k>`), every variant draws its names and
numbers from a seeded generator (a fresh seed gives fresh variants), and the held-out templates are held out WHOLE (tag `holdout`, bench
bucket): a template the adapter trained on is never measured. Each task carries a reference solution (the hidden judge is proven
satisfiable) and a plausible wrong one (the judge is proven to catch it). Standard library only, deterministic, no network.

    python evals/families/curriculum.py --check [--quick]   # prove every task
    python evals/families/curriculum.py --list
"""
import hashlib
import os
import random
import sys

FLOW = "build"
DEFAULT_N = 1
PREFIX = "train-cur"
TAG = "curriculum"
HOLDOUT_TEMPLATES = ("edge_intervals", "cli_csvcol", "class_ledger", "contract_size-fix")   # whole templates, never trained on
LESSONS = ("wiring", "contract_first", "repair_focus", "edge_cases", "tests_present")
PER_TEMPLATE = 6                # variants of each template in all_tasks()


def _sub(s, d):
    for k, v in d.items():
        s = s.replace("@" + k + "@", str(v))
    return s


ADJ = ["amber", "brisk", "cobalt", "dusky", "ember", "fable", "gilded", "hazel", "ivory", "jade", "kelp", "lunar", "mossy", "nimbus",
       "opal", "pearl", "quartz", "russet", "sable", "tidal", "umber", "velvet", "willow", "zephyr"]
NOUN = ["relay", "vault", "pager", "beacon", "scribe", "harbor", "compass", "lantern", "anvil", "bobbin", "cairn", "drover", "easel",
        "ferry", "gantry", "hinge", "ingot", "jetty", "kiln", "loom"]


def _pkg(r):
    return r.choice(ADJ) + r.choice(NOUN)


def _head(imports):
    return "import unittest\n" + imports + "\n\n\n"


# ------------------------------------------------------------------ 1. package wiring: __init__ exports
def t_pkgexport(r):
    pkg = _pkg(r)
    m1, m2 = r.sample(["meter", "rules", "engine", "model", "kit", "parts"], 2)
    cls = r.choice(["Meter", "Gauge", "Ticker", "Stepper", "Dial"])
    meth = r.choice(["bump", "tick", "advance", "step_up"])
    fn = r.choice(["count_over", "above", "tally_above", "exceeding"])
    step, thr = r.randint(2, 9), r.randint(3, 20)
    ver = f"{r.randint(0, 2)}.{r.randint(1, 9)}.{r.randint(0, 9)}"
    d = dict(pkg=pkg, m1=m1, m2=m2, cls=cls, meth=meth, fn=fn, step=step, thr=thr, ver=ver)
    goal = _sub("Build a Python package `@pkg@` (a folder @pkg@/ with __init__.py) with tests in tests/. It has two modules: `@pkg@/@m1@.py` defines the class "
                "`@cls@` and `@pkg@/@m2@.py` defines the function `@fn@`. `@cls@(start=0)` keeps `.value`; `.@meth@()` adds @step@ to it and returns the new "
                "value. `@fn@(values)` returns how many of the numbers in `values` are strictly greater than @thr@. The package must re-export both with "
                "relative imports, so `from @pkg@ import @cls@, @fn@` works, and define `__version__ = \"@ver@\"` and `__all__ = [\"@cls@\", \"@fn@\"]` in "
                "__init__.py. Standard library only.", d)
    hidden = _sub('''
import importlib
import @pkg@
from @pkg@ import @cls@, @fn@
assert @pkg@.__all__ == ["@cls@", "@fn@"], @pkg@.__all__
assert @pkg@.__version__ == "@ver@", @pkg@.__version__
assert importlib.import_module("@pkg@.@m1@").@cls@ is @cls@
assert importlib.import_module("@pkg@.@m2@").@fn@ is @fn@
c = @cls@()
assert c.value == 0 and c.@meth@() == @step@ and c.@meth@() == 2 * @step@ and c.value == 2 * @step@
assert @cls@(10).@meth@() == 10 + @step@
assert @fn@([]) == 0 and @fn@([@thr@, @thr@ + 1, @thr@ + 5, 0]) == 2
print("ok")
''', d)
    sol = {
        f"{pkg}/__init__.py": _sub('"""@pkg@."""\nfrom .@m1@ import @cls@\nfrom .@m2@ import @fn@\n\n__version__ = "@ver@"\n__all__ = ["@cls@", "@fn@"]\n', d),
        f"{pkg}/{m1}.py": _sub('"""The counter."""\n\n\nclass @cls@:\n    def __init__(self, start=0):\n        self.value = start\n\n    def @meth@(self):\n'
                                '        self.value += @step@\n        return self.value\n', d),
        f"{pkg}/{m2}.py": _sub('"""Counting helper."""\n\n\ndef @fn@(values):\n    return sum(1 for v in values if v > @thr@)\n', d),
        f"tests/test_{pkg}.py": _head(_sub("from @pkg@ import @cls@, @fn@", d)) + _sub(
            "class T(unittest.TestCase):\n    def test_counter(self):\n        self.assertEqual(@cls@().@meth@(), @step@)\n\n"
            "    def test_count(self):\n        self.assertEqual(@fn@([@thr@, @thr@ + 1]), 1)\n", d)}
    wrong = dict(sol)
    wrong[f"{pkg}/__init__.py"] = _sub('"""@pkg@."""\nfrom .@m1@ import @cls@\n\n__version__ = "@ver@"\n', d)       # forgets to export the function
    return dict(goal=goal, hidden=hidden, sol=sol, wrong=wrong, must=[f"{pkg}/__init__.py", f"{pkg}/{m1}.py", f"{pkg}/{m2}.py"], lesson="wiring")


# ------------------------------------------------------------------ 2. package wiring: python -m
def t_pkgmain(r):
    pkg = _pkg(r)
    verb, expr = r.choice([("shout", "text.upper()"), ("flip", "text[::-1]"), ("headline", "text.title()"), ("swap", "text.swapcase()")])
    d = dict(pkg=pkg, verb=verb, expr=expr)
    goal = _sub("Build a Python package `@pkg@` with tests in tests/. `@pkg@/cli.py` defines `main(argv=None)` (argparse; `argv` is the list of arguments "
                "after the program name, default sys.argv[1:]): it takes one positional TEXT and an optional `--times N` (default 1, must be at least 1), "
                "prints @verb@(TEXT) -- the text with `@expr@` applied -- once per time on its own line, and RETURNS the exit code 0. A missing TEXT or "
                "`--times 0` is an argparse usage error (exit code 2). `@pkg@/__main__.py` makes `python -m @pkg@ TEXT` work and exits with main()'s "
                "return value. Standard library only.", d)
    hidden = _sub('''
import contextlib, io, subprocess, sys
from @pkg@.cli import main
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = main(["Hello Wide", "--times", "2"])
t = (lambda text: @expr@)("Hello Wide")
assert rc == 0, rc
assert buf.getvalue() == t + "\\n" + t + "\\n", repr(buf.getvalue())
for bad in ([], ["x", "--times", "0"]):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            main(bad)
        raise SystemExit("bad arguments were accepted: %r" % (bad,))
    except SystemExit as e:
        assert e.code == 2, (bad, e.code)
p = subprocess.run([sys.executable, "-m", "@pkg@", "Hello Wide"], capture_output=True, text=True, timeout=30)
assert p.returncode == 0 and p.stdout == t + "\\n", (p.returncode, p.stdout, p.stderr[-200:])
p = subprocess.run([sys.executable, "-m", "@pkg@"], capture_output=True, text=True, timeout=30)
assert p.returncode == 2, p.returncode
print("ok")
''', d)
    sol = {
        f"{pkg}/__init__.py": f'"""{pkg}."""\n',
        f"{pkg}/cli.py": _sub('''"""The command line."""
import argparse


def transform(text):
    return @expr@


def main(argv=None):
    p = argparse.ArgumentParser(prog="@pkg@")
    p.add_argument("text")
    p.add_argument("--times", type=int, default=1)
    a = p.parse_args(argv)
    if a.times < 1:
        p.error("--times must be at least 1")
    for _ in range(a.times):
        print(transform(a.text))
    return 0
''', d),
        f"{pkg}/__main__.py": _sub('"""python -m @pkg@"""\nimport sys\n\nfrom .cli import main\n\nsys.exit(main())\n', d),
        f"tests/test_{pkg}.py": _head(_sub("import contextlib\nimport io\n\nfrom @pkg@.cli import main", d)) + _sub(
            "class T(unittest.TestCase):\n    def test_exit_code(self):\n        with contextlib.redirect_stdout(io.StringIO()):\n"
            "            self.assertEqual(main([\"ab\"]), 0)\n\n    def test_times(self):\n        buf = io.StringIO()\n"
            "        with contextlib.redirect_stdout(buf):\n            main([\"ab\", \"--times\", \"3\"])\n        self.assertEqual(len(buf.getvalue().splitlines()), 3)\n", d)}
    wrong = dict(sol)
    wrong[f"{pkg}/cli.py"] = sol[f"{pkg}/cli.py"].replace("    return 0\n", "")     # main() forgets to return its exit code
    return dict(goal=goal, hidden=hidden, sol=sol, wrong=wrong, must=[f"{pkg}/cli.py", f"{pkg}/__main__.py"], lesson="wiring")


# ------------------------------------------------------------------ 3. contract-first functions
def t_contract_duration(r):
    mod = r.choice(["spans", "durations", "timeparse", "lengths"])
    fn = r.choice(["parse_duration", "to_seconds", "read_span", "seconds_of"])
    units = r.choice([{"w": 604800, "d": 86400, "h": 3600}, {"h": 3600, "m": 60, "s": 1}, {"d": 86400, "h": 3600, "m": 60},
                      {"m": 60, "s": 1}, {"d": 86400, "h": 3600, "m": 60, "s": 1}])
    ks = list(units)
    a, b = ks[0], ks[1]
    d = dict(mod=mod, fn=fn, units=repr(units), table=", ".join(f"{k}={v}" for k, v in units.items()), a=a, b=b, ua=units[a], ub=units[b])
    goal = _sub("Write `@mod@.py` with tests in tests/. `@fn@(text)` turns a duration like `1@a@2@b@` into whole seconds (int). Units, largest first: @table@ "
                "(seconds each). Each unit may appear at most once and they must appear in that order; no spaces, no signs, no decimals. `@fn@(\"\")`, "
                "an unknown unit, a bare number such as \"5\", a number with no unit, a repeated unit and an out-of-order unit all raise ValueError; "
                "a non-str argument raises TypeError. Standard library only.", d)
    hidden = _sub('''
from @mod@ import @fn@
assert @fn@("1@a@2@b@") == @ua@ + 2 * @ub@
assert @fn@("30@b@") == 30 * @ub@
assert @fn@("007@b@") == 7 * @ub@
def bad(x, exc=ValueError):
    try:
        @fn@(x)
    except exc:
        return
    raise SystemExit("no %s for %r" % (exc.__name__, x))
for x in ["", "5", "5q", "@a@", "1@b@1@a@", "1@a@1@a@", "1@a@ 2@b@", "-1@a@", "1.5@a@", "1@a@2"]:
    bad(x)
bad(123, TypeError); bad(None, TypeError)
print("ok")
''', d)
    src = _sub('''"""Parse durations like 1@a@2@b@."""
import re

UNITS = @units@
_PART = re.compile(r"(\\d+)([a-z])")


def @fn@(text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text:
        raise ValueError("empty duration")
    order = list(UNITS)
    pos = total = 0
    last = -1
    for m in _PART.finditer(text):
        if m.start() != pos:
            raise ValueError("bad duration: %r" % text)
        number, unit = m.groups()
        if unit not in UNITS:
            raise ValueError("unknown unit %r" % unit)
        i = order.index(unit)
        if i <= last:
            raise ValueError("unit repeated or out of order: %r" % unit)
        last = i
        total += int(number) * UNITS[unit]
        pos = m.end()
    if pos != len(text):
        raise ValueError("bad duration: %r" % text)
    return total
''', d)
    wrong = src.replace("        if i <= last:\n", "        if False:\n")
    tests = _head(_sub("from @mod@ import @fn@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(@fn@(\"2@b@\"), 2 * @ub@)\n\n"
        "    def test_bad(self):\n        with self.assertRaises(ValueError):\n            @fn@(\"\")\n\n"
        "    def test_repeat(self):\n        with self.assertRaises(ValueError):\n            @fn@(\"1@a@1@a@\")\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="contract_first", mod=mod, fix_ok=True)


def t_contract_size(r):
    mod = r.choice(["sizes", "humanize", "bytefmt", "filesize"])
    fn = r.choice(["format_size", "human_size", "show_bytes", "size_text"])
    base, suf = r.choice([(1000, ["B", "KB", "MB", "GB", "TB"]), (1024, ["B", "KiB", "MiB", "GiB"]), (1024, ["o", "Ko", "Mo", "Go"]),
                          (1000, ["b", "kb", "mb", "gb"])])
    d = dict(mod=mod, fn=fn, base=base, suf=repr(suf), s0=suf[0], s1=suf[1], s2=suf[2], last=suf[-1], suffixes=", ".join(suf), n=len(suf))
    goal = _sub("Write `@mod@.py` with tests in tests/. `@fn@(num_bytes, *, precision=1)` returns a short text for a size. Steps of @base@; suffixes in order: "
                "@suffixes@. A size below @base@ is written as the plain integer and the first suffix (`@fn@(0)` is \"0 @s0@\"). From @base@ up, divide by @base@ "
                "until the value is below @base@ or the last suffix is reached, and write the value with `precision` decimals then a space and the suffix "
                "(`@fn@(@base@)` is \"1.0 @s1@\"). Exactly @base@ steps up (>=, not >). A negative size or negative precision raises ValueError; a bool or "
                "non-int size raises TypeError. Standard library only.", d)
    hidden = _sub('''
from @mod@ import @fn@
B = @base@
assert @fn@(0) == "0 @s0@" and @fn@(B - 1) == "%d @s0@" % (B - 1)
assert @fn@(B) == "1.0 @s1@", @fn@(B)
assert @fn@(B * B * 3 // 2) == "1.5 @s2@", @fn@(B * B * 3 // 2)
assert @fn@(B * B * 3 // 2, precision=3) == "1.500 @s2@"
assert @fn@(B * B, precision=0) == "1 @s2@"
assert @fn@(B ** @n@ * 5).endswith(" @last@")
def bad(args, exc):
    try:
        @fn@(*args)
    except exc:
        return
    raise SystemExit("no %s for %r" % (exc.__name__, args))
bad((-1,), ValueError)
try:
    @fn@(5, precision=-1); raise SystemExit("negative precision accepted")
except ValueError:
    pass
bad((True,), TypeError); bad((1.5,), TypeError); bad(("3",), TypeError)
print("ok")
''', d)
    src = _sub('''"""Human-readable sizes."""
BASE = @base@
SUFFIXES = @suf@


def @fn@(num_bytes, *, precision=1):
    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an int")
    if num_bytes < 0 or precision < 0:
        raise ValueError("negative size or precision")
    value, i = float(num_bytes), 0
    while value >= BASE and i < len(SUFFIXES) - 1:
        value /= BASE
        i += 1
    if i == 0:
        return "%d %s" % (num_bytes, SUFFIXES[0])
    return "%.*f %s" % (precision, value, SUFFIXES[i])
''', d)
    wrong = src.replace("while value >= BASE", "while value > BASE")
    tests = _head(_sub("from @mod@ import @fn@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_small(self):\n        self.assertEqual(@fn@(5), \"5 @s0@\")\n\n"
        "    def test_step(self):\n        self.assertEqual(@fn@(@base@), \"1.0 @s1@\")\n\n"
        "    def test_negative(self):\n        with self.assertRaises(ValueError):\n            @fn@(-1)\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="contract_first", mod=mod, fix_ok=True)


def t_contract_kv(r):
    mod = r.choice(["pairs", "kvtext", "settings_text", "fieldparse"])
    fn = r.choice(["parse_pairs", "read_fields", "split_pairs", "kv_dict"])
    sep, asg = r.choice([(";", "="), ("|", "="), (",", ":"), (";", ":"), ("&", "=")])
    d = dict(mod=mod, fn=fn, sep=sep, asg=asg)
    goal = _sub("Write `@mod@.py` with tests in tests/. `@fn@(text, *, sep=\"@sep@\", assign=\"@asg@\")` parses `a@asg@1@sep@b@asg@2` into {\"a\": \"1\", \"b\": \"2\"} "
                "(str to str). Whitespace around keys and values is stripped. Empty or whitespace-only text gives {}; empty segments (a trailing separator) are "
                "ignored. Only the FIRST assign character splits a segment, so values may contain it. A segment with no assign character raises ValueError whose "
                "message contains the segment; an empty key raises ValueError; a key that appears twice raises ValueError whose message contains the key. "
                "Standard library only.", d)
    hidden = _sub('''
from @mod@ import @fn@
S, A = "@sep@", "@asg@"
assert @fn@("a" + A + "1" + S + " b " + A + " 2 ") == {"a": "1", "b": "2"}
assert @fn@("") == {} and @fn@("  ") == {} and @fn@("a" + A + "1" + S) == {"a": "1"}
assert @fn@("u" + A + "x" + A + "y") == {"u": "x" + A + "y"}
assert @fn@("a/1#b/2", sep="#", assign="/") == {"a": "1", "b": "2"}
for text, needle in [("a" + A + "1" + S + "oops", "oops"), ("a" + A + "1" + S + "k" + A + "2" + S + "k" + A + "3", "k"), (A + "5", None)]:
    try:
        @fn@(text)
    except ValueError as e:
        assert needle is None or needle in str(e), (text, str(e))
    else:
        raise SystemExit("accepted %r" % text)
print("ok")
''', d)
    src = _sub('''"""Parse key/value text."""


def @fn@(text, *, sep="@sep@", assign="@asg@"):
    out = {}
    for seg in text.split(sep):
        if not seg.strip():
            continue
        if assign not in seg:
            raise ValueError("no %r in segment %r" % (assign, seg.strip()))
        key, value = (x.strip() for x in seg.split(assign, 1))
        if not key:
            raise ValueError("empty key in %r" % seg.strip())
        if key in out:
            raise ValueError("duplicate key %r" % key)
        out[key] = value
    return out
''', d)
    wrong = src.replace('        if key in out:\n            raise ValueError("duplicate key %r" % key)\n', "")
    tests = _head(_sub("from @mod@ import @fn@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_basic(self):\n        self.assertEqual(@fn@(\"a@asg@1@sep@b@asg@2\"), {\"a\": \"1\", \"b\": \"2\"})\n\n"
        "    def test_missing_assign(self):\n        with self.assertRaises(ValueError):\n            @fn@(\"nope\")\n\n"
        "    def test_duplicate(self):\n        with self.assertRaises(ValueError):\n            @fn@(\"a@asg@1@sep@a@asg@2\")\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="contract_first", mod=mod, fix_ok=True)


# ------------------------------------------------------------------ 4. edge-case behaviour
def t_edge_windows(r):
    mod = r.choice(["windows", "slices", "framing", "spans_of"])
    fn = r.choice(["windows", "frames", "sliding", "slabs"])
    dflt = r.choice(["1", "size"])
    d = dict(mod=mod, fn=fn, dflt=dflt)
    goal = _sub("Write `@mod@.py` with tests in tests/. `@fn@(seq, size, step=None)` returns a list of windows over any sequence (list, tuple or str): "
                "`seq[i:i+size]` for i = 0, step, 2*step... Only COMPLETE windows count (a trailing shorter one is dropped), so each window has exactly `size` items "
                "and keeps the sequence's type (a str gives str windows). `step=None` means @dflt@. A `size` or `step` below 1 raises ValueError; a size longer "
                "than the sequence gives []. The input is never modified. Standard library only.", d)
    hidden = _sub('''
from @mod@ import @fn@
D = @dflt@ if "@dflt@" == "1" else None
a = list(range(7))
assert @fn@(a, 3, 2) == [[0, 1, 2], [2, 3, 4], [4, 5, 6]]
assert @fn@(a, 3, 3) == [[0, 1, 2], [3, 4, 5]]
assert @fn@("abcde", 2, 2) == ["ab", "cd"] and @fn@((1, 2, 3), 2, 1) == [(1, 2), (2, 3)]
assert @fn@(a, 8) == [] and @fn@([], 1) == [] and @fn@(a, 7, 5) == [a]
assert @fn@("abcd", 2) == (["ab", "bc", "cd"] if D == 1 else ["ab", "cd"]), @fn@("abcd", 2)
assert a == list(range(7))
for args in [(a, 0), (a, -1), (a, 2, 0), (a, 2, -3)]:
    try:
        @fn@(*args); raise SystemExit("accepted %r" % (args,))
    except ValueError:
        pass
print("ok")
''', d)
    src = _sub('''"""Complete sliding windows."""


def @fn@(seq, size, step=None):
    if step is None:
        step = @dflt@
    if size < 1 or step < 1:
        raise ValueError("size and step must be at least 1")
    return [seq[i:i + size] for i in range(0, len(seq) - size + 1, step)]
''', d)
    wrong = src.replace("range(0, len(seq) - size + 1, step)", "range(0, len(seq), step)")
    tests = _head(_sub("from @mod@ import @fn@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_overlap(self):\n        self.assertEqual(@fn@([1, 2, 3, 4], 2, 1), [[1, 2], [2, 3], [3, 4]])\n\n"
        "    def test_too_big(self):\n        self.assertEqual(@fn@([1], 2, 1), [])\n\n"
        "    def test_partial_dropped(self):\n        self.assertEqual(@fn@(\"abcde\", 2, 2), [\"ab\", \"cd\"])\n\n"
        "    def test_bad_size(self):\n        with self.assertRaises(ValueError):\n            @fn@([1], 0)\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="edge_cases", mod=mod, fix_ok=True)


def t_edge_intervals(r):
    mod = r.choice(["ranges", "spanmerge", "blocks"])
    fn = r.choice(["merge", "union", "coalesce"])
    touch = r.choice([True, False])
    d = dict(mod=mod, fn=fn, touch=touch, word="merged into one" if touch else "kept separate")
    goal = _sub("Write `@mod@.py` with tests in tests/. `@fn@(intervals)` takes a list of (start, end) number pairs in any order and returns the merged list of "
                "(start, end) TUPLES sorted by start. Overlapping intervals are merged, an interval inside another is absorbed, and intervals that merely touch "
                "(one ends exactly where the next starts) are @word@. An empty list gives []. A pair with start > end raises ValueError. The input list is "
                "never modified. Standard library only.", d)
    hidden = _sub('''
from @mod@ import @fn@
data = [(5, 8), (1, 3), (2, 4), (6, 7), (10, 12)]
before = list(data)
assert @fn@(data) == [(1, 4), (5, 8), (10, 12)], @fn@(data)
assert data == before
assert @fn@([]) == [] and @fn@([[1, 2]]) == [(1, 2)]
assert @fn@([(3, 3), (1, 5)]) == [(1, 5)]
assert @fn@([(1, 2), (2, 3)]) == ([(1, 3)] if @touch@ else [(1, 2), (2, 3)])
try:
    @fn@([(4, 1)]); raise SystemExit("accepted start > end")
except ValueError:
    pass
print("ok")
''', d)
    src = _sub('''"""Merge intervals."""


def @fn@(intervals):
    items = [tuple(p) for p in intervals]
    for a, b in items:
        if a > b:
            raise ValueError("start after end: %r" % ((a, b),))
    out = []
    for a, b in sorted(items):
        if out and (a <= out[-1][1] if @touch@ else a < out[-1][1]):
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
''', d)
    wrong = src.replace("    for a, b in sorted(items):", "    for a, b in items:")
    tests = _head(_sub("from @mod@ import @fn@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_overlap(self):\n        self.assertEqual(@fn@([(1, 3), (2, 4)]), [(1, 4)])\n\n"
        "    def test_empty(self):\n        self.assertEqual(@fn@([]), [])\n\n"
        "    def test_bad(self):\n        with self.assertRaises(ValueError):\n            @fn@([(2, 1)])\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="edge_cases", mod=mod)


def t_edge_topk(r):
    mod = r.choice(["leaders", "ranking", "toplist", "podium"])
    fn = r.choice(["top_k", "leaders", "best", "rank_top"])
    asc = r.choice([True, False])
    minc = r.choice([0, 1, 2])
    d = dict(mod=mod, fn=fn, asc=asc, order="ascending" if asc else "descending", minc=minc)
    goal = _sub("Write `@mod@.py` with tests in tests/. `@fn@(counts, k)` takes a dict name -> int count and returns the k best as a list of (name, count) tuples, "
                "highest count first; equal counts are ordered by name @order@. Entries whose count is below @minc@ are left out first. k=0 gives []; k larger than "
                "what is left gives everything left; a negative k raises ValueError; a non-int k (or a bool) raises TypeError. The dict is never modified. "
                "Standard library only.", d)
    hidden = _sub('''
from @mod@ import @fn@
c = {"pear": 3, "apple": 3, "fig": 7, "kiwi": 1, "plum": 0}
keep = {n: v for n, v in c.items() if v >= @minc@}
want = sorted(keep.items(), key=lambda kv: (-kv[1], kv[0]) if @asc@ else (-kv[1], [-ord(ch) for ch in kv[0]]))
assert @fn@(c, 10) == want, (@fn@(c, 10), want)
assert @fn@(c, 2) == want[:2] and @fn@(c, 0) == [] and @fn@({}, 3) == []
assert c == {"pear": 3, "apple": 3, "fig": 7, "kiwi": 1, "plum": 0}
for k, exc in [(-1, ValueError), (1.5, TypeError), (True, TypeError), ("2", TypeError)]:
    try:
        @fn@(c, k); raise SystemExit("accepted k=%r" % (k,))
    except exc:
        pass
print("ok")
''', d)
    src = _sub('''"""The k best entries."""


def @fn@(counts, k):
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError("k must be an int")
    if k < 0:
        raise ValueError("k must not be negative")
    items = [(n, v) for n, v in counts.items() if v >= @minc@]
    items.sort(key=lambda kv: kv[0], reverse=not @asc@)
    items.sort(key=lambda kv: kv[1], reverse=True)
    return items[:k]
''', d)
    wrong = src.replace("reverse=not %s)" % asc, "reverse=%s)" % asc)
    tests = _head(_sub("from @mod@ import @fn@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_order(self):\n        self.assertEqual(@fn@({\"b\": 5, \"a\": 9}, 2)[0], (\"a\", 9))\n\n"
        "    def test_zero(self):\n        self.assertEqual(@fn@({\"a\": 9}, 0), [])\n\n"
        "    def test_negative(self):\n        with self.assertRaises(ValueError):\n            @fn@({}, -1)\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="edge_cases", mod=mod, fix_ok=True)


# ------------------------------------------------------------------ 5. small command-line tools judged by running them
def t_cli_tally(r):
    mod = r.choice(["wordtally", "freqs", "lexcount", "tokenfreq"])
    top, minlen = r.randint(2, 5), r.randint(1, 3)
    sep = r.choice(["\\t", " ", ": "])
    d = dict(mod=mod, top=top, minlen=minlen, sep=sep, sepshow=sep.replace("\\t", "TAB"))
    goal = _sub("Write a command-line tool `@mod@.py` (run as `python @mod@.py [--top N] [--min-len L] [--ignore-case]`) with `main(argv=None)` returning the exit "
                "code, and tests in tests/ (at least three test functions). It reads text from stdin, finds words (runs of ASCII letters), and prints the "
                "most frequent ones, one per line, as `word@sepshow@count`: highest count first, equal counts in alphabetical order. `--top` defaults to @top@ "
                "(must be at least 1), `--min-len` defaults to @minlen@ (words shorter than that are ignored), words are case-sensitive unless "
                "`--ignore-case` (then lower-cased). Invalid options are an argparse usage error (exit code 2). Empty input prints nothing and exits 0. "
                "Standard library only.", d)
    hidden = _sub('''
import subprocess, sys
def run(text, *args):
    return subprocess.run([sys.executable, "@mod@.py", *args], input=text, capture_output=True, text=True, timeout=30)
S = "@sep@".replace("\\\\t", "\\t")
text = "pear apple pear Fig fig fig kiwi apple zed a"
p = run(text, "--top", "50", "--min-len", "1")
assert p.returncode == 0, p.stderr[-300:]
assert p.stdout.splitlines() == ["fig" + S + "2", "apple" + S + "2", "pear" + S + "2", "Fig" + S + "1", "a" + S + "1", "kiwi" + S + "1", "zed" + S + "1"][:0] or True
lines = p.stdout.splitlines()
assert lines[:3] == ["apple" + S + "2", "fig" + S + "2", "pear" + S + "2"], lines
assert lines[3:] == ["Fig" + S + "1", "a" + S + "1", "kiwi" + S + "1", "zed" + S + "1"], lines
p = run(text, "--top", "50", "--min-len", "1", "--ignore-case")
assert p.stdout.splitlines()[:2] == ["fig" + S + "3", "apple" + S + "2"], p.stdout
p = run(text, "--top", "2", "--min-len", "4")
assert p.stdout.splitlines() == ["apple" + S + "2", "pear" + S + "2"], p.stdout
assert run("").stdout == "" and run("").returncode == 0
assert run(text, "--top", "0").returncode == 2 and run(text, "--bogus").returncode == 2
d = run(text)
assert len(d.stdout.splitlines()) <= @top@
print("ok")
''', d)
    src = _sub('''"""Most frequent words from stdin."""
import argparse
import collections
import re
import sys

SEP = "@sep@".replace("\\\\t", "\\t")


def main(argv=None):
    p = argparse.ArgumentParser(prog="@mod@")
    p.add_argument("--top", type=int, default=@top@)
    p.add_argument("--min-len", type=int, default=@minlen@)
    p.add_argument("--ignore-case", action="store_true")
    a = p.parse_args(argv)
    if a.top < 1:
        p.error("--top must be at least 1")
    text = sys.stdin.read()
    if a.ignore_case:
        text = text.lower()
    counts = collections.Counter(w for w in re.findall(r"[A-Za-z]+", text) if len(w) >= a.min_len)
    for word, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:a.top]:
        print("%s%s%d" % (word, SEP, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
''', d)
    tests = _head("import io\nimport sys\nfrom unittest import mock\n\nimport " + mod) + _sub(
        "def _run(text, *args):\n    out = io.StringIO()\n    with mock.patch.object(sys, \"stdin\", io.StringIO(text)), mock.patch.object(sys, \"stdout\", out):\n"
        "        rc = @mod@.main(list(args))\n    return rc, out.getvalue()\n\n\nclass T(unittest.TestCase):\n"
        "    def test_order(self):\n        rc, out = _run(\"b a b\", \"--min-len\", \"1\")\n        self.assertEqual(rc, 0)\n        self.assertTrue(out.startswith(\"b\"))\n\n"
        "    def test_empty(self):\n        self.assertEqual(_run(\"\")[1], \"\")\n\n"
        "    def test_bad_top(self):\n        with self.assertRaises(SystemExit):\n            _run(\"x\", \"--top\", \"0\")\n", d)
    wrong = src.replace("key=lambda kv: (-kv[1], kv[0])", "key=lambda kv: -kv[1]")
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="tests_present", mod=mod, min_tests=3)


def t_cli_csvcol(r):
    mod = r.choice(["colstat", "csvcalc", "columnop", "gridsum"])
    ops = r.sample(["sum", "mean", "max", "min"], 3)
    d = dict(mod=mod, ops=", ".join(ops), default=ops[0], o1=ops[1], o2=ops[2])
    goal = _sub("Write a command-line tool `@mod@.py` (run as `python @mod@.py FILE COLUMN [--op @ops@]`, default `--op @default@`) with `main(argv=None)` returning "
                "the exit code, and tests in tests/. FILE is a CSV with a header row. It prints the operation over the numeric cells of COLUMN with exactly two "
                "decimals (`%.2f`). Blank or non-numeric cells are skipped (never counted as 0) and their number goes to stderr as `skipped N` when N > 0. "
                "An unknown COLUMN prints `no such column: COLUMN` to stderr and exits 1; a missing FILE or a column with no numbers also exits 1 with a "
                "message on stderr. An unknown op is an argparse usage error (exit 2). Standard library only.", d)
    hidden = _sub('''
import os, subprocess, sys, tempfile
p0 = os.path.join(tempfile.mkdtemp(), "t.csv")
with open(p0, "w") as f:
    f.write("name,score,note\\na,3,x\\nb,,y\\nc,10.5,z\\nd,oops,w\\ne,2,v\\n")
def run(*a):
    return subprocess.run([sys.executable, "@mod@.py", *a], capture_output=True, text=True, timeout=30)
vals = [3.0, 10.5, 2.0]
want = {"sum": sum(vals), "mean": sum(vals) / 3, "max": max(vals), "min": min(vals)}
p = run(p0, "score")
assert p.returncode == 0 and p.stdout.strip() == "%.2f" % want["@default@"], (p.stdout, p.stderr)
assert "skipped 2" in p.stderr, p.stderr
for op in ["@default@", "@o1@", "@o2@"]:
    p = run(p0, "score", "--op", op)
    assert p.stdout.strip() == "%.2f" % want[op], (op, p.stdout)
p = run(p0, "nope")
assert p.returncode == 1 and "no such column: nope" in p.stderr, (p.returncode, p.stderr)
assert run(p0, "name").returncode == 1 and run(p0 + ".missing", "score").returncode == 1
assert run(p0, "score", "--op", "median").returncode == 2
print("ok")
''', d)
    src = _sub('''"""One statistic over one CSV column."""
import argparse
import csv
import sys

OPS = {"sum": sum, "mean": lambda v: sum(v) / len(v), "max": max, "min": min}
CHOICES = [@choices@]


def main(argv=None):
    p = argparse.ArgumentParser(prog="@mod@")
    p.add_argument("file")
    p.add_argument("column")
    p.add_argument("--op", choices=CHOICES, default="@default@")
    a = p.parse_args(argv)
    try:
        with open(a.file, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            fields = rows and list(rows[0]) or []
    except OSError as e:
        print("cannot read %s: %s" % (a.file, e), file=sys.stderr)
        return 1
    if a.column not in fields:
        print("no such column: %s" % a.column, file=sys.stderr)
        return 1
    nums, skipped = [], 0
    for row in rows:
        try:
            nums.append(float(row[a.column]))
        except (TypeError, ValueError):
            skipped += 1
    if skipped:
        print("skipped %d" % skipped, file=sys.stderr)
    if not nums:
        print("no numbers in column %s" % a.column, file=sys.stderr)
        return 1
    print("%.2f" % OPS[a.op](nums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
''', dict(d, choices=", ".join(repr(o) for o in ops)))
    tests = _head("import os\nimport tempfile\n\nimport " + mod) + _sub(
        "class T(unittest.TestCase):\n    def _csv(self, text):\n        p = os.path.join(tempfile.mkdtemp(), \"t.csv\")\n        with open(p, \"w\") as f:\n            f.write(text)\n"
        "        return p\n\n    def test_default_op(self):\n        self.assertEqual(@mod@.main([self._csv(\"a\\n1\\n2\\n\"), \"a\"]), 0)\n\n"
        "    def test_unknown_column(self):\n        self.assertEqual(@mod@.main([self._csv(\"a\\n1\\n\"), \"b\"]), 1)\n", d)
    wrong = src.replace("        except (TypeError, ValueError):\n            skipped += 1", "        except (TypeError, ValueError):\n            nums.append(0.0)")
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="contract_first", mod=mod)


# ------------------------------------------------------------------ 6. HTTP handlers judged over a real socket
_HTTP_HELP = '''
import json, threading, urllib.error, urllib.request
from @pkg@ import make_server
srv = make_server(0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
def call(method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        return e.code, (json.loads(txt) if txt else None)
'''


def t_http_counter(r):
    pkg = _pkg(r)
    res = r.choice(["counters", "meters", "tallies", "hits"])
    step = r.randint(2, 7)
    d = dict(pkg=pkg, res=res, step=step)
    goal = _sub("Build a Python package `@pkg@` with tests in tests/. `from @pkg@ import make_server` must work: `make_server(port=0)` returns an "
                "http.server.HTTPServer bound to 127.0.0.1 (port 0 = any free port; read it from `.server_address`), not yet serving. JSON API: "
                "`POST /@res@/<name>` adds @step@ to the named counter (starting at 0) and answers 200 `{\"name\": name, \"value\": value}`; "
                "`GET /@res@/<name>` answers 200 with the same shape, or 404 `{\"error\": \"not found\"}` if it was never posted; `GET /@res@` answers 200 with "
                "an object of every counter `{name: value}`. A <name> is lower-case letters, digits, `_` or `-`; any other path is 404 `{\"error\": \"not found\"}`. "
                "Any other method (PUT, DELETE...) on a known path is 405 `{\"error\": \"method not allowed\"}`. Replies carry Content-Type application/json and a "
                "Content-Length. Standard library only; log nothing to stderr.", d)
    hidden = _sub(_HTTP_HELP + '''
assert call("GET", "/@res@/a") == (404, {"error": "not found"})
assert call("POST", "/@res@/a") == (200, {"name": "a", "value": @step@})
assert call("POST", "/@res@/a") == (200, {"name": "a", "value": 2 * @step@})
assert call("GET", "/@res@/a") == (200, {"name": "a", "value": 2 * @step@})
call("POST", "/@res@/b-2")
assert call("GET", "/@res@") == (200, {"a": 2 * @step@, "b-2": @step@})
assert call("GET", "/@res@/A!") == (404, {"error": "not found"}) and call("GET", "/other")[0] == 404
assert call("DELETE", "/@res@/a") == (405, {"error": "method not allowed"})
assert call("PUT", "/@res@") == (405, {"error": "method not allowed"})
srv.shutdown()
print("ok")
''', d)
    handler = _sub('''"""The counter service."""
import http.server
import json
import re

NAME = re.compile(r"^/@res@/([a-z0-9_-]+)$")


def make_server(port=0):
    counts = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _known(self):
            return self.path == "/@res@" or NAME.match(self.path)

        def _other(self):
            if self._known():
                self._send(405, {"error": "method not allowed"})
            else:
                self._send(404, {"error": "not found"})

        do_PUT = do_DELETE = do_PATCH = _other

        def do_GET(self):
            m = NAME.match(self.path)
            if self.path == "/@res@":
                self._send(200, dict(counts))
            elif m and m.group(1) in counts:
                self._send(200, {"name": m.group(1), "value": counts[m.group(1)]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            m = NAME.match(self.path)
            if not m:
                return self._other()
            counts[m.group(1)] = counts.get(m.group(1), 0) + @step@
            self._send(200, {"name": m.group(1), "value": counts[m.group(1)]})

    return http.server.HTTPServer(("127.0.0.1", port), Handler)
''', d)
    sol = {f"{pkg}/__init__.py": _sub('"""@pkg@."""\nfrom .server import make_server\n\n__all__ = ["make_server"]\n', d), f"{pkg}/server.py": handler,
           f"tests/test_{pkg}.py": _head(_sub("import json\nimport threading\nimport urllib.request\n\nfrom @pkg@ import make_server", d)) + _sub(
               "class T(unittest.TestCase):\n    def test_post_then_get(self):\n        srv = make_server(0)\n        threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
               "        try:\n            url = \"http://127.0.0.1:%d/@res@/x\" % srv.server_address[1]\n"
               "            urllib.request.urlopen(urllib.request.Request(url, method=\"POST\")).read()\n"
               "            self.assertEqual(json.load(urllib.request.urlopen(url))[\"value\"], @step@)\n        finally:\n            srv.shutdown()\n            srv.server_close()\n", d)}
    wrong = dict(sol)
    wrong[f"{pkg}/server.py"] = handler.replace("counts.get(m.group(1), 0) + %d" % step, "counts.get(m.group(1), 0) + 1")
    return dict(goal=goal, hidden=hidden, sol=sol, wrong=wrong, must=[f"{pkg}/__init__.py", f"{pkg}/server.py"], lesson="wiring")


def t_http_kv(r):
    pkg = _pkg(r)
    res = r.choice(["items", "cells", "slots", "entries"])
    d = dict(pkg=pkg, res=res)
    goal = _sub("Build a Python package `@pkg@` with tests in tests/ (at least three test functions). `from @pkg@ import make_server` must work: "
                "`make_server(port=0)` returns an http.server.HTTPServer on 127.0.0.1 (read the port from `.server_address`), not yet serving. A JSON store: "
                "`PUT /@res@/<key>` with body `{\"value\": <any JSON>}` stores it: 201 the first time, 200 when it replaces; both answer "
                "`{\"key\": key, \"value\": value}`. A body that is not JSON or has no \"value\" is 400 `{\"error\": \"bad request\"}`. `GET /@res@/<key>` is 200 "
                "with the same shape or 404 `{\"error\": \"not found\"}`. `DELETE /@res@/<key>` is 204 with no body, or 404. `GET /@res@` is 200 "
                "`{\"keys\": [sorted keys]}`. Keys are lower-case letters, digits, `_`, `-`; any other path is 404. Replies are JSON with Content-Length. "
                "Standard library only; log nothing.", d)
    hidden = _sub(_HTTP_HELP + '''
assert call("GET", "/@res@/k") == (404, {"error": "not found"})
assert call("PUT", "/@res@/k", {"value": [1, {"a": None}]}) == (201, {"key": "k", "value": [1, {"a": None}]})
assert call("PUT", "/@res@/k", {"value": 5}) == (200, {"key": "k", "value": 5})
assert call("PUT", "/@res@/k", {"value": None})[0] == 200
assert call("GET", "/@res@/k") == (200, {"key": "k", "value": None})
assert call("PUT", "/@res@/a_1", {"value": 1})[0] == 201
assert call("GET", "/@res@") == (200, {"keys": ["a_1", "k"]})
assert call("PUT", "/@res@/z", {"nope": 1}) == (400, {"error": "bad request"})
assert call("PUT", "/@res@/z", raw=b"{not json") == (400, {"error": "bad request"})
assert call("DELETE", "/@res@/k") == (204, None)
assert call("DELETE", "/@res@/k")[0] == 404 and call("GET", "/@res@/k")[0] == 404
assert call("GET", "/@res@/BAD!")[0] == 404
srv.shutdown()
print("ok")
''', d)
    handler = _sub('''"""A small JSON key/value service."""
import http.server
import json
import re

KEY = re.compile(r"^/@res@/([a-z0-9_-]+)$")


def make_server(port=0):
    store = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _missing(self):
            self._send(404, {"error": "not found"})

        def do_GET(self):
            m = KEY.match(self.path)
            if self.path == "/@res@":
                self._send(200, {"keys": sorted(store)})
            elif m and m.group(1) in store:
                self._send(200, {"key": m.group(1), "value": store[m.group(1)]})
            else:
                self._missing()

        def do_DELETE(self):
            m = KEY.match(self.path)
            if m and m.group(1) in store:
                del store[m.group(1)]
                self._send(204)
            else:
                self._missing()

        def do_PUT(self):
            m = KEY.match(self.path)
            if not m:
                return self._missing()
            try:
                size = int(self.headers.get("Content-Length") or 0)
                doc = json.loads(self.rfile.read(size).decode("utf-8"))
                value = doc["value"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad request"})
            created = m.group(1) not in store
            store[m.group(1)] = value
            self._send(201 if created else 200, {"key": m.group(1), "value": value})

    return http.server.HTTPServer(("127.0.0.1", port), Handler)
''', d)
    tests = _head(_sub("import json\nimport threading\nimport urllib.error\nimport urllib.request\n\nfrom @pkg@ import make_server", d)) + _sub(
        "class T(unittest.TestCase):\n    def setUp(self):\n        self.srv = make_server(0)\n        threading.Thread(target=self.srv.serve_forever, daemon=True).start()\n"
        "        self.base = \"http://127.0.0.1:%d/@res@\" % self.srv.server_address[1]\n\n    def tearDown(self):\n        self.srv.shutdown()\n        self.srv.server_close()\n\n"
        "    def _put(self, key, body):\n        req = urllib.request.Request(self.base + \"/\" + key, data=json.dumps(body).encode(), method=\"PUT\")\n        return urllib.request.urlopen(req)\n\n"
        "    def test_create_then_replace(self):\n        self.assertEqual(self._put(\"a\", {\"value\": 1}).status, 201)\n        self.assertEqual(self._put(\"a\", {\"value\": 2}).status, 200)\n\n"
        "    def test_get(self):\n        self._put(\"a\", {\"value\": 7})\n        self.assertEqual(json.load(urllib.request.urlopen(self.base + \"/a\"))[\"value\"], 7)\n\n"
        "    def test_bad_body(self):\n        with self.assertRaises(urllib.error.HTTPError) as cm:\n            self._put(\"a\", {\"x\": 1})\n        self.assertEqual(cm.exception.code, 400)\n", d)
    sol = {f"{pkg}/__init__.py": _sub('"""@pkg@."""\nfrom .server import make_server\n\n__all__ = ["make_server"]\n', d), f"{pkg}/server.py": handler,
           f"tests/test_{pkg}.py": tests}
    wrong = dict(sol)
    wrong[f"{pkg}/server.py"] = handler.replace("201 if created else 200", "200")
    return dict(goal=goal, hidden=hidden, sol=sol, wrong=wrong, must=[f"{pkg}/__init__.py", f"{pkg}/server.py"], lesson="tests_present", min_tests=3)


# ------------------------------------------------------------------ 7. classes with an exact contract
def t_class_lru(r):
    mod = r.choice(["recent", "memo_cache", "hotset", "keepers"])
    cls = r.choice(["RecentCache", "LruMap", "HotSet", "Keeper"])
    d = dict(mod=mod, cls=cls)
    goal = _sub("Write `@mod@.py` with tests in tests/ (at least three test functions). `@cls@(capacity, on_evict=None)` is a least-recently-used map. capacity below 1 "
                "raises ValueError. `put(key, value)` stores; when that makes it hold more than `capacity` items the least recently used one is removed and "
                "`on_evict(key, value)` is called (if given). Putting an existing key replaces its value, makes it the most recent and evicts nothing. "
                "`get(key, default=None)` returns the value (or default) and makes a found key the most recent. `key in cache` and `len(cache)` never change the "
                "order. `keys()` lists the keys from least to most recently used. Standard library only.", d)
    hidden = _sub('''
from @mod@ import @cls@
gone = []
c = @cls@(2, on_evict=lambda k, v: gone.append((k, v)))
c.put("a", 1); c.put("b", 2)
assert c.get("a") == 1
c.put("c", 3)
assert gone == [("b", 2)] and c.keys() == ["a", "c"], (gone, c.keys())
assert "a" in c and "b" not in c and len(c) == 2 and c.keys() == ["a", "c"]
c.put("a", 10)
assert gone == [("b", 2)] and c.keys() == ["c", "a"] and c.get("a") == 10
assert c.get("zzz") is None and c.get("zzz", 5) == 5
c.put("d", 4)
assert gone[-1] == ("c", 3), gone
for bad in (0, -3):
    try:
        @cls@(bad); raise SystemExit("accepted capacity %r" % bad)
    except ValueError:
        pass
@cls@(1).put("x", 1)
print("ok")
''', d)
    src = _sub('''"""A least-recently-used map."""
import collections


class @cls@:
    def __init__(self, capacity, on_evict=None):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self.on_evict = on_evict
        self._data = collections.OrderedDict()

    def put(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self.capacity:
            k, v = self._data.popitem(last=False)
            if self.on_evict:
                self.on_evict(k, v)

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def keys(self):
        return list(self._data)

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)
''', d)
    wrong = src.replace("        self._data.move_to_end(key)\n        return self._data[key]", "        return self._data[key]")
    tests = _head(_sub("from @mod@ import @cls@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_evicts_oldest(self):\n        c = @cls@(1)\n        c.put(\"a\", 1)\n        c.put(\"b\", 2)\n        self.assertEqual(c.keys(), [\"b\"])\n\n"
        "    def test_get_refreshes(self):\n        c = @cls@(2)\n        c.put(\"a\", 1)\n        c.put(\"b\", 2)\n        c.get(\"a\")\n        self.assertEqual(c.keys(), [\"b\", \"a\"])\n\n"
        "    def test_bad_capacity(self):\n        with self.assertRaises(ValueError):\n            @cls@(0)\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="tests_present", mod=mod, fix_ok=True, min_tests=3)


def t_class_ledger(r):
    mod = r.choice(["purse", "wallet_ops", "balances", "pocket"])
    cls = r.choice(["Purse", "Wallet", "Account", "Pocket"])
    exc = r.choice(["Insufficient", "TooLittle", "Overdrawn"])
    od = r.choice([0, 0, 50, 100])
    d = dict(mod=mod, cls=cls, exc=exc, od=od)
    goal = _sub("Write `@mod@.py` with tests in tests/. `@cls@(balance=0, *, overdraft=@od@)` holds an int balance. `deposit(amount)` and `withdraw(amount)` need a "
                "positive int (a bool or non-int raises TypeError, zero or negative raises ValueError), change the balance and return the new balance. A withdrawal "
                "that would take the balance below -overdraft raises `@exc@` (a subclass of ValueError that you define in the module) and changes nothing. "
                "`.balance` is a read-only property. `.history` returns a NEW list each time of (\"deposit\"|\"withdraw\", amount) for the successful operations only, "
                "oldest first. Standard library only.", d)
    hidden = _sub('''
from @mod@ import @cls@, @exc@
a = @cls@(100)
assert issubclass(@exc@, ValueError)
assert a.deposit(20) == 120 and a.withdraw(30) == 90 and a.balance == 90
try:
    a.withdraw(90 + @od@ + 1); raise SystemExit("overdrew")
except @exc@:
    pass
assert a.balance == 90
assert a.withdraw(90 + @od@) == -@od@
assert a.history == [("deposit", 20), ("withdraw", 30), ("withdraw", 90 + @od@)], a.history
a.history.append("x"); assert len(a.history) == 3
for bad, exc in [(0, ValueError), (-5, ValueError), (True, TypeError), (1.5, TypeError), ("3", TypeError)]:
    for op in (a.deposit, a.withdraw):
        try:
            op(bad); raise SystemExit("accepted %r" % (bad,))
        except exc:
            pass
try:
    a.balance = 5; raise SystemExit("balance is writable")
except AttributeError:
    pass
assert len(a.history) == 3
print("ok")
''', d)
    src = _sub('''"""A tiny account."""


class @exc@(ValueError):
    pass


class @cls@:
    def __init__(self, balance=0, *, overdraft=@od@):
        self._balance = balance
        self.overdraft = overdraft
        self._history = []

    @property
    def balance(self):
        return self._balance

    @property
    def history(self):
        return list(self._history)

    @staticmethod
    def _check(amount):
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError("amount must be an int")
        if amount <= 0:
            raise ValueError("amount must be positive")

    def deposit(self, amount):
        self._check(amount)
        self._balance += amount
        self._history.append(("deposit", amount))
        return self._balance

    def withdraw(self, amount):
        self._check(amount)
        if self._balance - amount < -self.overdraft:
            raise @exc@("not enough balance for %d" % amount)
        self._balance -= amount
        self._history.append(("withdraw", amount))
        return self._balance
''', d)
    wrong = src.replace("        if self._balance - amount < -self.overdraft:\n            raise %s(\"not enough balance for %%d\" %% amount)\n" % exc,
                        "        self._history.append((\"withdraw\", amount))\n        if self._balance - amount < -self.overdraft:\n            raise %s(\"not enough balance for %%d\" %% amount)\n" % exc
                        ).replace("        self._balance -= amount\n        self._history.append((\"withdraw\", amount))\n", "        self._balance -= amount\n")
    tests = _head(_sub("from @mod@ import @cls@, @exc@", d)) + _sub(
        "class T(unittest.TestCase):\n    def test_deposit(self):\n        self.assertEqual(@cls@(5).deposit(5), 10)\n\n"
        "    def test_overdraw(self):\n        with self.assertRaises(@exc@):\n            @cls@(0, overdraft=0).withdraw(1)\n", d)
    return dict(goal=goal, hidden=hidden, sol={f"{mod}.py": src, f"tests/test_{mod}.py": tests}, wrong={f"{mod}.py": wrong, f"tests/test_{mod}.py": tests},
                must=[f"{mod}.py"], lesson="edge_cases", mod=mod)


BUILDERS = [("pkgexport", t_pkgexport), ("pkgmain", t_pkgmain), ("contract_duration", t_contract_duration), ("contract_size", t_contract_size),
            ("contract_kv", t_contract_kv), ("edge_windows", t_edge_windows), ("edge_intervals", t_edge_intervals), ("edge_topk", t_edge_topk),
            ("cli_tally", t_cli_tally), ("cli_csvcol", t_cli_csvcol), ("http_counter", t_http_counter), ("http_kv", t_http_kv),
            ("class_lru", t_class_lru), ("class_ledger", t_class_ledger)]

# the shapes the triage families name -> which templates drill them (arch_curriculum weights these by the measured failure counts)
SHAPES = {"wiring": ["pkgexport", "pkgmain", "http_counter", "http_kv"],
          "contract": ["contract_duration", "contract_size", "contract_kv", "cli_csvcol", "cli_tally"],
          "behaviour": ["edge_windows", "edge_intervals", "edge_topk", "class_lru", "class_ledger"],
          "tests": ["cli_tally", "http_kv", "class_lru"]}


def templates():
    """Every template key (a build template, or `<key>-fix`: the same code planted with its bug, to be repaired against honest tests)."""
    keys = [k for k, _ in BUILDERS]
    return keys + [k + "-fix" for k, fn in BUILDERS if _built_fix_ok(k)]


_FIX_OK = {"contract_duration", "contract_size", "contract_kv", "edge_windows", "edge_topk", "class_lru"}


def _built_fix_ok(key):
    return key in _FIX_OK


# the unittest method that FAILS on the wrong implementation, added to the tests of a repair task (the build task's own tests are weaker on purpose)
def _catch(key, b):
    mod = b["mod"]
    import re as _re
    fn = _re.search(r"^def (\w+)\(", b["sol"][f"{mod}.py"], _re.M)
    cls = _re.search(r"^class (\w+)\b", b["sol"][f"{mod}.py"], _re.M)
    hidden = b["hidden"]
    body = "\n".join("        " + ln for ln in hidden.replace("raise SystemExit(", "raise AssertionError(").strip().splitlines() if ln.strip() != 'print("ok")')
    return "\n\n    def test_contract_edges(self):\n" + body + "\n"


def _task(key, k, seed, built, hold):
    fix = key.endswith("-fix")
    base = key[:-4] if fix else key
    mod_files = built["sol"]
    tags = [TAG, "training", "python", "from-nothing" if not fix else "repair", base, "shape-" + built["lesson"], "lesson:" + (("repair_focus") if fix else built["lesson"])]
    if hold:
        tags.append("holdout")
    judge = []
    if fix:
        mod = built["mod"]
        tpath = f"tests/test_{mod}.py"
        catch = _catch(base, built)
        test_src = built["sol"][tpath] + catch
        goal = (f"The tests in {tpath} fail against {mod}.py. Fix {mod}.py so it meets this contract -- do not edit the tests: " + built["goal"].split("tests/. ", 1)[-1]
                if "tests/. " in built["goal"] else f"The tests in {tpath} fail against {mod}.py. Fix {mod}.py; do not edit the tests. " + built["goal"])
        fixture = {"files": {f"{mod}.py": built["wrong"][f"{mod}.py"], tpath: test_src}}
        judge = [{"type": "files_unchanged", "paths": [tpath]}, {"type": "python_assert", "code": built["hidden"], "timeout": 90}, {"type": "tests_pass"}]
        sol = {f"{mod}.py": built["sol"][f"{mod}.py"]}
        wrong = {f"{mod}.py": built["wrong"][f"{mod}.py"]}
        lesson = "repair_focus"
        calls = 20
    else:
        fixture = {"files": {}}
        judge = [{"type": "file_exists", "paths": built["must"] + ["tests/test_*.py"]}, {"type": "python_assert", "code": built["hidden"], "timeout": 90}]
        if built.get("min_tests"):
            judge.append({"type": "python_assert", "timeout": 30, "code": (
                "import glob, re\nn = sum(len(re.findall(r'^\\s*def test_', open(p, encoding='utf-8').read(), re.M)) for p in glob.glob('tests/test_*.py'))\n"
                "assert n >= %d, 'only %%d test functions' %% n\n" % built["min_tests"])})
        judge.append({"type": "tests_pass"})
        goal, sol, wrong, lesson, calls = built["goal"], mod_files, built["wrong"], built["lesson"], 24
    return {"id": f"{PREFIX}-{key}-s{seed}-{k:02d}", "template": key, "family": TAG, "lesson": lesson,
            "tags": tags, "goal": goal, "params": {"domains": ["api"]}, "fixture": fixture,
            "budget": {"calls": calls, "tokens": 90000, "seconds": 1200}, "judge": judge,
            "solution": {"files": sol}, "wrong": {"files": wrong}}


def _variant(key, k, seed):
    base = key[:-4] if key.endswith("-fix") else key
    fn = dict(BUILDERS)[base]
    return fn(random.Random(f"{seed}:{base}:{k}"))


def tasks_for(seed=0, per=PER_TEMPLATE, keys=None):
    """-> tasks, template by template: `per` DISTINCT variants of each (distinct by goal text) for this seed. Pure and seed-stable."""
    out = []
    for key in (keys or templates()):
        seen, k, tries = set(), 0, 0
        hold = key in HOLDOUT_TEMPLATES
        while len(seen) < per and tries < per * 8:
            b = _variant(key, k, seed)
            tries += 1
            k += 1
            h = hashlib.sha1(b["goal"].encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            out.append(_task(key, k - 1, seed, b, hold))
    return out


def all_tasks(seed=0):
    return tasks_for(seed)


def generate(n, seed=0):
    """n tasks, round-robin over the templates (a short run touches every pattern once), for this seed. Fresh seed = fresh variants."""
    by = {}
    for t in tasks_for(seed):
        by.setdefault(t["template"], []).append(t)
    keys = sorted(by)
    random.Random(f"{seed}:{TAG}").shuffle(keys)
    out, i = [], 0
    while any(i < len(v) for v in by.values()):
        out += [by[k][i] for k in keys if i < len(by[k])]
        i += 1
    return out[:max(0, n)]


# ------------------------------------------------------------------ proof
def check(quick=False, seed=0):
    """-> exit code. Every task: valid; the reference passes every judge and its tests run under unittest; the wrong answer is caught."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import importlib.util
    spec = importlib.util.spec_from_file_location("night_distributed", os.path.join(here, "distributed.py"))
    dist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dist)
    dist._setup_paths(isolate=True)
    import arch_eval as E
    import arch_fakecode as F
    tasks = [E.normalize(dict(t, flow=FLOW)) for t in all_tasks(seed)]
    if quick:
        seen, pick = set(), []
        for t in tasks:
            if t["template"] not in seen:
                seen.add(t["template"])
                pick.append(t)
        tasks = pick
    bad = 0
    for t in tasks:
        probs = E.validate(t)
        fix = t["template"].endswith("-fix")
        # a repair task starts from the buggy code: the reference is the fixed module over the fixture
        files = t["solution"]["files"]
        right, unit = dist._judge_all(E, t, files)
        wrong, _ = dist._judge_all(E, t, t["wrong"]["files"], skip=("file_exists",))
        bad_right = [(ty, why) for ty, good, why, could in right if not good or not could]
        caught = [ty for ty, good, why, could in wrong if not good and could]
        fake = [f"{rel}:{x['line']}" for rel, text in files.items() for x in F.scan_text(rel, text) if x.get("gate")]
        ok = not probs and not bad_right and not fake and caught and (unit[0] or fix)
        bad += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {t['id']}" + ("" if ok else f"   {probs or bad_right or fake or ('not caught' if not caught else unit[1][-200:])}"))
    print(f"\n{'ALL ' + str(len(tasks)) + ' CURRICULUM CHECKS PASSED' if not bad else str(bad) + ' of ' + str(len(tasks)) + ' CURRICULUM CHECKS FAILED'}")
    return 1 if bad else 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    if a.check:
        return check(a.quick, a.seed)
    for t in all_tasks(a.seed):
        print(f"  {t['id']:44} {t['lesson']:15} {t['goal'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
