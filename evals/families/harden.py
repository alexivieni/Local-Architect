"""harden: error handling that hides failures (bare except, swallowed errors, no timeout, shell=True, assert as validation, a file opened
outside `with`) must be fixed. The flow's own AST scan (via the scanner_zero judge) finds none left, the tests still pass, the function is
still there, and a case-specific text check shows the real fix and not a deletion."""
import _bank as B

FLOW = "harden"
DEFAULT_N = 6

CASES = [
    {"name": "bare-except", "mod": "ports", "fn": "parse_port",
     "bad": '''"""Parse a TCP port number from text."""


def parse_port(text):
    """The port as an int, or None when text is not a valid port."""
    try:
        port = int(text)
    except:
        return None
    if 0 < port < 65536:
        return port
    return None
''',
     "tests": '''import unittest
from ports import parse_port


class T(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_port("8080"), 8080)

    def test_not_a_number(self):
        self.assertIsNone(parse_port("http"))

    def test_out_of_range(self):
        self.assertIsNone(parse_port("70000"))


if __name__ == "__main__":
    unittest.main()
''',
     "wrong": '''"""Parse a TCP port number from text."""


def parse_port(text):
    """The port as an int, or None when text is not a valid port."""
    return None
''',
     "lacks": ["except:"], "has": []},
    {"name": "swallowed-error", "mod": "settings", "fn": "load_settings",
     "bad": '''"""Load settings from a JSON file."""
import json


def load_settings(path):
    """The settings dict, or an empty dict when the file cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    return {}
''',
     "tests": '''import json
import os
import tempfile
import unittest
from settings import load_settings


class T(unittest.TestCase):
    def test_reads_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"a": 1}, f)
            self.assertEqual(load_settings(p), {"a": 1})

    def test_missing_file_gives_empty(self):
        self.assertEqual(load_settings(os.path.join(tempfile.gettempdir(), "no_such_settings_file.json")), {})

    def test_broken_json_gives_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(load_settings(p), {})


if __name__ == "__main__":
    unittest.main()
''',
     "wrong": '''"""Load settings from a JSON file."""


def load_settings(path):
    """The settings dict, or an empty dict when the file cannot be read."""
    return {}
''',
     "lacks": [], "has": []},
    {"name": "no-timeout", "mod": "fetcher", "fn": "fetch_text",
     "bad": '''"""Download a text page."""
import urllib.request


def fetch_text(url):
    """The page body decoded as UTF-8."""
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")
''',
     "tests": '''import unittest
from unittest import mock
from fetcher import fetch_text


class T(unittest.TestCase):
    def test_returns_the_decoded_body(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"hello"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(fetch_text("http://example.invalid/"), "hello")


if __name__ == "__main__":
    unittest.main()
''',
     "wrong": '''"""Download a text page."""


def fetch_text(url):
    """The page body decoded as UTF-8."""
    return ""
''',
     "lacks": [], "has": ["timeout"]},
    {"name": "shell-true", "mod": "runner", "fn": "shout",
     "bad": '''"""Run a tiny Python snippet in a child process."""
import subprocess
import sys


def shout(word):
    """Upper-case word in a child Python process and return what it printed."""
    cmd = f'"{sys.executable}" -c "import sys; print(sys.argv[1].upper())" {word}'
    done = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return done.stdout.strip()
''',
     "tests": '''import unittest
from runner import shout


class T(unittest.TestCase):
    def test_shout(self):
        self.assertEqual(shout("hello"), "HELLO")


if __name__ == "__main__":
    unittest.main()
''',
     "wrong": '''"""Run a tiny Python snippet in a child process."""


def shout(word):
    """Upper-case word in a child Python process and return what it printed."""
    return word
''',
     "lacks": ["shell=True"], "has": []},
    {"name": "assert-for-validation", "mod": "person", "fn": "set_age",
     "bad": '''"""A tiny profile record."""


def set_age(profile, age):
    """Store age on the profile; ages must not be negative."""
    assert age >= 0, "age must not be negative"
    profile["age"] = age
    return profile
''',
     "tests": '''import unittest
from person import set_age


class T(unittest.TestCase):
    def test_stores_the_age(self):
        self.assertEqual(set_age({}, 30), {"age": 30})

    def test_negative_age_is_refused(self):
        with self.assertRaises((AssertionError, ValueError)):
            set_age({}, -1)


if __name__ == "__main__":
    unittest.main()
''',
     "wrong": '''"""A tiny profile record."""


def set_age(profile, age):
    """Store age on the profile; ages must not be negative."""
    return profile
''',
     "lacks": ["assert "], "has": ["raise "]},
    {"name": "open-without-with", "mod": "notes", "fn": "read_notes",
     "bad": '''"""Read a notes file."""


def read_notes(path):
    """The file's text, stripped."""
    f = open(path, encoding="utf-8")
    text = f.read()
    f.close()
    return text.strip()
''',
     "tests": '''import os
import tempfile
import unittest
from notes import read_notes


class T(unittest.TestCase):
    def test_reads_and_strips(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "n.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("  hello \\n")
            self.assertEqual(read_notes(p), "hello")


if __name__ == "__main__":
    unittest.main()
''',
     "wrong": '''"""Read a notes file."""


def read_notes(path):
    """The file's text, stripped."""
    return ""
''',
     "lacks": [], "has": ["with open"]},
]

