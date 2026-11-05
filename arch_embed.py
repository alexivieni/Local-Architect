"""Optional dense ranker: Ollama /api/embed over the same chunks arch_fts indexes, fused into arch_index's RRF only when
`retrieval.embed_model` names a model (default "" = off, keyword-only, exactly as before).

Rules: the model is asked with keep_alive 0 so it is unloaded right after each batch and never sits next to the chat model on a small
card; loopback hosts only (the offline seal); vectors are cached per (model, chunk text) hash in `.arch/embed.db` as float32 blobs, so
an unchanged chunk is never embedded twice; every public call returns None instead of raising, and None means "keyword-only".
Like the rest of the index, this only orders what the prompt shows; it never decides a pass.
"""
import array
import hashlib
import json
import math
import os
import sqlite3
import urllib.parse
import urllib.request

DB_REL = os.path.join(".arch", "embed.db")
DEFAULT_HOST = "http://127.0.0.1:11434"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def chunk_key(model, text):
    return hashlib.sha1((model + "\n" + text).encode("utf-8", "replace")).hexdigest()


def _pack(vec):
    return array.array("f", vec).tobytes()


def _unpack(blob):
    a = array.array("f")
    a.frombytes(blob)
    return a


def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else None


def model_from(cfg):
    return (((cfg or {}).get("retrieval") or {}).get("embed_model") or "").strip()


def ollama_post(host, timeout=120):
    """-> post(payload) -> dict, for Ollama's /api/embed on a loopback host; None for any other host."""
    try:
        h = urllib.parse.urlparse(host).hostname
    except ValueError:
        return None
    if h not in LOOPBACK:
        return None

    def post(payload):
        req = urllib.request.Request(host.rstrip("/") + "/api/embed", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    return post


def fake_post(dim=256):
    """Deterministic stand-in for /api/embed (tests, `--fake`): hashed word + character-trigram counts. No network, no model."""
    import re

    def vec(text):
        v = [0.0] * dim
        for w in re.findall(r"[a-z]+", text.lower()):
            for g in [w] + [w[i:i + 3] for i in range(max(len(w) - 2, 0))]:
                h = int(hashlib.md5(g.encode()).hexdigest()[:8], 16)
                v[h % dim] += 1.0 if (h >> 20) & 1 else -1.0
        return v

    def post(payload):
        post.calls += 1
        post.inputs += len(payload["input"])
        return {"embeddings": [vec(t) for t in payload["input"]]}
    post.calls, post.inputs = 0, 0
    return post


class Embedder:
    """Dense ranking for one project. post: callable(payload)->dict (injected in tests); default = Ollama on `host`."""

    def __init__(self, root, model, host=None, post=None, batch=16, keep_alive=0, db_path=None):
        self.root, self.model, self.batch, self.keep_alive = root, model, max(1, int(batch)), keep_alive
        self.post = post or ollama_post(host or DEFAULT_HOST)
        self.path = db_path or os.path.join(os.path.abspath(root), DB_REL)
        self.hits = self.misses = 0
        self.error = None if self.post else "embedding host is not loopback; keyword-only"

    def _open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        con = sqlite3.connect(self.path)
        con.execute("create table if not exists vec(key text primary key, model text, dim integer, v blob)")
        return con

    def _call(self, texts):
        out = []
        for i in range(0, len(texts), self.batch):
            part = texts[i:i + self.batch]
            r = self.post({"model": self.model, "input": part, "keep_alive": self.keep_alive, "truncate": True})
            embs = (r or {}).get("embeddings")
            if not isinstance(embs, list) or len(embs) != len(part):
                raise ValueError("embed reply has the wrong shape")
            out.extend(embs)
        return out

    def embed(self, texts):
        """-> [unit vector] in order, cached by content hash; None on any failure."""
        if not self.post or not self.model:
            return None
        con = None
        try:
            con = self._open()
            keys = [chunk_key(self.model, t) for t in texts]
            have = {}
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                q = "select key, v from vec where key in (%s)" % ",".join("?" * len(part))
                have.update({k: _unpack(b) for k, b in con.execute(q, part)})
            miss = [i for i, k in enumerate(keys) if k not in have]
            self.hits += len(keys) - len(miss)
            self.misses += len(miss)
            if miss:
                vecs = self._call([texts[i][:8000] for i in miss])
                rows = []
                for i, v in zip(miss, vecs):
                    u = _norm([float(x) for x in v])
                    if u is None:
                        raise ValueError("zero vector")
                    have[keys[i]] = array.array("f", u)
                    rows.append((keys[i], self.model, len(u), _pack(u)))
                with con:
                    con.executemany("insert or replace into vec(key, model, dim, v) values (?,?,?,?)", rows)
            return [have[k] for k in keys]
        except Exception as e:                       # never raises: None = keyword-only
            self.error = f"embedding unavailable ({type(e).__name__}: {str(e)[:120]}); keyword-only"
            return None
        finally:
            if con is not None:
                con.close()

    def prune(self, chunks):
        """Drop cached vectors (this model) whose chunk text no longer exists. -> count dropped, or None."""
        try:
            want = {chunk_key(self.model, c["text"]) for c in chunks}
            con = self._open()
            try:
                gone = [(k,) for (k,) in con.execute("select key from vec where model=?", (self.model,)) if k not in want]
                with con:
                    con.executemany("delete from vec where key=?", gone)
                return len(gone)
            finally:
                con.close()
        except Exception:
            return None

    def search(self, query, chunks, top=24):
        """-> [(chunk, cosine)] best first over code chunks, or None (keyword-only)."""
        try:
            code = [c for c in chunks if c.get("kind", "code") == "code"]
            if not code or not query.strip():
                return None
            vecs = self.embed([c["text"] for c in code])
            qv = self.embed([query[:4000]]) if vecs is not None else None
            if not vecs or not qv:
                return None
            q = qv[0]
            scored = [(c, sum(a * b for a, b in zip(q, v))) for c, v in zip(code, vecs) if len(v) == len(q)]
            scored.sort(key=lambda x: -x[1])
            return [(c, round(s, 4)) for c, s in scored[:top]]
        except Exception as e:
            self.error = f"embedding unavailable ({type(e).__name__}); keyword-only"
            return None


def for_cfg(root, cfg, post=None):
    """The Embedder the settings ask for, or None when retrieval.embed_model is empty (the default)."""
    m = model_from(cfg)
    if not m:
        return None
    host = ((cfg.get("retrieval") or {}).get("embed_host")) or ((cfg.get("ollama") or {}).get("host")) or DEFAULT_HOST
    return Embedder(root, m, host=host if isinstance(host, str) else DEFAULT_HOST, post=post)
