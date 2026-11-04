"""test: a module with weak tests; new tests must pass on today's code and land in the file the flow names."""
import _bank as B

FLOW = "test"
DEFAULT_N = 6
WEAK = "import unittest\nimport {mod}\n\n\nclass T(unittest.TestCase):\n    def test_imports(self):\n        self.assertTrue(hasattr({mod}, '{f}'))\n"


def _extra_tests(mod):
    """A real unittest file derived from the bank's honest tests, renamed so it never collides with the weak one."""
    return B.MODULES[mod]["tests"].replace("class T(", "class TExtra(")


def generate(n, seed=0):
    rng = B.rng_for(seed, "test")
    out = []
    for i, mod in enumerate(B.pick(rng, list(B.MODULES), n)):
        f = B.MODULES[mod]["funcs"][0]
        out.append({"id": f"test-{mod}-{i:02d}", "tags": ["tests"], "goal": f"Improve the tests for {mod}.py (round {i + 1}): cover every function, boundaries and error cases.",
                    "fixture": {"files": {f"{mod}.py": B.MODULES[mod]["src"], B.test_path(mod): WEAK.format(mod=mod, f=f)}},
                    "budget": {"calls": 16, "tokens": 60000, "seconds": 900},
                    "judge": [{"type": "file_exists", "paths": [f"tests/test_{mod}_extra.py"]}, {"type": "file_contains", "path": f"tests/test_{mod}_extra.py", "needles": ["assert"]},
                              {"type": "files_unchanged", "paths": [f"{mod}.py"]}, {"type": "tests_pass"}],
                    "oracle": {"tests_body": _extra_tests(mod)},
                    "wrong": {"files": {f"tests/test_{mod}_extra.py": "import unittest\n\n\nclass TExtra(unittest.TestCase):\n    def test_nothing(self):\n        pass\n"}}})
    return out
