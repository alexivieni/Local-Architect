import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__ import convert as c  # noqa: E402


def test_text_and_case():
    assert c.text("  ada   LOVELACE ", "title") == "Ada Lovelace" and c.text(" a  b ") == "a b"


@pytest.mark.parametrize("raw,want", [("1,234.50", "1234.5"), ("12%", "0.12"), (" 7 ", "7"), ("-3.25", "-3.25")])
def test_numbers(raw, want):
    assert c.number(raw) == want


def test_integers_refuse_fractions():
    assert c.integer("1,000") == "1000"
    with pytest.raises(ValueError):
        c.integer("2.5")


@pytest.mark.parametrize("raw", ["2026-01-05", "2026/01/05", "05/01/2026", "5 Jan 2026", "Jan 05, 2026"])
def test_dates_become_iso(raw):
    assert c.date(raw) == "2026-01-05"


def test_bool_email_and_empties():
    assert c.boolean("Y") == "true" and c.boolean("no") == "false"
    assert c.email(" ADA@Example.com ") == "ada@example.com"
    assert c.is_empty(" N/A ") and not c.is_empty("0")
    for fn, bad in ((c.boolean, "maybe"), (c.email, "alan@example"), (c.date, "not a date"), (c.number, "abc")):
        with pytest.raises(ValueError):
            fn(bad)
