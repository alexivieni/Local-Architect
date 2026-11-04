"""secure: real static findings (hardcoded secret, shell=True, weak hash, unsafe deserialisation) fixed without changing behaviour; the same scanner re-run proves it."""
import _bank as B

FLOW = "secure"
DEFAULT_N = 6
CASES = [
    ("hardcoded-secret",
     "import hashlib\n\nAPI_KEY = \"sk-live-0123456789abcdef0123456789\"\n\n\ndef sign(msg, key=None):\n    return hashlib.sha256((msg + (key or API_KEY)).encode()).hexdigest()\n\n\ndef add(a, b):\n    return a + b\n",
     "import hashlib\nimport os\n\n\ndef sign(msg, key=None):\n    key = key or os.environ.get(\"API_KEY\", \"\")\n    return hashlib.sha256((msg + key).encode()).hexdigest()\n\n\ndef add(a, b):\n    return a + b\n",
     "from app import sign\nassert sign('a', 'k') == sign('a', 'k') and len(sign('a', 'k')) == 64"),
    ("shell-true",
     "import subprocess\n\n\ndef list_dir(path):\n    return subprocess.run(\"ls \" + path, shell=True, capture_output=True, text=True).stdout\n\n\ndef add(a, b):\n    return a + b\n",
     "import os\n\n\ndef list_dir(path):\n    return \"\\n\".join(sorted(os.listdir(path))) + \"\\n\"\n\n\ndef add(a, b):\n    return a + b\n",
     "from app import add\nassert add(1, 2) == 3"),
    ("weak-hash",
     "import hashlib\n\n\ndef fingerprint(data: bytes) -> str:\n    return hashlib.md5(data).hexdigest()\n\n\ndef add(a, b):\n    return a + b\n",
     "import hashlib\n\n\ndef fingerprint(data: bytes) -> str:\n    return hashlib.sha256(data).hexdigest()\n\n\ndef add(a, b):\n    return a + b\n",
     "from app import fingerprint, add\nassert len(fingerprint(b'x')) == 64 and add(1, 2) == 3"),
    ("pickle-load",
     "import pickle\n\n\ndef load_settings(path):\n    with open(path, 'rb') as f:\n        return pickle.load(f)\n\n\ndef add(a, b):\n    return a + b\n",
     "import json\n\n\ndef load_settings(path):\n    with open(path, 'r', encoding='utf-8') as f:\n        return json.load(f)\n\n\ndef add(a, b):\n    return a + b\n",
     "from app import add\nassert add(1, 2) == 3"),
    ("eval-input",
     "def calc(expr):\n    return eval(expr)\n\n\ndef add(a, b):\n    return a + b\n",
     "import ast\nimport operator\n\n_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}\n\n\ndef _ev(node):\n    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):\n        return node.value\n    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:\n        return _OPS[type(node.op)](_ev(node.left), _ev(node.right))\n    raise ValueError(\"unsupported expression\")\n\n\ndef calc(expr):\n    return _ev(ast.parse(expr, mode=\"eval\").body)\n\n\ndef add(a, b):\n    return a + b\n",
     "from app import calc, add\nassert calc('2+3*4') == 14 and add(1, 2) == 3"),
    ("random-token",
     "import random\n\n\ndef new_token():\n    return \"%032x\" % random.getrandbits(128)\n\n\ndef add(a, b):\n    return a + b\n",
     "import secrets\n\n\ndef new_token():\n    return secrets.token_hex(16)\n\n\ndef add(a, b):\n    return a + b\n",
     "from app import new_token, add\nassert len(new_token()) == 32 and add(1, 2) == 3"),
]
TEST = "import unittest\nfrom app import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"


def generate(n, seed=0):
    rng = B.rng_for(seed, "secure")
    out = []
    for i, (name, bad, good, hidden) in enumerate(B.pick(rng, CASES, n)):
        out.append({"id": f"secure-{name}-{i:02d}", "tags": ["security"], "goal": f"Fix the security findings in app.py ({name}) without changing what it does.",
                    "fixture": {"files": {"app.py": bad, "tests/test_app.py": TEST}},
                    "budget": {"calls": 16, "tokens": 50000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "sec", "severity": "medium", "files": ["app.py"]}, {"type": "python_assert", "code": hidden}, {"type": "tests_pass"}],
                    "solution": {"files": {"app.py": good}}, "wrong": {"files": {"app.py": bad}}})
    return out
