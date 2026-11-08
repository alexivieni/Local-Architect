"""Self-test: the Caches page's numbers (arch_cachestats.ledger) come from where the runs really are, and an empty layer says why.

- the KV prefix layer reads the run logs of the project folders picked on Home (arch_usage's remembered list), not only the portfolio,
  this folder and the eval runs: those alone made the page say "no data" while every run had logged its reuse (2026-11-08)
- llama-server's exact count (cache_n) wins over the estimate
- a hub reading a throwaway data folder (ARCHITECT_DATA_DIR into the temp folder, as the 'hub-test' preview does) says so first
- an empty reply cache explains itself with this install's settings (temperature, seed)
Fixture data in a throwaway home; no model, no network.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    import arch_core as core
    tmp = tempfile.mkdtemp(prefix="archcs_", dir=os.path.expanduser("~"))   # NOT under the temp folder: that is what one check detects
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_HOME")}
    old_home = core.HOME
    try:
        home = os.path.join(tmp, "home")
        core.set_home(home)
        data = os.path.join(home, "hub_data")
        os.environ["ARCHITECT_DATA_DIR"] = data
        os.makedirs(data, exist_ok=True)
        with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"behavior": {"seed": None}}, f)
        proj = os.path.join(tmp, "my_project")
        rd = os.path.join(proj, ".arch", "runs", "20261108_120000_bugfix")
        os.makedirs(rd)
        calls = [dict(type="call", transport="llama", model="w:8b", flow="bugfix", prompt_tokens=1000, cache_n=600, prefix_reused_est=100, cached_exact=True),
                 dict(type="call", transport="llama", model="w:8b", flow="bugfix", prompt_tokens=1000, cache_n=400, prefix_reused_est=100, cached_exact=True),
                 dict(type="call", transport="ollama", model="o:7b", flow="build", prompt_tokens=500, prefix_reused_est=250)]
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(c) for c in calls) + "\n")

        import arch_cachestats as cs
        import arch_usage
        check("before the project is known, its runs are not read (the page cannot guess folders)",
              os.path.abspath(proj) not in cs.trace_roots())
        arch_usage.remember_root(proj)
        roots = cs.trace_roots()
        check("a project folder picked on Home (remembered) is read by the Caches page", os.path.abspath(proj) in roots, roots)
        p = cs.prefix_layer()
        check("its traced calls are counted, per model and per workflow", p["calls"] == 3 and {r["name"] for r in p["by_model"]} == {"w:8b", "o:7b"}
              and {r["name"] for r in p["by_flow"]} == {"bugfix", "build"}, p)
        check("llama-server's exact count (cache_n) wins over the estimate; Ollama calls use the estimate",
              p["cached_tokens"] == 600 + 400 + 250 and p["rho"] == round(1250 / 2500, 3), (p["cached_tokens"], p["rho"]))
        check("the layer says where it read: how many project folders", "project folder" in p["where"] and p["roots"] == len(roots), p["where"])

        led = cs.ledger(transcripts=False)
        check("the usual data folder: no warning, and the page knows which folder it reads",
              led["data_dir"]["path"] == os.path.abspath(data) and not led["data_dir"]["warn"]
              and not any("throwaway data folder" in f["text"] for f in led["findings"]), led["data_dir"])
        fs = " ".join(f["text"] for f in led["findings"])
        check("an empty reply cache explains itself with this install's settings (temperature, no seed) and names the fix",
              "Local replies stay empty with your settings" in fs and "temperature" in fs and "seed" in fs, fs[:400])
        check("an empty route cache says why (the request's own words picked the workflow), without blaming anything",
              "keeps only decisions the router MODEL made" in fs)
        check("no stale advice about traces 'thrown away until 2026-11-05'", "thrown away until" not in fs)

        tdata = tempfile.mkdtemp(prefix="hubtest_")
        try:
            led2 = cs.ledger(transcripts=False, data=tdata)
            first = led2["findings"][0] if led2["findings"] else {}
            check("a hub reading a throwaway data folder says so FIRST, in red, with the usual folder and how to get back to it",
                  led2["data_dir"]["temp"] and first.get("severity") == "high" and "throwaway data folder" in first.get("text", "")
                  and "START_HUB" in first.get("text", ""), first)
        finally:
            shutil.rmtree(tdata, ignore_errors=True)
        other = os.path.join(tmp, "elsewhere")
        os.makedirs(other)
        led3 = cs.ledger(transcripts=False, data=other)
        check("another (non-temp) data folder is named as such, not called throwaway",
              led3["data_dir"]["warn"] and "throwaway" not in led3["data_dir"]["warn"] and not led3["data_dir"]["temp"], led3["data_dir"])
        os.environ["ARCHITECT_NO_CACHE"] = "1"
        try:
            check("the reply reason describes real runs, not this process's test flag (ARCHITECT_NO_CACHE)",
                  "ARCHITECT_NO_CACHE" not in cs._reply_why_empty() and os.environ.get("ARCHITECT_NO_CACHE") == "1")
        finally:
            os.environ.pop("ARCHITECT_NO_CACHE", None)
        # a run started anywhere (CLI, crew, queue) remembers its project when it finishes; a throwaway (temp) folder is never kept
        cli_proj = os.path.join(tmp, "cli_project")
        os.makedirs(cli_proj)
        e = core.Engine(cli_proj, "bugfix", "g", core.load_config(cli_proj), review="auto", say=lambda m: None)
        e.finish(True)
        scratch = tempfile.mkdtemp(prefix="throwaway_")
        try:
            e2 = core.Engine(scratch, "bugfix", "g", core.load_config(scratch), review="auto", say=lambda m: None)
            e2.finish(True)
            known = arch_usage.known_roots()
            check("a finished run remembers its project (so a CLI or crew run shows on the Caches page), never a temp folder",
                  os.path.abspath(cli_proj) in known and os.path.abspath(scratch) not in known and os.path.abspath(cli_proj) in cs.trace_roots(), known)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"behavior": {"seed": 7}}, f)
        check("with a seed set the reply cache has no reason to be empty, so no such finding", cs._reply_why_empty() == "")
    finally:
        core.set_home(old_home)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CACHESTATS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CACHESTATS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
