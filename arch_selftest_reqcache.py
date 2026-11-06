"""Self-test for arch_reqcache: near-duplicate match (Jaccard >= 0.8 AND same touched files), invalidation by file changes, only validated
plans stored, stats, off switches. Isolated data dir, no model.

    py -3.12 arch_selftest_reqcache.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    home = tempfile.mkdtemp(prefix="selftest_reqcache_")
    prev = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_EVAL")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ.pop("ARCHITECT_NO_CACHE", None)
    os.environ.pop("ARCHITECT_EVAL", None)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    try:
        import arch_reqcache as rc
        proj = os.path.join(home, "proj")
        os.makedirs(os.path.join(proj, "pkg"))
        for rel, txt in (("app.py", "def main():\n    return 1\n"), ("util.py", "X = 1\n"), ("pkg/mod.py", "Y = 2\n")):
            with open(os.path.join(proj, rel), "w", newline="") as f:
                f.write(txt)
        plan = {"sprints": [{"name": "s1", "files": ["app.py"], "goal": "add retries"}]}
        ctx = ["app.py", "util.py"]
        req = "Add retries with backoff to the HTTP client in app.py"

        check("normalize: case, punctuation and spacing do not matter", rc.normalize("  Add Retries,  to app.py. ") == "add retries to app.py")
        check("jaccard: identical = 1, disjoint = 0", rc.jaccard({"a", "b"}, {"a", "b"}) == 1.0 and rc.jaccard({"a"}, {"b"}) == 0.0)
        check("touched_in: finds the files a request names", rc.touched_in("fix app.py and pkg\\mod.py please") == {"app.py", "pkg/mod.py"}, rc.touched_in("fix app.py and pkg\\mod.py"))

        snap = rc.snapshot(proj, ctx + ["app.py"])
        check("store: a plan that did not pass its gates is never stored", rc.store(proj, req, plan, ctx, snap=snap, validated=False) is None
              and not rc.lookup(proj, req)["hit"])
        eid = rc.store(proj, req, plan, ctx, snap=snap, validated=True)
        check("store: a validated plan gets an id", bool(eid))

        r = rc.lookup(proj, req)
        check("lookup: the same request hits (exact) and returns plan + context files", r["hit"] and r["kind"] == "exact" and r["plan"] == plan and r["context_files"] == ctx, r)
        check("lookup: a hit says the gates still run", r["gates"] == "still run" and "gate" in r["why"])
        r["plan"]["sprints"][0]["name"] = "mutated"
        check("lookup: the returned plan is a copy", rc.lookup(proj, req)["plan"]["sprints"][0]["name"] == "s1")

        near = "please add retries with backoff to the HTTP client in app.py"
        r = rc.lookup(proj, near)
        check("lookup: a reworded request (Jaccard >= 0.8, same files) hits as near", r["hit"] and r["kind"] == "near" and r["similarity"] >= 0.8, r)
        long_req = "Add retries with exponential backoff and jitter to the HTTP client wrapper in app.py"
        rc.store(proj, long_req, plan, ctx, snap=snap, validated=True)
        r = rc.lookup(proj, long_req.replace("app.py", "util.py"))
        check("lookup: nearly the same words but a different touched file is a miss", not r["hit"] and "different files" in r["why"], r)
        rc.invalidate(proj, long_req)
        r = rc.lookup(proj, "Write a README for the project in app.py")
        check("lookup: a different request is a miss", not r["hit"], r)

        with open(os.path.join(proj, "pkg", "mod.py"), "w") as f:
            f.write("Y = 3\n")
        check("invalidation: editing an unrelated file keeps the entry", rc.lookup(proj, req)["hit"])
        with open(os.path.join(proj, "app.py"), "w", newline="") as f:
            f.write("def main():\r\n    return 1\r\n")
        check("invalidation: only line endings changed -> still valid", rc.lookup(proj, req)["hit"])
        with open(os.path.join(proj, "app.py"), "w") as f:
            f.write("def main():\n    return 2\n")
        r = rc.lookup(proj, req)
        check("invalidation: editing a file the plan depends on -> miss, says which", not r["hit"] and "app.py changed" in r["why"], r)
        with open(os.path.join(proj, "app.py"), "w") as f:
            f.write("def main():\n    return 1\n")
        check("invalidation: restoring the file makes it valid again", rc.lookup(proj, req)["hit"])
        with open(os.path.join(proj, "new.py"), "w") as f:
            f.write("Z = 1\n")
        r = rc.lookup(proj, req)
        check("invalidation: a file added to the project -> miss", not r["hit"] and "added to or removed" in r["why"], r)
        os.remove(os.path.join(proj, "new.py"))

        s = rc.stats(proj)
        check("stats: counts hits, near hits, misses, stale and stored", s["hits"] >= 4 and s["near"] == 1 and s["misses"] >= 3 and s["stale"] >= 2 and s["stored"] == 2 and s["entries"] == 1
              and s["hit_rate"] is not None, s)
        check("stats: all projects", rc.stats()["entries"] == 1)

        cfg_on, cfg_off = {"behavior": {"request_cache": "on"}}, {"behavior": {"request_cache": "off"}}
        check("enabled: on only when the setting says on", rc.enabled(cfg_on) and not rc.enabled(cfg_off) and not rc.enabled({}))
        check("lookup(cfg off) is a miss without touching the store", not rc.lookup(proj, req, cfg=cfg_off)["hit"] and rc.lookup(proj, req, cfg=cfg_on)["hit"])
        os.environ["ARCHITECT_NO_CACHE"] = "1"
        check("ARCHITECT_NO_CACHE: always a miss", not rc.lookup(proj, req)["hit"] and not rc.lookup(proj, req, cfg=cfg_on)["hit"])
        os.environ.pop("ARCHITECT_NO_CACHE")
        os.environ["ARCHITECT_EVAL"] = "1"
        check("eval: always a miss", not rc.lookup(proj, req)["hit"])
        os.environ.pop("ARCHITECT_EVAL")

        rc.store(proj, req, {"sprints": [{"name": "s2", "files": ["app.py"]}]}, ctx, validated=True)
        check("store: the newest validated plan for the same words replaces the old", rc.stats(proj)["entries"] == 1 and rc.lookup(proj, req)["plan"]["sprints"][0]["name"] == "s2")
        check("invalidate: drops the project's entries", rc.invalidate(proj) == 1 and not rc.lookup(proj, req)["hit"])
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    print(f"reqcache: {sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
