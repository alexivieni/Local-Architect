"""review: docs/review.md must exist with the required sections and the facts block the tools produced."""
import _bank as B

FLOW = "review"
DEFAULT_N = 6
REVIEW = ("## Summary\nSmall, readable module. Overall healthy. The most important fix is input validation.\n\n"
          "## Risks\n| severity | where | problem | suggested fix |\n|---|---|---|---|\n| low | {mod}.py:{f} | no input validation | validate arguments |\n\n"
          "## Design\n- functions are small\n- naming is clear\n\n## Missing tests\n- empty input for {f}\n")


def generate(n, seed=0):
    rng = B.rng_for(seed, "review")
    out = []
    for i, mod in enumerate(B.pick(rng, list(B.MODULES), n)):
        f = B.MODULES[mod]["funcs"][0]
        out.append({"id": f"review-{mod}-{i:02d}", "tags": ["review"], "goal": f"Do a code review of {mod}.py (pass {i + 1}).",
                    "fixture": {"files": {f"{mod}.py": B.MODULES[mod]["src"], B.test_path(mod): B.MODULES[mod]["tests"]}},
                    "budget": {"calls": 6, "tokens": 30000, "seconds": 600},
                    "judge": [{"type": "docs_structure", "kind": "sections", "path": "docs/review.md", "headings": ["Facts from tools", "Summary", "Risks", "Design", "Missing tests"]},
                              {"type": "files_unchanged", "paths": [f"{mod}.py"]}],
                    "oracle": {"review": REVIEW.format(mod=mod, f=f)}, "wrong": {"files": {}}})
    return out
