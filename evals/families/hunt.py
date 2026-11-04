"""hunt: a module with a planted bug its own tests miss; the hunter must prove it with a failing test and docs/bugs.md must list it."""
import _bank as B

FLOW = "hunt"
DEFAULT_N = 6


def generate(n, seed=0):
    rng = B.rng_for(seed, "hunt")
    out = []
    for i, (mod, bug) in enumerate(B.pick(rng, B.bug_cases(), n)):
        name, search, replace, hidden = bug
        tpath = B.test_path(mod)
        weak = "import unittest\nimport " + mod + "\n\n\nclass T(unittest.TestCase):\n    def test_imports(self):\n        self.assertTrue(hasattr(" + mod + ", '" + B.MODULES[mod]["funcs"][0] + "'))\n"
        body = hidden.replace("raise SystemExit('no error raised')", "raise AssertionError('no error raised')").replace("raise SystemExit('no error')", "raise AssertionError('no error')")
        test = f'"""BUG: {name} | {mod}.py:{B.MODULES[mod]["funcs"][0]} | the result is wrong for a simple input"""\n\n\ndef test_bug():\n' + "\n".join("    " + ln for ln in body.splitlines()) + "\n"
        out.append({"id": f"hunt-{mod}-{i:02d}", "tags": ["hunt"], "goal": f"Hunt for bugs in {mod}.py (round {i + 1}).",
                    "fixture": {"files": {f"{mod}.py": B.with_bug(mod, bug), tpath: weak}},
                    "budget": {"calls": 10, "tokens": 40000, "seconds": 600},
                    "judge": [{"type": "file_exists", "paths": ["docs/bugs.md", "tests/hunt/test_*.py"]}, {"type": "file_contains", "path": "docs/bugs.md", "needles": ["Confirmed bugs (1"]},
                              {"type": "files_unchanged", "paths": [f"{mod}.py"]}],
                    "oracle": {"hunt": {"tests/hunt/test_planted.py": test}},
                    "wrong": {"files": {"tests/hunt/test_planted.py": test.replace("def test_bug():", "def test_bug():\n    return")}}})
    return out
