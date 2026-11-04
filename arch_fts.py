"""Full-text search over a project's functions with SQLite FTS5 (standard library; no embedding model, no vectors, nothing loaded on the GPU).

The index is `.arch/index.db` inside the project: one row per chunk (a Python function/class or a window of lines for other files; the same
chunker the vector index used), keyed by a content hash so an edit re-indexes only the chunks that changed. Two indexed columns: the chunk's
text, and its identifier terms split the way the keyword ranker splits them (snake_case parts, camelCase parts, whole names), so a request
for "throttling" finds `rate_limit` through its description and `allow(key)` through its terms. Ranking is BM25; the engine fuses this order
with the file-level TF-IDF order (arch_index.hybrid) and shows the functions the hits name in full (focus(boost=...)).

Also indexed, when present: the project's Markdown memory (`.arch/memory/*.md`, kind "memory") so a lesson can be found by its words.
"""
import os
import re
import sqlite3
import time

DB_REL = os.path.join(".arch", "index.db")
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def available():
    try:
        c = sqlite3.connect(":memory:")
        c.execute("create virtual table t using fts5(x)")
        c.close()
        return True
    except sqlite3.Error:
        return False


def terms(text):
    """Identifier terms the way arch_core._terms makes them (whole lowercase names plus their camel/snake parts)."""
    out = []
    for w in _TOK.findall(text or ""):
        out.append(w.lower())
        for p in w.split("_"):
            if len(p) > 2 and p.lower() != w.lower():
                out.append(p.lower())
        out.extend(p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w) if len(p) > 2 and p.lower() != w.lower())
    return out


def _query(text, limit=24):
    """A safe FTS5 query: each distinct term quoted, OR-ed, over both columns. '' when the text has no usable term."""
    seen, out = set(), []
    for t in terms(text):
        if t in seen or len(t) > 60:
            continue
        seen.add(t)
        out.append('"' + t.replace('"', '""') + '"')
        if len(out) >= limit:
            break
    return "{text terms}: (" + " OR ".join(out) + ")" if out else ""


class Index:
    """The FTS5 index of one project. open() lazily; every method is safe to call on a project with no index yet."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, DB_REL)
        self._con = None

    def _open(self, create=False):
        """A fresh connection per operation (closed by the caller): nothing stays open between calls, so a project folder can always be
        moved or deleted on Windows and two engines never share a handle."""
        if not create and not os.path.isfile(self.path):
            return None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        con = sqlite3.connect(self.path)
        con.execute("create virtual table if not exists chunks using fts5(text, terms, id unindexed, path unindexed, name unindexed, start unindexed, end unindexed, kind unindexed, tokenize='unicode61')")
        con.execute("create table if not exists meta(key text primary key, value text)")
        return con

    def close(self):
        pass

    def ids(self):
        con = self._open()
        if con is None:
            return {}
        try:
            return {r[0]: (r[1], r[2]) for r in con.execute("select id, path, name from chunks")}
        finally:
            con.close()

    def summary(self):
        con = self._open()
        if con is None:
            return {"exists": False, "chunks": 0, "files": 0, "bytes": 0, "updated": None}
        try:
            n = con.execute("select count(*) from chunks").fetchone()[0]
            files = con.execute("select count(distinct path) from chunks").fetchone()[0]
            upd = con.execute("select value from meta where key='updated'").fetchone()
        finally:
            con.close()
        return {"exists": n > 0, "chunks": n, "files": files, "bytes": os.path.getsize(self.path) if os.path.isfile(self.path) else 0, "updated": float(upd[0]) if upd else None}

    def refresh(self, chunks, kind="code", say=None):
        """Bring the index in line with `chunks` (from arch_index.chunks_for): insert the ids that are new, delete the ones that vanished.
        -> {"new", "kept", "dropped", "secs", "error"}. Never raises."""
        t0 = time.time()
        con = None
        try:
            con = self._open(create=True)
            have = {r[0] for r in con.execute("select id from chunks where kind=?", (kind,))}
            want = {c["id"]: c for c in chunks}
            new = [c for cid, c in want.items() if cid not in have]
            gone = [cid for cid in have if cid not in want]
            with con:
                if gone:
                    con.executemany("delete from chunks where id=?", [(g,) for g in gone])
                con.executemany("insert into chunks(text, terms, id, path, name, start, end, kind) values (?,?,?,?,?,?,?,?)",
                                [(c["text"], " ".join(terms(c["path"] + " " + c["name"] + " " + c["text"])), c["id"], c["path"], c["name"], c.get("start", 1), c.get("end", 1), kind) for c in new])
                con.execute("insert or replace into meta(key, value) values ('updated', ?)", (str(time.time()),))
            return {"new": len(new), "kept": len(have) - len(gone), "dropped": len(gone), "secs": round(time.time() - t0, 2), "error": None}
        except sqlite3.Error as e:
            return {"new": 0, "kept": 0, "dropped": 0, "secs": round(time.time() - t0, 2), "error": f"index database problem: {e}"}
        finally:
            if con is not None:
                con.close()

    def search(self, text, top=24, kind=None):
        """-> [(chunk dict, score)] best first, score > 0 (BM25 made positive). Hits well below the best are dropped as noise."""
        con = self._open()
        q = _query(text)
        if con is None or not q:
            return []
        try:
            sql = "select id, path, name, start, end, kind, -bm25(chunks, 1.0, 1.5) as s from chunks where chunks match ?" + (" and kind=?" if kind else "") + " order by s desc limit ?"
            rows = con.execute(sql, (q, kind, top) if kind else (q, top)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        if not rows:
            return []
        best = rows[0][6]
        floor = best * 0.35
        return [({"id": r[0], "path": r[1], "name": r[2], "start": r[3], "end": r[4], "kind": r[5]}, round(r[6], 3)) for r in rows if r[6] >= floor and r[6] > 0]

    def wipe(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
