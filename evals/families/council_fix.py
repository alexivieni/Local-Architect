"""council_fix: a module with one planted bug and its honest tests (which fail), repaired by the flow that tries locally first and, when the
worker is stuck, asks a council of the heavy tier. The judges are the bugfix family's: the tests are untouched, a hidden check the model never
saw passes, and the project's tests pass. Half the tasks (odd ones, tag `council`) make the scripted LOCAL fix fail (the oracle hands back the
buggy file again), so the flow has to reach its council step: the escalation path is exercised, not just the happy one. Whatever the council
returns still has to pass the same judges; a vote never decides, the tests do."""
import _bank as B

FLOW = "council_fix"
DEFAULT_N = 4
GOALS = ["Bug report: the test suite in tests/ fails ({t}). Find the cause in {m}.py and fix it.",
         "Bug report: tests/{t} is red. Fix the bug in {m}.py without changing the tests.",
         "Bug report: {m}.py gives wrong results ({name}); tests/{t} shows it. Fix it with the smallest change."]


def generate(n, seed=0):
    rng = B.rng_for(seed, "council_fix")
    out = []
    for i, (mod, bug) in enumerate(B.pick(rng, B.bug_cases(), n)):
        name, search, replace, hidden = bug
        tpath = B.test_path(mod)
        goal = GOALS[i % len(GOALS)].format(t=tpath.split("/")[-1], m=mod, name=name)
        repro = "import pytest\n" + hidden.replace("raise SystemExit('no error raised')", "pytest.fail('no error raised')").replace("raise SystemExit('no error')", "pytest.fail('no error')")
        repro_test = "def test_repro():\n" + "\n".join("    " + ln for ln in repro.splitlines()) + "\n"
        buggy = B.with_bug(mod, bug)
        council = i % 2 == 1
        oracle = {"repro": {"tests/test_repro_bug.py": repro_test}}
        if council:                      # the local tries hand back the buggy file (a try that fixes nothing); only the council's answer is right
            oracle["bad_fix"] = {f"{mod}.py": buggy.rstrip("\n") + "\n\n# tried a fix\n"}
        out.append({
            "id": f"council_fix-{mod}-{'council' if council else 'local'}-{i:02d}", "tags": ["python", "small"] + (["council"] if council else []), "goal": goal,
            "fixture": {"files": {f"{mod}.py": buggy, tpath: B.MODULES[mod]["tests"]}},
            "budget": {"calls": 24, "tokens": 90000, "seconds": 1200},
            "judge": [{"type": "files_unchanged", "paths": [tpath]}, {"type": "python_assert", "code": hidden}, {"type": "tests_pass"},
                      {"type": "functions_present", "path": f"{mod}.py", "names": B.MODULES[mod]["funcs"]}],
            "solution": {"files": {f"{mod}.py": B.MODULES[mod]["src"]}},
            "oracle": oracle,
            "wrong": {"files": {tpath: B.MODULES[mod]["tests"].replace("import unittest", "import unittest  # edited")}},   # 'fix' the tests instead of the code
        })
    return out
