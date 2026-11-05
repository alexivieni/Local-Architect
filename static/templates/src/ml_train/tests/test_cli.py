import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_train_then_predict(tmp_path, capsys):
    out = str(tmp_path / "m.json")
    assert main(["train", os.path.join(ROOT, "data", "sample.csv"), "--out", out]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["accuracy"] >= 0.8 and rep["test_rows"] == 6
    assert main(["predict", out, "2.5", "2.5"]) == 0
    assert json.loads(capsys.readouterr().out)["label"] == 1
    assert main(["predict", out, "-2.5", "-2.5"]) == 0
    assert json.loads(capsys.readouterr().out)["label"] == 0


def test_wrong_value_count_is_usage_error(tmp_path, capsys):
    out = str(tmp_path / "m.json")
    main(["train", os.path.join(ROOT, "data", "sample.csv"), "--out", out])
    capsys.readouterr()
    assert main(["predict", out, "1"]) == 2
