import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.pyrules import scan_python  # noqa: E402
from __PKG__.secrets import entropy, mask, scan_text  # noqa: E402


def rules(found):
    return sorted(f.rule for f in found)


def test_key_shapes_are_found_and_masked():
    fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"          # built at run time so this file itself is not a finding
    found = scan_text("x.env", f"KEY={fake_key}\n")
    assert rules(found) == ["SEC001"] and fake_key not in found[0].message


def test_password_literal_vs_environment():
    assert rules(scan_text("a.py", 'password = "letmein1"')) == ["SEC002"]
    assert scan_text("a.py", 'password = os.environ["PW"]') == []


def test_entropy_separates_random_from_words():
    assert entropy("aaaaaaaa") == 0.0 and entropy("Zx8qP2vL9rT4mW7nB3kD") > 4.0
    assert mask("abcdefghij") == "ab******ij"


def test_python_rules_hit_and_near_miss():
    bad = "import subprocess, pickle, hashlib, yaml\nsubprocess.run(c, shell=True)\npickle.loads(b)\nhashlib.md5(b)\nyaml.load(s)\neval(x)\nrequests.get(u, verify=False)\n"
    assert rules(scan_python("a.py", bad)) == ["PY001", "PY002", "PY003", "PY003", "PY004", "PY005"]
    ok = "import subprocess, yaml, hashlib\nsubprocess.run(['ls'], shell=False)\nyaml.load(s, Loader=yaml.SafeLoader)\nhashlib.sha256(b)\n# eval(x) in a comment\ns = 'eval(x)'\n"
    assert scan_python("a.py", ok) == []


def test_unparseable_python_is_skipped():
    assert scan_python("a.py", "def (:") == []
