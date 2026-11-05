"""Walk a tree, run the rules, apply a baseline."""
import json
import os

from .pyrules import scan_python
from .secrets import scan_text

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}
TEXT_EXT = {".py", ".txt", ".js", ".ts", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env", ".md", ".sh"}
MAX_BYTES = 1_000_000


def iter_files(root):
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            p = os.path.join(folder, f)
            ext = os.path.splitext(f)[1].lower()
            if (ext in TEXT_EXT or f.startswith(".env")) and os.path.getsize(p) <= MAX_BYTES:
                yield p


def scan_tree(root):
    found = []
    for p in iter_files(root):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
        found += scan_text(rel, text)
        if rel.endswith(".py") or rel.endswith(".py.txt"):
            found += scan_python(rel, text)
    return sorted(found, key=lambda x: (x.path, x.line, x.rule))


def load_baseline(path):
    with open(path, encoding="utf-8") as f:
        return set(json.load(f))


def write_baseline(path, findings):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted({x.fingerprint() for x in findings}), f, indent=2)


def new_only(findings, baseline):
    return [x for x in findings if x.fingerprint() not in baseline]
