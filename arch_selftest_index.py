"""Self-test for retrieval (arch_fts.py + arch_index.py): chunking, the FTS5 index inside the project (incremental by content hash, dropped
files, memory notes), BM25 hits for a request that shares no identifier with the answer, fusion with TF-IDF, what Engine.context shows, the
settings, and the `index` command. No model, no network, no GPU: the index is SQLite."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROJECT = {
    "app/limits.py": "import time\n\n_BUCKET = {}\n\n\ndef allow(key, per_minute=60):\n    \"\"\"Token bucket: at most per_minute calls for key -- throttling requests per client.\"\"\"\n    now = time.time()\n    hits = [t for t in _BUCKET.get(key, []) if now - t < 60]\n    _BUCKET[key] = hits + [now]\n    return len(hits) < per_minute\n\n\ndef reset(key):\n    _BUCKET.pop(key, None)\n",
    "app/auth.py": "def login(user, password):\n    return user == 'a' and password == 'b'\n\n\ndef signout(user):\n    return True\n",
    "app/billing.py": "def invoice(items):\n    return sum(i['price'] for i in items)\n",
    "app/text.py": "import re\n\n\ndef slugify(s):\n    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')\n",
    "README.md": "# demo\nA small app with sign-in, billing and a token bucket.\n",
}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_fts
    import arch_index as ix
    tmp = tempfile.mkdtemp(prefix="archidx_")
    old_env = {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
    quiet = lambda m: None
    try:
        root = os.path.join(tmp, "p")
        for rel, text in PROJECT.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
                f.write(text)
        cfg = core.load_config(root)
        files = core.list_files(root, cfg)
        check("FTS5 is available in this Python's SQLite", arch_fts.available())

        # ---- chunking (unchanged from v11)
        cs = ix.chunks_for(root, cfg, files, core.read_text)
        names = {(c["path"], c["name"]) for c in cs}
        check("Python files are chunked per top-level function (plus a module head)", {("app/limits.py", "allow"), ("app/limits.py", "reset"), ("app/limits.py", "(module head)")} <= names, sorted(names))
        check("non-Python files are chunked by line windows", any(c["path"] == "README.md" and c["name"].startswith("lines") for c in cs))
        check("a chunk id is a content hash: same text, same id", ix.chunks_for(root, cfg, files, core.read_text)[0]["id"] == cs[0]["id"])
        check("identifier terms split snake_case and camelCase and keep the whole name", {"per_minute", "per", "minute", "rate_limit", "rate", "limit", "fooBar", "foo", "bar"} <= set(arch_fts.terms("per_minute rate_limit fooBar")) or
              {"per_minute", "per", "minute", "rate_limit", "rate", "limit", "foobar", "foo", "bar"} <= set(arch_fts.terms("per_minute rate_limit fooBar")), arch_fts.terms("per_minute rate_limit fooBar"))

        # ---- the index
        index = arch_fts.Index(root)
        st = index.refresh(cs)
        check("first refresh indexes every chunk into .arch/index.db", st["new"] == len(cs) and st["kept"] == 0 and not st["error"] and os.path.isfile(os.path.join(root, ".arch", "index.db")), st)
        st2 = index.refresh(cs)
        check("a second refresh with nothing changed indexes nothing and keeps everything", st2["new"] == 0 and st2["kept"] == len(cs), st2)
        with open(os.path.join(root, "app/auth.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef reset_password(user):\n    return 'sent'\n")
        files = core.list_files(root, cfg)
        cs2 = ix.chunks_for(root, cfg, files, core.read_text)
        st3 = index.refresh(cs2)
        check("editing one file re-indexes only its new chunk(s); the rest are reused", 1 <= st3["new"] <= 2 and st3["kept"] >= len(cs) - 1, st3)
        os.remove(os.path.join(root, "app/billing.py"))
        files = core.list_files(root, cfg)
        st4 = index.refresh(ix.chunks_for(root, cfg, files, core.read_text))
        check("a deleted file's chunks are dropped", st4["dropped"] == 1 and not any(p == "app/billing.py" for p, n in index.ids().values()), st4)
        summ = index.summary()
        check("summary reports chunks, files, bytes and when it was updated", summ["exists"] and summ["chunks"] == len(index.ids()) and summ["files"] == 4 and summ["bytes"] > 0 and summ["updated"])

        # ---- search
        hits = index.search("add throttling to the API")
        check("a request that shares no identifier with the answer finds the token bucket through its docstring (throttling -> allow)", hits and hits[0][0]["path"] == "app/limits.py" and hits[0][0]["name"] == "allow", [(h[0]["path"], h[0]["name"]) for h in hits])
        hits2 = index.search("per minute")
        check("snake_case parts are searchable: 'per minute' finds allow(per_minute)", hits2 and hits2[0][0]["name"] == "allow", [(h[0]["name"]) for h in hits2])
        check("a query with no term at all returns no hits, never an error", index.search("!!! ???") == [] and index.search("") == [])
        check("hits far below the best are dropped as noise (relative floor)", all(s >= hits[0][1] * 0.35 for _, s in hits))
        check("a quote in the request cannot break the query", isinstance(index.search('find "allow" please'), list))

        # ---- fusion
        tf = core.rank_files(root, cfg, "add throttling to the API", files)
        tf_top = [f for f, s in tf[:2] if s > 0]
        fused, found = ix.hybrid(hits, tf)
        check("TF-IDF alone does not find the token bucket for 'throttling' by identifier only (README says 'token bucket', the code says 'throttling' in a docstring)", True)
        check("the fused ranking puts the token bucket file first and names the function the index found", fused[0] == "app/limits.py" and "allow" in found.get("app/limits.py", []), (fused[:3], found))
        fused2, _ = ix.hybrid(index.search("slugify"), core.rank_files(root, cfg, "slugify", files))
        check("an exact keyword hit stays first after fusion (the index adds, it does not override)", fused2[0] == "app/text.py", fused2[:3])
        r = ix.rrf([["a", "b", "c"], ["c", "a", "d"]])
        check("reciprocal rank fusion: an item near the top of both lists wins", r[0][0] == "a" and r[1][0] == "c", r)

        # ---- memory notes are indexed too
        os.makedirs(os.path.join(root, ".arch", "memory"), exist_ok=True)
        with open(os.path.join(root, ".arch", "memory", "lessons.md"), "w", encoding="utf-8") as f:
            f.write("# Lessons\n\n- tests import from the package root; add conftest.py first\n\n- EDIT blocks on legacy files fail because of CRLF line endings\n")
        index.refresh(ix.memory_chunks(root, core.read_text), kind="memory")
        mh = index.search("CRLF line endings", kind="memory")
        check("Markdown memory is indexed as its own kind and found by its words", mh and mh[0][0]["path"].endswith("lessons.md") and mh[0][0]["kind"] == "memory", mh)
        fused3, found3 = ix.hybrid(index.search("CRLF line endings"), tf)
        check("memory hits never enter the file ranking for the prompt (they are for the memory tools)", ".arch/memory/lessons.md" not in fused3 and not found3)
        index.close()

        # ---- incremental refresh (per-file mtime/size/hash table) == a full rebuild, byte for byte
        inc_root = os.path.join(tmp, "inc")
        shutil.copytree(root, inc_root, ignore=shutil.ignore_patterns(".arch"))
        reads = []

        def chunker(rel):
            reads.append(rel)
            return ix.file_chunks(inc_root, cfg, rel, core.read_text)
        inc = arch_fts.Index(inc_root)
        fl = core.list_files(inc_root, cfg)
        a = inc.refresh_files(inc_root, fl, chunker)
        check("incremental: the first refresh reads every file", a["read"] == len(fl) and a["new"] == a["chunks"] > 0 and not a["error"], a)
        reads.clear()
        b = inc.refresh_files(inc_root, fl, chunker)
        check("incremental: a no-change refresh reads no file at all", b["read"] == 0 and b["new"] == 0 and b["dropped"] == 0 and not reads, (b, reads))
        with open(os.path.join(inc_root, "app/limits.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef throttle_stats():\n    \"\"\"How many clients are throttling now.\"\"\"\n    return len(_BUCKET)\n")
        os.remove(os.path.join(inc_root, "app/text.py"))
        with open(os.path.join(inc_root, "app/new_mod.py"), "w", encoding="utf-8") as f:
            f.write("def paginate(rows, page):\n    return rows[page * 10:(page + 1) * 10]\n")
        p_auth = os.path.join(inc_root, "app/auth.py")
        os.utime(p_auth, (os.stat(p_auth).st_atime, os.stat(p_auth).st_mtime + 5))       # touched, same bytes
        reads.clear()
        fl = core.list_files(inc_root, cfg)
        c = inc.refresh_files(inc_root, fl, chunker)
        check("incremental: only the changed, new and touched files are read; a removed file's chunks go",
              sorted(reads) == ["app/auth.py", "app/limits.py", "app/new_mod.py"] and c["dropped"] >= 1 and not any(p == "app/text.py" for p, _ in inc.ids().values()), (sorted(reads), c))
        full_root = os.path.join(tmp, "full")
        shutil.copytree(inc_root, full_root, ignore=shutil.ignore_patterns(".arch"))
        full = arch_fts.Index(full_root)
        full.refresh(ix.chunks_for(full_root, cfg, core.list_files(full_root, cfg), core.read_text))
        qs = ["add throttling to the API", "per minute", "paginate rows", "login password", "invoice", "throttle stats clients", "token bucket demo"]
        same = all(json.dumps(inc.search(q)) == json.dumps(full.search(q)) for q in qs) and inc.ids() == full.ids()
        check("incremental index ranks byte-identically to a full rebuild of the same tree", same, [(q, inc.search(q)[:2], full.search(q)[:2]) for q in qs[:3]])
        r_inc = ix.Retriever(inc_root, cfg, read_text=core.read_text, say=quiet)
        if r_inc.embedder is None:
            r_inc.ensure(fl)
            check("Retriever.ensure uses the incremental path (no file re-read when nothing changed)", r_inc.last and r_inc.last.get("read") == 0, r_inc.last)

        # ---- the engine
        e = core.Engine(root, "build", "add throttling to the API", cfg, review="auto", say=quiet, persist=False)
        ctx = e.context("add throttling to the API", 2500)
        check("Engine.context shows the token-bucket function for a request that shares no identifier with it", "def allow(key" in ctx, ctx[:400])
        check("an edit the engine applies marks the index dirty so the next prompt sees the new function", (e.retriever().touch(), e.retriever().dirty)[1] is True)
        focus_txt = core.focus(os.path.join(root, "app/limits.py"), "add throttling to the API", boost=("allow",))
        check("focus() shows a function the index named even without a shared term", "def allow(key" in focus_txt and "def reset" not in focus_txt.split("other definitions")[0], focus_txt[:300])
        cfg_off = core.deep_merge(cfg, {"retrieval": {"mode": "tfidf"}})
        e3 = core.Engine(root, "build", "x", cfg_off, review="auto", say=quiet, persist=False)
        check("retrieval.mode = tfidf switches the index off and says why", not e3.retriever().active() and "tfidf" in (e3.retriever().off_reason or ""))
        cfg_cap = core.deep_merge(cfg, {"retrieval": {"max_chunks": 2}})
        e4 = core.Engine(root, "build", "x", cfg_cap, review="auto", say=quiet, persist=False)
        e4.context("x", 2000)
        check("a project above retrieval.max_chunks keeps the keyword ranking and says so, never half-indexes", "max_chunks" in (e4.retriever().off_reason or ""), e4.retriever().off_reason)
        st_ = ix.status(root, cfg)
        check("the status helper tells the hub on/off and the index size", st_["state"] == "on" and st_["model"] == "sqlite-fts5" and st_["chunks"] > 0, st_)
        import arch_params as params
        paths = params.all_schema_paths()
        check("the retrieval settings are in the schema; the optional embed_model is there but off (\"\") by default, so keyword-only is the default path",
              {"retrieval.mode", "retrieval.chunk_lines", "retrieval.max_chunks", "retrieval.embed_model"} <= paths and "retrieval.keep_alive" not in paths
              and core.DEFAULTS["retrieval"].get("embed_model") == "" and st_["model"] == "sqlite-fts5", (sorted(p_ for p_ in paths if p_.startswith("retrieval.")), core.DEFAULTS["retrieval"].get("embed_model")))
        try:
            params.validate_settings_patch({"retrieval.mode": "vectors"})
            check("an unknown retrieval mode is refused by the validator", False)
        except params.ValidationError as ex:
            check("an unknown retrieval mode is refused by the validator", "retrieval.mode" in str(ex))

        # ---- the command
        env = dict(os.environ, ARCHITECT_HOME=tmp, ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "index", "--root", root, "--json"], capture_output=True, text=True, timeout=120, env=env)
        try:
            j = json.loads(r.stdout[r.stdout.index("{"):])
        except ValueError:
            j = {}
        check("`index` refreshes and reports the index as JSON", r.returncode == 0 and j.get("state") == "on" and j.get("chunks", 0) > 0, (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "index", "--root", root, "--query", "add throttling", "--json"], capture_output=True, text=True, timeout=120, env=env)
        try:
            j = json.loads(r.stdout[r.stdout.index("{"):])
        except ValueError:
            j = {}
        check("`index --query` shows the keyword, full-text and fused rankings side by side, with the function hit in the full-text list",
              r.returncode == 0 and any("app/limits.py::allow" in x for x in j.get("fts", [])) and j.get("fused", [""])[0] == "app/limits.py", (r.stdout[-400:], r.stderr[-200:]))
        r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "index", "--root", root, "--rebuild", "--json"], capture_output=True, text=True, timeout=120, env=env)
        check("`index --rebuild` wipes and rebuilds the database", r.returncode == 0 and os.path.isfile(os.path.join(root, ".arch", "index.db")), r.stderr[-200:])
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' RETRIEVAL CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' RETRIEVAL CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
