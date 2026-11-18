"""document: modules and public functions with no docstring must get real ones. The flow's own AST check (via the scanner_zero judge), an
independent runtime check (inspect.getdoc), the tests and the function list all have to agree: nothing but the documentation changed."""
import _bank as B
import _scanbank as S

FLOW = "document"
DEFAULT_N = 6
# (name, what is taken out of the clean form, what a plausible half-fix leaves)
VARIANTS = [("all", dict(module=True, functions=True), dict(module=True, functions=False)),
            ("functions", dict(module=False, functions=True), dict(module=True, functions=False))]


def generate(n, seed=0):
    rng = B.rng_for(seed, "document")
    out = []
    for i, (mod, (vname, flaw, half)) in enumerate(B.pick(rng, [(m, v) for m in S.CLEAN for v in VARIANTS], n)):
        clean = S.CLEAN[mod]
        tpath = B.test_path(mod)
        what = "the module and every public function" if vname == "all" else "every public function"
        out.append({"id": f"document-{mod}-{vname}-{i:02d}", "tags": ["docs"],
                    "goal": f"Write docstrings for {what} in {mod}.py (round {i + 1}), saying what the code actually does. Do not change the code.",
                    "fixture": {"files": {f"{mod}.py": S.strip_docstrings(clean, **flaw), tpath: S.tests_of(mod)}},
                    "budget": {"calls": 16, "tokens": 60000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "docs", "files": [f"{mod}.py"]}, {"type": "python_assert", "code": S.documented_check(mod)},
                              {"type": "tests_pass"}, {"type": "functions_present", "path": f"{mod}.py", "names": S.FUNCS[mod]},
                              {"type": "no_shrink", "path": f"{mod}.py", "min_ratio": 0.8}],
                    "solution": {"files": {f"{mod}.py": clean}},
                    "wrong": {"files": {f"{mod}.py": S.strip_docstrings(clean, **half)}}})
    return out
