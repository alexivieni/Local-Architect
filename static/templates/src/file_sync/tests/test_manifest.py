import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__ import sync  # noqa: E402


def test_manifest_skips_hashing_unchanged_files(tmp_path, monkeypatch):
    src, dest = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    (src / "f.bin").write_bytes(b"x" * 100)
    sync.apply(src, dest, sync.plan(src, dest))
    m = json.loads((dest / sync.MANIFEST).read_text(encoding="utf-8"))
    assert set(m) == {"f.bin"}
    calls = []
    monkeypatch.setattr(sync, "sha256", lambda p, chunk=0: calls.append(p) or "h")
    assert sync.plan(src, dest) == [] and calls == []


def test_manifest_is_never_synced_itself(tmp_path):
    src, dest = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    (src / sync.MANIFEST).write_text("{}", encoding="utf-8")
    assert sync.plan(src, dest) == []
