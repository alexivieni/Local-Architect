"""Hybrid retrieval, no vectors: SQLite FTS5 over the project's functions (arch_fts.py) fused with the TF-IDF file ranking the harness
always had. On a small context window the prompt can only carry a few files, so WHICH files and WHICH functions it carries decides whether a
small model can do the job at all. TF-IDF finds the right files by exact identifiers; the FTS5 index finds the function whose text or
identifier parts match the request (`throttle` finds `rate_limit` through "throttling requests" in its docstring; `allow` finds `allow(key)`).
Reciprocal rank fusion keeps the exact matches and adds the function-level ones. Neither side decides anything: the index only orders what the
prompt shows; the code checks still decide.

Why this and not an embedding model: nothing to download, nothing to load next to the chat model on a 6 GB card, the standard library only,
and the same index also holds the project's Markdown memory so a lesson is found by its words. Store: `.arch/index.db`.
"""
import ast
import hashlib
import os
import re

import arch_fts

EMBED_RX = re.compile(r"embed|nomic|bge|minilm|arctic|e5-|gte-|mxbai", re.I)      # still used to keep embedding-only models out of chat roles
DEFAULTS = {"mode": "hybrid",          # hybrid = TF-IDF files + FTS5 functions fused | tfidf = the file ranking only
            "chunk_lines": 60,         # non-Python files are cut into windows of this many lines; Python is cut per function/class
            "max_chunks": 20000}       # above this the project is too big to index here: the index stays off and says so
INDEX_DIR = os.path.join(".arch", "index")        # the old vector store's folder (removed on `index --rebuild`)


