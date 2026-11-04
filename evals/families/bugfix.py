"""bugfix: a module with one planted bug and its honest tests (which fail). The fix must leave the tests untouched and make a hidden check pass."""
import _bank as B

FLOW = "bugfix"
DEFAULT_N = 17
GOALS = ["The test suite in tests/ fails ({t}). Find the cause in {m}.py and fix it.",
         "Tests are red: tests/{t} fails. Fix the bug in {m}.py without changing the tests.",
         "{m}.py has a bug that makes tests/{t} fail ({name}). Fix it with the smallest change."]


def generate(n, seed=0):
    rng = B.rng_for(seed, "bugfix")
    out = []
    for i, (mod, bug) in enumerate(B.pick(rng, B.bug_cases(), n)):
        name, search, replace, hidden = bug
        tpath = B.test_path(mod)
        goal = GOALS[i % len(GOALS)].format(t=tpath.split("/")[-1], m=mod, name=name)
        repro = "import pytest\n" + hidden.replace("raise SystemExit('no error raised')", "pytest.fail('no error raised')").replace("raise SystemExit('no error')", "pytest.fail('no error')")
        repro_test = "def test_repro():\n" + "\n".join("    " + ln for ln in repro.splitlines()) + "\n"
        out.append({
            "id": f"bugfix-{mod}-{i:02d}", "tags": ["python", "small"], "goal": goal,
            "fixture": {"files": {f"{mod}.py": B.with_bug(mod, bug), tpath: B.MODULES[mod]["tests"]}},
            "budget": {"calls": 20, "tokens": 60000, "seconds": 900},
            "judge": [{"type": "files_unchanged", "paths": [tpath]}, {"type": "python_assert", "code": hidden}, {"type": "tests_pass"}],
            "solution": {"files": {f"{mod}.py": B.MODULES[mod]["src"]}},
            "oracle": {"repro": {"tests/test_repro_bug.py": repro_test}},
            "wrong": {"files": {tpath: B.MODULES[mod]["tests"].replace("import unittest", "import unittest  # edited")}},   # 'fix' the tests instead of the code
        })
    return out
