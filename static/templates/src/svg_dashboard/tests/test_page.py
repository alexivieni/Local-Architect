import os
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.page import build, delta  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.srcs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.srcs += [v for k, v in attrs if k in ("src", "href") and v]


def test_delta_direction():
    assert delta(110, 100) == ("up", "+10.0%") and delta(90, 100) == ("down", "-10.0%") and delta(1, 0) == ("", "")


def test_sample_page_is_self_contained(tmp_path):
    out = tmp_path / "d.html"
    assert main([os.path.join(ROOT, "samples", "metrics.json"), "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    t = Tags()
    t.feed(html)
    assert t.tags.count("svg") == 4 and "script" not in t.tags and t.srcs == []
    assert "prefers-color-scheme: dark" in html and "Weekly overview" in html


def test_title_is_escaped():
    assert "<b>" not in build({"title": "<b>x</b>"})
