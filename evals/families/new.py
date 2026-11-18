"""new: from an empty folder the flow picks a template (by code: the goal's words, or the template the person named), writes its files and runs
the template's own tests -- no model is needed for this part. The judges read the finished folder: every file the template renders is there,
the template's tests pass, and the hand-off note exists. The build step is switched off (params then_build = False; the goals are also under 5 words), so a model's quality never
hides a break in the scaffold; a wrong template choice shows up as missing files. Measures the flow that runs by default (the Python class in
arch_flows_pro.py); flows/new.yaml only runs with flows.engine = data."""
import _bank as B

FLOW = "new"
DEFAULT_N = 6
NAME = "demo_app"

# (goal of at most 24 characters, template the code must pick, params that name it explicitly or {})
# Left out ON PURPOSE, a product gap this family does not hide: under the default settings the writer refuses some template files (an empty
# __init__.py: rest_api, fullstack, data_sql, fastapi; a .csv or .log sample, "file type is not enabled": etl_pipeline, data_cleaner,
# log_analyzer, ml_train, pdf_report, security_lab), so those projects come out incomplete and log_analyzer's own test fails. See
# docs/claude/decisions.md, 2026-11-17. Once the scaffold writes its own files, add them here.
CASES = [
    ("a python library", "python_lib", {}),
    ("a CLI tool", "cli_tool", {}),
    ("a cache with TTL", "cache_layer", {}),
    ("a state machine", "state_machine", {}),
    ("an event bus", "event_bus", {}),
    ("a tiny project", "worker", {"template": "worker"}),
]


def generate(n, seed=0):
    import arch_templates as T
    rng = B.rng_for(seed, "new")
    out = []
    for i, (goal, tid, extra) in enumerate(B.pick(rng, CASES, n)):
        assert len(goal) <= 24, goal
        files = dict(T.render(tid, NAME))
        out.append({"id": f"new-{tid}-{i:02d}", "tags": ["new", "scaffold"], "goal": goal, "params": dict({"name": NAME, "then_build": False}, **extra),
                    "fixture": {"files": {}},
                    "budget": {"calls": 8, "tokens": 30000, "seconds": 600},
                    "judge": [{"type": "file_exists", "paths": sorted(files) + [".arch/handoff.json"]}, {"type": "tests_pass"}],
                    "solution": {"files": dict(files, **{".arch/handoff.json": "{}\n"})},
                    "wrong": {"files": {"README.md": f"# {NAME}\n"}}})
    return out
