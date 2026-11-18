"""migrate: every call of an old helper must move to the new one. The flow's own search (via the scanner_zero judge, kind migrate) finds
no old call left anywhere, the new calls are there, the tests still pass, and the two helper modules and the tests were not touched
(the old helper stays shipped; only its callers move)."""
import _bank as B

FLOW = "migrate"
DEFAULT_N = 4

# old name, new name, parameters, the shared body, a call with its expected result
CASES = [
    ("slug", "slugify", "text", 'return "-".join(text.lower().split())', '"Hello Big World"', '"hello-big-world"'),
    ("fmt_price", "format_price", "cents", 'return f"${cents / 100:.2f}"', "1999", '"$19.99"'),
    ("limit", "clamp", "x, lo, hi", "return max(lo, min(hi, x))", "15, 0, 10", "10"),
    ("count_words", "word_count", "text", "return len(text.split())", '"a b  c"', "3"),
]


def _lib(doc, name, params, body):
    return f'"""{doc}"""\n\n\ndef {name}({params}):\n    """Same result as its twin in the other module."""\n    {body}\n'


def _app(lib, name, params):
    return (f'"""The app\'s helpers, built on the helper library."""\nimport {lib}\n\n\n'
            f'def first({params}):\n    return {lib}.{name}({params})\n\n\n'
            f'def both({params}):\n    return [{lib}.{name}({params}), {lib}.{name}({params})]\n\n\n'
            f'def tagged({params}):\n    return "<" + str({lib}.{name}({params})) + ">"\n')


def _tests(call, expect):
    return ('import unittest\nimport app\n\n\nclass T(unittest.TestCase):\n'
            f'    def test_first(self):\n        self.assertEqual(app.first({call}), {expect})\n\n'
            f'    def test_both(self):\n        self.assertEqual(app.both({call}), [{expect}, {expect}])\n\n'
            f'    def test_tagged(self):\n        self.assertEqual(app.tagged({call}), "<" + str({expect}) + ">")\n\n\n'
            'if __name__ == "__main__":\n    unittest.main()\n')


def generate(n, seed=0):
    rng = B.rng_for(seed, "migrate")
    out = []
    for i, (old, new, params, body, call, expect) in enumerate(B.pick(rng, CASES, n)):
        legacy, modern, tests = _lib("The old helpers: still shipped, dropped once nothing calls them.", old, params, body), \
            _lib("The current helpers.", new, params, body), _tests(call, expect)
        half = _app("legacy", old, params).replace(f"def first({params}):\n    return legacy.{old}({params})", f"def first({params}):\n    return modern.{new}({params})", 1)
        out.append({"id": f"migrate-{old}-{i:02d}", "tags": ["migrate"],
                    "goal": f"Replace all uses of `legacy.{old}(` with `modern.{new}(` in app.py. The behaviour must not change.",
                    "fixture": {"files": {"legacy.py": legacy, "modern.py": modern, "app.py": _app("legacy", old, params), "tests/test_app.py": tests}},
                    "budget": {"calls": 16, "tokens": 60000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "migrate", "pattern": f"legacy.{old}("}, {"type": "tests_pass"},
                              {"type": "file_contains", "path": "app.py", "needles": [rf"\b{new}\("], "regex": True},     # modern.x( or a from-import: both are the new call
                              {"type": "functions_present", "path": "app.py", "names": ["first", "both", "tagged"]},
                              {"type": "files_unchanged", "paths": ["legacy.py", "modern.py", "tests/test_app.py"]}],
                    "solution": {"files": {"app.py": _app("modern", new, params)}},
                    "wrong": {"files": {"app.py": half.replace("import legacy\n", "import legacy\nimport modern\n", 1)}}})
    return out
