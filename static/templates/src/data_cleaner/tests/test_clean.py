import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.clean import clean_rows  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_sample_end_to_end(tmp_path):
    out, audit = tmp_path / "clean.csv", tmp_path / "audit.json"
    assert main([os.path.join(ROOT, "samples", "messy.csv"), "--rules", os.path.join(ROOT, "samples", "rules.json"),
                 "--out", str(out), "--audit", str(audit)]) == 0
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    a = json.load(open(audit, encoding="utf-8"))
    assert [r["name"] for r in rows] == ["Ada Lovelace", "Grace Hopper"]
    assert rows[0] == {"name": "Ada Lovelace", "email": "ada@example.com", "joined": "2026-01-05", "spend": "1234.5", "active": "true"}
    assert rows[1]["spend"] == "0.12" and rows[1]["joined"] == "2026-02-05"
    assert a["rows_in"] == 6 and a["rows_out"] == 2
    assert {r["line"] for r in a["rejected"]} == {4, 6, 7} and a["duplicates"] == [{"line": 5, "same_as": 2}]


def test_unknown_columns_pass_through_untouched():
    rows, audit = clean_rows([{"a": " x ", "extra": "  keep  "}], {"columns": {"a": {"type": "text"}}})
    assert rows == [{"a": "x", "extra": "  keep  "}] and audit["changed"] == 1


def test_bad_rules_are_refused():
    with pytest.raises(ValueError):
        clean_rows([], {"columns": {"a": {"type": "colour"}}})
    with pytest.raises(ValueError):
        clean_rows([], {"columns": {"a": {}}, "key": ["b"]})