# ----------------------------------------------------------------------------------------------- chunks
def _py_chunks(rel, text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        body = "\n".join(lines[start:node.end_lineno])
        out.append({"path": rel, "name": node.name, "start": start + 1, "end": node.end_lineno, "text": body})
    head = "\n".join(lines[: (out[0]["start"] - 1) if out else 40]).strip()        # imports, constants, module docstring
    if head and len(head) >= 12:
        out.insert(0, {"path": rel, "name": "(module head)", "start": 1, "end": head.count("\n") + 1, "text": head})
    return out


def _window_chunks(rel, text, n):
    lines = text.splitlines()
    out = []
    for i in range(0, max(len(lines), 1), n):
        body = "\n".join(lines[i:i + n]).strip()
        if body:
            out.append({"path": rel, "name": f"lines {i + 1}-{min(i + n, len(lines))}", "start": i + 1, "end": min(i + n, len(lines)), "text": body})
    return out


def chunks_for(root, cfg, files, read_text):
    """-> [chunk] over the given project files. Each chunk gets an id = sha1(path + text): the same text in the same place is the same chunk."""
    n = int(((cfg.get("retrieval") or {}).get("chunk_lines")) or DEFAULTS["chunk_lines"])
    out = []
    for rel in files:
        try:
            text, _ = read_text(os.path.join(root, rel), 200000)
        except OSError:
            continue
        cs = (_py_chunks(rel, text) if rel.endswith(".py") else None) or _window_chunks(rel, text, n)
        for c in cs:
            c["text"] = c["text"][:6000]
            c["id"] = hashlib.sha1((rel + "\n" + c["text"]).encode("utf-8", "replace")).hexdigest()
            out.append(c)
    return out


def memory_chunks(root, read_text):
    """The project's Markdown memory as chunks (kind 'memory'), one per paragraph/bullet block, so lessons are searchable."""
    out = []
    mdir = os.path.join(root, ".arch", "memory")
    if not os.path.isdir(mdir):
        return out
    for f in sorted(os.listdir(mdir)):
        if not f.endswith(".md"):
            continue
        rel = ".arch/memory/" + f
        try:
            text, _ = read_text(os.path.join(mdir, f), 200000)
        except OSError:
            continue
        for i, block in enumerate(b for b in re.split(r"\n\s*\n", text) if b.strip()):
            body = block.strip()[:6000]
            out.append({"path": rel, "name": f"note {i + 1}", "start": 1, "end": 1, "text": body, "id": hashlib.sha1((rel + "\n" + body).encode("utf-8", "replace")).hexdigest()})
    return out


# ----------------------------------------------------------------------------------------------- fusion
def rrf(rankings, k=60):
    """Reciprocal rank fusion of several [item] lists -> [(item, score)] best first. An item high in any list ends up high."""
    score = {}
    for ranked in rankings:
        for pos, item in enumerate(ranked):
            score[item] = score.get(item, 0.0) + 1.0 / (k + pos + 1)
    return sorted(score.items(), key=lambda x: -x[1])


def hybrid(hits, tfidf_ranked, dense=None):
    """Fuse the TF-IDF file order with the FTS5 chunk hits (and, only when given, the optional dense hits from arch_embed).
    -> (files best first, {file: [chunk names the index found]})."""
    by_file, fts_files, dense_files = {}, [], []
    for c, s in dense or []:
        if c.get("kind", "code") == "code" and c["path"] not in dense_files:
            dense_files.append(c["path"])
    for c, s in hits:
        if c.get("kind", "code") != "code":
            continue
        if c["path"] not in fts_files:
            fts_files.append(c["path"])
        by_file.setdefault(c["path"], []).append(c["name"])
    tf = [f for f, s in tfidf_ranked if s > 0]                    # a zero score is "no evidence", not a rank: it must not tie with a real hit
    fused = [f for f, _ in rrf([tf, fts_files] + ([dense_files] if dense_files else []))]
    for f, _ in tfidf_ranked:                                      # files neither side ranked keep their old order at the tail
        if f not in fused:
            fused.append(f)
    return fused, by_file


# ----------------------------------------------------------------------------------------------- one entry point for the engine
class Retriever:
    """Owned by an Engine for one run: keeps the FTS5 index fresh (cheaply) and answers rank(query). TF-IDF alone when the index is off."""

    def __init__(self, root, cfg, installed=None, host=None, read_text=None, say=None):
        import arch_core
        self.root, self.cfg, self.read_text, self.say = root, cfg, read_text or arch_core.read_text, say or (lambda m: None)
        self.mode = ((cfg.get("retrieval") or {}).get("mode") or "hybrid")
        self.index = arch_fts.Index(root) if (self.mode == "hybrid" and arch_fts.available()) else None
        self.dirty, self.off_reason, self.last = True, None, None
        self.chunks = []
        try:
            import arch_embed
            self.embedder = arch_embed.for_cfg(root, cfg)          # None unless retrieval.embed_model is set
        except Exception:
            self.embedder = None
        if self.mode == "hybrid" and not arch_fts.available():
            self.off_reason = "this Python's SQLite has no FTS5; keyword ranking only"
        elif self.mode != "hybrid":
            self.off_reason = "retrieval.mode = tfidf"

    def active(self):
        return bool(self.index) and not self.off_reason

    def touch(self):
        """Files changed on disk (the engine applied an edit): refresh lazily before the next ranking."""
        self.dirty = True

    def ensure(self, files):
        if not self.index or not self.dirty:
            return
        cs = chunks_for(self.root, self.cfg, files, self.read_text)
        cap = int(((self.cfg.get("retrieval") or {}).get("max_chunks")) or DEFAULTS["max_chunks"])
        if len(cs) > cap:
            self.off_reason = f"{len(cs)} chunks is more than retrieval.max_chunks ({cap}); the index stays off for this project"
            self.last = {"new": 0, "kept": 0, "dropped": 0, "secs": 0.0, "error": self.off_reason}
            self.dirty = False
            self.say(f"    [index] {self.off_reason}; using the keyword ranking")
            return
        self.chunks = cs
        st = self.index.refresh(cs, kind="code")
        mem = memory_chunks(self.root, self.read_text)
        if mem or st.get("new") or st.get("dropped"):
            self.index.refresh(mem, kind="memory")
        self.last = st
        self.dirty = False
        if st.get("error"):
            self.off_reason = st["error"]
            self.say(f"    [index] {st['error']}; using the keyword ranking")
        elif st["new"] or st["dropped"]:
            self.say(f"    [index] {st['new']} chunk(s) indexed in {st['secs']}s ({st['kept']} reused, {st['dropped']} dropped)")

    def rank(self, query, files, tfidf_ranked):
        """-> (files best first, {file: [relevant function names]}). TF-IDF alone when the index is off."""
        if not self.active():
            return [f for f, _ in tfidf_ranked], {}
        self.ensure(files)
        if not self.active():
            return [f for f, _ in tfidf_ranked], {}
        hits = self.index.search(query[:4000], top=24)
        dense = None
        if self.embedder is not None:
            dense = self.embedder.search(query, self.chunks, top=24)       # None on any failure -> keyword-only
            if dense is None and self.embedder.error:
                self.say(f"    [index] {self.embedder.error}")
                self.embedder = None
        return hybrid(hits, tfidf_ranked, dense)

    def search(self, query, top=12, kind=None):
        """Direct chunk search (the `search` tool and the hub): -> [{"path","name","start","end","kind","score"}]. Indexes first if needed."""
        import arch_core
        if not self.index:
            return []
        self.ensure(arch_core.list_files(self.root, self.cfg))
        if not self.active():
            return []
        return [dict(c, score=s) for c, s in self.index.search(query[:4000], top=top, kind=kind)]


# ----------------------------------------------------------------------------------------------- CLI / hub helpers
def status(root, cfg, installed=None):
    """What the hub and `index` show: on/off, why, size. Reads only."""
    mode = ((cfg.get("retrieval") or {}).get("mode") or "hybrid")
    out = {"mode": mode, "model": "sqlite-fts5" if (mode == "hybrid" and arch_fts.available()) else None, "exists": False, "suggest": None}
    if mode != "hybrid":
        out["state"] = "off (retrieval.mode = tfidf)"
    elif not arch_fts.available():
        out["state"] = "off: this Python's SQLite has no FTS5"
    else:
        out["state"] = "on"
    if root and os.path.isfile(os.path.join(root, arch_fts.DB_REL)):
        out.update(arch_fts.Index(root).summary())
    return out


def compare(root, cfg, installed, host, query, read_text, list_files, rank_files, top=8):
    """For `index --query`: the TF-IDF order, the FTS5 hits and the fused order side by side, so the gain is visible, not claimed."""
    files = list_files(root, cfg)
    tf = rank_files(root, cfg, query, files)
    r = Retriever(root, cfg, installed, host, read_text)
    r.ensure(files)
    out = {"query": query, "tfidf": [f for f, s in tf[:top] if s > 0], "fts": [], "fused": [], "index": r.off_reason or "on", "refresh": r.last}
    if r.active():
        hits = r.index.search(query, 12)
        out["fts"] = [f"{c['path']}::{c['name']} ({s})" for c, s in hits[:top]]
        fused, _ = hybrid(hits, tf)
        out["fused"] = fused[:top]
    out["vector"] = out["fts"]            # the old key, kept for one release
    return out
