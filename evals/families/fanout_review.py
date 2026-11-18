"""fanout_review: three reviewer passes read the same module through three lenses, a second reader keeps only the findings it can point at in the
code, and every kept behaviour bug must be proven by a test that fails on today's code (or stay unproven). The module has one planted bug and
its honest tests, like the `hunt` family. Judges, all code: the report exists with its sections, the kept findings name the file the bug is
in (a finding that names no real place proves nothing), a proving test exists under tests/hunt/, and the application code and tests are
untouched (a review changes nothing). Quality of the prose is not judged here; where it points is."""
import _bank as B

FLOW = "fanout_review"
DEFAULT_N = 4


def generate(n, seed=0):
    rng = B.rng_for(seed, "fanout_review")
    out = []
    for i, (mod, bug) in enumerate(B.pick(rng, B.bug_cases(), n)):
        name, search, replace, hidden = bug
        tpath = B.test_path(mod)
        fn = B.MODULES[mod]["funcs"][0]
        weak = "import unittest\nimport " + mod + "\n\n\nclass T(unittest.TestCase):\n    def test_imports(self):\n        self.assertTrue(hasattr(" + mod + ", '" + fn + "'))\n"
        body = hidden.replace("raise SystemExit('no error raised')", "raise AssertionError('no error raised')").replace("raise SystemExit('no error')", "raise AssertionError('no error')")
        proof = f'"""BUG: {name} | {mod}.py:{fn} | the result is wrong for a simple input"""\n\n\ndef test_bug():\n' + "\n".join("    " + ln for ln in body.splitlines()) + "\n"
        finding = f"[high] {mod}.py:{fn} -- {name} -- `{search.strip().splitlines()[0][:60]}` -- fix the line -- kind: bug"
        buggy = B.with_bug(mod, bug)
        report = ("# Review from several angles\n\n## Findings that survived a second reading\n\n- " + finding + "\n\n## Proven by a failing test\n\n"
                  f"### {name}\n- Proving test: `tests/hunt/test_planted.py` (fails today)\n")
        out.append({"id": f"fanout_review-{mod}-{i:02d}", "tags": ["review", "hunt"], "goal": f"Review {mod}.py from several angles and prove any behaviour bug (round {i + 1}).",
                    "fixture": {"files": {f"{mod}.py": buggy, tpath: weak}},
                    "budget": {"calls": 12, "tokens": 90000, "seconds": 900},
                    "judge": [{"type": "file_exists", "paths": ["docs/review_angles.md", "tests/hunt/test_*.py"]},
                              {"type": "file_contains", "path": "docs/review_angles.md", "regex": True,
                               "needles": [r"## Findings that survived a second reading\s+\S", r"## Proven by a failing test", rf"{mod}\.py"]},
                              {"type": "files_unchanged", "paths": [f"{mod}.py", tpath]}],
                    "oracle": {"default": finding, "lens": "- " + finding, "crosscheck": "## Kept\n- " + finding + "\n## Dropped\n- nothing\n",
                               "hunt": {"tests/hunt/test_planted.py": proof}},
                    "solution": {"files": {"docs/review_angles.md": report, "tests/hunt/test_planted.py": proof}},
                    "wrong": {"files": {"docs/review_angles.md": "# Review from several angles\n\n## Findings that survived a second reading\n\nnothing\n"}}})
    return out
