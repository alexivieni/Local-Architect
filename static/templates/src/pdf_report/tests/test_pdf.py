import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.layout import Flow, wrap  # noqa: E402
from __PKG__.pdf import Document, escape  # noqa: E402


def check_structure(data):
    """The xref table must point at each 'N 0 obj' exactly: readers depend on it."""
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[start:start + 4] == b"xref"
    count = int(re.search(rb"xref\n0 (\d+)", data).group(1))
    entries = re.findall(rb"(\d{10}) 00000 n ", data[start:])
    assert len(entries) == count - 1
    for i, off in enumerate(entries, start=1):
        assert data[int(off):].startswith(f"{i} 0 obj".encode())


def test_empty_document_is_valid():
    check_structure(Document().to_bytes())


def test_text_is_escaped_and_stream_decompresses():
    d = Document(compress=True)
    d.new_page().text(50, 800, "a (tricky) \\ line")
    data = d.to_bytes()
    check_structure(data)
    stream = re.search(rb"stream\n(.*?)\nendstream", data, re.S).group(1)
    assert b"a \\(tricky\\) \\\\ line" in zlib.decompress(stream)
    assert escape("()") == "\\(\\)"


def test_wrap_respects_width_and_keeps_words():
    words = "the quick brown fox jumps over the lazy dog " * 5
    lines = wrap(words, 10, 120)
    assert all(len(line) * 5 <= 120 for line in lines) and " ".join(lines).split() == words.split()


def test_long_content_breaks_onto_new_pages():
    f = Flow("t")
    for i in range(80):
        f.paragraph(f"paragraph {i} " * 6)
    assert len(f.doc.pages) > 2
    check_structure(f.doc.to_bytes())
