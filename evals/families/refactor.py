"""refactor: duplicated code must be simplified; characterization tests pin today's behaviour and every public function must survive."""
import _bank as B

FLOW = "refactor"
DEFAULT_N = 6


def _duplicated(mod):
    """The correct module with a copy-pasted helper added twice (the smell to remove)."""
    src = B.MODULES[mod]["src"]
    dup = ("\n\ndef _norm_a(xs):\n    out = []\n    for x in xs:\n        if x is not None:\n            out.append(x)\n    return out\n"
           "\n\ndef _norm_b(xs):\n    out = []\n    for x in xs:\n        if x is not None:\n            out.append(x)\n    return out\n"
           "\n\ndef cleaned(xs):\n    \"\"\"Non-None items, in order.\"\"\"\n    return _norm_a(xs) if len(xs) % 2 else _norm_b(xs)\n")
    return src.rstrip("\n") + dup


def _clean(mod):
    src = B.MODULES[mod]["src"]
    return src.rstrip("\n") + "\n\n\ndef cleaned(xs):\n    \"\"\"Non-None items, in order.\"\"\"\n    return [x for x in xs if x is not None]\n"


def generate(n, seed=0):
    rng = B.rng_for(seed, "refactor")
    out = []
    mods = [m for m in B.MODULES if m != "cart"]
    for i, mod in enumerate(B.pick(rng, mods, n)):
        tpath = B.test_path(mod)
        char = ("import unittest\nfrom " + mod + " import cleaned\n\n\nclass TChar(unittest.TestCase):\n    def test_cleaned(self):\n        self.assertEqual(cleaned([0, None, 2]), [0, 2])\n        self.assertEqual(cleaned([None]), [])\n")
        out.append({"id": f"refactor-{mod}-{i:02d}", "tags": ["refactor"], "goal": f"Refactor {mod}.py (round {i + 1}): the two duplicated helpers _norm_a and _norm_b should become one simple expression; behaviour must not change.",
                    "fixture": {"files": {f"{mod}.py": _duplicated(mod), tpath: B.MODULES[mod]["tests"]}},
                    "budget": {"calls": 20, "tokens": 70000, "seconds": 900},
                    "judge": [{"type": "functions_present", "path": f"{mod}.py", "names": B.MODULES[mod]["funcs"] + ["cleaned"]},
                              {"type": "file_lacks", "path": f"{mod}.py", "needles": ["_norm_b"]},
                              {"type": "python_assert", "code": f"from {mod} import cleaned\nassert cleaned([0, None, 2]) == [0, 2] and cleaned([]) == []"}, {"type": "tests_pass"}],
                    "solution": {"files": {f"{mod}.py": _clean(mod)}}, "oracle": {"characterization": {f"tests/characterization/test_{mod}_char.py": char}},
                    "wrong": {"files": {f"{mod}.py": _clean(mod).replace("if x is not None", "if x")}}})
    return out
