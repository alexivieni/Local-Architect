"""build: create a small module with tests from nothing; a hidden behaviour check the model never saw must pass."""
import _bank as B

FLOW = "build"
DEFAULT_N = 6


def generate(n, seed=0):
    rng = B.rng_for(seed, "build")
    out = []
    for i, (mod, goal, hidden) in enumerate(B.pick(rng, B.BUILDS, n)):
        tpath = B.test_path(mod)
        out.append({
            "id": f"build-{mod}-{i:02d}", "tags": ["python", "from-nothing"], "goal": goal, "params": {"domains": ["api"]},
            "fixture": {"files": {}},
            "budget": {"calls": 24, "tokens": 90000, "seconds": 1200},
            "judge": [{"type": "python_assert", "code": hidden}, {"type": "file_exists", "paths": ["tests/test_*.py"]}, {"type": "tests_pass"}],
            "solution": {"files": {f"{mod}.py": B.MODULES[mod]["src"], tpath: B.MODULES[mod]["tests"]}},
            "wrong": {"files": {f"{mod}.py": B.with_bug(mod, B.MODULES[mod]["bugs"][0]), tpath: B.MODULES[mod]["tests"]}},
        })
    return out
