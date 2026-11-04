"""planjson: a plan must come back in the required structure (schema-constrained JSON, re-validated in code)."""
import _bank as B

FLOW = "planjson"
DEFAULT_N = 6
GOALS = ["a todo list command line app", "a function that parses dates", "a small REST endpoint for notes", "a CSV import with validation",
         "a rate limiter for an API", "a markdown to HTML converter"]


def generate(n, seed=0):
    rng = B.rng_for(seed, "planjson")
    out = []
    for i, g in enumerate(B.pick(rng, GOALS, n)):
        out.append({"id": f"planjson-{i:02d}", "kind": "json", "role": "planner", "schema": "PLAN", "tags": ["structured"],
                    "goal": "Plan the work as small SPRINTS now. " + g, "fixture": {"files": {}},
                    "budget": {"calls": 1, "tokens": 6000, "seconds": 180},
                    "judge": [{"type": "json_shape", "keys": ["sprints"]}]})
    return out
