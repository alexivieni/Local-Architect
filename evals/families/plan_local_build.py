"""plan_local_build: the thinking roles plan and judge, the local worker writes every line. The same small from-nothing builds as the `build`
family (a hidden behaviour check the model never saw, tests under tests/ that pass), run through the flow whose plan comes from the heavy
tier and is judged against its own contract. Offline by default: the Claude branch of the flow needs `online.enabled`, which the eval never turns on."""
import _bank as B
import _scanbank as S

FLOW = "plan_local_build"
DEFAULT_N = 4


def generate(n, seed=0):
    rng = B.rng_for(seed, "plan_local_build")
    out = []
    for i, (mod, goal, hidden) in enumerate(B.pick(rng, B.BUILDS, n)):
        tpath = B.test_path(mod)
        out.append({
            "id": f"plan_local_build-{mod}-{i:02d}", "tags": ["python", "from-nothing", "planned"], "goal": goal, "params": {"domains": ["api"]},
            "fixture": {"files": {}},
            "budget": {"calls": 30, "tokens": 120000, "seconds": 1500},
            "judge": [{"type": "python_assert", "code": S.build_hidden(mod, hidden)}, {"type": "file_exists", "paths": ["tests/test_*.py"]}, {"type": "tests_pass"},
                      {"type": "functions_present", "path": f"{mod}.py", "names": B.MODULES[mod]["funcs"]}],
            "solution": {"files": {f"{mod}.py": B.MODULES[mod]["src"], tpath: B.MODULES[mod]["tests"]}},
            "wrong": {"files": {f"{mod}.py": B.with_bug(mod, B.MODULES[mod]["bugs"][0]), tpath: B.MODULES[mod]["tests"]}},
        })
    return out
