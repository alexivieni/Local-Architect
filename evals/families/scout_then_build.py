"""scout_then_build: the smallest model looks around first (read-only tools) and writes a brief; the planner and every sprint then work from that
brief. The tasks are the `feature` family's (add one function to a working module, keep every old one): a project that is NOT empty, so the
scout has something to find. Judges: the old functions survive and the file is not cut down, a hidden check passes, the tests pass -- and the
scout's own work is read from the run's trace: its brief names the module the request is about (tool use is not demanded: a grounded brief is judged by what it says)."""
import _bank as B

FLOW = "scout_then_build"
DEFAULT_N = 4


def generate(n, seed=0):
    rng = B.rng_for(seed, "scout_then_build")
    out = []
    for i, (mod, fname, goal, add_src, hidden, add_test) in enumerate(B.pick(rng, B.FEATURES, n)):
        src, tests, tpath = B.MODULES[mod]["src"], B.MODULES[mod]["tests"], B.test_path(mod)
        new_src = src.rstrip("\n") + "\n" + add_src
        new_tests = tests.replace("\n\nif __name__", add_test + "\n\nif __name__")
        brief = (f"FILES: {mod}.py holds the code to extend; {tpath} holds its tests.\nUSE: the existing functions in {mod}.py.\n"
                 f"TESTS: unittest files under tests/, named test_<module>.py.\nCONVENTIONS: standard library only, small functions.\nUNKNOWN: nothing.")
        out.append({
            "id": f"scout_then_build-{mod}-{i:02d}", "tags": ["python", "edit", "scout"], "goal": goal, "params": {"domains": ["api"]},
            "fixture": {"files": {f"{mod}.py": src, tpath: tests}},
            "budget": {"calls": 30, "tokens": 110000, "seconds": 1500},
            "judge": [{"type": "functions_present", "path": f"{mod}.py", "names": B.MODULES[mod]["funcs"]}, {"type": "no_shrink", "path": f"{mod}.py"},
                      {"type": "python_assert", "code": hidden}, {"type": "tests_pass"},
                      {"type": "answer_mentions", "needles": [rf"{mod}\.py"]}],
            "oracle": {"tool": "ls", "tool_args": {}, "answer": brief},
            "solution": {"files": {f"{mod}.py": new_src, tpath: new_tests}},
            "wrong": {"files": {f"{mod}.py": add_src.lstrip("\n") if not add_src.startswith("\n    def") else "class Cart:\n" + add_src}},   # only the new function survives
        })
    return out
