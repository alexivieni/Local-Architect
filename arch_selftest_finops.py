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

        # 2. Full report structure & 10 optimization layers
        rep = arch_finops.report(days=30, team_devs=5, tasks_per_day=20)
        check("report returns available=True, hardware, measured, projection, 10 optimizations, and markdown",
              rep.get("available") is True and len(rep.get("optimizations") or []) == 10
              and rep.get("optimization_count") == 10 and rep.get("active_optimizations") >= 9
              and isinstance(rep.get("markdown"), str) and "FinOps & Optimization Executive Report" in rep["markdown"],
              list(rep.keys()))

        proj = rep.get("projection") or {}
        check("team ROI projection scales with devs and tasks_per_day (5 devs * 20 tasks * 22 days = 2200 tasks/mo)",
              proj.get("monthly_tasks") == 2200 and proj.get("local_monthly_usd") == 0.0
              and proj.get("sonnet_yearly_saved_usd", 0) > 500 and proj.get("opus_yearly_saved_usd", 0) > 2500, proj)

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

        # 5. One-click visual frontend demo creation & pytest verification
        demo_dir = os.path.join(tmp, "visual_demo")
        demo = arch_finops.create_visual_demo(root=demo_dir, start_server=False)
        check("create_visual_demo scaffolds app/server.py, static/index.html, tests/test_demo.py and passes pytest",
              demo.get("ok") is True and demo.get("tests_ok") is True
              and os.path.isfile(os.path.join(demo_dir, "static", "index.html"))
              and os.path.isfile(os.path.join(demo_dir, ".arch", "handoff.json")), demo)

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
