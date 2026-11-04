"""purge: stubs and assertion-free tests must become real; the fake-code scanner re-run proves it and no real test may be lost."""
import _bank as B

FLOW = "purge"
DEFAULT_N = 6


def _stubbed(mod, rng):
    """The correct module with one function body replaced by `pass`, plus a test file whose extra test asserts nothing."""
    src = B.MODULES[mod]["src"]
    funcs = [f for f in B.MODULES[mod]["funcs"] if f[0].islower()]
    f = rng.choice(funcs)
    import re
    m = re.search(rf"^def {f}\((.*?)\):\n(?:    \"\"\".*?\"\"\"\n)?((?:    .*\n)+)", src, re.M | re.S)
    body = m.group(2)
    stub = src.replace(body, "    pass\n", 1) if m else src
    tests = B.MODULES[mod]["tests"].replace("\n\nif __name__", "\n    def test_placeholder(self):\n        assert True\n\n\nif __name__")
    return f, stub, tests


def generate(n, seed=0):
    rng = B.rng_for(seed, "purge")
    out = []
    mods = [m for m in B.MODULES if m != "cart"]
    for i, mod in enumerate(B.pick(rng, mods, n)):
        f, stub, tests = _stubbed(mod, B.rng_for(seed, f"purge{i}"))
        tpath = B.test_path(mod)
        real_tests = B.MODULES[mod]["tests"]
        out.append({"id": f"purge-{mod}-{i:02d}", "tags": ["fakecode"], "goal": f"Remove the fake code in {mod}.py and tests/ (round {i + 1}): implement the stub {f}() for real and make every test assert something.",
                    "fixture": {"files": {f"{mod}.py": stub, tpath: tests}},
                    "budget": {"calls": 16, "tokens": 50000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "fakecode", "files": [f"{mod}.py", tpath]}, {"type": "tests_pass"},
                              {"type": "python_assert", "code": B.MODULES[mod]["bugs"][0][3].replace("raise SystemExit", "raise SystemExit")}],
                    "solution": {"files": {f"{mod}.py": B.MODULES[mod]["src"], tpath: real_tests}},
                    "wrong": {"files": {f"{mod}.py": stub, tpath: real_tests}}})
    return out
