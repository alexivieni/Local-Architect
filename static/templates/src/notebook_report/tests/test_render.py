import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.__main__ import main  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_example_analysis_renders_html_and_notebook(tmp_path):
    html, nb = tmp_path / "r.html", tmp_path / "r.ipynb"
    assert main([os.path.join(ROOT, "analysis.py"), "--out", str(html), "--ipynb", str(nb)]) == 0
    page = html.read_text(encoding="utf-8")
    assert "<h2>Coffee sales</h2>" in page and "<svg" in page and "best day: Fri (74)" in page
    data = json.loads(nb.read_text(encoding="utf-8"))
    assert data["nbformat"] == 4 and {c["cell_type"] for c in data["cells"]} == {"code", "markdown"}
    assert any(o.get("output_type") == "stream" for c in data["cells"] if c["cell_type"] == "code" for o in c["outputs"])


def test_failing_analysis_exits_1(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("# %%\nraise ValueError('boom')\n", encoding="utf-8")
    assert main([str(src), "--out", str(tmp_path / "r.html")]) == 1
    assert "boom" in (tmp_path / "r.html").read_text(encoding="utf-8")
