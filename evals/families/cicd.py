"""cicd: a weak GitHub Actions workflow must be repaired: no blocking findings, the test step kept, failures not swallowed."""
import _bank as B

FLOW = "cicd"
DEFAULT_N = 6
TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
BAD = [
    ("swallowed-failure", "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@main\n      - run: python -m unittest discover -s tests -q || true\n"),
    ("unpinned-action", "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@main\n      - uses: actions/setup-python@main\n      - run: python -m unittest discover -s tests -q\n"),
    ("continue-on-error", "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: python -m unittest discover -s tests -q\n        continue-on-error: true\n"),
    ("script-injection", "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo \"${{ github.event.pull_request.title }}\"\n      - run: python -m unittest discover -s tests -q\n"),
    ("fake-green", "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo \"tests passed\"\n"),
    ("no-permissions-and-main", "name: CI\non: [push]\npermissions: write-all\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@main\n      - run: python -m unittest discover -s tests -q || true\n"),
]
GOOD = "name: CI\non: [push]\npermissions:\n  contents: read\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n      - run: python -m unittest discover -s tests -q\n"


def generate(n, seed=0):
    rng = B.rng_for(seed, "cicd")
    out = []
    for i, (name, wf) in enumerate(B.pick(rng, BAD, n)):
        out.append({"id": f"cicd-{name}-{i:02d}", "tags": ["ci"], "goal": f"Fix the CI pipeline ({name}): it has security and reliability problems.",
                    "fixture": {"files": {".github/workflows/ci.yml": wf, "tests/test_a.py": TEST}},
                    "budget": {"calls": 16, "tokens": 50000, "seconds": 600},
                    "judge": [{"type": "ci_no_blocking"}, {"type": "file_contains", "path": ".github/workflows/ci.yml", "needles": ["unittest"]},
                              {"type": "file_lacks", "path": ".github/workflows/ci.yml", "needles": ["|| true", "continue-on-error: true", "echo \"tests passed\""]}],
                    "solution": {"files": {".github/workflows/ci.yml": GOOD}}, "oracle": {"ci": {".github/workflows/ci.yml": GOOD}},
                    "wrong": {"files": {".github/workflows/ci.yml": GOOD.replace("      - run: python -m unittest discover -s tests -q\n", "      - run: echo hi\n")}}})
    return out
