"""typehints: the family for the `types` flow. Public functions with unannotated parameters or return values must be annotated. The flow's
own AST check (via the scanner_zero judge), an independent runtime check (inspect), the tests and the function list all have to agree.
Not named types.py: this folder is put on sys.path by the loader, and that name would shadow the standard library's `types`."""
import _bank as B
import _scanbank as S

FLOW = "types"
DEFAULT_N = 6
# (name, what is taken out of the clean form, what a plausible half-fix leaves)
VARIANTS = [("all", dict(params=True, returns=True), dict(params=False, returns=True)),
            ("returns", dict(params=False, returns=True), dict(params=True, returns=False))]


def generate(n, seed=0):
    rng = B.rng_for(seed, "types")
    out = []
    for i, (mod, (vname, flaw, half)) in enumerate(B.pick(rng, [(m, v) for m in S.CLEAN for v in VARIANTS], n)):
        clean = S.CLEAN[mod]
        tpath = B.test_path(mod)
        what = "every parameter and every return value" if vname == "all" else "the return value"
        out.append({"id": f"types-{mod}-{vname}-{i:02d}", "tags": ["types"],
                    "goal": f"Add type hints to every public function in {mod}.py: {what} (round {i + 1}). Do not change what the code does.",
                    "fixture": {"files": {f"{mod}.py": S.strip_annotations(clean, **flaw), tpath: S.tests_of(mod)}},
                    "budget": {"calls": 16, "tokens": 60000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "types", "files": [f"{mod}.py"]}, {"type": "python_assert", "code": S.typed_check(mod)},
                              {"type": "tests_pass"}, {"type": "functions_present", "path": f"{mod}.py", "names": S.FUNCS[mod]},
                              {"type": "no_shrink", "path": f"{mod}.py", "min_ratio": 0.8}],
                    "solution": {"files": {f"{mod}.py": clean}},
                    "wrong": {"files": {f"{mod}.py": S.strip_annotations(clean, **half)}}})
    return out
