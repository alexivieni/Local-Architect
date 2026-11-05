"""Selftest: optional dense ranker (arch_embed) + the retrieval benchmark. Fake embedder only: no model, no network.
  py -3.12 arch_selftest_retrieval.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import arch_embed   # noqa: E402
import arch_index   # noqa: E402

RESULTS = []


def ok(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def chunks(*texts):
    return [{"path": f"f{i}.py", "name": f"n{i}", "text": t, "kind": "code"} for i, t in enumerate(texts)]


def test_cache(tmp):
    post = arch_embed.fake_post()
    db = os.path.join(tmp, "e.db")
    e = arch_embed.Embedder(tmp, "m1", post=post, db_path=db)
    v = e.embed(["alpha beta", "gamma delta"])
    ok("embed returns one unit vector per text", v is not None and len(v) == 2 and abs(sum(x * x for x in v[0]) - 1) < 1e-3)
    ok("first call is all misses", e.misses == 2 and e.hits == 0 and post.inputs == 2)
    e.embed(["alpha beta", "gamma delta"])
    ok("second call is all cache hits (no request)", e.hits == 2 and post.inputs == 2)
    e.embed(["alpha beta", "gamma CHANGED"])
    ok("changed text is a miss, unchanged a hit", e.hits == 3 and e.misses == 3 and post.inputs == 3)
    e2 = arch_embed.Embedder(tmp, "m2", post=post, db_path=db)
    e2.embed(["alpha beta"])
    ok("another model does not reuse m1 vectors", e2.misses == 1)
    dropped = e.prune(chunks("alpha beta"))
    ok("prune drops vectors of vanished chunks (this model only)", dropped == 2, dropped)
    seen = []
    pb = arch_embed.fake_post()
    e3 = arch_embed.Embedder(tmp, "m3", post=lambda p: (seen.append(p), pb(p))[1], db_path=db, batch=2)
    e3.embed(["a1", "a2", "a3", "a4", "a5"])
    ok("batched and keep_alive 0", len(seen) == 3 and all(p["keep_alive"] == 0 for p in seen))


def test_fallback(tmp):
    def boom(p):
        raise ConnectionError("down")
    e = arch_embed.Embedder(tmp, "m", post=boom, db_path=os.path.join(tmp, "f.db"))
    ok("failure -> embed None, no raise", e.embed(["x"]) is None and e.error)
    ok("failure -> search None", e.search("q", chunks("x")) is None)
    e = arch_embed.Embedder(tmp, "m", post=lambda p: {"embeddings": []}, db_path=os.path.join(tmp, "g.db"))
    ok("bad reply shape -> None", e.embed(["x"]) is None)
    ok("non-loopback host refused", arch_embed.ollama_post("http://10.1.2.3:11434") is None
       and arch_embed.Embedder(tmp, "m", host="http://example.com").embed(["x"]) is None)
    ok("off by default", arch_embed.for_cfg(tmp, {"retrieval": {}}) is None and arch_embed.for_cfg(tmp, {}) is None)


def test_fusion(tmp):
    tf = [("a.py", 2.0), ("b.py", 1.0), ("c.py", 0.0)]
    hits = [({"path": "b.py", "name": "x", "kind": "code"}, 3.0)]
    base, _ = arch_index.hybrid(hits, tf)
    same, _ = arch_index.hybrid(hits, tf, None)
    ok("no dense hits -> order identical to keyword-only", base == same)
    dense = [({"path": "c.py", "name": "y", "kind": "code"}, 0.9), ({"path": "b.py", "name": "x", "kind": "code"}, 0.8)]
    tf2 = [("a.py", 2.0), ("b.py", 0.0), ("c.py", 0.0)]
    base2, _ = arch_index.hybrid([], tf2)
    fused, _ = arch_index.hybrid([], tf2, dense)
    ok("dense hits change the order only when given", base2 == ["a.py", "b.py", "c.py"] and fused.index("c.py") < fused.index("b.py") and fused.index("c.py") < 2, (base2, fused))
    # Retriever: no embed_model -> no embedder; failing embedder -> keyword-only
    import arch_core
    import copy
    cfg = copy.deepcopy(arch_core.DEFAULTS)
    proj = os.path.join(tmp, "proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "m.py"), "w") as f:
        f.write("def throttle(x):\n    return x\n\ndef other():\n    pass\n")
    r = arch_index.Retriever(proj, cfg, read_text=arch_core.read_text)
    ok("Retriever has no embedder unless retrieval.embed_model is set", r.embedder is None)
    files = ["m.py"]
    tfr = arch_core.rank_files(proj, cfg, "throttle", files)
    want = r.rank("throttle", files, tfr)
    r.embedder = arch_embed.Embedder(proj, "m", post=lambda p: 1 / 0, db_path=os.path.join(tmp, "r.db"))
    got = r.rank("throttle", files, tfr)
    ok("failing embedder -> keyword-only result and embedder switched off", got == want and r.embedder is None)
    r.embedder = arch_embed.Embedder(proj, "m", post=arch_embed.fake_post(), db_path=os.path.join(tmp, "r2.db"))
    got = r.rank("throttle", files, tfr)
    ok("working embedder fused in", got[0] == ["m.py"] and r.embedder is not None and r.embedder.misses > 0)


def test_bench(tmp):
    import retrieval_bench as rb
    qs = rb.load()
    ok("benchmark has ~60 questions of both types", len(qs) >= 55 and {q["type"] for q in qs} == {"identifier", "paraphrase"})
    d = os.path.join(tmp, "gt")
    bad = rb.check(qs, rb.materialise([q["corpus"] for q in qs], d), rb.cfg_default())
    ok("ground truth valid (every expected file exists and holds its evidence)", not bad, bad[:3])
    wrong = [dict(qs[0], expect=["no_such_file.py"])]
    ok("ground-truth check catches a wrong file", rb.check(wrong, rb.materialise([wrong[0]["corpus"]], d), rb.cfg_default()))
    sub = [q for q in qs if q["corpus"].startswith("tpl:")]
    res = rb.run("selftest_fake", fake=True, out_dir=os.path.join(tmp, "out"), questions=sub, say=lambda m: None)
    ok("fake run end to end: both rankers scored, files written",
       set(res["runs"]) == {"keyword", "keyword+embed"} and os.path.isfile(os.path.join(tmp, "out", "retrieval_selftest_fake.json"))
       and os.path.isfile(os.path.join(tmp, "out", "retrieval_selftest_fake.md")))
    s = res["runs"]["keyword"]["scores"]
    print(f"       keyword-only on templates: r@1 {s['all']['r@1']} r@3 {s['all']['r@3']} r@8 {s['all']['r@8']} MRR {s['all']['mrr']} (n={s['all']['n']})")
    ok("keyword-only numbers reported with Wilson intervals", all(k in s["all"] for k in ("r@1", "r@3", "r@8", "mrr", "r@1_ci")))
    ok("wilson sane", rb.wilson(0, 10)[0] == 0.0 and rb.wilson(10, 10)[1] == 1.0 and rb.wilson(5, 10)[0] < 0.5 < rb.wilson(5, 10)[1])


def main():
    tmp = tempfile.mkdtemp(prefix="selftest_retrieval_")
    try:
        for t in (test_cache, test_fallback, test_fusion, test_bench):
            print(t.__name__)
            try:
                t(tmp)
            except Exception as e:
                import traceback
                traceback.print_exc()
                ok(t.__name__ + " raised", False, repr(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, g in RESULTS if not g]
    print(f"retrieval selftest: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
