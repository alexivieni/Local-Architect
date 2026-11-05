"""Scan, plan, apply. The plan is plain data so it can be printed, tested and reviewed before apply()."""
import fnmatch
import hashlib
import json
import os
import shutil
from dataclasses import dataclass

MANIFEST = ".sync-manifest.json"


@dataclass(frozen=True)
class Action:
    op: str          # copy / update / delete
    path: str        # relative, forward slashes


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def ignored(rel, patterns):
    return rel == MANIFEST or any(fnmatch.fnmatch(rel, p) for p in patterns)


def scan(root, patterns=()):
    """-> {relative path: (size, mtime_ns)}"""
    out = {}
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            full = os.path.join(folder, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not ignored(rel, patterns) and not os.path.islink(full):
                st = os.stat(full)
                out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def check_roots(src, dest):
    s, d = os.path.realpath(src), os.path.realpath(dest)
    if not os.path.isdir(s):
        raise ValueError(f"source is not a folder: {src}")
    if s == d or d.startswith(s + os.sep) or s.startswith(d + os.sep):
        raise ValueError("source and destination must not contain each other")


def load_manifest(dest):
    try:
        with open(os.path.join(dest, MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def plan(src, dest, patterns=(), delete=False):
    check_roots(src, dest)
    a, b = scan(src, patterns), scan(dest, patterns) if os.path.isdir(dest) else {}
    manifest = load_manifest(dest)
    actions = []
    for rel, (size, mtime) in a.items():
        if rel not in b:
            actions.append(Action("copy", rel))
            continue
        known = manifest.get(rel)
        if b[rel][0] != size:
            actions.append(Action("update", rel))
        elif known and known["size"] == size and known["mtime_ns"] == mtime and known["dest_mtime_ns"] == b[rel][1]:
            continue                                  # unchanged since the last sync: no hashing needed
        elif sha256(os.path.join(src, rel)) != sha256(os.path.join(dest, rel)):
            actions.append(Action("update", rel))
    if delete:
        actions += [Action("delete", rel) for rel in b if rel not in a]
    return sorted(actions, key=lambda x: (x.op, x.path))


def _copy_atomic(src_file, dest_file):
    os.makedirs(os.path.dirname(dest_file) or ".", exist_ok=True)
    tmp = dest_file + ".sync-tmp"
    shutil.copy2(src_file, tmp)
    os.replace(tmp, dest_file)


def apply(src, dest, actions, patterns=()):
    os.makedirs(dest, exist_ok=True)
    for act in actions:
        target = os.path.join(dest, *act.path.split("/"))
        if act.op == "delete":
            os.remove(target)
        else:
            _copy_atomic(os.path.join(src, *act.path.split("/")), target)
    a, b = scan(src, patterns), scan(dest, patterns)
    manifest = {rel: {"size": s, "mtime_ns": m, "dest_mtime_ns": b[rel][1]} for rel, (s, m) in a.items() if rel in b}
    with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
