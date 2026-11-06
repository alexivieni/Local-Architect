"""Self-test for arch_bundle (run bundle) and arch_usage (usage meter), on fixture data in a throwaway home. No model, no network, no Claude."""
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    tmp = tempfile.mkdtemp(prefix="archbundle_")
    old = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "ARCHITECT_TELEMETRY", "USERNAME")}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.environ["USERNAME"] = "fixtureuser"
    os.makedirs(os.environ["ARCHITECT_HOME"])
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  -> {detail}"))

    try:
        import arch_core
        arch_core.set_home(os.environ["ARCHITECT_HOME"])
        import arch_bundle
        import arch_telemetry
        import arch_usage
        data = arch_core.data_dir()
        now = time.time()
        root = os.path.join(tmp, "proj")
        key = "sk-" + "A1b2C3d4E5f6G7h8I9j0"

        def mkrun(rid, verdict, calls):
            rd = os.path.join(root, ".arch", "runs", rid)
            os.makedirs(rd, exist_ok=True)
            with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
                for c in calls:
                    f.write(json.dumps(dict(type="call", t=now - 60, **c)) + "\n")
                f.write("this line is not json " + key + "\n")
                f.write(json.dumps({"type": "verdict", "t": now - 30, "verdict": verdict}) + "\n")
            return rd

        c_local = dict(model="w:4b", tier="worker", transport="llama", prompt_tokens=1000, gen_tokens=200, cache_n=800)
        rd_fail = mkrun("20261105-aaa", "fail", [
            c_local, dict(c_local, cache_n=0, prompt_tokens=500, gen_tokens=100),
            dict(model="r:1b", tier="router", transport="llama", prompt_tokens=50, gen_tokens=5, cache_n=None),
            dict(model="w:4b", tier="worker", transport="cache", cached=True, done_reason="cached", prompt_tokens=900, gen_tokens=90),
            dict(model="claude:claude-sonnet-5-5", tier="heavy", transport="claude", prompt_tokens=300, gen_tokens=30, usd=0.25)])
        mkrun("20261105-bbb", "pass", [c_local])
        old_run = mkrun("20250101-old", "pass", [c_local])
        os.utime(os.path.join(old_run, "trace.jsonl"), (now - 40 * 86400,) * 2)
        with open(os.path.join(rd_fail, "report.md"), "w", encoding="utf-8") as f:
            f.write("# report\nopened C:\\Users\\fixtureuser\\proj\\app.py with key " + key + "\n")
        with open(os.path.join(rd_fail, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "incomplete", "api_key": "plainsecretvalue", "goal": "fix /home/fixtureuser/x"}, f)
        os.makedirs(os.path.join(data, "server"), exist_ok=True)
        with open(os.path.join(data, "server", "worker.log"), "w", encoding="utf-8") as f:
            f.write("x" * 200000 + "\nlast line Bearer abcdefghijklmnop1234 at C:\\Users\\fixtureuser\\m.gguf\n")
        with open(os.path.join(data, "server.json"), "w", encoding="utf-8") as f:
            json.dump({"processes": {"worker": {"tier": "worker", "ngl": 37, "started": now - 3600, "pid": 1}}, "mode": "llama"}, f)
        with open(arch_core.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"cloud": {"api_key": key}, "default_model": "w:4b"}, f)
        # telemetry: a run event, plus the router card time (2h, stopped) and an orphan event of another run
        os.makedirs(os.path.join(data, "telemetry"), exist_ok=True)
        evs = [dict(t=now - 50, kind="call", sid="1", run="20261105-aaa", model="w:4b"),
               dict(t=now - 50, kind="call", sid="2", run="other"),
               dict(t=now - 4 * 3600, kind="server.ready", sid="3", tier="router", layers_on_card=20),
               dict(t=now - 2 * 3600, kind="server.stop", sid="4", tier="router"),
               dict(t=now - 5000, kind="server.ready", sid="5", tier="heavy", layers_on_card=0)]
        with open(arch_telemetry.path(), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(e) for e in evs) + "\n")

        # ---- bundle
        z = arch_bundle.bundle(rd_fail)
        check("bundle: zip lands in <data>/bundles/", os.path.dirname(z) == os.path.join(data, "bundles") and zipfile.is_zipfile(z), z)
        zf = zipfile.ZipFile(z)
        names = set(zf.namelist())
        for n in ("run/trace.jsonl", "run/report.md", "run/run.json", "telemetry.jsonl", "server_state.json", "versions.json", "settings.json",
                  "logs/worker.log.tail", "README.txt"):
            check("bundle has " + n, n in names, sorted(names))
        blob = "\n".join(zf.read(n).decode("utf-8") for n in names)
        check("secrets redacted everywhere (sk- key, Bearer, api_key values)",
              key not in blob and "abcdefghijklmnop1234" not in blob and "plainsecretvalue" not in blob, "leak")
        check("user name and paths anonymised", "fixtureuser" not in blob and "Users\\~" in blob and "/home/~" in blob)
        tel = zf.read("telemetry.jsonl").decode().splitlines()
        check("telemetry only this run's events", len(tel) == 1 and "20261105-aaa" in tel[0], tel)
        check("log tail is a tail (bounded) and keeps the last line",
              len(zf.read("logs/worker.log.tail")) < 70000 and b"last line" in zf.read("logs/worker.log.tail"))
        check("non-JSON trace line kept as redacted text", b"this line is not json" in zf.read("run/trace.jsonl"))
        check("settings carry the effective config", json.loads(zf.read("settings.json")).get("default_model") == "w:4b")
        check("versions has python", "python" in json.loads(zf.read("versions.json")))
        zf.close()
        check("auto: pass -> nothing", arch_bundle.auto(rd_fail, "pass") is None)
        check("auto: not_sure -> a zip", (arch_bundle.auto(rd_fail, "not_sure") or "").endswith(".zip"))
        check("auto: missing run folder never raises", arch_bundle.auto(os.path.join(tmp, "nope"), "fail") is None)
        try:
            arch_bundle.bundle(os.path.join(tmp, "nope"))
            check("bundle: missing folder raises FileNotFoundError", False)
        except FileNotFoundError:
            check("bundle: missing folder raises FileNotFoundError", True)

        # ---- usage
        u = arch_usage.usage("day", roots=[root], fresh=True)
        loc = u["local"]
        check("usage: local tokens in/out exclude cached and Claude calls", loc["tokens_in"] == 1000 + 500 + 50 + 1000 and loc["tokens_out"] == 200 + 100 + 5 + 200,
              loc)
        check("usage: by model and by tier", {m["model"] for m in loc["by_model"]} == {"w:4b", "r:1b"}
              and {t["tier"]: t["calls"] for t in loc["by_tier"]} == {"worker": 3, "router": 1}, loc)
        check("usage: runs 2 (old run outside the window), pass rate 0.5", u["runs"] == 2 and u["passed"] == 1 and u["pass_rate"] == 0.5, u)
        check("usage: week sees still not the 40 day old run", arch_usage.usage("week", roots=[root], fresh=True)["runs"] == 2)
        check("usage: cache rates (1 exact of 6 calls, 2 of 5 prefix-measured... )",
              u["cache"]["exact_hits"] == 1 and u["cache"]["prefix_hits"] == 2 and u["cache"]["prefix_hit_rate"] == round(2 / 3, 3), u["cache"])
        check("usage: Claude spend from arch_costs", abs(u["claude"]["usd"] - 0.25) < 1e-6 and u["claude"]["tokens_in"] == 300, u["claude"])
        g = u["gpu_hours"]["by_tier"]
        check("usage: GPU hours by tier (router 2 h stopped, worker 1 h running, heavy on CPU 0)",
              abs(g.get("router", 0) - 2.0) < 0.01 and abs(g.get("worker", 0) - 1.0) < 0.01 and "heavy" not in g, g)
        u2 = arch_usage.usage("day", roots=[root])
        check("usage: second call is the cached object (60 s)", u2 is arch_usage.usage("day", roots=[root]) and u2["generated"] == u["generated"])
        check("usage: a bad window falls back to day; unreadable roots give zeros",
              arch_usage.usage("year", roots=[], fresh=True)["window"] == "day" and arch_usage.usage("day", roots=[], fresh=True)["runs"] == 0)
        check("usage: JSON-serialisable", bool(json.dumps(u)))
        # known roots: remembered folder + practice project + eval run folders are all read when no roots are given
        def mkroot_run(base, rid, verdict):
            rd = os.path.join(base, ".arch", "runs", rid)
            os.makedirs(rd, exist_ok=True)
            with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps(dict(type="call", t=now - 20, model="w:4b", tier="worker", transport="llama", prompt_tokens=10, gen_tokens=5)) + "\n")
                f.write(json.dumps({"type": "verdict", "t": now - 10, "verdict": verdict}) + "\n")
        recent = os.path.join(tmp, "recent_proj")
        practice = os.path.join(data, "practice", "textkit")
        evalroot = os.path.join(data, "evals", "runs", "train")
        for b, rid in ((recent, "r1"), (practice, "p1"), (evalroot, "e1")):
            os.makedirs(b, exist_ok=True)
            mkroot_run(b, rid, "pass")
        base = arch_usage.usage("day", fresh=True)["runs"]
        check("usage: the practice and eval run folders are read with no roots given", base == 2, base)
        check("usage: a remembered folder is added", arch_usage.remember_root(recent) and arch_usage.usage("day", fresh=True)["runs"] == 3)
        check("usage: the page's own recent list can be passed in", recent in arch_usage.known_roots([recent, os.path.join(tmp, "nope")])
              and os.path.join(tmp, "nope") not in arch_usage.known_roots([os.path.join(tmp, "nope")]))
        check("usage: explicit roots still read only those", arch_usage.usage("day", roots=[recent], fresh=True)["runs"] == 1)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            import arch_core as _c
            _c.set_home(old["ARCHITECT_HOME"] or HERE)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' BUNDLE/USAGE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' BUNDLE/USAGE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
