import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.render import render  # noqa: E402


def test_render_lines_up_columns():
    text = render(((2, 0, 0, 0), (0, 128, 0, 0), (0, 0, 0, 0), (0, 0, 0, 2048)), score=12)
    lines = text.splitlines()
    assert lines[0] == "score: 12"
    assert len({len(line) for line in lines[1:]}) == 1 and "2048" in text


def test_loop_quits_on_q():
    shown = []
    keys = iter(["d", "s", "q"])
    assert main(["--seed", "1"], read=lambda _: next(keys), write=shown.append) == 0
    assert len(shown) == 3