GOOD = {
    "bare-except": ("    except:\n        return None\n", "    except ValueError:\n        return None\n"),
    "swallowed-error": ("    except Exception:\n        pass\n    return {}\n", "    except (OSError, ValueError):\n        return {}\n"),
    "no-timeout": ("urllib.request.urlopen(url)", "urllib.request.urlopen(url, timeout=10)"),
    "shell-true": ('''    cmd = f'"{sys.executable}" -c "import sys; print(sys.argv[1].upper())" {word}'\n    done = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)\n''',
                   '''    cmd = [sys.executable, "-c", "import sys; print(sys.argv[1].upper())", word]\n    done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)\n'''),
    "assert-for-validation": ('    assert age >= 0, "age must not be negative"\n', '    if age < 0:\n        raise ValueError("age must not be negative")\n'),
    "open-without-with": ('    f = open(path, encoding="utf-8")\n    text = f.read()\n    f.close()\n', '    with open(path, encoding="utf-8") as f:\n        text = f.read()\n'),
}


def fixed(case):
    """The reference solution: the bad module with exactly the flaw replaced."""
    old, new = GOOD[case["name"]]
    assert old in case["bad"], case["name"]
    return case["bad"].replace(old, new)


def generate(n, seed=0):
    rng = B.rng_for(seed, "harden")
    out = []
    for i, c in enumerate(B.pick(rng, CASES, n)):
        mod, path = c["mod"], f"{c['mod']}.py"
        judge = [{"type": "scanner_zero", "kind": "harden", "files": [path]}, {"type": "tests_pass"},
                 {"type": "functions_present", "path": path, "names": [c["fn"]]}]
        if c["lacks"]:
            judge.append({"type": "file_lacks", "path": path, "needles": c["lacks"]})
        if c["has"]:
            judge.append({"type": "file_contains", "path": path, "needles": c["has"]})
        out.append({"id": f"harden-{c['name']}-{i:02d}", "tags": ["harden"],
                    "goal": f"Harden the error handling in {path} ({c['name']}): it hides failures or can hang. Keep what the function returns.",
                    "fixture": {"files": {path: c["bad"], B.test_path(mod): c["tests"]}},
                    "budget": {"calls": 16, "tokens": 60000, "seconds": 600},
                    "judge": judge,
                    "solution": {"files": {path: fixed(c)}},
                    "wrong": {"files": {path: c["wrong"]}}})
    return out
