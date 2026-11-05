"""Self-test: arch_telemetry (emit/span/read/summary/timeline, roll, off switch, never raises) and the rank_files term cache."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    d = tempfile.mkdtemp(prefix="tel_")
    os.environ["ARCHITECT_DATA_DIR"] = d
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ.pop("ARCHITECT_TELEMETRY", None)
    import arch_telemetry as T
    import arch_core as core
    T.configure(enabled=True, cap_mb=20)
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))
        print(("PASS " if cond else "FAIL ") + name)

    T.set_run("r1")
    with T.span("step", step="a") as outer:
        T.emit("call", prompt_tokens=100, gen_tokens=10, cache={"state": "hit"}, messages=[{"x": "y" * 50}])
        with T.span("tool", name="ls"):
            pass
    T.emit("call", prompt_tokens=50, gen_tokens=5, cache={"state": "miss"}, prompt_est=100, prefix_reused_est=40)
    T.emit("error", where="x")
    T.set_run("r2")
    T.emit("server.start", tier="worker")
    evs = T.read(limit=0)
    check("emit+read", len(evs) == 6)
    check("heavy fields reduced", all("messages" not in e for e in evs) and any("messages_chars" in e for e in evs))
    check("filter run", len(T.read(run="r2")) == 1)
    check("filter kind prefix", len(T.read(kinds="server")) == 1 and len(T.read(kinds=["call"])) == 2)
    check("filter since", len(T.read(since=time.time() + 60)) == 0)
    s = T.summary(3600)
    check("summary math", s["calls"] == 2 and s["tokens_in"] == 150 and s["tokens_out"] == 15
          and s["cache_hit_share"] == 0.5 and s["prefix_reuse"] == 0.4 and s["errors"] == 1 and s["by_kind"]["step"] == 1)
    tl = T.timeline("r1")
    step = [r for r in tl if r["kind"] == "step"]
    check("span nesting", step and {c["kind"] for c in step[0]["children"]} == {"call", "tool"} and step[0]["secs"] >= 0)
    try:
        with T.span("boom"):
            raise ValueError("x")
    except ValueError:
        pass
    check("span error status", T.read(kinds="boom")[-1]["status"] == "error")
    # rolling cap
    T.configure(cap_mb=0.05)
    for i in range(1500):
        T.emit("fill", i=i, pad="z" * 40)
    T.flush()
    check("rolling cap", os.path.exists(T.path() + ".1") and os.path.getsize(T.path()) < 0.2 * 1024 * 1024)
    os.utime(T.path() + ".1", (0, 0))
    check("prune old generation", T.prune(3600) == 1)
    T.configure(cap_mb=20)
    # never raises on disk errors
    real = T.path
    T.path = lambda: os.path.join(d, "nofile\0bad", "x")
    try:
        r1 = T.emit("x")
        r2 = T.flush()
        check("never raises on disk error", r1 is not None and r2 is False and T.drops() > 0)
    except Exception:
        check("never raises on disk error", False)
    T.path = real
    T.swallow("w", OSError("a"))
    T.swallow("w", OSError("b"))
    check("swallow rate-limited", len([e for e in T.read(kinds="error", limit=0) if e.get("where") == "w"]) == 1 and T.diag_count() >= 2)
    # off switch
    n = len(T.read(limit=0))
    T.configure(enabled=False)
    check("off switch", T.emit("x") is None and len(T.read(limit=0)) == n)
    T.configure(enabled=True)
    os.environ["ARCHITECT_TELEMETRY"] = "off"
    check("env off", T.emit("x") is None)
    os.environ.pop("ARCHITECT_TELEMETRY")
    check("legacy upgrade", T.upgrade({"type": "call", "ts": 5.0})["kind"] == "call" and T.upgrade({"ts": 5.0})["t"] == 5.0)
    # rank_files term cache: miss, hit, invalidation
    root = tempfile.mkdtemp(prefix="rk_")
    with open(os.path.join(root, "a.py"), "w") as f:
        f.write("def throttle(): pass\n")
    with open(os.path.join(root, "b.py"), "w") as f:
        f.write("def other(): pass\n")
    cfg = core.DEFAULTS
    core._TERMS.clear()
    h0, m0 = core._TERMS_STATS["hit"], core._TERMS_STATS["miss"]
    r = core.rank_files(root, cfg, "throttle", files=["a.py", "b.py"])
    check("rank miss", core._TERMS_STATS["miss"] - m0 == 2 and r[0][0] == "a.py")
    core.rank_files(root, cfg, "throttle", files=["a.py", "b.py"])
    check("rank hit", core._TERMS_STATS["hit"] - h0 == 2)
    with open(os.path.join(root, "b.py"), "w") as f:
        f.write("def throttle(): throttle(); throttle()\n")
    st = os.stat(os.path.join(root, "b.py"))
    os.utime(os.path.join(root, "b.py"), ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    m1 = core._TERMS_STATS["miss"]
    r = core.rank_files(root, cfg, "throttle", files=["a.py", "b.py"])
    check("rank invalidation", core._TERMS_STATS["miss"] - m1 == 1)
    check("rank telemetry", len(T.read(kinds="cache.rank_terms")) >= 3)
    check("settings in both", "telemetry" in core.DEFAULTS["behavior"] and "rank_cache" in core.DEFAULTS["behavior"]
          and "telemetry_mb" in core.DEFAULTS["behavior"])
    bad = [n for n, c in ok if not c]
    print(f"telemetry: {len(ok) - len(bad)}/{len(ok)} passed")
    return not bad


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
