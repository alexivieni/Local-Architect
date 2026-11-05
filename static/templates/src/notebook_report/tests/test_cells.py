import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.cells import bar, run, split, to_html  # noqa: E402


def test_split_finds_code_and_markdown_cells():
    cells = split("x = 1\n# %% [markdown]\n# # Title\n# %%\nprint(x)\n# %%\n\n")
    assert [c.kind for c in cells] == ["code", "markdown", "code"]


def test_cells_share_a_namespace_and_capture_output():
    cells = split("# %%\nx = 2\n# %%\nprint(x * 21)\nshow([{'a': 1}])\n")
    assert run(cells)
    assert cells[1].stdout == "42\n" and cells[1].rich[0].startswith("<table>")


def test_a_failing_cell_is_recorded_and_stops_the_run():
    cells = split("# %%\nprint('before')\n1/0\n# %%\nprint('never')\n")
    assert run(cells) is False
    assert "ZeroDivisionError" in cells[0].error and cells[0].stdout == "before\n" and cells[1].stdout == ""


def test_bar_is_valid_svg_and_tables_escape():
    ET.fromstring(bar({"a": 1, "b<": 3}))
    assert "&lt;b&gt;" in to_html([{"x": "<b>"}])
