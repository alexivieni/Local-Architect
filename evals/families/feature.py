"""feature: add one function to a working module. Every old function must survive, the file must not be cut down, the new one must work."""
import _bank as B

FLOW = "feature"
DEFAULT_N = 6


def generate(n, seed=0):
    rng = B.rng_for(seed, "feature")
    out = []
    for i, (mod, fname, goal, add_src, hidden, add_test) in enumerate(B.pick(rng, B.FEATURES, n)):
        src, tests, tpath = B.MODULES[mod]["src"], B.MODULES[mod]["tests"], B.test_path(mod)
        new_src = src.rstrip("\n") + "\n" + add_src if not add_src.startswith("\n    def") else src.rstrip("\n") + "\n" + add_src
        new_tests = tests.replace("\n\nif __name__", add_test + "\n\nif __name__")
        out.append({
            "id": f"feature-{mod}-{i:02d}", "tags": ["python", "edit"], "goal": goal, "params": {"domains": ["api"]},
            "fixture": {"files": {f"{mod}.py": src, tpath: tests}},
            "budget": {"calls": 24, "tokens": 90000, "seconds": 1200},
            "judge": [{"type": "functions_present", "path": f"{mod}.py", "names": B.MODULES[mod]["funcs"]}, {"type": "no_shrink", "path": f"{mod}.py"},
                      {"type": "python_assert", "code": hidden}, {"type": "tests_pass"}],
            "solution": {"files": {f"{mod}.py": new_src, tpath: new_tests}},
            "wrong": {"files": {f"{mod}.py": add_src.lstrip("\n") if not add_src.startswith("\n    def") else "class Cart:\n" + add_src}},   # only the new function survives
        })
    return out
