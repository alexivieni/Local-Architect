"""Self-test for arch_agentlib (the agent library: agent_templates/*.md -> briefs -> fleet plans).
No SDK, no network, nothing is launched: plans are only built and checked with arch_fleet.plan_check.
Run: python arch_selftest_agentlib.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

GENERAL = ("builder", "reviewer", "ux_pass", "research_doc", "monitor", "migration", "bug_hunt", "perf_pass", "common")
FROM_BRIEFS = ("api_review", "spend_report", "scheduler", "dataset_polish", "setting_switch", "dispatcher", "knowledge_store",
               "template_library", "local_twin", "online_option", "step_plan", "hub_builder")
COMMON_LINES = ("Read CLAUDE.md first.", "Edit ONLY the files your brief names.", "No commits, pushes, deletions",
                "You cannot run commands; the reviewer runs the tests.", "Originality: name things in the app's own voice",
                "End with a short report: what already existed")


def run():
    results = []

    def check(name, cond, detail=None):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f"  -> {str(detail)[:400]}"))

    tmp = tempfile.mkdtemp(prefix="arch_agentlib_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    try:
        import arch_agentlib as lib
        import arch_fleet as fl

        # ---- the folder
        listed = lib.list_templates()
        names = [t["name"] for t in listed]
        check("every general pattern is there", all(n in names for n in GENERAL), [n for n in GENERAL if n not in names])
        check("every brief-derived template is there", all(n in names for n in FROM_BRIEFS),
              [n for n in FROM_BRIEFS if n not in names])
        broken = [t for t in listed if t.get("kind") == "broken"]
        check("no template is broken", not broken, broken)

        agents = [n for n in names if lib.load(n)["kind"] == "agent"]
        for n in names:
            t = lib.load(n)
            problems = []
            if not t["purpose"] or not t["why"]:
                problems.append("purpose and why are required")
            if t["kind"] == "agent":
                if t["mode"] not in fl.MODES:
                    problems.append("bad mode")
                no_value = [k for k in t["placeholders"]
                            if k not in t["examples"] and k not in t["defaults"] and k not in lib.SHARED]
                if no_value:
                    problems.append(f"slots without an example: {no_value}")
                undescribed = [k for k in t["placeholders"] if k not in t["slots"] and k not in lib.SHARED]
                if undescribed:
                    problems.append(f"slots without words: {undescribed}")
                stray = [k for k in list(t["slots"]) + list(t["examples"]) if k not in t["placeholders"]]
                if stray:
                    problems.append(f"described but unused: {stray}")
            check(f"{n}: header complete", not problems, problems)

            if t["kind"] != "agent":
                continue
            ex = dict(t["examples"])
            text = lib.fill(n, **ex)
            check(f"{n}: fills with no leftover slots", not lib.leftovers(text) and "{{" not in text, lib.leftovers(text))
            if t["footer"] == "common":
                check(f"{n}: the common footer is appended", all(s in text for s in COMMON_LINES))
            parts = lib.fill_parts(n, **ex)
            bad_tests = [c for c in parts["tests"] if fl.command_problem(c)]
            check(f"{n}: its tests are commands the fleet accepts", not bad_tests, bad_tests)
            try:
                plan = lib.to_fleet_plan([{"template": n, "values": ex}])
                check(f"{n}: a plan_check-clean fleet plan", fl.plan_check(plan) == [] and plan[0]["brief"] == text
                      and plan[0]["mode"] == t["mode"] and plan[0]["budget_usd"] == t["budget_usd"], plan)
            except ValueError as e:
                check(f"{n}: a plan_check-clean fleet plan", False, e)
            words = lib.explain(n)
            check(f"{n}: explain names what goes in", t["purpose"] in words and all(k in words for k in t["placeholders"]),
                  words[:300])

        # ---- the common footer is the launcher's own text
        common = lib.fill("common")
        check("common fills with nothing and has every shared rule", all(s in common for s in COMMON_LINES), common[:200])
        launch_py = os.path.join(HERE, "hub_data", "learning", "briefs", "launch.py")
        if os.path.isfile(launch_py):
            src = open(launch_py, encoding="utf-8").read()
            body = src.split('COMMON = r"""', 1)[1].split('"""', 1)[0].strip()
            check("common is word for word the launcher's COMMON", body == common)
        else:
            check("common is word for word the launcher's COMMON (launcher not on this machine: skipped)", True)
        try:
            lib.to_fleet_plan(["common"])
            check("a footer is not an agent", False)
        except ValueError as e:
            check("a footer is not an agent", "footer" in str(e), e)

        # ---- refusals in plain words
        ex = lib.load("builder")["examples"]
        try:
            lib.fill("builder", **{k: v for k, v in ex.items() if k != "module"})
            check("a missing slot is refused by name", False)
        except ValueError as e:
            check("a missing slot is refused by name", "module" in str(e), e)
        try:
            lib.fill("builder", modul="x", **ex)
            check("an unknown slot (a typo) is refused", False)
        except ValueError as e:
            check("an unknown slot (a typo) is refused", "modul" in str(e), e)
        for bad in ("../CLAUDE", "nope_not_here", ""):
            try:
                lib.load(bad)
                check(f"load({bad!r}) refused", False)
            except ValueError:
                check(f"load({bad!r}) refused", True)

        # ---- several templates in one plan
        mixed = [{"template": "builder", "values": ex},
                 {"template": "reviewer", "values": lib.load("reviewer")["examples"], "after": ["builder"]},
                 {"template": "research_doc", "values": lib.load("research_doc")["examples"]}]
        plan = lib.to_fleet_plan(mixed)
        check("a mixed plan is clean, names unique, 'after' kept",
              fl.plan_check(plan) == [] and [a["name"] for a in plan] == ["builder", "reviewer", "research_doc"]
              and plan[1]["after"] == ["builder"] and plan[1]["mode"] == "plan", plan)
        twice = lib.to_fleet_plan([{"template": "builder", "values": ex},
                                   {"template": "builder", "values": dict(ex, module="arch_other", suite="other")}])
        check("the same template twice gets distinct names", [a["name"] for a in twice] == ["builder", "builder-2"])
        try:
            lib.to_fleet_plan([{"template": "ux_pass", "values": lib.load("ux_pass")["examples"]},
                               {"template": "hub_builder", "values": lib.load("hub_builder")["examples"]}])
            check("two templates on one shared file are refused", False)
        except ValueError as e:
            check("two templates on one shared file are refused", "both own" in str(e), e)
        try:
            lib.to_fleet_plan(mixed, total_cap_usd=1.0)
            check("budgets past the cap are refused", False)
        except ValueError as e:
            check("budgets past the cap are refused", "above the cap" in str(e), e)
        cheap = lib.to_fleet_plan([{"template": "builder", "values": ex, "budget_usd": 0.5, "model": "claude-sonnet-5-5"}],
                                  total_cap_usd=1.0)
        check("budget and model can be set per item", cheap[0]["budget_usd"] == 0.5 and cheap[0]["model"] == "claude-sonnet-5-5")
        tests = lib.fleet_tests(mixed + [{"template": "builder", "values": ex}])
        check("fleet_tests: each command once, all accepted", tests == ["python arch_selftest_widget.py"], tests)

        # ---- a slot that stands for several files
        sp = lib.fill_parts("step_plan", **lib.load("step_plan")["examples"])
        check("a multi-file slot becomes separate files",
              sp["files"] == ["arch_autotune.py", "train_lora.py", "arch_selftest_tune2.py"], sp["files"])

        # ---- local agents
        lp = lib.to_fleet_plan([{"template": "builder", "values": ex, "kind": "local", "flow": "feature"}])
        check("a template can run as a local agent (goal, no cost, no common footer)",
              lp[0]["kind"] == "local" and "budget_usd" not in lp[0] and "Shared rules" not in lp[0]["goal"]
              and lp[0]["flow"] == "feature" and fl.plan_check(lp) == [], lp)
        try:
            lib.to_fleet_plan([{"template": "reviewer", "values": lib.load("reviewer")["examples"], "kind": "local"}])
            check("a read-only template cannot be a local agent", False)
        except ValueError as e:
            check("a read-only template cannot be a local agent", "read-only" in str(e), e)

        # ---- the format itself, in a private folder
        own = os.path.join(tmp, "tpl")
        os.makedirs(own)
        with open(os.path.join(own, "tiny.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: tiny\npurpose: p\nwhy: w\nbudget_usd: 1\nfiles: {thing}.py\ntests: python arch_selftest_{thing}.py\n"
                    "footer: none\nslot.thing: the thing\ndefault.thing: arch_tiny\n---\nMake {thing} with {{literal}} braces.\n")
        with open(os.path.join(own, "wrongname.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: other\npurpose: p\nbudget_usd: 1\nfiles: a.py\n---\nbody\n")
        check("defaults fill a slot; {{ }} are literal braces", lib.fill("tiny", folder=own) == "Make arch_tiny with {literal} braces.")
        check("a caller's value beats the default", lib.fill_parts("tiny", folder=own, thing="arch_x")["files"] == ["arch_x.py"])
        with open(os.path.join(own, "clash.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: clash\npurpose: p\nwhy: w\nbudget_usd: 1\nfiles: {folder}/a.py\nfooter: none\n"
                    "slot.folder: where\nexample.folder: x\n---\nWork in {folder}.\n")
        mine = {t["name"]: t for t in lib.list_templates(own)}
        check("a header naming another file shows as broken, the rest still list",
              mine["wrongname"]["kind"] == "broken" and mine["tiny"]["kind"] == "agent", mine)
        check("a slot named like the library's own argument (folder) is refused at load",
              mine["clash"]["kind"] == "broken" and "folder" in mine["clash"].get("error", ""), mine.get("clash"))
        check("leftovers() finds an unfilled slot", lib.leftovers("a {slot} b {{x}}") == ["{slot}"])
        check("the CLI lists without error", lib.main(["list"]) == 0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"no crash ({type(e).__name__}: {e})", False)
    finally:
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(tmp, ignore_errors=True)
    n, ok = len(results), sum(results)
    print(f"\n  {'ALL ' + str(n) + ' AGENTLIB CHECKS PASSED' if ok == n else str(n - ok) + ' of ' + str(n) + ' AGENTLIB CHECKS FAILED'}\n")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(run())
