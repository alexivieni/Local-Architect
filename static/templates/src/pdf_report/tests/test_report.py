import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.report import load, totals  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "samples", "sales.csv")


def test_totals_sorted_by_revenue():
    assert totals(load(SAMPLE), "region")[0] == ("South", 5550.0)


def test_cli_writes_a_pdf(tmp_path, capsys):
    out = tmp_path / "r.pdf"
    assert main([SAMPLE, "--out", str(out), "--title", "Sales (Nov)"]) == 0
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and b"/Title (Sales \\(Nov\\))" in data and len(data) > 800


def test_missing_column_is_an_error(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    assert main([str(p), "--out", str(tmp_path / "x.pdf")]) == 1
