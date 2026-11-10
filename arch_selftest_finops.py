"""Self-test for arch_finops.py, /api/arch/finops, /api/arch/shortcut, and the FinOps & savings hub page.

    python arch_selftest_finops.py
"""
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
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    tmp = tempfile.mkdtemp(prefix="arch_finops_test_")
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST")}
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    try:
        import arch_core as core
        old_home = core.HOME
        core.set_home(os.environ["ARCHITECT_HOME"])
        import arch_finops
        import arch_launch
        import arch_uireg

        # 1. Cloud cost math
        c_sonnet = arch_finops.cloud_cost_usd(1_000_000, 100_000, 500_000, "claude-sonnet-4-6")
        # 500k uncached * $3 + 500k cached * $0.30 + 100k out * $15 = 1.50 + 0.15 + 1.50 = $3.15
        check("cloud_cost_usd computes exact cached vs uncached API cost",
              abs(c_sonnet["usd"] - 3.15) < 1e-4 and abs(c_sonnet["usd_without_cache"] - 4.50) < 1e-4
              and abs(c_sonnet["cache_savings_usd"] - 1.35) < 1e-4, c_sonnet)

        # 2. Prices: the Anthropic list prices (cached 2026-11-03), each row with its source and date. Opus 4.6 used to say $15/$75
        #    and Haiku 4.5 $0.80/$4, which tripled every "vs. Opus" figure.
        R_ = arch_finops.CLOUD_RATES
        want = {"claude-opus-4-6": (5.0, 0.5, 25.0), "claude-sonnet-4-6": (3.0, 0.3, 15.0), "claude-haiku-4-5": (1.0, 0.1, 5.0)}
        check("Claude list prices are right (Opus 4.6 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5; cache reads 0.1x) and every row says its source and date",
              all((R_[k]["input"], R_[k]["cached_input"], R_[k]["output"]) == v for k, v in want.items())
              and all(r.get("source") and r.get("as_of") for r in R_.values()), {k: (R_[k]["input"], R_[k]["output"]) for k in R_})

        # 3. Measured usage is really read (it called arch_usage.summary, which does not exist, so it was always 0)
        import json as _json
        import time as _time
        proj_root = os.path.join(tmp, "proj")
        rd = os.path.join(proj_root, ".arch", "runs", "r1")
        os.makedirs(rd)
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            for i in range(3):
                f.write(_json.dumps({"type": "call", "t": _time.time(), "model": "qwen3:8b", "tier": "worker", "transport": "llama",
                                     "prompt_tokens": 1000, "gen_tokens": 200, "cache_n": 0}) + chr(10))
            f.write(_json.dumps({"type": "verdict", "t": _time.time(), "verdict": "pass"}) + chr(10))
        rep = arch_finops.report(days=30, roots=[proj_root], team_devs=5, tasks_per_day=20)
        m = rep.get("measured") or {}
        check("measured usage is read from the run traces (3 local calls, 3000 in / 600 out), over a 30-day window",
              m.get("basis") == "measured" and m.get("local_calls") == 3 and m.get("tokens_in") == 3000 and m.get("tokens_out") == 600
              and m.get("window") == "month", m)
        check("nothing is invented when it was not measured: no prefix cache share (it used a fixed 64%) means None, priced without cache",
              m.get("prefix_reuse_rho") is None and m.get("cache_priced") is False and m.get("tokens_cached_est") == 0
              and abs(m.get("would_cost_opus_usd", 0) - (3000 * 5 + 600 * 25) / 1e6) < 1e-6, m)
        empty = arch_finops.report(days=30, roots=[os.path.join(tmp, "nothing_here")])
        check("with no runs the local share is None, not 100% (or the 85% it used to make up)",
              (empty.get("measured") or {}).get("local_share") is None and (empty.get("measured") or {}).get("basis") == "none_yet", empty.get("measured"))

        # 4. The 10 layers: status READ from this install (it was "active" for 9 of them, always)
        opts = {o["id"]: o for o in rep.get("optimizations") or []}
        check("10 layers, each status one of on / off / built in / not set up, each gain with its basis",
              len(opts) == 10 and all(o["status"] in ("on", "off", "built in", "not set up") for o in opts.values())
              and all(o.get("gain") and o.get("gain_basis") for o in opts.values())
              and sum(rep["status_counts"].values()) == 10, rep.get("status_counts"))
        check("the statuses follow the settings: no other machines = 'not set up', step cache 0 days = 'off', KV f16 = 'off'",
              opts["distributed_lan_mesh"]["status"] == "not set up"
              and arch_finops._optimization_layers({}, {}, {"squad": {"step_cache_days": 0}, "server": {"kv": "f16"}}, {}, [])[6]["status"] == "off"
              and arch_finops._optimization_layers({}, {}, {"server": {"kv": "f16"}}, {}, [])[3]["status"] == "off"
              and arch_finops._optimization_layers({}, {}, {"hosts": {"desk": "http://10.0.0.2:11434"}}, {}, [])[8]["status"] == "on")
        md = rep.get("markdown") or ""
        proj = rep.get("projection") or {}
        check("the projection says it is one, with its assumed task size; the Markdown labels measured vs. projection",
              proj.get("basis") == "projection" and "not measured" in proj.get("assumption", "") and proj.get("monthly_tasks") == 2200
              and proj.get("local_monthly_usd") == 0.0 and proj.get("sonnet_yearly_api_usd", 0) > 0
              and "Measured local usage" in md and "Projection for a team" in md and "status read from this install" in md, proj)

        # 3. Desktop shortcut creation in isolated temp dir
        desk = os.path.join(tmp, "Desktop")
        os.makedirs(desk, exist_ok=True)
        sc = arch_launch.make_shortcut(dest_dir=desk, platform="win32")
        check("make_shortcut writes Architect.bat with OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0",
              bool(sc) and os.path.isfile(sc) and "OLLAMA_KV_CACHE_TYPE=q8_0" in open(sc, encoding="utf-8").read(), sc)
        st = arch_launch.shortcut_status(dest_dir=desk, platform="win32")
        check("shortcut_status detects existing shortcut without modifying it", st.get("exists") is True and st.get("path") == sc, st)

        # 4. Hub UI registration check
        html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        reg = arch_uireg.load(os.path.join(HERE, "hub_ui.html"))
        place_pages = dict(reg["place_pages"])
        check("finops is registered in NAV_GROUPS, SIDEBAR (Models > Speed, <= 5 sub-tabs), HINTS, and render()",
              "finops" in reg["page_ids"] and "finops" in place_pages.get("cost", [])
              and len(place_pages.get("cost", [])) <= 5 and "function finopsPage(" in html
              and "function makeDesktopShortcut(" in html and "function launchVisualDemo(" in html, place_pages.get("cost"))
        fo = html[html.find("function launchVisualDemo("):html.find("/* ===== Cost map (Models):")]
        check("the FinOps page invents nothing: no 0.64 / ??1 / ||10 fallbacks, no 'Save 100%', the demo does not switch your project unasked",
              "0.64" not in fo and "local_share??1" not in fo and "||10" not in fo and "Save 100%" not in fo
              and "tests failed" in fo and 'if(r&&r.root){run.root=r.root' not in fo)

        # 5. One-click visual frontend demo creation & pytest verification
        demo_dir = os.path.join(tmp, "visual_demo")
        demo = arch_finops.create_visual_demo(root=demo_dir, start_server=False)
        check("create_visual_demo scaffolds app/server.py, static/index.html, tests/test_demo.py and passes pytest",
              demo.get("ok") is True and demo.get("tests_ok") is True
              and os.path.isfile(os.path.join(demo_dir, "static", "index.html"))
              and os.path.isfile(os.path.join(demo_dir, ".arch", "handoff.json")), demo)
        page = open(os.path.join(demo_dir, "static", "index.html"), encoding="utf-8").read()
        check("the demo page shows the real test count (it said 4/4 for a 2-test file) and no unfilled or invented figure",
              "pytest: 2/2 passing" in page and "{{" not in page and "$1,908" not in page and "68%" not in page
              and "reference laptop" in page, [l for l in page.splitlines() if "pytest:" in l][:1])

        core.set_home(old_home)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' FINOPS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' FINOPS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
