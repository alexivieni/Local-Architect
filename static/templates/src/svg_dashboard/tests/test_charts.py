import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.charts import bar_chart, line_chart, sparkline  # noqa: E402
from __PKG__.scale import linear, nice_ticks  # noqa: E402

NS = "{http://www.w3.org/2000/svg}"


def parse(svg):
    return ET.fromstring(svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1))


def test_nice_ticks_cover_the_range_on_1_2_5_steps():
    t = nice_ticks(0, 87)
    assert t[0] <= 0 and t[-1] >= 87 and len({round(b - a, 6) for a, b in zip(t, t[1:])}) == 1
    assert nice_ticks(0, 87) == [0, 20, 40, 60, 80, 100]
    assert nice_ticks(5, 5)[-1] > 5


def test_linear_maps_ends():
    f = linear(0, 10, 100, 0)
    assert f(0) == 100 and f(10) == 0 and f(5) == 50


def test_line_chart_is_valid_svg_with_one_polyline_per_series():
    root = parse(line_chart(["a", "b", "c"], {"x": [1, 5, 3], "y": [2, 2, 2]}, title="T & U"))
    lines = root.findall(f".//{NS}polyline")
    assert len(lines) == 2 and len(lines[0].get("points").split()) == 3
    assert root.find(f"{NS}title").text == "T & U"


def test_bars_are_to_scale():
    root = parse(bar_chart([("a", 50), ("b", 100)]))
    h = [float(r.get("height")) for r in root.findall(f".//{NS}rect")]
    assert abs(h[1] - 2 * h[0]) < 0.5


def test_labels_are_escaped_and_sparkline_needs_two_points():
    assert "<script>" not in bar_chart([("<script>", 1)])
    assert sparkline([1]) == "" and sparkline([1, 2]).startswith("<svg")
