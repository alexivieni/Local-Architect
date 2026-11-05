import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__.__main__ import main  # noqa: E402
from __PKG__.sync import Action, apply, plan  # noqa: E402


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def dirs(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    write(src, "a.txt", "alpha")
    write(src, "sub/b.txt", "beta")
    write(src, "junk.tmp", "x")
    return src, dest


def test_first_sync_copies_everything_but_ignored(dirs):
    src, dest = dirs
    acts = plan(src, dest, ["*.tmp"])
    assert acts == [Action("copy", "a.txt"), Action("copy", "sub/b.txt")]
    apply(src, dest, acts, ["*.tmp"])
    assert (dest / "sub" / "b.txt").read_text(encoding="utf-8") == "beta" and not (dest / "junk.tmp").exists()
    assert plan(src, dest, ["*.tmp"]) == []


def test_same_size_content_change_is_caught_by_hash(dirs):
    src, dest = dirs
    apply(src, dest, plan(src, dest))
    write(dest, "a.txt", "ALPHA")                   # same size, different bytes, edited on the destination
    assert Action("update", "a.txt") in plan(src, dest)


def test_delete_only_when_asked(dirs):
    src, dest = dirs
    apply(src, dest, plan(src, dest))
    (src / "a.txt").unlink()
    assert plan(src, dest) == []
    assert plan(src, dest, delete=True) == [Action("delete", "a.txt")]


def test_nested_roots_are_refused(dirs):
    src, _ = dirs
    with pytest.raises(ValueError):
        plan(src, src / "inner")
    with pytest.raises(ValueError):
        plan(src / "missing", src.parent / "x")


def test_dry_run_changes_nothing(dirs, capsys):
    src, dest = dirs
    assert main([str(src), str(dest), "--dry-run"]) == 0
    assert "copy   a.txt" in capsys.readouterr().out and not dest.exists()
    assert main([str(src), str(dest)]) == 0 and (dest / "a.txt").exists()
    assert main([str(src), str(src / "x")]) == 2
