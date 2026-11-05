import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.validate import note  # noqa: E402


def test_good_note_is_trimmed():
    assert note({"title": "  Hi ", "body": "x"}) == ({"title": "Hi", "body": "x"}, {})


def test_problems_are_reported_per_field():
    clean, errors = note({"title": "", "body": 5})
    assert clean is None and errors == {"title": "required", "body": "must be text"}
    assert note({"title": "x" * 121})[1]["title"].startswith("at most")
    assert note([1, 2])[1] == {"_": "body must be a JSON object"}
