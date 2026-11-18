"""deps: the dependency flow checks every import against what is installed (code, not opinion), has a reviewer comment on it and writes
docs/dependencies.md; it changes nothing. The judges read the written report: it exists, it names what the code found (the missing
package, or that all is installed), the reviewer's section is not empty, and no source file was touched. The fixtures import a package
that cannot be installed on any machine (zxqv_clientlib), so the answer never depends on this laptop."""
import _bank as B

FLOW = "deps"
DEFAULT_N = 4

APP_MISSING = 'import json\nimport zxqv_clientlib\n\n\ndef fetch(url):\n    """The parsed JSON at url."""\n    return json.loads(zxqv_clientlib.get(url))\n'
APP_CLEAN = 'import json\nimport re\n\nimport helpers\n\n\ndef parse(text):\n    """The JSON in text with its keys cleaned."""\n    return {helpers.clean(k): v for k, v in json.loads(re.sub(r"\\s+", " ", text)).items()}\n'
HELPERS = 'def clean(key):\n    """The key stripped and lower-cased."""\n    return key.strip().lower()\n'
SHAPES_TEST = 'import unittest\nfrom shapes import area\n\n\nclass T(unittest.TestCase):\n    def test_area(self):\n        self.assertEqual(area(2, 3), 6)\n'

CASES = [
    ("missing-undeclared", {"app.py": APP_MISSING}, ["zxqv_clientlib", "NOT installed"]),
    ("missing-declared", {"app.py": APP_MISSING, "requirements.txt": "zxqv_clientlib>=1.0\n"}, ["zxqv_clientlib", "NOT installed"]),
    ("all-installed", {"app.py": APP_CLEAN, "helpers.py": HELPERS}, ["every import the code uses is installed"]),
    ("tests-import-own-module", {"app.py": APP_CLEAN, "helpers.py": HELPERS, "tests/test_shapes.py": SHAPES_TEST}, ["shapes", "do not exist yet"]),
]


def generate(n, seed=0):
    rng = B.rng_for(seed, "deps")
    out = []
    for i, (name, files, needles) in enumerate(B.pick(rng, CASES, n)):
        stub = "# Dependencies\n\nNothing to report.\n\n## Review\n\nAll good.\n"
        out.append({"id": f"deps-{name}-{i:02d}", "tags": ["deps"],
                    "goal": f"Check the dependencies of this project ({name}) and write docs/dependencies.md. Change no source file.",
                    "fixture": {"files": dict(files)},
                    "budget": {"calls": 8, "tokens": 40000, "seconds": 600},
                    "judge": [{"type": "file_exists", "paths": ["docs/dependencies.md"]},
                              {"type": "file_contains", "path": "docs/dependencies.md", "needles": needles},
                              {"type": "file_contains", "path": "docs/dependencies.md", "needles": [r"## Review\s+\S"], "regex": True},
                              {"type": "files_unchanged", "paths": sorted(files)}],
                    "oracle": {"default": "- Declared: see requirements.txt if present.\n- Unused: none found.\n- Risk: none beyond the list above."},
                    "solution": {"files": {}},            # the flow writes the report itself; the oracle only answers the reviewer step
                    "wrong": {"files": {"docs/dependencies.md": stub}}})
    return out
