import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__ import data, metrics, model  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_sigmoid_is_stable_at_extremes():
    assert model.sigmoid(1000) == 1.0 and model.sigmoid(-1000) == 0.0 and model.sigmoid(0) == 0.5


def test_fit_separates_the_sample_and_loss_falls():
    _, X, y = data.load_csv(os.path.join(ROOT, "data", "sample.csv"), "label")
    s = data.fit_scaler(X)
    w, b, hist = model.fit(data.scale(X, s), y)
    preds = [int(model.predict_proba(w, b, r) >= 0.5) for r in data.scale(X, s)]
    assert metrics.report(y, preds)["accuracy"] >= 0.95
    assert hist[-1] < hist[0]


def test_scaler_uses_only_given_rows_and_handles_constant_columns():
    s = data.fit_scaler([[1.0, 5.0], [3.0, 5.0]])
    assert s["mean"] == [2.0, 5.0] and s["std"] == [1.0, 1.0]


def test_split_is_seeded_and_disjoint():
    X = [[float(i)] for i in range(20)]
    y = [i % 2 for i in range(20)]
    a = data.split(X, y, seed=3)
    assert a == data.split(X, y, seed=3)
    assert len(a[0]) + len(a[2]) == 20 and not {r[0] for r in a[0]} & {r[0] for r in a[2]}


def test_bad_csv_is_refused(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("a,label\nx,1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        data.load_csv(str(p), "label")
    with pytest.raises(ValueError):
        data.load_csv(str(p), "missing")


def test_metrics_edge_cases():
    r = metrics.report([1, 0, 1, 0], [0, 0, 0, 0])
    assert r["precision"] == 0.0 and r["recall"] == 0.0 and r["accuracy"] == 0.5
