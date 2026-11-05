import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.scan import scan_tree  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def test_bad_sample_is_flagged_and_clean_sample_is_not():
    by_file = {}
    for f in scan_tree(SAMPLES):
        by_file.setdefault(f.path, []).append(f.rule)
    assert "clean_example.py.txt" not in by_file
    assert {"SEC002", "SEC003", "PY001", "PY002", "PY003", "PY004"} <= set(by_file["bad_example.py.txt"])


def test_exit_codes_and_json(capsys):
    assert main([SAMPLES, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == 1 and data["findings"]


def test_baseline_accepts_known_and_catches_new(tmp_path, capsys):
    tree = tmp_path / "src"
    shutil.copytree(SAMPLES, tree)
    base = str(tmp_path / "base.json")
    assert main([str(tree), "--write-baseline", base]) == 0
    assert main([str(tree), "--baseline", base]) == 0
    (tree / "new.py").write_text("import os\nos.system(cmd)\n", encoding="utf-8")
    capsys.readouterr()
    assert main([str(tree), "--baseline", base]) == 1
    assert "PY002" in capsys.readouterr().out
