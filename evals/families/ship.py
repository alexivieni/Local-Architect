"""ship: one goal, end to end -- build it, then tests, a bug hunt, a security scan and a review on what was built, every stage an existing flow with
its own gates. This family does not re-judge the stages (their families do); it judges that the whole chain delivered: the built module passes
a hidden behaviour check the model never saw, its tests pass, the project's final summary (docs/ship.md) exists, lists every stage, and says the
build stage succeeded. Expensive on purpose (the flow's budget is 1.2M tokens): two tasks, tagged `slow`, so they can be picked out on their own
(`eval --tags slow`); a plain `eval --quick` carries them like any other flow."""
import _bank as B
import _scanbank as S

FLOW = "ship"
DEFAULT_N = 2



def _summary(goal, stages):
    """The report the flow writes (arch_pipeline.py): `- **stage**: passed | failed | could not be checked (not sure) | skipped ...`."""
    return f"# Shipped: {goal}\n\nPipeline: build -> test\n\n" + "\n".join(f"- **{s}**: {v}" for s, v in stages) + "\n\nFiles touched: 2\n"


# Read the written summary: it starts right, the build passed, no stage failed, and the core stages are listed. A stage that was skipped or
# could not be checked is not a failure (the flow itself counts those as fine); "failed" is.
SUMMARY_CHECK = ("import re\nt = open('docs/ship.md', encoding='utf-8').read()\nassert t.startswith('# Shipped:'), t[:80]\n"
                 "st = dict(re.findall(r'^- \\*\\*(\\w+)\\*\\*: (.*)$', t, re.M))\nassert st.get('build', '').startswith('passed'), 'build: %r' % st.get('build')\n"
                 "bad = {k: v for k, v in st.items() if v.startswith('failed')}\nassert not bad, 'stages failed: %r' % bad\n"
                 "assert {'build', 'test', 'review'} <= set(st), 'stages listed: %r' % sorted(st)\n")


def generate(n, seed=0):
    rng = B.rng_for(seed, "ship")
    out = []
    for i, (mod, goal, hidden) in enumerate(B.pick(rng, B.BUILDS, n)):
        tpath = B.test_path(mod)
        out.append({
            "id": f"ship-{mod}-{i:02d}", "tags": ["python", "from-nothing", "ship", "slow"], "goal": goal, "params": {"domains": ["api"]},
            "fixture": {"files": {}},
            "budget": {"calls": 120, "tokens": 400000, "seconds": 3000},
            "judge": [{"type": "python_assert", "code": S.build_hidden(mod, hidden)}, {"type": "file_exists", "paths": ["tests/test_*.py", "docs/ship.md"]}, {"type": "tests_pass"},
                      {"type": "python_assert", "code": SUMMARY_CHECK}],
            "oracle": {"tests_body": B.MODULES[mod]["tests"].replace("class T(", "class TExtra(")},      # the `test` stage's new file (the test family's own trick)
            "solution": {"files": {f"{mod}.py": B.MODULES[mod]["src"], tpath: B.MODULES[mod]["tests"],
                                   "docs/ship.md": _summary(goal, [(s, "passed") for s in ("build", "test", "hunt", "secure", "review")])}},
            "wrong": {"files": {f"{mod}.py": B.with_bug(mod, B.MODULES[mod]["bugs"][0]), tpath: B.MODULES[mod]["tests"],
                                "docs/ship.md": _summary(goal, [("build", "failed: the tests do not pass")])}},
        })
    return out
