"""optimize: a correct but quadratic (or exponential) function must become measurably faster with the same results. The judges are code:
the tests pass, the function is still there, and a timing check the model never saw runs the function on a big fixed input and wants the
answer to be right AND at least SPEEDUP times faster than the original code timed in the same process (a RATIO, so a busy or a faster
machine moves both sides together and cannot flip the verdict; the obvious fixes here are 50-3000x). Half the tasks ship a benchmark,
half make the model write one (the flow's write_bench step) and the judge then wants bench/bench_*.py to exist."""
import _bank as B

FLOW = "optimize"
DEFAULT_N = 4
SPEEDUP = 3

CASES = [
    {"mod": "dedupe", "fn": "dedupe", "goal": "dedupe() in dedupe.py is slow on long lists; make it faster without changing its result or its order.",
     "slow": '''"""Remove repeated items from a list."""


def dedupe(xs):
    """The items of xs without repeats, in first-seen order."""
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out
''',
     "fast": '''"""Remove repeated items from a list."""


def dedupe(xs):
    """The items of xs without repeats, in first-seen order."""
    seen = set()
    out = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
''',
     "tests": 'import unittest\nfrom dedupe import dedupe\n\n\nclass T(unittest.TestCase):\n    def test_order_kept(self):\n        self.assertEqual(dedupe([3, 1, 3, 2, 1]), [3, 1, 2])\n\n    def test_empty(self):\n        self.assertEqual(dedupe([]), [])\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
     "data": "[i % 3000 for i in range(12000)]", "call": "{f}(DATA)", "expect": "list(range(3000))"},
    {"mod": "common", "fn": "common_items", "goal": "common_items() in common.py is slow on big lists; make it faster without changing its result or its order.",
     "slow": '''"""Items two lists share."""


def common_items(a, b):
    """The items of a that also appear in b, in a's order (repeats in a are kept)."""
    return [x for x in a if x in b]
''',
     "fast": '''"""Items two lists share."""


def common_items(a, b):
    """The items of a that also appear in b, in a's order (repeats in a are kept)."""
    wanted = set(b)
    return [x for x in a if x in wanted]
''',
     "tests": 'import unittest\nfrom common import common_items\n\n\nclass T(unittest.TestCase):\n    def test_basic(self):\n        self.assertEqual(common_items([1, 2, 2, 3], [2, 3, 4]), [2, 2, 3])\n\n    def test_none(self):\n        self.assertEqual(common_items([1], [2]), [])\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
     "data": "(list(range(0, 16000, 2)), list(range(0, 16000, 3)))", "call": "{f}(*DATA)", "expect": "[x for x in range(0, 16000, 2) if x % 3 == 0]"},
    {"mod": "freq", "fn": "word_freq", "goal": "word_freq() in freq.py is slow on long word lists; make it faster without changing its result.",
     "slow": '''"""Count how often each word occurs."""


def word_freq(words):
    """{word: number of times it occurs in words}."""
    return {w: words.count(w) for w in set(words)}
''',
     "fast": '''"""Count how often each word occurs."""


def word_freq(words):
    """{word: number of times it occurs in words}."""
    counts = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    return counts
''',
     "tests": 'import unittest\nfrom freq import word_freq\n\n\nclass T(unittest.TestCase):\n    def test_counts(self):\n        self.assertEqual(word_freq(["a", "b", "a"]), {"a": 2, "b": 1})\n\n    def test_empty(self):\n        self.assertEqual(word_freq([]), {})\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
     "data": '[f"w{i % 1000}" for i in range(10000)]', "call": "{f}(DATA)", "expect": '{f"w{i}": 10 for i in range(1000)}'},
    {"mod": "series", "fn": "fib", "goal": "fib() in series.py is slow for n around 30; make it faster without changing what it returns.",
     "slow": '''"""Fibonacci numbers."""


def fib(n):
    """The n-th Fibonacci number (fib(0) = 0, fib(1) = 1)."""
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)
''',
     "fast": '''"""Fibonacci numbers."""


def fib(n):
    """The n-th Fibonacci number (fib(0) = 0, fib(1) = 1)."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
''',
     "tests": 'import unittest\nfrom series import fib\n\n\nclass T(unittest.TestCase):\n    def test_small(self):\n        self.assertEqual([fib(i) for i in range(8)], [0, 1, 1, 2, 3, 5, 8, 13])\n\n    def test_twenty(self):\n        self.assertEqual(fib(20), 6765)\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
     "data": "29", "call": "{f}(DATA)", "expect": "514229"},
]


def bench_text(c):
    return ('"""Benchmark for ' + c["fn"] + '(): prints the seconds per run as the last line."""\nimport json\nimport os\nimport sys\nimport time\n\n'
            'sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n'
            f'from {c["mod"]} import {c["fn"]}\n\nDATA = {c["data"]}\nRUNS = 3\n\n'
            f't0 = time.perf_counter()\nfor _ in range(RUNS):\n    {c["call"].replace("{f}", c["fn"])}\n'
            'print(json.dumps({"seconds": (time.perf_counter() - t0) / RUNS}))\n')


def timing_check(c):
    """Hidden from the model: the answer is right, and the best of five runs is at least SPEEDUP times faster than the ORIGINAL code's best of
    five, both timed here, one after the other, on the same input (the original comes from the task's own fixture source)."""
    new = c["call"].replace("{f}", "new")
    probe = c["call"].replace("{f}", "f")
    return (f"import time\nfrom {c['mod']} import {c['fn']} as new\nns = {{}}\nexec({c['slow']!r}, ns)\nref = ns[{c['fn']!r}]\nDATA = {c['data']}\n"
            f"assert {new} == {c['expect']}, 'wrong result'\n"
            f"def best(f):\n    b = 1e9\n    for _ in range(5):\n        t0 = time.perf_counter()\n        {probe}\n"
            "        b = min(b, time.perf_counter() - t0)\n    return b\n"
            "t_ref, t_new = best(ref), best(new)\n"
            f"assert t_new * {SPEEDUP} <= t_ref, 'not faster: %.5fs now vs %.5fs before (needs {SPEEDUP}x)' % (t_new, t_ref)\n")


def generate(n, seed=0):
    rng = B.rng_for(seed, "optimize")
    out = []
    for i, c in enumerate(B.pick(rng, CASES, n)):
        mod, path = c["mod"], f"{c['mod']}.py"
        with_bench = i % 2 == 0
        bench = f"bench/bench_{mod}.py"
        files = {path: c["slow"], B.test_path(mod): c["tests"]}
        goal = c["goal"] + (f" A benchmark is in {bench}." if with_bench else " There is no benchmark yet: write one first.")
        if with_bench:
            files[bench] = bench_text(c)
        judge = [{"type": "python_assert", "code": timing_check(c), "timeout": 90}, {"type": "tests_pass"},
                 {"type": "functions_present", "path": path, "names": [c["fn"]]}, {"type": "file_exists", "paths": ["bench/bench_*.py"]}]
        out.append({"id": f"optimize-{mod}-{'bench' if with_bench else 'nobench'}-{i:02d}", "tags": ["optimize"], "goal": goal,
                    "fixture": {"files": files},
                    "budget": {"calls": 24, "tokens": 90000, "seconds": 900},
                    "judge": judge,
                    "oracle": {"bench": {bench: bench_text(c)}},
                    "solution": {"files": {path: c["fast"], bench: bench_text(c)}},
                    "wrong": {"files": {path: c["slow"].rstrip("\n") + "\n\n# tuned\n"}}})     # a comment is not a speed-up
    return out
